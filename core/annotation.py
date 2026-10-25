from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .ai_output import generated_completion_text
from .tag_collection import TagCollection,normalize_tag
from version import APP_TITLE

SCHEMA_VERSION = 6
CHARACTER_TAG_GROUPS = (
    "appearance",
    "clothing",
    "pose",
    "expression",
    "actions",
    "other",
)
GLOBAL_TAG_GROUPS = (
    "subject_count",
    "composition",
    "quality",
    "style",
    "other",
)
SCENE_TAG_GROUPS = (
    "location",
    "background",
    "lighting",
    "weather",
    "time",
    "other",
)
CONTEXT_KINDS = (
    "seen_through",
    "reflected_in",
    "shown_on_screen",
    "depicted_in_picture",
)
CONTEXT_KIND_LABELS = {
    "seen_through": "Seen Through",
    "reflected_in": "Reflected In",
    "shown_on_screen": "Shown On Screen",
    "depicted_in_picture": "Depicted In Picture",
}


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tag_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return TagCollection(str(value) for value in values).to_list()


def _group_dict(values: Any, allowed_groups: tuple[str, ...]) -> dict[str, list[str]]:
    source = values if isinstance(values, dict) else {}
    result: dict[str, list[str]] = {}

    for group in allowed_groups:
        result[group] = _tag_list(source.get(group, []))

    # Preserve future/custom groups without silently dropping data.
    for group, tags in source.items():
        if group not in result and isinstance(group, str):
            result[group] = _tag_list(tags)

    return result


def _unknown_fields(data: Any, known: set[str]) -> dict[str, Any]:
    """Retain fields introduced by newer producers across a read/write cycle."""
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if key not in known}


@dataclass
class CharacterAnnotation:
    slot: int
    id: str = field(default_factory=lambda: f"char_{uuid4().hex[:10]}")
    character_id: str | None = None
    role: str | None = None
    outfit_ids: list[str] = field(default_factory=list)
    tags: dict[str, list[str]] = field(
        default_factory=lambda: {group: [] for group in CHARACTER_TAG_GROUPS}
    )
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_named(self) -> bool:
        return bool(self.character_id)

    def all_tags(self) -> list[str]:
        result = TagCollection()
        for values in self.tags.values():
            result.extend(values)
        return result.to_list()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "slot": self.slot,
            "character_id": self.character_id,
            "role": self.role,
            "outfit_ids": list(self.outfit_ids),
            "tags": {key: list(values) for key, values in self.tags.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_slot: int) -> CharacterAnnotation:
        slot = data.get("slot", fallback_slot)
        try:
            slot = max(1, int(slot))
        except (TypeError, ValueError):
            slot = fallback_slot

        return cls(
            id=_clean_string(data.get("id")) or f"char_{uuid4().hex[:10]}",
            slot=slot,
            character_id=_clean_string(data.get("character_id")),
            role=_clean_string(data.get("role")),
            outfit_ids=_tag_list(data.get("outfit_ids", [])),
            tags=_group_dict(data.get("tags"), CHARACTER_TAG_GROUPS),
            extra_fields=_unknown_fields(
                data, {"id", "slot", "character_id", "role", "outfit_ids", "tags"}
            ),
        )


@dataclass
class ObjectAnnotation:
    """A reviewed, per-image object owner for exact object-scoped facts."""

    slot: int
    id: str = field(default_factory=lambda: f"obj_{uuid4().hex[:10]}")
    identity_tag_id: str | None = None
    identity_tag: str | None = None
    tags: list[str] = field(default_factory=list)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_identified(self) -> bool:
        return bool(self.identity_tag_id and self.identity_tag)

    def all_tags(self) -> list[str]:
        result = TagCollection()
        if self.identity_tag:
            result.add(self.identity_tag)
        result.extend(self.tags)
        return result.to_list()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "slot": self.slot,
            "identity_tag_id": self.identity_tag_id,
            "identity_tag": self.identity_tag,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_slot: int) -> ObjectAnnotation:
        slot = data.get("slot", fallback_slot)
        try:
            slot = max(1, int(slot))
        except (TypeError, ValueError):
            slot = fallback_slot

        return cls(
            id=_clean_string(data.get("id")) or f"obj_{uuid4().hex[:10]}",
            slot=slot,
            identity_tag_id=_clean_string(data.get("identity_tag_id")),
            identity_tag=_clean_string(data.get("identity_tag")),
            tags=_tag_list(data.get("tags", [])),
            extra_fields=_unknown_fields(
                data, {"id", "slot", "identity_tag_id", "identity_tag", "tags"}
            ),
        )


