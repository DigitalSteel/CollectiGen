"""Read-only Krea 2 package loss reports; no export, recipe or runner."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json

from .model_resource_registry import ModelResourceRegistry
from .training_adapter_preview import AdapterImage, AdapterRequirement, AdapterSetting, _package_states
from .training_configuration import TrainingConfiguration
from .training_package import TrainingPackageService, TrainingPackageVerification
from .training_source_io import _assert_unredirected, _identity


KREA_SOURCE_REVISION = "db3984fbc6e13b34c0064990fc2d95ac64d00058"
TRAINING_SOURCE_REVISION = "2a9417c30ae556b8d8864ad2d5a6068d221dc871"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _cancel(callback):
    if callback and callback():
        raise InterruptedError("Krea package report was cancelled.")


@dataclass(frozen=True)
class KreaTrainingCompatibilityReport:
    package_fingerprint: str
    manifest_json: str
    krea_source_revision: str
    training_source_revision: str
    base_resource_id: str
    base_status: str
    images: tuple[AdapterImage, ...]
    excluded_ids: tuple[str, ...]
    settings: tuple[AdapterSetting, ...]
    requirements: tuple[AdapterRequirement, ...]
    fingerprint: str

    @property
    def execution_ready(self) -> bool:
        return False


class KreaTrainingCompatibilityService:
    """Inspect only an explicit package and its exact registered base.

    Construction is inert. The expected verification is a snapshot, never a
    validity grant. No output paths, source datasets or remote resources are read.
    """

    def report(self, expected_package: TrainingPackageVerification,
               registry: ModelResourceRegistry, *, progress=None,
               should_cancel=None, limits=None) -> KreaTrainingCompatibilityReport:
        _cancel(should_cancel)
        if (expected_package.valid is not True or not isinstance(expected_package.manifest, dict)
                or not isinstance(expected_package.package_fingerprint, str)
                or len(expected_package.package_fingerprint) != 64):
            raise ValueError("Start from a verified training package.")
        # Freeze caller-owned mutable evidence before callbacks or verification.
        expected = replace(expected_package, manifest=json.loads(_json(expected_package.manifest)))
        before = _package_states(expected, should_cancel)
        if progress:
            progress(0, 0, "Verifying the exact package for the Krea compatibility report")
        _cancel(should_cancel)
        current = TrainingPackageService().verify(expected.destination, should_cancel=should_cancel, limits=limits)
        _cancel(should_cancel)
        if current.inspection_limited:
            raise ValueError("The package exceeds the review limits. Choose a smaller package.")
        if not current.valid or current.package_fingerprint != expected.package_fingerprint:
            raise ValueError("The training package changed or failed verification. Verify or rebuild it first.")
        if current.manifest != expected.manifest:
            raise ValueError("The expected inventory differs from the verified manifest. Verify it again.")
        config = TrainingConfiguration.from_dict(current.manifest["training_configuration"])
        if config.target_model_family.casefold() not in {"krea 2", "krea2"} or config.output_kind != "lora":
            raise ValueError("This report covers Krea 2 LoRA packages only.")
        registered_base = registry.snapshot()[0]["resources"].get(config.base_model_id)
        resolved = registry.resolve(config.base_model_id, role="checkpoint", family="Krea 2",
                                    progress=progress, should_cancel=should_cancel)
        manifest = current.manifest
        artifacts = {item["path"]: item for item in manifest["artifacts"]}
        images = []
        for item in sorted(manifest["package_plan"]["items"], key=lambda item: item["stable_id"]):
            _cancel(should_cancel)
            image, caption = artifacts[item["image_path"]], artifacts[item["caption_path"]]
            width, height = image["transform"]["output_dimensions"]
            images.append(AdapterImage(item["stable_id"], item["role"], item["image_path"], image["sha256"],
                item["caption_path"], caption["sha256"], caption["package_caption"], width, height,
                item["effective_exposures"]))
        requirements = (
            AdapterRequirement("base_identity", "Exact training base evidence", resolved.reason,
                "Keep or register the exact local base identity; declared family and matching bytes do not verify RAW architecture or loader compatibility."),
            AdapterRequirement("variant_and_companions", "RAW, Turbo and companions remain unqualified",
                "Training targets RAW. Turbo validation is a separate resource; checkpoint revisions, encoder, VAE and tokenizer bindings are unresolved.",
                "Bind each exact resource and its acquisition authority separately before a future export. Do not treat training-model fallback as qualified Turbo validation."),
            AdapterRequirement("caption_staging", "Direct package-folder input is incompatible",
                "The upstream folder reader opens TXT sidecars as images and uses a shared instance prompt. Per-image captions require its dataset-column branch.",
                "Design a separate explicit dataset-column translation preserving these exact image and caption hashes; this report writes no staging files."),
            AdapterRequirement("roles_and_weights", "Recorded roles and exposures are not a trainer sampler",
                "Training, validation, holdout and excluded identities remain distinct. Generated validation samples do not implement package evaluation roles.",
                "Keep only reviewed training items in a future training translation; qualify exposure, split and sampler semantics without silently duplicating or dropping items."),
            AdapterRequirement("geometry_and_tokens", "Geometry and caption tokens need qualification",
                "Resolution, buckets, crops, upscale, transforms and token limits have no qualified neutral-to-Krea mapping. No caption is rewritten or truncated.",
                "Qualify exact tokenizer and geometry behavior before accepting a translation; do not borrow SDXL defaults."),
            AdapterRequirement("output_custody", "Output ownership is unresolved",
                "This report neither inspects nor reserves an output destination. Upstream accepts existing output directories.",
                "A future runner must exclusively reserve an unused destination through existing process and recovery owners."),
            AdapterRequirement("strict_resume", "Resume and retention authority are unresolved",
                "Upstream latest with no checkpoint can start fresh; configured retention can delete old checkpoints. Final LoRA weights are not resumable state.",
                "Require an exact stopped-run/checkpoint identity, refuse missing resume state, and obtain explicit retention authority; never adopt silent fresh fallback."),
            AdapterRequirement("external_effects", "External effects require separate choices",
                "Remote component loading, tracking and Hub transfer exist upstream. This report performs none of them.",
                "Keep acquisition, tracking and transfer opt-ins separate; no credential or network action is authorized by this report."),
            AdapterRequirement("runtime_and_fit", "Runtime, machine fit and results are unmeasured",
                "Pinned source is not an installed runtime, recipe, hardware measurement, produced LoRA or quality result.",
                "Retain manual handoff and existing runtime/native/package gates; do not launch from this report."),
        )
        settings = _settings(manifest["training_configuration"], config, should_cancel)
        payload = dict(package_fingerprint=current.package_fingerprint, manifest_json=_json(manifest),
            krea_source_revision=KREA_SOURCE_REVISION, training_source_revision=TRAINING_SOURCE_REVISION,
            base_resource_id=resolved.resource_id, base_status=resolved.status,
            images=tuple(images), excluded_ids=tuple(sorted(item["stable_id"] for item in manifest["excluded"])),
            settings=settings, requirements=requirements)
        fingerprint = hashlib.sha256(_json({k: [asdict(x) for x in v] if k in {"images", "settings", "requirements"}
                                            else v for k, v in payload.items()}).encode("utf-8")).hexdigest()
        if _package_states(expected, should_cancel) != before:
            raise ValueError("The training package changed during the report. Verify or rebuild it first.")
        if resolved.inspection is not None:
            _assert_unredirected(resolved.inspection.path)
            try:
                state = resolved.inspection.path.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ValueError("The base model disappeared during the report. Inspect it again.") from exc
            if _identity(state) != resolved.inspection.file_state:
                raise ValueError("The base model changed during the report. Inspect it again.")
        if registry.snapshot()[0]["resources"].get(config.base_model_id) != registered_base:
            raise ValueError("The base model registry entry changed during the report. Review it again.")
        _cancel(should_cancel)
        return KreaTrainingCompatibilityReport(**payload, fingerprint=fingerprint)


def _settings(raw, config, should_cancel):
    """Account for raw fields as well as canonical fields without emitting config."""
    known = config.to_dict()
    result = []
    for section, value in sorted(raw.items()):
        for key, requested in sorted(value.items()) if isinstance(value, dict) else (("", value),):
            _cancel(should_cancel)
            field = f"{section}.{key}" if key else section
            status, proposed = "retained", "Exact manifest evidence; no trainer setting emitted"
            reason = "Preserved provenance or package evidence; no executable translation or recommendation."
            if section not in known or (key and (not isinstance(known[section], dict) or key not in known[section])):
                status, reason = "unresolved", "Unknown future field retained exactly; no interpretation or silent conversion."
            elif section in {"image_preparation", "optimization_request", "weighting"}:
                status, reason = "unresolved", "Requested value retained; upstream geometry, optimizer, exposure or sampler equivalence needs qualification."
            elif field == "captions.max_tokens":
                status, reason = "unsupported", "No qualified tokenizer limit mapping; captions remain exact, without truncation."
            elif section == "target":
                status, reason = "unresolved", "Exact byte resolution is separate from RAW/Turbo revision, architecture and loader qualification."
            elif section == "adapter":
                status, reason = "unsupported", "The manual package adapter identity does not admit a Krea trainer or executable export."
            elif section == "model_guidance":
                reason = "Preserved guidance and resource evidence; no companion is automatically acquired or admitted."
            result.append(AdapterSetting(field, status, _json(requested), proposed, reason))
    return tuple(result)
