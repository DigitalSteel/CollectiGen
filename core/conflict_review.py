from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .annotation import ImageAnnotation
from .conflict_rules import ConflictMatch, ConflictRule, ConflictRuleEngine
from .image_record import ImageRecord
from .operation_journal import OperationJournal
from .tag_collection import normalize_tag


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConflictReviewStaleError(RuntimeError):
    """The Image Knowledge or exact conflict changed after queue preview."""


@dataclass(frozen=True)
class ConflictReviewEntry:
    image: Path
    fingerprint: str
    match_identity: str
    rule_id: str
    relationship_id: str
    label: str
    scope: str
    found_tags: tuple[str, ...]
    message: str
    ambiguous: bool = False
    character_slot: int | None = None
    object_slot: int | None = None
    context_id: str = ""
    context_label: str = ""
    owner_kind: str = ""
    owner_id: str = ""
    owner_label: str = ""
    provenance: str = ""
    rule_version: int = 1
    allow_when: str = "No automatic exceptions"
    accepted: bool = False
    accepted_reason: str = ""
    deferred: bool = False
    deferred_reason: str = ""


@dataclass
class ConflictReviewQueueResult:
    entries: list[ConflictReviewEntry] = field(default_factory=list)
    scanned: int = 0
    matching: int = 0
    existing_exceptions: int = 0
    deferred: int = 0
    errors: int = 0
    cancelled: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ConflictReviewDecisionResult:
    operation_id: str
    image: Path
    rule_id: str
    decision: str
    history_operation_id: str = ""
    items: tuple["ConflictReviewDecisionItem", ...] = ()
    cancelled: bool = False
    history_interrupted: bool = False


@dataclass(frozen=True)
class ConflictReviewDecisionItem:
    image: Path
    status: str
    version_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class ConflictReviewBatchDecisionResult:
    operation_id: str
    images: tuple[Path, ...]
    rule_id: str
    decision: str
    history_operation_id: str = ""
    items: tuple[ConflictReviewDecisionItem, ...] = ()
    cancelled: bool = False
    history_interrupted: bool = False


