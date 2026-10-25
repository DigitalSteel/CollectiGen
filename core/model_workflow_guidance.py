"""Offline, version-aware model workflow guidance without execution authority."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .dependencies import nvidia_gpu_profile
from .external_write_transaction import atomic_verified_bytes
from .export_profile import ExportProfile, export_profile_by_id
from .onboarding import validate_locations
from .model_resource_registry import RESOURCE_ROLES, ModelResourceError, _assert_unredirected, _bounded_read as _read_guarded_bytes
from .training_configuration import (
    RECOMMENDED_SETTING_FIELDS,
    TrainingConfiguration,
    TrainingSettingRecommendation,
    model_guidance_evidence_json,
    recommended_training_configuration,
    validate_training_configuration,
)
from .training_presets import preview_training_preset


BASELINE_PATH = Path(__file__).resolve().parents[1] / "data" / "model_workflow_baseline.json"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 256
MAX_RECOVERY_RECEIPTS = 64
SUPPORTED_SCHEMA_VERSION = 1
PRIORITIES = {"balanced", "turnaround", "lower_resources", "fidelity"}
WORKFLOW_KINDS = {"training", "generation"}
REQUIREMENT_USES = {"training", "generation", "validation"}
REQUIREMENT_LEVELS = {"required", "recommended", "optional"}
COMPATIBILITY_STATES = {"tested", "documented", "not_assessed"}
KNOWN_RECORD_FIELDS = {
    "id", "family", "version", "variant", "modality", "goal_kinds",
    "catalog_status", "license", "summary", "sources", "recipe", "limitations",
    "companion_requirements", "generation_recipe", "user_notes", "user_overrides",
}

RECIPE_FIELDS = {
    "id", "version", "method", "caption_profile_id", "caption_profile_version",
    "caption_profile_base_id", "caption_strategy", "resolution",
    "minimum_storage_gib", "minimum_ram_gib", "minimum_vram_gib",
    "required_companions", "adapter", "limitations",
}
GENERATION_RECIPE_FIELDS = {
    "id", "version", "process", "configuration", "editable_fields",
    "configuration_evidence", "conditioning_inputs", "required_runtime_settings", "adapter",
    "resource_evidence_status",
    "minimum_storage_gib", "minimum_ram_gib", "minimum_vram_gib", "limitations",
}
ADAPTER_FIELDS = {"id", "version", "source_revision", "status", "runtime_policy"}
GENERATION_CONFIGURATION_FIELDS = {
    "width", "height", "steps", "cfg_scale", "sampler", "scheduler",
}
COMPANION_REQUIREMENT_FIELDS = {
    "slot", "role", "family", "variant", "revision", "revision_note", "applies_to", "necessity",
    "compatibility_status", "accepted_sha256",
}


class ModelWorkflowKnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class MachineCapabilityProfile:
    """A bounded snapshot assembled from Setup and the shared job coordinator."""

    cpu_threads: int | None = None
    ram_gib: float | None = None
    gpu_name: str = ""
    gpu_backend: str = "unknown"
    gpu_vram_gib: float | None = None
    free_storage_gib: float | None = None
    competing_gpu_jobs: int = 0
    observed_at: str = ""
    evidence: str = "detected"
    measurement_fingerprint: str = ""
    current_fingerprint: str = ""

    @property
    def measurement_current(self) -> bool:
        return bool(
            self.measurement_fingerprint
            and self.measurement_fingerprint == self.current_fingerprint
        )


@dataclass(frozen=True)
class GuidanceRequest:
    goal: str
    goal_kind: str = "concept"
    target_id: str = ""
    priority: str = "balanced"
    base_model_id: str = ""
    workflow_kind: str = "training"
    resource_bindings: tuple[ResourceBinding, ...] = ()


@dataclass(frozen=True)
class ResourceBinding:
    """A caller-selected exact registry identity for one catalog slot."""

    slot: str
    resource_id: str


@dataclass(frozen=True)
class CompanionRequirement:
    slot: str
    role: str
    family: str
    variant: str
    revision: str
    revision_note: str
    applies_to: tuple[str, ...]
    necessity: str
    compatibility_status: str
    accepted_sha256: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompanionAssessment:
    slot: str
    resource_id: str
    availability: str
    verification: str
    compatibility: str
    blocking: bool
    summary: str

    @property
    def ready(self) -> bool:
        return (
            self.availability == "selected"
            and self.verification == "verified"
            and self.compatibility == "matched"
        )


@dataclass(frozen=True)
class CompanionGuidanceProjection:
    """Catalog claims for an explicitly chosen profile; never adapter authority."""
    target_id: str
    workflow_kind: str
    catalog_fingerprint: str
    known: bool
    family: str
    version: str
    sources: tuple[str, ...]
    requirements: tuple[CompanionRequirement, ...]
    assessments: tuple[CompanionAssessment, ...]


@dataclass(frozen=True)
class GenerationAdapterAvailability:
    """Exact live identity returned by an independently admitted adapter probe."""

    adapter_id: str
    adapter_version: int
    runtime_version: str
    runtime_fingerprint: str


@dataclass(frozen=True)
class GenerationConfigurationRecommendation:
    """A transient, generator-neutral patch candidate; GenerationPlan owns saves/runs."""

    target_model_family: str
    target_model_version: str
    recipe_id: str
    recipe_version: int
    process: str
    width: int
    height: int
    steps: int
    cfg_scale: float | None
    sampler: str
    scheduler: str
    recommendation_scope: str
    runtime_version: str = ""
    runtime_fingerprint: str = ""
    override_reason: str = ""
    explicit_fields: tuple[str, ...] = ()
    setting_evidence: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_request(
        cls, *, width: int, height: int, steps: int, cfg_scale: float | None,
        sampler: str, scheduler: str,
    ) -> GenerationConfigurationRecommendation:
        """Capture the user's current neutral request without claiming recipe support."""
        return cls(
            target_model_family="", target_model_version="", recipe_id="current-request",
            recipe_version=1, process="text_to_image", width=width, height=height,
            steps=steps, cfg_scale=cfg_scale, sampler=sampler, scheduler=scheduler,
            recommendation_scope="Existing reviewed generation request",
            override_reason="Preserve the current reviewed generation request.",
            explicit_fields=("width", "height", "steps", "cfg_scale", "sampler", "scheduler"),
        )

    def request_changes(self) -> dict[str, object]:
        """Return only fields owned by the neutral request's deliberate revise path."""
        values: dict[str, object] = {}
        for field in ("width", "height", "steps", "cfg_scale", "sampler", "scheduler"):
            value = getattr(self, field)
            if field in self.explicit_fields or value not in (None, ""):
                values[field] = value
        return values


@dataclass(frozen=True)
class GuidanceIssue:
    code: str
    level: str
    summary: str
    direct_fix: str


@dataclass(frozen=True)
class WorkflowAlternative:
    label: str
    consequence: str
    changes: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class WorkflowGuidancePlan:
    request: GuidanceRequest
    record_id: str
    family: str
    version: str
    modality: str
    catalog_status: str
    adapter_status: str
    machine_status: str
    explanation: str
    caption_profile: ExportProfile | None
    recommended_configuration: TrainingConfiguration | None
    configuration: TrainingConfiguration | None
    preserved_configuration: TrainingConfiguration | None
    issues: tuple[GuidanceIssue, ...]
    alternatives: tuple[WorkflowAlternative, ...]
    source_urls: tuple[str, ...]
    limitations: tuple[str, ...]
    recipe_id: str = ""
    recipe_version: int = 0
    companion_requirements: tuple[CompanionRequirement, ...] = ()
    companion_assessments: tuple[CompanionAssessment, ...] = ()
    recommended_generation_configuration: GenerationConfigurationRecommendation | None = None
    generation_configuration: GenerationConfigurationRecommendation | None = None
    preserved_generation_configuration: GenerationConfigurationRecommendation | None = None
    planned_image_upper_bound: int | None = None

    @property
    def can_apply(self) -> bool:
        """A neutral preview can be applied without claiming it can run."""
        if self.request.workflow_kind == "generation":
            return self.generation_configuration is not None and not any(
                issue.level == "blocking"
                for issue in validate_generation_configuration(
                    self.generation_configuration,
                    family=self.family,
                    version=self.version,
                )
            )
        return self.configuration is not None and not any(
            issue.level == "blocking" for issue in validate_training_configuration(
                self.configuration, profile_family=self.family, profile_version=self.version,
            )
        )

    @property
    def can_run(self) -> bool:
        configuration = (
            self.generation_configuration
            if self.request.workflow_kind == "generation"
            else self.configuration
        )
        if configuration is None or not self.can_apply or self.machine_status != "ready_after_preflight":
            return False
        if self.request.workflow_kind == "training":
            return preview_training_preset(configuration, preset_id="current",
                training_image_count=self.planned_image_upper_bound).native_status == "native_candidate"
        return True

    def resource_binding_evidence(self) -> dict[str, object]:
        """Portable evidence for the existing training/generation plan authority."""
        return _resource_binding_evidence(
            self.record_id, self.request.workflow_kind, self.recipe_id, self.recipe_version,
            self.companion_requirements, self.companion_assessments,
        )


