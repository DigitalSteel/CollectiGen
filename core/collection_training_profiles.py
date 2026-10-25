"""Disconnected typed preparation adapter; no store, writer or trainer runner."""
from dataclasses import dataclass, asdict
from hashlib import sha256
import json

from .collection_training_version import (
    CollectionTrainingVersion, CollectionTrainingExportPlan, prepare_collection_training_export,
)
from .training_configuration import TrainingConfiguration
from .training_adapter_preview import SdxlLoraRecipe, SOURCE_REVISION
from .training_anima import (
    AnimaSettings, anima_optimizer_steps, SOURCE_REVISION as ANIMA_SOURCE_REVISION,
    BASE_ID, QWEN_ID, VAE_ID,
)
from .training_source_io import read_stable_training_source


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class PreparationProfile:
    profile_id: str
    trainer_revision: str
    rank: int = 32
    alpha: float = 16.0
    output_precision: str | None = "fp16"
    gradient_checkpointing: bool = True
    require_validation: bool = True


@dataclass(frozen=True)
class PreparationIssue:
    code: str
    level: str
    detail: str
    stable_id: str = ""


@dataclass(frozen=True)
class ResourceRequirement:
    role: str
    identity: str
    status: str = "not_verified"


@dataclass(frozen=True)
class ExportImage:
    stable_id: str
    role: str
    source_sha256: str
    caption: str
    caption_sha256: str
    provenance: bytes


@dataclass(frozen=True)
class ExportPreparationPlan:
    version: CollectionTrainingVersion
    profile: PreparationProfile
    images: tuple[ExportImage, ...]
    issues: tuple[PreparationIssue, ...]
    requirements: tuple[ResourceRequirement, ...]
    trainer_settings: bytes

    @property
    def ready_for_package_preview(self):
        return not any(issue.level == "blocking" for issue in self.issues)

    @property
    def execution_ready(self):
        return False  # Installed resources/native execution are a later authority.

    def to_dict(self):
        return {"schema_version": 1, "kind": "collection_trainer_preparation_candidate",
            "collection_version_id": self.version.version_id,
            "profile": asdict(self.profile),
            "images": [{**{key: value for key, value in asdict(image).items() if key != "provenance"},
                        "provenance": json.loads(image.provenance)} for image in self.images],
            "issues": [asdict(issue) for issue in self.issues],
            "requirements": [asdict(item) for item in self.requirements],
            "trainer_settings": json.loads(self.trainer_settings),
            "ready_for_package_preview": self.ready_for_package_preview,
            "execution_ready": False}

    @property
    def fingerprint(self):
        return sha256(encoded(self.to_dict())).hexdigest()

    def package_preview(self, store, current_plan, destination, **options):
        if not self.ready_for_package_preview:
            raise ValueError("Trainer preparation is held; review its named issues.")
        package = prepare_collection_training_export(self.version, store,
            self.version.to_dict()["collection"]["id"], current_plan, destination, **options)
        return ProfilePackagePreview(self, package)


@dataclass(frozen=True)
class ProfilePackagePreview:
    preparation: ExportPreparationPlan
    canonical: CollectionTrainingExportPlan

    @property
    def package_plan(self):
        return self.canonical.package_plan

    def to_dict(self):
        return {"kind": "collection_profile_package_preview", "schema_version": 1,
            "preparation": self.preparation.to_dict(),
            "preparation_fingerprint": self.preparation.fingerprint,
            "collection_version_id": self.canonical.version.version_id,
            "package_intent_fingerprint": self.package_plan.intent_fingerprint,
            "native_trainer_files_emitted": False, "execution_ready": False}


def _image(row, manifest):
    state = row["source_state"]
    return ExportImage(row["stable_id"], row["role"], state["file"]["sha256"] if state else "",
        row["caption"], row["caption_sha256"], encoded({"candidate": row,
            "profile": manifest["profile"], "profile_provenance": manifest["profile_provenance"]}))