@dataclass
class ImageContextAnnotation:
    """One stable secondary view containing confirmed per-image facts."""

    kind: str
    id: str = field(default_factory=lambda: f"ctx_{uuid4().hex[:10]}")
    label: str = ""
    owner_kind: str = ""
    owner_id: str = ""
    owner_label: str = ""
    tags: list[str] = field(default_factory=list)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def display_label(self) -> str:
        if clean := _clean_string(self.label):
            return clean
        prefix = CONTEXT_KIND_LABELS.get(
            self.kind,
            str(self.kind or "Context").replace("_", " ").title(),
        )
        owner = _clean_string(self.owner_label)
        owner = owner[:1].upper() + owner[1:] if owner else ""
        return f"{prefix} {owner}" if owner else prefix

    def all_tags(self) -> list[str]:
        return TagCollection(self.tags).to_list()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "kind": self.kind,
            "label": self.display_label,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "owner_label": self.owner_label,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageContextAnnotation:
        return cls(
            id=_clean_string(data.get("id")) or f"ctx_{uuid4().hex[:10]}",
            kind=_clean_string(data.get("kind")) or "seen_through",
            label=_clean_string(data.get("label")) or "",
            owner_kind=_clean_string(data.get("owner_kind")) or "",
            owner_id=_clean_string(data.get("owner_id")) or "",
            owner_label=_clean_string(data.get("owner_label")) or "",
            tags=_tag_list(data.get("tags", [])),
            extra_fields=_unknown_fields(
                data,
                {
                    "id", "kind", "label", "owner_kind", "owner_id",
                    "owner_label", "tags",
                },
            ),
        )


@dataclass
class VisualStyleAnnotation:
    relationship: str = "unknown"
    source_series_ids: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    notes: str = ""
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "relationship": self.relationship,
            "source_series_ids": list(self.source_series_ids),
            "style_tags": list(self.style_tags),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> VisualStyleAnnotation:
        value = data if isinstance(data, dict) else {}
        relationship = str(value.get("relationship") or "unknown")
        if relationship not in {"original", "modified", "cross_source", "mixed", "unknown"}:
            relationship = "unknown"
        return cls(
            relationship=relationship,
            source_series_ids=_tag_list(value.get("source_series_ids", [])),
            style_tags=_tag_list(value.get("style_tags", [])),
            notes=str(value.get("notes") or ""),
            extra_fields=_unknown_fields(
                value, {"relationship", "source_series_ids", "style_tags", "notes"}
            ),
        )


