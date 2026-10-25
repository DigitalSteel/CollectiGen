from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .related_tag_suggestions import RelatedTagSuggestion


FACT_EXPANSION_POLICY_ID = "reviewed-related-facts"
FACT_EXPANSION_POLICY_NAME = "Reviewed relationship expansion"
FACT_EXPANSION_POLICY_VERSION = 3
FACT_EXPANSION_MIN_DEPTH = 1
FACT_EXPANSION_MAX_DEPTH = 3

FACT_EXPANSION_RELATIONSHIPS = (
    "base",
    "implies",
    "template_modifier",
    "character_identity",
    "character_source",
    "character_outfit",
)
FACT_EXPANSION_RELATIONSHIP_GROUPS = (
    ("Broader facts", ("base",)),
    ("Explicit implications", ("implies",)),
    ("Reviewed reusable-template modifier paths", ("template_modifier",)),
    (
        "Reviewed character, source, and outfit links",
        ("character_identity", "character_source", "character_outfit"),
    ),
)
FACT_EXPANSION_OWNER_KINDS = frozenset(
    {"image", "global", "scene", "visual_style", "custom_group", "character", "object"}
)


def clean_fact_expansion_relationships(values) -> list[str]:
    """Return known relationship kinds in stable display order."""

    selected = {
        str(value).strip()
        for value in values
        if str(value).strip() in FACT_EXPANSION_RELATIONSHIPS
    } if isinstance(values, (list, tuple, set, frozenset)) else set()
    return [
        relationship
        for relationship in FACT_EXPANSION_RELATIONSHIPS
        if relationship in selected
    ]


@dataclass(frozen=True)
class FactExpansionDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class DeterministicFactExpansionPolicy:
    """Explicit opt-in policy for staging reviewed related Image Knowledge."""

    enabled: bool = False
    relationship_kinds: tuple[str, ...] = FACT_EXPANSION_RELATIONSHIPS
    max_depth: int = FACT_EXPANSION_MIN_DEPTH
    policy_id: str = FACT_EXPANSION_POLICY_ID
    version: int = FACT_EXPANSION_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relationship_kinds",
            tuple(clean_fact_expansion_relationships(self.relationship_kinds)),
        )
        object.__setattr__(
            self,
            "max_depth",
            max(
                FACT_EXPANSION_MIN_DEPTH,
                min(FACT_EXPANSION_MAX_DEPTH, int(self.max_depth)),
            ),
        )

    @property
    def display_name(self) -> str:
        return f"{FACT_EXPANSION_POLICY_NAME} v{self.version}"

    @classmethod
    def from_settings(cls, settings) -> "DeterministicFactExpansionPolicy":
        if settings is None:
            return cls()
        return cls(
            enabled=bool(getattr(settings, "fact_expansion_enabled", False)),
            relationship_kinds=tuple(
                getattr(
                    settings,
                    "fact_expansion_relationships",
                    FACT_EXPANSION_RELATIONSHIPS,
                )
            ),
            max_depth=getattr(
                settings,
                "fact_expansion_max_depth",
                FACT_EXPANSION_MIN_DEPTH,
            ),
        )

    def evaluate(self, suggestion: RelatedTagSuggestion) -> FactExpansionDecision:
        if not self.enabled:
            return FactExpansionDecision(False, "The deterministic fact-expansion policy is off")
        if not suggestion.eligible:
            return FactExpansionDecision(
                False,
                suggestion.blocked_reason or "Blocked by an Image Knowledge decision",
            )
        if not suggestion.policy_decisions:
            return FactExpansionDecision(
                False,
                "No reviewed consumer-applicability decision was recorded",
            )
        if not suggestion.policy_permits_fact_expansion:
            return FactExpansionDecision(
                False,
                "The effective consumer-applicability policy permits review only "
                "or excludes this path",
            )
        if not suggestion.tag_id or suggestion.owner_kind not in FACT_EXPANSION_OWNER_KINDS:
            return FactExpansionDecision(False, "The exact destination is not supported")
        if not suggestion.relation_kinds:
            return FactExpansionDecision(False, "No reviewed relationship path was recorded")
        if len(suggestion.relation_kinds) > self.max_depth:
            return FactExpansionDecision(
                False,
                f"The reviewed path exceeds the configured depth of {self.max_depth}",
            )
        unsupported = [
            kind
            for kind in suggestion.relation_kinds
            if kind not in self.relationship_kinds
        ]
        if unsupported:
            return FactExpansionDecision(
                False,
                "The reviewed path uses a relationship type that is not enabled",
            )
        return FactExpansionDecision(True, self.display_name)

    def eligible(
        self,
        suggestions: Iterable[RelatedTagSuggestion],
    ) -> tuple[RelatedTagSuggestion, ...]:
        return tuple(
            suggestion
            for suggestion in suggestions
            if self.evaluate(suggestion).allowed
        )