@dataclass(frozen=True)
class CatalogMergePreview:
    payload: dict[str, Any]
    issues: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class CatalogRecoveryState:
    status: str
    summary: str
    pending: bool = False


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _clean_text(value: object, field: str, maximum: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > maximum:
        raise ModelWorkflowKnowledgeError(f"{field} is missing or exceeds {maximum} characters.")
    return text


def _normalized_digest_identity(value: object) -> str:
    text = str(value or "").strip().casefold()
    prefix = "pack-sha256:" if text.startswith("pack-sha256:") else "sha256:"
    digest = text.removeprefix("pack-sha256:").removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return ""
    return prefix + digest


def _validate_adapter(value: object, *, label: str) -> None:
    if (
        not isinstance(value, dict)
        or value.get("status") not in {"tested_adapter", "unavailable", "unsupported"}
        or not isinstance(value.get("id"), str)
        or type(value.get("version")) is not int
        or value["version"] < 0
        or not isinstance(value.get("source_revision"), str)
        or (
            value["status"] == "tested_adapter"
            and (
                not value["id"].strip()
                or value["version"] < 1
                or not value["source_revision"].strip()
            )
        )
    ):
        raise ModelWorkflowKnowledgeError(f"{label} adapter state is invalid.")
    runtime_policy = value.get("runtime_policy", "")
    if runtime_policy not in {"", "live_exact_identity"}:
        raise ModelWorkflowKnowledgeError(f"{label} adapter runtime policy is invalid.")


def _validate_companion_requirements(value: object) -> None:
    if not isinstance(value, list) or len(value) > 64:
        raise ModelWorkflowKnowledgeError("A companion requirement list is invalid or excessive.")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ModelWorkflowKnowledgeError("Every companion requirement must be an object.")
        if not isinstance(item.get("slot"), str):
            raise ModelWorkflowKnowledgeError("A companion slot must be text.")
        slot = _clean_text(item.get("slot"), "companion slot", 100)
        if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in slot):
            raise ModelWorkflowKnowledgeError(f"Companion slot {slot!r} must use lowercase letters, numbers, and underscores.")
        if slot in seen:
            raise ModelWorkflowKnowledgeError(f"The companion slot {slot!r} is duplicated.")
        seen.add(slot)
        if item.get("role") not in RESOURCE_ROLES:
            raise ModelWorkflowKnowledgeError(f"Companion slot {slot!r} has an unsupported role.")
        for field in ("family", "variant"):
            _clean_text(item.get(field), f"companion {field}", 300)
        revision = item.get("revision")
        if not isinstance(revision, str) or len(revision.strip()) > 300:
            raise ModelWorkflowKnowledgeError("A companion revision must be bounded text.")
        _clean_text(item.get("revision_note"), "companion revision note", 500)
        uses = item.get("applies_to")
        if (
            not isinstance(uses, list) or not uses or len(uses) > len(REQUIREMENT_USES)
            or any(not isinstance(use, str) or use not in REQUIREMENT_USES for use in uses)
            or len(set(uses)) != len(uses)
        ):
            raise ModelWorkflowKnowledgeError(f"Companion slot {slot!r} has invalid workflow uses.")
        if item.get("necessity") not in REQUIREMENT_LEVELS:
            raise ModelWorkflowKnowledgeError(f"Companion slot {slot!r} has invalid necessity.")
        if item.get("compatibility_status") not in COMPATIBILITY_STATES:
            raise ModelWorkflowKnowledgeError(f"Companion slot {slot!r} has invalid compatibility evidence.")
        if item.get("compatibility_status") != "not_assessed" and not revision.strip():
            raise ModelWorkflowKnowledgeError(
                f"Companion slot {slot!r} requires an exact revision when compatibility is assessed."
            )
        hashes = item.get("accepted_sha256", [])
        normalized_hashes = [_normalized_digest_identity(digest) for digest in hashes] if isinstance(hashes, list) else []
        if (
            not isinstance(hashes, list) or len(hashes) > 64
            or len(set(normalized_hashes)) != len(hashes)
            or any(not digest for digest in normalized_hashes)
        ):
            raise ModelWorkflowKnowledgeError(f"Companion slot {slot!r} has invalid accepted SHA-256 identities.")
        if item.get("compatibility_status") == "not_assessed" and hashes:
            raise ModelWorkflowKnowledgeError(
                f"Companion slot {slot!r} cannot list accepted identities while compatibility is not assessed."
            )


def _validate_generation_recipe(value: object) -> None:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] < 1:
        raise ModelWorkflowKnowledgeError("A generation recipe must be a versioned object.")
    for field in ("id", "process"):
        _clean_text(value.get(field), f"generation recipe {field}", 200)
    _validate_adapter(value.get("adapter"), label="Generation recipe")
    if value["adapter"].get("status") == "tested_adapter" and value["adapter"].get("runtime_policy") != "live_exact_identity":
        raise ModelWorkflowKnowledgeError("A tested generation recipe requires exact live runtime identity.")
    configuration = value.get("configuration")
    if not isinstance(configuration, dict):
        raise ModelWorkflowKnowledgeError("A generation recipe configuration must be an object.")
    for field in ("width", "height"):
        setting = configuration.get(field)
        if type(setting) is not int or not 64 <= setting <= 2_048 or setting % 8:
            raise ModelWorkflowKnowledgeError(f"Generation recipe {field} is invalid.")
    steps = configuration.get("steps")
    if type(steps) is not int or not 1 <= steps <= 150:
        raise ModelWorkflowKnowledgeError("Generation recipe steps are invalid.")
    cfg = configuration.get("cfg_scale")
    if cfg is not None and (type(cfg) not in {int, float} or not math.isfinite(cfg) or not 0 <= cfg <= 30):
        raise ModelWorkflowKnowledgeError("Generation recipe CFG scale is invalid.")
    for field in ("sampler", "scheduler"):
        if not isinstance(configuration.get(field), str) or len(configuration[field]) > 200:
            raise ModelWorkflowKnowledgeError(f"Generation recipe {field} is invalid.")
    editable = value.get("editable_fields", [])
    runtime = value.get("required_runtime_settings", [])
    conditioning = value.get("conditioning_inputs", [])
    configuration_evidence = value.get("configuration_evidence")
    if (
        not isinstance(editable, list) or len(editable) > len(GENERATION_CONFIGURATION_FIELDS)
        or any(not isinstance(field, str) or field not in GENERATION_CONFIGURATION_FIELDS for field in editable)
        or len(set(editable)) != len(editable)
    ):
        raise ModelWorkflowKnowledgeError("Generation recipe editable fields are invalid.")
    if (
        not isinstance(runtime, list) or len(runtime) > len(GENERATION_CONFIGURATION_FIELDS)
        or any(not isinstance(field, str) or field not in GENERATION_CONFIGURATION_FIELDS for field in runtime)
        or len(set(runtime)) != len(runtime)
    ):
        raise ModelWorkflowKnowledgeError("Generation recipe runtime settings are invalid.")
    if (
        not isinstance(conditioning, list) or not conditioning or len(conditioning) > 16
        or any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in conditioning)
        or len(set(conditioning)) != len(conditioning)
    ):
        raise ModelWorkflowKnowledgeError("Generation recipe conditioning inputs are invalid.")
    evidence_states = {"documented_model_source", "collectigen_neutral_default", "exact_runtime_required"}
    if (
        not isinstance(configuration_evidence, dict)
        or set(configuration_evidence) != GENERATION_CONFIGURATION_FIELDS
        or any(not isinstance(state, str) or state not in evidence_states for state in configuration_evidence.values())
    ):
        raise ModelWorkflowKnowledgeError("Generation recipe setting evidence is invalid or incomplete.")
    resource_status = value.get("resource_evidence_status")
    if resource_status not in {"tested", "documented", "unmeasured"}:
        raise ModelWorkflowKnowledgeError("Generation recipe resource evidence status is invalid.")
    for field in ("minimum_storage_gib", "minimum_ram_gib", "minimum_vram_gib"):
        setting = value.get(field)
        if resource_status == "unmeasured":
            if setting not in (None, 0):
                raise ModelWorkflowKnowledgeError("Unmeasured generation resources cannot declare qualified minima.")
        elif type(setting) not in {int, float} or not math.isfinite(setting) or not 0 <= setting <= 10_000:
            raise ModelWorkflowKnowledgeError(f"Generation recipe {field} is invalid.")
    limitations = value.get("limitations", [])
    if not isinstance(limitations, list) or len(limitations) > 64 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 2_000 for item in limitations
    ):
        raise ModelWorkflowKnowledgeError("A generation recipe limitation list is invalid or excessive.")


def _bounded_read(path: str | Path, *, allow_missing: bool = False) -> bytes | None:
    candidate = Path(path)
    try:
        raw = _read_guarded_bytes(candidate, MAX_CATALOG_BYTES)
    except ModelResourceError as exc:
        raise ModelWorkflowKnowledgeError(str(exc)) from exc
    if raw is None and not allow_missing:
        raise FileNotFoundError(candidate)
    return raw


