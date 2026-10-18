from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .consumer_policies import (
    CANONICALIZE_IDENTITY,
    CLASSIFY_POLICY,
    DISCOVER_RELATED,
    ORGANIZE_ONLY,
    SEARCH_ORGANIZATION,
    ConsumerPolicyDecision,
    ConsumerPolicyResolver,
)
from .knowledge_relationships import (
    ADVISORY_ASSOCIATION,
    ENTAILMENT,
    IDENTITY_ALIAS,
    MODIFIER_VALUE,
    SPECIFICITY,
    KnowledgeRelationship,
)
from .paths import CONSUMER_POLICIES_PATH, DEFAULTS_DIR, RELATIONSHIPS_PATH
from .relationship_workspace import RelationshipWorkspaceService
from .tag_collection import normalize_tag
from .tag_database import TagDatabase


SEARCH_RELATIONSHIP_KINDS = frozenset(
    {IDENTITY_ALIAS, SPECIFICITY, ENTAILMENT, MODIFIER_VALUE, ADVISORY_ASSOCIATION}
)
SEARCH_POLICY_MODES = frozenset(
    {CANONICALIZE_IDENTITY, DISCOVER_RELATED, ORGANIZE_ONLY, CLASSIFY_POLICY}
)


@dataclass(frozen=True)
class PolicyAwareSearchMatch:
    tag_id: str
    display: str
    matched_text: str
    path_ids: tuple[str, ...]
    path_displays: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    relationship_kinds: tuple[str, ...]
    relationship_labels: tuple[str, ...]
    scopes: tuple[str, ...]
    decisions: tuple[ConsumerPolicyDecision, ...]

    @property
    def relationship_label(self) -> str:
        return " → ".join(self.relationship_labels) or "Preferred name"

    @property
    def path_label(self) -> str:
        return " → ".join(self.path_displays)

    @property
    def scope_label(self) -> str:
        return " → ".join(dict.fromkeys(self.scopes)) or "identity"

    @property
    def effective_use_label(self) -> str:
        modes = tuple(dict.fromkeys(decision.mode for decision in self.decisions))
        return " → ".join(mode.replace("_", " ").title() for mode in modes)

    @property
    def explanation(self) -> str:
        lines = [
            f"Path: {self.path_label}",
            f"Relationship: {self.relationship_label}",
            f"Scope: {self.scope_label}",
        ]
        for decision in self.decisions:
            lines.extend(decision.explanation_path)
            lines.append(f"Effective use: {decision.mode} — {decision.explanation}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _SearchEdge:
    source_id: str
    target_id: str
    relationship_id: str
    relationship_kind: str
    label: str
    scope: str


class ConsumerPolicySearchService:
    """Bounded, explicit relationship discovery for Tag Encyclopedia search."""

    DEFAULT_MAX_RESULTS = 200
    DEFAULT_MAX_DEPTH = 2
    DEFAULT_MAX_SEEDS = 200

    def __init__(
        self,
        knowledge: TagDatabase,
        relationships: Iterable[KnowledgeRelationship],
        resolver: ConsumerPolicyResolver,
    ) -> None:
        self.knowledge = knowledge
        self.resolver = resolver
        self.relationships = tuple(relationships)
        self._adjacency: dict[str, list[_SearchEdge]] = {}
        self._alias_edges: dict[tuple[str, str], KnowledgeRelationship] = {}
        for relationship in self.relationships:
            if (
                relationship.kind not in SEARCH_RELATIONSHIP_KINDS
                or relationship.disabled
                or not relationship.reviewed
            ):
                continue
            tag_endpoints = tuple(
                endpoint for endpoint in relationship.endpoints if endpoint.kind == "tag"
            )
            if relationship.kind == IDENTITY_ALIAS:
                aliases = tuple(
                    endpoint
                    for endpoint in relationship.endpoints
                    if endpoint.kind == "alias_text"
                )
                for alias in aliases:
                    for tag in tag_endpoints:
                        self._alias_edges.setdefault(
                            (normalize_tag(alias.label or alias.id), tag.id), relationship
                        )
                continue
            if len(tag_endpoints) < 2:
                continue
            if relationship.kind in {SPECIFICITY, ENTAILMENT} and relationship.directed:
                source = relationship.source_endpoint
                if source is None or source.kind != "tag":
                    continue
                for target in relationship.target_endpoints:
                    if target.kind != "tag":
                        continue
                    if relationship.kind == SPECIFICITY:
                        self._add_edge(relationship, source.id, target.id, "Broader")
                        self._add_edge(relationship, target.id, source.id, "Narrower")
                    else:
                        self._add_edge(relationship, source.id, target.id, "Also Means")
                        self._add_edge(
                            relationship, target.id, source.id, "Entailed By"
                        )
                continue
            label = (
                "Composition"
                if relationship.kind == MODIFIER_VALUE
                else "Usually Related"
            )
            for source in tag_endpoints:
                for target in tag_endpoints:
                    if source.id != target.id:
                        self._add_edge(relationship, source.id, target.id, label)
        for edges in self._adjacency.values():
            edges.sort(
                key=lambda edge: (
                    edge.relationship_kind,
                    edge.target_id,
                    edge.relationship_id,
                )
            )

    @classmethod
    def from_paths(
        cls,
        knowledge: TagDatabase,
        *,
        default_relationships: str | Path = DEFAULTS_DIR / "relationships.json",
        user_relationships: str | Path = RELATIONSHIPS_PATH,
        default_policies: str | Path = DEFAULTS_DIR / "consumer_policies.json",
        user_policies: str | Path = CONSUMER_POLICIES_PATH,
    ) -> "ConsumerPolicySearchService":
        workspace = RelationshipWorkspaceService(
            default_relationships, user_relationships, tags=knowledge
        )
        resolver = ConsumerPolicyResolver.from_paths(default_policies, user_policies)
        return cls(knowledge, workspace.effective, resolver)

    def _add_edge(
        self,
        relationship: KnowledgeRelationship,
        source_id: str,
        target_id: str,
        label: str,
    ) -> None:
        if source_id not in self.knowledge.tags or target_id not in self.knowledge.tags:
            return
        self._adjacency.setdefault(source_id, []).append(
            _SearchEdge(
                source_id,
                target_id,
                relationship.id,
                relationship.kind,
                label,
                relationship.scope or "any",
            )
        )

    def _decision(self, edge: _SearchEdge) -> ConsumerPolicyDecision:
        return self.resolver.resolve(
            SEARCH_ORGANIZATION,
            relationship_id=edge.relationship_id,
            tag_id=edge.target_id,
            relationship_kind=edge.relationship_kind,
            context={"scope": edge.scope, "direction": edge.label.casefold()},
        )

    def _direct_seeds(self, query: str) -> tuple[tuple[str, str], ...]:
        key = normalize_tag(query)
        if not key:
            return ()
        matches = []
        for record in sorted(
            self.knowledge.tags.values(), key=lambda item: (item.display.casefold(), item.id)
        ):
            names = tuple(
                self.knowledge.identity_names(record)
                if hasattr(self.knowledge, "identity_names")
                else (record.display, *record.aliases)
            )
            matched = next(
                (str(value) for value in names if key in normalize_tag(str(value))), ""
            )
            if matched:
                matches.append((record.id, matched))
                if len(matches) >= self.DEFAULT_MAX_SEEDS:
                    break
        return tuple(matches)

    def search(
        self,
        query: str,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> tuple[PolicyAwareSearchMatch, ...]:
        max_results = max(1, min(int(max_results), self.DEFAULT_MAX_RESULTS))
        max_depth = max(1, min(int(max_depth), self.DEFAULT_MAX_DEPTH))
        seeds = self._direct_seeds(query)
        if not seeds:
            return ()
        seed_ids = {tag_id for tag_id, _matched in seeds}
        results: dict[str, PolicyAwareSearchMatch] = {}
        queue = deque()
        visited_depth: dict[str, int] = {}
        for tag_id, matched in seeds:
            record = self.knowledge.tags[tag_id]
            alias_key = normalize_tag(matched)
            if alias_key != normalize_tag(record.display):
                relationship = self._alias_edges.get((alias_key, tag_id))
                relationship_id = relationship.id if relationship is not None else ""
                decision = self.resolver.resolve(
                    SEARCH_ORGANIZATION,
                    relationship_id=relationship_id,
                    tag_id=tag_id,
                    relationship_kind=IDENTITY_ALIAS,
                    context={"scope": "identity", "matched_alias": alias_key},
                )
                if not decision.failed_closed and decision.mode in SEARCH_POLICY_MODES:
                    results[tag_id] = PolicyAwareSearchMatch(
                        tag_id,
                        record.display,
                        matched,
                        (tag_id,),
                        (matched, record.display),
                        (relationship_id,) if relationship_id else (),
                        (IDENTITY_ALIAS,),
                        ("Other Name",),
                        ("identity",),
                        (decision,),
                    )
            queue.append(
                (
                    tag_id,
                    0,
                    (tag_id,),
                    (record.display,),
                    (),
                    (),
                    (),
                    (),
                    (),
                )
            )
            visited_depth[tag_id] = 0
        while queue and len(results) < max_results:
            (
                current_id,
                depth,
                path_ids,
                path_displays,
                relationship_ids,
                relationship_kinds,
                relationship_labels,
                scopes,
                decisions,
            ) = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self._adjacency.get(current_id, ()):
                if edge.target_id in path_ids:
                    continue
                decision = self._decision(edge)
                if decision.failed_closed or decision.mode not in SEARCH_POLICY_MODES:
                    continue
                target = self.knowledge.tags[edge.target_id]
                next_depth = depth + 1
                next_path_ids = (*path_ids, edge.target_id)
                next_displays = (*path_displays, target.display)
                next_relationship_ids = (*relationship_ids, edge.relationship_id)
                next_kinds = (*relationship_kinds, edge.relationship_kind)
                next_labels = (*relationship_labels, edge.label)
                next_scopes = (*scopes, edge.scope)
                next_decisions = (*decisions, decision)
                if edge.target_id not in seed_ids and edge.target_id not in results:
                    results[edge.target_id] = PolicyAwareSearchMatch(
                        edge.target_id,
                        target.display,
                        query,
                        next_path_ids,
                        next_displays,
                        next_relationship_ids,
                        next_kinds,
                        next_labels,
                        next_scopes,
                        next_decisions,
                    )
                    if len(results) >= max_results:
                        break
                previous_depth = visited_depth.get(edge.target_id)
                if previous_depth is None or next_depth < previous_depth:
                    visited_depth[edge.target_id] = next_depth
                    queue.append(
                        (
                            edge.target_id,
                            next_depth,
                            next_path_ids,
                            next_displays,
                            next_relationship_ids,
                            next_kinds,
                            next_labels,
                            next_scopes,
                            next_decisions,
                        )
                    )
        return tuple(
            sorted(
                results.values(),
                key=lambda item: (
                    len(item.relationship_ids),
                    item.display.casefold(),
                    item.tag_id,
                ),
            )[:max_results]
        )