def prepare(version, current_plan, profile: PreparationProfile, *, should_cancel=None):
    """Read only explicit plan paths; never discover or alter media/selection."""
    if not isinstance(profile, PreparationProfile):
        raise ValueError("An explicit typed preparation profile is required.")
    try:
        encoded(asdict(profile))
    except (TypeError, ValueError) as exc:
        raise ValueError("Profile fields must be finite JSON evidence.") from exc
    version = CollectionTrainingVersion.from_bytes(version.to_bytes())
    manifest = version.to_dict()["candidate_plan"]
    issues = []
    requirements = []
    settings = {"configuration": manifest["training_configuration"]}
    def hold(code, detail, stable_id=""):
        issues.append(PreparationIssue(code, "blocking", detail, stable_id))
    if not version.ready:
        hold("collection_version_held", ", ".join(version.issues))
    current_manifest = current_plan.to_manifest_dict()
    if current_manifest != manifest:
        hold("candidate_evidence_changed", "Rebuild/review a new immutable Collection version.")
    try:
        config = TrainingConfiguration.from_dict(manifest["training_configuration"])
    except (TypeError, ValueError):
        hold("configuration_missing_or_unsupported", "Retain evidence and rebuild through the canonical configuration owner.")
        rows = tuple(_image(row, manifest) for row in manifest["candidates"] if row["decision"] == "include")
        return ExportPreparationPlan(version, profile, rows, tuple(issues), (), encoded(settings))
    if type(profile.require_validation) is not bool:
        hold("unsupported_profile", "Validation policy must be explicit boolean evidence.")
    if config.output_kind != "lora":
        hold("unsupported_training_kind", "This candidate exposes LoRA preparation only.")
    if profile.profile_id == "sd-scripts.sdxl-lora":
        requirements.append(ResourceRequirement("base_model", config.base_model_id))
        requirements.append(ResourceRequirement("native_runtime", "sd-scripts/SDXL qualification required"))
        if profile.trainer_revision != SOURCE_REVISION:
            hold("unsupported_trainer_revision", "Only the inspected local adapter revision is represented.")
        if config.target_model_family.casefold() != "sdxl" or config.target_model_version not in {"", "1.0", "base-1.0"}:
            hold("unsupported_model", "The existing stock proposal is SDXL 1.0 LoRA only.")
        try:
            recipe = SdxlLoraRecipe(profile.rank, profile.alpha, profile.output_precision,
                                   profile.gradient_checkpointing)
            recipe.validate()
            settings["recipe"] = asdict(recipe)
        except (TypeError, ValueError) as exc:
            hold("unsupported_trainer_configuration", str(exc))
    elif profile.profile_id == "sd-scripts.anima-dit-lora":
        requirements.extend(ResourceRequirement(role, identity) for role, identity in (
            ("anima_base_v1", BASE_ID), ("qwen3_06b", QWEN_ID), ("qwen_image_vae", VAE_ID),
            ("llm_adapter_freeze", "Required by model guidance; native trainable-parameter proof required"),
            ("tokenizers", "Canonical Anima CONFIG_SHA256 and retained native qualification required")))
        if profile.trainer_revision != ANIMA_SOURCE_REVISION:
            hold("unsupported_trainer_revision", "Do not replace the existing bounded Anima source pin implicitly.")
        if config.target_model_family.casefold() != "anima" or config.base_model_id != BASE_ID:
            hold("unsupported_model", "The represented native profile is exact Anima Base v1.0.")
        try:
            training_count = sum(row["decision"] == "include" and row["role"] == "training"
                                 for row in manifest["candidates"])
            per_epoch, total_steps = anima_optimizer_steps(training_count, config.repeats,
                config.epochs, config.batch_size, config.gradient_accumulation)
            bounded = AnimaSettings(max_steps=total_steps, resolution=config.target_resolution,
                rank=profile.rank, seed=config.seed, learning_rate=config.learning_rate,
                batch_size=config.batch_size, gradient_accumulation=config.gradient_accumulation,
                repeats=config.repeats)
            bounded.validate()
            if (config.precision != "bf16" or profile.output_precision is not None
                    or profile.alpha != profile.rank or profile.gradient_checkpointing is not True):
                raise ValueError("Anima uses bf16 mixed precision, rank-matched alpha and checkpointing; explicit output precision is not translated.")
            settings["bounded_settings"] = bounded.payload()
            settings["optimizer_steps_per_epoch"] = per_epoch
            settings["scope"] = "DiT-only preparation intent; LLM freeze requires native proof; no full fine-tuning admission"
        except (TypeError, ValueError) as exc:
            hold("unsupported_trainer_configuration", str(exc))
    elif profile.profile_id == "musubi.krea2-raw-lora-research":
        requirements.extend(ResourceRequirement(role, identity) for role, identity in (
            ("raw_training_model", "krea/Krea-2-Raw@6b0ece7fffb640c5e3bcbe0a7f10f66b8e60a603"),
            ("turbo_inference_model", "krea/Krea-2-Turbo@98e0fe118d17c9e3547fbb2e25acdbae2cadf7c7"),
            ("qwen_image_vae", "Exact artifact identity unresolved"),
            ("qwen3_vl_4b_tokenizers", "Exact selected artifact hashes and native qualification required")))
        hold("trainer_adapter_unimplemented", "Musubi Krea2 research is not an installed/implemented CollectiGen adapter.")
        if profile.trainer_revision != "f8a1b03794a49239a3539015075f5123d6c07d66":
            hold("unsupported_trainer_revision", "Research evidence refers only to the recorded immutable revision.")
    else:
        hold("unsupported_profile", "No inspected preparation profile matches this trainer.")
    rows = []
    content_roles = {}
    split_roles = {}
    known_candidates = {candidate.stable_id: candidate for candidate in current_plan.candidates}
    cancelled = False
    inspect_sources = version.ready and current_manifest == manifest and not any(
        issue.level == "blocking" for issue in issues)
    for row in manifest["candidates"]:
        if row["decision"] != "include":
            continue
        state = row["source_state"]
        digest = state["file"]["sha256"] if state else ""
        rows.append(_image(row, manifest))
        if digest:
            content_roles.setdefault(digest, []).append(row["role"])
        if row.get("split_group"):
            split_roles.setdefault(row["split_group"], set()).add(row["role"])
        if inspect_sources and not cancelled:
            if should_cancel and should_cancel():
                hold("cancelled", "No further file inspection was performed.")
                cancelled = True
                continue
            candidate = known_candidates[row["stable_id"]]
            try:
                _, observation = read_stable_training_source(candidate.image)
                if observation["sha256"] != digest:
                    hold("stale_file", "Source content differs from the captured version.", row["stable_id"])
            except FileNotFoundError:
                hold("missing_file", "Restore this exact source identity before export.", row["stable_id"])
            except (OSError, RuntimeError):
                hold("unreadable_source", "Source is unavailable, redirected or changing.", row["stable_id"])
    for roles in content_roles.values():
        if len(roles) > 1:
            hold("exact_duplicate_images", "Repeated image content requires explicit collection review; no image was removed.")
        if len(set(roles)) > 1:
            hold("cross_split_content_leakage", "Identical content crosses train/validation/holdout roles.")
    if any(len(roles) > 1 for roles in split_roles.values()):
        hold("cross_split_group_leakage", "A captured duplicate/derivative split group crosses roles.")
    if not any(row.role == "training" for row in rows):
        hold("training_split_missing", "The explicit version needs an included training role.")
    if profile.require_validation and not any(row.role == "validation" for row in rows):
        hold("validation_split_missing", "Assign/review validation explicitly; this adapter never moves images between roles.")
    return ExportPreparationPlan(version, profile, tuple(rows), tuple(issues),
                                 tuple(requirements), encoded(settings))



