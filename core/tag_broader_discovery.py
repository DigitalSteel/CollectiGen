from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .category_registry import CategoryRegistry
from .knowledge_engine import SuggestionEngine
from .knowledge_relationships import SPECIFICITY
from .related_knowledge_rerank import (
    ReviewedKnowledgeDecision,
    rerank_related_knowledge_questions,
)
from .relationship_workspace import RelationshipWorkspaceService
from .review_queue import ReviewTask, ReviewTaskDatabase
from .tag_collection import normalize_tag
from .tag_database import TagDatabase, TagRecord
from .tag_semantic_relationships import (
    TagCategoryAssignment,
    TagSemanticChange,
    TagSemanticRelationshipService,
    effective_tag_semantic_links_from_index,
    tag_semantic_relationship_index,
)


TAG_BROADER_PROPOSAL_TASK_TYPE = "tag_broader_proposal"
MAX_BROADER_DISCOVERY_TAGS = 20_000
MAX_BROADER_DISCOVERY_CANDIDATES = 1_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _authority_fingerprint(paths: Iterable[Path]) -> str:
    return _fingerprint(tuple(
        (str(path.resolve(strict=False)), _path_fingerprint(path))
        for path in dict.fromkeys(Path(value) for value in paths)
    ))


@dataclass(frozen=True)
class TagBroaderDiscoveryIssue:
    tag_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class TagBroaderProposal:
    tag_id: str
    tag: str
    category_id: str
    category_path: str
    target_id: str
    target: str
    target_category_id: str
    target_category_path: str
    pattern_id: str
    pattern_name: str
    pattern_source: str
    confidence: float
    basis: str
    evidence_fingerprint: str
    source_key: str
    proposed_category_id: str = ""
    proposed_category_path: str = ""
    review_boundary: str = "ready"

    @property
    def independently_safe(self) -> bool:
        return self.review_boundary == "ready"

    def suggestion(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "category": self.proposed_category_id or self.category_id,
            "concept_id": None,
            "properties": {},
            "confidence": self.confidence,
            "reason": self.basis,
            "pattern_id": self.pattern_id,
            "similar_tags": [],
            "parent_id": self.target_id,
            "generic_tag": "",
        }

    def task_details(self, report: "TagBroaderDiscoveryReport") -> dict[str, Any]:
        return {
            "tag_id": self.tag_id,
            "tag": self.tag,
            "category_id": self.category_id,
            "category_path": self.category_path,
            "current_parent_id": "",
            "target_id": self.target_id,
            "target": self.target,
            "target_category_id": self.target_category_id,
            "target_category_path": self.target_category_path,
            "pattern_id": self.pattern_id,
            "pattern_name": self.pattern_name,
            "pattern_source": self.pattern_source,
            "confidence": self.confidence,
            "basis": self.basis,
            "before": "No active canonical Broader Tag.",
            "after": f"One reviewed Broader Tag: {self.tag} → {self.target}.",
            "downstream_effects": [
                "Makes the broader target available to related-Tag traversal and reviewed semantic inheritance.",
                "May affect effective Base or Allowed Details inheritance and opt-in broader-Tag export without replacing the source Tag.",
                "Does not confirm a Tag on an image or rewrite Image Knowledge.",
            ],
            "suggestion": self.suggestion(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "authority_fingerprint": report.authority_fingerprint,
            "proposed_category_id": self.proposed_category_id,
            "proposed_category_path": self.proposed_category_path,
            "review_boundary": self.review_boundary,
            "independently_safe": self.independently_safe,
        }


@dataclass(frozen=True)
class TagBroaderDiscoveryReport:
    candidates: tuple[TagBroaderProposal, ...]
    issues: tuple[TagBroaderDiscoveryIssue, ...]
    authority_fingerprint: str
    processed_tags: int
    bounded_tags: int
    bounded_candidates: int
    held_candidates: tuple[TagBroaderProposal, ...] = ()

    def unrecorded(self, reviews: ReviewTaskDatabase) -> tuple[TagBroaderProposal, ...]:
        return tuple(
            candidate for candidate in self.candidates
            if reviews.find_by_source_key(candidate.source_key) is None
        )

    def unrecorded_held(
        self, reviews: ReviewTaskDatabase,
    ) -> tuple[TagBroaderProposal, ...]:
        return tuple(
            candidate for candidate in self.held_candidates
            if reviews.find_by_source_key(candidate.source_key) is None
        )


@dataclass(frozen=True)
class TagBroaderQueueResult:
    queued: int
    updated: int
    skipped: int
    task_ids: tuple[str, ...]
    superseded: int = 0


@dataclass(frozen=True)
class TagBroaderDecisionResult:
    task_id: str
    decision: str
    tag_id: str
    target_id: str
    related_pending_reranked: int = 0
    backup_directory: Path = Path()
    recovered: bool = False


class TagBroaderDiscoveryService:
    """Find exact, pattern-backed Broader Tag proposals in saved local authority."""

    def __init__(
        self,
        knowledge: TagDatabase,
        categories: CategoryRegistry,
        suggestions: SuggestionEngine,
        *,
        default_relationship_path: str | Path,
        user_relationship_path: str | Path,
    ) -> None:
        self.knowledge = knowledge
        self.categories = categories
        self.suggestions = suggestions
        self.default_relationship_path = Path(default_relationship_path)
        self.user_relationship_path = Path(user_relationship_path)

    def _authority_paths(self) -> tuple[Path, ...]:
        pattern_registry = self.suggestions.patterns
        foundation = self.suggestions.foundation
        return tuple(dict.fromkeys((
            self.knowledge.path,
            self.categories.default_path,
            self.categories.user_path,
            Path(pattern_registry.default_path),
            Path(pattern_registry.user_path),
            Path(foundation.modifiers_path),
            Path(foundation.concepts_path),
            self.default_relationship_path,
            self.user_relationship_path,
        )))

    def authority_fingerprint(self) -> str:
        return _authority_fingerprint(self._authority_paths())

    def _semantic_index(self):
        if bool(getattr(self.knowledge, "_semantic_authority_enabled", False)):
            return None
        workspace = RelationshipWorkspaceService(
            self.default_relationship_path,
            self.user_relationship_path,
            tags=self.knowledge,
        )
        return tag_semantic_relationship_index(workspace.effective)

    def _candidate(
        self, record: TagRecord, semantic_index,
    ) -> tuple[TagBroaderProposal | None, TagBroaderDiscoveryIssue | None]:
        if semantic_index is None:
            current_parent = self.knowledge.effective_parent_id(record)
            represented = record.id in getattr(
                self.knowledge, "_represented_parent_sources", (),
            )
        else:
            links = effective_tag_semantic_links_from_index(record, semantic_index)
            current_parent = links.parent_id
            represented = links.specificity_represented
        if current_parent or represented:
            return None, None
        suggestion = self.suggestions.suggest(
            record.display, include_similar=False,
        )
        pattern_id = str(suggestion.pattern_id or "")
        parent_id = str(suggestion.parent_id or "")
        if (
            not pattern_id
            or pattern_id.startswith("known-family:")
            or not parent_id
            or parent_id == record.id
            or float(suggestion.confidence or 0) < 0.95
        ):
            return None, None
        target = self.knowledge.tags.get(parent_id)
        if target is None:
            return None, TagBroaderDiscoveryIssue(
                record.id, "missing_broader_target", parent_id,
            )
        category_path_ids = set(self.categories.path_ids(record.category))
        pattern = self.suggestions.patterns.patterns.get(pattern_id)
        if pattern is None or not pattern.enabled:
            return None, TagBroaderDiscoveryIssue(
                record.id, "pattern_no_longer_available", pattern_id,
            )
        evidence = {
            "tag_id": record.id,
            "tag": record.display,
            "category_id": record.category,
            "target_id": target.id,
            "target": target.display,
            "target_category_id": target.category,
            "pattern": {
                **pattern.to_dict(),
                "source": pattern.source,
            },
            "confidence": round(float(suggestion.confidence or 0), 6),
            "basis": str(suggestion.reason or ""),
        }
        evidence_fingerprint = _fingerprint(evidence)
        category_path = self.categories.path_label(record.category)
        target_category_path = self.categories.path_label(target.category)
        proposed_category_id = str(suggestion.category or "")
        proposed_category_path = (
            self.categories.path_label(proposed_category_id)
            if proposed_category_id in self.categories.categories else
            proposed_category_id or "Not available"
        )
        if proposed_category_id != str(record.category or ""):
            review_boundary = "category_change_required"
            issue = TagBroaderDiscoveryIssue(
                record.id, review_boundary,
                (
                    "The reusable pattern also proposes moving this Tag from "
                    f"{category_path} to {proposed_category_path}, so the Broader Tag "
                    "cannot be reviewed independently."
                ),
            )
        elif target.category not in category_path_ids | {record.category}:
            review_boundary = "broader_target_outside_category_path"
            issue = TagBroaderDiscoveryIssue(
                record.id, review_boundary,
                (
                    f"The proposed target {target.display} is in {target_category_path}, "
                    f"outside the source path {category_path}."
                ),
            )
        else:
            review_boundary = "ready"
            issue = None
        candidate = TagBroaderProposal(
            record.id, record.display, record.category, category_path,
            target.id, target.display, target.category, target_category_path,
            pattern.id, pattern.name, pattern.source,
            round(float(suggestion.confidence or 0), 6),
            str(suggestion.reason or ""), evidence_fingerprint,
            f"tag_broader:{record.id}:{evidence_fingerprint[:24]}",
            proposed_category_id, proposed_category_path, review_boundary,
        )
        return candidate, issue

    def candidate_for_tag(self, tag_id: str) -> TagBroaderProposal | None:
        candidate = self.finding_for_tag(tag_id)
        return (
            candidate
            if candidate is not None and candidate.independently_safe else None
        )

    def finding_for_tag(self, tag_id: str) -> TagBroaderProposal | None:
        record = self.knowledge.tags.get(str(tag_id))
        if record is None:
            return None
        candidate, _issue = self._candidate(record, self._semantic_index())
        return candidate

    def discover(self) -> TagBroaderDiscoveryReport:
        before = self.authority_fingerprint()
        records = sorted(
            self.knowledge.tags.values(),
            key=lambda item: (normalize_tag(item.display), item.id),
        )
        bounded_tags = max(0, len(records) - MAX_BROADER_DISCOVERY_TAGS)
        records = records[:MAX_BROADER_DISCOVERY_TAGS]
        semantic_index = self._semantic_index()
        candidates = []
        held_candidates = []
        issues = []
        for record in records:
            candidate, issue = self._candidate(record, semantic_index)
            if candidate is not None:
                (
                    candidates if candidate.independently_safe else held_candidates
                ).append(candidate)
            if issue is not None:
                issues.append(issue)
        candidates.sort(key=lambda item: (
            -item.confidence, item.pattern_id, normalize_tag(item.tag), item.tag_id,
        ))
        held_candidates.sort(key=lambda item: (
            item.review_boundary, -item.confidence,
            item.pattern_id, normalize_tag(item.tag), item.tag_id,
        ))
        total_candidates=len(candidates)+len(held_candidates)
        candidates=candidates[:MAX_BROADER_DISCOVERY_CANDIDATES]
        held_capacity=max(
            0,MAX_BROADER_DISCOVERY_CANDIDATES-len(candidates),
        )
        held_candidates=held_candidates[:held_capacity]
        bounded_candidates=max(
            0,total_candidates-len(candidates)-len(held_candidates),
        )
        if bounded_tags:
            issues.append(TagBroaderDiscoveryIssue(
                "knowledge_store", "tag_scan_limit",
                f"{bounded_tags:,} Tags were not scanned by this bounded report.",
            ))
        if bounded_candidates:
            issues.append(TagBroaderDiscoveryIssue(
                "knowledge_store", "candidate_limit",
                f"{bounded_candidates:,} lower-ranked proposals were not retained by this bounded report.",
            ))
        after = self.authority_fingerprint()
        if before != after:
            raise RuntimeError(
                "System Knowledge changed while Broader Tag proposals were being prepared. Run Find New Knowledge again."
            )
        return TagBroaderDiscoveryReport(
            tuple(candidates), tuple(issues), after,
            len(records), bounded_tags, bounded_candidates,
            tuple(held_candidates),
        )

    def queue(
        self,
        report: TagBroaderDiscoveryReport,
        reviews: ReviewTaskDatabase,
        *,
        selected_source_keys: Iterable[str] | None = None,
        allow_held: bool = False,
    ) -> TagBroaderQueueResult:
        if self.authority_fingerprint() != report.authority_fingerprint:
            raise RuntimeError(
                "System Knowledge changed after this Broader Tag report. Run Find New Knowledge again."
            )
        selected = (
            None if selected_source_keys is None
            else {str(value) for value in selected_source_keys}
        )
        held_keys = {
            candidate.source_key for candidate in report.held_candidates
        }
        if allow_held and (
            selected is None
            or len(selected) != 1
            or not selected.issubset(held_keys)
        ):
            raise ValueError(
                "Choose exactly one held Broader Tag finding for explicit review."
            )
        queue_candidates = (
            (*report.candidates, *report.held_candidates)
            if allow_held else report.candidates
        )
        queued = updated = skipped = superseded = 0
        task_ids = []
        try:
            with reviews.transaction():
                for candidate in queue_candidates:
                    if selected is not None and candidate.source_key not in selected:
                        continue
                    existing = reviews.find_by_source_key(candidate.source_key)
                    if existing is not None and existing.status in {"resolved", "ignored"}:
                        skipped += 1
                        continue
                    for previous in reviews.tasks.values():
                        if (
                            previous.status == "pending"
                            and previous.task_type == TAG_BROADER_PROPOSAL_TASK_TYPE
                            and str(previous.details.get("tag_id") or "") == candidate.tag_id
                            and previous.source_key != candidate.source_key
                        ):
                            previous.status = "resolved"
                            previous.details["resolution"] = {
                                "decision": "superseded_by_evidence_refresh",
                                "replacement_source_key": candidate.source_key,
                                "resolved_at": _now(),
                                "system_knowledge_effect": "none",
                            }
                            previous.updated_at = _now()
                            superseded += 1
                    before = existing.to_dict() if existing is not None else None
                    task = reviews.add_task(
                        task_type=TAG_BROADER_PROPOSAL_TASK_TYPE,
                        title=f"Review Broader Tag for {candidate.tag}",
                        description=(
                            "One exact reusable pattern proposes a canonical Broader Tag. Pattern confidence ranks this question but does not approve it."
                            + (
                                " This held finding also requires an explicit Category-impact review."
                                if not candidate.independently_safe else ""
                            )
                        ),
                        details=candidate.task_details(report),
                        priority="normal",
                        source_key=candidate.source_key,
                    )
                    task_ids.append(task.id)
                    if before is None:
                        queued += 1
                    elif task.to_dict() != before:
                        updated += 1
                if queued or updated or superseded:
                    reviews.save()
        except BaseException:
            reviews.load()
            raise
        return TagBroaderQueueResult(
            queued, updated, skipped, tuple(task_ids), superseded,
        )


class TagBroaderReviewService:
    """Record one reviewed Broader Tag through canonical relationship authority."""

    def __init__(
        self,
        knowledge: TagDatabase,
        categories: CategoryRegistry,
        reviews: ReviewTaskDatabase,
        suggestions: SuggestionEngine,
        *,
        default_relationship_path: str | Path,
        user_relationship_path: str | Path,
        backup_root: str | Path | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.categories = categories
        self.reviews = reviews
        self.suggestions = suggestions
        self.default_relationship_path = Path(default_relationship_path)
        self.user_relationship_path = Path(user_relationship_path)
        self.backup_root = (
            Path(backup_root) if backup_root is not None
            else knowledge.path.parent / "backups" / "relationship-tag-semantics"
        )

    def _discovery(self) -> TagBroaderDiscoveryService:
        return TagBroaderDiscoveryService(
            self.knowledge, self.categories, self.suggestions,
            default_relationship_path=self.default_relationship_path,
            user_relationship_path=self.user_relationship_path,
        )

    def _reload_authority(self) -> TagSemanticRelationshipService:
        self.knowledge.load()
        self.categories.load()
        self.suggestions.patterns.load()
        self.suggestions.foundation.load()
        return TagSemanticRelationshipService(
            self.knowledge,
            default_path=self.default_relationship_path,
            user_path=self.user_relationship_path,
        )

    def _task(self, task_id: str) -> ReviewTask:
        task = self.reviews.tasks.get(str(task_id))
        if task is None or task.task_type != TAG_BROADER_PROPOSAL_TASK_TYPE:
            raise ValueError("The Broader Tag Review question is no longer available.")
        return task

    @staticmethod
    def _decision(task: ReviewTask) -> ReviewedKnowledgeDecision:
        suggestion = task.details.get("suggestion")
        return ReviewedKnowledgeDecision(
            task.id,
            (str(task.details.get("tag") or ""),),
            dict(suggestion) if isinstance(suggestion, dict) else {},
        )

    def _rerank(self, task: ReviewTask) -> int:
        return rerank_related_knowledge_questions(
            self.reviews, self.suggestions, (self._decision(task),),
        ).count

    def _validate_pending(self, task: ReviewTask) -> TagBroaderProposal:
        discovery = self._discovery()
        if discovery.authority_fingerprint() != str(
            task.details.get("authority_fingerprint") or ""
        ):
            raise RuntimeError(
                "System Knowledge or reusable-pattern evidence changed after this question was prepared. Run Find New Knowledge again."
            )
        candidate = discovery.finding_for_tag(
            str(task.details.get("tag_id") or ""),
        )
        if (
            candidate is None
            or candidate.evidence_fingerprint
            != str(task.details.get("evidence_fingerprint") or "")
            or candidate.target_id != str(task.details.get("target_id") or "")
            or candidate.review_boundary
            != str(task.details.get("review_boundary") or "ready")
            or candidate.proposed_category_id
            != str(task.details.get("proposed_category_id") or "")
        ):
            raise RuntimeError(
                "The exact Broader Tag proposal changed after discovery. Run Find New Knowledge again."
            )
        return candidate

    def recover_interrupted(self) -> tuple[TagBroaderDecisionResult, ...]:
        with self.reviews.transaction():
            if not any(
                task.task_type == TAG_BROADER_PROPOSAL_TASK_TYPE
                and task.status == "applying"
                for task in self.reviews.tasks.values()
            ):
                return ()
            semantic = self._reload_authority()
            self.reviews.load()
            changed = False
            recovered = []
            for task in self.reviews.tasks.values():
                if (
                    task.task_type != TAG_BROADER_PROPOSAL_TASK_TYPE
                    or task.status != "applying"
                ):
                    continue
                tag_id = str(task.details.get("tag_id") or "")
                target_id = str(task.details.get("target_id") or "")
                record = self.knowledge.tags.get(tag_id)
                applied = bool(
                    record is not None
                    and semantic.effective_for_tag(tag_id).parent_id == target_id
                    and (
                        str(task.details.get("review_boundary") or "ready")
                        != "category_change_required"
                        or record.category
                        == str(task.details.get("proposed_category_id") or "")
                    )
                )
                task.status = "resolved" if applied else "pending"
                resolution = task.details.get("resolution")
                resolution = dict(resolution) if isinstance(resolution, dict) else {}
                decision = "accepted" if applied else "recovered_for_review"
                resolution.update({"decision": decision, "recovered_at": _now()})
                task.details["resolution"] = resolution
                task.updated_at = _now()
                reranked = self._rerank(task) if applied else 0
                recovered.append(TagBroaderDecisionResult(
                    task.id, decision, tag_id, target_id,
                    reranked, recovered=True,
                ))
                changed = True
            if changed:
                try:
                    self.reviews.save()
                except BaseException:
                    self.reviews.load()
                    raise
            return tuple(recovered)

    def decide(self, task_id: str, decision: str) -> TagBroaderDecisionResult:
        decision = str(decision)
        if decision not in {"accepted", "rejected"}:
            raise ValueError("Choose accepted or rejected for this Broader Tag proposal.")
        with self.reviews.transaction():
            semantic = self._reload_authority()
            self.reviews.load()
            task = self._task(task_id)
            if task.status != "pending":
                raise RuntimeError("This Broader Tag question is no longer pending.")
            candidate = self._validate_pending(task)
            if decision == "rejected":
                task.status = "ignored"
                task.details["resolution"] = {
                    "decision": "rejected",
                    "evidence_fingerprint": candidate.evidence_fingerprint,
                    "decided_at": _now(),
                    "system_knowledge_effect": "none",
                    "image_knowledge_effect": "none",
                }
                task.updated_at = _now()
                reranked = self._rerank(task)
                try:
                    self.reviews.save()
                except BaseException:
                    self.reviews.load()
                    raise
                return TagBroaderDecisionResult(
                    task.id, decision, candidate.tag_id, candidate.target_id,
                    reranked,
                )

            task.status = "applying"
            task.details["resolution"] = {
                "decision": "accepted",
                "tag_id": candidate.tag_id,
                "target_id": candidate.target_id,
                "evidence_fingerprint": candidate.evidence_fingerprint,
                "decided_at": _now(),
                "image_knowledge_effect": "none",
            }
            task.updated_at = _now()
            try:
                self.reviews.save()
            except BaseException:
                self.reviews.load()
                raise
            try:
                category_assignments = ()
                if candidate.review_boundary == "category_change_required":
                    category = self.categories.categories.get(
                        candidate.proposed_category_id,
                    )
                    if category is None:
                        raise RuntimeError(
                            "The proposed Category is no longer available. Run Find New Knowledge again."
                        )
                    group, priority, _reviewed, _source = (
                        self.categories.effective_ordering(category.id)
                    )
                    category_assignments = (
                        TagCategoryAssignment(
                            candidate.tag_id, category.id, group, priority,
                            source="reviewed_local_discovery",
                            reason=(
                                "Reviewed KM-03 Broader Tag category repair "
                                f"from {candidate.pattern_name}: {candidate.basis}"
                            ),
                        ),
                    )
                saved = semantic.apply(
                    (TagSemanticChange(
                        candidate.tag_id,
                        parent_id=candidate.target_id,
                        kinds=(SPECIFICITY,),
                        source="reviewed_local_discovery",
                        reason=(
                            "Reviewed KM-03 Broader Tag proposal from "
                            f"{candidate.pattern_name}: {candidate.basis}"
                        ),
                    ),),
                    category_assignments=category_assignments,
                    backup_root=self.backup_root,
                )
                self.knowledge.load()
                verified = TagSemanticRelationshipService(
                    self.knowledge,
                    default_path=self.default_relationship_path,
                    user_path=self.user_relationship_path,
                )
                verified_record = self.knowledge.tags.get(candidate.tag_id)
                if (
                    verified.effective_for_tag(candidate.tag_id).parent_id
                    != candidate.target_id
                    or verified_record is None
                    or (
                        candidate.review_boundary == "category_change_required"
                        and verified_record.category
                        != candidate.proposed_category_id
                    )
                ):
                    raise RuntimeError("The reviewed Broader Tag failed semantic read-back verification.")
                task.status = "resolved"
                task.details["resolution"].update({
                    "relationship_ids": list(saved.relationship_ids),
                    "recovery_manifest": str(saved.manifest_path),
                    "applied_at": _now(),
                    "review_boundary": candidate.review_boundary,
                    "category_id": candidate.proposed_category_id,
                })
                task.updated_at = _now()
                reranked = self._rerank(task)
                self.reviews.save()
            except BaseException:
                self.knowledge.load()
                current_semantic = TagSemanticRelationshipService(
                    self.knowledge,
                    default_path=self.default_relationship_path,
                    user_path=self.user_relationship_path,
                )
                current_record = self.knowledge.tags.get(candidate.tag_id)
                fully_applied = bool(
                    current_record is not None
                    and current_semantic.effective_for_tag(
                        candidate.tag_id,
                    ).parent_id == candidate.target_id
                    and (
                        candidate.review_boundary != "category_change_required"
                        or current_record.category
                        == candidate.proposed_category_id
                    )
                )
                if not fully_applied:
                    task.status = "pending"
                    task.updated_at = _now()
                    try:
                        self.reviews.save()
                    except Exception:
                        self.reviews.load()
                raise
            return TagBroaderDecisionResult(
                task.id, decision, candidate.tag_id, candidate.target_id,
                reranked, saved.manifest_path.parent,
            )


__all__ = [
    "MAX_BROADER_DISCOVERY_CANDIDATES",
    "MAX_BROADER_DISCOVERY_TAGS",
    "TAG_BROADER_PROPOSAL_TASK_TYPE",
    "TagBroaderDecisionResult",
    "TagBroaderDiscoveryIssue",
    "TagBroaderDiscoveryReport",
    "TagBroaderDiscoveryService",
    "TagBroaderProposal",
    "TagBroaderQueueResult",
    "TagBroaderReviewService",
]
