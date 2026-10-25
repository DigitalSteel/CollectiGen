from __future__ import annotations

import hashlib
import json
import math
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable,Iterable
from uuid import uuid4

from .category_registry import CategoryRegistry
from .discovery_registry import DiscoveryRegistry
from .merge_review import MergeBatchItem, MergeBatchResult
from .review_queue import ReviewTaskDatabase
from .tag_database import TagDatabase
from .automation_rule_conversion import RulePairAdmission,RuleConversionAdmissionError
from .tag_collection import normalize_tag
from .paths import DEFAULTS_DIR
from .relationship_store import RelationshipStore
from .relationship_workspace import RelationshipWorkspaceService
from .tag_semantic_relationships import (
    TagSemanticChange,stage_tag_semantic_changes,
)
from .tag_alias_relationships import (
    TagAliasChange,TagAliasPreflightError,TagAliasProposal,
    TagAliasRelationshipService,stage_tag_alias_changes,
)
from .knowledge_relationships import SPECIFICITY
from .knowledge_engine import FoundationKnowledge,SuggestionEngine
from .pattern_registry import PatternRegistry
from .related_knowledge_rerank import (
    ReviewedKnowledgeDecision,rerank_related_knowledge_questions,
)


class ReviewSafeBatchStaleError(RuntimeError):
    """The saved authority used by a Review Safe Suggestions preview changed."""


@dataclass(frozen=True)
class ReviewSafeKnowledgeItem:
    task_id: str
    task_type: str
    names: tuple[str, ...]
    suggestion: dict
    task_fingerprint: str
    remaining_names: tuple[str, ...] = ()


@dataclass
class ReviewSafeBatchPreview:
    knowledge_items: list[ReviewSafeKnowledgeItem] = field(default_factory=list)
    merge_items: list[MergeBatchItem] = field(default_factory=list)
    source_fingerprints: dict[str, str] = field(default_factory=dict)
    uncached_questions: int = 0
    cancelled: bool = False

    @property
    def interpreted_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for item in self.knowledge_items for name in item.names))

    @property
    def knowledge_question_count(self) -> int:
        return len(self.knowledge_items)

    @property
    def cleanup_image_count(self) -> int:
        return len({str(item.image_path) for item in self.merge_items})

    @property
    def total_actions(self) -> int:
        return len(self.interpreted_names) + len(self.merge_items)


@dataclass
class ReviewSafeBatchResult:
    knowledge: TagDatabase
    discovery: DiscoveryRegistry
    reviews: ReviewTaskDatabase
    merge_result: MergeBatchResult = field(default_factory=MergeBatchResult)
    backup_directory: Path | None = None
    knowledge_names: int = 0
    knowledge_questions: int = 0
    related_pending_reranked: int = 0


@dataclass(frozen=True)
class ReviewTagDecisionRequest:
    task_ids: tuple[str, ...]
    task_fingerprints: dict[str, str]
    suggestions: dict[str, dict]
    choice: str
    category_id: str = ""
    target_id: str = ""


@dataclass
class ReviewTagDecisionResult:
    knowledge: TagDatabase
    discovery: DiscoveryRegistry
    reviews: ReviewTaskDatabase
    backup_directory: Path
    task_count: int
    tag_names: tuple[str, ...]
    related_pending_reranked: int = 0


@dataclass(frozen=True)
class ReviewExampleSet:
    paths: tuple[Path, ...]
    coverage: dict[str, tuple[str, ...]]
    unavailable_count: int = 0