@dataclass
class AIEvidence:
    """A durable model observation, separate from any policy-confirmed image fact."""

    id: str = field(default_factory=lambda: f"ai_{uuid4().hex[:12]}")
    model_id: str = ""
    model_revision: str = ""
    kind: str = "sdxl_tags"
    prompt: str = ""
    created_at: str = ""
    image_hash: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    observed_tags: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    plain_language: str = ""
    raw_output: Any = None
    decisions: dict[str, str] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    VALID_KINDS = {"sdxl_tags", "plain_language"}

    def typed_tags(self) -> list[dict[str, Any]]:
        return list(self.tags) if self.kind == "sdxl_tags" else []

    def typed_observed_tags(self) -> list[dict[str, Any]]:
        """Return the exact structured model observations when available."""
        if self.kind != "sdxl_tags":
            return []
        return list(self.observed_tags or self.tags)

    def typed_plain_language(self) -> str:
        return self.plain_language if self.kind == "plain_language" else ""

    def type_issues(self) -> list[str]:
        issues: list[str] = []
        if self.kind not in self.VALID_KINDS:
            issues.append(f"AI evidence has unsupported kind {self.kind!r}.")
        if self.kind != "sdxl_tags" and (self.observed_tags or self.tags):
            issues.append("AI evidence contains SDXL Tag values under a non-tag kind.")
        if self.kind != "plain_language" and self.plain_language:
            issues.append("AI evidence contains Plain Language under a non-description kind.")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "kind": self.kind,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "image_hash": self.image_hash,
            "settings": dict(self.settings),
            "observed_tags": list(self.observed_tags),
            "tags": list(self.tags),
            "plain_language": self.plain_language,
            "raw_output": self.raw_output,
            "decisions": dict(self.decisions),
        }

    @classmethod
    def from_dict(cls, data: Any) -> AIEvidence:
        value = data if isinstance(data, dict) else {}
        tags = value.get("tags", [])
        observed_tags = value.get("observed_tags", [])
        prompt = str(value.get("prompt") or "")
        kind = str(value.get("kind") or "")
        plain_language = str(value.get("plain_language") or "")
        if not kind:
            if plain_language and not tags and not observed_tags:
                kind = "plain_language"
            elif (tags or observed_tags) and not plain_language:
                kind = "sdxl_tags"
        if plain_language and kind == "plain_language":
            plain_language = generated_completion_text(
                plain_language,user_prompt=prompt,
            )
        return cls(
            id=_clean_string(value.get("id")) or f"ai_{uuid4().hex[:12]}",
            model_id=str(value.get("model_id") or ""),
            model_revision=str(value.get("model_revision") or ""),
            kind=kind,
            prompt=prompt,
            created_at=str(value.get("created_at") or ""),
            image_hash=str(value.get("image_hash") or ""),
            settings=value.get("settings", {}) if isinstance(value.get("settings"), dict) else {},
            observed_tags=[dict(item) for item in observed_tags if isinstance(item, dict)]
            if isinstance(observed_tags, list) else [],
            tags=[dict(item) for item in tags if isinstance(item, dict)] if isinstance(tags, list) else [],
            plain_language=plain_language,
            raw_output=value.get("raw_output"),
            decisions={str(key): str(item) for key, item in value.get("decisions", {}).items()}
            if isinstance(value.get("decisions"), dict) else {},
            extra_fields=_unknown_fields(
                value,
                {
                    "id", "model_id", "model_revision", "kind", "prompt",
                    "created_at", "image_hash", "settings", "observed_tags", "tags",
                    "plain_language", "raw_output", "decisions",
                },
            ),
        )


@dataclass
class GenerationResource:
    """A model or extension resource referenced by a generation request."""

    id: str = field(default_factory=lambda: f"res_{uuid4().hex[:12]}")
    role: str = "extension"
    name: str = ""
    hashes: dict[str, str] = field(default_factory=dict)
    source_id: str = ""
    version_id: str = ""
    compatibility: list[str] = field(default_factory=list)
    weights: dict[str, float | str] = field(default_factory=dict)
    trigger_words: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "hashes": dict(self.hashes),
            "source_id": self.source_id,
            "version_id": self.version_id,
            "compatibility": list(self.compatibility),
            "weights": dict(self.weights),
            "trigger_words": list(self.trigger_words),
            "provenance": dict(self.provenance),
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(cls, data: Any) -> GenerationResource:
        value = data if isinstance(data, dict) else {}
        return cls(
            id=_clean_string(value.get("id")) or f"res_{uuid4().hex[:12]}",
            role=str(value.get("role") or "extension"),
            name=str(value.get("name") or ""),
            hashes={str(key): str(item) for key, item in value.get("hashes", {}).items()}
            if isinstance(value.get("hashes"), dict) else {},
            source_id=str(value.get("source_id") or ""),
            version_id=str(value.get("version_id") or ""),
            compatibility=[str(item) for item in value.get("compatibility", [])]
            if isinstance(value.get("compatibility"), list) else [],
            weights={str(key): item for key, item in value.get("weights", {}).items()}
            if isinstance(value.get("weights"), dict) else {},
            trigger_words=[str(item) for item in value.get("trigger_words", [])]
            if isinstance(value.get("trigger_words"), list) else [],
            provenance=dict(value.get("provenance", {}))
            if isinstance(value.get("provenance"), dict) else {},
            extras=dict(value.get("extras", {}))
            if isinstance(value.get("extras"), dict) else {},
            extra_fields=_unknown_fields(
                value,
                {
                    "id", "role", "name", "hashes", "source_id", "version_id",
                    "compatibility", "weights", "trigger_words", "provenance", "extras",
                },
            ),
        )


