from __future__ import annotations

import copy
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .annotation import ImageAnnotation
from .confirmed_tag_identity import (
    ConfirmedTagIdentityError,
    ConfirmedTagIdentityService,
)
from .image_record import ImageRecord
from .image_tag_batch import image_tag_batch_fingerprint
from .operation_journal import OperationJournal
from .typed_boundary import typed_tag_sources
from .history_configuration import HistoryConfigurationError
from .history_versions import HistoryEntityRef, HistoryError, HistoryStore, read_record, record_token
from .image_knowledge_history import HistoryWriteInterrupted


class ImageTagRemovalStaleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageTagRemovalTarget:
    stable_id: str
    display: str


@dataclass(frozen=True)
class ImageTagRemovalItem:
    image: Path
    fingerprint: str
    status: str
    values: tuple[str, ...] = ()
    error: str = ""


@dataclass
class ImageTagRemovalPreview:
    targets: tuple[ImageTagRemovalTarget, ...]
    items: list[ImageTagRemovalItem] = field(default_factory=list)
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def will_remove(self) -> int:
        return self.count("will_remove")

    @property
    def already_absent(self) -> int:
        return self.count("already_absent")

    @property
    def unavailable(self) -> int:
        return self.count("unavailable")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def removable_facts(self) -> int:
        return sum(len(item.values) for item in self.items if item.status == "will_remove")

    @property
    def ready(self) -> bool:
        return bool(
            not self.cancelled
            and self.items
            and not self.unavailable
            and not self.failed
        )


@dataclass(frozen=True)
class ImageTagRemovalEvidence:
    """Native locked observation, not global retirement or restart authority.

    Already-absent members observe any previous head without claiming a new
    committed version. A continuation must retain this evidence in its zone and
    freshly verify it before offering retirement.
    """

    entity: HistoryEntityRef
    child_operation_id: str
    record_token: str
    observed_head_version_id: str = ""
    committed_version_id: str = ""


@dataclass(frozen=True)
class ImageTagRemovalApplyItem:
    image: Path
    status: str
    removed_values: tuple[str, ...] = ()
    error: str = ""
    version_id: str = ""
    evidence: ImageTagRemovalEvidence | None = None


@dataclass
class ImageTagRemovalApplyResult:
    operation_id: str = ""
    items: list[ImageTagRemovalApplyItem] = field(default_factory=list)
    cancelled: bool = False
    cache_warning: str = ""
    history_operation_id: str = ""
    history_interrupted: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def changed(self) -> int:
        return self.count("changed")

    @property
    def already_absent(self) -> int:
        return self.count("already_absent")

    @property
    def stale(self) -> int:
        return self.count("stale")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def not_processed(self) -> int:
        return self.count("not_processed")

    @property
    def complete(self) -> bool:
        return bool(
            self.items
            and not self.cancelled
            and all(item.status in {"changed", "already_absent"} for item in self.items)
        )