def _strict_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Unsupported JSON constant: {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ModelWorkflowKnowledgeError("The model-workflow catalog is not strict UTF-8 JSON.") from exc


def _read_catalog(path: str | Path) -> dict[str, Any]:
    raw = _bounded_read(path)
    assert raw is not None
    value = _strict_json(raw)
    validate_catalog(value)
    return value


def validate_catalog(value: object) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ModelWorkflowKnowledgeError("The model-workflow catalog schema is unsupported.")
    _clean_text(value.get("catalog_id"), "catalog ID", 160)
    if type(value.get("catalog_version")) is not int or value["catalog_version"] < 1:
        raise ModelWorkflowKnowledgeError("The catalog version must be a positive integer.")
    try:
        date.fromisoformat(str(value.get("as_of")))
    except ValueError as exc:
        raise ModelWorkflowKnowledgeError("The catalog as-of date must use YYYY-MM-DD.") from exc
    records = value.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ModelWorkflowKnowledgeError("The model-workflow record list is invalid or too large.")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ModelWorkflowKnowledgeError("Every model-workflow record must be an object.")
        record_id = _clean_text(record.get("id"), "record ID", 200)
        if record_id in seen:
            raise ModelWorkflowKnowledgeError(f"The model-workflow record {record_id!r} is duplicated.")
        seen.add(record_id)
        for field in ("family", "version", "variant", "modality", "catalog_status", "license", "summary"):
            _clean_text(record.get(field), field, 1000)
        if record["modality"] not in {"image", "video"}:
            raise ModelWorkflowKnowledgeError("A record modality must be image or video.")
        if record["catalog_status"] not in {"current", "stale", "conflicting", "unsupported", "investigation_only"}:
            raise ModelWorkflowKnowledgeError("A record catalog status is unsupported.")
        if (
            not isinstance(record.get("goal_kinds"), list) or not record["goal_kinds"]
            or len(record["goal_kinds"]) > 32
            or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in record["goal_kinds"])
        ):
            raise ModelWorkflowKnowledgeError("A record must declare at least one goal kind.")
        sources = record.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ModelWorkflowKnowledgeError("A record must retain primary-source evidence.")
        for source in sources:
            if not isinstance(source, dict) or not str(source.get("url", "")).startswith("https://"):
                raise ModelWorkflowKnowledgeError("A source must be an HTTPS evidence record.")
            date.fromisoformat(str(source.get("checked_on")))
            _clean_text(source.get("applicable_revision"), "applicable source revision", 500)
            if source.get("revision_status", "dated_moving_reference") not in {"immutable_revision", "dated_moving_reference"}:
                raise ModelWorkflowKnowledgeError("A source revision status is invalid.")
        limitations = record.get("limitations", [])
        if not isinstance(limitations, list) or len(limitations) > 64 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2_000
            for item in limitations
        ):
            raise ModelWorkflowKnowledgeError("A record limitation list is invalid or excessive.")
        _validate_companion_requirements(record.get("companion_requirements", []))
        generation_recipe = record.get("generation_recipe")
        if generation_recipe is not None:
            _validate_generation_recipe(generation_recipe)
        recipe = record.get("recipe")
        if recipe is not None:
            if (
                not isinstance(recipe, dict) or type(recipe.get("version")) is not int
                or recipe["version"] < 1
                or type(recipe.get("caption_profile_version")) is not int
                or recipe["caption_profile_version"] < 1
                or type(recipe.get("resolution")) is not int
                or not 256 <= recipe["resolution"] <= 2_048
            ):
                raise ModelWorkflowKnowledgeError("A recipe must be a versioned object.")
            for field in ("id", "method", "caption_profile_id", "caption_profile_base_id", "caption_strategy"):
                _clean_text(recipe.get(field), f"recipe {field}", 200)
            _validate_adapter(recipe.get("adapter"), label="Recipe")
            for field in ("minimum_storage_gib", "minimum_ram_gib", "minimum_vram_gib"):
                value = recipe.get(field, 0)
                if type(value) not in {int, float} or not 0 <= value <= 10_000:
                    raise ModelWorkflowKnowledgeError(f"Recipe {field} is invalid.")
            companions = recipe.get("required_companions", [])
            recipe_limits = recipe.get("limitations", [])
            if any(
                not isinstance(values, list) or len(values) > 64
                or any(not isinstance(item, str) or not item.strip() or len(item) > 2_000 for item in values)
                for values in (companions, recipe_limits)
            ):
                raise ModelWorkflowKnowledgeError("A recipe companion or limitation list is invalid or excessive.")


def machine_capability_from_setup(settings: object, jobs: object | None = None, *, model_directory: str = "") -> MachineCapabilityProfile:
    """Reuse Setup's GPU and location detection; no model, media, or runtime is loaded."""
    index = int(getattr(settings, "joycaption_gpu_index", 0) or 0)
    gpu = nvidia_gpu_profile(index)
    assessment = validate_locations({
        "ingest_directory": str(getattr(settings, "ingest_directory", "") or ""),
        "working_directory": str(getattr(settings, "working_directory", "") or ""),
        "problems_directory": str(getattr(settings, "problems_directory", "") or ""),
        "ai_models_directory": str(model_directory or getattr(settings, "ai_models_directory", "") or ""),
    })
    model_location = next((item for item in assessment.locations if item.key == "ai_models_directory"), None)
    scheduler = jobs.scheduler_snapshot() if jobs is not None and hasattr(jobs, "scheduler_snapshot") else {}
    active = scheduler.get("active", {}) if isinstance(scheduler, dict) else {}
    try:
        import psutil
        ram_gib = float(psutil.virtual_memory().total) / 1024**3
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        ram_gib = None
    fingerprint = _fingerprint({
        "cpu_threads": os.cpu_count(),
        "ram_gib": ram_gib,
        "gpu": None if gpu is None else (gpu.index, gpu.name, gpu.total_memory_mib),
        "model_storage": None if model_location is None else model_location.path,
    })
    return MachineCapabilityProfile(
        cpu_threads=os.cpu_count(),
        ram_gib=ram_gib,
        gpu_name=gpu.name if gpu else "",
        gpu_backend="cuda" if gpu else "cpu",
        gpu_vram_gib=gpu.total_memory_gib if gpu else None,
        free_storage_gib=(model_location.free_bytes / 1024**3 if model_location and model_location.free_bytes is not None else None),
        competing_gpu_jobs=max(0, int(active.get("gpu", 0) or 0)) if isinstance(active, dict) else 0,
        observed_at=datetime.now(timezone.utc).isoformat(),
        evidence="detected",
        current_fingerprint=fingerprint,
    )


def _profile_for_recipe(recipe: Mapping[str, Any], family: str, version: str) -> ExportProfile:
    base = export_profile_by_id(str(recipe["caption_profile_base_id"]))
    return replace(
        base,
        profile_id=str(recipe["caption_profile_id"]),
        version=int(recipe["caption_profile_version"]),
        format_version=base.effective_format_version,
        target_model_family=family,
        target_model_version=version,
        caption_strategy=str(recipe["caption_strategy"]),
        name=f"{family} {version} · {str(recipe['caption_strategy']).replace('_', ' ')}",
        description=(
            "Versioned model-workflow projection. It derives captions from the same confirmed "
            "Image Knowledge without deleting or reclassifying any fact."
        ),
        adapter_assumptions=("Guidance preview only; adapter readiness is evaluated separately.",),
    )


def _recommendation_values(configuration: TrainingConfiguration, changes: Mapping[str, object], scope: str) -> TrainingConfiguration:
    configuration = replace(configuration, **changes, recommendation_scope=scope)
    recommendations = []
    reasons = {
        "target_resolution": ("The native Anima starting point is 256 px; the catalog's 1024 px research preview is not an admitted native training setting."
                              if configuration.target_model_family.casefold() == "anima" else
                              "The exact recipe documents this planning resolution; source bytes remain unchanged."),
        "batch_size": "This is a planning batch request, not a measured GPU fit; free VRAM and exact package settings need native preflight.",
        "gradient_accumulation": (f"{configuration.gradient_accumulation} accumulated batch(es) use the planned-image upper bound for Anima's 2,000-step limit; exact training roles are checked later. This does not reduce batch memory."
                                  if configuration.target_model_family.casefold() == "anima" else
                                  "Accumulation groups successive batches for each optimizer update; it does not establish GPU fit."),
        "precision": "The requested precision follows detected backend capability and remains subject to adapter admission.",
    }
    previous = {item.field: item for item in configuration.recommendations}
    for field in RECOMMENDED_SETTING_FIELDS:
        value = getattr(configuration, field)
        reason = reasons.get(field, previous[field].reason)
        recommendations.append(TrainingSettingRecommendation(field, value, reason, scope))
    return replace(configuration, recommendations=tuple(recommendations))


def _configuration(
    record: Mapping[str, Any], recipe: Mapping[str, Any], machine: MachineCapabilityProfile,
    count: int, *, goal: str, goal_kind: str,
) -> TrainingConfiguration:
    profile = _profile_for_recipe(recipe, str(record["family"]), str(record["version"]))
    base = recommended_training_configuration(
        target_model_family=str(record["family"]), target_model_version=str(record["version"]),
        goal_kind=goal_kind, included_count=count, profile_token_budget=profile.token_budget,
    )
    vram = machine.gpu_vram_gib
    if str(record["family"]).casefold() == "anima":
        batch, accumulation, precision = 1, base.gradient_accumulation, "bf16"
    elif machine.gpu_backend == "cuda" and vram is not None and vram >= 24:
        batch, accumulation, precision = 4, 1, "bf16"
    elif machine.gpu_backend == "cuda" and vram is not None and vram >= 12:
        batch, accumulation, precision = 2, 2, "bf16"
    elif machine.gpu_backend == "cuda":
        batch, accumulation, precision = 1, 4, "fp16"
    else:
        batch, accumulation, precision = 1, 4, "float32"
    scope = (
        f"{recipe['id']} v{recipe['version']}; {record['family']} {record['version']}; "
        f"{goal_kind} goal: {goal}; {count:,} planned image(s); {machine.evidence} machine capability"
    )
    return _recommendation_values(base, {
        "target_resolution": (256 if str(record["family"]).casefold() == "anima" else int(recipe["resolution"])),
        "batch_size": batch,
        "gradient_accumulation": accumulation, "precision": precision,
    }, scope)


def _registered_for(snapshot: Mapping[str, Any] | None, resource_id: str, family: str) -> bool:
    if not resource_id or not isinstance(snapshot, Mapping):
        return False
    record = snapshot.get("resources", {}).get(resource_id) if isinstance(snapshot.get("resources", {}), Mapping) else None
    return bool(isinstance(record, Mapping) and any(
        isinstance(item, Mapping) and item.get("role") == "checkpoint"
        and str(item.get("family", "")).casefold() == family.casefold()
        for item in record.get("declarations", ())
    ))


def _resource_record(snapshot: Mapping[str, Any] | None, resource_id: str) -> Mapping[str, Any] | None:
    if not resource_id or not isinstance(snapshot, Mapping):
        return None
    resources = snapshot.get("resources", {})
    record = resources.get(resource_id) if isinstance(resources, Mapping) else None
    return record if isinstance(record, Mapping) else None


