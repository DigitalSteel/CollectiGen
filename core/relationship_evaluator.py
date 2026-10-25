from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .knowledge_relationships import (
    SCOPED_CONSTRAINT,
    SPECIFICITY,
    KnowledgeRelationship,
    KnowledgeRelationshipRegistry,
    RelationshipEndpoint,
)


@dataclass(frozen=True)
class HierarchyDecision:
    tag_id: str
    action: str
    output_tag_id: str
    reason: str
    relationship_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class HierarchyConflict:
    relationship_id: str
    label: str
    scope: str
    branch_matches: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class HierarchyProjection:
    tag_ids: tuple[str, ...]
    decisions: tuple[HierarchyDecision, ...]
    conflicts: tuple[HierarchyConflict, ...]


class RelationshipEvaluator:
    """Evaluate reviewed hierarchy without mutating Image Knowledge."""

    def __init__(self, registry: KnowledgeRelationshipRegistry) -> None:
        self.registry = registry
        self._broader: dict[str, list[tuple[str, str]]] = {}
        self._narrower: dict[str, list[tuple[str, str]]] = {}
        self._constraints: list[KnowledgeRelationship] = []
        self._broader_path_cache: dict[
            tuple[str, int], dict[str, tuple[str, ...]]
        ] = {}
        for relationship in registry.relationships:
            if relationship.disabled or not relationship.reviewed:
                continue
            if relationship.kind == SPECIFICITY and relationship.directed:
                source = relationship.source_endpoint
                if source is None or source.kind != "tag":
                    continue
                for target in relationship.target_endpoints:
                    if target.kind != "tag":
                        continue
                    self._broader.setdefault(source.id, []).append(
                        (target.id, relationship.id)
                    )
                    self._narrower.setdefault(target.id, []).append(
                        (source.id, relationship.id)
                    )
            elif relationship.kind == SCOPED_CONSTRAINT and not relationship.directed:
                self._constraints.append(relationship)

    def broader_paths(self, tag_id: str, *, max_depth: int = 12) -> dict[str, tuple[str, ...]]:
        cache_key=(str(tag_id),int(max_depth))
        cached=self._broader_path_cache.get(cache_key)
        if cached is not None:
            return cached
        result: dict[str, tuple[str, ...]] = {}
        pending = [(str(tag_id), ())]
        while pending:
            current, path = pending.pop(0)
            if len(path) >= max_depth:
                continue
            for target, relationship_id in self._broader.get(current, ()):
                candidate = (*path, relationship_id)
                if target == tag_id or (
                    target in result and len(result[target]) <= len(candidate)
                ):
                    continue
                result[target] = candidate
                pending.append((target, candidate))
        self._broader_path_cache[cache_key]=result
        return result

    def is_same_branch(self, first_id: str, second_id: str) -> bool:
        if first_id == second_id:
            return True
        return (
            second_id in self.broader_paths(first_id)
            or first_id in self.broader_paths(second_id)
        )

    def _descends_from(self, tag_id: str, root_id: str) -> bool:
        return tag_id == root_id or root_id in self.broader_paths(tag_id)

    def conflicts(
        self, tag_ids: Iterable[str], *, scope: str,
        relationship_ids: Iterable[str] | None = None,
    ) -> tuple[HierarchyConflict, ...]:
        values = tuple(dict.fromkeys(str(value) for value in tag_ids))
        requested=(
            frozenset(str(value) for value in relationship_ids)
            if relationship_ids is not None else None
        )
        result = []
        for relationship in self._constraints:
            if requested is not None and relationship.id not in requested:
                continue
            if relationship.scope not in {"any", scope}:
                continue
            branches = []
            for endpoint in relationship.endpoints:
                if endpoint.kind != "tag":
                    continue
                matches = tuple(
                    value for value in values if self._descends_from(value, endpoint.id)
                )
                if matches:
                    branches.append((endpoint.id, matches))
            if len(branches) > 1:
                result.append(
                    HierarchyConflict(
                        relationship.id,
                        relationship.label or "Contrary Values",
                        relationship.scope,
                        tuple(branches),
                    )
                )
        return tuple(result)

    def keep_most_specific(
        self, tag_ids: Iterable[str], *, scope: str
    ) -> HierarchyProjection:
        values = tuple(dict.fromkeys(str(value) for value in tag_ids))
        conflicts = self.conflicts(values, scope=scope)
        if conflicts:
            held_values = {
                value
                for conflict in conflicts
                for _, matches in conflict.branch_matches
                for value in matches
            }
            return HierarchyProjection(
                values,
                tuple(
                    HierarchyDecision(
                        value,
                        "held" if value in held_values else "emitted",
                        value,
                        (
                            "Held because contrary values require review in this scope."
                            if value in held_values
                            else "Kept as a compatible value outside the conflict."
                        ),
                    )
                    for value in values
                ),
                conflicts,
            )

        omitted: dict[str, tuple[str, tuple[str, ...]]] = {}
        for specific in values:
            broader = self.broader_paths(specific)
            for candidate in values:
                if candidate == specific or candidate not in broader:
                    continue
                previous = omitted.get(candidate)
                path = broader[candidate]
                # Prefer the deepest reviewed path so the explanation names the
                # value that will actually survive a specificity chain, rather
                # than an intermediate value that is itself omitted.
                if previous is None or len(path) > len(previous[1]):
                    omitted[candidate] = (specific, path)

        output = tuple(value for value in values if value not in omitted)
        decisions = []
        for value in values:
            if value not in omitted:
                decisions.append(
                    HierarchyDecision(
                        value,
                        "emitted",
                        value,
                        "Kept as the most specific compatible value.",
                    )
                )
                continue
            specific, path = omitted[value]
            decisions.append(
                HierarchyDecision(
                    value,
                    "omitted",
                    specific,
                    f"Kept the more specific value {specific!r}.",
                    path,
                )
            )
        return HierarchyProjection(output, tuple(decisions), ())