@dataclass
class GenerationInput:
    """A durable reference to an init image, mask, or control input."""

    kind: str = "image"
    reference: str = ""
    source_node: str = ""
    strength: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "kind": self.kind,
            "reference": self.reference,
            "source_node": self.source_node,
            "strength": self.strength,
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(cls, data: Any) -> GenerationInput:
        value = data if isinstance(data, dict) else {}
        return cls(
            kind=str(value.get("kind") or "image"),
            reference=str(value.get("reference") or ""),
            source_node=str(value.get("source_node") or ""),
            strength=_optional_float(value.get("strength")),
            extras=dict(value.get("extras", {}))
            if isinstance(value.get("extras"), dict) else {},
            extra_fields=_unknown_fields(
                value, {"kind", "reference", "source_node", "strength", "extras"}
            ),
        )


@dataclass
class GenerationStage:
    """One ordered first-pass, hires, refiner, control, or post-process stage."""

    id: str = field(default_factory=lambda: f"stage_{uuid4().hex[:12]}")
    kind: str = "first_pass"
    label: str = ""
    positive_prompt: str = ""
    negative_prompt: str = ""
    seeds: list[int | str] = field(default_factory=list)
    batch_index: int | None = None
    batch_size: int | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    cfg_scale: float | None = None
    sampler: str = ""
    scheduler: str = ""
    denoising_strength: float | None = None
    resource_ids: list[str] = field(default_factory=list)
    inputs: list[GenerationInput] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "seeds": list(self.seeds),
            "batch_index": self.batch_index,
            "batch_size": self.batch_size,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg_scale": self.cfg_scale,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "denoising_strength": self.denoising_strength,
            "resource_ids": list(self.resource_ids),
            "inputs": [item.to_dict() for item in self.inputs],
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(cls, data: Any) -> GenerationStage:
        value = data if isinstance(data, dict) else {}
        seeds = value.get("seeds", [])
        return cls(
            id=_clean_string(value.get("id")) or f"stage_{uuid4().hex[:12]}",
            kind=str(value.get("kind") or "first_pass"),
            label=str(value.get("label") or ""),
            positive_prompt=str(value.get("positive_prompt") or ""),
            negative_prompt=str(value.get("negative_prompt") or ""),
            seeds=[item for item in seeds if isinstance(item, (int, str))]
            if isinstance(seeds, list) else [],
            batch_index=_optional_int(value.get("batch_index")),
            batch_size=_optional_int(value.get("batch_size")),
            width=_optional_int(value.get("width")),
            height=_optional_int(value.get("height")),
            steps=_optional_int(value.get("steps")),
            cfg_scale=_optional_float(value.get("cfg_scale")),
            sampler=str(value.get("sampler") or ""),
            scheduler=str(value.get("scheduler") or ""),
            denoising_strength=_optional_float(value.get("denoising_strength")),
            resource_ids=[str(item) for item in value.get("resource_ids", [])]
            if isinstance(value.get("resource_ids"), list) else [],
            inputs=[GenerationInput.from_dict(item) for item in value.get("inputs", [])]
            if isinstance(value.get("inputs"), list) else [],
            extras=dict(value.get("extras", {}))
            if isinstance(value.get("extras"), dict) else {},
            extra_fields=_unknown_fields(
                value,
                {
                    "id", "kind", "label", "positive_prompt", "negative_prompt", "seeds",
                    "batch_index", "batch_size", "width", "height", "steps", "cfg_scale",
                    "sampler", "scheduler", "denoising_strength", "resource_ids", "inputs", "extras",
                },
            ),
        )


