from __future__ import annotations

from typing import Any, Iterable, Mapping

from .knowledge_relationships import KnowledgeRelationship
from .reusable_modifier_projection import (
    ReusableModifierCompatibilityIndex,
    reusable_modifier_compatibility_index,
    reusable_modifier_compatibility_payload,
)
from .scoped_constraint_projection import (
    ScopedConstraintCompatibilityIndex,
    scoped_constraint_compatibility_index,
    scoped_constraint_compatibility_payload,
)
from .tag_alias_relationships import (
    TagAliasCompatibilityIndex,
    tag_alias_compatibility_index,
    tag_alias_compatibility_payload,
)
from .tag_database import TagDatabase, TagRecord
from .tag_semantic_relationships import (
    tag_semantic_compatibility_payload,
    tag_semantic_relationship_index,
)


def tag_relationship_compatibility_payload(
    record: TagRecord,
    relationships: Iterable[KnowledgeRelationship],
    *,
    tags: TagDatabase,
    categories: Any,
    foundation: Any,
    modifier_roles: Mapping[str, Any] | None = None,
    alias_index: TagAliasCompatibilityIndex | None = None,
    semantic_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    modifier_index: ReusableModifierCompatibilityIndex | None = None,
    constraint_index: ScopedConstraintCompatibilityIndex | None = None,
) -> dict[str, Any]:
    """Serialize one complete old-reader Tag relationship view.

    Ordinary live storage remains projection-free. Knowledge Pack export,
    removed-vocabulary archives, and lifecycle fingerprints call this explicit
    boundary so semantic, reusable-modifier, and scoped-constraint fields are
    reconstructed in one deterministic order.
    """

    # Bulk pack and lifecycle callers already build the reusable-modifier index.
    # Reuse its frozen relationship tuple instead of copying the complete graph
    # once per projected Tag.
    all_relationships = (
        modifier_index.relationships
        if modifier_index is not None
        else tuple(relationships)
    )
    resolved_aliases = (
        alias_index
        if alias_index is not None
        else tag_alias_compatibility_index(all_relationships)
    )
    resolved_semantics = (
        semantic_index
        if semantic_index is not None
        else tag_semantic_relationship_index(all_relationships)
    )
    resolved_modifiers = (
        modifier_index
        if modifier_index is not None
        else reusable_modifier_compatibility_index(
            tags,
            all_relationships,
            categories=categories,
            foundation=foundation,
            modifier_roles=modifier_roles,
        )
    )
    resolved_constraints = (
        constraint_index
        if constraint_index is not None
        else scoped_constraint_compatibility_index(all_relationships)
    )
    aliases = tag_alias_compatibility_payload(
        record,all_relationships,index=resolved_aliases,
    )
    semantic = tag_semantic_compatibility_payload(
        TagRecord.from_dict(aliases),resolved_semantics,
    )
    reusable = reusable_modifier_compatibility_payload(
        TagRecord.from_dict(semantic),
        all_relationships,
        tags=tags,
        categories=categories,
        foundation=foundation,
        modifier_roles=modifier_roles,
        index=resolved_modifiers,
    )
    return scoped_constraint_compatibility_payload(
        TagRecord.from_dict(reusable),
        all_relationships,
        index=resolved_constraints,
    )
