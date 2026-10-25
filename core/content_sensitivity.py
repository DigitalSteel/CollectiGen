from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .annotation import ImageAnnotation
from .tag_collection import normalize_tag
from .tag_database import CONTENT_RATINGS, TagDatabase, TagRecord


RATING_ORDER = {rating: index for index, rating in enumerate(CONTENT_RATINGS)}


@dataclass(frozen=True)
class RatingConflictDetail:
    """Structured explanation of one image's confirmed rating contradiction."""

    declared_ratings: tuple[str, ...]
    strongest_declared: str
    implied_rating: str
    implied_tags: tuple[tuple[str, str], ...]
    messages: tuple[str, ...]

    @property
    def required_rating(self) -> str:
        candidates=(self.strongest_declared,self.implied_rating)
        return max(
            (value for value in candidates if value in RATING_ORDER),
            key=RATING_ORDER.get,
            default="",
        )


def sensitivity_dependency_groups(
    knowledge: TagDatabase,
    categories,
    *,
    tag_ids: Iterable[str] = (),
    category_ids: Iterable[str] = (),
    identity_groups: Iterable[Iterable[str]] = (),
) -> tuple[tuple[str, ...], ...]:
    """Return normalized evidence groups whose effective rating may have changed.

    Direct tag changes affect only that reusable identity. Category sensitivity
    and structure changes also affect members of the selected category subtree.
    Explicit identity groups preserve old aliases or removed identities that no
    longer resolve through the current System Knowledge database.
    """
    groups: set[tuple[str, ...]] = set()

    def add_group(values: Iterable[str]) -> None:
        normalized_values={normalize_tag(value) for value in values}
        normalized=tuple(sorted(value for value in normalized_values if value))
        if normalized:groups.add(normalized)

    for values in identity_groups:
        add_group((values,) if isinstance(values,str) else values)

    selected_tag_ids: set[str] = set()
    for stable_id in tag_ids:
        resolved=knowledge.resolve_tag_id(str(stable_id))
        if resolved:selected_tag_ids.add(resolved)

    selected_categories={str(value) for value in category_ids if str(value)}
    pending=list(selected_categories)
    while pending:
        parent_id=pending.pop()
        for child in categories.children_of(parent_id):
            if child.id in selected_categories:continue
            selected_categories.add(child.id); pending.append(child.id)

    if selected_categories:
        selected_tag_ids.update(
            record.id for record in knowledge.tags.values()
            if record.category in selected_categories
        )
    for stable_id in selected_tag_ids:
        record=knowledge.tags.get(stable_id)
        if record is not None:add_group(knowledge.identity_names(record))
    return tuple(sorted(groups))


def effective_tag_sensitivity(record, categories=None) -> tuple[str, str | None, str]:
    """Return effective rating, owning category, and resolution kind."""
    sensitivity=record.content_sensitivity
    if sensitivity.classification_status=="reviewed" and sensitivity.minimum_rating in RATING_ORDER:
        return sensitivity.minimum_rating,None,"explicit"
    if sensitivity.classification_status=="intentionally_unclassified":
        return "",None,"explicit_none"
    if categories is not None and hasattr(categories,"effective_sensitivity"):
        candidate,source,_=categories.effective_sensitivity(record.category)
        if candidate in RATING_ORDER:return candidate,source,"inherited"
    return "",None,"none"


def _reviewed_record_rating(record, categories=None) -> str:
    return effective_tag_sensitivity(record,categories)[0]


def effective_rating(annotation: ImageAnnotation, knowledge: TagDatabase, categories=None) -> str:
    """Return the strongest reviewed rating supported by confirmed image tags."""
    rating = "general"
    for tag in annotation.all_structured_tags():
        normalized=" ".join(tag.casefold().replace("_"," ").split())
        # The four rating tags are themselves confirmed image facts. They do
        # not require a separate vocabulary record before gallery safety can
        # honor them.
        if normalized in RATING_ORDER:
            if RATING_ORDER[normalized]>RATING_ORDER[rating]: rating=normalized
            continue
        record = knowledge.find(tag)
        if not record:
            continue
        candidate = _reviewed_record_rating(record,categories)
        if candidate not in RATING_ORDER:continue
        if RATING_ORDER[candidate] > RATING_ORDER[rating]:
            rating = candidate
    return rating