class ImageTagRemovalService:
    """Remove selected stable tag identities from explicit image scope.

    Only authoritative ``.tags.json`` Image Knowledge is written. Raw evidence,
    adjacent TXT, embedded Metadata, source images, and file locations are not
    changed. System Knowledge removal remains a separate caller-owned step and
    is safe only after :attr:`ImageTagRemovalApplyResult.complete` is true.
    """

    OPERATION_KIND = "image_tag_removal"
    UNDO_KIND = "image_tag_removal_undo"

    def __init__(self, knowledge, journal: OperationJournal, *, index=None) -> None:
        self.knowledge = knowledge
        self.journal = journal
        self.index = index
        self.identities = ConfirmedTagIdentityService(knowledge)

    def preview(
        self,
        images: Iterable[str | Path],
        tag_ids: Iterable[str],
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ImageTagRemovalPreview:
        targets = self._targets(tag_ids)
        paths = tuple(dict.fromkeys(Path(value) for value in images))
        should_cancel = should_cancel or (lambda: False)
        result = ImageTagRemovalPreview(targets)
        target_ids = {target.stable_id for target in targets}
        for position, image in enumerate(paths, 1):
            if should_cancel():
                result.cancelled = True
                break
            result.items.append(self._preview_image(image, target_ids))
            if progress is not None:
                progress(position, len(paths), image.name)
        return result

    def apply(
        self,
        preview: ImageTagRemovalPreview,
        *,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        history_operation=None,
    ) -> ImageTagRemovalApplyResult:
        if not preview.ready:
            raise ValueError(
                "Every affected image must pass the current preview before removal."
            )
        target_ids = {target.stable_id for target in preview.targets}
        should_cancel = should_cancel or (lambda: False)
        result = ImageTagRemovalApplyResult()
        operation_id = history_operation.operation_id if history_operation is not None else self.journal.begin(
            self.OPERATION_KIND,
            expected=preview.will_remove,
            reason="Remove selected SDXL Tags from all affected images",
            metadata={
                "tag_ids": sorted(target_ids),
                "tag_displays": [target.display for target in preview.targets],
                "writes": "image_knowledge_only",
            },
        )
        if history_operation is None:
            result.operation_id = operation_id
        else:
            result.history_operation_id = operation_id
        terminal_status = "complete"

        for position, item in enumerate(preview.items):
            if item.status == "already_absent" and history_operation is None:
                result.items.append(ImageTagRemovalApplyItem(item.image, "already_absent"))
                continue
            if item.status not in {"will_remove", "already_absent"}:
                result.items.append(ImageTagRemovalApplyItem(item.image, "failed", error=item.error))
                continue
            if should_cancel():
                result.cancelled = True
                terminal_status = "cancelled"
                for remaining in preview.items[position:]:
                    result.items.append(ImageTagRemovalApplyItem(
                        remaining.image,
                        "not_processed",
                        error=(
                            "The operation was cancelled before this image changed."
                        ),
                    ))
                break
            if history_operation is None and image_tag_batch_fingerprint(item.image) != item.fingerprint:
                result.items.append(ImageTagRemovalApplyItem(
                    item.image, "stale", error="Image Knowledge changed after the preview."
                ))
                continue
            try:
                removed, version_id, evidence = self._apply_one(
                    operation_id, item, target_ids, history_operation=history_operation,
                    result=result,
                )
            except ImageTagRemovalStaleError as exc:
                result.items.append(ImageTagRemovalApplyItem(item.image, "stale", error=str(exc)))
            except Exception as exc:
                interrupted = isinstance(exc, HistoryWriteInterrupted)
                held = history_operation is not None and isinstance(
                    exc, (HistoryError, HistoryConfigurationError, InterruptedError)
                )
                status = "interrupted" if interrupted else "held" if held else "failed"
                result.history_interrupted |= interrupted
                result.items.append(ImageTagRemovalApplyItem(item.image, status, error=str(exc)))
                if interrupted or held:
                    result.items.extend(ImageTagRemovalApplyItem(
                        remaining.image, "not_processed",
                        error="Resolve the held image before continuing removal.",
                    ) for remaining in preview.items[position + 1:])
                    break
            else:
                result.items.append(ImageTagRemovalApplyItem(
                    item.image,
                    "changed" if removed else "already_absent",
                    tuple(removed),
                    version_id=version_id,
                    evidence=evidence,
                ))
            if progress is not None:
                progress(len(result.items), len(preview.items), item.image.name)

        if history_operation is None:
            self.journal.finish(operation_id, terminal_status)
            self.journal.prune()
        return result

    def undo(self, operation_id: str) -> ImageTagRemovalApplyResult:
        operation = next(
            (item for item in self.journal.operations() if item.id == operation_id),
            None,
        )
        if (
            operation is None
            or operation.kind != self.OPERATION_KIND
            or operation.status not in {"complete", "cancelled"}
            or not operation.changes
        ):
            raise ValueError("That all-images tag removal is not available to undo.")

        prepared = []
        for change in operation.changes:
            image = Path(change["item"])
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            if ImageRecord(image).annotation.to_dict() != after:
                raise RuntimeError(
                    f"{image.name} changed after removal, so it cannot be restored automatically."
                )
            prepared.append((image, before, after, bool(change["metadata"].get("sidecar_existed", True))))

        undo_id = self.journal.begin(
            self.UNDO_KIND,
            expected=len(prepared),
            reason=f"Undo all-images tag removal {operation_id}",
            metadata={"restores": operation_id, "writes": "image_knowledge_only"},
        )
        restored = []
        result = ImageTagRemovalApplyResult(operation_id=undo_id)
        try:
            for image, before, after, sidecar_existed in prepared:
                self._restore(image, before, sidecar_existed=sidecar_existed)
                self._publish_cache(ImageRecord(image), result)
                self.journal.append_change(
                    undo_id,
                    item=str(image),
                    before=after,
                    after=before,
                    reason="Undo all-images SDXL Tag removal",
                    metadata={"sidecar_existed": True},
                )
                restored.append((image, after))
                result.items.append(ImageTagRemovalApplyItem(image, "changed"))
        except Exception as exc:
            rollback_errors = []
            for image, after in reversed(restored):
                try:
                    self._restore(image, after, sidecar_existed=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{image.name}: {rollback_exc}")
            message = str(exc)
            if rollback_errors:
                message += "; automatic recovery also failed: " + "; ".join(rollback_errors)
            self.journal.finish(undo_id, "failed", message=message)
            raise RuntimeError(message) from exc
        self.journal.finish(undo_id, "complete")
        self.journal.finish(operation_id, "undone")
        self.journal.prune()
        return result

    def _targets(self, tag_ids: Iterable[str]) -> tuple[ImageTagRemovalTarget, ...]:
        result = []
        for stable_id in dict.fromkeys(str(value) for value in tag_ids if str(value)):
            record = self.knowledge.tags.get(stable_id)
            if record is None:
                raise ValueError(f'System Knowledge tag "{stable_id}" is unavailable.')
            result.append(ImageTagRemovalTarget(record.id, record.display))
        if not result:
            raise ValueError("Choose at least one System Knowledge SDXL Tag.")
        return tuple(result)

    def _preview_image(self, image: Path, target_ids: set[str]) -> ImageTagRemovalItem:
        fingerprint = image_tag_batch_fingerprint(image)
        if not image.is_file():
            return ImageTagRemovalItem(image, fingerprint, "unavailable", error="The image is unavailable.")
        try:
            record = ImageRecord(image)
            values = self._target_values(record, target_ids)
        except ConfirmedTagIdentityError as exc:
            return ImageTagRemovalItem(
                image,
                fingerprint,
                "failed",
                error=f"Confirmed Tag Identity Repair is required first. {exc}",
            )
        except Exception as exc:
            return ImageTagRemovalItem(image, fingerprint, "failed", error=str(exc))
        return ImageTagRemovalItem(
            image,
            fingerprint,
            "will_remove" if values else "already_absent",
            values,
        )

    def _target_values(self, record: ImageRecord, target_ids: set[str]) -> tuple[str, ...]:
        plan = self.identities.plan(
            record.annotation.all_structured_tags(),
            stored_ids=record.annotation.tag_identities,
            confirmation=False,
        )
        if plan.blockers or plan.new_entries:
            raise ConfirmedTagIdentityError(plan)
        return tuple(entry.raw_value for entry in plan.entries if entry.stable_id in target_ids)

    def _apply_one(
        self,
        operation_id: str,
        item: ImageTagRemovalItem,
        target_ids: set[str],
        *, history_operation=None, result=None,
    ) -> tuple[list[str], str, ImageTagRemovalEvidence | None]:
        context = history_operation.context_for(item.image) if history_operation is not None else None
        if history_operation is not None and (context is None or context.operation_id != operation_id):
            raise HistoryError("Tag removal requires its exact admitted saved-history operation.")
        with context.locked(item.image) if context is not None else nullcontext():
            if context is not None and context.store.has_pending(context.entity):
                raise HistoryWriteInterrupted("Open this image for saved-history recovery before removing Tags.")
            if image_tag_batch_fingerprint(item.image) != item.fingerprint:
                raise ImageTagRemovalStaleError("Image Knowledge changed after the preview.")
            removed = self._apply_current(operation_id, item, target_ids, context)
            version_id = ""
            evidence = None
            if context is not None:
                # Capture completion while configuration, source identity and
                # per-image history/record locks still protect this observation.
                if context.store.validate_entity(context.entity) != item.image.resolve():
                    raise HistoryWriteInterrupted("The saved removal's image identity changed. Review History before continuing.")
                if context.store.has_pending(context.entity):
                    raise HistoryWriteInterrupted("The saved removal needs history recovery before continuing.")
                raw = read_record(item.image.with_suffix(".tags.json"))
                head = context.store.current_head(context.entity)
                if removed and (head is None or head.operation_id != operation_id
                        or context.store.payload(context.entity, head.snapshot) != raw):
                    raise HistoryWriteInterrupted("The saved removal could not be verified in History. Open this image for recovery.")
                if self._target_values(ImageRecord(item.image), target_ids):
                    raise HistoryWriteInterrupted("Selected Tag facts remain in this image. Review it before continuing.")
                if read_record(item.image.with_suffix(".tags.json")) != raw:
                    raise HistoryWriteInterrupted("Image Knowledge changed during completion verification. Review it before continuing.")
                version_id = head.version_id if removed else ""
                evidence = ImageTagRemovalEvidence(context.entity, operation_id,
                    record_token(raw), head.version_id if head else "", version_id)
            if removed:
                self._publish_cache(ImageRecord(item.image), result)
            return removed, version_id, evidence

    def verify_evidence(self, configuration, descriptor, evidence, target_ids):
        """Recheck one retained observation without binding or recovering it.

        This is a per-image current-state check, not a global frozen snapshot or
        permission to retire vocabulary. The continuation owns exact membership,
        receipt decoding, current retirement review and publication admission.
        """
        if not isinstance(evidence, ImageTagRemovalEvidence):
            raise HistoryError("This image has no verified removal observation.")
        store = HistoryStore(descriptor.scope())
        entity = evidence.entity
        if not isinstance(entity, HistoryEntityRef) or entity.scope_key != store.scope.key:
            raise HistoryError("Removal evidence belongs to a different history scope.")
        image = store.scope.image(store.scope.library_root / entity.locator)
        with configuration.write_guard(descriptor, path=image), store.locked(image):
            if store.validate_entity(entity) != image or store.has_pending(entity):
                raise HistoryError("Removal evidence needs exact-image history review.")
            raw = read_record(image.with_suffix(".tags.json"))
            head = store.current_head(entity)
            observed = head.version_id if head else ""
            if (record_token(raw) != evidence.record_token
                    or observed != evidence.observed_head_version_id):
                raise HistoryError("This image changed after removal. Review it before continuing.")
            if evidence.committed_version_id and (
                    head is None or head.version_id != evidence.committed_version_id
                    or head.operation_id != evidence.child_operation_id
                    or store.payload(entity, head.snapshot) != raw):
                raise HistoryError("The retained removal version is not current. Review Image History.")
            targets = {target.stable_id for target in self._targets(target_ids)}
            if self._target_values(ImageRecord(image), targets):
                raise HistoryError("Selected Tag facts remain in this image. Review it before continuing.")
            if read_record(image.with_suffix(".tags.json")) != raw:
                raise HistoryError("Image Knowledge changed during verification. Review it before continuing.")
        return image

    def _apply_current(self, operation_id, item, target_ids, context):
        record = ImageRecord(item.image, **({"history_context": context} if context is not None else {}))
        before = copy.deepcopy(record.annotation.to_dict())
        sidecar_existed = record.sidecar_path.exists()
        removed = []
        for value in self._target_values(record, target_ids):
            if record.remove_tag(value):
                removed.append(value)
        if not removed:
            return []
        if self._target_values(record, target_ids):
            raise RuntimeError("The selected tag identity remained in Image Knowledge after removal.")
        record.annotation.source["latest_operation"] = {
            "id": operation_id,
            "kind": self.OPERATION_KIND,
        }
        self.identities.synchronize_annotation(record.annotation)
        record.modified = True
        after = copy.deepcopy(record.annotation.to_dict())
        try:
            record.save_sidecar(force=True, knowledge=self.knowledge)
            if ImageRecord(item.image).annotation.to_dict() != after:
                raise RuntimeError("Saved Image Knowledge did not match the reviewed removal.")
            if context is None:
                self.journal.append_change(
                    operation_id,
                    item=str(item.image),
                    before=before,
                    after=after,
                    reason="Remove selected SDXL Tags from Image Knowledge",
                    metadata={
                        "tag_ids": sorted(target_ids),
                        "removed_values": list(removed),
                        "sidecar_existed": sidecar_existed,
                    },
                )
        except Exception as exc:
            if context is not None:
                raise HistoryWriteInterrupted(
                    "This image may have been saved. Open it for scoped history recovery before continuing removal. " + str(exc)
                ) from exc
            self._restore(item.image, before, sidecar_existed=sidecar_existed)
            raise
        return removed

    def _publish_cache(self, record: ImageRecord, result: ImageTagRemovalApplyResult | None) -> None:
        if self.index is None:
            return
        try:
            sources = typed_tag_sources(record, self.knowledge)
            self.index.put(
                record.image_path,list(sources),tag_sources=sources,
                plain_language=record.annotation.plain_language,
            )
        except Exception as exc:
            if result is not None and not result.cache_warning:
                result.cache_warning = str(exc)

    @staticmethod
    def _restore(image: Path, snapshot: dict, *, sidecar_existed: bool) -> None:
        sidecar = image.with_suffix(".tags.json")
        if not sidecar_existed:
            sidecar.unlink(missing_ok=True)
            return
        record = ImageRecord(image, auto_load=False)
        record.annotation = ImageAnnotation.from_dict(copy.deepcopy(snapshot), image.name)
        record.loaded = True
        record.modified = True
        record.save_sidecar(force=True)
        if ImageRecord(image).annotation.to_dict() != snapshot:
            raise RuntimeError(f"{image.name} could not be restored exactly.")
