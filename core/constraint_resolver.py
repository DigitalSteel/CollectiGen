from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from .knowledge_relationships import (
    SCOPED_CONSTRAINT,
    KnowledgeRelationship,
)


def constraint_pairs(
    relationship: KnowledgeRelationship,
) -> set[tuple[str, str]]:
    if relationship.kind != SCOPED_CONSTRAINT or relationship.directed:
        return set()
    tag_ids = sorted(
        {endpoint.id for endpoint in relationship.endpoints if endpoint.kind == "tag"}
    )
    return {tuple(sorted(pair)) for pair in combinations(tag_ids, 2)}


def owns_inline_constraint_projection(
    relationship: KnowledgeRelationship,
) -> bool:
    return (
        dict(relationship.details).get("compatibility_projection")
        == "tag.relations.conflicts"
    )


@dataclass(frozen=True)
class EffectiveConstraintConflict:
    relationship_id: str
    label: str
    scope: str
    tag_ids: tuple[str, ...]
    authority: str = "canonical"


class EffectiveConstraintResolver:
    """Resolve canonical constraints plus unretired inline compatibility pairs."""

    def __init__(self, knowledge, evaluator=None, *, relationship_error: str = ""):
        self.knowledge = knowledge
        self.evaluator = evaluator
        self.relationship_error = str(relationship_error)
        self.relationships = tuple(
            relationship
            for relationship in (
                evaluator.registry.relationships if evaluator is not None else ()
            )
            if relationship.kind == SCOPED_CONSTRAINT
        )
        self._owned_inline_pairs = {
            pair
            for relationship in self.relationships
            if owns_inline_constraint_projection(relationship)
            for pair in constraint_pairs(relationship)
        }

    @classmethod
    def load(cls, knowledge) -> "EffectiveConstraintResolver":
        from .relationship_evaluator import active_relationship_evaluator

        try:
            evaluator = active_relationship_evaluator(knowledge)
        except Exception as joined_exc:
            try:
                # Small/partial registries may not contain every shipped stable
                # endpoint. The relationship store can still be validated and
                # evaluated by ID without the optional current-name join.
                evaluator = active_relationship_evaluator()
            except Exception as store_exc:
                # A damaged canonical store must not revive its inline
                # projection as active behavior. Keep the caller available and
                # fail closed.
                return cls(
                    knowledge,
                    relationship_error=f"{joined_exc}; {store_exc}",
                )
        return cls(knowledge, evaluator)

    def conflicts(
        self, tag_ids: Iterable[str], *, scope: str
    ) -> tuple[EffectiveConstraintConflict, ...]:
        if self.evaluator is None:
            return ()
        values = tuple(dict.fromkeys(str(value) for value in tag_ids if str(value)))
        result = list(self._canonical_conflicts(values, scope=scope))
        for first, second in combinations(values, 2):
            compatibility = self._compatibility_conflict(first, second)
            if compatibility is not None:
                result.append(compatibility)
        return tuple(result)

    def conflicts_with(
        self,
        target_id: str,
        present_ids: Iterable[str],
        *,
        scope: str,
    ) -> tuple[EffectiveConstraintConflict, ...]:
        if self.evaluator is None:
            return ()
        target_id = str(target_id)
        present = tuple(
            value
            for value in dict.fromkeys(str(item) for item in present_ids if str(item))
            if value != target_id
        )
        values = (*present, target_id)
        result = [
            conflict
            for conflict in self._canonical_conflicts(values, scope=scope)
            if target_id in conflict.tag_ids
        ]
        for present_id in present:
            compatibility = self._compatibility_conflict(target_id, present_id)
            if compatibility is not None:
                result.append(compatibility)
        return tuple(result)

    def _canonical_conflicts(
        self,
        values: tuple[str, ...],
        *,
        scope: str,
    ) -> tuple[EffectiveConstraintConflict, ...]:
        result = []
        for conflict in self.evaluator.conflicts(values, scope=scope):
            involved = {
                tag_id
                for _branch, matches in conflict.branch_matches
                for tag_id in matches
            }
            result.append(
                EffectiveConstraintConflict(
                    conflict.relationship_id,
                    conflict.label,
                    conflict.scope,
                    tuple(value for value in values if value in involved),
                )
            )
        return tuple(result)

    def _compatibility_conflict(
        self,
        first: str,
        second: str,
    ) -> EffectiveConstraintConflict | None:
        pair = tuple(sorted((str(first), str(second))))
        if pair in self._owned_inline_pairs:
            return None
        if (
            second not in self.knowledge.legacy_conflict_ids(first)
            and first not in self.knowledge.legacy_conflict_ids(second)
        ):
            return None
        return EffectiveConstraintConflict(
            f"system:{pair[0]}:{pair[1]}",
            "Reviewed SDXL Tag conflict",
            "legacy_unspecified",
            (str(first), str(second)),
            authority="compatibility",
        )

    def conflicting_tag_ids(
        self,
        target_id: str,
        present_ids: Iterable[str],
        *,
        scope: str,
    ) -> tuple[str, ...]:
        present = tuple(dict.fromkeys(str(value) for value in present_ids))
        involved = set()
        for conflict in self.conflicts_with(target_id, present, scope=scope):
            involved.update(
                tag_id for tag_id in conflict.tag_ids if tag_id != str(target_id)
            )
        return tuple(tag_id for tag_id in present if tag_id in involved)
