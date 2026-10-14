from __future__ import annotations

import copy
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterable, Mapping

from .knowledge_relationships import SCOPED_CONSTRAINT, KnowledgeRelationship
from .tag_database import TagDatabase, TagRecord


SCOPED_CONSTRAINT_COMPATIBILITY_PROJECTION = "tag.relations.conflicts"
LEGACY_INLINE_CONSTRAINT_COMPILER = "reviewed_inline_constraints_v1"
CONFLICT_RULE_MEMBER_PROJECTION_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class ScopedConstraintCompatibilityIndex:
    represented_targets: Mapping[str, tuple[str, ...]]
    active_targets: Mapping[str, tuple[str, ...]]


def _projection_pairs(
    relationship: KnowledgeRelationship,
) -> tuple[tuple[str, str], ...]:
    if relationship.kind != SCOPED_CONSTRAINT or relationship.directed:
        return ()
    if (
        dict(relationship.details).get("compatibility_projection")
        != SCOPED_CONSTRAINT_COMPATIBILITY_PROJECTION
    ):
        return ()
    endpoint_ids = sorted(
        {
            endpoint.id
            for endpoint in relationship.endpoints
            if endpoint.kind == "tag" and endpoint.id
        }
    )
    return tuple(combinations(endpoint_ids, 2))


def scoped_constraint_compatibility_index(
    relationships: Iterable[KnowledgeRelationship],
) -> ScopedConstraintCompatibilityIndex:
    """Index represented and active old-reader conflict pairs once."""

    represented: dict[str, set[str]] = {}
    active: dict[str, set[str]] = {}
    for relationship in sorted(relationships, key=lambda item: item.id):
        for first, second in _projection_pairs(relationship):
            represented.setdefault(first, set()).add(second)
            represented.setdefault(second, set()).add(first)
            if relationship.reviewed and not relationship.disabled:
                active.setdefault(first, set()).add(second)
                active.setdefault(second, set()).add(first)
    return ScopedConstraintCompatibilityIndex(
        represented_targets={
            source: tuple(sorted(targets))
            for source, targets in sorted(represented.items())
        },
        active_targets={
            source: tuple(sorted(targets))
            for source, targets in sorted(active.items())
        },
    )