def tag_endpoint(tag_id: str, label: str = "") -> RelationshipEndpoint:
    return RelationshipEndpoint("tag", str(tag_id), str(label))


_ACTIVE_EVALUATOR_CACHE: tuple[tuple, RelationshipEvaluator] | None = None


def legacy_specificity_signature(tags) -> tuple[tuple[str,str],...]:
    """Fingerprint only inline Broader Tag compatibility inputs."""
    if tags is None:return ()
    revision=getattr(tags,"_relationship_signature_revision",None)
    cached=getattr(tags,"_legacy_specificity_signature_cache",None)
    if revision is not None and cached is not None and cached[0]==revision:
        return cached[1]
    signature=tuple(sorted(
        (str(record.id),str(record.parent_id))
        for record in getattr(tags,"tags",{}).values()
        if str(getattr(record,"parent_id","") or "")
    ))
    if revision is not None:
        tags._legacy_specificity_signature_cache=(revision,signature)
    return signature


def _active_evaluator_signature(tags=None) -> tuple:
    from .paths import DEFAULTS_DIR, RELATIONSHIPS_PATH

    paths = (DEFAULTS_DIR / "relationships.json", RELATIONSHIPS_PATH)
    signature = tuple(
        (
            str(Path(path).resolve()),
            path.stat().st_mtime_ns if path.exists() else -1,
            path.stat().st_size if path.exists() else -1,
        )
        for path in paths
    )
    if tags is not None:
        signature=(*signature,legacy_specificity_signature(tags))
    return signature


def _with_compatible_legacy_specificity(registry,tags):
    if tags is None:
        return registry
    legacy = KnowledgeRelationshipRegistry.from_legacy(tags,kinds=(SPECIFICITY,))
    represented_sources = {
        (item.kind, item.source_endpoint.id)
        for item in registry.relationships
        if item.kind == SPECIFICITY
        and item.directed
        and item.source_endpoint is not None
        and item.source_endpoint.kind == "tag"
    }
    compatible_legacy = tuple(
        item
        for item in legacy.relationships
        if item.kind == SPECIFICITY
        and item.source_endpoint is not None
        and (item.kind, item.source_endpoint.id) not in represented_sources
    )
    return KnowledgeRelationshipRegistry(
        (*registry.relationships, *compatible_legacy),tags=tags,
    )


def prime_active_relationship_evaluator(tags,relationships) -> RelationshipEvaluator:
    """Publish an evaluator from the already loaded application authority."""
    global _ACTIVE_EVALUATOR_CACHE
    registry=KnowledgeRelationshipRegistry(tuple(relationships),tags=tags)
    evaluator=RelationshipEvaluator(
        _with_compatible_legacy_specificity(registry,tags),
    )
    _ACTIVE_EVALUATOR_CACHE=(_active_evaluator_signature(tags),evaluator)
    return evaluator


def active_relationship_evaluator(tags=None) -> RelationshipEvaluator:
    """Load the effective default/user relationship layers with a cheap file cache."""
    global _ACTIVE_EVALUATOR_CACHE
    from .paths import DEFAULTS_DIR, RELATIONSHIPS_PATH
    from .relationship_store import load_effective_relationship_registry

    paths = (DEFAULTS_DIR / "relationships.json", RELATIONSHIPS_PATH)
    signature = _active_evaluator_signature(tags)
    if _ACTIVE_EVALUATOR_CACHE is not None and _ACTIVE_EVALUATOR_CACHE[0] == signature:
        return _ACTIVE_EVALUATOR_CACHE[1]
    registry = load_effective_relationship_registry(
            default_path=paths[0],
            user_path=paths[1],
            tags=None,
        )
    registry=_with_compatible_legacy_specificity(registry,tags)
    evaluator = RelationshipEvaluator(registry)
    _ACTIVE_EVALUATOR_CACHE = (signature, evaluator)
    return evaluator