def validate_profile_companion(value, *, candidate_plan_fingerprint=None):
    """One supported schema for both atomic writes and reloads; no draft imports."""
    import re
    from copy import deepcopy
    from .collection_training_version import _structural_bounds
    fields={"kind","schema_version","collection_version_id","collection_id",
        "collection_store_revision","candidate_plan_fingerprint","preparation_fingerprint",
        "preparation","registration_phase","execution_ready","companion_id"}
    _structural_bounds(value)
    if not isinstance(value,dict) or set(value)!=fields:
        raise ValueError("Unsupported companion registration evidence.")
    if (value["kind"]!="collection_profile_companion" or type(value["schema_version"]) is not int
            or value["schema_version"]!=1 or value["registration_phase"]!="draft_preparation_intent"
            or value["execution_ready"] is not False
            or not isinstance(value["collection_id"],str)
            or not re.fullmatch(r"concept\.[0-9a-f]{32}",value["collection_id"])):
        raise ValueError("Companion schema, phase or non-execution boundary is invalid.")
    for key in ("collection_version_id","collection_store_revision","candidate_plan_fingerprint",
                "preparation_fingerprint","companion_id"):
        if not isinstance(value[key],str) or not re.fullmatch("[0-9a-f]{64}",value[key]):
            raise ValueError("Companion identity digest is invalid.")
    if candidate_plan_fingerprint is not None and value["candidate_plan_fingerprint"]!=candidate_plan_fingerprint:
        raise ValueError("Companion candidate linkage differs.")
    prep=value["preparation"]
    required={"schema_version","kind","collection_version_id","profile","images","issues",
        "requirements","trainer_settings","ready_for_package_preview","execution_ready"}
    if (not isinstance(prep,dict) or set(prep)!=required
            or type(prep["schema_version"]) is not int or prep["schema_version"]!=1
            or prep["kind"]!="collection_trainer_preparation_candidate"
            or prep["collection_version_id"]!=value["collection_version_id"]
            or prep["execution_ready"] is not False
            or type(prep["ready_for_package_preview"]) is not bool
            or any(not isinstance(prep[key],list) for key in ("images","issues","requirements"))
            or not isinstance(prep["trainer_settings"],dict)):
        raise ValueError("Companion preparation schema or linkage is invalid.")
    try:
        profile=PreparationProfile(**prep["profile"])
        if asdict(profile)!=prep["profile"]:
            raise ValueError("Companion profile is not canonical.")
        encoded(asdict(profile))
    except (TypeError,ValueError) as exc:
        raise ValueError("Companion typed profile is invalid.") from exc
    for image in prep["images"]:
        if (not isinstance(image,dict) or set(image)!={"stable_id","role","source_sha256","caption","caption_sha256","provenance"}
                or not isinstance(image["stable_id"],str) or not image["stable_id"]
                or not isinstance(image["role"],str) or image["role"] not in {"training","validation","holdout"}
                or not isinstance(image["caption"],str)
                or not isinstance(image["source_sha256"],str)
                or (image["source_sha256"] and not re.fullmatch("[0-9a-f]{64}",image["source_sha256"]))
                or not isinstance(image["caption_sha256"],str)
                or image["caption_sha256"] not in {"",sha256(image["caption"].encode("utf-8")).hexdigest()}
                or not isinstance(image["provenance"],dict)
                or set(image["provenance"])!={"candidate","profile","profile_provenance"}):
            raise ValueError("Companion image evidence is invalid.")
        if any(not isinstance(image["provenance"][key],dict) for key in image["provenance"]):
            raise ValueError("Companion image provenance is invalid.")
    for issue in prep["issues"]:
        if (not isinstance(issue,dict) or set(issue)!={"code","level","detail","stable_id"}
                or any(not isinstance(item,str) for item in issue.values())
                or issue["level"] not in {"blocking","caution"}):
            raise ValueError("Companion issue evidence is invalid.")
    if prep["ready_for_package_preview"]!= (not any(issue["level"]=="blocking" for issue in prep["issues"])):
        raise ValueError("Companion preparation readiness contradicts its holds.")
    for requirement in prep["requirements"]:
        if (not isinstance(requirement,dict) or set(requirement)!={"role","identity","status"}
                or any(not isinstance(item,str) for item in requirement.values())
                or requirement["status"]!="not_verified"):
            raise ValueError("Companion resource requirement is invalid.")
    unsigned={key:item for key,item in value.items() if key!="companion_id"}
    if (sha256(encoded(unsigned)).hexdigest()!=value["companion_id"]
            or sha256(encoded(prep)).hexdigest()!=value["preparation_fingerprint"]):
        raise ValueError("Companion or preparation fingerprint does not match.")
    return deepcopy(value)
