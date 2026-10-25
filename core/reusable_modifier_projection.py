from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .allowed_dimension_policy import (
    allowed_dimension_compatibility_payload_from_relationships,
    allowed_dimension_relationship_index,
    publish_allowed_dimension_storage_authority,
)
from .knowledge_relationships import KnowledgeRelationship
from .modifier_composition import (
    modifier_composition_compatibility_payload,
    modifier_composition_resolutions,
    publish_modifier_composition_storage_authority,
)
from .tag_database import TagDatabase, TagRecord


@dataclass(frozen=True)
class ReusableModifierCompatibilityIndex:
    relationships: tuple[KnowledgeRelationship, ...]
    modifier_resolutions: Mapping[str, Mapping[str, Any]]
    allowed_records_by_tag: Mapping[str, tuple[KnowledgeRelationship, ...]]


def reusable_modifier_compatibility_index(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
    *,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
) -> ReusableModifierCompatibilityIndex:
    """Build one bounded compatibility index for a bulk lifecycle view."""

    all_relationships = tuple(relationships)
    return ReusableModifierCompatibilityIndex(
        relationships=all_relationships,
        modifier_resolutions=modifier_composition_resolutions(
            all_relationships,
            tags=tags,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
        ),
        allowed_records_by_tag=allowed_dimension_relationship_index(
            all_relationships
        ),
    )


def publish_reusable_modifier_storage_authority(
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
    *,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
) -> tuple[KnowledgeRelationship, ...]:
    """Publish both reusable-modifier authorities for mutable Tag storage."""

    all_relationships = tuple(relationships)
    publish_modifier_composition_storage_authority(
        tags,
        all_relationships,
        categories=categories,
        foundation=foundation,
        modifier_roles=modifier_roles,
    )
    publish_allowed_dimension_storage_authority(tags, all_relationships)
    return all_relationships


def reusable_modifier_compatibility_payload(
    record: TagRecord,
    relationships: Iterable[KnowledgeRelationship],
    *,
    tags: TagDatabase,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
    index: ReusableModifierCompatibilityIndex | None = None,
) -> dict[str, Any]:
    """Serialize explicit old-reader composition and policy projections."""

    compatibility_index = index or reusable_modifier_compatibility_index(
        tags,
        relationships,
        categories=categories,
        foundation=foundation,
        modifier_roles=modifier_roles,
    )
    all_relationships = compatibility_index.relationships
    composed = modifier_composition_compatibility_payload(
        record,
        all_relationships,
        tags=tags,
        categories=categories,
        foundation=foundation,
        modifier_roles=modifier_roles,
        resolutions=compatibility_index.modifier_resolutions,
    )
    return allowed_dimension_compatibility_payload_from_relationships(
        TagRecord.from_dict(composed),
        all_relationships,
        records=compatibility_index.allowed_records_by_tag.get(str(record.id), ()),
    )


def retire_shipped_reusable_modifier_projection_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove validated reusable-modifier projections from one shipped layer.

    The default relationship compiler must validate the effective nonempty
    projections against canonical Modifier Value and Allowed Details authority
    before this helper is called. Mutable user records and their unresolved,
    exceptional, or future values are outside this shipped-layer operation.
    """

    result = copy.deepcopy(dict(payload))
    result["schema_version"] = max(
        3, int(result.get("schema_version", 1) or 1),
    )
    for item in result.get("tags", ()):
        if not isinstance(item, dict):
            continue
        relations = item.get("relations")
        if not isinstance(relations, dict):
            continue
        relations.pop("attributes", None)
        relations.pop("modifier_value_provenance", None)
        relations.pop("allowed_modifier_groups", None)
        relations.pop("allowed_modifier_groups_provenance", None)
    return result
