from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .review_queue import ReviewTask, ReviewTaskDatabase
from .tag_collection import normalize_tag


@dataclass(frozen=True)
class ReviewedKnowledgeDecision:
    """One exact reviewed Tag-meaning change that can affect saved questions."""

    task_id: str
    names: tuple[str, ...]
    suggestion: Mapping[str, Any]


@dataclass(frozen=True)
class RelatedKnowledgeRerankResult:
    """The pending Tag-meaning questions recomputed after reviewed authority."""

    task_ids: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.task_ids)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _question_names(task: ReviewTask) -> tuple[str, ...]:
    if task.task_type == "new_tag":
        value = str(task.details.get("tag") or "").strip()
        return (value,) if value else ()
    if task.task_type == "pattern_group":
        return tuple(dict.fromkeys(
            str(value).strip()
            for value in task.details.get("tags", ())
            if str(value).strip()
        ))
    return ()


def _suggestion_signature(suggestion: Mapping[str, Any]) -> tuple[Any, ...]:
    properties = suggestion.get("properties")
    property_items = tuple(sorted(
        (str(key), str(value))
        for key, value in properties.items()
    )) if isinstance(properties, Mapping) else ()
    return (
        str(suggestion.get("category") or "uncategorized"),
        str(suggestion.get("concept_id") or ""),
        property_items,
        str(suggestion.get("pattern_id") or ""),
        str(suggestion.get("parent_id") or ""),
        normalize_tag(suggestion.get("generic_tag") or ""),
    )


def _recompute_suggestion(engine, names: tuple[str, ...]) -> dict[str, Any]:
    suggestions = [dict(engine.suggest(name).__dict__) for name in names]
    if len(suggestions) == 1:
        return suggestions[0]
    signatures = {_suggestion_signature(item) for item in suggestions}
    if len(signatures) == 1:
        result = dict(suggestions[0])
        result["tag"] = ""
        result["confidence"] = min(
            float(item.get("confidence") or 0.0) for item in suggestions
        )
        return result
    return {
        "tag": "",
        "category": "uncategorized",
        "concept_id": None,
        "properties": {},
        "confidence": 0.0,
        "reason": (
            "Related reviewed Knowledge changed, and these names no longer "
            "share one deterministic interpretation. Review them individually."
        ),
        "pattern_id": "",
        "similar_tags": [],
        "parent_id": None,
        "generic_tag": "",
    }


def rerank_related_knowledge_questions(
    reviews: ReviewTaskDatabase,
    engine,
    decisions: Iterable[ReviewedKnowledgeDecision],
) -> RelatedKnowledgeRerankResult:
    """Recompute only pending questions related to exact reviewed decisions.

    Dependency keys come from reviewed pattern/concept/parent authority and the
    same bounded lexical anchors used by the suggestion engine.  The operation
    scans the saved Review queue, never an image library, and does not resolve
    or auto-approve any pending question.
    """

    decision_ids_by_key: dict[str, set[str]] = {}
    for decision in decisions:
        keys = set()
        for name in decision.names:
            keys.update(engine.dependency_keys(name, decision.suggestion))
        for key in keys:
            decision_ids_by_key.setdefault(key,set()).add(decision.task_id)
    if not decision_ids_by_key:
        return RelatedKnowledgeRerankResult()

    impacted = []
    for task in reviews.tasks.values():
        if task.status != "pending" or task.task_type not in {
            "new_tag", "pattern_group",
        }:
            continue
        names = _question_names(task)
        if not names:
            continue
        cached = task.details.get("suggestion")
        cached = cached if isinstance(cached, Mapping) else {}
        task_keys = set()
        for name in names:
            task_keys.update(engine.dependency_keys(name, cached))
        matching_keys=sorted(task_keys.intersection(decision_ids_by_key))
        if not matching_keys:
            continue
        suggestion = _recompute_suggestion(engine, names)
        impacted.append((
            matching_keys[0],
            -float(suggestion.get("confidence") or 0.0),
            -int(task.details.get("seen_count") or 0),
            normalize_tag(names[0]),
            task.id,
            task,
            suggestion,
            tuple(sorted(set().union(*(
                decision_ids_by_key[key] for key in matching_keys
            )))),
        ))

    grouped: dict[str, list[tuple]] = {}
    for row in impacted:
        grouped.setdefault(row[0], []).append(row)
    changed_at = _now()
    changed_ids = []
    for family_key in sorted(grouped):
        rows = sorted(grouped[family_key], key=lambda row: row[:5])
        total = len(rows)
        for rank, row in enumerate(rows, 1):
            task, suggestion, basis_task_ids = row[5], row[6], row[7]
            previous = task.details.get("suggestion")
            task.details["suggestion"] = suggestion
            task.details["related_rerank"] = {
                "family_key": family_key,
                "rank": rank,
                "total_in_family": total,
                "basis_task_ids": list(basis_task_ids),
                "suggestion_changed": previous != suggestion,
                "reranked_at": changed_at,
            }
            task.updated_at = changed_at
            changed_ids.append(task.id)
    return RelatedKnowledgeRerankResult(tuple(changed_ids))


__all__ = [
    "RelatedKnowledgeRerankResult",
    "ReviewedKnowledgeDecision",
    "rerank_related_knowledge_questions",
]
