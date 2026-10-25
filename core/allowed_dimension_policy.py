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
from .automation_rule_conversion import RulePairAdmission,RuleConversionAdmissionError

from .category_registry import exact_category_anchor_ids
from .knowledge_engine import ModifierPolicyBaseEntry
from .knowledge_relationships import (
    ALLOWED_DIMENSION,
    ALLOWED_DIMENSION_STATE,
    NO_ALLOWED_DIMENSIONS,
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
from .tag_database import TagDatabase
from .tag_semantic_relationships import publish_tag_semantic_authority

VALUE_SCOPE_ALL = "all"
VALUE_SCOPE_ONLY = "only"
VALUE_SCOPE_ALL_EXCEPT = "all_except"
VALUE_SCOPE_UNREVIEWED = "unreviewed"
VALUE_SCOPE_MODES = frozenset((
    VALUE_SCOPE_ALL,VALUE_SCOPE_ONLY,VALUE_SCOPE_ALL_EXCEPT,VALUE_SCOPE_UNREVIEWED,
))
SCOPE_ROLE_KEY = "allowed_scope_role"
SCOPE_ROLE_SELECTION = "selection"
SCOPE_ROLE_EXCEPTION = "exception"
SCOPE_ROLES = frozenset((SCOPE_ROLE_SELECTION,SCOPE_ROLE_EXCEPTION))
DEFAULT_ALLOWED_DIMENSION_COMPILER = "reviewed_allowed_dimensions_v1"
DEFAULT_ALLOWED_DIMENSION_SOURCE = (
    "CollectiGen reviewed shipped Allowed Details policies"
)
DIRECT_ALLOWED_DIMENSION_REVIEW_SOURCE = "qt_knowledge_editor"
REVIEWED_ALLOWED_DIMENSION_AUTHORITY = (
    "reviewed_allowed_dimension_relationships_v1"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_fingerprint(path: str | Path) -> str:
    target = Path(path)
    return sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"


def _payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


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


def allowed_dimension_relationship_id(tag_id: str, family_id: str) -> str:
    payload = f"{tag_id}\x1f{family_id}".encode("utf-8")
    return f"allowed_dimension.{sha256(payload).hexdigest()[:20]}"


def allowed_dimension_none_relationship_id(tag_id: str) -> str:
    payload = f"{tag_id}\x1f{NO_ALLOWED_DIMENSIONS}".encode("utf-8")
    return f"allowed_dimension.none.{sha256(payload).hexdigest()[:20]}"


def build_default_allowed_dimension_relationships(
    tags: TagDatabase,
) -> tuple[KnowledgeRelationship, ...]:
    """Compile directly reviewed shipped Allowed Details authority.

    An exact empty list with direct review provenance is a complete no-details
    policy. A non-empty list saved by the direct policy editor proves family
    membership, but not whether the family permits all values, a selection, or
    all values except a selection; compile that membership with an unreviewed
    value scope so it cannot authorize a composition. Other provenance remains
    compatibility-only because reverse review may merely preserve unrelated
    families. Inline fields remain unchanged as old-reader projections.
    """
    relationships: list[KnowledgeRelationship] = []
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        raw_groups = record.allowed_modifier_groups
        if raw_groups is None:
            continue
        provenance = record.relation_extras.get(
            "allowed_modifier_groups_provenance", {}
        )
        if not isinstance(provenance, Mapping):
            continue
        review_source = str(provenance.get("source") or "").strip()
        reviewed_at = str(provenance.get("reviewed_at") or "").strip()
        if not review_source or not reviewed_at or "migrat" in review_source.lower():
            continue
        source = RelationshipEndpoint("tag", record.id, record.display)
        common_details = (
            ("review_source", review_source),
            ("reviewed_at", reviewed_at),
            (
                "compatibility_projection",
                "tag.relations.allowed_modifier_groups",
            ),
            ("default_compiler", DEFAULT_ALLOWED_DIMENSION_COMPILER),
        )
        if not raw_groups:
            relationships.append(
                KnowledgeRelationship(
                    id=allowed_dimension_none_relationship_id(record.id),
                    kind=ALLOWED_DIMENSION,
                    endpoints=(
                        source,
                        RelationshipEndpoint(
                            ALLOWED_DIMENSION_STATE,
                            NO_ALLOWED_DIMENSIONS,
                            "No allowed details",
                        ),
                    ),
                    directed=True,
                    scope="any",
                    label=f"{record.display} allows no details",
                    reviewed=True,
                    source=DEFAULT_ALLOWED_DIMENSION_SOURCE,
                    details=(("policy_mode", "explicit_none"), *common_details),
                    layer="default",
                )
            )
            continue
        if review_source != DIRECT_ALLOWED_DIMENSION_REVIEW_SOURCE:
            continue
        groups: list[str] = []
        for raw_group in raw_groups:
            family_id = str(raw_group).strip()
            if not family_id:
                raise ValueError(
                    f"Shipped Tag {record.id!r} has an empty Allowed Details family."
                )
            if family_id in groups:
                raise ValueError(
                    f"Shipped Tag {record.id!r} repeats Allowed Details family "
                    f"{family_id!r}."
                )
            groups.append(family_id)
        for policy_order, family_id in enumerate(groups):
            family_label = family_id.removeprefix("modifier_").replace("_", " ").title()
            relationships.append(
                KnowledgeRelationship(
                    id=allowed_dimension_relationship_id(record.id, family_id),
                    kind=ALLOWED_DIMENSION,
                    endpoints=(
                        source,
                        RelationshipEndpoint(
                            "modifier_dimension", family_id, family_label,
                        ),
                    ),
                    directed=True,
                    scope="any",
                    label=f"{record.display} allowed {family_label}",
                    reviewed=True,
                    source=DEFAULT_ALLOWED_DIMENSION_SOURCE,
                    details=(
                        ("policy_mode", "explicit_set"),
                        ("canonical_family_id", family_id),
                        ("policy_order", str(policy_order)),
                        ("value_scope", VALUE_SCOPE_UNREVIEWED),
                        *common_details,
                    ),
                    layer="default",
                )
            )
    return tuple(relationships)


@dataclass(frozen=True)
class AllowedDimensionValueScope:
    family_id: str
    mode: str = VALUE_SCOPE_UNREVIEWED
    value_ids: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()
    exception_value_ids: tuple[str, ...] = ()
    exception_category_ids: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        return self.mode == VALUE_SCOPE_UNREVIEWED

    def allows(
        self,value_id: str,category_path_ids: Iterable[str] = (),
    ) -> bool:
        """Resolve one value with exact Tag, deepest-branch, then broad scope.

        ``value_ids`` and ``category_ids`` follow the scope mode: they allow
        values for ``only`` and exclude them for ``all_except``.  The matching
        exception collections do the inverse.  Exact Tag decisions always win;
        among category decisions the deepest matching branch wins.
        """
        stable_id=str(value_id)
        if self.mode not in {
            VALUE_SCOPE_ALL,VALUE_SCOPE_ONLY,VALUE_SCOPE_ALL_EXCEPT,
        }:
            return False
        selected_allows=self.mode==VALUE_SCOPE_ONLY
        if stable_id in self.exception_value_ids:
            return not selected_allows
        if stable_id in self.value_ids:
            return selected_allows
        branch_decision: bool | None = None
        for category_id in category_path_ids:
            stable_category_id=str(category_id)
            if stable_category_id in self.category_ids:
                branch_decision=selected_allows
            elif stable_category_id in self.exception_category_ids:
                branch_decision=not selected_allows
        if branch_decision is not None:
            return branch_decision
        return self.mode in {VALUE_SCOPE_ALL,VALUE_SCOPE_ALL_EXCEPT}


@dataclass(frozen=True)
class AllowedDimensionPolicyUpdate:
    groups: tuple[str, ...]
    value_scopes: tuple[AllowedDimensionValueScope, ...] = ()


@dataclass(frozen=True)
class AllowedDimensionPolicyResolution:
    tag_id: str
    groups: tuple[str, ...] = ()
    source_kind: str = "none"
    source_tag_id: str = ""
    source_label: str = ""
    authority: str = "none"
    layer: str = ""
    relationship_ids: tuple[str, ...] = ()
    record_fingerprint: str = ""
    provenance_source: str = ""
    reviewed_at: str = ""
    blocked_reason: str = ""
    value_scopes: tuple[AllowedDimensionValueScope, ...] = ()

    @property
    def is_explicit(self) -> bool:
        return self.source_kind == "explicit"

    @property
    def is_resolved(self) -> bool:
        return self.source_kind != "none" and not self.blocked_reason

    def scope_for(self,family_id: str) -> AllowedDimensionValueScope:
        wanted=str(family_id)
        return next((item for item in self.value_scopes if item.family_id==wanted),AllowedDimensionValueScope(wanted))

    def allows_value(
        self,family_id: str,value_id: str,category_path_ids: Iterable[str] = (),
    ) -> bool:
        return not self.blocked_reason and self.scope_for(family_id).allows(value_id,category_path_ids)


@dataclass(frozen=True)
class AllowedDimensionPolicyAuditEntry:
    """One direct policy-owner/family decision for local review.

    Inherited consumers are deliberately omitted: editing the direct owner is
    the only action that can change the policy, and repeating every dependent
    Tag would turn a policy audit into an unbounded derived inventory.
    """

    tag_id: str
    display: str
    category_id: str
    family_id: str
    family_label: str
    scope_mode: str
    selected_value_ids: tuple[str, ...] = ()
    selected_category_ids: tuple[str, ...] = ()
    exception_value_ids: tuple[str, ...] = ()
    exception_category_ids: tuple[str, ...] = ()
    source_kind: str = ""
    source_label: str = ""
    authority: str = ""
    layer: str = ""
    relationship_ids: tuple[str, ...] = ()
    provenance_source: str = ""
    reviewed_at: str = ""
    blocked_reason: str = ""

    @property
    def stable_id(self) -> str:
        return f"{self.tag_id}\x1f{self.family_id or NO_ALLOWED_DIMENSIONS}"

    @property
    def needs_review(self) -> bool:
        return bool(
            self.blocked_reason
            or self.scope_mode == VALUE_SCOPE_UNREVIEWED
            or self.authority == "compatibility"
        )

    @property
    def selected_count(self) -> int:
        return len(self.selected_value_ids) + len(self.selected_category_ids)

    @property
    def exception_count(self) -> int:
        return len(self.exception_value_ids) + len(self.exception_category_ids)


@dataclass(frozen=True)
class AllowedDimensionUndoEntry:
    tag_id: str
    user_relationships: tuple[KnowledgeRelationship, ...]
    inline_present: bool
    inline_groups: tuple[str, ...]
    provenance_present: bool
    provenance: Any
    before_fingerprint: str
    after_fingerprint: str


@dataclass(frozen=True)
class AllowedDimensionPolicyResult:
    tag_ids: tuple[str, ...]
    manifest_path: Path
    resolutions: tuple[AllowedDimensionPolicyResolution, ...]
    undo_entries: tuple[AllowedDimensionUndoEntry, ...] = ()

    def fingerprints(self) -> dict[str, str]:
        return {
            resolution.tag_id: resolution.record_fingerprint
            for resolution in self.resolutions
        }


def _source_tag_id(relationship: KnowledgeRelationship) -> str:
    if (
        relationship.kind == ALLOWED_DIMENSION
        and relationship.directed
        and relationship.endpoints
        and relationship.endpoints[0].kind == "tag"
    ):
        return str(relationship.endpoints[0].id)
    return ""


def _records_for_tag(
    relationships: Iterable[KnowledgeRelationship], tag_id: str,
) -> tuple[KnowledgeRelationship, ...]:
    stable_id = str(tag_id)
    return tuple(
        sorted(
            (
                relationship
                for relationship in relationships
                if _source_tag_id(relationship) == stable_id
            ),
            key=lambda relationship: relationship.id,
        )
    )


def _records_by_tag(
    relationships: Iterable[KnowledgeRelationship],
) -> dict[str, tuple[KnowledgeRelationship, ...]]:
    indexed: dict[str, list[KnowledgeRelationship]] = {}
    for relationship in relationships:
        tag_id = _source_tag_id(relationship)
        if tag_id:
            indexed.setdefault(tag_id, []).append(relationship)
    return {
        tag_id: tuple(sorted(records, key=lambda relationship: relationship.id))
        for tag_id, records in indexed.items()
    }


def publish_allowed_dimension_storage_authority(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> dict[str, tuple[KnowledgeRelationship, ...]]:
    """Publish canonical Allowed Details ownership for schema-v10 Tag saves."""

    indexed = allowed_dimension_relationship_index(relationships)
    tags.configure_allowed_dimension_authority(indexed)
    return indexed


def allowed_dimension_relationship_index(
    relationships: Iterable[KnowledgeRelationship],
) -> dict[str, tuple[KnowledgeRelationship, ...]]:
    """Index direct canonical Allowed Details records once for bulk adapters."""

    return _records_by_tag(relationships)


def _record_target(
    relationship: KnowledgeRelationship,
) -> tuple[str, tuple[str, ...]]:
    targets = relationship.endpoints[1:]
    if targets and all(target.kind == "modifier_dimension" for target in targets):
        return "dimensions", tuple(str(target.id) for target in targets)
    if (
        targets and targets[0].kind=="modifier_dimension"
        and all(target.kind in {"category","tag"} for target in targets[1:])
        and all(
            str(target.extras.get(SCOPE_ROLE_KEY) or SCOPE_ROLE_SELECTION)
            in SCOPE_ROLES
            for target in targets[1:]
        )
    ):
        return "dimensions", (str(targets[0].id),)
    if (
        len(targets) == 1
        and targets[0].kind == ALLOWED_DIMENSION_STATE
        and targets[0].id == NO_ALLOWED_DIMENSIONS
    ):
        return "none", ()
    return "unknown", ()


def load_reviewed_default_allowed_dimension_relationships(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> tuple[KnowledgeRelationship, ...]:
    """Validate and normalize retained shipped Allowed Details authority.

    Canonical relationships are the compiler input. Any remaining explicit
    shipped ``allowed_modifier_groups`` projection must have the same active
    family set, or one active no-details state, before schema-v3 layers retire
    the old-reader field.
    """

    all_relationships = tuple(relationships)
    records_by_tag = _records_by_tag(all_relationships)
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        if record.allowed_modifier_groups is None:
            continue
        active = tuple(
            relationship
            for relationship in records_by_tag.get(record.id, ())
            if relationship.reviewed and not relationship.disabled
        )
        dimensions: list[str] = []
        none_count = 0
        invalid = False
        for relationship in active:
            target_kind, target_ids = _record_target(relationship)
            if target_kind == "dimensions":
                dimensions.extend(target_ids)
            elif target_kind == "none":
                none_count += 1
            else:
                invalid = True
        projected = tuple(
            str(group).strip()
            for group in record.allowed_modifier_groups
            if str(group).strip()
        )
        if (
            invalid
            or len(projected) != len(set(projected))
            or len(dimensions) != len(set(dimensions))
            or (
                projected
                and (none_count or set(projected) != set(dimensions))
            )
            or (not projected and (none_count != 1 or dimensions))
        ):
            raise ValueError(
                f"Shipped Tag {record.id!r} Allowed Details projection lacks "
                "matching active canonical authority."
            )

    normalized: list[KnowledgeRelationship] = []
    represented: set[tuple[str, str]] = set()
    for relationship in sorted(all_relationships, key=lambda item: item.id):
        details = dict(relationship.details)
        marker = details.get("default_compiler", "")
        authority = details.get("reviewed_authority", "")
        projection = details.get("compatibility_projection", "")
        managed = (
            marker == DEFAULT_ALLOWED_DIMENSION_COMPILER
            or authority == REVIEWED_ALLOWED_DIMENSION_AUTHORITY
            or (
                relationship.source == DEFAULT_ALLOWED_DIMENSION_SOURCE
                and projection == "tag.relations.allowed_modifier_groups"
            )
        )
        if not managed:
            continue
        source = relationship.source_endpoint
        source_record = (
            tags.tags.get(source.id)
            if source is not None and source.kind == "tag" else None
        )
        target_kind, target_ids = _record_target(relationship)
        family_id = target_ids[0] if target_kind == "dimensions" else ""
        expected_id = (
            allowed_dimension_relationship_id(source.id, family_id)
            if source is not None and family_id else
            allowed_dimension_none_relationship_id(source.id)
            if source is not None and target_kind == "none" else ""
        )
        policy_mode = str(details.get("policy_mode") or "")
        if (
            relationship.kind != ALLOWED_DIMENSION
            or not relationship.directed
            or not relationship.reviewed
            or relationship.layer != "default"
            or relationship.scope != "any"
            or source_record is None
            or target_kind not in {"dimensions", "none"}
            or (target_kind == "dimensions" and len(target_ids) != 1)
            or policy_mode
            != ("explicit_none" if target_kind == "none" else "explicit_set")
            or (
                family_id
                and str(details.get("canonical_family_id") or "") != family_id
            )
            or projection != "tag.relations.allowed_modifier_groups"
        ):
            raise ValueError(
                f"Shipped Allowed Details relationship {relationship.id!r} is "
                "not one valid reviewed canonical authority record."
            )
        if relationship.id != expected_id:
            raise ValueError(
                f"Shipped Allowed Details relationship for {source_record.id!r} "
                f"has unstable ID {relationship.id!r}."
            )
        key = (source_record.id, family_id or NO_ALLOWED_DIMENSIONS)
        if key in represented:
            raise ValueError(
                f"Shipped Tag {source_record.id!r} has duplicate retained "
                "Allowed Details authority."
            )
        represented.add(key)
        if marker and marker != DEFAULT_ALLOWED_DIMENSION_COMPILER:
            raise ValueError(
                f"Shipped Allowed Details relationship {relationship.id!r} has "
                f"unknown legacy compiler marker {marker!r}."
            )
        if authority and authority != REVIEWED_ALLOWED_DIMENSION_AUTHORITY:
            raise ValueError(
                f"Shipped Allowed Details relationship {relationship.id!r} has "
                f"unknown reviewed authority marker {authority!r}."
            )
        details.pop("default_compiler", None)
        details["reviewed_authority"] = REVIEWED_ALLOWED_DIMENSION_AUTHORITY
        endpoints = list(relationship.endpoints)
        endpoints[0] = RelationshipEndpoint(
            "tag", source_record.id, source_record.display,
        )
        normalized.append(replace(
            relationship,
            endpoints=tuple(endpoints),
            label=(
                f"{source_record.display} allows no details"
                if target_kind == "none" else
                f"{source_record.display} allowed {endpoints[1].label}"
            ),
            source=DEFAULT_ALLOWED_DIMENSION_SOURCE,
            details=tuple((str(key), str(value)) for key, value in details.items()),
        ))
    return tuple(normalized)


def _record_value_scope(
    relationship: KnowledgeRelationship,family_id: str,
) -> AllowedDimensionValueScope:
    details=dict(relationship.details)
    mode=str(details.get("value_scope") or VALUE_SCOPE_UNREVIEWED)
    if mode not in VALUE_SCOPE_MODES:mode=VALUE_SCOPE_UNREVIEWED
    targets=relationship.endpoints[2:]
    def ids(kind: str,role: str) -> tuple[str,...]:
        return tuple(dict.fromkeys(
            str(target.id) for target in targets
            if target.kind==kind and str(
                target.extras.get(SCOPE_ROLE_KEY) or SCOPE_ROLE_SELECTION
            )==role
        ))
    value_ids=ids("tag",SCOPE_ROLE_SELECTION)
    category_ids=ids("category",SCOPE_ROLE_SELECTION)
    exception_value_ids=ids("tag",SCOPE_ROLE_EXCEPTION)
    exception_category_ids=ids("category",SCOPE_ROLE_EXCEPTION)
    if mode in {VALUE_SCOPE_ALL,VALUE_SCOPE_UNREVIEWED}:
        value_ids=category_ids=exception_value_ids=exception_category_ids=()
    return AllowedDimensionValueScope(
        str(family_id),mode,value_ids,category_ids,
        exception_value_ids,exception_category_ids,
    )


@dataclass(frozen=True)
class LegacyModifierPolicyCompatibility:
    """Finite reviewed legacy selection, never a native family assignment.

    Keep the original dimension and stock scope. Resolved targets are a detached
    recipient identity view, not replacement IDs to write into source records.
    """
    relationship_id: str
    dimension_id: str
    bank_group: str
    scope: AllowedDimensionValueScope
    selected_targets: tuple
    reason: str = ""

    @property
    def supported(self):
        return not self.reason


def inspect_legacy_finite_modifier_policy(row, *, foundation, categories, resolve_tag):
    """One shared finite legacy admission rule for Foundation and runtime consumers."""
    if row.kind != "allowed_dimension" or len(row.endpoints) < 2:
        return None
    base, dimension = row.endpoints[:2]
    bank = {"modifier_size": "size", "modifier_state": "state"}.get(dimension.id)
    if dimension.kind != "modifier_dimension" or bank is None:
        return None
    if (categories is not None and foundation.canonical_modifier_group(dimension.id, categories)
            in getattr(categories, "categories", {})):
        return None  # Existing native dimension authority takes precedence.
    scope = _record_value_scope(row, dimension.id)
    reason = ""
    details = dict(row.details)
    if row.disabled or not row.reviewed or base.kind != "tag":
        reason = "unsupported_legacy_policy_authority"
    elif any(details.get(key, dimension.id) != dimension.id
             for key in ("canonical_family_id", "modifier_dimension_id")):
        reason = "conflicting_legacy_dimension_identity"
    elif not foundation.modifiers.get(bank):
        reason = "unavailable_legacy_modifier_bank"
    elif (scope.mode != VALUE_SCOPE_ONLY or not scope.value_ids
          or scope.category_ids or scope.exception_value_ids or scope.exception_category_ids
          or any(endpoint.kind != "tag" or
                 endpoint.extras.get(SCOPE_ROLE_KEY, SCOPE_ROLE_SELECTION) != SCOPE_ROLE_SELECTION
                 for endpoint in row.endpoints[2:])
          or len(scope.value_ids) != len(row.endpoints[2:])):
        # A finite exact selection is already semantic authority in the saved
        # relationship. All/except/branch scopes need family closure, which
        # this compatibility path deliberately cannot infer from a bank.
        reason = "unsupported_legacy_value_scope"
    elif resolve_tag(base.id) is None:
        reason = "unavailable_legacy_policy_base"
    targets = tuple((identity, resolve_tag(identity)) for identity in scope.value_ids)
    if not reason and any(target is None for _, target in targets):
        reason = "unavailable_legacy_selected_tag"
    return LegacyModifierPolicyCompatibility(row.id, dimension.id, bank, scope, targets, reason)


def allowed_dimension_compatibility_payload(
    record: Any,
    resolution: AllowedDimensionPolicyResolution,
    relationships: Iterable[KnowledgeRelationship] = (),
) -> dict[str, Any]:
    """Serialize the explicit old-reader Allowed Details projection."""

    projected = copy.deepcopy(record)
    if resolution.authority in {"canonical_user", "canonical_default"}:
        projected.allowed_modifier_groups = list(resolution.groups)
        represented = {
            relationship.id: relationship
            for relationship in relationships
            if relationship.id in resolution.relationship_ids
        }
        provenance = _allowed_dimension_projection_provenance(
            tuple(represented.values()),
            source=resolution.provenance_source,
            reviewed_at=resolution.reviewed_at,
        )
        projected.relation_extras[
            "allowed_modifier_groups_provenance"
        ] = provenance
    return projected.to_dict(
        include_allowed_modifier_groups_projection=(
            projected.allowed_modifier_groups is not None
        ),
    )


def _allowed_dimension_projection_provenance(
    relationships: Iterable[KnowledgeRelationship],
    *,
    source: str = "",
    reviewed_at: str = "",
) -> dict[str, str]:
    """Reconstruct the exact mutable-writer provenance shape when available."""

    records = tuple(sorted(relationships, key=lambda item: item.id))
    detail_sets = tuple(dict(item.details) for item in records)

    def first(detail_key: str, fallback: str = "") -> str:
        return next(
            (
                str(details.get(detail_key) or "")
                for details in detail_sets
                if str(details.get(detail_key) or "")
            ),
            fallback,
        )

    provenance: dict[str, str] = {}
    projection_source = first(
        "review_source",
        source or next((str(item.source) for item in records if item.source), ""),
    )
    if projection_source:
        provenance["source"] = projection_source
    modifier_tag_id = first("modifier_tag_id")
    if modifier_tag_id or any("modifier_tag_id" in details for details in detail_sets):
        provenance["modifier_tag_id"] = modifier_tag_id
    projection_reviewed_at = first("reviewed_at", reviewed_at)
    if projection_reviewed_at:
        provenance["reviewed_at"] = projection_reviewed_at
    operation = first("operation")
    if operation:
        provenance["operation"] = operation
    provenance["authority"] = "relationships.json"
    reviewed_family_id = first("reviewed_family_id")
    if reviewed_family_id:
        provenance["reviewed_family_id"] = reviewed_family_id
    return provenance


def allowed_dimension_compatibility_payload_from_relationships(
    record: Any,
    relationships: Iterable[KnowledgeRelationship],
    *,
    records: Iterable[KnowledgeRelationship] | None = None,
) -> dict[str, Any]:
    """Project one direct canonical policy without constructing a service."""

    direct_records = tuple(
        _records_for_tag(relationships, str(record.id))
        if records is None else records
    )
    if not direct_records:
        return copy.deepcopy(record).to_dict()
    active_none = False
    ordered: list[tuple[int, int, str]] = []
    provenance_source = ""
    reviewed_at = ""
    for record_index, relationship in enumerate(direct_records):
        details = dict(relationship.details)
        provenance_source = provenance_source or str(
            details.get("review_source") or relationship.source or ""
        )
        reviewed_at = reviewed_at or str(details.get("reviewed_at") or "")
        if relationship.disabled:
            continue
        target_kind, target_ids = _record_target(relationship)
        if target_kind == "none":
            active_none = True
            continue
        if target_kind != "dimensions":
            continue
        try:
            policy_order = int(details.get("policy_order", record_index))
        except (TypeError, ValueError):
            policy_order = record_index
        ordered.extend(
            (policy_order, target_index, family_id)
            for target_index, family_id in enumerate(target_ids)
        )
    groups = tuple(dict.fromkeys(
        family_id for _order, _index, family_id in sorted(ordered)
    ))
    if active_none and groups:
        raise ValueError(
            f"Tag {record.id!r} has contradictory canonical Allowed Details authority."
        )
    projected = copy.deepcopy(record)
    projected.allowed_modifier_groups = list(() if active_none else groups)
    provenance = _allowed_dimension_projection_provenance(
        direct_records, source=provenance_source, reviewed_at=reviewed_at,
    )
    projected.relation_extras[
        "allowed_modifier_groups_provenance"
    ] = provenance
    return projected.to_dict(
        include_allowed_modifier_groups_projection=True,
    )


class AllowedDimensionPolicyService:
    """Layered Allowed Details authority with an atomic legacy projection."""

    def __init__(
        self,
        tags: TagDatabase,
        categories: Any,
        foundation: Any,
        *,
        default_path: str | Path,
        user_path: str | Path,
        workspace: RelationshipWorkspaceService | None = None,
        publish_shared_authority: bool = True,
    ) -> None:
        self.tags = tags
        self.rule_pair_admission=getattr(tags,"rule_pair_admission",None)
        if self.rule_pair_admission is not None and not self.rule_pair_admission.owns(tags.path,"knowledge"):
            raise ValueError("Allowed Details must use its exact Knowledge participant.")
        self.categories = categories
        self.foundation = foundation
        self._canonical_group_cache: dict[str, str] = {}
        self._group_label_cache: dict[str, str] = {}
        anchor_resolver = getattr(categories, "exact_category_anchor_ids", None)
        if callable(anchor_resolver):
            self._derived_base_anchors = dict(anchor_resolver())
        else:
            category_records = getattr(categories, "categories", {})
            category_values = (
                category_records.values()
                if hasattr(category_records, "values") else category_records
            )
            self._derived_base_anchors = exact_category_anchor_ids(
                category_values, tags.tags.values(),
            )
        self.workspace = workspace or RelationshipWorkspaceService(
            default_path, user_path, tags=tags,
        )
        if hasattr(self.categories, "configure_category_base_authority"):
            self.categories.configure_category_base_authority(
                self.workspace.effective,
            )
        paths = [
            self.workspace.default_store.path,
            getattr(categories, "default_path", None),
            getattr(categories, "user_path", None),
            getattr(foundation, "modifiers_path", None),
            getattr(foundation, "concepts_path", None),
        ]
        self._authority_fingerprints = tuple(
            (str(Path(path).resolve()), _file_fingerprint(path))
            for path in paths
            if path is not None
        )
        self._publish_shared_authority=bool(publish_shared_authority)
        self._refresh_relationship_indexes()

    def _admission(self):
        if getattr(self.tags,"rule_pair_admission",None)!=self.rule_pair_admission or (
            self.rule_pair_admission is not None
            and (
                not self.rule_pair_admission.owns(self.tags.path,"knowledge")
                or self.workspace.user_store.path.absolute()!=self.rule_pair_admission.root/"relationships.json"
            )
        ):
            raise RuleConversionAdmissionError("Saved Knowledge changed. Reopen Allowed Details before saving.")
        return self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()

    def _canonical_group(self, group: str) -> str:
        requested = str(group or "").strip()
        if requested not in self._canonical_group_cache:
            self._canonical_group_cache[requested] = (
                self.foundation.canonical_modifier_group(
                    requested, self.categories,
                )
            )
        return self._canonical_group_cache[requested]

    def _group_label(self, group: str) -> str:
        stable_id=str(group or "")
        if stable_id not in self._group_label_cache:
            self._group_label_cache[stable_id]=(
                self.foundation.modifier_group_label(stable_id,self.categories)
            )
        return self._group_label_cache[stable_id]

    def _ordered_groups(self, groups: Iterable[str]) -> tuple[str, ...]:
        result: list[str] = []
        for group in groups:
            stable_id = self._canonical_group(str(group))
            if stable_id and stable_id not in result:
                result.append(stable_id)
        return tuple(result)

    def _category_path_ids(self, category_id: str | None) -> tuple[str, ...]:
        stable_id=str(category_id or "")
        if not stable_id:
            return ()
        path_ids=getattr(self.categories,"path_ids",None)
        if callable(path_ids):
            return tuple(str(value) for value in path_ids(stable_id))
        result=[]; seen=set(); current=self.categories.get(stable_id)
        while current is not None and current.id not in seen:
            result.append(str(current.id)); seen.add(current.id)
            current=self.categories.get(current.parent_id or "")
        return tuple(reversed(result))

    def _scope_reference_issues(
        self,scope: AllowedDimensionValueScope,
    ) -> tuple[str,...]:
        issues=[]; family_id=scope.family_id
        if scope.mode != VALUE_SCOPE_UNREVIEWED and family_id not in self.categories.reusable_modifier_family_ids():
            return (f"unavailable reviewed modifier family {family_id!r}",)
        for value_id in (*scope.value_ids,*scope.exception_value_ids):
            record=self.tags.tags.get(value_id)
            if record is None:
                issues.append(f"unavailable Tag {value_id!r}")
                continue
            modifier=self.foundation.modifier_for_tag(record,self.categories)
            if modifier is None or self._canonical_group(modifier.group)!=family_id:
                issues.append(f"Tag {value_id!r} is not a reviewed member")
        for category_id in (*scope.category_ids,*scope.exception_category_ids):
            path=self._category_path_ids(category_id)
            if not self._category_path_ids(family_id):
                issues.append(f"unavailable collection {family_id!r}")
                break
            if not path:
                issues.append(f"unavailable Category {category_id!r}")
            elif (
                category_id==family_id or family_id not in path
                or self._canonical_group(
                    self.categories.modifier_family_source(category_id)
                )!=family_id
            ):
                issues.append(
                    f"Category {category_id!r} is not a descendant branch of this collection"
                )
        if set(scope.value_ids)&set(scope.exception_value_ids):
            issues.append("one exact Tag has conflicting decisions")
        if set(scope.category_ids)&set(scope.exception_category_ids):
            issues.append("one Category branch has conflicting decisions")
        return tuple(issues)

    def allows_value(
        self,policy: AllowedDimensionPolicyResolution,family_id: str,value_id: str,
    ) -> bool:
        record=self.tags.tags.get(str(value_id))
        path=self._category_path_ids(getattr(record,"category",None))
        return policy.allows_value(family_id,value_id,path)

    def _scope_endpoints(
        self,scope: AllowedDimensionValueScope,
        existing: Iterable[RelationshipEndpoint] = (),
    ) -> tuple[RelationshipEndpoint,...]:
        endpoints=[]; prior={
            (endpoint.kind,endpoint.id):endpoint
            for endpoint in existing
            if endpoint.kind in {"category","tag"}
        }
        for kind,stable_ids,role in (
            ("category",scope.category_ids,SCOPE_ROLE_SELECTION),
            ("category",scope.exception_category_ids,SCOPE_ROLE_EXCEPTION),
            ("tag",scope.value_ids,SCOPE_ROLE_SELECTION),
            ("tag",scope.exception_value_ids,SCOPE_ROLE_EXCEPTION),
        ):
            for stable_id in stable_ids:
                record=(
                    self.categories.get(stable_id)
                    if kind=="category" else self.tags.tags.get(stable_id)
                )
                label=str(
                    getattr(record,"name","") or
                    getattr(record,"display","") or stable_id
                )
                extras=copy.deepcopy(
                    getattr(prior.get((kind,stable_id)),"extras",{})
                ); extras[SCOPE_ROLE_KEY]=role
                endpoints.append(RelationshipEndpoint(kind,stable_id,label,extras))
        return tuple(endpoints)

    def _refresh_relationship_indexes(self) -> None:
        from .tag_alias_relationships import publish_tag_alias_storage_authority
        if self._publish_shared_authority:
            publish_tag_alias_storage_authority(self.tags,self.workspace.effective)
            publish_tag_semantic_authority(self.tags,self.workspace.effective)
        self._default_records_by_tag = _records_by_tag(
            self.workspace.default_store.relationships.values()
        )
        self._user_records_by_tag = _records_by_tag(
            self.workspace.user_store.relationships.values()
        )
        self._effective_records_by_tag = publish_allowed_dimension_storage_authority(
            self.tags, self.workspace.effective,
        )
        publish_scoped_constraint_storage_authority(
            self.tags,self.workspace.effective,
        )
        self._user_record_fingerprints = {
            tag_id: _payload_fingerprint(
                [relationship.to_dict() for relationship in relationships]
            )
            for tag_id, relationships in self._user_records_by_tag.items()
        }
        self._default_record_fingerprints = {
            tag_id: _payload_fingerprint(
                [relationship.to_dict() for relationship in relationships]
            )
            for tag_id, relationships in self._default_records_by_tag.items()
        }
        self._effective_record_fingerprints = {
            tag_id: _payload_fingerprint(
                [relationship.to_dict() for relationship in relationships]
            )
            for tag_id, relationships in self._effective_records_by_tag.items()
        }

    def _verify_authority(self) -> None:
        for path, expected in self._authority_fingerprints:
            if _file_fingerprint(path) != expected:
                raise RuntimeError(
                    "Reusable-policy authority changed after this review opened. "
                    "Reload and review it again."
                )
        relationship_path = self.workspace.user_store.path
        current = relationship_path.read_bytes() if relationship_path.exists() else None
        if current != self.workspace.user_store._loaded_bytes:
            raise RuntimeError(
                "Relationships changed after this review opened. Reload and review them again."
            )
        tag_path = self.tags.path
        current_tags = tag_path.read_bytes() if tag_path.exists() else None
        if current_tags != getattr(self.tags, "_loaded_bytes", None):
            raise RuntimeError(
                "System Knowledge changed after this review opened. Reload and review it again."
            )

    def _resolution_from_records(
        self,
        tag_id: str,
        records: tuple[KnowledgeRelationship, ...],
        *,
        source_kind: str,
        source_label: str,
        authority: str,
        layer: str,
        relationships_fingerprint: str,
    ) -> AllowedDimensionPolicyResolution:
        group_entries: list[tuple[int, int, str]] = []
        active_none = False
        blocked: list[str] = []
        scopes: dict[str,AllowedDimensionValueScope] = {}
        provenance_source = ""
        reviewed_at = ""
        for record_index, relationship in enumerate(records):
            target_kind, target_ids = _record_target(relationship)
            if target_kind == "unknown":
                blocked.append(
                    f"Relationship {relationship.id!r} uses a future or unsupported Allowed Details shape."
                )
                continue
            details = dict(relationship.details)
            provenance_source = provenance_source or str(
                details.get("review_source") or relationship.source or ""
            )
            reviewed_at = reviewed_at or str(details.get("reviewed_at") or "")
            if relationship.disabled:
                continue
            if target_kind == "none":
                if active_none:
                    blocked.append("More than one explicit no-details record is active.")
                active_none = True
                continue
            try:
                policy_order = int(details.get("policy_order", record_index))
            except (TypeError, ValueError):
                policy_order = record_index
            for target_index, family_id in enumerate(target_ids):
                canonical = self._canonical_group(family_id)
                if any(entry[2] == canonical for entry in group_entries):
                    blocked.append(
                        f"More than one active record allows family {canonical!r}."
                    )
                else:
                    group_entries.append(
                        (policy_order, target_index, canonical)
                    )
                    scope=_record_value_scope(relationship,canonical)
                    scope_targets=relationship.endpoints[2:]
                    scope_target_keys=tuple(
                        (target.kind,target.id) for target in scope_targets
                    )
                    if scope.mode in {VALUE_SCOPE_ALL,VALUE_SCOPE_UNREVIEWED} and scope_targets:
                        blocked.append(
                            f"Allowed Details family {canonical!r} stores branch or Tag references for a scope that cannot use them."
                        )
                    if len(scope_target_keys)!=len(set(scope_target_keys)):
                        blocked.append(
                            f"Allowed Details family {canonical!r} repeats one branch or exact Tag reference."
                        )
                    legacy = inspect_legacy_finite_modifier_policy(relationship,
                        foundation=self.foundation, categories=self.categories,
                        resolve_tag=self.tags.resolve_tag_id)
                    scope_issues = ((legacy.reason,) if legacy.reason else ()) if legacy is not None else self._scope_reference_issues(scope)
                    if scope_issues:
                        blocked.append(
                            f"Allowed Details family {canonical!r} has invalid scope references: "+
                            "; ".join(scope_issues[:3])+"."
                        )
                    scopes[canonical]=scope
        if active_none and group_entries:
            blocked.append(
                "The policy both allows modifier families and explicitly allows none."
            )
        ordered = self._ordered_groups(
            entry[2] for entry in sorted(group_entries)
        )
        fingerprint = _payload_fingerprint(
            {
                "tag_id": str(tag_id),
                "authority": authority,
                "relationships_fingerprint": relationships_fingerprint,
            }
        )
        return AllowedDimensionPolicyResolution(
            tag_id=str(tag_id),
            groups=() if active_none else ordered,
            source_kind=source_kind,
            source_tag_id=str(tag_id),
            source_label=source_label,
            authority=authority,
            layer=layer,
            relationship_ids=tuple(item.id for item in records),
            record_fingerprint=fingerprint,
            provenance_source=provenance_source,
            reviewed_at=reviewed_at,
            blocked_reason=" ".join(blocked),
            value_scopes=tuple(scopes[group] for group in ordered if group in scopes),
        )

    def _direct_for_tag(
        self,
        tag_id: str,
        *,
        reset_direct: bool = False,
    ) -> AllowedDimensionPolicyResolution:
        stable_id = str(tag_id)
        record = self.tags.tags.get(stable_id)
        if record is None:
            return AllowedDimensionPolicyResolution(
                tag_id=stable_id,
                record_fingerprint=_payload_fingerprint({"tag_id": stable_id, "missing": True}),
                blocked_reason="The SDXL Tag is no longer available.",
            )
        user_records = (
            () if reset_direct else self._user_records_by_tag.get(stable_id, ())
        )
        if user_records:
            return self._resolution_from_records(
                stable_id,
                user_records,
                source_kind="explicit",
                source_label="Explicit reviewed relationship policy",
                authority="canonical_user",
                layer="user",
                relationships_fingerprint=self._user_record_fingerprints[stable_id],
            )
        canonical_records = (
            self._default_records_by_tag.get(stable_id, ())
            if reset_direct
            else self._effective_records_by_tag.get(stable_id, ())
        )
        if canonical_records:
            layers = tuple(dict.fromkeys(item.layer for item in canonical_records))
            return self._resolution_from_records(
                stable_id,
                canonical_records,
                source_kind="default",
                source_label="Reviewed relationship default",
                authority="canonical_default",
                layer="+".join(layers),
                relationships_fingerprint=(
                    self._default_record_fingerprints[stable_id]
                    if reset_direct else
                    self._effective_record_fingerprints[stable_id]
                ),
            )
        explicit = (
            None
            if reset_direct
            else getattr(record, "allowed_modifier_groups", None)
        )
        if explicit is not None:
            provenance = getattr(record, "relation_extras", {}).get(
                "allowed_modifier_groups_provenance", {}
            )
            if not isinstance(provenance, dict):
                provenance = {}
            groups = self._ordered_groups(explicit)
            return AllowedDimensionPolicyResolution(
                tag_id=stable_id,
                groups=groups,
                source_kind="explicit",
                source_tag_id=stable_id,
                source_label="Inline compatibility policy awaiting migration",
                authority="compatibility",
                layer="legacy",
                record_fingerprint=_payload_fingerprint(
                    {
                        "tag_id": stable_id,
                        "authority": "compatibility",
                        "groups": list(explicit),
                        "provenance": provenance,
                    }
                ),
                provenance_source=str(provenance.get("source") or ""),
                reviewed_at=str(provenance.get("reviewed_at") or ""),
                value_scopes=tuple(
                    AllowedDimensionValueScope(group) for group in groups
                ),
            )
        concept = self.foundation.modifier_policy_concept_for_tag(record)
        if concept is not None:
            groups = self._ordered_groups(concept.allowed_modifiers)
            return AllowedDimensionPolicyResolution(
                tag_id=stable_id,
                groups=groups,
                source_kind="foundation",
                source_tag_id=stable_id,
                source_label=f"Reviewed foundation default for {record.display}",
                authority="foundation",
                layer="default",
                record_fingerprint=_payload_fingerprint(
                    {
                        "tag_id": stable_id,
                        "authority": "foundation",
                        "display": record.display,
                        "aliases": list(self.tags.effective_aliases(record)),
                        "concept_id": record.concept_id,
                        "resolved_concept_id": concept.id,
                        "groups": list(concept.allowed_modifiers),
                    }
                ),
                provenance_source="shipped_foundation_concept",
                value_scopes=tuple(
                    AllowedDimensionValueScope(group) for group in groups
                ),
            )
        return AllowedDimensionPolicyResolution(
            tag_id=stable_id,
            record_fingerprint=_payload_fingerprint(
                {
                    "tag_id": stable_id,
                    "authority": "none",
                    "display": record.display,
                    "aliases": list(self.tags.effective_aliases(record)),
                    "concept_id": record.concept_id,
                    "parent_id": self.tags.effective_parent_id(record),
                    "category": record.category,
                }
            ),
        )

    def effective_for_tag(self, tag_id: str) -> AllowedDimensionPolicyResolution:
        return self._effective_for_tag(tag_id)

    def direct_for_tag(self, tag_id: str) -> AllowedDimensionPolicyResolution:
        """Return the direct policy used by compatibility serializers."""

        return self._direct_for_tag(str(tag_id))

    def effective_after_reset(
        self,
        tag_id: str,
    ) -> AllowedDimensionPolicyResolution:
        """Preview the effective policy after clearing this Tag's direct authority."""
        return self._effective_for_tag(tag_id, reset_direct=True)

    def _effective_for_tag(
        self,
        tag_id: str,
        *,
        reset_direct: bool = False,
    ) -> AllowedDimensionPolicyResolution:
        stable_id = str(tag_id)
        visited: set[str] = set()
        current_id = stable_id
        path: list[dict[str, str]] = []
        cycle_detected = False
        inheritance_error = ""
        while current_id and current_id not in visited:
            visited.add(current_id)
            direct = self._direct_for_tag(
                current_id,
                reset_direct=reset_direct and current_id == stable_id,
            )
            path.append(
                {
                    "tag_id": current_id,
                    "fingerprint": direct.record_fingerprint,
                }
            )
            if direct.source_kind != "none" or direct.blocked_reason:
                if current_id == stable_id:
                    return direct
                base = self.tags.tags.get(current_id)
                return replace(
                    direct,
                    tag_id=stable_id,
                    source_kind="inherited",
                    source_label=(
                        f"Inherited from Base Tag {getattr(base, 'display', current_id)} "
                        f"({direct.source_kind} policy)"
                    ),
                    record_fingerprint=_payload_fingerprint(
                        {"tag_id": stable_id, "path": path}
                    ),
                )
            record = self.tags.tags.get(current_id)
            if record is None:
                break
            base_id, _source, resolution_kind = self.tags.effective_base_tag(
                record,
                self.categories,
                derived_anchors=self._derived_base_anchors,
            )
            if resolution_kind == "cycle":
                cycle_detected = True
                path.append({"tag_id": current_id, "fingerprint": "cycle"})
                break
            if resolution_kind == "ambiguous_explicit":
                inheritance_error = (
                    "The Base Tag policy inheritance has more than one active "
                    "canonical Broader Tag."
                )
                break
            if resolution_kind == "ambiguous_category_base":
                inheritance_error = (
                    "The Category Base policy has more than one active canonical record."
                )
                break
            if resolution_kind in {"missing_explicit", "missing_inherited"}:
                inheritance_error = (
                    "The Base Tag policy inheritance references a missing Tag."
                )
                break
            if not base_id:
                break
            next_id = str(base_id)
            if next_id in visited:
                cycle_detected = True
                path.append({"tag_id": next_id, "fingerprint": "cycle"})
                break
            current_id = next_id
        reason = (
            "The Base Tag policy inheritance contains a cycle."
            if cycle_detected else inheritance_error
        )
        return AllowedDimensionPolicyResolution(
            tag_id=stable_id,
            record_fingerprint=_payload_fingerprint(
                {"tag_id": stable_id, "path": path, "blocked": reason}
            ),
            blocked_reason=reason,
        )

    def _evidence_for_group(
        self,
        policy: AllowedDimensionPolicyResolution,
        requested_group: str,
    ) -> tuple[str, str, str]:
        """Classify exact stored evidence without inferring semantic fit."""
        family_label = (
            self._group_label(requested_group)
            if requested_group else "the displayed families"
        )
        scope=policy.scope_for(requested_group) if requested_group else None
        if scope is not None and scope.needs_review:
            return (
                "needs_review",
                "Value scope needs review",
                f"The policy allows {family_label}, but does not record whether all, selected, or all-except values apply.",
            )
        if policy.authority == "foundation":
            return (
                "reviewed",
                "Reviewed foundation family",
                "An exact shipped foundation concept allows this family.",
            )
        if policy.authority == "compatibility":
            source = policy.provenance_source.strip()
            if "migrat" in source.casefold():
                reason = (
                    f"Migration preserved {family_label}, but did not establish "
                    "a reviewed canonical family decision."
                )
            elif not source:
                reason = (
                    f"The inline compatibility policy has no recorded source for "
                    f"{family_label}."
                )
            else:
                reason = (
                    f"The attributed inline compatibility policy is not canonical "
                    f"reviewed authority for {family_label}."
                )
            return "needs_review", "Compatibility family needs review", reason

        if policy.authority not in {"canonical_user", "canonical_default"}:
            return (
                "needs_review",
                "Family evidence needs review",
                "No exact reviewed family authority could be identified.",
            )

        records = (
            self._user_records_by_tag.get(policy.source_tag_id, ())
            if policy.authority == "canonical_user" else
            self._effective_records_by_tag.get(policy.source_tag_id, ())
        )
        active: list[tuple[KnowledgeRelationship, str]] = []
        for relationship in records:
            if relationship.disabled:
                continue
            target_kind, target_ids = _record_target(relationship)
            if target_kind == "none":
                if not requested_group:
                    active.append((relationship, ""))
                continue
            for family_id in target_ids:
                canonical = self._canonical_group(family_id)
                if not requested_group or canonical == requested_group:
                    active.append((relationship, canonical))
        if not active:
            return (
                "needs_review",
                "Canonical family evidence is incomplete",
                "The effective family has no matching active canonical record.",
            )
        for relationship, target_family in active:
            details = dict(relationship.details)
            source = str(
                details.get("review_source") or relationship.source or ""
            )
            if not relationship.reviewed:
                return (
                    "needs_review",
                    "Canonical family needs review",
                    "The matching canonical relationship is not marked reviewed.",
                )
            if "migrat" in source.casefold():
                return (
                    "needs_review",
                    "Migrated canonical family needs review",
                    "Migration preserved the relationship without an exact family decision.",
                )
            if source == "qt_modifier_compatibility_workspace":
                reviewed_family = self._canonical_group(
                    str(details.get("reviewed_family_id") or "")
                )
                if not reviewed_family or (
                    target_family and reviewed_family != target_family
                ):
                    return (
                        "needs_review",
                        "Canonical review scope needs confirmation",
                        "A reverse-policy batch preserved this family, but its exact "
                        "review scope was different or was not recorded.",
                    )
        return (
            "reviewed",
            "Reviewed canonical family",
            "The exact family has reviewed canonical relationship authority.",
        )

    def base_entries(
        self, *, group: str = "", value_id: str = "",
    ) -> tuple[ModifierPolicyBaseEntry, ...]:
        requested = self._canonical_group(group)
        value_record=self.tags.tags.get(str(value_id)) if value_id else None
        value_category_path=self._category_path_ids(
            getattr(value_record,"category",None)
        )
        entries: list[ModifierPolicyBaseEntry] = []
        for record in self.tags.tags.values():
            policy = self._direct_for_tag(record.id)
            if policy.blocked_reason or policy.source_kind == "none":
                continue
            if requested and requested not in policy.groups:
                continue
            if value_id and not policy.allows_value(
                requested,str(value_id),value_category_path,
            ):
                continue
            evidence_status, evidence_label, evidence_reason = (
                self._evidence_for_group(policy, requested)
            )
            entries.append(
                ModifierPolicyBaseEntry(
                    tag_id=record.id,
                    display=record.display,
                    category_id=str(record.category or ""),
                    groups=policy.groups,
                    source_kind=policy.source_kind,
                    source_label=policy.source_label,
                    provenance_source=policy.provenance_source,
                    reviewed_at=policy.reviewed_at,
                    policy_fingerprint=policy.record_fingerprint,
                    authority=policy.authority,
                    relationship_ids=policy.relationship_ids,
                    evidence_status=evidence_status,
                    evidence_label=evidence_label,
                    evidence_reason=evidence_reason,
                )
            )
        return tuple(
            sorted(entries, key=lambda item: (item.display.casefold(), item.tag_id))
        )

    def audit_entries(self) -> tuple[AllowedDimensionPolicyAuditEntry, ...]:
        """Inventory direct policy decisions without judging semantic fit.

        One row represents one editable policy owner/family. Explicit
        no-details policies remain visible as owner rows, while inherited Tags
        are omitted because their source owner is already present.
        """
        entries: list[AllowedDimensionPolicyAuditEntry] = []
        family_labels = {"": "No allowed details"}
        for record in self.tags.tags.values():
            policy = self._direct_for_tag(record.id)
            if policy.source_kind == "none" and not policy.blocked_reason:
                continue
            family_ids = policy.groups or ("",)
            for family_id in family_ids:
                if family_id not in family_labels:
                    family_labels[family_id] = self.foundation.modifier_group_label(
                        family_id, self.categories,
                    )
                scope = (
                    policy.scope_for(family_id)
                    if family_id else
                    AllowedDimensionValueScope("", "none")
                )
                entries.append(
                    AllowedDimensionPolicyAuditEntry(
                        tag_id=record.id,
                        display=record.display,
                        category_id=str(record.category or ""),
                        family_id=family_id,
                        family_label=family_labels[family_id],
                        scope_mode=scope.mode,
                        selected_value_ids=scope.value_ids,
                        selected_category_ids=scope.category_ids,
                        exception_value_ids=scope.exception_value_ids,
                        exception_category_ids=scope.exception_category_ids,
                        source_kind=policy.source_kind,
                        source_label=policy.source_label,
                        authority=policy.authority,
                        layer=policy.layer,
                        relationship_ids=policy.relationship_ids,
                        provenance_source=policy.provenance_source,
                        reviewed_at=policy.reviewed_at,
                        blocked_reason=policy.blocked_reason,
                    )
                )
        return tuple(sorted(
            entries,
            key=lambda item: (
                item.display.casefold(), item.tag_id,
                item.family_label.casefold(), item.family_id,
            ),
        ))

    def _updated_relationship(
        self,
        current: KnowledgeRelationship,
        *,
        disabled: bool,
        reviewed_at: str,
        operation: str,
        review_source: str,
        modifier_tag_id: str,
        reviewed_family_id: str,
        policy_order: int | None = None,
        value_scope: AllowedDimensionValueScope | None = None,
    ) -> KnowledgeRelationship:
        details = dict(current.details)
        target_kind, target_ids = _record_target(current)
        canonical_family_id = (
            self._canonical_group(target_ids[0])
            if target_kind == "dimensions" and len(target_ids) == 1
            else ""
        )
        details.update(
            {
                "policy_mode": (
                    "explicit_none" if target_kind == "none" else "explicit_set"
                ),
                "operation": operation,
                "reviewed_at": reviewed_at,
                "review_source": review_source,
                "modifier_tag_id": modifier_tag_id,
                "compatibility_projection": "tag.relations.allowed_modifier_groups",
            }
        )

        if canonical_family_id:
            details["canonical_family_id"] = canonical_family_id
        if reviewed_family_id:
            details["reviewed_family_id"] = reviewed_family_id
        else:
            details.pop("reviewed_family_id", None)
        if policy_order is None:
            details.pop("policy_order", None)
        else:
            details["policy_order"] = str(policy_order)
        if value_scope is not None and target_kind=="dimensions" and len(target_ids)==1:
            details["value_scope"]=value_scope.mode
            family_endpoint=current.endpoints[1]
            endpoints=(
                current.endpoints[0],family_endpoint,
                *self._scope_endpoints(value_scope,current.endpoints[2:]),
            )
        else:
            endpoints=current.endpoints
        extras = copy.deepcopy(current.extras)
        history = [
            dict(item)
            for item in extras.get("allowed_dimension_history", ())
            if isinstance(item, Mapping)
        ]
        history.append(
            {
                "changed_at": reviewed_at,
                "relationship_fingerprint": relationship_fingerprint(current),
                "disabled": current.disabled,
                "layer": current.layer,
                "source": current.source,
                "endpoints": [endpoint.to_dict() for endpoint in current.endpoints],
            }
        )
        extras["allowed_dimension_history"] = history
        return replace(
            current,
            endpoints=endpoints,
            details=tuple((str(key), str(value)) for key, value in details.items()),
            version=current.version + 1,
            layer="user",
            source="user_review",
            reviewed=True,
            disabled=disabled,
            extras=extras,
        )

    def _new_dimension_relationship(
        self,
        tag_id: str,
        family_id: str,
        *,
        reviewed_at: str,
        operation: str,
        review_source: str,
        modifier_tag_id: str,
        reviewed_family_id: str,
        policy_order: int,
        value_scope: AllowedDimensionValueScope,
    ) -> KnowledgeRelationship:
        record = self.tags.tags[tag_id]
        family_label = self.foundation.modifier_group_label(
            family_id, self.categories,
        )
        details = [
            ("policy_mode", "explicit_set"),
            ("operation", operation),
            ("reviewed_at", reviewed_at),
            ("review_source", review_source),
            ("modifier_tag_id", modifier_tag_id),
            ("canonical_family_id", family_id),
            ("policy_order", str(policy_order)),
            ("value_scope",value_scope.mode),
            (
                "compatibility_projection",
                "tag.relations.allowed_modifier_groups",
            ),
        ]
        if reviewed_family_id:
            details.append(("reviewed_family_id", reviewed_family_id))
        return KnowledgeRelationship(
            id=allowed_dimension_relationship_id(tag_id, family_id),
            kind=ALLOWED_DIMENSION,
            endpoints=(
                RelationshipEndpoint("tag", tag_id, record.display),
                RelationshipEndpoint("modifier_dimension", family_id, family_label),
                *self._scope_endpoints(value_scope),
            ),
            directed=True,
            scope="any",
            label=f"{record.display} allowed {family_label}",
            reviewed=True,
            source="user_review",
            details=tuple(details),
            version=1,
            layer="user",
        )

    def _new_none_relationship(
        self,
        tag_id: str,
        *,
        reviewed_at: str,
        operation: str,
        review_source: str,
        modifier_tag_id: str,
        reviewed_family_id: str,
    ) -> KnowledgeRelationship:
        record = self.tags.tags[tag_id]
        details = [
            ("policy_mode", "explicit_none"),
            ("operation", operation),
            ("reviewed_at", reviewed_at),
            ("review_source", review_source),
            ("modifier_tag_id", modifier_tag_id),
            (
                "compatibility_projection",
                "tag.relations.allowed_modifier_groups",
            ),
        ]
        if reviewed_family_id:
            details.append(("reviewed_family_id", reviewed_family_id))
        return KnowledgeRelationship(
            id=allowed_dimension_none_relationship_id(tag_id),
            kind=ALLOWED_DIMENSION,
            endpoints=(
                RelationshipEndpoint("tag", tag_id, record.display),
                RelationshipEndpoint(
                    ALLOWED_DIMENSION_STATE,
                    NO_ALLOWED_DIMENSIONS,
                    "No allowed details",
                ),
            ),
            directed=True,
            scope="any",
            label=f"{record.display} allows no details",
            reviewed=True,
            source="user_review",
            details=tuple(details),
            version=1,
            layer="user",
        )

    def _build_explicit_records(
        self,
        tag_id: str,
        desired_groups: tuple[str, ...],
        user_values: dict[str, KnowledgeRelationship],
        user_records: tuple[KnowledgeRelationship, ...],
        *,
        reviewed_at: str,
        operation: str,
        review_source: str,
        modifier_tag_id: str,
        reviewed_family_id: str,
        value_scopes: Mapping[str,AllowedDimensionValueScope],
    ) -> None:
        default_records = self._default_records_by_tag.get(tag_id, ())
        current_by_id = {item.id: item for item in default_records}
        current_by_id.update({item.id: item for item in user_records})
        primary_by_family: dict[str, str] = {}
        primary_none = ""
        ordered_records = (*default_records, *user_records)
        for relationship in ordered_records:
            target_kind, target_ids = _record_target(relationship)
            if target_kind == "unknown" or len(target_ids) > 1:
                raise ValueError(
                    "This Tag has a future or unsupported Allowed Details record. "
                    "Review that canonical relationship before changing the policy."
                )
            if target_kind == "none":
                primary_none = primary_none or relationship.id
            elif target_ids:
                family_id = self._canonical_group(target_ids[0])
                primary_by_family.setdefault(family_id, relationship.id)

        desired = set(desired_groups)
        for relationship_id, current in current_by_id.items():
            target_kind, target_ids = _record_target(current)
            active = False
            if target_kind == "none":
                active = not desired and relationship_id == primary_none
            elif target_ids:
                family_id = self._canonical_group(target_ids[0])
                active = (
                    family_id in desired
                    and relationship_id == primary_by_family.get(family_id)
                )
            user_values[relationship_id] = self._updated_relationship(
                current,
                disabled=not active,
                reviewed_at=reviewed_at,
                operation=operation,
                review_source=review_source,
                modifier_tag_id=modifier_tag_id,
                reviewed_family_id=reviewed_family_id,
                policy_order=(
                    desired_groups.index(family_id)
                    if active and target_kind == "dimensions" else None
                ),
                value_scope=(value_scopes.get(family_id) if active and target_kind=="dimensions" else None),
            )

        for policy_order, family_id in enumerate(desired_groups):
            if family_id in primary_by_family:
                continue
            relationship = self._new_dimension_relationship(
                tag_id,
                family_id,
                reviewed_at=reviewed_at,
                operation=operation,
                review_source=review_source,
                modifier_tag_id=modifier_tag_id,
                reviewed_family_id=reviewed_family_id,
                policy_order=policy_order,
                value_scope=value_scopes.get(
                    family_id,AllowedDimensionValueScope(family_id)
                ),
            )
            collision = user_values.get(relationship.id)
            if collision is None:
                collision = self.workspace.default_store.relationships.get(
                    relationship.id
                )
            if collision is not None and (
                collision.kind != ALLOWED_DIMENSION
                or _source_tag_id(collision) != tag_id
                or _record_target(collision) != ("dimensions", (family_id,))
            ):
                raise ValueError(
                    "The reserved Allowed Details relationship ID is already in use."
                )
            user_values[relationship.id] = relationship

        if not desired and not primary_none:
            relationship = self._new_none_relationship(
                tag_id,
                reviewed_at=reviewed_at,
                operation=operation,
                review_source=review_source,
                modifier_tag_id=modifier_tag_id,
                reviewed_family_id=reviewed_family_id,
            )
            collision = user_values.get(relationship.id)
            if collision is None:
                collision = self.workspace.default_store.relationships.get(
                    relationship.id
                )
            if collision is not None and (
                collision.kind != ALLOWED_DIMENSION
                or _source_tag_id(collision) != tag_id
                or _record_target(collision) != ("none", ())
            ):
                raise ValueError(
                    "The reserved Allowed Details relationship ID is already in use."
                )
            user_values[relationship.id] = relationship

    def apply(
        self,
        updates: Mapping[str, Iterable[str] | AllowedDimensionPolicyUpdate | None],
        *,
        expected_fingerprints: Mapping[str, str] | None = None,
        operation: str = "manual_policy_edit",
        tag_operations: Mapping[str, str] | None = None,
        review_source: str = "qt_knowledge_editor",
        modifier_tag_id: str = "",
        reviewed_family_id: str = "",
        backup_root: str | Path | None = None,
    ) -> AllowedDimensionPolicyResult:
        with self._admission():
            return self._apply_admitted(updates,expected_fingerprints=expected_fingerprints,
                operation=operation,tag_operations=tag_operations,review_source=review_source,
                modifier_tag_id=modifier_tag_id,reviewed_family_id=reviewed_family_id,backup_root=backup_root)

    def _apply_admitted(
        self, updates: Mapping[str, Iterable[str] | AllowedDimensionPolicyUpdate | None], *,
        expected_fingerprints: Mapping[str, str] | None = None,
        operation: str = "manual_policy_edit",tag_operations: Mapping[str, str] | None = None,
        review_source: str = "qt_knowledge_editor",modifier_tag_id: str = "",
        reviewed_family_id: str = "",backup_root: str | Path | None = None,
    ) -> AllowedDimensionPolicyResult:
        self._verify_authority()
        normalized: dict[str, AllowedDimensionPolicyUpdate | None] = {}
        before_resolutions: dict[str, AllowedDimensionPolicyResolution] = {}
        for raw_tag_id, raw_groups in updates.items():
            tag_id = str(raw_tag_id)
            if tag_id not in self.tags.tags:
                raise ValueError("One reviewed Base Tag is no longer available.")
            current = self.effective_for_tag(tag_id)
            if current.blocked_reason:
                raise ValueError(current.blocked_reason)
            if expected_fingerprints is not None:
                expected = str(expected_fingerprints.get(tag_id, "missing"))
                if current.record_fingerprint != expected:
                    raise RuntimeError(
                        "A reusable policy changed after this review opened. "
                        "Reload and review it again."
                    )
            before_resolutions[tag_id] = current
            if raw_groups is None:
                normalized[tag_id]=None
            elif isinstance(raw_groups,AllowedDimensionPolicyUpdate):
                groups=self._ordered_groups(raw_groups.groups)
                provided={self._canonical_group(scope.family_id):scope for scope in raw_groups.value_scopes}
                scopes=[]
                for family_id in groups:
                    scope=provided.get(family_id,AllowedDimensionValueScope(family_id))
                    mode=scope.mode if scope.mode in VALUE_SCOPE_MODES else VALUE_SCOPE_UNREVIEWED
                    value_ids=tuple(dict.fromkeys(str(value_id) for value_id in scope.value_ids))
                    category_ids=tuple(dict.fromkeys(
                        str(category_id) for category_id in scope.category_ids
                    ))
                    exception_value_ids=tuple(dict.fromkeys(
                        str(value_id) for value_id in scope.exception_value_ids
                    ))
                    exception_category_ids=tuple(dict.fromkeys(
                        str(category_id)
                        for category_id in scope.exception_category_ids
                    ))
                    if mode in {VALUE_SCOPE_ALL,VALUE_SCOPE_UNREVIEWED}:
                        value_ids=category_ids=exception_value_ids=exception_category_ids=()
                    normalized_scope=AllowedDimensionValueScope(
                        family_id,mode,value_ids,category_ids,
                        exception_value_ids,exception_category_ids,
                    )
                    scope_issues=self._scope_reference_issues(normalized_scope)
                    if scope_issues:
                        raise ValueError(
                            f"Reusable collection {family_id!r} has an invalid value scope: "+
                            "; ".join(scope_issues[:3])+"."
                        )
                    scopes.append(normalized_scope)
                normalized[tag_id]=AllowedDimensionPolicyUpdate(groups,tuple(scopes))
            else:
                groups=self._ordered_groups(str(group) for group in raw_groups if str(group))
                normalized[tag_id]=AllowedDimensionPolicyUpdate(
                    groups,tuple(AllowedDimensionValueScope(group) for group in groups),
                )
        if not normalized:
            raise ValueError("No reviewed reusable-policy change was provided.")

        reviewed_at = _utc_now()
        reviewed_family = (
            self._canonical_group(reviewed_family_id)
            if reviewed_family_id else ""
        )
        user_values = dict(self.workspace.user_store.relationships)
        user_records_by_tag = _records_by_tag(user_values.values())
        undo_entries: list[AllowedDimensionUndoEntry] = []
        original_live: dict[str, tuple[list[str] | None, dict[str, Any]]] = {}
        for tag_id, desired_update in normalized.items():
            record = self.tags.tags[tag_id]
            tag_operation = str(
                (tag_operations or {}).get(tag_id) or operation
            )
            original_live[tag_id] = (
                copy.deepcopy(record.allowed_modifier_groups),
                copy.deepcopy(record.relation_extras),
            )
            prior_user = user_records_by_tag.get(tag_id, ())
            provenance_present = (
                "allowed_modifier_groups_provenance" in record.relation_extras
            )
            undo_entries.append(
                AllowedDimensionUndoEntry(
                    tag_id=tag_id,
                    user_relationships=prior_user,
                    inline_present=record.allowed_modifier_groups is not None,
                    inline_groups=tuple(record.allowed_modifier_groups or ()),
                    provenance_present=provenance_present,
                    provenance=copy.deepcopy(
                        record.relation_extras.get(
                            "allowed_modifier_groups_provenance"
                        )
                    ),
                    before_fingerprint=before_resolutions[tag_id].record_fingerprint,
                    after_fingerprint="",
                )
            )
            if desired_update is None:
                for relationship in prior_user:
                    user_values.pop(relationship.id, None)
                record.allowed_modifier_groups = None
                record.relation_extras.pop(
                    "allowed_modifier_groups_provenance", None,
                )
            else:
                desired=desired_update.groups
                scopes={scope.family_id:scope for scope in desired_update.value_scopes}
                self._build_explicit_records(
                    tag_id,
                    desired,
                    user_values,
                    prior_user,
                    reviewed_at=reviewed_at,
                    operation=tag_operation,
                    review_source=review_source,
                    modifier_tag_id=str(modifier_tag_id or ""),
                    reviewed_family_id=reviewed_family,
                    value_scopes=scopes,
                )
                record.allowed_modifier_groups = list(desired)
                record.relation_extras["allowed_modifier_groups_provenance"] = {
                    "source": review_source,
                    "modifier_tag_id": str(modifier_tag_id or ""),
                    "reviewed_at": reviewed_at,
                    "operation": tag_operation,
                    "authority": "relationships.json",
                }
                if reviewed_family:
                    record.relation_extras[
                        "allowed_modifier_groups_provenance"
                    ]["reviewed_family_id"] = reviewed_family

        self.tags._rebuild_lookup()
        effective = dict(self.workspace.default_store.relationships)
        effective.update(user_values)
        issues = validate_relationships(effective.values())
        if issues:
            for tag_id, (groups, extras) in original_live.items():
                record = self.tags.tags[tag_id]
                record.allowed_modifier_groups = groups
                record.relation_extras = extras
            self.tags._rebuild_lookup()
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        publish_allowed_dimension_storage_authority(
            self.tags, effective.values(),
        )
        publish_scoped_constraint_storage_authority(
            self.tags,effective.values(),
        )

        try:
            manifest_path, verified_service = self._write_transaction(
                user_values,
                normalized,
                backup_root=backup_root,
                reviewed_at=reviewed_at,
                kind="allowed_dimension_policy",
            )
        except BaseException:
            for tag_id, (groups, extras) in original_live.items():
                record = self.tags.tags.get(tag_id)
                if record is not None:
                    record.allowed_modifier_groups = groups
                    record.relation_extras = extras
            self.tags._rebuild_lookup()
            publish_allowed_dimension_storage_authority(
                self.tags, self.workspace.effective,
            )
            publish_scoped_constraint_storage_authority(
                self.tags,self.workspace.effective,
            )
            raise

        self.workspace = verified_service.workspace
        self._refresh_relationship_indexes()
        resolutions = tuple(
            self.effective_for_tag(tag_id) for tag_id in sorted(normalized)
        )
        by_tag = {resolution.tag_id: resolution for resolution in resolutions}
        completed_undo = tuple(
            replace(
                entry,
                after_fingerprint=by_tag[entry.tag_id].record_fingerprint,
            )
            for entry in undo_entries
        )
        return AllowedDimensionPolicyResult(
            tag_ids=tuple(sorted(normalized)),
            manifest_path=manifest_path,
            resolutions=resolutions,
            undo_entries=completed_undo,
        )

    def restore(
        self,
        result: AllowedDimensionPolicyResult,
        *,
        backup_root: str | Path | None = None,
    ) -> AllowedDimensionPolicyResult:
        with self._admission():
            return self._restore_admitted(result,backup_root=backup_root)

    def _restore_admitted(
        self,result: AllowedDimensionPolicyResult, *,backup_root: str | Path | None = None,
    ) -> AllowedDimensionPolicyResult:
        self._verify_authority()
        if not result.undo_entries:
            raise ValueError("This reusable-policy result has no saved undo state.")
        user_values = dict(self.workspace.user_store.relationships)
        user_records_by_tag = _records_by_tag(user_values.values())
        expected_before: dict[str, str] = {}
        original_live: dict[str, tuple[list[str] | None, dict[str, Any]]] = {}
        for entry in result.undo_entries:
            current = self.effective_for_tag(entry.tag_id)
            if current.record_fingerprint != entry.after_fingerprint:
                raise RuntimeError(
                    "A Base Tag policy changed after this batch. Review the "
                    "current policies instead of overwriting newer work."
                )
            record = self.tags.tags.get(entry.tag_id)
            if record is None:
                raise ValueError("One Base Tag from the saved batch is unavailable.")
            original_live[entry.tag_id] = (
                copy.deepcopy(record.allowed_modifier_groups),
                copy.deepcopy(record.relation_extras),
            )
            for relationship in user_records_by_tag.get(entry.tag_id, ()):
                user_values.pop(relationship.id, None)
            for relationship in entry.user_relationships:
                user_values[relationship.id] = relationship
            record.allowed_modifier_groups = (
                list(entry.inline_groups) if entry.inline_present else None
            )
            if entry.provenance_present:
                record.relation_extras["allowed_modifier_groups_provenance"] = (
                    copy.deepcopy(entry.provenance)
                )
            else:
                record.relation_extras.pop(
                    "allowed_modifier_groups_provenance", None,
                )
            expected_before[entry.tag_id] = entry.before_fingerprint
        self.tags._rebuild_lookup()
        effective = dict(self.workspace.default_store.relationships)
        effective.update(user_values)
        issues = validate_relationships(effective.values())
        if issues:
            for tag_id, (groups, extras) in original_live.items():
                record = self.tags.tags[tag_id]
                record.allowed_modifier_groups = groups
                record.relation_extras = extras
            self.tags._rebuild_lookup()
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        publish_allowed_dimension_storage_authority(
            self.tags, effective.values(),
        )
        publish_scoped_constraint_storage_authority(
            self.tags,effective.values(),
        )
        try:
            manifest_path, verified_service = self._write_transaction(
                user_values,
                {tag_id: "restore" for tag_id in expected_before},
                backup_root=backup_root,
                reviewed_at=_utc_now(),
                kind="allowed_dimension_policy_undo",
                expected_fingerprints=expected_before,
            )
        except BaseException:
            for tag_id, (groups, extras) in original_live.items():
                record = self.tags.tags.get(tag_id)
                if record is not None:
                    record.allowed_modifier_groups = groups
                    record.relation_extras = extras
            self.tags._rebuild_lookup()
            publish_allowed_dimension_storage_authority(
                self.tags, self.workspace.effective,
            )
            publish_scoped_constraint_storage_authority(
                self.tags,self.workspace.effective,
            )
            raise
        self.workspace = verified_service.workspace
        self._refresh_relationship_indexes()
        resolutions = tuple(
            self.effective_for_tag(tag_id) for tag_id in sorted(expected_before)
        )
        return AllowedDimensionPolicyResult(
            tag_ids=tuple(sorted(expected_before)),
            manifest_path=manifest_path,
            resolutions=resolutions,
        )

    def _write_transaction(
        self,
        user_values: Mapping[str, KnowledgeRelationship],
        expected: Mapping[str, Any],
        *,
        backup_root: str | Path | None,
        reviewed_at: str,
        kind: str,
        expected_fingerprints: Mapping[str, str] | None = None,
    ) -> tuple[Path, "AllowedDimensionPolicyService"]:
        relationship_path = self.workspace.user_store.path
        tag_path = self.tags.path
        relationship_bytes = (
            relationship_path.read_bytes() if relationship_path.exists() else None
        )
        tag_bytes = tag_path.read_bytes() if tag_path.exists() else None
        original_user = dict(self.workspace.user_store.relationships)
        self.workspace.user_store.replace(user_values.values())
        relationship_payload = self.workspace.user_store.payload()
        self.workspace.user_store.replace(original_user.values())
        effective = dict(self.workspace.default_store.relationships)
        effective.update(user_values)
        publish_scoped_constraint_storage_authority(
            self.tags,effective.values(),
        )
        tag_payload = self.tags._payload()

        root = (
            Path(backup_root)
            if backup_root is not None
            else tag_path.parent / "backups" / "relationship-allowed-dimensions"
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = root / f"{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        entries: list[dict[str, Any]] = []
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
            "kind": kind,
            "tag_ids": sorted(expected),
            "files": entries,
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(relationship_path, relationship_payload)
            _atomic_json(tag_path, tag_payload)
            saved_relationships = RelationshipStore(relationship_path)
            saved_tags = TagDatabase(tag_path,rule_pair_admission=self.rule_pair_admission)
            if saved_relationships.payload() != relationship_payload:
                raise RuntimeError(
                    "Allowed Details relationship save failed semantic read-back verification."
                )
            if saved_tags._payload() != tag_payload:
                raise RuntimeError(
                    "Allowed Details Tag storage failed semantic read-back verification."
                )
            verified = AllowedDimensionPolicyService(
                saved_tags,
                self.categories,
                self.foundation,
                default_path=self.workspace.default_store.path,
                user_path=relationship_path,
            )
            for tag_id, desired in expected.items():
                resolution = verified.effective_for_tag(tag_id)
                if expected_fingerprints is not None:
                    if resolution.record_fingerprint != expected_fingerprints[tag_id]:
                        raise RuntimeError(
                            "Allowed Details undo failed semantic read-back verification."
                        )
                elif desired is None:
                    if _records_for_tag(
                        verified.workspace.user_store.relationships.values(), tag_id,
                    ) or saved_tags.tags[tag_id].allowed_modifier_groups is not None:
                        raise RuntimeError(
                            "Inherited Allowed Details restoration failed semantic read-back verification."
                        )
                else:
                    desired_groups = (
                        desired.groups
                        if isinstance(desired,AllowedDimensionPolicyUpdate)
                        else tuple(desired)
                    )
                    if (
                        resolution.groups != desired_groups
                        or resolution.authority != "canonical_user"
                        or saved_tags.tags[tag_id].allowed_modifier_groups is not None
                        or "allowed_modifier_groups_provenance"
                        in saved_tags.tags[tag_id].relation_extras
                    ):
                        raise RuntimeError(
                            "Allowed Details policy failed semantic read-back verification."
                        )
                    if isinstance(desired,AllowedDimensionPolicyUpdate):
                        expected_scopes={scope.family_id:scope for scope in desired.value_scopes}
                        if any(
                            resolution.scope_for(family_id)!=expected_scopes.get(
                                family_id,AllowedDimensionValueScope(family_id)
                            )
                            for family_id in desired_groups
                        ):
                            raise RuntimeError(
                                "Allowed Details value scope failed semantic read-back verification."
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
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            raise
        manifest["status"] = "complete"
        manifest["completed_at"] = _utc_now()
        _atomic_json(manifest_path, manifest)
        self.tags._loaded_bytes = tag_path.read_bytes()
        return manifest_path, verified

    @classmethod
    def recover_interrupted(cls, data_root: str | Path) -> tuple[Path, ...]:
        with RulePairAdmission(Path(data_root).absolute()).admit():
            return cls._recover_interrupted_admitted(data_root)

    @classmethod
    def _recover_interrupted_admitted(cls, data_root: str | Path) -> tuple[Path, ...]:
        root = Path(data_root).resolve()
        backup_root = root / "backups" / "relationship-allowed-dimensions"
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
                        "Allowed Details recovery manifest contains an unsafe target."
                    )
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError(
                        "Allowed Details recovery manifest contains an unsafe backup path."
                    )
                if target in seen_targets:
                    raise RuntimeError(
                        "Allowed Details recovery manifest repeats a target."
                    )
                if entry.get("existed") and not backup.is_file():
                    raise RuntimeError("Allowed Details recovery backup is missing.")
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