def build_review_example_set(
    tag_examples,
    *,
    available: Callable[[str], bool] | None = None,
) -> ReviewExampleSet:
    """Deduplicate and fairly interleave selected-tag evidence by stable path."""
    predicate = available or (lambda _path: True)
    ordered_tags = []
    queues: dict[str, list[str]] = {}
    coverage: dict[str, list[str]] = {}
    display_paths: dict[str, Path] = {}
    unavailable = set()
    for raw_tag, raw_paths in tag_examples:
        tag = str(raw_tag or "").strip()
        if not tag:
            continue
        ordered_tags.append(tag)
        queue = queues.setdefault(tag, [])
        for raw_path in raw_paths:
            path = str(Path(raw_path))
            if not path or path in queue:
                continue
            if not predicate(path):
                unavailable.add(path)
                continue
            queue.append(path)
            display_paths.setdefault(path, Path(raw_path))
            labels = coverage.setdefault(path, [])
            if tag not in labels:
                labels.append(tag)
    result = []
    selected = set()
    while True:
        added = False
        for tag in ordered_tags:
            queue = queues.get(tag, [])
            while queue and queue[0] in selected:
                queue.pop(0)
            if not queue:
                continue
            path = queue.pop(0)
            selected.add(path)
            result.append(display_paths[path])
            added = True
        if not added:
            break
    return ReviewExampleSet(
        tuple(result),
        {path: tuple(labels) for path, labels in coverage.items()},
        len(unavailable),
    )