def _resource_sha256(record: Mapping[str, Any] | None, resource_id: str) -> str:
    candidates: list[tuple[str, object]] = [("", resource_id)]
    if isinstance(record, Mapping):
        identity = record.get("identity", {})
        if isinstance(identity, Mapping):
            candidates.extend((
                ("sha256:", identity.get("sha256")),
                ("sha256:", identity.get("full_sha256")),
                ("pack-sha256:", identity.get("pack_sha256")),
                ("", identity.get("resource_id")),
            ))
    for default_prefix, candidate in candidates:
        text = str(candidate or "").strip()
        if default_prefix and ":" not in text:
            text = default_prefix + text
        normalized = _normalized_digest_identity(text)
        if normalized:
            return normalized
    return ""


def _requirements_for(record: Mapping[str, Any], workflow_kind: str) -> tuple[CompanionRequirement, ...]:
    result: list[CompanionRequirement] = []
    for item in record.get("companion_requirements", ()):
        if workflow_kind not in item["applies_to"]:
            continue
        result.append(CompanionRequirement(
            slot=str(item["slot"]), role=str(item["role"]), family=str(item["family"]),
            variant=str(item["variant"]), revision=str(item["revision"]),
            revision_note=str(item["revision_note"]),
            applies_to=tuple(str(use) for use in item["applies_to"]),
            necessity=str(item["necessity"]),
            compatibility_status=str(item["compatibility_status"]),
            accepted_sha256=tuple(_normalized_digest_identity(digest) for digest in item.get("accepted_sha256", ())),
        ))
    return tuple(result)


def _assess_companions(
    requirements: Iterable[CompanionRequirement], bindings: Iterable[ResourceBinding],
    registry_snapshot: Mapping[str, Any] | None, verified_resource_ids: Iterable[str],
) -> tuple[CompanionAssessment, ...]:
    binding_map: dict[str, str] = {}
    for binding in bindings:
        slot = " ".join(str(binding.slot or "").split())
        resource_id = str(binding.resource_id or "").strip()
        if not slot or len(slot) > 100 or not resource_id or len(resource_id) > 300:
            raise ModelWorkflowKnowledgeError("Every companion binding needs a bounded slot and exact resource identity.")
        if slot in binding_map:
            raise ModelWorkflowKnowledgeError(f"The companion binding slot {slot!r} is duplicated.")
        binding_map[slot] = resource_id
    requirement_list = tuple(requirements)
    known_slots = {item.slot for item in requirement_list}
    unknown = sorted(set(binding_map) - known_slots)
    if unknown:
        raise ModelWorkflowKnowledgeError(f"The companion binding slot {unknown[0]!r} is not required by this workflow.")
    verified = {str(item) for item in verified_resource_ids}
    results: list[CompanionAssessment] = []
    for requirement in requirement_list:
        resource_id = binding_map.get(requirement.slot, "")
        record = _resource_record(registry_snapshot, resource_id)
        declarations = record.get("declarations", ()) if record is not None else ()
        family_declarations = [
            item for item in declarations
            if isinstance(item, Mapping)
            and str(item.get("role", "")) == requirement.role
            and str(item.get("family", "")).casefold() == requirement.family.casefold()
        ]
        declared = record is not None and any(
            not (
                requirement.variant and str(item.get("variant", "")).strip()
                and str(item.get("variant", "")).casefold() != requirement.variant.casefold()
            )
            and not (
                requirement.revision and str(item.get("version") or item.get("revision") or "").strip()
                and str(item.get("version") or item.get("revision") or "").casefold() != requirement.revision.casefold()
            )
            for item in family_declarations
        )
        availability = "missing" if record is None else ("selected" if declared else "declaration_mismatch")
        verification = "verified" if declared and resource_id in verified else (
            "recheck_required" if record is not None else "not_assessed"
        )
        digest = _resource_sha256(record, resource_id)
        if not requirement.accepted_sha256 or requirement.compatibility_status == "not_assessed":
            compatibility = "not_assessed"
        elif digest and digest in requirement.accepted_sha256:
            compatibility = "matched"
        else:
            compatibility = "mismatch"
        ready = availability == "selected" and verification == "verified" and compatibility == "matched"
        blocking = requirement.necessity == "required" and not ready
        if not resource_id:
            summary = f"{requirement.slot.replace('_', ' ').title()} is not selected."
        elif availability == "declaration_mismatch":
            summary = f"The selected resource is not explicitly declared as {requirement.family} {requirement.role.replace('_', ' ')}."
        elif verification != "verified":
            summary = "The selected resource needs a fresh exact identity check."
        elif compatibility == "not_assessed":
            summary = "The selected exact resource is verified, but catalog compatibility is not assessed."
        elif compatibility == "mismatch":
            summary = "The verified resource hash is not admitted by this recipe."
        else:
            summary = "The selected exact resource matches the catalog requirement."
        results.append(CompanionAssessment(
            requirement.slot, resource_id, availability, verification, compatibility, blocking, summary,
        ))
    return tuple(results)


def _resource_binding_evidence(
    record_id: str, workflow_kind: str, recipe_id: str, recipe_version: int,
    requirements: Iterable[CompanionRequirement], assessments: Iterable[CompanionAssessment],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "catalog_record_id": str(record_id),
        "workflow_kind": str(workflow_kind),
        "recipe_id": str(recipe_id),
        "recipe_version": int(recipe_version),
        "bindings": [
            {
                "slot": requirement.slot,
                "role": requirement.role,
                "family": requirement.family,
                "variant": requirement.variant,
                "revision": requirement.revision,
                "revision_note": requirement.revision_note,
                "necessity": requirement.necessity,
                "compatibility_status": requirement.compatibility_status,
                "resource_id": assessment.resource_id,
                "availability": assessment.availability,
                "verification": assessment.verification,
                "compatibility": assessment.compatibility,
            }
            for requirement, assessment in zip(requirements, assessments)
        ],
    }


def validate_generation_configuration(
    configuration: GenerationConfigurationRecommendation, *, family: str, version: str,
) -> tuple[GuidanceIssue, ...]:
    issues: list[GuidanceIssue] = []
    if (
        configuration.target_model_family.casefold() != str(family).casefold()
        or configuration.target_model_version.casefold() != str(version).casefold()
    ):
        issues.append(GuidanceIssue(
            "generation_target_mismatch", "blocking",
            "The generation recommendation targets another model version.",
            "Restore the recommendation for this exact catalog target.",
        ))
    if not 64 <= configuration.width <= 2_048 or configuration.width % 8:
        issues.append(GuidanceIssue("generation_width_invalid", "blocking", "Generation width is outside the admitted range.", "Choose a multiple of 8 from 64 through 2048."))
    if not 64 <= configuration.height <= 2_048 or configuration.height % 8:
        issues.append(GuidanceIssue("generation_height_invalid", "blocking", "Generation height is outside the admitted range.", "Choose a multiple of 8 from 64 through 2048."))
    if not 1 <= configuration.steps <= 150:
        issues.append(GuidanceIssue("generation_steps_invalid", "blocking", "Generation steps are outside the admitted range.", "Choose from 1 through 150 steps."))
    if configuration.cfg_scale is not None and (
        type(configuration.cfg_scale) not in {int, float}
        or not math.isfinite(configuration.cfg_scale)
        or not 0 <= configuration.cfg_scale <= 30
    ):
        issues.append(GuidanceIssue("generation_cfg_invalid", "blocking", "Generation CFG scale is invalid.", "Choose a finite value from 0 through 30."))
    if len(configuration.sampler) > 200 or len(configuration.scheduler) > 200:
        issues.append(GuidanceIssue("generation_sampling_invalid", "blocking", "Generation sampling settings are too long.", "Choose bounded settings reported by the exact runtime."))
    if not configuration.recipe_id or configuration.recipe_version < 1 or not configuration.process:
        issues.append(GuidanceIssue("generation_recipe_invalid", "blocking", "Generation recipe identity is incomplete.", "Restore the versioned catalog recommendation."))
    if (
        any(not isinstance(field, str) or field not in GENERATION_CONFIGURATION_FIELDS for field in configuration.explicit_fields)
        or len(set(configuration.explicit_fields)) != len(configuration.explicit_fields)
    ):
        issues.append(GuidanceIssue("generation_explicit_fields_invalid", "blocking", "Generation request field ownership is invalid.", "Restore the recommendation or current request snapshot."))
    if (
        len(configuration.runtime_version) > 500
        or len(configuration.runtime_fingerprint) > 500
        or len(configuration.recommendation_scope) > 2_000
        or len(configuration.override_reason) > 2_000
    ):
        issues.append(GuidanceIssue("generation_evidence_too_large", "blocking", "Generation recommendation evidence exceeds its bound.", "Rebuild the recommendation from bounded catalog and runtime evidence."))
    return tuple(issues)


def _generation_configuration(
    record: Mapping[str, Any], recipe: Mapping[str, Any], request: GuidanceRequest,
    availability: GenerationAdapterAvailability | None,
) -> GenerationConfigurationRecommendation:
    values = recipe["configuration"]
    return GenerationConfigurationRecommendation(
        target_model_family=str(record["family"]), target_model_version=str(record["version"]),
        recipe_id=str(recipe["id"]), recipe_version=int(recipe["version"]), process=str(recipe["process"]),
        width=int(values["width"]), height=int(values["height"]), steps=int(values["steps"]),
        cfg_scale=float(values["cfg_scale"]) if values.get("cfg_scale") is not None else None,
        sampler=str(values.get("sampler", "")), scheduler=str(values.get("scheduler", "")),
        recommendation_scope=f"{record['family']} {record['version']} {recipe['process']} for {request.goal}",
        runtime_version=availability.runtime_version if availability else "",
        runtime_fingerprint=availability.runtime_fingerprint if availability else "",
        explicit_fields=tuple(
            field for field in ("width", "height", "steps", "cfg_scale", "sampler", "scheduler")
            if field not in set(recipe.get("required_runtime_settings", ())) or values.get(field) not in (None, "")
        ),
        setting_evidence=tuple(
            (field, str(recipe["configuration_evidence"][field]))
            for field in ("width", "height", "steps", "cfg_scale", "sampler", "scheduler")
        ),
    )