def protective_display_rating(
    tag_sources: dict[str, list[str]], knowledge: TagDatabase, categories=None,
) -> tuple[str, str]:
    """Return a conservative gallery-only rating and its authority basis.

    Confirmed Image Knowledge may use reviewed System Knowledge sensitivity.
    Unconfirmed evidence is intentionally narrower: only an exact current TXT
    or Metadata rating tag may protect a preview before review.  AI predictions
    and semantic guesses never acquire rating authority through this path.
    """
    rating = "unknown"
    basis = "No dependable rating yet"
    for tag, raw_sources in tag_sources.items():
        sources = [str(value) for value in raw_sources if str(value).strip()]
        confirmed = "Confirmed Image Knowledge" in sources
        portable_source = any(
            ("metadata" in source.casefold() or "txt" in source.casefold())
            and not source.casefold().startswith("ai:")
            for source in sources
        )
        normalized = " ".join(str(tag).casefold().replace("_", " ").split())
        candidate = ""
        candidate_basis = ""
        if normalized in RATING_ORDER and (confirmed or portable_source):
            candidate = normalized
            candidate_basis = (
                "Confirmed Image Knowledge" if confirmed else
                "Unconfirmed current TXT or Metadata evidence"
            )
        elif confirmed:
            record = knowledge.find(tag)
            if record is not None:
                reviewed=_reviewed_record_rating(record,categories)
                if reviewed in RATING_ORDER:
                    candidate = reviewed
                    candidate_basis = "Confirmed Image Knowledge with reviewed System Knowledge"
        if candidate and (
            rating == "unknown" or RATING_ORDER[candidate] > RATING_ORDER[rating]
        ):
            rating = candidate
            basis = candidate_basis
    return rating, basis


def rating_conflict_detail(
    annotation: ImageAnnotation,
    knowledge: TagDatabase,
    categories=None,
) -> RatingConflictDetail | None:
    """Return the values and reviewed rules behind a rating contradiction."""
    declared=set(); implied="general"; implied_tags=[]
    for tag in annotation.all_structured_tags():
        normalized=" ".join(tag.casefold().replace("_"," ").split()); record=knowledge.find(tag)
        if normalized in RATING_ORDER and (record is None or record.category=="rating"):
            declared.add(normalized); continue
        if not record: continue
        candidate=_reviewed_record_rating(record,categories)
        if candidate in RATING_ORDER:
            implied_tags.append((record.display,candidate))
            if RATING_ORDER[candidate]>RATING_ORDER[implied]: implied=candidate
    conflicts=[]
    if len(declared)>1: conflicts.append("Multiple confirmed rating tags disagree: "+", ".join(sorted(declared,key=RATING_ORDER.get)))
    strongest=max(declared,key=RATING_ORDER.get) if declared else ""
    if declared:
        if RATING_ORDER[implied]>RATING_ORDER[strongest]: conflicts.append(f"Confirmed rating is {strongest}, but reviewed tags imply at least {implied}")
    if not conflicts:
        return None
    stronger_tags=tuple(dict.fromkeys(
        (display,rating) for display,rating in implied_tags
        if not strongest or RATING_ORDER[rating]>RATING_ORDER[strongest]
    ))
    return RatingConflictDetail(
        tuple(sorted(declared,key=RATING_ORDER.get)),strongest,implied,
        stronger_tags,tuple(conflicts),
    )


def rating_conflicts(annotation: ImageAnnotation, knowledge: TagDatabase, categories=None) -> list[str]:
    """Describe confirmed rating tags that disagree with stronger reviewed tag evidence."""
    detail=rating_conflict_detail(annotation,knowledge,categories)
    return list(detail.messages) if detail is not None else []


@dataclass(frozen=True)
class SensitivityHealthIssue:
    code: str
    tag_id: str
    display: str
    detail: str


@dataclass
class SensitivityHealthReport:
    obvious_unclassified: list[SensitivityHealthIssue] = field(default_factory=list)
    conflicting: list[SensitivityHealthIssue] = field(default_factory=list)
    low_confidence: list[SensitivityHealthIssue] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.obvious_unclassified) + len(self.conflicting) + len(self.low_confidence)


