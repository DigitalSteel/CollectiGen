from __future__ import annotations

from .record_layout import WeakReferenceableRecord

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from .tag_collection import normalize_tag

if TYPE_CHECKING:
    from .character_database import CharacterDatabase
    from .tag_database import TagDatabase, TagRecord


ORDER_GROUPS = (
    "rating",
    "image_style",
    "subject_count",
    "identity",
    "subject_traits",
    "outfit",
    "action",
    "scene",
    "other",
)
ORDER_GROUP_LABELS = {
    "rating": "Rating",
    "image_style": "Image style",
    "subject_count": "Subject count",
    "identity": "Character / series identity",
    "subject_traits": "Subject traits",
    "outfit": "Outfit",
    "action": "Action",
    "scene": "Scene",
    "other": "Other",
}
ORDER_GROUP_RANK = {name: index for index, name in enumerate(ORDER_GROUPS)}
ORDER_GROUP_DEFAULT_PRIORITY = {
    "rating": 900,
    "image_style": 800,
    "subject_count": 700,
    "identity": 600,
    "subject_traits": 500,
    "outfit": 400,
    "action": 300,
    "scene": 200,
    "other": 100,
}
# Numeric ranks are an internal compatibility detail.  The wider range leaves
# enough sparse space for deterministic relative ordering in very large
# categories without exposing or requiring those values in the interface.
MIN_TAG_PRIORITY = -1_000_000_000
MAX_TAG_PRIORITY = 1_000_000_000


def default_order_group(category: str) -> str:
    """Return the deterministic migration default for an existing category.

    Content sensitivity is deliberately absent: safety never changes semantic
    placement in a caption.
    """
    value = str(category or "uncategorized").strip().casefold()
    if value == "rating" or value.startswith("rating_"):
        return "rating"
    if value in {"style", "artist"} or value.startswith(("style_", "artist_")):
        return "image_style"
    if value == "image_subject_count" or value.startswith("image_subject_count_"):
        return "subject_count"
    if value in {"character_identity", "copyright_series"} or value.startswith(
        ("character_identity_", "copyright_series_")
    ):
        return "identity"
    if value in {"appearance", "expression", "species", "ears", "tail", "wings", "subject_occupation", "subject_relationship"} or value.startswith(
        ("appearance_", "expression_", "species_", "ears_", "tail_", "wings_", "subject_occupation_", "subject_relationship_")
    ):
        return "subject_traits"
    if value == "clothing" or value.startswith("clothing_"):
        return "outfit"
    if value in {"action", "pose"} or value.startswith(("action_", "pose_")):
        return "action"
    if value in {"scene", "lighting", "weather", "time"} or value.startswith(
        ("scene_", "lighting_", "weather_", "time_")
    ):
        return "scene"
    return "other"


@dataclass(slots=True)
class TagOrdering(WeakReferenceableRecord):
    group: str = "other"
    priority: int = ORDER_GROUP_DEFAULT_PRIORITY["other"]
    reviewed: bool = False
    provenance: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result=dict(self.extras)
        result.update({
            "group": self.group,
            "priority": self.priority,
            "reviewed": self.reviewed,
            "provenance": list(self.provenance),
        })
        return result

    @classmethod
    def from_dict(cls, data: Any, *, category: str) -> "TagOrdering":
        payload = data if isinstance(data, dict) else {}
        known={"group","priority","reviewed","provenance"}
        group = str(payload.get("group") or default_order_group(category))
        if group not in ORDER_GROUP_RANK:
            group = default_order_group(category)
        try:
            priority = int(payload.get("priority", ORDER_GROUP_DEFAULT_PRIORITY[group]))
        except (TypeError, ValueError):
            priority = ORDER_GROUP_DEFAULT_PRIORITY[group]
        return cls(
            group=group,
            priority=min(MAX_TAG_PRIORITY, max(MIN_TAG_PRIORITY, priority)),
            reviewed=bool(payload.get("reviewed", False)),
            provenance=[item for item in payload.get("provenance", []) if isinstance(item, dict)],
            extras={key:value for key,value in payload.items() if key not in known},
        )


@dataclass(frozen=True)
class TagOrderCandidate:
    value: str
    group_hint: str = "other"
    subject_slot: int | None = None
    identity_kind: str = ""
    sequence: int = 0
    source: str = ""
    owner_kind: str = ""
    owner_id: str = ""


@dataclass(frozen=True)
class _ResolvedCandidate:
    source_value: str
    display: str
    canonical_key: str
    group: str
    priority: int
    subject_slot: int | None
    identity_kind: str
    sequence: int
    source: str
    owner_kind: str
    owner_id: str


@dataclass(frozen=True)
class TagOrderDecision:
    """One source tag's exact result from canonical ordering."""

    source_value: str
    source: str
    action: str
    output: str = ""
    reason: str = ""
    canonical_key: str = ""


@dataclass(frozen=True)
class TagOrderResult:
    values: tuple[str, ...] = ()
    decisions: tuple[TagOrderDecision, ...] = ()