class ConflictReviewService:
    """Build and apply review-only per-image diagnostic-conflict decisions.

    This service writes only portable Image Knowledge sidecars. It never edits
    Training TXT, embedded Metadata, source images, reusable rules, or file
    locations.
    """

    OPERATION_KIND = "conflict_review"
    REVIEW_VERSION = 1

    def __init__(
        self,
        conflicts: ConflictRuleEngine,
        journal: OperationJournal,
        *,
        knowledge=None,
    ) -> None:
        self.conflicts = conflicts
        self.journal = journal
        self.knowledge = knowledge

    @staticmethod
    def fingerprint(image: str | Path) -> str:
        image = Path(image)
        digest = hashlib.sha256()
        try:
            stat = image.stat()
            digest.update(f"{image}:{stat.st_size}:{stat.st_mtime_ns}:".encode("utf-8"))
        except OSError:
            digest.update(f"{image}:missing:".encode("utf-8"))
        try:
            digest.update(image.with_suffix(".tags.json").read_bytes())
        except OSError:
            digest.update(b"sidecar:missing")
        return digest.hexdigest()

    @staticmethod
    def _canonical_id(rule: ConflictRule) -> str:
        return str(rule.relationship_id or rule.id)

    @classmethod
    def _match_identity(cls, match: ConflictMatch) -> str:
        payload = (
            cls._canonical_id(match.rule),
            match.rule.scope,
            match.character_slot,
            match.object_slot,
            str(match.context_id or ""),
            str(match.owner_kind or ""),
            str(match.owner_id or ""),
            tuple(sorted(normalize_tag(value) for value in match.found_tags)),
        )
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _accepted_value_id(value) -> str:
        if isinstance(value, dict):
            return str(value.get("rule_id") or value.get("id") or "")
        return str(value or "")

    @classmethod
    def _accepted_ids(cls, record: ImageRecord) -> set[str]:
        return {
            rule_id
            for value in record.annotation.source.get("accepted_conflict_rules", ())
            if (rule_id := cls._accepted_value_id(value))
        }

    @classmethod
    def _review_payload(cls, annotation, *, create: bool = False) -> dict:
        source = annotation.source
        payload = source.get("conflict_review")
        if not isinstance(payload, dict):
            if not create:
                return {}
            payload = {}
            source["conflict_review"] = payload
        payload["version"] = cls.REVIEW_VERSION
        if create and not isinstance(payload.get("deferred"), dict):
            payload["deferred"] = {}
        return payload

    def _rule(self, rule_id: str) -> ConflictRule | None:
        rule_id = str(rule_id or "")
        return next(
            (rule for rule in self.conflicts.rules if rule_id in rule.accepted_ids),
            None,
        )

    def _relationship(self, rule: ConflictRule):
        evaluator = self.conflicts.relationship_evaluator
        if evaluator is None or not rule.relationship_id:
            return None
        return next(
            (
                item for item in evaluator.registry.relationships
                if item.id == rule.relationship_id
            ),
            None,
        )

    def _entry(
        self,
        record: ImageRecord,
        match: ConflictMatch,
    ) -> ConflictReviewEntry:
        rule = match.rule
        canonical_id = self._canonical_id(rule)
        accepted_ids = self._accepted_ids(record)
        accepted = bool(accepted_ids.intersection(rule.accepted_ids))
        details = record.annotation.source.get("accepted_conflict_rule_details")
        details = details if isinstance(details, dict) else {}
        accepted_detail = next(
            (
                details.get(rule_id) for rule_id in (canonical_id, *rule.accepted_ids)
                if isinstance(details.get(rule_id), dict)
            ),
            {},
        )
        review = self._review_payload(record.annotation)
        deferred_map = review.get("deferred")
        deferred_map = deferred_map if isinstance(deferred_map, dict) else {}
        deferred_detail = next(
            (
                deferred_map.get(rule_id)
                for rule_id in (canonical_id, *rule.accepted_ids)
                if isinstance(deferred_map.get(rule_id), dict)
            ),
            {},
        )
        relationship = self._relationship(rule)
        provenance = (
            f"{relationship.source} / {relationship.layer}"
            if relationship is not None else "legacy diagnostic definition"
        )
        version = int(getattr(relationship, "version", 1) or 1)
        return ConflictReviewEntry(
            image=record.image_path,
            fingerprint=self.fingerprint(record.image_path),
            match_identity=self._match_identity(match),
            rule_id=rule.id,
            relationship_id=canonical_id,
            label=rule.label or "Contrary Values",
            scope=rule.scope,
            found_tags=tuple(match.found_tags),
            message=match.message,
            ambiguous=bool(match.ambiguous),
            character_slot=match.character_slot,
            object_slot=match.object_slot,
            context_id=str(match.context_id or ""),
            context_label=str(match.context_label or ""),
            owner_kind=str(match.owner_kind or ""),
            owner_id=str(match.owner_id or ""),
            owner_label=str(match.owner_label or ""),
            provenance=provenance,
            rule_version=version,
            allow_when=rule.allow_when.summary(
                lambda tag_id: (
                    self.knowledge.tags[tag_id].display
                    if self.knowledge is not None
                    and tag_id in getattr(self.knowledge, "tags", {})
                    else tag_id
                )
            ),
            accepted=accepted,
            accepted_reason=str(accepted_detail.get("reason") or ""),
            deferred=bool(deferred_detail),
            deferred_reason=str(deferred_detail.get("reason") or ""),
        )

    def entries_for_image(
        self,
        image: str | Path,
        *,
        rule_id: str = "",
    ) -> tuple[ConflictReviewEntry, ...]:
        record = ImageRecord(Path(image))
        entries = []
        for match in self.conflicts.evaluate(
            record,
            include_accepted=True,
            rule_ids=((rule_id,) if rule_id else None),
        ):
            entries.append(self._entry(record, match))
        return tuple(entries)

    def build_queue(
        self,
        images: Iterable[str | Path],
        *,
        rule_id: str = "",
        include_accepted: bool = False,
        include_deferred: bool = False,
        max_entries: int = 5000,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ConflictReviewQueueResult:
        paths = tuple(dict.fromkeys(Path(value) for value in images))
        should_cancel = should_cancel or (lambda: False)
        result = ConflictReviewQueueResult()
        progress_interval = max(1, len(paths) // 200)
        for index, image in enumerate(paths, 1):
            if should_cancel():
                result.cancelled = True
                break
            result.scanned += 1
            try:
                entries = self.entries_for_image(image, rule_id=rule_id)
            except Exception:
                result.errors += 1
                entries = ()
            for entry in entries:
                result.matching += 1
                if entry.accepted and not include_accepted:
                    result.existing_exceptions += 1
                    continue
                if entry.deferred and not include_deferred:
                    result.deferred += 1
                    continue
                if len(result.entries) < max(1, int(max_entries)):
                    result.entries.append(entry)
                else:
                    result.truncated = True
            if progress is not None and (
                index == 1 or index == len(paths) or index % progress_interval == 0
            ):
                progress(index, len(paths), image.name)
        return result

    def _verify_entry(self, entry: ConflictReviewEntry, *, history_context=None) -> ImageRecord:
        if self.fingerprint(entry.image) != entry.fingerprint:
            raise ConflictReviewStaleError(
                "Image Knowledge changed after this conflict was queued. Refresh "
                "the item before recording a decision."
            )
        record = ImageRecord(entry.image, history_context=history_context)
        current = self.entries_for_image(entry.image, rule_id=entry.rule_id)
        if not any(item.match_identity == entry.match_identity for item in current):
            raise ConflictReviewStaleError(
                "The exact conflict changed after this item was queued. Rebuild "
                "the queue before recording a decision."
            )
        return record

    def _restore_snapshot(
        self,
        image: Path,
        snapshot: dict,
        *,
        expected_current: dict | None = None,
    ) -> None:
        restored = ImageRecord(image)
        if (
            expected_current is not None
            and restored.annotation.to_dict() != expected_current
        ):
            raise ConflictReviewStaleError(
                "Image Knowledge changed after this conflict operation. The "
                "newer record was preserved instead of restoring over it."
            )
        restored.annotation = ImageAnnotation.from_dict(
            copy.deepcopy(snapshot), image.name
        )
        restored.modified = True
        restored.save_sidecar(force=True, knowledge=self.knowledge)
        if ImageRecord(image).annotation.to_dict() != snapshot:
            raise RuntimeError(
                "The restored Image Knowledge did not match the journaled snapshot."
            )

    @staticmethod
    def _unique_entries(
        entries: Iterable[ConflictReviewEntry],
    ) -> tuple[ConflictReviewEntry, ...]:
        result = []
        seen = set()
        for entry in entries:
            key = (str(entry.image).casefold(), entry.relationship_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(entry)
        if not result:
            raise ValueError("Select at least one conflict finding.")
        if len({entry.relationship_id for entry in result}) != 1:
            raise ValueError(
                "Selected conflict findings must use one canonical rule authority."
            )
        if len({entry.accepted for entry in result}) != 1:
            raise ValueError(
                "Selected conflict findings must use one accepted or active state."
            )
        return tuple(result)

    def _mutate_many(
        self,
        entries: Iterable[ConflictReviewEntry],
        decision: str,
        mutate: Callable[[ImageRecord, ConflictReviewEntry], None],
        *,
        history_operation=None,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ConflictReviewBatchDecisionResult:
        selected = self._unique_entries(entries)
        if history_operation is not None:
            return self._mutate_with_history(
                selected, decision, mutate, history_operation,
                should_cancel=should_cancel, progress=progress,
            )
        prepared = []
        for entry in selected:
            record = self._verify_entry(entry)
            prepared.append(
                (entry, record, copy.deepcopy(record.annotation.to_dict()))
            )
        relationship_id = selected[0].relationship_id
        operation_id = self.journal.begin(
            self.OPERATION_KIND,
            expected=len(prepared),
            reason=(
                f"Conflict {relationship_id}: {decision} on "
                f"{len(prepared):,} image(s)"
            ),
            metadata={
                "images": [str(entry.image) for entry in selected],
                "rule_id": selected[0].rule_id,
                "relationship_id": relationship_id,
                "decision": decision,
                "writes": "image_knowledge_only",
            },
        )
        written = []
        try:
            for entry, record, before in prepared:
                mutate(record, entry)
                record.annotation.source["latest_operation"] = {
                    "id": operation_id,
                    "kind": self.OPERATION_KIND,
                }
                record.modified = True
                record.save_sidecar(force=True, knowledge=self.knowledge)
                after = copy.deepcopy(record.annotation.to_dict())
                if ImageRecord(entry.image).annotation.to_dict() != after:
                    raise RuntimeError(
                        "Saved Image Knowledge did not match the reviewed "
                        f"conflict decision for {entry.image.name}."
                    )
                written.append((entry, before, after))
                self.journal.append_change(
                    operation_id,
                    item=str(entry.image),
                    before=before,
                    after=after,
                    reason=f"{entry.relationship_id}: {decision}",
                    metadata={
                        "rule_id": entry.rule_id,
                        "relationship_id": entry.relationship_id,
                        "decision": decision,
                    },
                )
            self.journal.finish(operation_id)
        except Exception as exc:
            rollback_errors = []
            for entry, before, after in reversed(written):
                try:
                    self._restore_snapshot(
                        entry.image,
                        before,
                        expected_current=after,
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(f"{entry.image}: {rollback_exc}")
            if rollback_errors:
                message = f"{exc}; rollback failed: {'; '.join(rollback_errors)}"
                self.journal.finish(operation_id, "failed", message=message)
                raise RuntimeError(
                    "The conflict decision failed and automatic Image Knowledge "
                    f"recovery also failed: {'; '.join(rollback_errors)}"
                ) from exc
            self.journal.finish(operation_id, "failed", message=str(exc))
            raise
        self.journal.prune()
        return ConflictReviewBatchDecisionResult(
            operation_id,
            tuple(entry.image for entry in selected),
            relationship_id,
            decision,
        )

    def _mutate_with_history(
        self, selected, decision, mutate, operation, *, should_cancel=None, progress=None,
    ) -> ConflictReviewBatchDecisionResult:
        """Commit independent images; never roll a native interruption back blindly."""
        from .history_workflow import HistoryCaptureCancelled
        from .image_knowledge_history import HistoryWriteInterrupted
        from .history_versions import HistoryError

        should_cancel = should_cancel or (lambda: False)
        items = []
        cancelled = interrupted = False
        for position, entry in enumerate(selected):
            if should_cancel():
                cancelled = True
                break
            try:
                context = operation.context_for(entry.image)
                if context is None or context.operation_id != operation.operation_id:
                    raise HistoryError("The exact conflict history operation is unavailable.")
                with context.locked(entry.image):
                    if context.store.has_pending(context.entity):
                        raise HistoryWriteInterrupted(
                            "This image needs saved-history recovery before another conflict decision."
                        )
                    record = self._verify_entry(entry, history_context=context)
                    mutate(record, entry)
                    record.annotation.source["latest_operation"] = {
                        "id": operation.operation_id, "kind": self.OPERATION_KIND,
                    }
                    record.modified = True
                    try:
                        record.save_sidecar(force=True, knowledge=self.knowledge)
                        if ImageRecord(entry.image).annotation.to_dict() != record.annotation.to_dict():
                            raise RuntimeError("Saved conflict decision did not match its read-back.")
                        head = context.store.current_head(context.entity)
                        if head is None or head.operation_id != operation.operation_id:
                            raise RuntimeError("The exact saved conflict version could not be verified.")
                    except Exception as exc:
                        raise HistoryWriteInterrupted(
                            "The conflict decision may have been saved. Review this exact image's "
                            f"history and recovery before continuing: {exc}"
                        ) from exc
                    items.append(ConflictReviewDecisionItem(entry.image, "complete", head.version_id))
            except Exception as exc:
                cancelled = isinstance(exc, HistoryCaptureCancelled)
                interrupted = isinstance(exc, HistoryWriteInterrupted)
                status = "interrupted" if interrupted else "stale" if isinstance(exc, ConflictReviewStaleError) else "held"
                items.append(ConflictReviewDecisionItem(entry.image, status, error=str(exc)))
                break
            if progress is not None:
                progress(position + 1, len(selected), entry.image.name)
        items.extend(ConflictReviewDecisionItem(
            entry.image, "not_processed", error="Cancelled before this image." if cancelled else
            "Stopped before this image; review the preceding outcome before retrying.",
        ) for entry in selected[len(items):])
        return ConflictReviewBatchDecisionResult(
            "", tuple(item.image for item in items if item.status == "complete"),
            selected[0].relationship_id, decision, operation.operation_id,
            tuple(items), cancelled, interrupted,
        )

    @staticmethod
    def _reason(value: str, *, required: bool) -> str:
        reason = str(value or "").strip()
        if required and not reason:
            raise ValueError(
                "Explain why these values are valid together on this image."
            )
        if len(reason) > 1000:
            raise ValueError("Conflict-review reasons are limited to 1,000 characters.")
        return reason

    def _accept_record(
        self,
        record: ImageRecord,
        entry: ConflictReviewEntry,
        reason: str,
        rule: ConflictRule,
    ) -> None:
        kept = [
            value
            for value in record.annotation.source.get(
                "accepted_conflict_rules", ()
            )
            if self._accepted_value_id(value) not in rule.accepted_ids
        ]
        kept.append(entry.relationship_id)
        record.annotation.source["accepted_conflict_rules"] = kept
        details = record.annotation.source.get("accepted_conflict_rule_details")
        details = dict(details) if isinstance(details, dict) else {}
        for rule_id in rule.accepted_ids:
            details.pop(rule_id, None)
        details[entry.relationship_id] = {
            "reason": reason,
            "reviewed_at": _utc_now(),
            "provenance": "user_conflict_review",
            "rule_id": entry.rule_id,
            "relationship_id": entry.relationship_id,
            "rule_version": entry.rule_version,
        }
        record.annotation.source["accepted_conflict_rule_details"] = details
        review = self._review_payload(record.annotation, create=True)
        for rule_id in rule.accepted_ids:
            review["deferred"].pop(rule_id, None)

    def accept_exceptions(
        self,
        entries: Iterable[ConflictReviewEntry],
        reason: str,
        **options,
    ) -> ConflictReviewBatchDecisionResult:
        reason = self._reason(reason, required=True)
        selected = self._unique_entries(entries)
        rules = {}
        for entry in selected:
            rule = self._rule(entry.rule_id)
            if rule is None or (rule.relationship_id or rule.id) != entry.relationship_id:
                raise ConflictReviewStaleError(
                    "The canonical conflict rule is no longer active."
                )
            rules[entry.rule_id] = rule
        return self._mutate_many(
            selected,
            "accepted_exception",
            lambda record, entry: self._accept_record(
                record, entry, reason, rules[entry.rule_id]
            ),
            **options,
        )

    def accept_exception(
        self,
        entry: ConflictReviewEntry,
        reason: str,
        **options,
    ) -> ConflictReviewDecisionResult:
        result = self.accept_exceptions((entry,), reason, **options)
        return ConflictReviewDecisionResult(
            result.operation_id, entry.image, entry.relationship_id,
            result.decision,
            result.history_operation_id, result.items, result.cancelled, result.history_interrupted,
        )

    def defer(
        self,
        entry: ConflictReviewEntry,
        reason: str = "",
        **options,
    ) -> ConflictReviewDecisionResult:
        result = self.defer_many((entry,), reason, **options)
        return ConflictReviewDecisionResult(
            result.operation_id, entry.image, entry.relationship_id,
            result.decision,
            result.history_operation_id, result.items, result.cancelled, result.history_interrupted,
        )

    def defer_many(
        self,
        entries: Iterable[ConflictReviewEntry],
        reason: str = "",
        **options,
    ) -> ConflictReviewBatchDecisionResult:
        reason = self._reason(reason, required=False)

        def defer_conflict(
            record: ImageRecord, entry: ConflictReviewEntry,
        ) -> None:
            review = self._review_payload(record.annotation, create=True)
            review["deferred"][entry.relationship_id] = {
                "reason": reason,
                "reviewed_at": _utc_now(),
                "provenance": "user_conflict_review",
                "rule_id": entry.rule_id,
                "relationship_id": entry.relationship_id,
                "rule_version": entry.rule_version,
            }

        return self._mutate_many(entries, "deferred", defer_conflict, **options)

    def clear_exception(
        self,
        entry: ConflictReviewEntry,
        **options,
    ) -> ConflictReviewDecisionResult:
        result = self.clear_exceptions((entry,), **options)
        return ConflictReviewDecisionResult(
            result.operation_id, entry.image, entry.relationship_id,
            result.decision,
            result.history_operation_id, result.items, result.cancelled, result.history_interrupted,
        )

    def clear_exceptions(
        self,
        entries: Iterable[ConflictReviewEntry],
        **options,
    ) -> ConflictReviewBatchDecisionResult:
        selected = self._unique_entries(entries)
        rules = {}
        for entry in selected:
            rule = self._rule(entry.rule_id)
            if rule is None or (rule.relationship_id or rule.id) != entry.relationship_id:
                raise ConflictReviewStaleError(
                    "The canonical conflict rule is no longer active."
                )
            rules[entry.rule_id] = rule

        def clear(record: ImageRecord, entry: ConflictReviewEntry) -> None:
            rule = rules[entry.rule_id]
            record.annotation.source["accepted_conflict_rules"] = [
                value
                for value in record.annotation.source.get(
                    "accepted_conflict_rules", ()
                )
                if self._accepted_value_id(value) not in rule.accepted_ids
            ]
            details = record.annotation.source.get("accepted_conflict_rule_details")
            details = dict(details) if isinstance(details, dict) else {}
            for rule_id in rule.accepted_ids:
                details.pop(rule_id, None)
            if details:
                record.annotation.source["accepted_conflict_rule_details"] = details
            else:
                record.annotation.source.pop("accepted_conflict_rule_details", None)
            review = self._review_payload(record.annotation)
            deferred = review.get("deferred")
            if isinstance(deferred, dict):
                for rule_id in rule.accepted_ids:
                    deferred.pop(rule_id, None)

        return self._mutate_many(selected, "reopened_exception", clear, **options)

    def undo(self, operation_id: str) -> Path | tuple[Path, ...]:
        operation = next(
            (
                item for item in self.journal.operations()
                if item.id == operation_id
            ),
            None,
        )
        if (
            operation is None
            or operation.kind != self.OPERATION_KIND
            or operation.status != "complete"
            or not operation.changes
        ):
            raise ValueError("That conflict-review decision is not available to undo.")
        snapshots = []
        for change in operation.changes:
            image = Path(change["item"])
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            if ImageRecord(image).annotation.to_dict() != after:
                raise ConflictReviewStaleError(
                    "Image Knowledge changed after this conflict decision, so "
                    "the selection cannot be undone automatically. Open the "
                    "universal editor to review the current records."
                )
            snapshots.append((image, before, after))
        restored = []
        try:
            for image, before, after in snapshots:
                self._restore_snapshot(
                    image,
                    before,
                    expected_current=after,
                )
                restored.append((image, before, after))
        except Exception as exc:
            recovery_errors = []
            for image, before, after in reversed(restored):
                try:
                    self._restore_snapshot(
                        image,
                        after,
                        expected_current=before,
                    )
                except Exception as rollback_exc:
                    recovery_errors.append(f"{image}: {rollback_exc}")
            if recovery_errors:
                raise RuntimeError(
                    "Undo failed and automatic recovery also failed: "
                    f"{'; '.join(recovery_errors)}"
                ) from exc
            if isinstance(exc, ConflictReviewStaleError):
                raise ConflictReviewStaleError(
                    "Image Knowledge changed while undo was running. The newer "
                    "record was preserved and any earlier restores were recovered."
                ) from exc
            raise RuntimeError(
                "Undo could not be completed. Earlier restores were recovered, "
                "so the reviewed conflict decision remains current."
            ) from exc
        self.journal.finish(operation_id, "undone")
        images = tuple(image for image, _before, _after in snapshots)
        return images[0] if len(images) == 1 else images
