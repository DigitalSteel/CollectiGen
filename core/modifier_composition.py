from __future__ import annotations

import copy
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .automation_rule_conversion import RulePairAdmission, RuleConversionAdmissionError

from .category_registry import reusable_modifier_family_snapshot
from .allowed_dimension_policy import inspect_legacy_finite_modifier_policy, _records_by_tag, _record_target
from .knowledge_relationships import (
    MODIFIER_VALUE,
    MODIFIER_VALUE_STATE,
    NOT_SET_MODIFIER_VALUE,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_store import RelationshipStore, validate_relationships
from .relationship_workspace import (
    RelationshipWorkspaceService,
    relationship_fingerprint,
)
from .scoped_constraint_projection import (
    publish_scoped_constraint_storage_authority,
)
from .tag_collection import normalize_tag
from .tag_database import TagDatabase


DEFAULT_MODIFIER_COMPOSITION_COMPILER = "reviewed_modifier_compositions_v1"
DEFAULT_MODIFIER_COMPOSITION_SOURCE = (
    "CollectiGen reviewed shipped modifier compositions"
)
DIRECT_MODIFIER_COMPOSITION_REVIEW_SOURCE = "reviewed_default_promotion"
REVIEWED_MODIFIER_COMPOSITION_AUTHORITY = (
    "reviewed_modifier_composition_relationships_v1"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_fingerprint(path: str | Path) -> str:
    target = Path(path)
    return sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".restore.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def modifier_composition_relationship_id(tag_id: str, family_id: str) -> str:
    payload = f"{tag_id}\x1f{family_id}".encode("utf-8")
    return f"modifier_value.{sha256(payload).hexdigest()[:20]}"


def build_default_modifier_composition_relationships(
    tags: TagDatabase,
    categories: Any,
    foundation: Any,
    *,
    modifier_roles: Mapping[str, Any] | None = None,
) -> tuple[KnowledgeRelationship, ...]:
    """Compile directly reviewed shipped modifier slots as default authority.

    Inline ``attributes`` remain unchanged as old-reader projections. A slot is
    compiled only when its exact value, family, Base Tag, normalized composed
    name, source, and review time are recorded in field-level provenance. A
    unique legacy literal may remain a ``modifier_value`` endpoint; an exact
    stable Tag ID uses a ``tag`` endpoint only when reviewed family authority
    resolves it. Ambiguous, unsupported, or duplicate-family values remain
    compatibility findings instead of becoming guessed canonical knowledge.
    """

    relationships: list[KnowledgeRelationship] = []
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        if not record.attributes:
            continue
        base_id = str(tags.effective_parent_id(record) or "").strip()
        base = tags.tags.get(base_id)
        provenance = record.relation_extras.get("modifier_value_provenance", {})
        if base is None or not isinstance(provenance, Mapping):
            continue

        candidates: dict[
            str, list[tuple[str, RelationshipEndpoint, Mapping[str, Any]]]
        ] = {}
        for raw_value in record.attributes:
            value_id = str(raw_value).strip()
            if not value_id:
                continue
            value_record = tags.tags.get(value_id)
            if value_record is not None:
                modifier = foundation.modifier_for_tag(
                    value_record, categories, modifier_roles,
                )
                if modifier is None:
                    continue
                endpoint = RelationshipEndpoint(
                    "tag", value_record.id, value_record.display,
                )
            else:
                matches = {
                    (str(item.id), str(item.group)): item
                    for item in foundation.modifiers_for(value_id)
                }
                if len(matches) != 1:
                    continue
                modifier = next(iter(matches.values()))
                endpoint = RelationshipEndpoint(
                    "modifier_value", value_id, str(modifier.display),
                )
            family_id = foundation.canonical_modifier_group(
                str(modifier.group), categories,
            )
            evidence = provenance.get(family_id, {})
            if not family_id or not isinstance(evidence, Mapping):
                continue
            candidates.setdefault(family_id, []).append(
                (value_id, endpoint, evidence)
            )

        for family_id in foundation.ordered_modifier_groups(
            candidates, categories,
        ):
            family_candidates = candidates.get(family_id, ())
            if len(family_candidates) != 1:
                continue
            value_id, endpoint, evidence = family_candidates[0]
            reviewed_at = str(evidence.get("reviewed_at") or "").strip()
            if (
                str(evidence.get("source") or "").strip()
                != DIRECT_MODIFIER_COMPOSITION_REVIEW_SOURCE
                or not reviewed_at
                or str(evidence.get("value_id") or "").strip() != value_id
                or str(evidence.get("base_tag_id") or "").strip() != base.id
                or normalize_tag(str(evidence.get("tag_name") or ""))
                != normalize_tag(record.display)
            ):
                continue
            relationships.append(
                KnowledgeRelationship(
                    id=modifier_composition_relationship_id(
                        record.id, family_id,
                    ),
                    kind=MODIFIER_VALUE,
                    endpoints=(
                        RelationshipEndpoint("tag", record.id, record.display),
                        endpoint,
                    ),
                    directed=True,
                    scope="any",
                    label=f"{record.display} modifier composition",
                    reviewed=True,
                    source=DEFAULT_MODIFIER_COMPOSITION_SOURCE,
                    details=(
                        ("modifier_dimension_id", family_id),
                        ("operation", "reviewed_shipped_default"),
                        ("base_tag_id", base.id),
                        ("tag_name", normalize_tag(record.display)),
                        (
                            "review_source",
                            DIRECT_MODIFIER_COMPOSITION_REVIEW_SOURCE,
                        ),
                        ("reviewed_at", reviewed_at),
                        (
                            "compatibility_projection",
                            "tag.relations.attributes",
                        ),
                        (
                            "default_compiler",
                            DEFAULT_MODIFIER_COMPOSITION_COMPILER,
                        ),
                    ),
                    layer="default",
                )
            )
    return tuple(sorted(relationships, key=lambda item: item.id))


@dataclass(frozen=True)
class ModifierCompositionResolution:
    tag_id: str
    family_id: str
    value_id: str
    relationship_id: str
    operation: str
    suggested_value_id: str
    base_tag_id: str
    tag_name: str
    reviewed_at: str
    record_fingerprint: str
    layer: str
    source: str
    disabled: bool = False

    @property
    def is_set(self) -> bool:
        return bool(self.value_id) and not self.disabled


@dataclass(frozen=True)
class EffectiveModifierComposition:
    tag_id: str
    values: tuple[str, ...]
    canonical: tuple[ModifierCompositionResolution, ...]
    compatibility_values: tuple[str, ...]

    def by_family(self) -> dict[str, ModifierCompositionResolution]:
        return {item.family_id: item for item in self.canonical}

    def fingerprints(self) -> dict[str, str]:
        return {
            item.family_id: item.record_fingerprint
            for item in self.canonical
        }


@dataclass(frozen=True)
class ModifierCompositionResult:
    tag_id: str
    relationship_ids: tuple[str, ...]
    values: tuple[str, ...]
    manifest_path: Path


def _family_for_relationship(
    relationship: KnowledgeRelationship,
    *,
    tags: Any = None,
    categories: Any = None,
    foundation: Any = None,
    modifier_roles: Mapping[str, Any] | None = None,
) -> str:
    family_id = str(dict(relationship.details).get("modifier_dimension_id", ""))
    if family_id:
        return (
            foundation.canonical_modifier_group(family_id, categories)
            if foundation is not None else family_id
        )
    if len(relationship.endpoints) != 2 or foundation is None or tags is None:
        return ""
    target = relationship.endpoints[1]
    record = getattr(tags, "tags", {}).get(str(target.id))
    modifier = foundation.modifier_for_tag(
        record, categories, modifier_roles,
    ) if record is not None else None
    return str(getattr(modifier, "group", "") or "")


def _legacy_policy_index(relationships, *, tags, categories, foundation):
    result = {}
    if tags is None or foundation is None:
        return result
    for base_id, records in _records_by_tag(relationships).items():
        user_records = tuple(row for row in records if row.layer == "user")
        selected = user_records or records  # Native direct user policy owns the entire policy.
        if any(not row.disabled and _record_target(row)[0] == "none" for row in selected):
            continue
        for row in selected:
            if row.disabled:
                continue
            policy = inspect_legacy_finite_modifier_policy(row, foundation=foundation,
                categories=categories, resolve_tag=tags.resolve_tag_id)
            if policy is not None:
                result.setdefault((tags.resolve_tag_id(base_id), policy.dimension_id), []).append(policy)
    return result


def _require_legacy_selection(family_id, base_id, value_id, policies, tags):
    if tags is None or not base_id or tags.resolve_tag_id(base_id) is None:
        raise ValueError("Legacy modifier composition has an unavailable exact base Tag.")
    matches = policies.get((tags.resolve_tag_id(base_id), family_id), ())
    if len(matches) != 1 or not matches[0].supported:
        reason = matches[0].reason if len(matches) == 1 else "missing or ambiguous finite policy"
        raise ValueError("Legacy modifier composition is unsupported: " + reason)
    if value_id and (tags.resolve_tag_id(value_id) is None or
            tags.resolve_tag_id(value_id) not in {target for _, target in matches[0].selected_targets}):
        raise ValueError("Legacy modifier value is outside its exact reviewed finite policy.")
    return matches[0]


def modifier_composition_resolutions(
    relationships: Iterable[KnowledgeRelationship],
    *,
    tags: Any = None,
    categories: Any = None,
    foundation: Any = None,
    modifier_roles: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, ModifierCompositionResolution]]:
    """Resolve canonical per-family composition without reading inline attributes."""
    categories = reusable_modifier_family_snapshot(categories)
    relationships = tuple(relationships)
    legacy_policies = _legacy_policy_index(relationships, tags=tags, categories=categories, foundation=foundation)
    result: dict[str, dict[str, ModifierCompositionResolution]] = {}
    for relationship in sorted(relationships, key=lambda item: item.id):
        if relationship.kind != MODIFIER_VALUE:
            continue
        if relationship.disabled and isinstance(
            relationship.extras.get("tag_removal"), Mapping
        ):
            # A removed endpoint is retained only as reversible lifecycle
            # evidence.  It no longer owns a live composition slot.
            continue
        if (
            not relationship.reviewed
            or not relationship.directed
            or len(relationship.endpoints) != 2
            or relationship.endpoints[0].kind != "tag"
        ):
            raise ValueError(
                f"Modifier Value relationship {relationship.id!r} is not one reviewed directed composition slot."
            )
        source, target = relationship.endpoints
        family_id = _family_for_relationship(
            relationship,
            tags=tags,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
        )
        if not family_id:
            # A forward-compatible or old generic relationship remains visible
            # in the workspace but cannot replace a named slot by guess.
            continue
        if target.kind in {"tag", "modifier_value"}:
            value_id = "" if relationship.disabled else str(target.id)
        elif (
            target.kind == MODIFIER_VALUE_STATE
            and target.id == NOT_SET_MODIFIER_VALUE
        ):
            value_id = ""
        else:
            raise ValueError(
                f"Modifier Value relationship {relationship.id!r} has an unsupported value endpoint."
            )
        if family_id in {"modifier_size", "modifier_state"} and family_id not in getattr(categories, "categories", {}):
            if tags is None or tags.resolve_tag_id(source.id) is None:
                raise ValueError("Legacy composed Tag is unavailable.")
            _require_legacy_selection(family_id, dict(relationship.details).get("base_tag_id", ""),
                value_id, legacy_policies, tags)
        slots = result.setdefault(str(source.id), {})
        if family_id in slots:
            raise ValueError(
                f"Tag {source.id!r} has more than one Modifier Value record for family {family_id!r}."
            )
        details = dict(relationship.details)
        slots[family_id] = ModifierCompositionResolution(
            tag_id=str(source.id),
            family_id=family_id,
            value_id=value_id,
            relationship_id=relationship.id,
            operation=details.get("operation", ""),
            suggested_value_id=details.get("suggested_value_id", ""),
            base_tag_id=details.get("base_tag_id", ""),
            tag_name=details.get("tag_name", ""),
            reviewed_at=details.get("reviewed_at", ""),
            record_fingerprint=relationship_fingerprint(relationship),
            layer=relationship.layer,
            source=relationship.source,
            disabled=relationship.disabled,
        )
    return result


def load_reviewed_default_modifier_composition_relationships(
    tags: TagDatabase,
    categories: Any,
    foundation: Any,
    relationships: Iterable[KnowledgeRelationship],
    *,
    modifier_roles: Mapping[str, Any] | None = None,
) -> tuple[KnowledgeRelationship, ...]:
    """Validate retained shipped Modifier Value authority.

    Reviewed canonical relationships are the compiler input. Any remaining
    nonempty shipped ``attributes`` projection must agree with one active
    canonical family slot before a schema-v3 layer may retire that field.
    Semantic-audit relationships can represent a former literal with an exact
    stable Tag endpoint, so comparison uses the reviewed value label as well as
    its canonical family instead of requiring the old literal to be an ID.
    """

    all_relationships = tuple(relationships)
    by_id = {relationship.id: relationship for relationship in all_relationships}
    resolutions = modifier_composition_resolutions(
        all_relationships,
        tags=tags,
        categories=categories,
        foundation=foundation,
        modifier_roles=modifier_roles,
    )
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        if not record.attributes:
            continue
        slots = resolutions.get(record.id, {})
        represented_families: set[str] = set()
        for raw_value in record.attributes:
            value_id = str(raw_value).strip()
            value_record = tags.tags.get(value_id)
            if value_record is not None:
                modifier = foundation.modifier_for_tag(
                    value_record, categories, modifier_roles,
                )
                expected_label = value_record.display
            else:
                matches = {
                    (str(item.id), str(item.group)): item
                    for item in foundation.modifiers_for(value_id)
                }
                if len(matches) != 1:
                    raise ValueError(
                        f"Shipped Tag {record.id!r} Modifier Value projection "
                        f"{value_id!r} has no unique reviewed family."
                    )
                modifier = next(iter(matches.values()))
                expected_label = str(modifier.display)
            family_id = foundation.canonical_modifier_group(
                str(getattr(modifier, "group", "") or ""), categories,
            )
            resolution = slots.get(family_id)
            relationship = (
                by_id.get(resolution.relationship_id)
                if resolution is not None else None
            )
            target = (
                relationship.target_endpoints[0]
                if relationship is not None and len(relationship.target_endpoints) == 1
                else None
            )
            target_record = (
                tags.tags.get(target.id)
                if target is not None and target.kind == "tag" else None
            )
            target_label = str(
                getattr(target_record, "display", "")
                or getattr(target, "label", "")
                or getattr(target, "id", "")
            )
            if (
                not family_id
                or family_id in represented_families
                or resolution is None
                or resolution.disabled
                or relationship is None
                or not relationship.reviewed
                or relationship.layer != "default"
                or target is None
                or normalize_tag(target_label) != normalize_tag(expected_label)
            ):
                raise ValueError(
                    f"Shipped Tag {record.id!r} Modifier Value projection "
                    f"{value_id!r} lacks matching active canonical authority."
                )
            represented_families.add(family_id)

    normalized: list[KnowledgeRelationship] = []
    represented: set[tuple[str, str]] = set()
    for relationship in sorted(all_relationships, key=lambda item: item.id):
        details = dict(relationship.details)
        marker = details.get("default_compiler", "")
        authority = details.get("reviewed_authority", "")
        projection = details.get("compatibility_projection", "")
        managed = (
            marker == DEFAULT_MODIFIER_COMPOSITION_COMPILER
            or authority == REVIEWED_MODIFIER_COMPOSITION_AUTHORITY
            or (
                relationship.source == DEFAULT_MODIFIER_COMPOSITION_SOURCE
                and projection == "tag.relations.attributes"
            )
        )
        if not managed:
            continue
        source = relationship.source_endpoint
        source_record = (
            tags.tags.get(source.id)
            if source is not None and source.kind == "tag" else None
        )
        target = (
            relationship.target_endpoints[0]
            if len(relationship.target_endpoints) == 1 else None
        )
        family_id = _family_for_relationship(
            relationship,
            tags=tags,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
        )
        base_id = (
            str(tags.effective_parent_id(source_record) or "")
            if source_record is not None else ""
        )
        if (
            relationship.kind != MODIFIER_VALUE
            or not relationship.directed
            or not relationship.reviewed
            or relationship.disabled
            or relationship.layer != "default"
            or relationship.scope != "any"
            or source_record is None
            or target is None
            or target.kind not in {"tag", "modifier_value"}
            or not family_id
            or not base_id
            or str(details.get("base_tag_id") or "") != base_id
            or normalize_tag(str(details.get("tag_name") or ""))
            != normalize_tag(source_record.display)
            or projection != "tag.relations.attributes"
        ):
            raise ValueError(
                f"Shipped Modifier Value relationship {relationship.id!r} is "
                "not one valid reviewed canonical authority record."
            )
        expected_id = modifier_composition_relationship_id(
            source_record.id, family_id,
        )
        if relationship.id != expected_id:
            raise ValueError(
                f"Shipped Modifier Value relationship for {source_record.id!r} "
                f"has unstable ID {relationship.id!r}."
            )
        key = (source_record.id, family_id)
        if key in represented:
            raise ValueError(
                f"Shipped Tag {source_record.id!r} has duplicate retained "
                f"Modifier Value authority for {family_id!r}."
            )
        represented.add(key)
        if marker and marker != DEFAULT_MODIFIER_COMPOSITION_COMPILER:
            raise ValueError(
                f"Shipped Modifier Value relationship {relationship.id!r} has "
                f"unknown legacy compiler marker {marker!r}."
            )
        if authority and authority != REVIEWED_MODIFIER_COMPOSITION_AUTHORITY:
            raise ValueError(
                f"Shipped Modifier Value relationship {relationship.id!r} has "
                f"unknown reviewed authority marker {authority!r}."
            )
        details.pop("default_compiler", None)
        details["reviewed_authority"] = REVIEWED_MODIFIER_COMPOSITION_AUTHORITY
        target_record = tags.tags.get(target.id) if target.kind == "tag" else None
        normalized.append(replace(
            relationship,
            endpoints=(
                RelationshipEndpoint("tag", source_record.id, source_record.display),
                replace(
                    target,
                    label=str(
                        getattr(target_record, "display", "")
                        or target.label or target.id
                    ),
                ),
            ),
            label=f"{source_record.display} modifier composition",
            source=DEFAULT_MODIFIER_COMPOSITION_SOURCE,
            details=tuple((str(key), str(value)) for key, value in details.items()),
        ))
    return tuple(normalized)


def effective_modifier_composition(
    record: Any,
    relationships: Iterable[KnowledgeRelationship],
    *,
    tags: Any,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
    resolutions: Mapping[str, Mapping[str, ModifierCompositionResolution]] | None = None,
) -> EffectiveModifierComposition:
    resolved_by_tag = (
        resolutions
        if resolutions is not None
        else modifier_composition_resolutions(
            relationships,
            tags=tags,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
        )
    )
    tag_resolutions = resolved_by_tag.get(str(record.id), {})
    canonical_value_ids = {
        item.value_id for item in tag_resolutions.values() if item.is_set
    }
    compatibility: list[str] = []
    for raw_value in getattr(record, "attributes", ()):
        value_id = str(raw_value)
        if value_id in canonical_value_ids:
            continue
        value_record = getattr(tags, "tags", {}).get(value_id)
        if value_record is None and hasattr(tags, "find"):
            value_record = tags.find(value_id)
        modifier = foundation.modifier_for_tag(
            value_record, categories, modifier_roles,
        ) if value_record is not None else None
        family_id = str(getattr(modifier, "group", "") or "")
        if family_id and family_id in tag_resolutions:
            continue
        compatibility.append(value_id)
    ordered_families = foundation.ordered_modifier_groups(
        tag_resolutions, categories,
    )
    canonical = tuple(
        tag_resolutions[family_id]
        for family_id in ordered_families
        if family_id in tag_resolutions
    )
    canonical_values = [item.value_id for item in canonical if item.is_set]
    values = tuple(dict.fromkeys((*canonical_values, *compatibility)))
    return EffectiveModifierComposition(
        tag_id=str(record.id),
        values=values,
        canonical=canonical,
        compatibility_values=tuple(compatibility),
    )


def publish_modifier_composition_storage_authority(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
    *,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, ModifierCompositionResolution]]:
    """Publish canonical slots and unresolved compatibility values for saves."""

    categories = reusable_modifier_family_snapshot(categories)
    all_relationships = tuple(relationships)
    resolutions = modifier_composition_resolutions(
        all_relationships,
        tags=tags,
        categories=categories,
        foundation=foundation,
        modifier_roles=modifier_roles,
    )
    compatibility_values: dict[str, tuple[str, ...]] = {}
    represented_families: dict[str, tuple[str, ...]] = {}
    for tag_id, slots in resolutions.items():
        represented_families[tag_id] = tuple(slots)
        record = tags.tags.get(tag_id)
        compatibility_values[tag_id] = (
            effective_modifier_composition(
                record,
                all_relationships,
                tags=tags,
                categories=categories,
                foundation=foundation,
                modifier_roles=modifier_roles,
                resolutions=resolutions,
            ).compatibility_values
            if record is not None else ()
        )
    tags.configure_modifier_composition_authority(
        compatibility_values, represented_families,
    )
    return resolutions


