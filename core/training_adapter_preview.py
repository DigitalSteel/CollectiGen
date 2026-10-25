"""Read-only SDXL LoRA proposals from verified packages; never a trainer runner."""
from __future__ import annotations

import hashlib
import json
import math
import stat
from dataclasses import asdict, dataclass, fields
from pathlib import Path, PurePosixPath

from .model_resource_registry import ModelResourceRegistry, ResourceResolution
from .training_configuration import TrainingConfiguration
from .training_package import MANIFEST_NAME, TrainingPackageService, TrainingPackageVerification, _child, _inspection_read
from .training_source_io import _assert_unredirected, _identity
from .training_runtime_contract import ADAPTER_ID, ADAPTER_VERSION, validate_configuration
from . import training_package_effects as effects


SOURCE_REVISION = "6721028c79ee85a78b3a06dfd8954dae310a1cce"
MAX_ITEMS = effects.MAX_ITEMS


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cancel(callback):
    if callback and callback():
        raise InterruptedError("Trainer preview was cancelled.")


@dataclass(frozen=True)
class SdxlLoraRecipe:
    """Explicit starting proposal, not a measured hardware or quality recipe."""
    rank: int = 32
    alpha: float = 16.0
    output_precision: str = "fp16"
    gradient_checkpointing: bool = True
    schema_version: int = 1

    def validate(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("This LoRA proposal version is unsupported.")
        if type(self.rank) is not int or not 4 <= self.rank <= 128:
            raise ValueError("This proposal supports LoRA rank 4–128.")
        if type(self.alpha) not in {float, int} or not math.isfinite(self.alpha) or not 1 <= self.alpha <= self.rank:
            raise ValueError("Choose alpha from 1 through the selected rank.")
        if not isinstance(self.output_precision, str) or self.output_precision not in {"fp16", "bf16", "float"} or type(self.gradient_checkpointing) is not bool:
            raise ValueError("Choose a supported output precision and checkpointing policy.")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("LoRA proposal settings contain missing or unsupported fields.")
        result = cls(**value)
        result.validate()
        return result


@dataclass(frozen=True)
class AdapterRequirement:
    code: str
    summary: str
    reason: str
    next_action: str


@dataclass(frozen=True)
class AdapterSetting:
    field: str
    disposition: str
    requested: str
    proposed: str
    reason: str


@dataclass(frozen=True)
class AdapterImage:
    stable_id: str
    role: str
    image_path: str
    image_sha256: str
    caption_path: str
    caption_sha256: str
    caption: str
    width: int
    height: int
    effective_exposures: int
    effects: effects.ImageEffectAssessment | None = None


@dataclass(frozen=True)
class TrainingAdapterPreview:
    package_fingerprint: str
    manifest_json: str
    recipe: SdxlLoraRecipe
    base_id: str
    base_status: str
    base_path: str
    base_state: tuple[int, ...]
    configuration_toml: str
    dataset_json: str
    images: tuple[AdapterImage, ...]
    settings: tuple[AdapterSetting, ...]
    requirements: tuple[AdapterRequirement, ...]
    excluded_ids: tuple[str, ...]
    fingerprint: str
    goal: str = ""
    image_effects_profile_sha256: str = ""

    @property
    def execution_ready(self) -> bool:
        # This record cannot authorize execution, even if all user-editable
        # settings are structurally valid. No production runner consumes it.
        return False


def verified_adapter_image_path(package: TrainingPackageVerification,
                                preview: TrainingAdapterPreview,
                                image: AdapterImage, *, should_cancel=None) -> Path:
    """Resolve one exact packaged image only after a fresh bounded byte check.

    This is a viewing check, not permission to alter the frozen package or the
    source Image Knowledge. The caller must discard the path when selection or
    preview changes.
    """
    if not package.valid or package.package_fingerprint != preview.package_fingerprint:
        raise ValueError("The training package review is no longer current. Verify it again.")
    if not any(candidate is image for candidate in preview.images):
        raise ValueError("Choose an image from the current training package review.")
    artifacts = package.manifest.get("artifacts") if isinstance(package.manifest, dict) else None
    if not isinstance(artifacts, list):
        raise ValueError("The training package inventory is unavailable. Verify it again.")
    matches = [item for item in artifacts if isinstance(item, dict)
               and item.get("path") in {image.image_path, image.caption_path}]
    by_path = {item.get("path"): item for item in matches}
    if (len(matches) != 2 or len(by_path) != 2
            or by_path[image.image_path].get("kind") != "image"
            or by_path[image.image_path].get("sha256") != image.image_sha256
            or by_path[image.caption_path].get("kind") != "caption"
            or by_path[image.caption_path].get("sha256") != image.caption_sha256):
        raise ValueError("The selected image or caption differs from the reviewed package inventory.")
    path = _child(Path(package.destination).absolute(), image.image_path)
    caption_path = _child(Path(package.destination).absolute(), image.caption_path)
    try:
        payload, _state = _inspection_read(path, 64 * 1024 * 1024,
            should_cancel or (lambda: False))
        caption_payload, _caption_state = _inspection_read(caption_path, 64 * 1024 * 1024,
            should_cancel or (lambda: False))
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("The selected packaged image is missing or unavailable. Verify or rebuild the package.") from exc
    if (hashlib.sha256(payload).hexdigest() != image.image_sha256
            or hashlib.sha256(caption_payload).hexdigest() != image.caption_sha256):
        raise ValueError("The selected packaged image or caption changed. Verify or rebuild the package before viewing it.")
    return path


def _package_states(package: TrainingPackageVerification, cancel=None):
    """Guard the expected immutable inventory before/after independent verification."""
    root = Path(package.destination).absolute()
    _assert_unredirected(root)
    root_state = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_state.st_mode):
        raise ValueError("The selected package is not a local directory.")
    artifacts = package.manifest.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_ITEMS * 2 + 32:
        raise ValueError("The package inventory exceeds the supported preview scope.")
    paths = [MANIFEST_NAME]
    for item in artifacts:
        value = item.get("path") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value or "\\" in value or ":" in value:
            raise ValueError("The expected package inventory has an unsafe path.")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("The expected package inventory has an unsafe path.")
        paths.append(value)
    if len(set(paths)) != len(paths):
        raise ValueError("The expected package inventory repeats an artifact.")
    result = [(".", _identity(root_state))]
    for relative in paths:
        _cancel(cancel)
        path = root / relative
        _assert_unredirected(path)
        state = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(state.st_mode) or int(getattr(state, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
            raise ValueError("A package artifact is not a fully local regular file.")
        result.append((relative, _identity(state)))
    return tuple(result)


class SdScriptsSdxlLoraPreviewService:
    """Revalidate a prior package result, resolve its base and explain a proposal.

    The prior result is an expected snapshot, never trusted as current evidence.
    No package, source, registry, config, environment or output file is written.
    """
    def preview(self, package: TrainingPackageVerification, registry: ModelResourceRegistry,
                recipe: SdxlLoraRecipe = SdxlLoraRecipe(), *, progress=None, should_cancel=None) -> TrainingAdapterPreview:
        recipe.validate()
        if not package.valid or not isinstance(package.manifest, dict) or not isinstance(package.package_fingerprint, str) or len(package.package_fingerprint) != 64:
            raise ValueError("Start from a verified training package.")
        before = _package_states(package, should_cancel)
        if progress:
            progress(0, 0, "Verifying the exact training package")
        current = TrainingPackageService().verify(package.destination, should_cancel=should_cancel)
        _cancel(should_cancel)
        if not current.valid or current.package_fingerprint != package.package_fingerprint:
            raise ValueError("The training package changed or failed verification. Verify or rebuild it first.")
        if current.manifest != package.manifest:
            raise ValueError("The cached package inventory differs from its verified manifest. Verify it again.")
        config = TrainingConfiguration.from_dict(current.manifest["training_configuration"])
        config.require_single_checkpoint_adapter()
        if config.target_model_family.casefold() != "sdxl" or config.target_model_version not in {"", "1.0", "base-1.0"} or config.output_kind != "lora":
            raise ValueError("This proposal covers SDXL 1.0 ordinary LoRA only. The manual package remains available.")
        if progress:
            progress(0, 0, "Verifying the exact registered base model")
        resolved = registry.resolve(config.base_model_id, role="checkpoint", family="SDXL",
                                    progress=progress, should_cancel=should_cancel)
        result = self._build_verified(current, config, resolved, recipe, should_cancel)
        # Reference assessment and fingerprinting can take time for 50,000
        # records. The final stale check must follow that work, not precede it.
        if _package_states(package, should_cancel) != before:
            raise ValueError("The training package changed during preview. Verify or rebuild it first.")
        if resolved.inspection is not None:
            _assert_unredirected(resolved.inspection.path)
            if _identity(resolved.inspection.path.stat(follow_symlinks=False)) != resolved.inspection.file_state:
                raise ValueError("The base model changed during preview. Inspect it again.")
        _cancel(should_cancel)
        return result

    @staticmethod
    def _build_verified(package, config, resolved, recipe, should_cancel=None):
        """Pure projection after the service's package/resource guards."""
        manifest = package.manifest
        planned = manifest["package_plan"]["items"]
        if len(planned) > MAX_ITEMS:
            raise ValueError("This trainer preview supports at most 50,000 included images.")
        artifacts = {item["path"]: item for item in manifest["artifacts"]}
        images = []
        for item in planned:
            _cancel(should_cancel)
            image, caption = artifacts[item["image_path"]], artifacts[item["caption_path"]]
            width, height = image["transform"]["output_dimensions"]
            images.append(AdapterImage(item["stable_id"], item["role"], item["image_path"], image["sha256"],
                item["caption_path"], caption["sha256"], caption["package_caption"], width, height, item["effective_exposures"],
                effects.assess_image(image, caption, item["role"], config)))
        inspection = resolved.inspection
        base_path = str(inspection.path) if inspection is not None else ""
        proposal = {
            "network_module": "networks.lora", "network_dim": recipe.rank, "network_alpha": float(recipe.alpha),
            "network_train_unet_only": True, "optimizer_type": "AdamW", "lr_scheduler": "constant",
            "max_train_epochs": config.epochs, "gradient_accumulation_steps": config.gradient_accumulation,
            "learning_rate": config.learning_rate, "mixed_precision": {"float32": "no"}.get(config.precision, config.precision),
            "seed": config.seed, "gradient_checkpointing": recipe.gradient_checkpointing,
            "save_model_as": "safetensors", "save_precision": recipe.output_precision, "output_name": config.output_name,
            "no_half_vae": True, "cache_latents": False, "cache_latents_to_disk": False,
            "max_data_loader_n_workers": 0, "persistent_data_loader_workers": False,
            "save_state": False, "save_state_on_train_end": False,
        }
        if base_path:
            proposal["pretrained_model_name_or_path"] = base_path
        validate_configuration(proposal)
        dataset = effects.dataset_proposal(Path(package.destination).absolute() / "images" / "training", config)
        requirements = [
            AdapterRequirement("trainer_runtime_unverified", "Trainer installation is unverified", "The proposal targets pinned source, not a qualified installed parser or runtime.", "Keep using the manual package. Installation validation for this trainer is not available in this build."),
            AdapterRequirement("tokenizers_unverified", "Caption token handling needs verification", f"The requested limit of {config.max_caption_tokens} tokens has no verified mapping to both SDXL tokenizers. No max_token_length is emitted.", "Keep package captions unchanged. Trainer-specific token measurement is not yet available."),
            AdapterRequirement("geometry_unverified", "Package image effects need runtime verification", "Generated reference cases now identify package-specific risks. A reference match does not prove this package's actual trainer pixels, captions or batches.", "Use Images & Captions to filter images needing review and inspect reference predictions. Qualified execution of this package remains unavailable."),
            AdapterRequirement("dataset_schema_unverified", "Dataset configuration needs verification", "The image reference exercised dataset constructors. This expanded JSON proposal and omitted defaults have not been qualified through the installed dataset configuration parser.", "Treat Configuration Detail as incomplete review text. It cannot be used to authorize a trainer run."),
            AdapterRequirement("offline_components_unverified", "Local model components are unverified", "Base identity does not verify the selected tokenizers or encoders.", "Component checks are not yet connected to this preview. Required resources will need setup before local training."),
            AdapterRequirement("output_process_unqualified", "Managed training and recovery are unavailable", "This proposal has no reserved output directory, command, qualified process owner or result receipt. Final weights are not a resumable checkpoint.", "Continue with the manual package. Managed launch and checkpoint recovery are not available in this build."),
            AdapterRequirement("machine_fit_unmeasured", "Machine fit and outcome are unmeasured", "The starting recipe has no applicable training memory, duration or output-quality measurement.", "Use Setup to review machine resources. Memory and duration recommendations for this trainer are not yet available."),
        ]
        for code, label in effects.HOLD_LABELS.items():
            count = sum(code in image.effects.holds for image in images)
            if count:
                requirements.append(AdapterRequirement("image_policy_" + code, f"{count:,} training images: {label.lower()}",
                    "Assessment uses this exact verified package and the finite generated reference profile. No image has been changed or approved for a lossy treatment.",
                    "Open Images & Captions and choose Needs image review. Select a row for its specific effects and unchanged caption."))
        if resolved.status != "identity_verified":
            requirements.insert(0, AdapterRequirement("base_unavailable", "Exact local base model is unavailable", resolved.reason,
                "Choose and verify the base in Training Setup, then rebuild the immutable package if its ID changes."))
        else:
            requirements.insert(0, AdapterRequirement("base_compatibility_unverified", "Base model compatibility is unverified",
                "The local bytes match the registered SHA-256. The declared SDXL family is not an architecture or loader test.",
                "Keep the exact model identity. This build cannot certify its compatibility with the trainer."))
        if any(item.role != "training" for item in images):
            requirements.append(AdapterRequirement("evaluation_roles_unavailable", "Evaluation images remain separate",
                "Validation and holdout identities are retained and omitted from the proposed training subset.",
                "Keep the reviewed split; do not replace it with random trainer validation. Evaluation support needs its own qualification."))
        if not config.target_model_version:
            requirements.append(AdapterRequirement("target_version_unconfirmed", "Target version needs confirmation",
                "The package declares SDXL but no exact target version; this proposal covers SDXL 1.0 only.",
                "Confirm the intended version in Training Setup and rebuild the package when its settings change."))

        neutral = config.to_dict()
        dispositions = {}
        for section, value in neutral.items():
            for key, requested in (value.items() if isinstance(value, dict) else (("", value),)):
                field = f"{section}.{key}" if key else section
                dispositions[field] = AdapterSetting(field, "retained", _json(requested), "Companion manifest",
                    "Retained exactly as CollectiGen evidence; not silently converted to a trainer option.")
        def mark(field, status, proposed, reason):
            old = dispositions[field]
            dispositions[field] = AdapterSetting(field, status, old.requested, proposed, reason)
        for field, key in (("weighting.epochs", "max_train_epochs"), ("optimization_request.gradient_accumulation", "gradient_accumulation_steps"),
                           ("optimization_request.learning_rate", "learning_rate"), ("optimization_request.seed", "seed")):
            mark(field, "translated", f"{key} = {_json(proposal[key])}", "Numeric value retained; training behavior and cross-device equivalence remain unverified.")
        for field, key, value in (("weighting.repeats", "num_repeats", config.repeats), ("optimization_request.batch_size", "batch_size", config.batch_size)):
            mark(field, "translated", f"{key} = {value}", "Training subset only; repeats, partial batches and optimizer steps need runtime verification.")
        for field, proposed in (("image_preparation.target_resolution", str(config.target_resolution)),
                                ("image_preparation.bucket_policy", str(dataset["datasets"][0]["enable_bucket"])),
                                ("image_preparation.allow_upscale", str(dataset["datasets"][0]["bucket_no_upscale"])),
                                ("optimization_request.precision", proposal["mixed_precision"])):
            mark(field, "approximated", proposed, "Trainer-specific semantics require qualification; no equivalence or machine-fit claim.")
        mark("target.base_model_id", "identity_verified" if inspection else "unavailable", config.base_model_id,
             "Only a current full SHA-256 can resolve a local file. Compatibility remains unverified.")
        mark("image_preparation.crop_policy", "unsupported", "random_crop = false", "Disabling random crop does not prove no crop or exact centered-crop geometry.")
        mark("captions.max_tokens", "unsupported", "No tokenizer limit emitted", "The neutral request is retained without truncating, rewriting or inventing tokenizer measurements.")
        mark("captions.prefix_policy", "retained", "Exact package caption once", "The package already applied trigger/class policy. The trainer proposal adds no second prefix.")
        mark("weighting.validation_policy", "unsupported", "Separate recorded roles", "No random split, validation or holdout images enter the training subset.")
        for key, value in proposal.items():
            if key in {"max_train_epochs", "gradient_accumulation_steps", "learning_rate", "mixed_precision", "seed", "pretrained_model_name_or_path"}:
                continue
            dispositions[f"recipe.{key}"] = AdapterSetting(f"recipe.{key}", "tool_specific", "Explicit starting proposal", _json(value),
                "Versioned adapter choice; not a measured quality or hardware recommendation. Custom code/modules are not accepted.")
        dispositions["recipe.checkpoint_policy"] = AdapterSetting("recipe.checkpoint_policy", "tool_specific", "Final weights only",
            "No periodic saves or resumable state proposed", "Stop or failure may lose progress; checkpoint/resume qualification remains separate.")
        comment = "# REVIEW ONLY: incomplete proposal; no output reservation, dataset config file, tokenizer mapping or launch authorization.\n"
        toml = comment + "\n".join(f"{_json(key)} = {_json(value)}" for key, value in sorted(proposal.items())) + "\n"
        manifest_json = _json(manifest)
        dataset_json = _json(dataset)
        effects_profile = effects.profile_sha256()
        excluded = tuple(str(row["stable_id"]) for row in manifest["candidate_plan"]["candidates"] if row["decision"] != "include")
        fingerprint = _hash(_json({"adapter": ADAPTER_ID, "version": ADAPTER_VERSION, "source": SOURCE_REVISION,
            "manifest": _hash(manifest_json), "recipe": asdict(recipe), "base_status": resolved.status,
            "base_path": base_path, "base_state": inspection.file_state if inspection else (),
            "configuration": toml, "dataset": dataset_json, "image_effects_profile": effects_profile,
            "image_effects": [asdict(image.effects) for image in images]}))
        return TrainingAdapterPreview(package.package_fingerprint, manifest_json, recipe, config.base_model_id, resolved.status,
            base_path, inspection.file_state if inspection else (), toml, dataset_json, tuple(images),
            tuple(dispositions.values()), tuple(requirements), excluded, fingerprint, goal=manifest["candidate_plan"]["goal"]["text"],
            image_effects_profile_sha256=effects_profile)
