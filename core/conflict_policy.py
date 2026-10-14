from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Iterable, Mapping

from .tag_collection import normalize_tag


ALLOW_ANY = "any"
ALLOW_ALL = "all"
ALLOW_LOGICS = frozenset((ALLOW_ANY, ALLOW_ALL))

REVIEWED_EXCEPTION_TAGS = "reviewed_exception_tags"
CONFIRMED_OWNER_SEPARATION = "confirmed_owner_separation"
SUBJECT_COUNT = "subject_count"
ALLOW_CONDITION_KINDS = frozenset(
    (REVIEWED_EXCEPTION_TAGS, CONFIRMED_OWNER_SEPARATION, SUBJECT_COUNT)
)

SUBJECT_MATCHING_SCHEMA_VERSION = 1
_SUBJECT_MATCHING_KEYS = frozenset(
    ("schema_version", "single_tags", "count_tags", "multiple_tags")
)
DEFAULT_SINGLE_SUBJECT_TAGS = ("solo",)
DEFAULT_SINGLE_SUBJECT_COUNT_TAGS = ("1girl", "1boy", "1other")
DEFAULT_MULTIPLE_SUBJECT_TAGS = (
    "2girls", "3girls", "4girls", "2boys", "3boys", "4boys",
    "multiple girls", "multiple boys", "multiple characters", "group",
)


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            text
            for value in values
            if (text := str(value or "").strip())
        )
    )