def modifier_composition_compatibility_payload(
    record: Any,
    relationships: Iterable[KnowledgeRelationship],
    *,
    tags: TagDatabase,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
    resolutions: Mapping[
        str, Mapping[str, ModifierCompositionResolution]
    ] | None = None,
) -> dict[str, Any]:
    """Serialize the explicit old-reader Modifier Value projection."""

    projected = copy.deepcopy(record)
    all_relationships = tuple(relationships)
    resolved_by_tag = (
        resolutions
        if resolutions is not None
        else modifier_composition_resolutions(
            all_relationships,
            tags=tags,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
        )
    )
    if str(projected.id) in resolved_by_tag:
        projected.attributes = list(effective_modifier_composition(
            projected,
            all_relationships,
            tags=tags,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
            resolutions=resolved_by_tag,
        ).values)
    return projected.to_dict(include_attributes_projection=True)


class ModifierCompositionService:
    """Canonical modifier-slot authority with an atomic old-reader projection."""

    def __init__(
        self,
        tags: TagDatabase,
        categories: Any,
        foundation: Any,
        *,
        modifier_roles: Mapping[str, Any] | None = None,
        default_path: str | Path | None = None,
        user_path: str | Path | None = None,
        workspace: RelationshipWorkspaceService | None = None,
        publish_alias_authority: bool = True,
    ) -> None:
        self.tags = tags
        self.rule_pair_admission=getattr(tags,"rule_pair_admission",None)
        if self.rule_pair_admission is not None and not self.rule_pair_admission.owns(tags.path,"knowledge"):
            raise ValueError("Base & Composition must use its exact Knowledge participant.")
        if workspace is None:
            if default_path is None or user_path is None:
                raise ValueError("Modifier composition needs both relationship store paths.")
            workspace = RelationshipWorkspaceService(
                default_path, user_path, tags=tags,
            )
        self.categories = categories
        self.foundation = foundation
        self.modifier_roles = dict(modifier_roles or {})
        self.workspace = workspace
        if hasattr(self.categories, "configure_category_base_authority"):
            self.categories.configure_category_base_authority(
                self.workspace.effective,
            )
        self._resolutions_cache: dict[
            str, dict[str, ModifierCompositionResolution]
        ] | None = None
        paths = [
            self.workspace.default_store.path,
            getattr(categories, "default_path", None),
            getattr(categories, "user_path", None),
            getattr(foundation, "modifiers_path", None),
        ]
        self._authority_fingerprints = tuple(
            (str(Path(path).resolve()), _file_fingerprint(path))
            for path in paths if path is not None
        )
        if publish_alias_authority:
            from .tag_alias_relationships import publish_tag_alias_storage_authority
            publish_tag_alias_storage_authority(self.tags,self.workspace.effective)

    def _admission(self):
        binding=self.rule_pair_admission
        if getattr(self.tags,"rule_pair_admission",None)!=binding or (
            binding is not None and (
                not binding.owns(self.tags.path,"knowledge")
                or self.workspace.user_store.path.absolute()!=binding.root/"relationships.json"
            )
        ):
            raise RuleConversionAdmissionError("Saved Knowledge changed. Reopen Base & Composition before saving.")
        return binding.admit() if binding else nullcontext()

    def resolutions(self) -> dict[str, dict[str, ModifierCompositionResolution]]:
        if self._resolutions_cache is None:
            self._resolutions_cache = publish_modifier_composition_storage_authority(
                self.tags,
                self.workspace.effective,
                categories=self.categories,
                foundation=self.foundation,
                modifier_roles=self.modifier_roles,
            )
        return self._resolutions_cache

    def effective_for_tag(self, tag_id: str) -> EffectiveModifierComposition:
        record = self.tags.tags.get(str(tag_id))
        if record is None:
            raise ValueError("The composed SDXL Tag is no longer available.")
        return effective_modifier_composition(
            record,
            self.workspace.effective,
            tags=self.tags,
            categories=self.categories,
            foundation=self.foundation,
            modifier_roles=self.modifier_roles,
            resolutions=self.resolutions(),
        )

    def _verify_authority(self) -> None:
        for path, expected in self._authority_fingerprints:
            if _file_fingerprint(path) != expected:
                raise RuntimeError(
                    "Modifier relationship authority changed after this Tag was opened. Reload and review it again."
                )
        relationship_path = self.workspace.user_store.path
        current = relationship_path.read_bytes() if relationship_path.exists() else None
        if current != self.workspace.user_store._loaded_bytes:
            raise RuntimeError(
                "Relationships changed after this Tag was opened. Reload and review it again."
            )
        tag_path = self.tags.path
        current_tags = tag_path.read_bytes() if tag_path.exists() else None
        if current_tags != getattr(self.tags, "_loaded_bytes", None):
            raise RuntimeError(
                "System Knowledge changed after this Tag was opened. Reload and review it again."
            )

    def _family_for_value(self, value_id: str) -> str:
        record = self.tags.tags.get(str(value_id))
        modifier = self.foundation.modifier_for_tag(
            record, self.categories, self.modifier_roles,
        ) if record is not None else None
        return str(getattr(modifier, "group", "") or "")

    def apply(
        self,
        tag_id: str,
        desired_values: Iterable[str],
        changes: Iterable[Mapping[str, Any]],
        *,
        base_tag_id: str = "",
        tag_name: str = "",
        expected_fingerprints: Mapping[str, str] | None = None,
        backup_root: str | Path | None = None,
    ) -> ModifierCompositionResult:
        with self._admission():
            return self._apply_admitted(tag_id,desired_values,changes,
                base_tag_id=base_tag_id,tag_name=tag_name,
                expected_fingerprints=expected_fingerprints,backup_root=backup_root)

    def _apply_admitted(
        self,tag_id: str,desired_values: Iterable[str],changes: Iterable[Mapping[str, Any]], *,
        base_tag_id: str="",tag_name: str="",
        expected_fingerprints: Mapping[str,str] | None=None,
        backup_root: str | Path | None=None,
    ) -> ModifierCompositionResult:
        self._verify_authority()
        stable_id = str(tag_id)
        record = self.tags.tags.get(stable_id)
        if record is None:
            raise ValueError("The composed SDXL Tag is no longer available.")
        changes_by_family = {
            str(change.get("group") or ""): dict(change)
            for change in changes
            if str(change.get("group") or "")
        }
        if not changes_by_family:
            raise ValueError("No reviewed modifier-slot change was provided.")

        current_resolutions = self.resolutions().get(stable_id, {})
        effective_by_id = {item.id: item for item in self.workspace.effective}
        user_values = dict(self.workspace.user_store.relationships)
        changed_ids: list[str] = []
        reviewed_at = _utc_now()
        for family_id in self.foundation.ordered_modifier_groups(
            changes_by_family, self.categories,
        ):
            change = changes_by_family[family_id]
            current_resolution = current_resolutions.get(family_id)
            current = (
                effective_by_id.get(current_resolution.relationship_id)
                if current_resolution is not None else None
            )
            if expected_fingerprints is not None:
                expected = str(expected_fingerprints.get(family_id, "missing"))
                actual = (
                    current_resolution.record_fingerprint
                    if current_resolution is not None else "missing"
                )
                if actual != expected:
                    raise RuntimeError(
                        "This modifier slot changed after it was opened. Reload and review it again."
                    )
            relationship_id = (
                current_resolution.relationship_id
                if current_resolution is not None
                else modifier_composition_relationship_id(stable_id, family_id)
            )
            reserved = effective_by_id.get(relationship_id)
            if reserved is not None and reserved.kind != MODIFIER_VALUE:
                raise ValueError(
                    "The reserved Modifier Value relationship ID is already used by another relationship."
                )
            if current is None:
                current = reserved

            value_id = str(change.get("value_id") or "")
            if value_id:
                if value_id not in self.tags.tags:
                    raise ValueError("Choose an available reviewed modifier Tag.")
                resolved_family = self._family_for_value(value_id)
                if family_id in {"modifier_size", "modifier_state"} and family_id not in getattr(self.categories, "categories", {}):
                    _require_legacy_selection(family_id,
                        base_tag_id or (current_resolution.base_tag_id if current_resolution else ""), value_id,
                        _legacy_policy_index(self.workspace.effective, tags=self.tags,
                            categories=self.categories, foundation=self.foundation), self.tags)
                elif resolved_family != family_id:
                    raise ValueError(
                        "The selected modifier value no longer belongs to this reviewed family."
                    )
                value_endpoint = RelationshipEndpoint(
                    "tag", value_id, self.tags.tags[value_id].display,
                )
            else:
                if family_id in {"modifier_size", "modifier_state"} and family_id not in getattr(self.categories, "categories", {}):
                    _require_legacy_selection(family_id,
                        base_tag_id or (current_resolution.base_tag_id if current_resolution else ""), "",
                        _legacy_policy_index(self.workspace.effective, tags=self.tags,
                            categories=self.categories, foundation=self.foundation), self.tags)
                value_endpoint = RelationshipEndpoint(
                    MODIFIER_VALUE_STATE,
                    NOT_SET_MODIFIER_VALUE,
                    "Not set",
                )

            details = dict(current.details) if current is not None else {}
            retained_base = (base_tag_id or (current_resolution.base_tag_id if current_resolution else "")) if family_id in {"modifier_size", "modifier_state"} else base_tag_id
            details.update(
                {
                    "modifier_dimension_id": family_id,
                    "operation": str(change.get("source") or "manual_choice"),
                    "suggested_value_id": str(change.get("suggested_value_id") or ""),
                    "base_tag_id": str(retained_base or ""),
                    "tag_name": normalize_tag(tag_name or record.display),
                    "reviewed_at": reviewed_at,
                    "compatibility_projection": "tag.relations.attributes",
                }
            )
            extras = copy.deepcopy(current.extras) if current is not None else {}
            history = [
                dict(item)
                for item in extras.get("modifier_composition_history", ())
                if isinstance(item, Mapping)
            ]
            if current_resolution is not None:
                history.append(
                    {
                        "changed_at": reviewed_at,
                        "family_id": family_id,
                        "value_id": current_resolution.value_id,
                        "operation": current_resolution.operation,
                        "relationship_fingerprint": current_resolution.record_fingerprint,
                        "reviewed_at": current_resolution.reviewed_at,
                        "layer": current_resolution.layer,
                        "source": current_resolution.source,
                    }
                )
            if history:
                extras["modifier_composition_history"] = history
            extras["modifier_composition_review"] = {
                "previous_value_ids": [
                    str(value)
                    for value in change.get("previous_value_ids", ())
                    if str(value)
                ],
                "suggested_value_id": str(change.get("suggested_value_id") or ""),
                "base_tag_id": str(base_tag_id or ""),
                "tag_name": normalize_tag(tag_name or record.display),
            }
            relationship = KnowledgeRelationship(
                id=relationship_id,
                kind=MODIFIER_VALUE,
                endpoints=(
                    RelationshipEndpoint("tag", stable_id, record.display),
                    value_endpoint,
                ),
                directed=True,
                scope="any",
                label=f"{record.display} modifier composition",
                reviewed=True,
                source="user_review",
                details=tuple((str(key), str(value)) for key, value in details.items()),
                version=(current.version + 1) if current is not None else 1,
                layer="user",
                disabled=False,
                extras=extras,
            )
            user_values[relationship.id] = relationship
            changed_ids.append(relationship.id)

        effective = dict(self.workspace.default_store.relationships)
        effective.update(user_values)
        issues = validate_relationships(effective.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))

        original_attributes = list(record.attributes)
        record.attributes = list(dict.fromkeys(
            str(value) for value in desired_values if str(value)
        ))
        projected = effective_modifier_composition(
            record,
            effective.values(),
            tags=self.tags,
            categories=self.categories,
            foundation=self.foundation,
            modifier_roles=self.modifier_roles,
        )
        record.attributes = list(projected.values)
        self.tags._rebuild_lookup()
        publish_modifier_composition_storage_authority(
            self.tags,
            effective.values(),
            categories=self.categories,
            foundation=self.foundation,
            modifier_roles=self.modifier_roles,
        )
        publish_scoped_constraint_storage_authority(
            self.tags,effective.values(),
        )

        original_user_values = dict(self.workspace.user_store.relationships)
        try:
            relationship_path = self.workspace.user_store.path
            tag_path = self.tags.path
            relationship_bytes = (
                relationship_path.read_bytes() if relationship_path.exists() else None
            )
            tag_bytes = tag_path.read_bytes() if tag_path.exists() else None
            self.workspace.user_store.replace(user_values.values())
            relationship_payload = self.workspace.user_store.payload()
            self.workspace.user_store.replace(original_user_values.values())
            tag_payload = self.tags._payload()

            root = (
                Path(backup_root)
                if backup_root is not None
                else tag_path.parent / "backups" / "relationship-modifier-composition"
            )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = root / f"{stamp}_{uuid4().hex[:8]}"
            backup.mkdir(parents=True, exist_ok=False)
            entries = []
            for index, (path, payload) in enumerate(
                ((relationship_path, relationship_bytes), (tag_path, tag_bytes))
            ):
                backup_name = f"{index}_{path.name}"
                if payload is not None:
                    (backup / backup_name).write_bytes(payload)
                entries.append(
                    {
                        "target": str(path.resolve()),
                        "backup": backup_name,
                        "existed": payload is not None,
                    }
                )
            manifest_path = backup / "recovery.json"
            manifest = {
                "schema_version": 1,
                "status": "pending",
                "created_at": reviewed_at,
                "kind": "modifier_composition",
                "tag_id": stable_id,
                "relationship_ids": changed_ids,
                "files": entries,
            }
            _atomic_json(manifest_path, manifest)
        except BaseException:
            self.workspace.user_store.replace(original_user_values.values())
            record.attributes = original_attributes
            self.tags._rebuild_lookup()
            original_effective = dict(self.workspace.default_store.relationships)
            original_effective.update(original_user_values)
            publish_modifier_composition_storage_authority(
                self.tags,
                original_effective.values(),
                categories=self.categories,
                foundation=self.foundation,
                modifier_roles=self.modifier_roles,
            )
            publish_scoped_constraint_storage_authority(
                self.tags,original_effective.values(),
            )
            raise
        try:
            _atomic_json(relationship_path, relationship_payload)
            _atomic_json(tag_path, tag_payload)
            saved_relationships = RelationshipStore(relationship_path)
            saved_tags = TagDatabase(tag_path,rule_pair_admission=self.rule_pair_admission)
            if saved_relationships.payload() != relationship_payload:
                raise RuntimeError(
                    "Modifier relationship save failed semantic read-back verification."
                )
            if saved_tags._payload() != tag_payload:
                raise RuntimeError(
                    "Modifier Tag storage failed semantic read-back verification."
                )
            if saved_tags.tags[stable_id].attributes != list(
                projected.compatibility_values
            ):
                raise RuntimeError(
                    "Represented Modifier Values remained in mutable Tag storage."
                )
            verified_workspace = RelationshipWorkspaceService(
                self.workspace.default_store.path,
                relationship_path,
                tags=saved_tags,
            )
            verified = modifier_composition_resolutions(
                verified_workspace.effective,
                tags=saved_tags,
                categories=self.categories,
                foundation=self.foundation,
                modifier_roles=self.modifier_roles,
            ).get(stable_id, {})
            for family_id, change in changes_by_family.items():
                resolution = verified.get(family_id)
                if resolution is None or resolution.value_id != str(change.get("value_id") or ""):
                    raise RuntimeError(
                        "Modifier composition failed semantic read-back verification."
                    )
        except BaseException:
            for entry in entries:
                target = Path(entry["target"])
                saved = backup / entry["backup"]
                if entry["existed"]:
                    _atomic_bytes(target, saved.read_bytes())
                elif target.exists():
                    target.unlink()
            self.workspace.default_store.load()
            self.workspace.user_store.load()
            self.tags.load()
            publish_modifier_composition_storage_authority(
                self.tags,
                self.workspace.effective,
                categories=self.categories,
                foundation=self.foundation,
                modifier_roles=self.modifier_roles,
            )
            publish_scoped_constraint_storage_authority(
                self.tags,self.workspace.effective,
            )
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            raise
        manifest["status"] = "complete"
        manifest["completed_at"] = _utc_now()
        _atomic_json(manifest_path, manifest)
        self.workspace = verified_workspace
        self._resolutions_cache = None
        self.tags._loaded_bytes = tag_path.read_bytes()
        return ModifierCompositionResult(
            tag_id=stable_id,
            relationship_ids=tuple(changed_ids),
            values=projected.values,
            manifest_path=manifest_path,
        )

    @classmethod
    def recover_interrupted(cls, data_root: str | Path) -> tuple[Path, ...]:
        with RulePairAdmission(Path(data_root).absolute()).admit():
            return cls._recover_interrupted_admitted(data_root)

    @classmethod
    def _recover_interrupted_admitted(cls, data_root: str | Path) -> tuple[Path, ...]:
        root = Path(data_root).resolve()
        backup_root = root / "backups" / "relationship-modifier-composition"
        restored: list[Path] = []
        if not backup_root.exists():
            return ()
        allowed = {"relationships.json", "tags.json"}
        for manifest_path in sorted(backup_root.glob("*/recovery.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "pending":
                continue
            entries = []
            seen_targets: set[Path] = set()
            for raw_entry in manifest.get("files", ()):
                entry = dict(raw_entry)
                target = Path(str(entry.get("target") or "")).resolve()
                backup = (
                    manifest_path.parent / str(entry.get("backup") or "")
                ).resolve()
                if target.parent != root or target.name not in allowed:
                    raise RuntimeError(
                        "Modifier composition recovery manifest contains an unsafe target."
                    )
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError(
                        "Modifier composition recovery manifest contains an unsafe backup path."
                    )
                if target in seen_targets:
                    raise RuntimeError(
                        "Modifier composition recovery manifest repeats a target."
                    )
                if entry.get("existed") and not backup.is_file():
                    raise RuntimeError("Modifier composition recovery backup is missing.")
                seen_targets.add(target)
                entries.append((entry, target, backup))
            for entry, target, backup in entries:
                if entry.get("existed"):
                    _atomic_bytes(target, backup.read_bytes())
                elif target.exists():
                    target.unlink()
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            restored.append(manifest_path)
        return tuple(restored)