def _preserve_unknown_mapping(
    local: object, incoming: object, known_fields: set[str],
) -> object:
    if not isinstance(local, Mapping) or not isinstance(incoming, Mapping):
        return deepcopy(incoming)
    result = deepcopy(dict(incoming))
    for key, value in local.items():
        if key not in known_fields:
            result[key] = deepcopy(value)
    return result


def _merge_record_extensions(local: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Take accepted public evidence while retaining unrecognized local extensions."""
    result = dict(_preserve_unknown_mapping(local, incoming, KNOWN_RECORD_FIELDS))
    for key, fields in (("recipe", RECIPE_FIELDS), ("generation_recipe", GENERATION_RECIPE_FIELDS)):
        local_value = local.get(key)
        incoming_value = incoming.get(key)
        if not isinstance(local_value, Mapping) or not isinstance(incoming_value, Mapping):
            continue
        merged = dict(_preserve_unknown_mapping(local_value, incoming_value, fields))
        if isinstance(local_value.get("adapter"), Mapping) and isinstance(incoming_value.get("adapter"), Mapping):
            merged["adapter"] = _preserve_unknown_mapping(local_value["adapter"], incoming_value["adapter"], ADAPTER_FIELDS)
        if (
            key == "generation_recipe"
            and isinstance(local_value.get("configuration"), Mapping)
            and isinstance(incoming_value.get("configuration"), Mapping)
        ):
            merged["configuration"] = _preserve_unknown_mapping(
                local_value["configuration"], incoming_value["configuration"], GENERATION_CONFIGURATION_FIELDS,
            )
        result[key] = merged
    local_requirements = {
        str(item.get("slot")): item for item in local.get("companion_requirements", ())
        if isinstance(item, Mapping) and item.get("slot")
    }
    incoming_requirements = incoming.get("companion_requirements")
    if isinstance(incoming_requirements, list):
        result["companion_requirements"] = [
            _preserve_unknown_mapping(
                local_requirements.get(str(item.get("slot")), {}), item,
                COMPANION_REQUIREMENT_FIELDS,
            ) if isinstance(item, Mapping) else deepcopy(item)
            for item in incoming_requirements
        ]
    for key in ("user_notes", "user_overrides"):
        if key in local:
            result[key] = deepcopy(local[key])
    return result


class ModelWorkflowGuidanceService:
    def __init__(
        self, catalog_path: str | Path = BASELINE_PATH, *,
        update_path: str | Path | None = None,
    ) -> None:
        self.baseline_path = Path(catalog_path).absolute()
        default_baseline = BASELINE_PATH.absolute()
        self.update_path = (
            Path(update_path).absolute()
            if update_path is not None else
            (self.baseline_path if self.baseline_path != default_baseline else None)
        )
        self.catalog_path = (
            self.update_path
            if self.update_path is not None and self.update_path.exists()
            else self.baseline_path
        )
        self._reload_catalog()

    @staticmethod
    def source_file_fingerprint(path: str | Path) -> str:
        raw = _bounded_read(path)
        assert raw is not None
        return hashlib.sha256(raw).hexdigest()

    def catalog_write_token(self) -> str:
        if self.update_path is None:
            return "read_only_baseline"
        raw = _bounded_read(self.update_path, allow_missing=True)
        if raw is None:
            baseline_raw = _bounded_read(self.baseline_path)
            assert baseline_raw is not None
            return "missing:" + hashlib.sha256(baseline_raw).hexdigest()
        return hashlib.sha256(raw).hexdigest()

    @contextmanager
    def _update_writer(self):
        if self.update_path is None or self.update_path == BASELINE_PATH.absolute():
            raise ModelWorkflowKnowledgeError(
                "The shipped model-workflow baseline is read-only. Configure a user-data update path first."
            )
        parent = self.update_path.parent
        _assert_unredirected(parent)
        parent.mkdir(parents=True, exist_ok=True)
        lock = self.update_path.with_name(self.update_path.name + ".lock")
        _assert_unredirected(lock)
        descriptor = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "r+b") as handle:
            state = os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or state.st_size > 1:
                raise ModelWorkflowKnowledgeError("The model-workflow update lock is invalid.")
            if state.st_size == 0:
                handle.write(b"0"); handle.flush(); os.fsync(handle.fileno())
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ModelWorkflowKnowledgeError(
                    "Another model-workflow knowledge update is active; retry shortly."
                ) from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _recovery_receipts(self):
        """Bounded reads of this catalog's own receipts; never follow supplied paths."""
        if self.update_path is None:
            return []
        folder = self.update_path.with_name(self.update_path.name + ".recovery")
        _assert_unredirected(folder)
        receipts = []
        try:
            with os.scandir(folder) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_RECOVERY_RECEIPTS * 4:
                        raise ModelWorkflowKnowledgeError("Knowledge recovery history exceeds the review limit; retain it for review.")
                    if not entry.name.endswith(".recovery.json"):
                        continue
                    if len(receipts) >= MAX_RECOVERY_RECEIPTS:
                        raise ModelWorkflowKnowledgeError("Too many knowledge recovery receipts; retain them for review.")
                    path = folder / entry.name
                    raw = _read_guarded_bytes(path, 8192)
                    value = _strict_json(raw or b"")
                    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                            or value["schema_version"] != 1 or value.get("kind") != "collectigen_model_workflow_update_recovery"
                            or value.get("target") != self.update_path.name
                            or value.get("status") not in ("prepared", "applied", "unchanged")):
                        raise ModelWorkflowKnowledgeError("Unsupported or malformed knowledge recovery receipt; retain it for review.")
                    receipts.append((path, raw, value))
        except FileNotFoundError:
            return []
        return receipts

    def _pending_recovery(self):
        return [entry for entry in self._recovery_receipts() if entry[2]["status"] == "prepared"]

    def _recovery_outcome(self, entry):
        path, _, receipt = entry
        stem = path.name.removesuffix(".recovery.json")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{12}Z_[0-9a-f]{32}", stem):
            raise ModelWorkflowKnowledgeError("Unrecognized knowledge receipt identity; retain it for review.")
        copies = {}
        for name in ("before", "after"):
            if receipt.get(name) != f"{stem}.{name}.json":
                raise ModelWorkflowKnowledgeError("Knowledge recovery copy is not bound to its receipt.")
            digest = receipt.get(name + "_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ModelWorkflowKnowledgeError("Knowledge recovery digest is invalid.")
            raw = _read_guarded_bytes(path.parent / receipt[name], MAX_CATALOG_BYTES)
            if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                raise ModelWorkflowKnowledgeError("Knowledge recovery copy is missing or changed; retain all evidence for review.")
            validate_catalog(_strict_json(raw)); copies[name] = raw
        if "before_exists" in receipt and type(receipt["before_exists"]) is not bool:
            raise ModelWorkflowKnowledgeError("Knowledge recovery prior-existence evidence is invalid.")
        current = _read_guarded_bytes(self.update_path, MAX_CATALOG_BYTES)
        if current == copies["after"]:
            return "applied", current
        if current is not None and current == copies["before"] and receipt.get("before_exists") is not False:
            return "unchanged", current
        if current is None and receipt.get("before_exists") is False:
            baseline = _read_guarded_bytes(self.baseline_path, MAX_CATALOG_BYTES)
            if baseline == copies["before"]:
                return "unchanged", None
        raise ModelWorkflowKnowledgeError(
            "Saved knowledge does not match a proven before/after state, or legacy absence is ambiguous. "
            "Nothing was overwritten; retain the catalog and receipts for review.")

    def update_recovery_state(self) -> CatalogRecoveryState:
        try:
            pending = self._pending_recovery()
            if not pending:
                return CatalogRecoveryState("current", "No interrupted knowledge update is pending.")
            if len(pending) != 1:
                raise ModelWorkflowKnowledgeError("Multiple pending knowledge updates conflict; retain them for review.")
            outcome, _ = self._recovery_outcome(pending[0])
            detail = "published update" if outcome == "applied" else "unchanged prior knowledge"
            return CatalogRecoveryState(outcome, f"Interrupted update matches {detail}. Reconcile and reload before continuing.", True)
        except (OSError, ValueError, ModelResourceError) as exc:
            return CatalogRecoveryState("uncertain", str(exc), True)

    def _require_reconciled_update(self):
        state = self.update_recovery_state()
        if state.pending:
            raise ModelWorkflowKnowledgeError("Recover / Reload Knowledge before another update. " + state.summary)

    def _reload_catalog(self):
        before = self.catalog_write_token()
        path = self.update_path if self.update_path is not None and self.update_path.exists() else self.baseline_path
        raw = _read_guarded_bytes(path, MAX_CATALOG_BYTES)
        value = _strict_json(raw or b""); validate_catalog(value)
        if self.catalog_write_token() != before:
            raise ModelWorkflowKnowledgeError("Saved knowledge changed during reload; retry the review.")
        self.catalog_path, self.catalog, self._loaded_token = path, value, before

    def reconcile_update(self) -> CatalogRecoveryState:
        """Classify retained bytes and finalize only the receipt; never replace a catalog."""
        if self.update_path is None:
            self._reload_catalog()
            return CatalogRecoveryState("current", "Reloaded the read-only knowledge baseline.")
        try:
            with self._update_writer():
                pending = self._pending_recovery()
                outcome = "current"
                if len(pending) > 1:
                    raise ModelWorkflowKnowledgeError("Multiple pending knowledge updates conflict; retain them for review.")
                if pending:
                    path, raw, receipt = pending[0]
                    outcome, current = self._recovery_outcome(pending[0])
                    if (_read_guarded_bytes(path, 8192) != raw
                            or _read_guarded_bytes(self.update_path, MAX_CATALOG_BYTES) != current):
                        raise ModelWorkflowKnowledgeError("Knowledge recovery inputs changed; retry the review.")
                    receipt = deepcopy(receipt)
                    receipt["status"] = outcome
                    receipt["reconciled_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_verified_bytes(path, (json.dumps(receipt, indent=2, ensure_ascii=False,
                        allow_nan=False) + "\n").encode("utf-8"), label="Model-workflow recovery reconciliation")
                self._reload_catalog()
                return CatalogRecoveryState(outcome, {
                    "applied": "The update was applied. Verified saved knowledge is now reloaded.",
                    "unchanged": "The update was not applied. Verified prior knowledge is now reloaded.",
                    "current": "Reloaded current saved knowledge; no interrupted update remains.",
                }[outcome])
        except (OSError, ValueError, RuntimeError) as exc:
            return CatalogRecoveryState("uncertain", str(exc), True)

    def targets(self, goal_kind: str = "") -> tuple[dict[str, Any], ...]:
        kind = str(goal_kind or "").casefold()
        return tuple(deepcopy(record) for record in self.catalog["records"] if not kind or kind in {str(item).casefold() for item in record["goal_kinds"]})

    def companion_projection(self, target_id: str, workflow_kind: str, *,
                             bindings: Iterable[ResourceBinding] = (),
                             registry_snapshot: Mapping[str, Any] | None = None,
                             verified_resource_ids: Iterable[str] = ()) -> CompanionGuidanceProjection:
        """Pure projection of the loaded catalog; does not choose a default target.

        Verified IDs describe the caller's observations only. Catalog hash
        matches and an empty companion list never grant execution authority.
        """
        if workflow_kind not in WORKFLOW_KINDS or not isinstance(target_id, str) or len(target_id) > 300:
            raise ModelWorkflowKnowledgeError("Choose an explicit supported companion workflow/profile.")
        bindings = tuple(bindings)
        if len(bindings) > 64 or any(not isinstance(item, ResourceBinding) for item in bindings):
            raise ModelWorkflowKnowledgeError("Use bounded exact companion slot bindings.")
        record = next((item for item in self.catalog["records"] if item["id"] == target_id), None)
        requirements = _requirements_for(record, workflow_kind) if record else ()
        return CompanionGuidanceProjection(target_id, workflow_kind, _fingerprint(self.catalog), record is not None,
            str(record["family"]) if record else "", str(record["version"]) if record else "",
            tuple(str(source["url"]) for source in record.get("sources", ())) if record else (),
            requirements, _assess_companions(requirements, bindings, registry_snapshot, verified_resource_ids))

    def _recommend_generation(
        self, request: GuidanceRequest, machine: MachineCapabilityProfile,
        record: Mapping[str, Any], *, registry_snapshot: Mapping[str, Any] | None,
        verified_resource_ids: Iterable[str],
        available_generation_adapters: Iterable[GenerationAdapterAvailability],
        current_generation_configuration: GenerationConfigurationRecommendation | None,
    ) -> WorkflowGuidancePlan:
        issues: list[GuidanceIssue] = []
        available_generation_adapters = tuple(available_generation_adapters)
        if any(not isinstance(item, GenerationAdapterAvailability) for item in available_generation_adapters):
            raise ModelWorkflowKnowledgeError("Generation adapter availability requires exact typed runtime identities.")
        requirements = _requirements_for(record, "generation")
        assessments = _assess_companions(
            requirements, request.resource_bindings, registry_snapshot, verified_resource_ids,
        )
        recipe = record.get("generation_recipe")
        recommended = selected = None
        preserved = current_generation_configuration
        adapter_status = "unavailable"
        machine_status = "guidance_only"
        limitations = tuple(str(item) for item in (record.get("limitations") or ()))
        if recipe is None:
            code = "video_contract" if record.get("modality") == "video" else "generation_recipe_unavailable"
            summary = (
                "Video planning is awareness-only."
                if code == "video_contract"
                else "This catalog target has no admitted generation recipe."
            )
            issues.append(GuidanceIssue(
                code, "blocking", summary,
                "Keep this as dated guidance until the exact generation adapter and recipe are qualified.",
            ))
        else:
            adapter = recipe["adapter"]
            exact_availability = next((item for item in available_generation_adapters if (
                isinstance(item, GenerationAdapterAvailability)
                and item.adapter_id == str(adapter.get("id", ""))
                and item.adapter_version == int(adapter.get("version", 0))
                and bool(item.runtime_version.strip())
                and bool(item.runtime_fingerprint.strip())
            )), None)
            adapter_status = (
                "tested_adapter"
                if adapter.get("status") == "tested_adapter" and exact_availability is not None
                else ("known_but_unavailable" if adapter.get("status") == "tested_adapter" else str(adapter.get("status")))
            )
            recommended = _generation_configuration(record, recipe, request, exact_availability)
            selected = recommended
            if current_generation_configuration is not None:
                same_target = (
                    current_generation_configuration.target_model_family.casefold(),
                    current_generation_configuration.target_model_version.casefold(),
                    current_generation_configuration.recipe_id,
                    current_generation_configuration.recipe_version,
                ) == (
                    str(record["family"]).casefold(), str(record["version"]).casefold(),
                    str(recipe["id"]), int(recipe["version"]),
                )
                if current_generation_configuration.recipe_id == "current-request":
                    explicit = set(current_generation_configuration.explicit_fields)
                    selected = replace(
                        recommended,
                        width=current_generation_configuration.width,
                        height=current_generation_configuration.height,
                        steps=current_generation_configuration.steps,
                        cfg_scale=current_generation_configuration.cfg_scale,
                        sampler=current_generation_configuration.sampler,
                        scheduler=current_generation_configuration.scheduler,
                        override_reason=current_generation_configuration.override_reason,
                        explicit_fields=current_generation_configuration.explicit_fields,
                        setting_evidence=tuple(
                            (field, "user_preserved" if field in explicit else evidence)
                            for field, evidence in recommended.setting_evidence
                        ),
                    )
                elif same_target:
                    selected = current_generation_configuration
                else:
                    issues.append(GuidanceIssue(
                        "preserved_generation_target_mismatch", "caution",
                        "The existing generation configuration targets another exact recipe and was preserved separately.",
                        "Compare it with this recommendation before deliberately revising the generation plan.",
                    ))
            registered = _registered_for(registry_snapshot, request.base_model_id, str(record["family"]))
            verified = request.base_model_id in {str(item) for item in verified_resource_ids}
            if not registered:
                issues.append(GuidanceIssue("exact_model_missing", "blocking", "The exact base model is not registered for this family.", "Choose and register the exact checkpoint in the shared model workspace."))
            elif not verified:
                issues.append(GuidanceIssue("model_recheck_required", "blocking", "The registered model bytes still need a fresh identity check.", "Verify the exact MOD-01 resource before deliberate generation planning."))
            if adapter_status != "tested_adapter":
                issues.append(GuidanceIssue(
                    "generation_adapter_unavailable", "blocking",
                    "The admitted generation adapter has no exact current runtime identity.",
                    "Probe the admitted local adapter and retain its version and capability fingerprint before execution.",
                ))
            elif selected is not None and (
                selected.runtime_version != exact_availability.runtime_version
                or selected.runtime_fingerprint != exact_availability.runtime_fingerprint
            ):
                issues.append(GuidanceIssue(
                    "generation_runtime_changed", "blocking",
                    "The selected recommendation belongs to another runtime identity.",
                    "Build a fresh recommendation from the current exact adapter probe.",
                ))
            for field in recipe.get("required_runtime_settings", ()):
                if selected is not None and getattr(selected, str(field), None) in (None, ""):
                    issues.append(GuidanceIssue(
                        "generation_runtime_setting_required", "blocking",
                        f"{str(field).replace('_', ' ').title()} must come from the exact local runtime.",
                        "Choose a value reported by the current admitted adapter before preparing a request.",
                    ))
            for requirement, assessment in zip(requirements, assessments):
                if assessment.ready:
                    continue
                level = "blocking" if assessment.blocking else ("caution" if requirement.necessity == "recommended" else "information")
                issues.append(GuidanceIssue(
                    f"companion_{assessment.availability}_{requirement.slot}", level,
                    assessment.summary,
                    f"Choose the exact {requirement.family} {requirement.role.replace('_', ' ')} for {requirement.slot.replace('_', ' ')} and verify it in Models.",
                ))
            if recipe.get("resource_evidence_status") == "unmeasured":
                issues.append(GuidanceIssue(
                    "generation_resources_unmeasured", "caution",
                    "This generation recipe has no qualified storage, RAM, or VRAM budget.",
                    "Keep machine fit unknown until the exact adapter performs its independent preflight.",
                ))
            else:
                resource_fields = (
                    ("minimum_storage_gib", "storage", "storage", machine.free_storage_gib),
                    ("minimum_ram_gib", "system RAM", "ram", machine.ram_gib),
                    ("minimum_vram_gib", "VRAM", "vram", machine.gpu_vram_gib),
                )
                for field, label, code, available in resource_fields:
                    minimum = float(recipe[field])
                    if available is None:
                        issues.append(GuidanceIssue(f"{code}_unknown", "caution", f"Available {label} is unknown.", "Refresh Setup's machine snapshot before execution."))
                    elif available < minimum:
                        issues.append(GuidanceIssue(f"{code}_low", "blocking", f"This recipe expects at least {minimum:g} GiB {label}; {available:.1f} GiB is available.", "Use supported hardware or keep this as a neutral preview."))
            if machine.competing_gpu_jobs:
                issues.append(GuidanceIssue("competing_jobs", "caution", f"{machine.competing_gpu_jobs} GPU job(s) are active.", "Wait for GPU work to finish and recheck free memory before launch."))
            if machine.measurement_fingerprint and not machine.measurement_current:
                issues.append(GuidanceIssue("measurement_stale", "caution", "Saved performance evidence is stale for this hardware/runtime snapshot.", "Refresh the applicable measurement before using time estimates."))
            limitations += tuple(str(item) for item in recipe.get("limitations", ()))
            readiness_codes = {
                "exact_model_missing", "model_recheck_required", "generation_adapter_unavailable",
                "generation_runtime_changed", "generation_runtime_setting_required", "generation_resources_unmeasured",
                "storage_unknown", "storage_low", "ram_unknown", "ram_low", "vram_unknown",
                "vram_low", "competing_jobs", "measurement_stale",
            }
            unavailable_companion = any(item.blocking for item in assessments)
            machine_status = "ready_after_preflight" if (
                not unavailable_companion and not any(issue.code in readiness_codes for issue in issues)
            ) else "not_ready"
        explanation = (
            f"{record['family']} {record['version']} matches the {request.goal_kind} goal in the dated offline catalog. "
            "This generation recommendation is a transient patch for deliberate review; the generation plan retains exact request and execution authority."
        )
        return WorkflowGuidancePlan(
            request=request, record_id=str(record["id"]), family=str(record["family"]), version=str(record["version"]),
            modality=str(record["modality"]), catalog_status=str(record["catalog_status"]),
            adapter_status=adapter_status, machine_status=machine_status, explanation=explanation,
            caption_profile=None, recommended_configuration=None, configuration=None,
            preserved_configuration=None, issues=tuple(issues), alternatives=(),
            source_urls=tuple(str(source["url"]) for source in record["sources"]), limitations=limitations,
            recipe_id=str(recipe["id"]) if recipe is not None else "",
            recipe_version=int(recipe["version"]) if recipe is not None else 0,
            companion_requirements=requirements, companion_assessments=assessments,
            recommended_generation_configuration=recommended, generation_configuration=selected,
            preserved_generation_configuration=preserved,
        )

    def recommend(
        self, request: GuidanceRequest, machine: MachineCapabilityProfile, *,
        image_count: int = 0, registry_snapshot: Mapping[str, Any] | None = None,
        verified_resource_ids: Iterable[str] = (),
        available_adapters: Iterable[tuple[str, int]] = (),
        current_configuration: TrainingConfiguration | None = None,
        available_generation_adapters: Iterable[GenerationAdapterAvailability] = (),
        current_generation_configuration: GenerationConfigurationRecommendation | None = None,
    ) -> WorkflowGuidancePlan:
        goal = " ".join(str(request.goal or "").split())
        if not goal:
            raise ModelWorkflowKnowledgeError("Describe what this workflow should teach or create.")
        if request.priority not in PRIORITIES:
            raise ModelWorkflowKnowledgeError("Choose a supported planning priority.")
        if request.workflow_kind not in WORKFLOW_KINDS:
            raise ModelWorkflowKnowledgeError("Choose training or generation guidance.")
        if any(not isinstance(item, ResourceBinding) for item in request.resource_bindings):
            raise ModelWorkflowKnowledgeError("Companion bindings must use exact ResourceBinding values.")
        records = self.targets(request.goal_kind)
        record = next((item for item in records if item["id"] == request.target_id), None)
        if not request.target_id and records:
            record = records[0]
        if record is None:
            raise ModelWorkflowKnowledgeError("The selected exact target is not available for this goal in the current catalog.")
        if request.workflow_kind == "generation":
            return self._recommend_generation(
                request, machine, record, registry_snapshot=registry_snapshot,
                verified_resource_ids=verified_resource_ids,
                available_generation_adapters=available_generation_adapters,
                current_generation_configuration=current_generation_configuration,
            )
        if type(image_count) is not int or not 0 <= image_count <= 50_000:
            raise ModelWorkflowKnowledgeError("Choose a bounded planned-image count for Training guidance.")
        issues: list[GuidanceIssue] = []
        requirements = _requirements_for(record, "training")
        assessments = _assess_companions(
            requirements, request.resource_bindings, registry_snapshot, verified_resource_ids,
        )
        recipe = record.get("recipe")
        profile = recommended = selected = None
        adapter_status = "unavailable"
        machine_status = "guidance_only"
        alternatives: list[WorkflowAlternative] = []
        if recipe is None:
            issues.append(GuidanceIssue("video_contract", "blocking", "Video planning is awareness-only.", "Use this evidence for investigation; VID-01 must accept clip-data and execution support before a runnable plan exists."))
        else:
            profile = _profile_for_recipe(recipe, str(record["family"]), str(record["version"]))
            recommended = _configuration(
                record, recipe, machine, image_count,
                goal=goal, goal_kind=request.goal_kind,
            )
            selected = recommended
            if current_configuration is not None:
                if (current_configuration.target_model_family.casefold(), current_configuration.target_model_version.casefold()) == (str(record["family"]).casefold(), str(record["version"]).casefold()):
                    selected = current_configuration
                else:
                    issues.append(GuidanceIssue("preserved_target_mismatch", "caution", "The existing expert configuration targets another model version and was preserved separately.", "Compare it with this recommendation; applying the new target requires a deliberate choice."))
            adapter = recipe["adapter"]
            adapter_key = (str(adapter.get("id", "")), int(adapter.get("version", 0)))
            adapter_status = "tested_adapter" if adapter.get("status") == "tested_adapter" and adapter_key in set(available_adapters) else ("known_but_unavailable" if adapter.get("status") == "tested_adapter" else str(adapter.get("status")))
            registered = _registered_for(registry_snapshot, request.base_model_id, str(record["family"]))
            verified = request.base_model_id in set(verified_resource_ids)
            if request.base_model_id:
                selected = replace(selected, base_model_id=request.base_model_id)
                recommended = replace(recommended, base_model_id=request.base_model_id)
            if not registered:
                issues.append(GuidanceIssue("exact_model_missing", "blocking", "The exact base model is not registered for this family.", "Choose and register the exact checkpoint in the shared model workspace."))
            elif not verified:
                issues.append(GuidanceIssue("model_recheck_required", "blocking", "The registered model bytes still need a fresh identity check.", "Verify the exact MOD-01 resource before any deliberate launch."))
            if adapter_status != "tested_adapter":
                issues.append(GuidanceIssue("adapter_unavailable", "blocking", "No available tested adapter matches this recipe and version.", "Keep the source-neutral preview or qualify the exact adapter through TRN-03/TRN-04."))
            for requirement, assessment in zip(requirements, assessments):
                if assessment.ready:
                    continue
                level = "blocking" if assessment.blocking else ("caution" if requirement.necessity == "recommended" else "information")
                issues.append(GuidanceIssue(
                    f"companion_{assessment.availability}_{requirement.slot}", level,
                    assessment.summary,
                    f"Choose the exact {requirement.family} {requirement.role.replace('_', ' ')} for {requirement.slot.replace('_', ' ')} and verify it in Models.",
                ))
            minimum_storage = float(recipe.get("minimum_storage_gib", 0))
            if machine.free_storage_gib is None:
                issues.append(GuidanceIssue("storage_unknown", "caution", "Available model storage is unknown.", "Open Setup and recheck the configured model location before staging resources."))
            elif machine.free_storage_gib < minimum_storage:
                issues.append(GuidanceIssue("storage_low", "blocking", f"This preview reserves at least {minimum_storage:g} GiB, but only {machine.free_storage_gib:.1f} GiB is detected.", "Free space or choose a documented lower-storage/export-only alternative."))
            minimum_ram = float(recipe.get("minimum_ram_gib", 0))
            if machine.ram_gib is None:
                issues.append(GuidanceIssue("ram_unknown", "caution", "Installed system RAM is unknown.", "Recheck machine capacity before staging or launching a trainer."))
            elif machine.ram_gib < minimum_ram:
                issues.append(GuidanceIssue("ram_low", "blocking", f"This preview expects at least {minimum_ram:g} GiB system RAM; {machine.ram_gib:.1f} GiB was detected.", "Use an export-only plan or supported hardware; reduced settings are not claimed equivalent."))
            minimum_vram = float(recipe.get("minimum_vram_gib", 0))
            if machine.gpu_backend not in {"cuda", "cpu"}:
                issues.append(GuidanceIssue("backend_unknown", "blocking", "The selected backend is unknown or unsupported by this recipe.", "Use Setup to detect a supported backend or keep this as an export-only preview."))
            elif machine.gpu_backend == "cpu":
                issues.append(GuidanceIssue("gpu_unavailable", "blocking", "This catalog recipe has no tested CPU training adapter.", "Keep the dataset/configuration preview and move it manually only to a trainer that confirms CPU support."))
            elif machine.gpu_vram_gib is None:
                issues.append(GuidanceIssue("vram_unknown", "caution", "Dedicated VRAM is unknown.", "Run Setup detection before choosing memory-sensitive settings."))
            elif machine.gpu_vram_gib < minimum_vram:
                issues.append(GuidanceIssue("vram_low", "blocking", f"The recipe evidence expects at least {minimum_vram:g} GiB VRAM; {machine.gpu_vram_gib:.1f} GiB was detected.", "Use the lower-resource preview or select supported hardware; lower settings are not claimed equivalent."))
            if machine.competing_gpu_jobs:
                issues.append(GuidanceIssue("competing_jobs", "caution", f"{machine.competing_gpu_jobs} GPU job(s) are active.", "Wait for GPU work to finish and recheck free memory before launch."))
            if machine.measurement_fingerprint and not machine.measurement_current:
                issues.append(GuidanceIssue("measurement_stale", "caution", "Saved performance evidence is stale for this hardware/runtime snapshot.", "Rerun the applicable synthetic calibration before using time estimates."))
            readiness_codes = {
                "exact_model_missing", "model_recheck_required", "adapter_unavailable",
                "storage_unknown", "storage_low", "ram_unknown", "ram_low", "backend_unknown",
                "gpu_unavailable", "vram_unknown", "vram_low", "competing_jobs",
                "measurement_stale",
            }
            machine_status = "ready_after_preflight" if (
                not any(item.blocking for item in assessments)
                and not any(item.code in readiness_codes for item in issues)
            ) else "not_ready"
            alternatives.extend((
                WorkflowAlternative("Lower resource", "Batch size 1 reduces peak memory and increases the number of optimizer passes; quality equivalence is unmeasured.", (("batch_size", 1), ("gradient_accumulation", 4))),
                WorkflowAlternative("Faster iteration", "A larger batch can improve throughput on high-memory GPUs; it still needs a fresh memory preflight.", (("batch_size", min(4, max(1, recommended.batch_size * 2))),)),
                WorkflowAlternative("Export only", "Keep the versioned captions and neutral configuration for manual review without claiming a runnable adapter.", ()),
            ))
        if recipe is not None and requirements:
            portable = _resource_binding_evidence(
                str(record["id"]), "training", str(recipe["id"]), int(recipe["version"]),
                requirements, assessments,
            )
            guidance_json = model_guidance_evidence_json(portable)
            if recommended is not None:
                recommended = replace(recommended, model_guidance_json=guidance_json)
            if selected is not None:
                selected = replace(selected, model_guidance_json=guidance_json)
        source_urls = tuple(str(source["url"]) for source in record["sources"])
        limitations = tuple(str(item) for item in (record.get("limitations") or ())) + tuple(str(item) for item in ((recipe or {}).get("limitations") or ()))
        explanation = (
            f"{record['family']} {record['version']} matches the {request.goal_kind} goal in the dated offline catalog. "
            f"The {machine.evidence} machine snapshot changes resource settings only; it does not predict training quality or creative fidelity."
        )
        if selected is not None and not any(
                issue.level == "blocking" for issue in validate_training_configuration(selected)):
            native = preview_training_preset(selected, preset_id="current",
                training_image_count=image_count or None)
            if native.native_status != "native_candidate":
                issues.append(GuidanceIssue("native_training_settings_incompatible", "caution",
                    "These settings are for an external/manual trainer.",
                    " ".join(native.native_reasons) + " Use an explicit local-adapter preset preview before native preparation."))
        return WorkflowGuidancePlan(
            request, str(record["id"]), str(record["family"]), str(record["version"]), str(record["modality"]),
            str(record["catalog_status"]), adapter_status, machine_status, explanation,
            profile, recommended, selected, current_configuration, tuple(issues), tuple(alternatives), source_urls, limitations,
            recipe_id=str(recipe["id"]) if recipe is not None else "",
            recipe_version=int(recipe["version"]) if recipe is not None else 0,
            companion_requirements=requirements, companion_assessments=assessments,
            planned_image_upper_bound=image_count or None,
        )

    @staticmethod
    def project_confirmed_caption(
        plan: WorkflowGuidancePlan, annotation: object, *, knowledge=None,
    ) -> str:
        """Project through the accepted versioned profile; source knowledge is untouched."""
        if plan.caption_profile is None:
            raise ModelWorkflowKnowledgeError("This target has no image-caption projection.")
        profile = plan.caption_profile
        return profile.project(annotation, knowledge=knowledge).render(profile.separator)

    def preview_update_file(self, incoming_path: str | Path) -> CatalogMergePreview:
        self.require_current_catalog()
        return self.preview_update(self.catalog, _read_catalog(incoming_path))

    def require_current_catalog(self):
        """A loaded catalog is not current merely because a later write token is fresh."""
        self._require_reconciled_update()
        if self.catalog_write_token() != self._loaded_token:
            raise ModelWorkflowKnowledgeError("Saved knowledge changed; Recover / Reload Knowledge before a new preview.")

    def apply_update_file(
        self, incoming_path: str | Path, *, expected_catalog_sha256: str,
        expected_incoming_sha256: str,
    ) -> CatalogMergePreview:
        if self.update_path is None:
            raise ModelWorkflowKnowledgeError(
                "The shipped baseline cannot be replaced. Configure a user-data update path."
            )
        with self._update_writer():
            self._require_reconciled_update()
            if self.catalog_write_token() != expected_catalog_sha256:
                raise ModelWorkflowKnowledgeError(
                    "The local catalog changed after preview; reload and retry."
                )
            incoming_raw = _bounded_read(incoming_path)
            assert incoming_raw is not None
            if hashlib.sha256(incoming_raw).hexdigest() != expected_incoming_sha256:
                raise ModelWorkflowKnowledgeError(
                    "The selected update file changed after preview; choose it again."
                )
            incoming = _strict_json(incoming_raw)
            validate_catalog(incoming)
            current_path = self.update_path if self.update_path.exists() else self.baseline_path
            current_raw = _bounded_read(current_path)
            assert current_raw is not None
            current = _strict_json(current_raw)
            validate_catalog(current)
            result = self.preview_update(current, incoming)
            if not result.changed:
                self._reload_catalog()
                return CatalogMergePreview(deepcopy(self.catalog), result.issues, False)
            encoded = json.dumps(
                result.payload, indent=2, ensure_ascii=False, allow_nan=False,
            ).encode("utf-8") + b"\n"
            if len(encoded) > MAX_CATALOG_BYTES:
                raise ModelWorkflowKnowledgeError("The merged model-workflow catalog is too large.")
            recovery = self.update_path.with_name(self.update_path.name + ".recovery")
            _assert_unredirected(recovery)
            recovery.mkdir(parents=True, exist_ok=True)
            attempt = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex
            before = recovery / f"{attempt}.before.json"
            after = recovery / f"{attempt}.after.json"
            receipt = recovery / f"{attempt}.recovery.json"
            atomic_verified_bytes(before, current_raw, label="Model-workflow before copy")
            atomic_verified_bytes(after, encoded, label="Model-workflow proposed copy")
            atomic_verified_bytes(
                receipt,
                (json.dumps({
                    "schema_version": 1,
                    "kind": "collectigen_model_workflow_update_recovery",
                    "status": "prepared",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "target": self.update_path.name,
                    "before_exists": current_path == self.update_path,
                    "before": before.name,
                    "before_sha256": hashlib.sha256(current_raw).hexdigest(),
                    "after": after.name,
                    "after_sha256": hashlib.sha256(encoded).hexdigest(),
                    "source_sha256": expected_incoming_sha256,
                }, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"),
                label="Model-workflow recovery receipt",
            )
            if self.catalog_write_token() != expected_catalog_sha256:
                raise ModelWorkflowKnowledgeError("Saved knowledge changed before publication; recover the retained attempt before continuing.")
            _assert_unredirected(self.update_path)
            atomic_verified_bytes(self.update_path, encoded, label="Model-workflow knowledge update")
            verified = _read_catalog(self.update_path)
            if _fingerprint(verified) != _fingerprint(result.payload):
                raise OSError("The model-workflow update failed semantic read-back verification.")
            receipt_payload = _strict_json(_bounded_read(receipt) or b"")
            assert isinstance(receipt_payload, dict)
            receipt_payload["status"] = "applied"
            receipt_payload["applied_at"] = datetime.now(timezone.utc).isoformat()
            atomic_verified_bytes(
                receipt,
                (json.dumps(receipt_payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"),
                label="Model-workflow recovery receipt finalize",
            )
            published_token = hashlib.sha256(encoded).hexdigest()
            if self.catalog_write_token() != published_token:
                raise ModelWorkflowKnowledgeError("Saved knowledge changed after publication; Recover / Reload Knowledge before continuing.")
            self.catalog_path = self.update_path
            self.catalog = verified
            self._loaded_token = published_token
            return result

    @staticmethod
    def preview_update(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> CatalogMergePreview:
        validate_catalog(current); validate_catalog(incoming)
        merged = deepcopy(dict(current)); issues: list[str] = []
        existing = {str(item["id"]): deepcopy(item) for item in current["records"]}
        for update in incoming["records"]:
            record_id = str(update["id"]); local = existing.get(record_id)
            if local is None:
                existing[record_id] = deepcopy(update); continue
            incoming_date = max(date.fromisoformat(str(source["checked_on"])) for source in update["sources"])
            local_date = max(date.fromisoformat(str(source["checked_on"])) for source in local["sources"])
            if incoming_date < local_date:
                issues.append(f"Ignored stale update for {record_id}."); continue
            if incoming_date == local_date and _fingerprint(update) != _fingerprint(local):
                issues.append(f"Retained conflicting same-date evidence for {record_id}."); continue
            existing[record_id] = _merge_record_extensions(local, update)
        merged.update({key: deepcopy(value) for key, value in incoming.items() if key in {"catalog_version", "as_of", "reviewer"}})
        merged["records"] = list(existing.values())
        validate_catalog(merged)
        return CatalogMergePreview(merged, tuple(issues), _fingerprint(merged) != _fingerprint(current))

    @staticmethod
    def apply_update(
        local_path: str | Path, incoming_path: str | Path, *,
        expected_local_sha256: str,
    ) -> CatalogMergePreview:
        service = ModelWorkflowGuidanceService(local_path)
        return service.apply_update_file(
            incoming_path,
            expected_catalog_sha256=expected_local_sha256,
            expected_incoming_sha256=service.source_file_fingerprint(incoming_path),
        )