def _normalized_literal_tags(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of exact tag names.")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{label} must contain only exact tag names.")
        normalized = normalize_tag(item)
        if not normalized:
            raise ValueError(f"{label} cannot contain blank tag names.")
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True)
class ConflictSubjectPolicy:
    """Exact tag clues used only for unassigned same-character fallback."""

    single_tags: tuple[str, ...] = DEFAULT_SINGLE_SUBJECT_TAGS
    count_tags: tuple[str, ...] = DEFAULT_SINGLE_SUBJECT_COUNT_TAGS
    multiple_tags: tuple[str, ...] = DEFAULT_MULTIPLE_SUBJECT_TAGS

    def __post_init__(self) -> None:
        single = _normalized_literal_tags(self.single_tags, "Single-subject tags")
        count = _normalized_literal_tags(self.count_tags, "Single-subject count tags")
        multiple = _normalized_literal_tags(self.multiple_tags, "Multiple-subject tags")
        values = (*single, *count, *multiple)
        if len(values) != len(set(values)):
            raise ValueError("Subject matching tag lists cannot overlap.")
        object.__setattr__(self, "single_tags", single)
        object.__setattr__(self, "count_tags", count)
        object.__setattr__(self, "multiple_tags", multiple)

    @classmethod
    def from_payload(cls, value: Any) -> "ConflictSubjectPolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("Subject matching must be one structured object.")
        unknown = set(value).difference(_SUBJECT_MATCHING_KEYS)
        if unknown:
            raise ValueError(
                "Unsupported subject matching field: "
                + ", ".join(sorted(str(item) for item in unknown))
                + "."
            )
        missing = _SUBJECT_MATCHING_KEYS.difference(value)
        if missing:
            raise ValueError(
                "Subject matching is missing required field: "
                + ", ".join(sorted(missing))
                + "."
            )
        version = value["schema_version"]
        if type(version) is not int or version != SUBJECT_MATCHING_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported subject matching schema version: {version!r}."
            )
        return cls(
            single_tags=_normalized_literal_tags(
                value["single_tags"], "Single-subject tags"
            ),
            count_tags=_normalized_literal_tags(
                value["count_tags"], "Single-subject count tags"
            ),
            multiple_tags=_normalized_literal_tags(
                value["multiple_tags"], "Multiple-subject tags"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SUBJECT_MATCHING_SCHEMA_VERSION,
            "single_tags": list(self.single_tags),
            "count_tags": list(self.count_tags),
            "multiple_tags": list(self.multiple_tags),
        }


def conflict_rule_cache_payload(rule, *, structured=False, explicit_defaults=False):
    """Preserve receipts from before subject clues became editable.

    This literal describes the historical evaluator, deliberately independent
    of today's defaults. A future default change must remain a cache change.
    ``explicit_defaults`` reconstructs the briefly shipped expanded receipts.
    Unknown fields and every custom policy remain part of the contract.
    """
    payload = asdict(rule) if structured and is_dataclass(rule) else dict(vars(rule))
    policy = getattr(rule, "subject_matching", None)
    if not explicit_defaults and isinstance(policy, ConflictSubjectPolicy) and policy.to_dict() == {
        "schema_version": 1,
        "single_tags": ["solo"],
        "count_tags": ["1girl", "1boy", "1other"],
        "multiple_tags": ["2girls", "3girls", "4girls", "2boys", "3boys", "4boys",
                          "multiple girls", "multiple boys", "multiple characters", "group"],
    }:
        payload.pop("subject_matching", None)
    return payload


def validate_unique_conflict_pairs(
    relationship_id: str,
    member_ids: Iterable[Any],
    existing_families: Iterable[tuple[str, Iterable[Any]]],
) -> None:
    """Reject active canonical families that own the same symmetric Tag pair."""

    relationship_id = str(relationship_id or "").strip()
    members = sorted(_unique_strings(member_ids))
    candidate_pairs = {
        (source, target)
        for index, source in enumerate(members)
        for target in members[index + 1 :]
    }
    for existing_id, existing_members in existing_families:
        existing_id = str(existing_id or "").strip()
        if existing_id == relationship_id:
            continue
        values = sorted(_unique_strings(existing_members))
        overlap = candidate_pairs.intersection(
            (source, target)
            for index, source in enumerate(values)
            for target in values[index + 1 :]
        )
        if not overlap:
            continue
        source, target = sorted(overlap)[0]
        raise ValueError(
            f"Conflict rules {existing_id!r} and {relationship_id!r} both own "
            f"the same symmetric Tag pair {source!r} / {target!r}."
        )


@dataclass(frozen=True)
class ConflictAllowCondition:
    """One reviewed, structured exception to a diagnostic conflict rule."""

    kind: str
    tag_ids: tuple[str, ...] = ()
    tag_logic: str = ALLOW_ANY
    minimum_subjects: int | None = None
    maximum_subjects: int | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConflictAllowCondition":
        if not isinstance(value, Mapping):
            raise ValueError("Every Allow when condition must be one object.")
        kind = str(value.get("kind") or "").strip()
        if kind not in ALLOW_CONDITION_KINDS:
            raise ValueError(f"Unsupported Allow when condition: {kind or 'missing kind'}.")
        if kind == REVIEWED_EXCEPTION_TAGS:
            raw_ids = value.get("tag_ids", ())
            if not isinstance(raw_ids, (list, tuple)):
                raise ValueError("Reviewed exception Tags must be a stable-ID list.")
            tag_ids = _unique_strings(raw_ids)
            if not tag_ids:
                raise ValueError("Reviewed exception Tags must name at least one stable ID.")
            tag_logic = str(value.get("logic") or ALLOW_ANY).strip().casefold()
            if tag_logic not in ALLOW_LOGICS:
                raise ValueError("Reviewed exception Tags must use Any or All matching.")
            return cls(kind, tag_ids=tag_ids, tag_logic=tag_logic)
        if kind == CONFIRMED_OWNER_SEPARATION:
            return cls(kind)

        def optional_bound(name: str) -> int | None:
            raw = value.get(name)
            if raw in (None, ""):
                return None
            try:
                result = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Subject-count bounds must be whole numbers.") from exc
            if result < 1:
                raise ValueError("Subject-count bounds must be at least 1.")
            return result

        minimum = optional_bound("minimum")
        maximum = optional_bound("maximum")
        if minimum is None and maximum is None:
            raise ValueError("A subject-count condition needs a minimum or maximum.")
        if minimum is not None and maximum is not None and maximum < minimum:
            raise ValueError("The maximum subject count cannot be below the minimum.")
        return cls(
            kind,
            minimum_subjects=minimum,
            maximum_subjects=maximum,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.kind == REVIEWED_EXCEPTION_TAGS:
            result.update({"logic": self.tag_logic, "tag_ids": list(self.tag_ids)})
        elif self.kind == SUBJECT_COUNT:
            if self.minimum_subjects is not None:
                result["minimum"] = self.minimum_subjects
            if self.maximum_subjects is not None:
                result["maximum"] = self.maximum_subjects
        return result


@dataclass(frozen=True)
class ConflictAllowPolicy:
    """Explicit Any/All semantics for reviewed diagnostic-rule exceptions."""

    logic: str = ALLOW_ANY
    conditions: tuple[ConflictAllowCondition, ...] = ()

    @classmethod
    def from_payload(cls, value: Any) -> "ConflictAllowPolicy":
        if value in (None, {}, []):
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("Allow when must be one structured object.")
        logic = str(value.get("logic") or ALLOW_ANY).strip().casefold()
        if logic not in ALLOW_LOGICS:
            raise ValueError("Allow when must use explicit Any or All semantics.")
        raw_conditions = value.get("conditions", ())
        if not isinstance(raw_conditions, (list, tuple)):
            raise ValueError("Allow when conditions must be a list.")
        conditions = tuple(
            ConflictAllowCondition.from_dict(item) for item in raw_conditions
        )
        kinds = [item.kind for item in conditions]
        if len(kinds) != len(set(kinds)):
            raise ValueError("Each Allow when condition type may appear only once.")
        return cls(logic, conditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logic": self.logic,
            "conditions": [condition.to_dict() for condition in self.conditions],
        }

    def condition(self, kind: str) -> ConflictAllowCondition | None:
        return next((item for item in self.conditions if item.kind == kind), None)

    def summary(self, display_name=None) -> str:
        if not self.conditions:
            return "No automatic exceptions"
        labels = []
        for condition in self.conditions:
            if condition.kind == REVIEWED_EXCEPTION_TAGS:
                values = [
                    str(display_name(tag_id) if callable(display_name) else tag_id)
                    for tag_id in condition.tag_ids
                ]
                labels.append(
                    f"{condition.tag_logic.title()} exception Tag"
                    f"{'s' if len(values) != 1 else ''}: {', '.join(values)}"
                )
            elif condition.kind == CONFIRMED_OWNER_SEPARATION:
                labels.append("confirmed separate owners")
            else:
                low = condition.minimum_subjects
                high = condition.maximum_subjects
                if low is not None and high is not None:
                    count = str(low) if low == high else f"{low}–{high}"
                elif low is not None:
                    count = f"{low}+"
                else:
                    count = f"up to {high}"
                labels.append(f"subject count {count}")
        return f"{self.logic.title()}: " + "; ".join(labels)