def reviewed_sensitivity_proposals(paths: Iterable[str | Path]) -> dict[str, tuple[str, list[str]]]:
    proposals={}
    for value in paths:
        path=Path(value)
        try: payload=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError,TypeError): continue
        for item in payload.get("tags",[]):
            if not isinstance(item,dict): continue
            sensitivity=item.get("content_sensitivity") if isinstance(item.get("content_sensitivity"),dict) else {}
            rating=str(sensitivity.get("minimum_rating") or "")
            if sensitivity.get("classification_status")=="reviewed" and rating in RATING_ORDER:
                key=" ".join(str(item.get("display") or "").casefold().replace("_"," ").split())
                if key: proposals[key]=(rating,[str(flag) for flag in sensitivity.get("content_flags",[]) if str(flag).strip()])
    return proposals


@dataclass
class SensitivityDecisionResult:
    changed: int = 0
    skipped: int = 0
    backup: Path | None = None


def apply_sensitivity_decisions(database: TagDatabase, tag_ids: Iterable[str], action: str,
                                proposals: dict[str, tuple[str, list[str]]], *, backup: bool=True) -> SensitivityDecisionResult:
    result=SensitivityDecisionResult(); chosen=list(dict.fromkeys(str(value) for value in tag_ids))
    if backup and chosen and database.path.exists():
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S"); target=database.path.parent/"backups"/f"tags_before_sensitivity_review_{stamp}.json"
        target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(database.path,target); result.backup=target
    for tag_id in chosen:
        record=database.tags.get(tag_id)
        if not record: result.skipped+=1; continue
        sensitivity=record.content_sensitivity; key=" ".join(record.display.casefold().replace("_"," ").split()); proposal=proposals.get(key)
        if action=="use_default" and proposal:
            sensitivity.minimum_rating=proposal[0]; sensitivity.content_flags=list(proposal[1]); sensitivity.classification_status="reviewed"; sensitivity.classification_confidence=1.0
        elif action=="keep_current" and sensitivity.minimum_rating in RATING_ORDER:
            sensitivity.classification_status="reviewed"; sensitivity.classification_confidence=1.0
        elif action=="intentionally_unclassified":
            sensitivity.minimum_rating=""; sensitivity.content_flags=[]; sensitivity.classification_status="intentionally_unclassified"; sensitivity.classification_confidence=1.0
        else: result.skipped+=1; continue
        sensitivity.provenance.append({"source":"qt_knowledge_health","decision":action,"reviewed":True}); result.changed+=1
    if result.changed: database.save()
    return result


def sensitivity_health(
    records: Iterable[TagRecord],
    reviewed_proposals: dict[str, tuple[str, list[str]]],
    *,
    confidence_floor: float = 0.9,
) -> SensitivityHealthReport:
    """Audit reusable classifications without treating ambiguous words as evidence."""
    report = SensitivityHealthReport()
    for record in records:
        sensitivity = record.content_sensitivity
        key = " ".join(record.display.casefold().replace("_", " ").split())
        proposal = reviewed_proposals.get(key)
        if proposal and sensitivity.classification_status == "unclassified":
            report.obvious_unclassified.append(SensitivityHealthIssue(
                "obvious_unclassified", record.id, record.display,
                f"Reviewed default proposal: {proposal[0]}",
            ))
        if sensitivity.minimum_rating in RATING_ORDER and sensitivity.classification_confidence < confidence_floor:
            report.low_confidence.append(SensitivityHealthIssue(
                "low_confidence", record.id, record.display,
                f"{sensitivity.classification_status.replace('_',' ').title()} confidence is {sensitivity.classification_confidence:.0%}",
            ))
        if sensitivity.classification_status == "reviewed":
            if sensitivity.minimum_rating not in RATING_ORDER:
                report.conflicting.append(SensitivityHealthIssue(
                    "reviewed_without_rating", record.id, record.display,
                    "Marked reviewed but has no valid minimum rating",
                ))
        elif sensitivity.minimum_rating:
            report.conflicting.append(SensitivityHealthIssue(
                "unreviewed_rating", record.id, record.display,
                f"{sensitivity.minimum_rating.title()} is present but not reviewed",
            ))
        if proposal and sensitivity.minimum_rating in RATING_ORDER and proposal[0] in RATING_ORDER:
            if sensitivity.classification_status == "reviewed" and sensitivity.minimum_rating != proposal[0]:
                report.conflicting.append(SensitivityHealthIssue(
                    "reviewed_default_conflict", record.id, record.display,
                    f"Saved {sensitivity.minimum_rating}; reviewed default proposes {proposal[0]}",
                ))
    return report