class TagOrderingService:
    """Canonicalize, deduplicate, and deterministically order caption tags."""

    def __init__(
        self,
        knowledge: TagDatabase | None = None,
        characters: CharacterDatabase | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.characters = characters
        self._identities: dict[str, tuple[str, str]] = {}
        if characters is not None:
            for record in characters.characters.values():
                for name in (record.name, *record.aliases):
                    self._identities.setdefault(normalize_tag(name), (record.name, "character"))
            for record in characters.series.values():
                for name in (record.name, *record.aliases):
                    self._identities.setdefault(normalize_tag(name), (record.name, "series"))
            for record in characters.outfits.values():
                for name in (record.name, *record.aliases):
                    self._identities.setdefault(normalize_tag(name), (record.name, "outfit"))

    def _resolve(self, candidate: TagOrderCandidate, *, canonicalize: bool) -> _ResolvedCandidate | None:
        value = candidate.value.strip()
        if not value:
            return None
        record: TagRecord | None = self.knowledge.find(value) if self.knowledge is not None else None
        identity = self._identities.get(normalize_tag(value))
        display = record.display if record is not None and canonicalize else value
        canonical_key = f"tag:{record.id}" if record is not None else normalize_tag(display)
        group = candidate.group_hint if candidate.group_hint in ORDER_GROUP_RANK else "other"
        priority = 0
        identity_kind = candidate.identity_kind
        if record is not None:
            group = record.ordering.group
            priority = record.ordering.priority
        elif identity is not None:
            canonical_name, kind = identity
            display = canonical_name if canonicalize else value
            canonical_key = f"{kind}:{normalize_tag(canonical_name)}"
            group = "outfit" if kind == "outfit" else "identity"
            identity_kind = kind
        return _ResolvedCandidate(
            source_value=value,
            display=display,
            canonical_key=canonical_key,
            group=group,
            priority=priority,
            subject_slot=candidate.subject_slot,
            identity_kind=identity_kind,
            sequence=candidate.sequence,
            source=candidate.source,
            owner_kind=candidate.owner_kind,
            owner_id=candidate.owner_id,
        )

    def canonical_key(self, value: str) -> str:
        resolved = self._resolve(TagOrderCandidate(value), canonicalize=True)
        return resolved.canonical_key if resolved is not None else ""

    @staticmethod
    def _sort_key(item: _ResolvedCandidate) -> tuple[Any, ...]:
        return (
            ORDER_GROUP_RANK[item.group],
            -item.priority,
            normalize_tag(item.display),
            item.canonical_key,
            item.subject_slot if item.subject_slot is not None else 1_000_000,
        )

    def order(
        self,
        candidates: Iterable[TagOrderCandidate],
        *,
        excluded: Iterable[str] = (),
        canonicalize: bool = True,
        deterministic: bool = True,
    ) -> list[str]:
        return list(
            self.order_with_trace(
                candidates,
                excluded=excluded,
                canonicalize=canonicalize,
                deterministic=deterministic,
            ).values
        )

    def order_with_trace(
        self,
        candidates: Iterable[TagOrderCandidate],
        *,
        excluded: Iterable[str] = (),
        canonicalize: bool = True,
        deterministic: bool = True,
    ) -> TagOrderResult:
        """Order tags and retain the exact decision for every source fact."""
        excluded_keys = {self.canonical_key(value) for value in excluded if value.strip()}
        by_key: dict[str, _ResolvedCandidate] = {}
        resolved_candidates: list[_ResolvedCandidate] = []
        decisions: list[TagOrderDecision] = []
        for candidate in candidates:
            resolved = self._resolve(candidate, canonicalize=canonicalize)
            if resolved is None:
                continue
            resolved_candidates.append(resolved)
            if resolved.canonical_key in excluded_keys:
                decisions.append(
                    TagOrderDecision(
                        source_value=candidate.value,
                        source=candidate.source,
                        action="omitted",
                        reason="This Image Knowledge fact is excluded from training projections.",
                        canonical_key=resolved.canonical_key,
                    )
                )
                continue
            current = by_key.get(resolved.canonical_key)
            if current is None or self._sort_key(resolved) < self._sort_key(current):
                by_key[resolved.canonical_key] = resolved
        values = list(by_key.values())
        if deterministic:
            values.sort(key=self._sort_key)
        else:
            values.sort(key=lambda item: item.sequence)
        selected_ids = {id(item) for item in values}
        for resolved in resolved_candidates:
            if resolved.canonical_key in excluded_keys:
                continue
            selected = by_key[resolved.canonical_key]
            if id(resolved) in selected_ids:
                canonicalized = normalize_tag(resolved.display) != normalize_tag(
                    resolved.source_value
                )
                decisions.append(
                    TagOrderDecision(
                        source_value=resolved.source_value,
                        source=resolved.source,
                        action="emitted",
                        output=resolved.display,
                        reason=(
                            "Canonicalized this alias to the reviewed SDXL Tag."
                            if canonicalized
                            else "Included by the selected training profile."
                        ),
                        canonical_key=resolved.canonical_key,
                    )
                )
            else:
                decisions.append(
                    TagOrderDecision(
                        source_value=resolved.source_value,
                        source=resolved.source,
                        action="combined",
                        output=selected.display,
                        reason="Combined duplicate sources with the same canonical SDXL Tag identity.",
                        canonical_key=resolved.canonical_key,
                    )
                )
        return TagOrderResult(
            tuple(item.display for item in values),
            tuple(decisions),
        )