def _fingerprint_path(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _task_fingerprint(task) -> str:
    payload=task.to_dict()
    details=payload.get("details")
    if isinstance(details,dict):
        # Queue triage and local reranking decorate loaded tasks for display;
        # neither is saved Review authority or a changed user decision.
        payload["details"]={
            key:value for key,value in details.items()
            if key not in {"queue_health","related_rerank"}
        }
    value = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def review_safe_task_fingerprint(task) -> str:
    """Public stable fingerprint for one cached safe-suggestion decision."""
    return _task_fingerprint(task)


def review_tag_selection_fingerprint(task) -> str:
    details = task.details if isinstance(task.details, dict) else {}
    value = json.dumps({
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "tag": str(details.get("tag") or ""),
        "updated_at": task.updated_at,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _write_json(path: Path, payload: dict) -> None:
    _atomic_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _discovery_state(database: DiscoveryRegistry) -> list[dict]:
    return [item.to_dict() for item in sorted(database.items.values(), key=lambda item: normalize_tag(item.display))]


def _review_state(database: ReviewTaskDatabase) -> list[dict]:
    return [item.to_dict() for item in sorted(database.tasks.values(), key=lambda item: item.id)]


class ReviewSafeBatchService:
    """Prepare and apply cached high-confidence Review outcomes off the UI thread."""

    def __init__(
        self,
        knowledge_path: str | Path,
        discovery_path: str | Path,
        reviews_path: str | Path,
        *,
        default_categories_path: str | Path,
        user_categories_path: str | Path,
        backup_root: str | Path,
        journal_path: str | Path,
        default_relationships_path: str | Path | None = None,
        user_relationships_path: str | Path | None = None,
        default_patterns_path: str | Path | None = None,
        user_patterns_path: str | Path | None = None,
        modifiers_path: str | Path | None = None,
        concepts_path: str | Path | None = None,
        rule_pair_admission: RulePairAdmission | None = None,
    ) -> None:
        self.knowledge_path = Path(knowledge_path)
        if rule_pair_admission is not None and not rule_pair_admission.owns(self.knowledge_path,"knowledge"):
            raise RuleConversionAdmissionError("Review requires its exact bound System Knowledge path.")
        self.rule_pair_admission=rule_pair_admission
        self.discovery_path = Path(discovery_path)
        self.reviews_path = Path(reviews_path)
        self.default_categories_path = Path(default_categories_path)
        self.user_categories_path = Path(user_categories_path)
        self.backup_root = Path(backup_root)
        self.journal_path = Path(journal_path)
        self.default_relationships_path=Path(
            default_relationships_path or DEFAULTS_DIR/"relationships.json"
        )
        self.user_relationships_path=Path(
            user_relationships_path or self.knowledge_path.with_name("relationships.json")
        )
        self.default_patterns_path=Path(
            default_patterns_path or self.default_categories_path.with_name("patterns.json")
        )
        self.user_patterns_path=Path(
            user_patterns_path or self.knowledge_path.with_name("patterns.json")
        )
        self.modifiers_path=Path(
            modifiers_path or self.default_categories_path.with_name("modifiers.json")
        )
        self.concepts_path=Path(
            concepts_path or self.default_categories_path.with_name("concepts.json")
        )

    @property
    def source_paths(self) -> tuple[Path, Path, Path, Path]:
        return (
            self.knowledge_path,self.user_relationships_path,
            self.discovery_path,self.reviews_path,
        )

    @property
    def guard_paths(self) -> tuple[Path, ...]:
        return (
            *self.source_paths,self.default_relationships_path,
            self.default_categories_path,self.user_categories_path,
            self.default_patterns_path,self.user_patterns_path,
            self.modifiers_path,self.concepts_path,
        )

    def _fingerprints(self) -> dict[str, str]:
        return {str(path): _fingerprint_path(path) for path in self.guard_paths}

    def _knowledge(self) -> TagDatabase:
        database = TagDatabase(self.knowledge_path,rule_pair_admission=self.rule_pair_admission)
        database.apply_category_ordering(CategoryRegistry(self.default_categories_path, self.user_categories_path))
        return database

    def _relationship_workspace(
        self, tags: TagDatabase | None = None,
    ) -> RelationshipWorkspaceService:
        return RelationshipWorkspaceService(
            self.default_relationships_path,self.user_relationships_path,
            tags=tags,
        )

    def _suggestion_engine(self, knowledge: TagDatabase) -> SuggestionEngine:
        return SuggestionEngine(
            FoundationKnowledge(self.modifiers_path,self.concepts_path),
            PatternRegistry(self.default_patterns_path,self.user_patterns_path),
            knowledge,
        )

    def preview(self, should_cancel: Callable[[], bool] | None = None) -> ReviewSafeBatchPreview:
        should_cancel = should_cancel or (lambda: False)
        before = self._fingerprints()
        knowledge = self._knowledge()
        self._relationship_workspace(knowledge)
        reviews = ReviewTaskDatabase(self.reviews_path)
        preview = ReviewSafeBatchPreview(source_fingerprints=before)
        for index, task in enumerate(reviews.tasks.values()):
            if index % 100 == 0 and should_cancel():
                preview.cancelled = True
                return preview
            if task.status != "pending":
                continue
            if task.task_type in {"new_tag", "pattern_group"}:
                cached = task.details.get("suggestion") if isinstance(task.details, dict) else None
                if not isinstance(cached, dict):
                    preview.uncached_questions += 1
                    continue
                try:
                    confidence = float(cached.get("confidence") or 0)
                except (TypeError, ValueError):
                    continue
                category = str(cached.get("category") or "").strip()
                categories = getattr(knowledge, "_categories", None)
                if (
                    not math.isfinite(confidence) or confidence < .95
                    or not category or category == "uncategorized"
                    or categories is None or category not in categories.categories
                ):
                    continue
                names = (
                    (str(task.details.get("tag") or "").strip(),)
                    if task.task_type == "new_tag"
                    else tuple(str(value).strip() for value in task.details.get("tags", []) if str(value).strip())
                )
                names = tuple(name for name in names if name)
                if names:
                    preview.knowledge_items.append(ReviewSafeKnowledgeItem(
                        task.id, task.task_type, names, dict(cached), _task_fingerprint(task),
                    ))
                continue
        after = self._fingerprints()
        if before != after:
            raise ReviewSafeBatchStaleError(
                "Saved Review Center or System Knowledge data changed while the preview was being prepared. Reload and try again."
            )
        preview.source_fingerprints = after
        return preview

    def preview_selected(
        self, items: Iterable[ReviewSafeKnowledgeItem],
    ) -> ReviewSafeBatchPreview:
        """Build one exact selected-question preview without broadening its scope."""
        before=self._fingerprints()
        knowledge=self._knowledge(); self._relationship_workspace(knowledge)
        reviews=ReviewTaskDatabase(self.reviews_path)
        preview=ReviewSafeBatchPreview(
            knowledge_items=list(items),source_fingerprints=before,
        )
        self._verify_preview(preview,reviews)
        after=self._fingerprints()
        if before!=after:
            raise ReviewSafeBatchStaleError(
                "Saved Review Center or System Knowledge data changed while the selected preview was prepared. Reload and try again."
            )
        preview.source_fingerprints=after
        return preview

    def _verify_preview(self, preview: ReviewSafeBatchPreview, reviews: ReviewTaskDatabase) -> None:
        if preview.merge_items:
            raise ReviewSafeBatchStaleError(
                "Legacy merge suggestions cannot be applied to confirmed Image Knowledge. "
                "Prepare a new System Knowledge-only preview."
            )
        if self._fingerprints() != preview.source_fingerprints:
            raise ReviewSafeBatchStaleError(
                "This safe-suggestion preview is no longer current because saved Review Center or System Knowledge data changed. Reload and review it again."
            )
        for item in preview.knowledge_items:
            task = reviews.tasks.get(item.task_id)
            if task is None or task.status != "pending" or _task_fingerprint(task) != item.task_fingerprint:
                raise ReviewSafeBatchStaleError(
                    "A question in this safe-suggestion preview changed before it could be saved. Reload and review the batch again."
                )

    def _create_backup(
        self, *, kind: str = "review_safe_suggestions", prefix: str = "safe_review",
    ) -> tuple[Path, Path, list[dict]]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self.backup_root / f"{prefix}_{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        entries = []
        for index, path in enumerate(self.source_paths):
            backup = directory / f"{index}_{path.name}"
            existed = path.exists()
            if existed:
                shutil.copy2(path, backup)
            entries.append({"target": str(path.resolve()), "backup": backup.name, "existed": existed})
        manifest = directory / "recovery.json"
        _write_json(manifest, {
            "schema_version": 1,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "applying",
            "files": entries,
        })
        return directory, manifest, entries

    @staticmethod
    def _restore(directory: Path, entries: list[dict]) -> None:
        for entry in entries:
            target = Path(entry["target"])
            if entry["existed"]:
                _atomic_bytes(target, (directory / entry["backup"]).read_bytes())
            elif target.exists():
                target.unlink()

    @staticmethod
    def _apply_knowledge_item(
        item: ReviewSafeKnowledgeItem,
        knowledge: TagDatabase,
        discovery: DiscoveryRegistry,
        reviews: ReviewTaskDatabase,
    ) -> tuple[TagSemanticChange, ...]:
        task = reviews.tasks[item.task_id]
        category = str(item.suggestion.get("category") or "uncategorized")
        semantic_changes=[]
        for name in item.names:
            observed = discovery.items.get(normalize_tag(name))
            seen = int(task.details.get("seen_count") or 0) if item.task_type == "new_tag" else int(observed.seen_count if observed else 0)
            record = knowledge.add_known_tag(
                name,
                category=category,
                concept_id=item.suggestion.get("concept_id"),
                properties=item.suggestion.get("properties") or {},
                seen_count=seen,
                provenance={
                    "source": "qt_confirmed_suggestion" if item.task_type == "new_tag" else "qt_confident_batch",
                    "reason": item.suggestion.get("reason") or "",
                },
            )
            parent_id = str(item.suggestion.get("parent_id") or "")
            if parent_id and parent_id in knowledge.tags and parent_id != record.id:
                semantic_changes.append(TagSemanticChange(
                    record.id,parent_id=parent_id,kinds=(SPECIFICITY,),
                    source=(
                        "qt_confirmed_suggestion"
                        if item.task_type=="new_tag" else "qt_confident_batch"
                    ),
                    reason=str(item.suggestion.get("reason") or ""),
                ))
            discovery.remove(name)
        if item.task_type == "pattern_group" and item.remaining_names:
            task.details["tags"] = list(item.remaining_names)
            task.title = f"Review {len(item.remaining_names)} related SDXL Tags"
            task.details.pop("resolution", None)
        else:
            resolution = {
                "decision": "kept_as_knowledge" if item.task_type == "new_tag" else "confirmed_confident_batch",
                "category": category,
                "concept_id": item.suggestion.get("concept_id"),
                "scope": "exact_sdxl_tag",
            }
            if item.task_type == "pattern_group":
                resolution["tags"] = list(item.names)
            task.details["resolution"] = resolution
            reviews.set_status(task.id, "resolved")
        return tuple(semantic_changes)

    @staticmethod
    def _verify_saved(
        knowledge: TagDatabase,
        discovery: DiscoveryRegistry,
        reviews: ReviewTaskDatabase,
    ) -> None:
        expected_knowledge=json.loads(json.dumps(knowledge._payload()))
        actual_knowledge=json.loads(knowledge.path.read_text(encoding="utf-8"))
        if actual_knowledge != expected_knowledge:
            raise RuntimeError("System Knowledge failed read-back verification.")
        if _discovery_state(DiscoveryRegistry(discovery.path)) != _discovery_state(discovery):
            raise RuntimeError("The new-name registry failed read-back verification.")
        if _review_state(ReviewTaskDatabase(reviews.path)) != _review_state(reviews):
            raise RuntimeError("Review outcomes failed read-back verification.")

    @staticmethod
    def _save_discovery(discovery: DiscoveryRegistry) -> None:
        _write_json(discovery.path, {
            "schema_version": 1,
            "discovered_tags": [
                item.to_dict()
                for item in sorted(discovery.items.values(), key=lambda item: normalize_tag(item.display))
            ],
        })

    def apply(self, preview: ReviewSafeBatchPreview) -> ReviewSafeBatchResult:
        with (self.rule_pair_admission.admit() if self.rule_pair_admission is not None else nullcontext()), ReviewTaskDatabase.path_transaction(self.reviews_path):
            return self._apply_locked(preview)

    def _apply_locked(self, preview: ReviewSafeBatchPreview) -> ReviewSafeBatchResult:
        if preview.cancelled:
            raise RuntimeError("A cancelled safe-suggestion preview cannot be applied.")
        if preview.merge_items:
            raise RuntimeError(
                "Legacy merge suggestions cannot be applied to confirmed Image Knowledge."
            )
        knowledge = self._knowledge()
        discovery = DiscoveryRegistry(self.discovery_path)
        reviews = ReviewTaskDatabase(self.reviews_path)
        relationships=self._relationship_workspace(knowledge)
        TagAliasRelationshipService(
            knowledge,default_path=self.default_relationships_path,
            user_path=self.user_relationships_path,workspace=relationships,
        )
        self._verify_preview(preview, reviews)
        directory, manifest, entries = self._create_backup()
        merge_result = MergeBatchResult()
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        try:
            if self._fingerprints() != preview.source_fingerprints:
                raise ReviewSafeBatchStaleError(
                    "Saved Review Center, System Knowledge, or category data changed before the batch could start. Reload and review it again."
                )
            semantic_changes=[]
            for item in preview.knowledge_items:
                semantic_changes.extend(
                    self._apply_knowledge_item(item,knowledge,discovery,reviews)
                )
            rerank=rerank_related_knowledge_questions(
                reviews,self._suggestion_engine(knowledge),(
                    ReviewedKnowledgeDecision(
                        item.task_id,item.names,item.suggestion,
                    )
                    for item in preview.knowledge_items
                ),
            )
            user_values,_changed_ids=stage_tag_semantic_changes(
                knowledge,relationships,semantic_changes,
            ) if semantic_changes else (dict(relationships.user_store.relationships),())
            relationships.user_store.replace(user_values.values())
            _write_json(
                relationships.user_store.path,relationships.user_store.payload(),
            )
            knowledge.save(); self._save_discovery(discovery); reviews.save()
            self._verify_saved(knowledge, discovery, reviews)
            saved_relationships=RelationshipStore(relationships.user_store.path)
            if saved_relationships.payload()!=relationships.user_store.payload():
                raise RuntimeError("Semantic relationships failed read-back verification.")

            manifest_payload.update({
                "status": "complete",
                "knowledge_names": list(preview.interpreted_names),
                "knowledge_questions": preview.knowledge_question_count,
                "cleanup_questions": 0,
                "cleanup_images": 0,
                "image_recovery_manifest": "",
                "related_pending_reranked": rerank.count,
                "related_pending_task_ids": list(rerank.task_ids),
            })
            _write_json(manifest, manifest_payload)
        except Exception as exc:
            recovery_errors = []
            try:
                self._restore(directory, entries)
            except Exception as recovery_exc:
                recovery_errors.append(f"registry recovery failed: {recovery_exc}")
            manifest_payload["status"] = "recovery_failed" if recovery_errors else "recovered"
            manifest_payload["error"] = str(exc)
            if recovery_errors:
                manifest_payload["recovery_errors"] = recovery_errors
            _write_json(manifest, manifest_payload)
            state = "Recovery needs attention" if recovery_errors else "System Knowledge and Review outcomes were restored"
            raise RuntimeError(f"{state}. {exc} Recovery record: {manifest}") from exc
        return ReviewSafeBatchResult(
            knowledge, discovery, reviews, merge_result, directory,
            len(preview.interpreted_names), preview.knowledge_question_count,
            rerank.count,
        )

    def disable_merge_rule(
        self, rule_id: str, task_fingerprints: dict[str, str],
    ) -> ReviewSafeBatchResult:
        """Disable one saved cleanup rule and close its exact pending questions."""
        with (self.rule_pair_admission.admit() if self.rule_pair_admission is not None else nullcontext()), ReviewTaskDatabase.path_transaction(self.reviews_path):
            return self._disable_merge_rule_locked(rule_id,task_fingerprints)

    def _disable_merge_rule_locked(
        self,rule_id: str,task_fingerprints: dict[str,str],
    ) -> ReviewSafeBatchResult:
        knowledge = self._knowledge()
        self._relationship_workspace(knowledge)
        discovery = DiscoveryRegistry(self.discovery_path)
        reviews = ReviewTaskDatabase(self.reviews_path)
        rule = knowledge.merge_rules.get(str(rule_id or ""))
        if rule is None:
            raise ReviewSafeBatchStaleError(
                "This cleanup rule is no longer available. Reload Review Center and try again."
            )
        matching = [
            task for task in reviews.tasks.values()
            if task.status == "pending" and task.task_type == "merge_suggestion"
            and str(task.details.get("rule_id") or "") == rule.id
        ]
        expected_ids = set(task_fingerprints)
        if expected_ids != {task.id for task in matching} or any(
            _task_fingerprint(task) != task_fingerprints.get(task.id)
            for task in matching
        ):
            raise ReviewSafeBatchStaleError(
                "The cleanup questions changed before the rule could be disabled. Reload Review Center and review the current scope."
            )
        before = self._fingerprints()
        directory, manifest, entries = self._create_backup(
            kind="review_disable_cleanup_rule", prefix="disable_cleanup_rule",
        )
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        try:
            if self._fingerprints() != before:
                raise ReviewSafeBatchStaleError(
                    "Review Center or System Knowledge changed before the cleanup rule could be disabled. Reload and try again."
                )
            rule.enabled = False
            for task in matching:
                task.details["resolution"] = {
                    "decision": "rule_disabled", "rule_id": rule.id,
                }
                reviews.set_status(task.id, "resolved")
            knowledge.save()
            reviews.save()
            self._verify_saved(knowledge, discovery, reviews)
            manifest_payload.update({
                "status": "complete", "rule_id": rule.id,
                "task_ids": [task.id for task in matching],
            })
            _write_json(manifest, manifest_payload)
        except Exception as exc:
            recovery_error = ""
            try:
                self._restore(directory, entries)
            except Exception as recovery_exc:
                recovery_error = str(recovery_exc)
            manifest_payload.update({
                "status": "recovery_failed" if recovery_error else "recovered",
                "error": str(exc),
            })
            if recovery_error:
                manifest_payload["recovery_error"] = recovery_error
            _write_json(manifest, manifest_payload)
            state = "Recovery needs attention" if recovery_error else "System Knowledge and Review outcomes were restored"
            raise RuntimeError(f"{state}. {exc} Recovery record: {manifest}") from exc
        return ReviewSafeBatchResult(
            knowledge, discovery, reviews, MergeBatchResult(), directory,
            0, len(matching),
        )


class ReviewTagDecisionBatchService(ReviewSafeBatchService):
    """Apply one explicit answer to compatible exact-tag Review questions."""

    def __init__(self, *args, excluded_path: str | Path, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.excluded_path = Path(excluded_path)

    @property
    def source_paths(self) -> tuple[Path, ...]:
        return (*super().source_paths,self.excluded_path)

    @staticmethod
    def _validate_request(
        request: ReviewTagDecisionRequest,
        knowledge: TagDatabase,
        reviews: ReviewTaskDatabase,
    ) -> tuple[list, object | None]:
        if request.choice not in {"keep", "existing", "exclude"}:
            raise ValueError("The shared Review decision is not supported.")
        if not request.task_ids or len(set(request.task_ids)) != len(request.task_ids):
            raise ValueError("Choose one or more distinct SDXL Tag questions.")
        tasks = []
        for task_id in request.task_ids:
            task = reviews.tasks.get(task_id)
            if (
                task is None or task.status != "pending" or task.task_type != "new_tag"
                or review_tag_selection_fingerprint(task) != request.task_fingerprints.get(task_id)
            ):
                raise ReviewSafeBatchStaleError(
                    "One of the selected SDXL Tag questions changed. Reload the Review queue and check the shared decision again."
                )
            tasks.append(task)
        target = knowledge.tags.get(request.target_id) if request.choice == "existing" else None
        if request.choice == "existing" and target is None:
            raise ReviewSafeBatchStaleError(
                "The selected preferred SDXL Tag is no longer available. Reload System Knowledge and choose it again."
            )
        if request.choice in {"keep", "exclude"}:
            categories = getattr(knowledge, "_categories", None)
            if categories is None or request.category_id not in categories.categories:
                raise ReviewSafeBatchStaleError(
                    "The selected category is no longer available. Reload System Knowledge and choose it again."
                )
        return tasks, target

    @staticmethod
    def _apply_task(
        task,
        suggestion: dict,
        request: ReviewTagDecisionRequest,
        target,
        knowledge: TagDatabase,
        discovery: DiscoveryRegistry,
        reviews: ReviewTaskDatabase,
        excluded_values: set[str],
    ) -> str:
        tag = str(task.details.get("tag") or "").strip()
        if not tag:
            raise ValueError("A selected Review question has no SDXL Tag name.")
        if request.choice == "existing":
            existing = knowledge.find(tag)
            if existing is not None and existing.id != target.id:
                raise ValueError(
                    f'"{tag}" already identifies "{existing.display}" and cannot also identify "{target.display}".'
                )
            if normalize_tag(tag) not in {normalize_tag(value) for value in target.aliases}:
                target.aliases.append(tag)
            knowledge._rebuild_lookup()
            task.details["resolution"] = {
                "decision": "use_existing", "target": target.display,
                "stable_id": target.id, "scope": "exact_sdxl_tag",
            }
        else:
            saved_tag=knowledge.add_known_tag(
                tag, category=request.category_id,
                concept_id=suggestion.get("concept_id"),
                properties=suggestion.get("properties") or {},
                seen_count=int(task.details.get("seen_count") or 0),
                provenance={"source": "qt_review", "reason": suggestion.get("reason") or ""},
            )
            if request.choice == "exclude":
                excluded_values.add(tag)
                task.details["resolution"] = {
                    "decision": "excluded_from_training_captions",
                    "stable_id": saved_tag.id,
                    "category": request.category_id,
                    "scope": "exact_sdxl_tag",
                }
            else:
                task.details["resolution"] = {
                    "decision": "kept_as_knowledge",
                    "stable_id": saved_tag.id,
                    "category": request.category_id,
                    "concept_id": suggestion.get("concept_id"),
                    "scope": "exact_sdxl_tag",
                }
        discovery.remove(tag)
        reviews.set_status(task.id, "resolved")
        return tag

    def apply_decision(self, request: ReviewTagDecisionRequest) -> ReviewTagDecisionResult:
        with (self.rule_pair_admission.admit() if self.rule_pair_admission is not None else nullcontext()), ReviewTaskDatabase.path_transaction(self.reviews_path):
            return self._apply_decision_locked(request)

    def _apply_decision_locked(
        self,request: ReviewTagDecisionRequest,
    ) -> ReviewTagDecisionResult:
        knowledge = self._knowledge()
        relationships=self._relationship_workspace(knowledge)
        alias_service=TagAliasRelationshipService(
            knowledge,default_path=self.default_relationships_path,
            user_path=self.user_relationships_path,workspace=relationships,
        )
        discovery = DiscoveryRegistry(self.discovery_path)
        reviews = ReviewTaskDatabase(self.reviews_path)
        tasks, target = self._validate_request(request, knowledge, reviews)
        reviewed_decisions=tuple(
            ReviewedKnowledgeDecision(
                task.id,(str(task.details.get("tag") or "").strip(),),
                dict(request.suggestions.get(task.id) or {}),
            )
            for task in tasks
        )
        if target is not None:
            plan=alias_service.preflight(tuple(
                TagAliasProposal(
                    str(task.details.get("tag") or ""),target.id,
                )
                for task in tasks
            ))
            if plan.blockers:raise TagAliasPreflightError(plan)
            target.aliases=list(alias_service.effective_for_tag(target.id).aliases)
        before = self._fingerprints()
        directory, manifest, entries = self._create_backup(
            kind="review_exact_tag_decision", prefix="tag_decision",
        )
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        excluded_values = set(
            self.excluded_path.read_text(encoding="utf-8").splitlines()
            if self.excluded_path.exists() else ()
        )
        names = []
        try:
            if self._fingerprints() != before:
                raise ReviewSafeBatchStaleError(
                    "Review Center or System Knowledge changed before the shared decision could start. Reload and try again."
                )
            for task in tasks:
                names.append(self._apply_task(
                    task, dict(request.suggestions.get(task.id) or {}), request, target,
                    knowledge, discovery, reviews, excluded_values,
                ))
            rerank=rerank_related_knowledge_questions(
                reviews,self._suggestion_engine(knowledge),reviewed_decisions,
            )
            if target is not None:
                user_values,_changed_ids,_affected=stage_tag_alias_changes(
                    knowledge,relationships,(TagAliasChange(
                        target.id,tuple(target.aliases),source="qt_review",
                        reason="Review Center exact Other Name decision",
                    ),),
                )
                relationships.user_store.replace(user_values.values())
                _write_json(
                    relationships.user_store.path,relationships.user_store.payload(),
                )
            knowledge.save()
            self._save_discovery(discovery)
            reviews.save()
            if request.choice == "exclude":
                payload = "\n".join(sorted(filter(None, excluded_values), key=str.casefold)) + "\n"
                _atomic_bytes(self.excluded_path, payload.encode("utf-8"))
            self._verify_saved(knowledge, discovery, reviews)
            if target is not None:
                saved_relationships=RelationshipStore(relationships.user_store.path)
                if saved_relationships.payload()!=relationships.user_store.payload():
                    raise RuntimeError("Other Name relationships failed read-back verification.")
            if request.choice == "exclude":
                saved = set(self.excluded_path.read_text(encoding="utf-8").splitlines())
                if saved != excluded_values:
                    raise RuntimeError("The training-caption exclusions failed read-back verification.")
            manifest_payload.update({
                "status": "complete", "choice": request.choice,
                "task_ids": list(request.task_ids), "tag_names": names,
                "related_pending_reranked": rerank.count,
                "related_pending_task_ids": list(rerank.task_ids),
            })
            _write_json(manifest, manifest_payload)
        except Exception as exc:
            recovery_error = ""
            try:
                self._restore(directory, entries)
            except Exception as recovery_exc:
                recovery_error = str(recovery_exc)
            manifest_payload.update({
                "status": "recovery_failed" if recovery_error else "recovered",
                "error": str(exc),
            })
            if recovery_error:
                manifest_payload["recovery_error"] = recovery_error
            _write_json(manifest, manifest_payload)
            state = "Recovery needs attention" if recovery_error else "System Knowledge and Review outcomes were restored"
            raise RuntimeError(f"{state}. {exc} Recovery record: {manifest}") from exc
        if target is not None:
            TagAliasRelationshipService(
                knowledge,default_path=self.default_relationships_path,
                user_path=self.user_relationships_path,
            )
        return ReviewTagDecisionResult(
            knowledge, discovery, reviews, directory, len(tasks), tuple(names),
            rerank.count,
        )
