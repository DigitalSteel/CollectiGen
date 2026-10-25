from __future__ import annotations

import copy
import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from .annotation import ImageAnnotation
from .automation_repository import active_automation_engine
from .conflict_rules import ConflictMatch, ConflictRuleEngine
from .constraint_resolver import EffectiveConstraintResolver
from .confirmed_tag_identity import (
    ConfirmedTagIdentityError,
    ConfirmedTagIdentityService,
)
from .image_fact_decisions import tag_addition_decision_issues
from .image_record import ImageRecord
from .operation_journal import JournalOperation, OperationJournal
from .related_tag_suggestions import (
    RelatedTagSuggestion,
    RelatedTagSuggestionService,
    add_related_tag_to_annotation,
)
from .tag_collection import TagCollection, normalize_tag
from .tag_database import TagDatabase, TagRecord


class ImageTagBatchStaleError(RuntimeError):
    """The Image Knowledge used by a bulk-tag preview changed before writing."""


@dataclass(frozen=True)
class ImageTagConflict:
    rule_id: str
    label: str
    message: str
    involved_tags: tuple[str, ...]


@dataclass
class ImageTagBatchImage:
    image: Path
    fingerprint: str
    status: str
    conflicts: tuple[ImageTagConflict, ...] = ()
    error: str = ""
    related_suggestions: dict[str, RelatedTagSuggestion] = field(default_factory=dict)
    related_conflicts: dict[str, tuple[ImageTagConflict, ...]] = field(
        default_factory=dict
    )


@dataclass
class ImageTagBatchRelated:
    key: str
    tag_id: str
    display: str
    relationship: str
    path: str
    scope: str
    source: str
    eligible_images: int = 0
    blocked_images: int = 0
    conflict_images: int = 0


@dataclass
class ImageTagBatchPreview:
    primary_tag_id: str
    primary_display: str
    images: list[ImageTagBatchImage] = field(default_factory=list)
    related: list[ImageTagBatchRelated] = field(default_factory=list)
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.images)

    @property
    def will_add(self) -> int:
        return self.count("will_add")

    @property
    def already_present(self) -> int:
        return self.count("already_present")

    @property
    def conflicts(self) -> int:
        return self.count("conflict")

    @property
    def unavailable_or_stale(self) -> int:
        return self.count("unavailable")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def actionable(self) -> int:
        return self.will_add + self.already_present

    @property
    def conflict_images(self) -> tuple[Path, ...]:
        return tuple(item.image for item in self.images if item.status == "conflict")


@dataclass(frozen=True)
class ImageTagBatchApplyItem:
    image: Path
    status: str
    added_tags: tuple[str, ...] = ()
    skipped_related_conflicts: tuple[str, ...] = ()
    error: str = ""
    version_id: str = ""


@dataclass
class ImageTagBatchApplyResult:
    operation_id: str = ""
    items: list[ImageTagBatchApplyItem] = field(default_factory=list)
    cancelled: bool = False
    history_operation_id: str = ""
    history_interrupted: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def changed(self) -> int:
        return self.count("changed")

    @property
    def already_present(self) -> int:
        return self.count("already_present")

    @property
    def conflicts_skipped(self) -> int:
        return self.count("conflict_skipped") + sum(
            len(item.skipped_related_conflicts) for item in self.items
        )

    @property
    def stale(self) -> int:
        return self.count("stale")

    @property
    def failed(self) -> int:
        return self.count("failed") + self.count("held") + self.count("interrupted")

    @property
    def not_processed(self) -> int:
        return self.count("not_processed")


def image_tag_batch_fingerprint(image: str | Path) -> str:
    """Fingerprint the image identity and exact portable Image Knowledge authority."""
    image = Path(image)
    digest = hashlib.sha256()
    try:
        stat = image.stat()
        digest.update(f"{image}:{stat.st_size}:{stat.st_mtime_ns}:".encode("utf-8"))
    except OSError:
        digest.update(f"{image}:missing:".encode("utf-8"))
    sidecar = image.with_suffix(".tags.json")
    try:
        digest.update(sidecar.read_bytes())
    except OSError:
        digest.update(b"sidecar:missing")
    return digest.hexdigest()


