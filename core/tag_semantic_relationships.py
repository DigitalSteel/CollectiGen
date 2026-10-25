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

from .knowledge_relationships import (
    ENTAILMENT,
    SPECIFICITY,
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


SEMANTIC_KINDS = (SPECIFICITY, ENTAILMENT)
DEFAULT_SEMANTIC_COMPILER = "reviewed_tag_semantics_v1"
REVIEWED_TAG_SEMANTIC_AUTHORITY = "reviewed_tag_semantic_relationships_v1"
DEFAULT_SEMANTIC_SOURCE = "CollectiGen reviewed shipped Tag semantics"


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


def tag_semantic_relationship_id(kind: str, tag_id: str) -> str:
    if kind not in SEMANTIC_KINDS:
        raise ValueError(f"Unsupported semantic relationship kind {kind!r}.")
    digest = sha256(f"{kind}\x1f{tag_id}".encode("utf-8")).hexdigest()[:20]
    return f"tag_{kind}.{digest}"


def _canonical_target_id(
    raw_value: Any,
    relationships: Iterable[KnowledgeRelationship],
) -> str:
    raw = str(raw_value or "").strip()
    identity = normalize_tag(raw)
    matches = {
        endpoint.id
        for relationship in relationships
        for endpoint in relationship.target_endpoints
        if endpoint.kind == "tag"
        and (endpoint.id == raw or normalize_tag(endpoint.label) == identity)
    }
    return next(iter(matches)) if len(matches) == 1 else ""


def _compatibility_values_covered(
    values: Iterable[Any],
    relationships: Iterable[KnowledgeRelationship],
) -> bool:
    records = tuple(relationships)
    values = tuple(str(value) for value in values if str(value))
    return bool(values) and all(
        _canonical_target_id(value, records) for value in values
    )


def load_reviewed_default_tag_semantic_relationships(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> tuple[KnowledgeRelationship, ...]:
    """Validate and normalize retained shipped Broader Tag/Also Means authority.

    The reviewed canonical relationships are the compiler input. Any remaining
    nonempty old-reader projection must agree with active canonical authority;
    disabled or incomplete records cannot be masked by inline fallback data.
    """

    all_relationships = tuple(relationships)
    source_index = tag_semantic_relationship_index(all_relationships)
    for record in sorted(tags.tags.values(), key=lambda item: item.id):
        specificity = tuple(source_index.get((SPECIFICITY, record.id), ()))
        entailment = tuple(source_index.get((ENTAILMENT, record.id), ()))
        if record.parent_id:
            active = tuple(
                item
                for item in specificity
                if item.reviewed and not item.disabled
            )
            if (
                len(active) != 1
                or len(active[0].target_endpoints) != 1
                or not _compatibility_values_covered((record.parent_id,), active)
            ):
                raise ValueError(
                    f"Shipped Tag {record.id!r} Broader Tag projection lacks "
                    "matching active canonical authority."
                )
        if record.implies:
            active = tuple(
                item
                for item in (*specificity, *entailment)
                if item.reviewed and not item.disabled
            )
            if not _compatibility_values_covered(record.implies, active):
                raise ValueError(
                    f"Shipped Tag {record.id!r} Also Means projection lacks "
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
            marker == DEFAULT_SEMANTIC_COMPILER
            or authority == REVIEWED_TAG_SEMANTIC_AUTHORITY
            or (
                relationship.source == DEFAULT_SEMANTIC_SOURCE
                and projection in {
                    "tag.relations.parent",
                    "tag.relations.implies",
                }
            )
        )
        if not managed:
            continue
        source = relationship.source_endpoint
        source_record = (
            tags.tags.get(source.id)
            if source is not None and source.kind == "tag"
            else None
        )
        targets = relationship.target_endpoints
        target_records = tuple(
            tags.tags.get(endpoint.id)
            if endpoint.kind == "tag" else None
            for endpoint in targets
        )
        expected_projection = (
            "tag.relations.parent"
            if relationship.kind == SPECIFICITY
            else "tag.relations.implies"
        )
        if (
            relationship.kind not in SEMANTIC_KINDS
            or not relationship.directed
            or not relationship.reviewed
            or relationship.disabled
            or relationship.layer != "default"
            or relationship.scope != "any"
            or source_record is None
            or not targets
            or any(record is None for record in target_records)
            or any(endpoint.id == source.id for endpoint in targets)
            or len({endpoint.id for endpoint in targets}) != len(targets)
            or (relationship.kind == SPECIFICITY and len(targets) != 1)
            or projection != expected_projection
        ):
            raise ValueError(
                f"Shipped Tag semantic relationship {relationship.id!r} is not "
                "one valid reviewed canonical authority record."
            )
        expected_id = tag_semantic_relationship_id(
            relationship.kind, source_record.id,
        )
        if relationship.id != expected_id:
            raise ValueError(
                f"Shipped Tag semantic relationship for {source_record.id!r} "
                f"has unstable ID {relationship.id!r}."
            )
        key = (relationship.kind, source_record.id)
        if key in represented:
            raise ValueError(
                f"Shipped Tag {source_record.id!r} has duplicate retained "
                f"{relationship.kind} authority."
            )
        represented.add(key)
        if marker and marker != DEFAULT_SEMANTIC_COMPILER:
            raise ValueError(
                f"Shipped Tag semantic relationship {relationship.id!r} has "
                f"unknown legacy compiler marker {marker!r}."
            )
        if authority and authority != REVIEWED_TAG_SEMANTIC_AUTHORITY:
            raise ValueError(
                f"Shipped Tag semantic relationship {relationship.id!r} has "
                f"unknown reviewed authority marker {authority!r}."
            )
        details.pop("default_compiler", None)
        details["reviewed_authority"] = REVIEWED_TAG_SEMANTIC_AUTHORITY
        endpoints = (
            RelationshipEndpoint("tag", source_record.id, source_record.display),
            *tuple(
                RelationshipEndpoint("tag", record.id, record.display)
                for record in target_records
                if record is not None
            ),
        )
        normalized.append(replace(
            relationship,
            endpoints=endpoints,
            label=(
                f"{source_record.display} Broader Tag"
                if relationship.kind == SPECIFICITY
                else f"{source_record.display} Also Means"
            ),
            source=DEFAULT_SEMANTIC_SOURCE,
            details=tuple((str(key), str(value)) for key, value in details.items()),
        ))
    return tuple(normalized)


@dataclass(frozen=True)
class EffectiveTagSemanticLinks:
    tag_id: str
    parent_id: str
    implies: tuple[str, ...]
    specificity_relationship_ids: tuple[str, ...]
    entailment_relationship_ids: tuple[str, ...]
    specificity_represented: bool
    entailment_represented: bool
    compatibility_parent_id: str
    compatibility_implies: tuple[str, ...]


@dataclass(frozen=True)
class TagSemanticChange:
    tag_id: str
    parent_id: str = ""
    implies: tuple[str, ...] = ()
    kinds: tuple[str, ...] = SEMANTIC_KINDS
    source: str = "user_review"
    reason: str = ""


@dataclass(frozen=True)
class TagCategoryAssignment:
    tag_id: str
    category_id: str
    ordering_group: str
    ordering_priority: int
    source: str = "user_review"
    reason: str = ""


@dataclass(frozen=True)
class TagSemanticSaveResult:
    tag_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    manifest_path: Path


def _active_targets(
    records: Iterable[KnowledgeRelationship],
) -> tuple[str, ...]:
    values = []
    for relationship in records:
        if relationship.disabled or not relationship.reviewed:
            continue
        for endpoint in relationship.target_endpoints:
            if endpoint.kind == "tag" and endpoint.id not in values:
                values.append(endpoint.id)
    return tuple(values)


def publish_tag_semantic_authority(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> dict[tuple[str,str],tuple[KnowledgeRelationship,...]]:
    """Publish cached semantic targets and return the canonical source index."""
    frozen=tag_semantic_relationship_index(relationships)
    parent_sources={
        source_id for kind,source_id in frozen if kind==SPECIFICITY
    }
    implication_sources=set()
    implication_targets={}
    for source_id in {
        source_id for _kind, source_id in frozen
    }:
        record=tags.tags.get(source_id)
        if record is None:
            continue
        entailment=tuple(frozen.get((ENTAILMENT,source_id),()))
        specificity=tuple(frozen.get((SPECIFICITY,source_id),()))
        compatibility_implies=tuple(
            str(value)
            for value in getattr(record,"implies",())
            if str(value)
        )
        represented=(
            bool(entailment)
            or bool(specificity)
            and _compatibility_values_covered(
                compatibility_implies,specificity,
            )
        )
        if represented:
            implication_sources.add(source_id)
            implication_targets[source_id]=_active_targets(entailment)
    tags.configure_semantic_authority(
        {
            source_id:_active_targets(frozen.get((SPECIFICITY,source_id),()))
            for source_id in parent_sources
        },
        implication_targets,
        parent_sources,
        implication_sources,
    )
    return frozen


def tag_semantic_relationship_index(
    relationships: Iterable[KnowledgeRelationship],
) -> dict[tuple[str,str],tuple[KnowledgeRelationship,...]]:
    """Index canonical semantic records once for bounded bulk consumers."""
    source_index: dict[tuple[str,str],list[KnowledgeRelationship]]={}
    for relationship in relationships:
        source=relationship.source_endpoint
        if (
            relationship.kind in SEMANTIC_KINDS
            and relationship.directed
            and source is not None
            and source.kind=="tag"
        ):
            source_index.setdefault(
                (relationship.kind,source.id),[],
            ).append(relationship)
    frozen={
        key:tuple(sorted(values,key=lambda item:item.id))
        for key,values in source_index.items()
    }
    return frozen


def effective_tag_semantic_links(
    record: Any,
    relationships: Iterable[KnowledgeRelationship],
) -> EffectiveTagSemanticLinks:
    """Resolve canonical links, using inline fields only when unrepresented."""
    return effective_tag_semantic_links_from_index(
        record,tag_semantic_relationship_index(relationships),
    )


def effective_tag_semantic_links_from_index(
    record: Any,
    source_index: Mapping[
        tuple[str,str],Iterable[KnowledgeRelationship]
    ],
) -> EffectiveTagSemanticLinks:
    """Resolve one Tag from a prebuilt semantic relationship index."""
    tag_id=str(record.id)
    return _resolved_links(
        record,
        source_index.get((SPECIFICITY,tag_id),()),
        source_index.get((ENTAILMENT,tag_id),()),
    )


def tag_semantic_compatibility_payload(
    record: Any,
    source_index: Mapping[
        tuple[str,str],Iterable[KnowledgeRelationship]
    ],
) -> dict[str, Any]:
    """Serialize one exact old-reader semantic view without changing ``record``."""

    projected = copy.deepcopy(record)
    resolved = effective_tag_semantic_links_from_index(projected, source_index)
    if resolved.specificity_represented:
        projected.parent_id = resolved.parent_id or None
    if resolved.entailment_represented:
        projected.implies = list(resolved.implies)
    return projected.to_dict(
        include_parent_projection=True,
        include_implies_projection=True,
    )


def retire_tag_semantic_projection_payload(
    payload: Mapping[str, Any],
    relationships: Iterable[KnowledgeRelationship],
    *,
    retire_all_semantic_fields: bool = False,
) -> dict[str, Any]:
    """Retire canonical projections, or every field in validated shipped layers."""

    from .tag_database import TagRecord

    result = copy.deepcopy(dict(payload))
    result["schema_version"] = max(
        2, int(result.get("schema_version", 1) or 1),
    )
    source_index = tag_semantic_relationship_index(relationships)
    source_ids_by_name: dict[str, set[str]] = {}
    for (_kind, source_id), records in source_index.items():
        for relationship in records:
            source = relationship.source_endpoint
            if source is not None and source.kind == "tag" and source.label:
                source_ids_by_name.setdefault(
                    normalize_tag(source.label), set(),
                ).add(source_id)
    for item in result.get("tags", ()):
        if not isinstance(item, dict):
            continue
        relations = item.get("relations")
        if not isinstance(relations, dict):
            continue
        record = TagRecord.from_dict(item)
        if not any((kind, record.id) in source_index for kind in SEMANTIC_KINDS):
            matched_ids = source_ids_by_name.get(normalize_tag(record.display), set())
            if len(matched_ids) == 1:
                record.id = next(iter(matched_ids))
        resolved = effective_tag_semantic_links_from_index(record, source_index)
        if retire_all_semantic_fields or resolved.specificity_represented:
            relations.pop("parent", None)
        if retire_all_semantic_fields or resolved.entailment_represented:
            relations.pop("implies", None)
    return result


def _resolved_links(
    record: Any,
    specificity: Iterable[KnowledgeRelationship],
    entailment: Iterable[KnowledgeRelationship],
) -> EffectiveTagSemanticLinks:
    tag_id = str(record.id)
    specificity = tuple(specificity)
    entailment = tuple(entailment)
    parent_targets = _active_targets(specificity)
    if len(parent_targets) > 1:
        raise ValueError(
            f"Tag {tag_id!r} has more than one active Broader Tag target."
        )
    canonical_implies = _active_targets(entailment)
    compatibility_parent = str(getattr(record, "parent_id", "") or "")
    compatibility_implies = tuple(
        dict.fromkeys(str(value) for value in getattr(record, "implies", ()) if str(value))
    )
    redundant_implies_represented = (
        bool(specificity)
        and _compatibility_values_covered(compatibility_implies, specificity)
    )
    parent = parent_targets[0] if parent_targets else ""
    if not specificity:
        parent = compatibility_parent
    entails_are_represented = bool(entailment) or redundant_implies_represented
    implies = canonical_implies if entails_are_represented else compatibility_implies
    implies = tuple(value for value in implies if value != parent)
    return EffectiveTagSemanticLinks(
        tag_id=tag_id,
        parent_id=parent,
        implies=implies,
        specificity_relationship_ids=tuple(item.id for item in specificity),
        entailment_relationship_ids=tuple(item.id for item in entailment),
        specificity_represented=bool(specificity),
        entailment_represented=entails_are_represented,
        compatibility_parent_id=compatibility_parent,
        compatibility_implies=compatibility_implies,
    )


def _history_entry(relationship: KnowledgeRelationship) -> dict[str, Any]:
    return {
        "changed_at": _utc_now(),
        "relationship": relationship.to_dict(),
        "relationship_fingerprint": relationship_fingerprint(relationship),
    }


def _updated_relationship(
    current: KnowledgeRelationship | None,
    *,
    relationship_id: str,
    kind: str,
    source_endpoint: RelationshipEndpoint,
    target_endpoints: tuple[RelationshipEndpoint, ...],
    disabled: bool,
    source: str,
    reason: str,
    legacy_payload: Mapping[str, Any],
) -> KnowledgeRelationship:
    details = dict(current.details) if current is not None else {}
    details.update(
        {
            "reviewed_at": _utc_now(),
            "compatibility_projection": (
                "tag.relations.parent"
                if kind == SPECIFICITY else "tag.relations.implies"
            ),
        }
    )
    if reason:
        details["reason"] = reason
    extras = copy.deepcopy(current.extras) if current is not None else {}
    history = [
        dict(item)
        for item in extras.get("semantic_link_history", ())
        if isinstance(item, Mapping)
    ]
    if current is not None:
        history.append(_history_entry(current))
    if history:
        extras["semantic_link_history"] = history
    if legacy_payload and "legacy_compatibility" not in extras:
        extras["legacy_compatibility"] = copy.deepcopy(dict(legacy_payload))
    return KnowledgeRelationship(
        id=relationship_id,
        kind=kind,
        endpoints=(source_endpoint, *target_endpoints),
        directed=True,
        scope=(current.scope if current is not None else "any"),
        label=(
            current.label
            if current is not None and current.label
            else (
                f"{source_endpoint.label} Broader Tag"
                if kind == SPECIFICITY
                else f"{source_endpoint.label} Also Means"
            )
        ),
        reviewed=True,
        source=source,
        details=tuple((str(key), str(value)) for key, value in details.items()),
        version=(current.version + 1) if current is not None else 1,
        layer="user",
        disabled=disabled,
        extras=extras,
    )


def stage_tag_semantic_changes(
    tags: TagDatabase,
    workspace: RelationshipWorkspaceService,
    changes: Iterable[TagSemanticChange],
) -> tuple[dict[str, KnowledgeRelationship], tuple[str, ...]]:
    """Stage canonical semantic records and retire represented inline storage."""
    user_values = dict(workspace.user_store.relationships)
    effective = dict(workspace.default_store.relationships)
    effective.update(user_values)
    source_index: dict[tuple[str, str], list[KnowledgeRelationship]] = {}
    for relationship in effective.values():
        source=relationship.source_endpoint
        if (
            relationship.kind in SEMANTIC_KINDS
            and relationship.directed
            and source is not None
            and source.kind=="tag"
        ):
            source_index.setdefault(
                (relationship.kind, source.id), [],
            ).append(relationship)
    for records in source_index.values():
        records.sort(key=lambda item:item.id)
    changed_ids: list[str] = []
    for change in changes:
        tag_id = str(change.tag_id)
        record = tags.tags.get(tag_id)
        if record is None:
            raise ValueError("The SDXL Tag being edited is no longer available.")
        kinds = tuple(dict.fromkeys(str(kind) for kind in change.kinds))
        invalid = [kind for kind in kinds if kind not in SEMANTIC_KINDS]
        if invalid:
            raise ValueError(f"Unsupported semantic relationship kind {invalid[0]!r}.")
        parent_id = str(change.parent_id or "")
        implies = tuple(
            dict.fromkeys(str(value) for value in change.implies if str(value))
        )
        if parent_id and parent_id not in tags.tags:
            raise ValueError("The selected Broader Tag is no longer available.")
        missing = [value for value in implies if value not in tags.tags]
        if missing:
            raise ValueError("An Also Means target is no longer available: " + missing[0])
        if tag_id == parent_id or tag_id in implies:
            raise ValueError("An SDXL Tag cannot link to itself.")
        implies = tuple(value for value in implies if value != parent_id)
        desired = {
            SPECIFICITY: (parent_id,) if parent_id else (),
            ENTAILMENT: implies,
        }
        source_endpoint = RelationshipEndpoint("tag", tag_id, record.display)
        for kind in kinds:
            current_records = tuple(source_index.get((kind, tag_id), ()))
            targets = desired[kind]
            legacy_targets = (
                (str(record.parent_id),) if kind == SPECIFICITY and record.parent_id
                else tuple(str(value) for value in record.implies if str(value))
                if kind == ENTAILMENT else ()
            )
            if not current_records and not targets and not legacy_targets:
                continue
            primary = current_records[0] if current_records else None
            relationship_id = (
                primary.id if primary is not None
                else tag_semantic_relationship_id(kind, tag_id)
            )
            reserved = effective.get(relationship_id)
            if reserved is not None and reserved.kind != kind:
                raise ValueError(
                    "The reserved semantic relationship ID is already used by another relationship."
                )
            target_ids = targets or (
                tuple(
                    endpoint.id
                    for endpoint in primary.target_endpoints
                    if endpoint.kind == "tag"
                )
                if primary is not None else legacy_targets
            )
            target_endpoints = tuple(
                RelationshipEndpoint("tag", target_id, tags.tags[target_id].display)
                for target_id in target_ids
                if target_id in tags.tags and target_id != tag_id
            )
            if not target_endpoints:
                # No stored compatibility value exists to carry a disabled
                # record. An empty, never-set kind is already unambiguous.
                continue
            legacy_payload = {
                "parent_id": str(record.parent_id or ""),
                "implies": list(record.implies),
            }
            updated = _updated_relationship(
                primary,
                relationship_id=relationship_id,
                kind=kind,
                source_endpoint=source_endpoint,
                target_endpoints=target_endpoints,
                disabled=not bool(targets),
                source=str(change.source or "user_review"),
                reason=str(change.reason or ""),
                legacy_payload=legacy_payload,
            )
            user_values[updated.id] = updated
            effective[updated.id] = updated
            changed_ids.append(updated.id)
            for extra in current_records[1:]:
                disabled = replace(
                    extra,
                    layer="user",
                    source=str(change.source or "user_review"),
                    version=extra.version + 1,
                    disabled=True,
                    extras={
                        **copy.deepcopy(extra.extras),
                        "semantic_link_history": [
                            *[
                                dict(item)
                                for item in extra.extras.get("semantic_link_history", ())
                                if isinstance(item, Mapping)
                            ],
                            _history_entry(extra),
                        ],
                    },
                )
                user_values[disabled.id] = disabled
                effective[disabled.id] = disabled
                changed_ids.append(disabled.id)

            source_ids = {updated.id}
            source_ids.update(item.id for item in current_records)
            source_index[(kind, tag_id)] = sorted(
                (effective[item_id] for item_id in source_ids),
                key=lambda item: item.id,
            )

        resolved = _resolved_links(
            record,
            source_index.get((SPECIFICITY, tag_id), ()),
            source_index.get((ENTAILMENT, tag_id), ()),
        )
        if resolved.specificity_represented:
            record.parent_id = None
            record.parent_projection_present = False
        else:
            record.parent_id = resolved.parent_id or None
        if resolved.entailment_represented:
            record.implies = []
            record.implies_projection_present = False
        else:
            record.implies = list(resolved.implies)

    issues = validate_relationships(effective.values())
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    tags._rebuild_lookup()
    return user_values, tuple(dict.fromkeys(changed_ids))


class TagSemanticRelationshipService:
    """Canonical Broader Tag / Also Means authority with old-reader projections."""

    def __init__(
        self,
        tags: TagDatabase,
        *,
        default_path: str | Path,
        user_path: str | Path,
        workspace: RelationshipWorkspaceService | None = None,
    ) -> None:
        self.tags = tags
        self.rule_pair_admission = getattr(tags, "rule_pair_admission", None)
        if self.rule_pair_admission is not None and not self.rule_pair_admission.owns(tags.path, "knowledge"):
            raise ValueError("Tag meaning links must use their exact Knowledge participant.")
        self.workspace = workspace or RelationshipWorkspaceService(
            default_path, user_path, tags=tags,
        )
        self._authority_fingerprints = (
            (
                str(self.workspace.default_store.path.resolve()),
                _file_fingerprint(self.workspace.default_store.path),
            ),
        )
        self._rebuild_index()

    def _admission(self):
        if getattr(self.tags, "rule_pair_admission", None) != self.rule_pair_admission or (
            self.rule_pair_admission is not None
            and (
                not self.rule_pair_admission.owns(self.tags.path, "knowledge")
                or self.workspace.user_store.path.absolute()!=self.rule_pair_admission.root/"relationships.json"
            )
        ):
            raise RuleConversionAdmissionError(
                "Saved Knowledge changed. Reopen Tag meaning links before saving."
            )
        return self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()

    def _rebuild_index(self) -> None:
        from .tag_alias_relationships import publish_tag_alias_storage_authority
        relationships=self.workspace.effective
        self._effective_relationships=relationships
        publish_tag_alias_storage_authority(self.tags,relationships)
        self._source_index=publish_tag_semantic_authority(
            self.tags,relationships,
        )
        publish_scoped_constraint_storage_authority(
            self.tags,relationships,
        )

    @property
    def effective_relationships(self) -> tuple:
        """Return the exact authority snapshot already joined for this service."""
        return self._effective_relationships

    def effective_for_tag(self, tag_id: str) -> EffectiveTagSemanticLinks:
        record = self.tags.tags.get(str(tag_id))
        if record is None:
            raise ValueError("The SDXL Tag is no longer available.")
        return _resolved_links(
            record,
            self._source_index.get((SPECIFICITY, str(tag_id)), ()),
            self._source_index.get((ENTAILMENT, str(tag_id)), ()),
        )

    def _verify_authority(self) -> None:
        for path, expected in self._authority_fingerprints:
            if _file_fingerprint(path) != expected:
                raise RuntimeError(
                    "Shipped relationship authority changed after this Tag was opened. Reload and review it again."
                )
        relationship_path = self.workspace.user_store.path
        relationship_bytes = relationship_path.read_bytes() if relationship_path.exists() else None
        if relationship_bytes != self.workspace.user_store._loaded_bytes:
            raise RuntimeError(
                "Relationships changed after this Tag was opened. Reload and review it again."
            )
        tag_path = self.tags.path
        tag_bytes = tag_path.read_bytes() if tag_path.exists() else None
        if tag_bytes != getattr(self.tags, "_loaded_bytes", None):
            raise RuntimeError(
                "System Knowledge changed after this Tag was opened. Reload and review it again."
            )

    def apply(
        self,
        changes: Iterable[TagSemanticChange],
        *,
        category_assignments: Iterable[TagCategoryAssignment] = (),
        backup_root: str | Path | None = None,
    ) -> TagSemanticSaveResult:
        with self._admission():
            return self._apply_admitted(
                changes, category_assignments=category_assignments, backup_root=backup_root,
            )

    def _apply_admitted(
        self,
        changes: Iterable[TagSemanticChange],
        *,
        category_assignments: Iterable[TagCategoryAssignment] = (),
        backup_root: str | Path | None = None,
    ) -> TagSemanticSaveResult:
        changes = tuple(changes)
        category_assignments = tuple(category_assignments)
        if not changes:
            raise ValueError("No semantic relationship change was provided.")
        change_ids = {str(change.tag_id) for change in changes}
        assignment_ids = [str(item.tag_id) for item in category_assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError(
                "Provide at most one Category assignment for each semantic Tag change."
            )
        if any(tag_id not in change_ids for tag_id in assignment_ids):
            raise ValueError(
                "A Category assignment must accompany the same Tag's semantic change."
            )
        self._verify_authority()
        original_user_values = dict(self.workspace.user_store.relationships)
        original_tags = copy.deepcopy(self.tags.tags)
        try:
            for assignment in category_assignments:
                record = self.tags.tags.get(str(assignment.tag_id))
                if record is None:
                    raise ValueError(
                        "The SDXL Tag being moved is no longer available."
                    )
                category_id = str(assignment.category_id or "")
                if not category_id:
                    raise ValueError("Choose an existing Category for the SDXL Tag.")
                previous = record.category
                if previous == category_id:
                    continue
                record.category = category_id
                if not record.ordering.reviewed:
                    record.ordering.group = str(assignment.ordering_group)
                    record.ordering.priority = int(assignment.ordering_priority)
                record.provenance.append({
                    "source": str(assignment.source or "user_review"),
                    "reviewed_at": _utc_now(),
                    "action": "change_category_with_semantic_relationship",
                    "previous_category": previous,
                    "category": category_id,
                    **(
                        {"reason": str(assignment.reason)}
                        if assignment.reason else {}
                    ),
                })
            user_values, changed_ids = stage_tag_semantic_changes(
                self.tags, self.workspace, changes,
            )
        except BaseException:
            self.tags.tags = original_tags
            self.tags._rebuild_lookup()
            raise
        relationship_path = self.workspace.user_store.path
        tag_path = self.tags.path
        relationship_bytes = relationship_path.read_bytes() if relationship_path.exists() else None
        tag_bytes = tag_path.read_bytes() if tag_path.exists() else None
        try:
            self.workspace.user_store.replace(user_values.values())
            relationship_payload = self.workspace.user_store.payload()
            effective = dict(self.workspace.default_store.relationships)
            effective.update(user_values)
            from .tag_alias_relationships import publish_tag_alias_storage_authority
            publish_tag_alias_storage_authority(self.tags,effective.values())
            publish_scoped_constraint_storage_authority(
                self.tags,effective.values(),
            )
            tag_payload = self.tags._payload()
            self.workspace.user_store.replace(original_user_values.values())
            root = (
                Path(backup_root) if backup_root is not None
                else tag_path.parent / "backups" / "relationship-tag-semantics"
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
                entries.append({
                    "target": str(path.resolve()),
                    "backup": backup_name,
                    "existed": payload is not None,
                })
            manifest_path = backup / "recovery.json"
            manifest = {
                "schema_version": 1,
                "status": "pending",
                "created_at": _utc_now(),
                "kind": "tag_semantic_relationships",
                "tag_ids": list(dict.fromkeys((
                    *(change.tag_id for change in changes),
                    *(item.tag_id for item in category_assignments),
                ))),
                "category_assignments": [
                    {
                        "tag_id": item.tag_id,
                        "category_id": item.category_id,
                    }
                    for item in category_assignments
                ],
                "relationship_ids": list(changed_ids),
                "files": entries,
            }
            _atomic_json(manifest_path, manifest)
        except BaseException:
            self.workspace.user_store.replace(original_user_values.values())
            restored = TagDatabase(tag_path, rule_pair_admission=self.rule_pair_admission)
            self.tags.tags = restored.tags
            self.tags.merge_rules = restored.merge_rules
            self.tags._payload_extras = restored._payload_extras
            self.tags._lookup = restored._lookup
            self._rebuild_index()
            raise
        try:
            _atomic_json(relationship_path, relationship_payload)
            _atomic_json(tag_path, tag_payload)
            saved_relationships = RelationshipStore(relationship_path)
            saved_tags = TagDatabase(tag_path, rule_pair_admission=self.rule_pair_admission)
            if saved_relationships.payload() != relationship_payload:
                raise RuntimeError("Semantic relationship save failed read-back verification.")
            if saved_tags._payload() != tag_payload:
                raise RuntimeError("Semantic Tag storage failed read-back verification.")
            verified_workspace = RelationshipWorkspaceService(
                self.workspace.default_store.path, relationship_path,
                tags=saved_tags,
            )
            for change in changes:
                expected = effective_tag_semantic_links(
                    saved_tags.tags[str(change.tag_id)], verified_workspace.effective,
                )
                if SPECIFICITY in change.kinds and expected.parent_id != str(change.parent_id or ""):
                    raise RuntimeError("Broader Tag failed semantic read-back verification.")
                wanted_implies = tuple(
                    value for value in dict.fromkeys(change.implies)
                    if value and value != str(change.parent_id or "")
                )
                if ENTAILMENT in change.kinds and expected.implies != wanted_implies:
                    raise RuntimeError("Also Means failed semantic read-back verification.")
            for assignment in category_assignments:
                record = saved_tags.tags.get(str(assignment.tag_id))
                if (
                    record is None
                    or record.category != str(assignment.category_id)
                ):
                    raise RuntimeError(
                        "SDXL Tag Category assignment failed read-back verification."
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
            self._rebuild_index()
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            raise
        manifest["status"] = "complete"
        manifest["completed_at"] = _utc_now()
        _atomic_json(manifest_path, manifest)
        self.workspace = verified_workspace
        self._rebuild_index()
        self.tags._loaded_bytes = tag_path.read_bytes()
        return TagSemanticSaveResult(
            tag_ids=tuple(dict.fromkeys((
                *(str(change.tag_id) for change in changes),
                *(str(item.tag_id) for item in category_assignments),
            ))),
            relationship_ids=changed_ids,
            manifest_path=manifest_path,
        )

    @classmethod
    def recover_interrupted(cls, data_root: str | Path) -> tuple[Path, ...]:
        with RulePairAdmission(Path(data_root).absolute()).admit():
            return cls._recover_interrupted_admitted(data_root)

    @classmethod
    def _recover_interrupted_admitted(cls, data_root: str | Path) -> tuple[Path, ...]:
        root = Path(data_root).resolve()
        backup_root = root / "backups" / "relationship-tag-semantics"
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
                backup = (manifest_path.parent / str(entry.get("backup") or "")).resolve()
                if target.parent != root or target.name not in allowed:
                    raise RuntimeError("Semantic relationship recovery manifest contains an unsafe target.")
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError("Semantic relationship recovery manifest contains an unsafe backup path.")
                if target in seen_targets:
                    raise RuntimeError("Semantic relationship recovery manifest repeats a target.")
                if entry.get("existed") and not backup.is_file():
                    raise RuntimeError("Semantic relationship recovery backup is missing.")
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