def publish_scoped_constraint_storage_authority(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> ScopedConstraintCompatibilityIndex:
    """Publish canonical conflict-pair ownership for schema-v10 Tag storage."""

    index = scoped_constraint_compatibility_index(relationships)
    tags.configure_scoped_constraint_authority(index.represented_targets)
    return index


def scoped_constraint_compatibility_payload(
    record: TagRecord,
    relationships: Iterable[KnowledgeRelationship] = (),
    *,
    index: ScopedConstraintCompatibilityIndex | None = None,
) -> dict[str, Any]:
    """Serialize an explicit old-reader conflict view without changing a Tag."""

    resolved = index or scoped_constraint_compatibility_index(relationships)
    represented = set(resolved.represented_targets.get(record.id, ()))
    values = [
        str(value)
        for value in record.conflicts
        if str(value) and str(value) not in represented
    ]
    values.extend(resolved.active_targets.get(record.id, ()))
    payload = record.to_dict(
        include_conflicts_projection=(
            bool(values)
            or (record.conflicts_projection_present and not represented)
        ),
        conflicts_projection_values=tuple(dict.fromkeys(values)),
    )
    relations = payload.get("relations")
    if represented and isinstance(relations, dict):
        provenance = relations.get("constraint_provenance")
        if isinstance(provenance, list):
            retained = [
                copy.deepcopy(item)
                for item in provenance
                if not isinstance(item, dict)
                or str(item.get("target_id") or "") not in represented
            ]
            if retained:
                relations["constraint_provenance"] = retained
            else:
                relations.pop("constraint_provenance", None)
    return payload


def canonical_scoped_constraint_projection_pairs(
    relationships: Iterable[KnowledgeRelationship],
) -> frozenset[tuple[str, str]]:
    """Return Tag pairs owned by independently reviewed constraint authority."""

    result: set[tuple[str, str]] = set()
    for relationship in relationships:
        details = dict(relationship.details)
        if (
            details.get("default_compiler") == LEGACY_INLINE_CONSTRAINT_COMPILER
        ):
            continue
        result.update(_projection_pairs(relationship))
    return frozenset(result)


def validate_shipped_scoped_constraint_projections(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> int:
    """Require canonical authority for every shipped inline conflict pair."""

    projected: set[tuple[str, str]] = set()
    invalid: set[tuple[str, str]] = set()
    for source in tags.tags.values():
        for target_id in source.conflicts:
            target = str(target_id).strip()
            pair = tuple(sorted((source.id, target)))
            if not target or target == source.id or target not in tags.tags:
                invalid.add(pair)
                continue
            projected.add(pair)
    if invalid:
        rendered = ", ".join(": ".join(pair) for pair in sorted(invalid)[:10])
        raise ValueError(
            "Shipped Tag conflict projections contain invalid endpoints: " + rendered
        )
    canonical = canonical_scoped_constraint_projection_pairs(relationships)
    missing = sorted(projected - canonical)
    if missing:
        rendered = ", ".join(": ".join(pair) for pair in missing[:10])
        raise ValueError(
            "Shipped Tag conflict projections lack canonical scoped-constraint "
            "authority: " + rendered
        )
    return len(projected)


def retire_shipped_scoped_constraint_projection_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove validated scoped-constraint projections from one shipped layer.

    The default relationship compiler must first validate the effective inline
    pairs against independently reviewed canonical authority. Mutable user
    records and unresolved or future compatibility values remain untouched.
    """

    result = copy.deepcopy(dict(payload))
    result["schema_version"] = max(
        4, int(result.get("schema_version", 1) or 1),
    )
    for item in result.get("tags", ()):
        if not isinstance(item, dict):
            continue
        relations = item.get("relations")
        if not isinstance(relations, dict):
            continue
        relations.pop("conflicts", None)
        relations.pop("constraint_provenance", None)
    return result


def validate_shipped_conflict_rule_member_projections(
    payload: Mapping[str, Any],
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
    *,
    require_retired: bool = False,
) -> int:
    """Require every shipped conflict-rule shim to use canonical authority.

    Schema-v2 rule members were a flat compatibility description. Before those
    members are removed, each value must resolve to exactly one branch of the
    active reviewed canonical scoped constraint. Schema-v3 keeps only the rule
    identity and presentation metadata beside that canonical relationship ID.
    """

    from .knowledge_relationships import KnowledgeRelationshipRegistry
    from .relationship_evaluator import RelationshipEvaluator

    raw_rules = payload.get("rules", payload.get("conflicts", ()))
    if not isinstance(raw_rules, list):
        raise ValueError("Shipped conflict rules must be one JSON list.")
    all_relationships = tuple(relationships)
    relationship_by_id = {item.id: item for item in all_relationships}
    registry = KnowledgeRelationshipRegistry(all_relationships, tags=tags)
    evaluator = RelationshipEvaluator(registry)
    seen_rule_ids: set[str] = set()
    seen_relationship_ids: set[str] = set()
    represented = 0
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise ValueError(
                f"Shipped conflict rule {index + 1} must be one JSON object."
            )
        rule_id = str(item.get("id") or "").strip()
        relationship_id = str(item.get("relationship_id") or "").strip()
        if not rule_id or not relationship_id:
            raise ValueError(
                "Every shipped conflict rule must name a stable rule ID and "
                "canonical relationship ID."
            )
        if rule_id in seen_rule_ids:
            raise ValueError(f"Duplicate shipped conflict rule ID: {rule_id}")
        if relationship_id in seen_relationship_ids:
            raise ValueError(
                "Multiple shipped conflict rules reference canonical relationship "
                f"{relationship_id}."
            )
        seen_rule_ids.add(rule_id)
        seen_relationship_ids.add(relationship_id)
        relationship = relationship_by_id.get(relationship_id)
        if (
            relationship is None
            or relationship.kind != SCOPED_CONSTRAINT
            or relationship.directed
            or relationship.disabled
            or not relationship.reviewed
        ):
            raise ValueError(
                f"Shipped conflict rule {rule_id} lacks active reviewed canonical "
                f"scoped-constraint authority {relationship_id}."
            )
        scope = str(item.get("scope") or "image")
        if relationship.scope != scope:
            raise ValueError(
                f"Shipped conflict rule {rule_id} scope {scope} does not match "
                f"canonical scope {relationship.scope}."
            )
        if str(item.get("relation") or "mutually_exclusive") != "mutually_exclusive":
            raise ValueError(
                f"Shipped conflict rule {rule_id} has unsupported relation semantics."
            )
        branch_ids = {
            endpoint.id
            for endpoint in relationship.endpoints
            if endpoint.kind == "tag" and endpoint.id
        }
        if len(branch_ids) < 2:
            raise ValueError(
                f"Canonical scoped constraint {relationship_id} has fewer than two "
                "Tag branches."
            )
        if "tags" in item:
            if require_retired:
                raise ValueError(
                    f"Shipped conflict rule {rule_id} still stores projected members."
                )
            raw_members = item.get("tags")
            if not isinstance(raw_members, list) or not raw_members:
                raise ValueError(
                    f"Shipped conflict rule {rule_id} has an invalid member projection."
                )
            represented_branches: set[str] = set()
            resolved_members: set[str] = set()
            for raw_member in raw_members:
                member = str(raw_member).strip()
                tag_id = registry.resolve_tag_id(member)
                if not member or not tag_id:
                    raise ValueError(
                        f"Shipped conflict rule {rule_id} has unknown member {member!r}."
                    )
                if tag_id in resolved_members:
                    raise ValueError(
                        f"Shipped conflict rule {rule_id} repeats member {member!r}."
                    )
                resolved_members.add(tag_id)
                branches = branch_ids.intersection(
                    {tag_id, *evaluator.broader_paths(tag_id)}
                )
                if len(branches) != 1:
                    raise ValueError(
                        f"Shipped conflict rule {rule_id} member {member!r} does not "
                        "resolve to exactly one canonical branch."
                    )
                represented_branches.update(branches)
            if represented_branches != branch_ids:
                missing = ", ".join(sorted(branch_ids - represented_branches))
                raise ValueError(
                    f"Shipped conflict rule {rule_id} omits canonical branches: {missing}"
                )
        represented += 1
    if require_retired and int(payload.get("schema_version", 1) or 1) < (
        CONFLICT_RULE_MEMBER_PROJECTION_SCHEMA_VERSION
    ):
        raise ValueError("Projection-free shipped conflict rules require schema v3.")
    return represented


def retire_shipped_conflict_rule_member_projection_payload(
    payload: Mapping[str, Any],
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> dict[str, Any]:
    """Remove validated flat members from canonical shipped conflict shims."""

    all_relationships = tuple(relationships)
    validate_shipped_conflict_rule_member_projections(
        payload, tags, all_relationships,
    )
    result = copy.deepcopy(dict(payload))
    result["schema_version"] = max(
        CONFLICT_RULE_MEMBER_PROJECTION_SCHEMA_VERSION,
        int(result.get("schema_version", 1) or 1),
    )
    raw_rules = result.get("rules", result.get("conflicts", ()))
    for item in raw_rules:
        if isinstance(item, dict) and str(item.get("relationship_id") or "").strip():
            item.pop("tags", None)
    validate_shipped_conflict_rule_member_projections(
        result, tags, all_relationships, require_retired=True,
    )
    return result