class ImageTagBatchService:
    """Preview, apply, and undo one stable-tag addition across many images.

    This service writes only ``.tags.json`` Image Knowledge. Training TXT,
    embedded Metadata, and file locations are deliberately outside its authority.
    """

    OPERATION_KIND = "image_tag_batch"
    UNDO_KIND = "image_tag_batch_undo"

    def __init__(
        self,
        knowledge: TagDatabase,
        conflicts: ConflictRuleEngine,
        journal: OperationJournal,
        *,
        categories=None,
        characters=None,
        constraint_resolver: EffectiveConstraintResolver | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.conflicts = conflicts
        self.journal = journal
        self.categories = categories
        self.characters = characters
        self.constraint_resolver = (
            constraint_resolver or EffectiveConstraintResolver.load(knowledge)
        )
        self.automation = active_automation_engine(knowledge)
        self.identities = ConfirmedTagIdentityService(knowledge)
        self.related_service = RelatedTagSuggestionService(
            knowledge,
            categories,
            characters,
            constraint_resolver=self.constraint_resolver,
        )

    def preview(
        self,
        images: Iterable[str | Path],
        primary_tag_id: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ImageTagBatchPreview:
        primary = self._tag(primary_tag_id)
        paths = tuple(dict.fromkeys(Path(value) for value in images))
        should_cancel = should_cancel or (lambda: False)
        result = ImageTagBatchPreview(primary.id, primary.display)
        related_by_key: dict[str, ImageTagBatchRelated] = {}

        for index, image in enumerate(paths, 1):
            if should_cancel():
                result.cancelled = True
                break
            item = self._preview_image(image, primary)
            result.images.append(item)
            if item.status in {"will_add", "already_present"}:
                for key, suggestion in item.related_suggestions.items():
                    aggregate = related_by_key.get(key)
                    if aggregate is None:
                        aggregate = ImageTagBatchRelated(
                            key=key,
                            tag_id=suggestion.tag_id,
                            display=suggestion.display,
                            relationship=suggestion.relationship_label,
                            path=suggestion.path_label,
                            scope=suggestion.scope,
                            source=suggestion.source,
                        )
                        related_by_key[key] = aggregate
                    if suggestion.eligible:
                        aggregate.eligible_images += 1
                    else:
                        aggregate.blocked_images += 1
                        if suggestion.conflict_tag_ids or suggestion.blocked_reason.startswith(
                            "Conflicts with"
                        ):
                            aggregate.conflict_images += 1
            if progress is not None:
                progress(index, len(paths), image.name)

        result.related = sorted(
            related_by_key.values(),
            key=lambda item: (
                item.scope.casefold(),
                item.display.casefold(),
                item.relationship.casefold(),
                item.key,
            ),
        )
        return result

    def apply(
        self,
        preview: ImageTagBatchPreview,
        *,
        related_keys: Iterable[str] = (),
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        history_operation=None,
    ) -> ImageTagBatchApplyResult:
        primary = self._tag(preview.primary_tag_id)
        selected_related = set(map(str, related_keys))
        should_cancel = should_cancel or (lambda: False)
        result = ImageTagBatchApplyResult()
        if history_operation is not None:
            result.history_operation_id = history_operation.operation_id
        candidates = [
            item
            for item in preview.images
            if item.status in {"will_add", "already_present"}
        ]
        if not candidates:
            result.items.extend(self._non_action_results(preview.images))
            return result

        operation_id = history_operation.operation_id if history_operation is not None else self.journal.begin(
            self.OPERATION_KIND,
            expected=len(candidates),
            reason=f"Add SDXL Tag {primary.display} to selected images",
            metadata={
                "primary_tag_id": primary.id,
                "primary_display": primary.display,
                "related_keys": sorted(selected_related),
                "writes": "image_knowledge_only",
            },
        )
        if history_operation is None:
            result.operation_id = operation_id
        terminal_status = "complete"

        for position, item in enumerate(preview.images):
            if item.status not in {"will_add", "already_present"}:
                result.items.append(self._non_action_result(item))
                continue
            if should_cancel():
                result.cancelled = True
                terminal_status = "cancelled"
                for remaining in preview.images[position:]:
                    if remaining.status in {"will_add", "already_present"}:
                        result.items.append(
                            ImageTagBatchApplyItem(
                                remaining.image,
                                "not_processed",
                                error="The batch was cancelled before this image was changed.",
                            )
                        )
                    else:
                        result.items.append(self._non_action_result(remaining))
                break
            if history_operation is None and image_tag_batch_fingerprint(item.image) != item.fingerprint:
                result.items.append(
                    ImageTagBatchApplyItem(
                        item.image,
                        "stale",
                        error="Image Knowledge changed after the preview.",
                    )
                )
                continue
            skipped_related_conflicts = tuple(
                item.related_suggestions[key].display
                for key in selected_related
                if key in item.related_conflicts
                and key in item.related_suggestions
            )
            try:
                added, version_id = self._apply_one(
                    operation_id,
                    item,
                    primary,
                    selected_related,
                    history_operation=history_operation,
                )
            except Exception as exc:
                from .image_knowledge_history import HistoryWriteInterrupted
                from .history_versions import HistoryError
                from .history_configuration import HistoryConfigurationError
                interrupted = isinstance(exc, HistoryWriteInterrupted)
                held = history_operation is not None and isinstance(exc, (HistoryError, HistoryConfigurationError, InterruptedError))
                result.items.append(ImageTagBatchApplyItem(item.image,
                    "interrupted" if interrupted else "stale" if isinstance(exc, ImageTagBatchStaleError) else "held" if held else "failed",
                    error=str(exc)))
                if held:
                    result.history_interrupted = interrupted
                    result.items.extend(ImageTagBatchApplyItem(remaining.image, "not_processed",
                        error="The batch stopped for explicit saved-history recovery or renewed admission.")
                        if remaining.status in {"will_add", "already_present"} else self._non_action_result(remaining)
                        for remaining in preview.images[position + 1:])
                    break
            else:
                result.items.append(
                    ImageTagBatchApplyItem(
                        item.image,
                        "changed" if added else "already_present",
                        tuple(added),
                        skipped_related_conflicts,
                        version_id=version_id,
                    )
                )
            if progress is not None:
                progress(len(result.items), len(preview.images), item.image.name)

        if history_operation is None:
            self.journal.finish(operation_id, terminal_status)
            self.journal.prune()
        return result

    def undo(self, operation_id: str) -> ImageTagBatchApplyResult:
        operation = self._operation(operation_id)
        if (
            operation.kind != self.OPERATION_KIND
            or operation.status not in {"complete", "cancelled"}
            or not operation.changes
        ):
            raise ValueError("That multi-image SDXL Tag change is not available to undo.")

        prepared = []
        for change in operation.changes:
            image = Path(change["item"])
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            try:
                current = ImageRecord(image).annotation.to_dict()
            except Exception as exc:
                raise ImageTagBatchStaleError(
                    f"{image.name} could not be checked before undo: {exc}"
                ) from exc
            if current != after:
                raise ImageTagBatchStaleError(
                    f"{image.name} changed after this batch, so the batch cannot be undone automatically."
                )
            prepared.append(
                (
                    image,
                    before,
                    after,
                    bool(change["metadata"].get("sidecar_existed", True)),
                )
            )

        undo_id = self.journal.begin(
            self.UNDO_KIND,
            expected=len(prepared),
            reason=f"Undo multi-image SDXL Tag batch {operation_id}",
            metadata={"restores": operation_id, "writes": "image_knowledge_only"},
        )
        restored = []
        result = ImageTagBatchApplyResult(operation_id=undo_id)
        try:
            for image, before, after, sidecar_existed in prepared:
                self._restore(image, before, sidecar_existed=sidecar_existed)
                self.journal.append_change(
                    undo_id,
                    item=str(image),
                    before=after,
                    after=before,
                    reason="Undo selected-image SDXL Tag addition",
                    metadata={"sidecar_existed": True},
                )
                restored.append((image, before, after, sidecar_existed))
                result.items.append(ImageTagBatchApplyItem(image, "changed"))
        except Exception as exc:
            rollback_errors = []
            for image, _before, after, _sidecar_existed in reversed(restored):
                try:
                    self._restore(image, after, sidecar_existed=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{image.name}: {rollback_exc}")
            message = str(exc)
            if rollback_errors:
                message += "; automatic recovery also failed: " + "; ".join(
                    rollback_errors
                )
            self.journal.finish(undo_id, "failed", message=message)
            raise RuntimeError(message) from exc
        self.journal.finish(undo_id, "complete")
        self.journal.finish(operation_id, "undone")
        self.journal.prune()
        return result

    def _preview_image(
        self, image: Path, primary: TagRecord
    ) -> ImageTagBatchImage:
        if not image.is_file():
            return ImageTagBatchImage(
                image, image_tag_batch_fingerprint(image), "unavailable",
                error="The image is unavailable.",
            )
        fingerprint = image_tag_batch_fingerprint(image)
        try:
            record = ImageRecord(image)
        except Exception as exc:
            return ImageTagBatchImage(
                image, fingerprint, "failed", error=str(exc)
            )
        try:
            self.identities.require_existing(
                record.annotation.all_structured_tags(),
                stored_ids=record.annotation.tag_identities,
                confirmation=False,
            )
        except ConfirmedTagIdentityError as exc:
            return ImageTagBatchImage(
                image,
                fingerprint,
                "failed",
                error=(
                    "Confirmed Tag Identity Repair is required before this "
                    f"image can join a multi-image tag change. {exc}"
                ),
            )
        present_ids = self._identity_ids(
            record.annotation.all_structured_tags(),
            stored_ids=record.annotation.tag_identities,
        )
        primary_present = primary.id in present_ids
        decision_conflicts = self._decision_conflicts(record, primary.display)
        conflicts = (
            tuple(
                item
                for item in decision_conflicts
                if ":confirmed_absent:" in item.rule_id
            )
            if primary_present
            else (
                *decision_conflicts,
                *self._prospective_conflicts(record, primary.display, primary.id),
            )
        )
        status = (
            "conflict"
            if conflicts
            else "already_present"
            if primary_present
            else "will_add"
        )
        related = {}
        related_conflicts = {}
        if status in {"will_add", "already_present"}:
            confirmed = TagCollection(self._unscoped_tags(record.annotation))
            if status == "will_add":
                confirmed.add(primary.display)
            rejected = record.annotation.source.get("rejected_tags", [])
            for suggestion in self.related_service.suggest(
                confirmed,
                relationship_roots=(primary.display,),
                confirmed_absent=record.annotation.confirmed_absent,
                rejected_tags=rejected,
                annotation=record.annotation,
            ):
                if suggestion.tag_id == primary.id:
                    continue
                if suggestion.owner_kind in {"character", "object", "context"}:
                    if (
                        primary.id not in suggestion.path_ids
                        and suggestion.supporting_tag_id != primary.id
                    ):
                        continue
                elif suggestion.tag_id in present_ids:
                    continue
                key = self._suggestion_key(suggestion)
                canonical_conflicts = self._prospective_conflicts(
                    record,
                    suggestion.display,
                    suggestion.tag_id,
                    suggestion=suggestion,
                )
                decision_conflicts = self._decision_conflicts(
                    record, suggestion.display
                )
                if decision_conflicts:
                    canonical_conflicts = (
                        *decision_conflicts,
                        *canonical_conflicts,
                    )
                if canonical_conflicts and not suggestion.blocked_reason:
                    names = ", ".join(
                        sorted(
                            {
                                tag
                                for conflict in canonical_conflicts
                                for tag in conflict.involved_tags
                                if normalize_tag(tag)
                                != normalize_tag(suggestion.display)
                            },
                            key=str.casefold,
                        )
                    )
                    suggestion = replace(
                        suggestion,
                        blocked_reason=(
                            f"Conflicts with confirmed tag: {names}"
                            if names
                            else "Conflicts with confirmed Image Knowledge"
                        ),
                    )
                if canonical_conflicts:
                    related_conflicts[key] = canonical_conflicts
                related[key] = suggestion
        return ImageTagBatchImage(
            image,
            fingerprint,
            status,
            conflicts,
            related_suggestions=related,
            related_conflicts=related_conflicts,
        )

    def _decision_conflicts(
        self, record: ImageRecord, display: str
    ) -> tuple[ImageTagConflict, ...]:
        return tuple(
            ImageTagConflict(
                rule_id=f"image_decision:{issue.kind}:{normalize_tag(issue.value)}",
                label="Reviewed image decision",
                message=issue.message,
                involved_tags=(issue.value,),
            )
            for issue in tag_addition_decision_issues(
                record.annotation,
                (display,),
                knowledge=self.knowledge,
                automation_engine=self.automation,
            )
        )

    def _apply_one(
        self,
        operation_id: str,
        item: ImageTagBatchImage,
        primary: TagRecord,
        selected_related: set[str],
        *, history_operation=None,
    ) -> tuple[list[str], str]:
        context = history_operation.context_for(item.image) if history_operation is not None else None
        if history_operation is not None and (context is None or context.operation_id != operation_id):
            from .history_versions import HistoryError
            raise HistoryError("The tag batch requires its exact admitted saved-history operation.")
        with context.locked(item.image) if context is not None else nullcontext():
            if context is not None:
                from .image_knowledge_history import HistoryWriteInterrupted
                if context.store.has_pending(context.entity):
                    raise HistoryWriteInterrupted("Reopen this image for saved-history recovery before adding Tags.")
            if image_tag_batch_fingerprint(item.image) != item.fingerprint:
                raise ImageTagBatchStaleError("Image Knowledge changed after the preview.")
            added = self._apply_current(operation_id, item, primary, selected_related, context)
            version_id = ""
            if context is not None and added:
                head = context.store.current_head(context.entity)
                if head is not None and head.operation_id == operation_id:
                    version_id = head.version_id
            return added, version_id

    def _apply_current(self, operation_id, item, primary, selected_related, context):
        record = ImageRecord(item.image, **({"history_context": context} if context is not None else {}))
        before = copy.deepcopy(record.annotation.to_dict())
        sidecar_existed = record.sidecar_path.exists()
        added = []
        if primary.id not in self._identity_ids(
            record.annotation.all_structured_tags(),
            stored_ids=record.annotation.tag_identities,
        ):
            if self._add_image_wide(record, primary.display):
                added.append(primary.display)
        for key in selected_related:
            suggestion = item.related_suggestions.get(key)
            if suggestion is None or not suggestion.eligible:
                continue
            if self._apply_suggestion(record, suggestion):
                added.append(suggestion.display)
        if not added:
            return []

        record.annotation.source["latest_operation"] = {
            "id": operation_id,
            "kind": self.OPERATION_KIND,
        }
        self.identities.synchronize_annotation(record.annotation)
        record.modified = True
        after = copy.deepcopy(record.annotation.to_dict())
        try:
            record.save_sidecar(force=True)
            if ImageRecord(item.image).annotation.to_dict() != after:
                raise RuntimeError(
                    "Saved Image Knowledge did not match the selected batch change."
                )
            if context is None:
                self.journal.append_change(
                    operation_id,
                    item=str(item.image),
                    before=before,
                    after=after,
                    reason=f"Add SDXL Tag {primary.display}",
                    metadata={
                        "primary_tag_id": primary.id,
                        "added_tags": added,
                        "sidecar_existed": sidecar_existed,
                    },
                )
        except Exception as exc:
            if context is None:
                self._restore(item.image, before, sidecar_existed=sidecar_existed)
            else:
                from .image_knowledge_history import HistoryWriteInterrupted
                raise HistoryWriteInterrupted("This image may have been saved. Reopen it for scoped history recovery before continuing the Tag batch. " + str(exc)) from exc
            raise
        return added

    def _apply_suggestion(
        self, record: ImageRecord, suggestion: RelatedTagSuggestion
    ) -> bool:
        changed = add_related_tag_to_annotation(record.annotation, suggestion)
        if changed:
            record.modified = True
        return changed

    @staticmethod
    def _add_image_wide(record: ImageRecord, display: str) -> bool:
        values = TagCollection(record.annotation.sdxl_tags)
        changed = values.add(display)
        if changed:
            record.annotation.sdxl_tags = values.to_list()
            absent = TagCollection(record.annotation.confirmed_absent)
            absent.remove(display)
            record.annotation.confirmed_absent = absent.to_list()
            record.modified = True
        return changed

    def _prospective_conflicts(
        self,
        record: ImageRecord,
        display: str,
        tag_id: str,
        *,
        suggestion: RelatedTagSuggestion | None = None,
    ) -> tuple[ImageTagConflict, ...]:
        baseline = {self._conflict_key(match) for match in self.conflicts.evaluate(record)}
        trial = ImageRecord(record.image_path, auto_load=False)
        trial.annotation = ImageAnnotation.from_dict(
            copy.deepcopy(record.annotation.to_dict()), record.image_path.name
        )
        trial.loaded = True
        if suggestion is None:
            self._add_image_wide(trial, display)
        else:
            self._apply_suggestion(trial, suggestion)
        introduced = [
            match
            for match in self.conflicts.evaluate(trial)
            if self._conflict_key(match) not in baseline
        ]
        result = [self._conflict_detail(match) for match in introduced]

        target = self.knowledge.tags.get(tag_id)
        if target is not None:
            present_values = (
                self._scoped_conflict_tags(record, suggestion)
                if (
                    suggestion is not None
                    and suggestion.owner_kind in {"character", "object", "context"}
                )
                else record.annotation.all_structured_tags()
            )
            present_ids = sorted(
                self._identity_ids(
                    present_values,
                    stored_ids=record.annotation.tag_identities,
                )
            )
            scopes = ["image"]
            if suggestion is not None and suggestion.owner_kind == "character":
                scopes.append("same_character")
            elif suggestion is not None and suggestion.owner_kind == "object":
                scopes.append("same_object")
            bound_relationship_ids = {
                rule.relationship_id
                for rule in self.conflicts.rules
                if rule.relationship_id
            }
            involved_keys = {
                frozenset(normalize_tag(value) for value in item.involved_tags)
                for item in result
            }
            for scope in scopes:
                for conflict in self.constraint_resolver.conflicts_with(
                    target.id,
                    present_ids,
                    scope=scope,
                ):
                    if (
                        target.id not in conflict.tag_ids
                        or conflict.relationship_id in bound_relationship_ids
                    ):
                        continue
                    involved = tuple(
                        sorted(
                            (
                                self.knowledge.tags[tag_id].display
                                for tag_id in conflict.tag_ids
                                if tag_id in self.knowledge.tags
                            ),
                            key=str.casefold,
                        )
                    )
                    involved_key = frozenset(
                        normalize_tag(value) for value in involved
                    )
                    if len(involved) < 2 or involved_key in involved_keys:
                        continue
                    involved_keys.add(involved_key)
                    label = conflict.label or "Reviewed SDXL Tag conflict"
                    result.append(
                        ImageTagConflict(
                            conflict.relationship_id,
                            label,
                            f"{label}: " + ", ".join(involved) + ".",
                            involved,
                        )
                    )
        return tuple(result)

    @staticmethod
    def _image_wide_tags(annotation: ImageAnnotation) -> list[str]:
        values = TagCollection()
        containers = [
            annotation.sdxl_tags,
            annotation.uncategorized,
            annotation.visual_style.style_tags,
        ]
        containers.extend(annotation.global_tags.values())
        containers.extend(annotation.scene.values())
        containers.extend(annotation.custom_groups.values())
        for container in containers:
            values.extend(container)
        return values.to_list()

    @staticmethod
    def _unscoped_tags(annotation: ImageAnnotation) -> list[str]:
        return TagCollection(
            [*annotation.sdxl_tags, *annotation.uncategorized]
        ).to_list()

    def _scoped_conflict_tags(
        self,
        record: ImageRecord,
        suggestion: RelatedTagSuggestion,
    ) -> list[str]:
        if suggestion.owner_kind == "context":
            context=record.annotation.get_context(suggestion.owner_id)
            return context.all_tags() if context is not None else []
        values = TagCollection(self._image_wide_tags(record.annotation))
        owners = (
            record.annotation.characters
            if suggestion.owner_kind == "character"
            else record.annotation.objects
            if suggestion.owner_kind == "object"
            else ()
        )
        owner = next(
            (
                assignment
                for assignment in owners
                if assignment.id == suggestion.owner_id
            ),
            None,
        )
        if owner is not None:
            values.extend(owner.all_tags())
        return values.to_list()

    @staticmethod
    def _conflict_key(match: ConflictMatch) -> tuple:
        return (
            match.rule.id,
            match.character_slot,
            bool(match.ambiguous),
            str(getattr(match,"context_id","") or ""),
            tuple(sorted(normalize_tag(value) for value in match.found_tags)),
        )

    @staticmethod
    def _conflict_detail(match: ConflictMatch) -> ImageTagConflict:
        return ImageTagConflict(
            match.rule.id,
            match.rule.label or "Conflicting SDXL Tags",
            match.message,
            tuple(match.found_tags),
        )

    def _identity_ids(
        self,
        values: Iterable[str],
        *,
        stored_ids: dict[str, str] | None = None,
    ) -> set[str]:
        resolution = self.identities.require_existing(
            values,
            stored_ids=stored_ids,
            confirmation=False,
        )
        return set(resolution.stable_ids)

    @staticmethod
    def _suggestion_key(suggestion: RelatedTagSuggestion) -> str:
        scope = suggestion.scope.casefold().strip()
        return "|".join(
            (
                suggestion.tag_id,
                suggestion.owner_kind,
                suggestion.owner_id,
                suggestion.owner_group,
                scope,
                ">".join(suggestion.path_ids),
                ">".join(suggestion.relation_kinds),
            )
        )

    def _tag(self, tag_id: str) -> TagRecord:
        tag = self.knowledge.tags.get(str(tag_id))
        if tag is None:
            raise ValueError("Choose one existing System Knowledge SDXL Tag.")
        return tag

    @staticmethod
    def _non_action_result(item: ImageTagBatchImage) -> ImageTagBatchApplyItem:
        status = {
            "conflict": "conflict_skipped",
            "unavailable": "stale",
            "failed": "failed",
        }.get(item.status, "already_present")
        return ImageTagBatchApplyItem(item.image, status, error=item.error)

    def _non_action_results(
        self, items: Iterable[ImageTagBatchImage]
    ) -> list[ImageTagBatchApplyItem]:
        return [self._non_action_result(item) for item in items]

    def _operation(self, operation_id: str) -> JournalOperation:
        operation = next(
            (item for item in self.journal.operations() if item.id == operation_id),
            None,
        )
        if operation is None:
            raise ValueError("The requested Image Knowledge operation is unavailable.")
        return operation

    @staticmethod
    def _restore(
        image: Path,
        snapshot: dict,
        *,
        sidecar_existed: bool,
    ) -> None:
        sidecar = image.with_suffix(".tags.json")
        if not sidecar_existed:
            sidecar.unlink(missing_ok=True)
            return
        record = ImageRecord(image, auto_load=False)
        record.annotation = ImageAnnotation.from_dict(
            copy.deepcopy(snapshot), image.name
        )
        record.loaded = True
        record.modified = True
        record.save_sidecar(force=True)
        if ImageRecord(image).annotation.to_dict() != snapshot:
            raise RuntimeError(
                f"{image.name} could not be restored to its exact prior Image Knowledge."
            )