@dataclass
class GenerationSettingResult:
    """Loss accounting for one source or target generation setting."""

    source_path: str = ""
    neutral_path: str = ""
    status: str = "supported"
    detail: str = ""
    value: Any = None
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "source_path": self.source_path,
            "neutral_path": self.neutral_path,
            "status": self.status,
            "detail": self.detail,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Any) -> GenerationSettingResult:
        value = data if isinstance(data, dict) else {}
        status = str(value.get("status") or "untranslated")
        if status not in {"supported", "translated", "approximated", "untranslated", "unsupported"}:
            status = "untranslated"
        return cls(
            source_path=str(value.get("source_path") or ""),
            neutral_path=str(value.get("neutral_path") or ""),
            status=status,
            detail=str(value.get("detail") or ""),
            value=value.get("value"),
            extra_fields=_unknown_fields(
                value, {"source_path", "neutral_path", "status", "detail", "value"}
            ),
        )


@dataclass
class GenerationRecord:
    """Reproducible image-generation settings preserved separately from tags."""

    id: str = field(default_factory=lambda: f"gen_{uuid4().hex[:12]}")
    source: str = ""
    source_field: str = ""
    format: str = "unknown"
    generator_name: str = ""
    generator_version: str = ""
    positive_prompt: str = ""
    negative_prompt: str = ""
    model: str = ""
    model_hash: str = ""
    vae: str = ""
    sampler: str = ""
    scheduler: str = ""
    seed: int | str | None = None
    steps: int | None = None
    cfg_scale: float | None = None
    width: int | None = None
    height: int | None = None
    denoising_strength: float | None = None
    loras: list[dict[str, Any]] = field(default_factory=list)
    resources: list[GenerationResource] = field(default_factory=list)
    stages: list[GenerationStage] = field(default_factory=list)
    adapter_report: list[GenerationSettingResult] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "id": self.id,
            "source": self.source,
            "source_field": self.source_field,
            "format": self.format,
            "generator_name": self.generator_name,
            "generator_version": self.generator_version,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "model": self.model,
            "model_hash": self.model_hash,
            "vae": self.vae,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "seed": self.seed,
            "steps": self.steps,
            "cfg_scale": self.cfg_scale,
            "width": self.width,
            "height": self.height,
            "denoising_strength": self.denoising_strength,
            "loras": [dict(item) for item in self.loras],
            "resources": [item.to_dict() for item in self.resources],
            "stages": [item.to_dict() for item in self.stages],
            "adapter_report": [item.to_dict() for item in self.adapter_report],
            "extras": dict(self.extras),
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: Any) -> GenerationRecord:
        value = data if isinstance(data, dict) else {}
        return cls(
            id=_clean_string(value.get("id")) or f"gen_{uuid4().hex[:12]}",
            source=str(value.get("source") or ""),
            source_field=str(value.get("source_field") or ""),
            format=str(value.get("format") or "unknown"),
            generator_name=str(value.get("generator_name") or ""),
            generator_version=str(value.get("generator_version") or ""),
            positive_prompt=str(value.get("positive_prompt") or ""),
            negative_prompt=str(value.get("negative_prompt") or ""),
            model=str(value.get("model") or ""),
            model_hash=str(value.get("model_hash") or ""),
            vae=str(value.get("vae") or ""),
            sampler=str(value.get("sampler") or ""),
            scheduler=str(value.get("scheduler") or ""),
            seed=value.get("seed"),
            steps=_optional_int(value.get("steps")),
            cfg_scale=_optional_float(value.get("cfg_scale")),
            width=_optional_int(value.get("width")),
            height=_optional_int(value.get("height")),
            denoising_strength=_optional_float(value.get("denoising_strength")),
            loras=[dict(item) for item in value.get("loras", []) if isinstance(item, dict)]
            if isinstance(value.get("loras"), list) else [],
            resources=[GenerationResource.from_dict(item) for item in value.get("resources", [])]
            if isinstance(value.get("resources"), list) else [],
            stages=[GenerationStage.from_dict(item) for item in value.get("stages", [])]
            if isinstance(value.get("stages"), list) else [],
            adapter_report=[GenerationSettingResult.from_dict(item) for item in value.get("adapter_report", [])]
            if isinstance(value.get("adapter_report"), list) else [],
            extras=value.get("extras", {}) if isinstance(value.get("extras"), dict) else {},
            raw=value.get("raw"),
            extra_fields=_unknown_fields(
                value,
                {
                    "id", "source", "source_field", "format", "generator_name",
                    "generator_version", "positive_prompt",
                    "negative_prompt", "model", "model_hash", "vae", "sampler",
                    "scheduler", "seed", "steps", "cfg_scale", "width", "height",
                    "denoising_strength", "loras", "resources", "stages",
                    "adapter_report", "extras", "raw",
                },
            ),
        )

    def identity(self) -> tuple[str, str, str]:
        return (self.source.casefold(), self.source_field.casefold(), repr(self.raw))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class ImageAnnotation:
    image: str
    schema_version: int = SCHEMA_VERSION
    global_tags: dict[str, list[str]] = field(
        default_factory=lambda: {group: [] for group in GLOBAL_TAG_GROUPS}
    )
    scene: dict[str, list[str]] = field(
        default_factory=lambda: {group: [] for group in SCENE_TAG_GROUPS}
    )
    characters: list[CharacterAnnotation] = field(default_factory=list)
    objects: list[ObjectAnnotation] = field(default_factory=list)
    contexts: list[ImageContextAnnotation] = field(default_factory=list)
    sdxl_tags: list[str] = field(default_factory=list)
    plain_language: list[str] = field(default_factory=list)
    uncategorized: list[str] = field(default_factory=list)
    tag_identities: dict[str, str] = field(default_factory=dict)
    confirmed_absent: list[str] = field(default_factory=list)
    # Kept as ``excluded`` in memory for compatibility with existing editors;
    # schema v3 serializes the unambiguous name ``export_excluded``.
    excluded: list[str] = field(default_factory=list)
    custom_groups: dict[str, list[str]] = field(default_factory=dict)
    review_status: str = "needs_review"
    source: dict[str, Any] = field(default_factory=dict)
    visual_style: VisualStyleAnnotation = field(default_factory=VisualStyleAnnotation)
    ai_evidence: list[AIEvidence] = field(default_factory=list)
    generation_records: list[GenerationRecord] = field(default_factory=list)
    standard_metadata: dict[str, Any] = field(default_factory=dict)
    file_identity: dict[str, Any] = field(default_factory=dict)
    projection_state: dict[str, Any] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def export_excluded(self) -> list[str]:
        return self.excluded

    @export_excluded.setter
    def export_excluded(self, values: list[str]) -> None:
        self.excluded = TagCollection(values).to_list()

    def add_generation_record(self, record: GenerationRecord) -> bool:
        identities = {item.identity() for item in self.generation_records}
        if record.identity() in identities:
            return False
        self.generation_records.append(record)
        return True

    def add_character(
        self,
        *,
        character_id: str | None = None,
        role: str | None = None,
    ) -> CharacterAnnotation:
        used = {character.slot for character in self.characters}
        slot = 1
        while slot in used:
            slot += 1

        character = CharacterAnnotation(
            slot=slot,
            character_id=_clean_string(character_id),
            role=_clean_string(role),
        )
        self.characters.append(character)
        self.sort_characters()
        return character

    def get_character(self, slot: int) -> CharacterAnnotation | None:
        return next(
            (character for character in self.characters if character.slot == slot),
            None,
        )

    def sort_characters(self) -> None:
        self.characters.sort(key=lambda character: character.slot)

    def add_object(
        self,
        *,
        identity_tag_id: str | None = None,
        identity_tag: str | None = None,
    ) -> ObjectAnnotation:
        used = {item.slot for item in self.objects}
        slot = 1
        while slot in used:
            slot += 1

        item = ObjectAnnotation(
            slot=slot,
            identity_tag_id=_clean_string(identity_tag_id),
            identity_tag=_clean_string(identity_tag),
        )
        self.objects.append(item)
        self.sort_objects()
        return item

    def get_object(self, object_id: str) -> ObjectAnnotation | None:
        return next((item for item in self.objects if item.id == object_id), None)

    def sort_objects(self) -> None:
        self.objects.sort(key=lambda item: item.slot)

    def add_context(
        self,
        *,
        kind: str,
        label: str = "",
        owner_kind: str = "",
        owner_id: str = "",
        owner_label: str = "",
    ) -> ImageContextAnnotation:
        context = ImageContextAnnotation(
            kind=kind,
            label=label,
            owner_kind=owner_kind,
            owner_id=owner_id,
            owner_label=owner_label,
        )
        self.contexts.append(context)
        return context

    def get_context(self, context_id: str) -> ImageContextAnnotation | None:
        return next((item for item in self.contexts if item.id == context_id), None)

    def all_structured_tags(self, *, include_contexts: bool = True) -> list[str]:
        result = TagCollection()

        for values in self.global_tags.values():
            result.extend(values)
        for character in sorted(self.characters, key=lambda item: item.slot):
            result.extend(character.all_tags())
        for item in sorted(self.objects, key=lambda value: value.slot):
            result.extend(item.all_tags())
        for values in self.scene.values():
            result.extend(values)
        for values in self.custom_groups.values():
            result.extend(values)
        result.extend(self.sdxl_tags)
        result.extend(self.uncategorized)
        result.extend(self.visual_style.style_tags)
        if include_contexts:
            for context in self.contexts:
                result.extend(context.all_tags())

        # Export exclusions are still confirmed image facts. Projection code,
        # not the authoritative record view, decides whether to omit them.
        return result.to_list()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra_fields,
            "schema_version": self.schema_version,
            "image": self.image,
            "global": {key: list(values) for key, values in self.global_tags.items()},
            "scene": {key: list(values) for key, values in self.scene.items()},
            "characters": [character.to_dict() for character in self.characters],
            "objects": [item.to_dict() for item in self.objects],
            "contexts": [item.to_dict() for item in self.contexts],
            "sdxl_tags": list(self.sdxl_tags),
            "plain_language": list(self.plain_language),
            "uncategorized": list(self.uncategorized),
            "tag_identities": dict(self.tag_identities),
            "confirmed_absent": list(self.confirmed_absent),
            "export_excluded": list(self.excluded),
            "custom_groups": {
                key: list(values) for key, values in self.custom_groups.items()
            },
            "review_status": self.review_status,
            "source": dict(self.source),
            "visual_style": self.visual_style.to_dict(),
            "ai_evidence": [item.to_dict() for item in self.ai_evidence],
            "generation_records": [item.to_dict() for item in self.generation_records],
            "standard_metadata": dict(self.standard_metadata),
            "file_identity": dict(self.file_identity),
            "projection_state": dict(self.projection_state),
            "created_by": APP_TITLE,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], image_name: str) -> ImageAnnotation:
        raw_characters = data.get("characters", [])
        characters: list[CharacterAnnotation] = []
        if isinstance(raw_characters, list):
            for index, value in enumerate(raw_characters, start=1):
                if isinstance(value, dict):
                    characters.append(CharacterAnnotation.from_dict(value, index))

        raw_objects = data.get("objects", [])
        objects: list[ObjectAnnotation] = []
        if isinstance(raw_objects, list):
            for index, value in enumerate(raw_objects, start=1):
                if isinstance(value, dict):
                    objects.append(ObjectAnnotation.from_dict(value, index))

        raw_contexts = data.get("contexts", [])
        contexts: list[ImageContextAnnotation] = []
        if isinstance(raw_contexts, list):
            for value in raw_contexts:
                if isinstance(value, dict):
                    contexts.append(ImageContextAnnotation.from_dict(value))

        custom_groups = data.get("custom_groups", {})
        if not isinstance(custom_groups, dict):
            custom_groups = {}

        annotation = cls(
            image=_clean_string(data.get("image")) or image_name,
            schema_version=max(SCHEMA_VERSION, int(data.get("schema_version", SCHEMA_VERSION))),
            global_tags=_group_dict(data.get("global"), GLOBAL_TAG_GROUPS),
            scene=_group_dict(data.get("scene"), SCENE_TAG_GROUPS),
            characters=characters,
            objects=objects,
            contexts=contexts,
            sdxl_tags=_tag_list(data.get("sdxl_tags", [])),
            plain_language=_tag_list(data.get("plain_language", [])),
            uncategorized=_tag_list(data.get("uncategorized", [])),
            tag_identities={
                normalize_tag(key): str(value)
                for key, value in (
                    data.get("tag_identities", {})
                    if isinstance(data.get("tag_identities"), dict)
                    else {}
                ).items()
                if normalize_tag(key) and str(value).strip()
            },
            confirmed_absent=_tag_list(data.get("confirmed_absent", [])),
            excluded=_tag_list(data.get("export_excluded", data.get("excluded", []))),
            custom_groups={
                str(key): _tag_list(value)
                for key, value in custom_groups.items()
                if isinstance(key, str)
            },
            review_status=_clean_string(data.get("review_status")) or "needs_review",
            source=data.get("source", {}) if isinstance(data.get("source"), dict) else {},
            visual_style=VisualStyleAnnotation.from_dict(data.get("visual_style")),
            ai_evidence=[
                AIEvidence.from_dict(item)
                for item in data.get("ai_evidence", [])
                if isinstance(item, dict)
            ] if isinstance(data.get("ai_evidence", []), list) else [],
            generation_records=[
                GenerationRecord.from_dict(item)
                for item in data.get("generation_records", [])
                if isinstance(item, dict)
            ] if isinstance(data.get("generation_records", []), list) else [],
            standard_metadata=data.get("standard_metadata", {})
            if isinstance(data.get("standard_metadata"), dict) else {},
            file_identity=data.get("file_identity", {})
            if isinstance(data.get("file_identity"), dict) else {},
            projection_state=data.get("projection_state", {})
            if isinstance(data.get("projection_state"), dict) else {},
            extra_fields=_unknown_fields(
                data,
                {
                    "schema_version", "image", "global", "scene", "characters", "objects", "contexts",
                    "sdxl_tags", "plain_language", "uncategorized", "tag_identities", "confirmed_absent",
                    "export_excluded", "excluded", "custom_groups", "review_status",
                    "source", "visual_style", "ai_evidence", "generation_records",
                    "standard_metadata", "file_identity", "projection_state", "created_by",
                },
            ),
        )
        annotation.sort_characters()
        annotation.sort_objects()
        return annotation

    @classmethod
    def from_legacy(
        cls,
        *,
        image_name: str,
        tags: list[str],
        caption: list[str],
        source_file: str,
        source_format: str = "legacy_txt",
        ambiguous_fragments: list[str] | None = None,
        raw_text: str = "",
    ) -> ImageAnnotation:
        return cls(
            image=image_name,
            sdxl_tags=TagCollection(tags).to_list(),
            plain_language=TagCollection(caption).to_list(),
            review_status="needs_review",
            source={
                "imported_from": source_file,
                "import_type": "legacy_txt",
                "detected_format": source_format,
                "segments": {
                    "sdxl_tags": len(tags),
                    "plain_language": len(caption),
                    "ambiguous": len(ambiguous_fragments or []),
                },
                "ambiguous_fragments": list(ambiguous_fragments or []),
                "raw_text": raw_text,
            },
        )
