"""Bounded contextual browsing and explicit typed restore coordination."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .history_configuration import path_key
from .history_versions import HistoryError, HistoryIndexPending, HistoryStore, VersionRef, HistoryEntityRef
from .image_knowledge_history import ImageKnowledgeHistory, RestoreSelection, RestoreUnit, RestorePreview, MAX_PREVIEW_BYTES

MAX_IMAGES = 1000
MAX_PAGE_SIZE = 50
MAX_COMPARISON_ROWS = 2000
MAX_COMPARISON_TEXT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class BrowserMember:
    image: Path
    descriptor: object


@dataclass(frozen=True)
class BrowserState:
    image: Path
    status: str
    message: str


@dataclass(frozen=True)
class BrowserVersion:
    image: Path
    entity: HistoryEntityRef
    version: VersionRef


@dataclass(frozen=True)
class BrowserCursor:
    member: int = 0
    version: int | None = None


@dataclass(frozen=True)
class BrowserPage:
    versions: tuple[BrowserVersion, ...]
    states: tuple[BrowserState, ...]
    next_cursor: BrowserCursor | None
    complete: bool


@dataclass(frozen=True)
class BrowserSelection:
    image: Path
    version_id: str = ""
    operation_id: str = ""
    units: tuple[RestoreUnit, ...] = (RestoreUnit("descriptions"),)


@dataclass(frozen=True)
class BrowserComparisonItem:
    image: Path
    preview: RestorePreview


@dataclass(frozen=True)
class BrowserComparison:
    items: tuple[BrowserComparisonItem, ...]


@dataclass(frozen=True)
class BrowserOutcome:
    image: Path
    status: str
    message: str = ""
    version_id: str = ""


@dataclass(frozen=True)
class BrowserComparisonRow:
    image: Path
    label: str
    choice_key: str
    historical: str
    current: str
    proposed: str
    changed: bool
    status: str


@dataclass(frozen=True)
class BrowserChoiceReview:
    rows: tuple[BrowserComparisonRow, ...]
    ready: bool
    message: str


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError("History operation cancelled before the next safe boundary.")


def _failure(image, exc):
    message = str(exc)
    lower = message.lower()
    status = "held"
    if isinstance(exc, HistoryIndexPending): status = "not_loaded"
    elif isinstance(exc, FileNotFoundError) or "missing" in lower or "journal is unavailable" in lower: status = "missing"
    elif "unsupported" in lower or "unknown" in lower: status = "unsupported"
    elif any(word in lower for word in ("digest", "corrupt", "invalid history", "integrity", "cache disagrees", "history event changed")): status = "corrupt"
    elif isinstance(exc, OSError): status = "unavailable"
    return BrowserState(image, status, message)


class HistoryBrowser:
    """Freeze exact selection without IO; operations inspect only those members.

    Query caches may be updated for existing journals. Opening this browser never
    enrolls an entity, creates history for an unknown image, or reconciles writes.
    """
    def __init__(self, configuration, images, *, operation_target=None, knowledge=None, characters=None):
        values = tuple(islice(iter(images), MAX_IMAGES + 1))
        if not values or len(values) > MAX_IMAGES:
            raise ValueError("Select between one and 1000 exact images for History & Versions.")
        paths = tuple(path_key(image) for image in values)
        if len(set(paths)) != len(paths):
            raise ValueError("Select each image once.")
        if operation_target is not None and (not isinstance(operation_target, str) or not operation_target or len(operation_target) > 128):
            raise ValueError("Choose one exact recorded operation identity.")
        self.configuration = configuration
        self.members = tuple(BrowserMember(path, configuration.resolve(path=path)) for path in paths)
        self.operation_target = operation_target
        self.knowledge, self.characters = knowledge, characters

    def _member(self, image):
        image = path_key(image)
        member = next((item for item in self.members if item.image == image), None)
        if member is None:
            raise HistoryError("The image is outside this browser's frozen selection.")
        self.configuration.require_current(member.descriptor, path=image)
        return member

    def _service(self, image):
        member = self._member(image)
        scope = member.descriptor.scope()
        store = HistoryStore(scope)
        return ImageKnowledgeHistory(store, knowledge=self.knowledge, characters=self.characters,
            authority_guard=lambda: self.configuration.write_guard(member.descriptor, path=member.image))

    def page(self, cursor=None, *, limit=MAX_PAGE_SIZE, image=None, cancelled=lambda: False):
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError("Choose a bounded history page of 1 to 50 records.")
        selected_index = next((index for index, member in enumerate(self.members) if member.image == path_key(image)), None) if image is not None else None
        if image is not None and selected_index is None:
            raise HistoryError("Choose an image from the frozen selection.")
        cursor = cursor or BrowserCursor(selected_index or 0)
        if not isinstance(cursor, BrowserCursor) or type(cursor.member) is not int or not 0 <= cursor.member < len(self.members):
            raise ValueError("History page cursor is outside the frozen selection.")
        position, before = cursor.member, cursor.version
        if selected_index is not None and position != selected_index:
            raise HistoryError("The page cursor belongs to another selected image.")
        end = len(self.members) if selected_index is None else selected_index + 1
        versions, states = [], []
        while position < end and len(versions) + len(states) < limit:
            _cancel(cancelled)
            member = self.members[position]
            if member.descriptor.status != "configured":
                reason = member.descriptor.reason
                if member.descriptor.status == "disabled":
                    reason += " Browsing disabled retained stores is not available in this browser."
                states.append(BrowserState(member.image, member.descriptor.status, reason))
            else:
                try:
                    service = self._service(member.image)
                    entity = service.store.lookup(member.image) if service else None
                    if entity is None:
                        states.append(BrowserState(member.image, "not_recorded", "No existing saved-history identity was recorded for this image."))
                    else:
                        # These metadata APIs may maintain the disposable index.
                        # Keep current settings authority before their scope lock.
                        with self.configuration.write_guard(member.descriptor, path=member.image):
                            if service.store.has_pending(entity):
                                states.append(BrowserState(member.image, "needs_recovery",
                                    "Reopen this exact image in its editor to resolve its scoped saved-data recovery, then reload History & Versions. Browsing does not repair interrupted writes."))
                            else:
                                page = service.store.query(entity, cursor=before, limit=limit - len(versions) - len(states), operation_id=self.operation_target or "")
                                versions.extend(BrowserVersion(member.image, entity, version) for version in page.versions)
                                if page.next_cursor is not None:
                                    return BrowserPage(tuple(versions), tuple(states), BrowserCursor(position, page.next_cursor), False)
                                if not page.versions:
                                    states.append(BrowserState(member.image, "no_record", "No versions match the exact operation." if self.operation_target else "The recorded identity has no committed saved-data versions."))
                except (HistoryError, ValueError, OSError) as exc:
                    states.append(_failure(member.image, exc))
            position += 1
            before = None
        _cancel(cancelled)
        next_cursor = BrowserCursor(position) if position < end else None
        return BrowserPage(tuple(versions), tuple(states), next_cursor, next_cursor is None)

    def update_index(self, image, *, cancelled=lambda: False):
        """One explicitly requested disposable-cache slice, never reconciliation."""
        _cancel(cancelled)
        service = self._service(image)
        try:
            service.store.lookup(image)  # validate raw paths without cache writes
        except HistoryIndexPending:
            pass
        if not service.store.journal.path.is_file():
            raise HistoryError("No existing history journal is available to index.")
        member = self._member(image)
        with self.configuration.write_guard(member.descriptor, path=member.image):
            complete = service.store.refresh_index(max_events=256)
        _cancel(cancelled)
        return complete

    def preview(self, selections, *, cancelled=lambda: False):
        selections = tuple(islice(iter(selections), MAX_IMAGES + 1))
        if not selections or len(selections) > MAX_IMAGES or len({path_key(item.image) for item in selections}) != len(selections):
            raise HistoryError("Select one exact restoration target per image, up to 1000 images.")
        results, size = [], 0
        for choice in selections:
            _cancel(cancelled)
            service = self._service(choice.image)
            entity = service.store.lookup(choice.image) if service else None
            if entity is None:
                raise HistoryError("No existing saved history is available for a selected image.")
            if self.operation_target and choice.operation_id != self.operation_target:
                raise HistoryError("The restoration target differs from this browser's frozen operation.")
            member = self._member(choice.image)
            with self.configuration.write_guard(member.descriptor, path=member.image):
                if choice.operation_id:
                    if choice.version_id:
                        raise HistoryError("Choose a version or an operation, not both.")
                    selection = service.operation_selection(entity, choice.operation_id, units=choice.units)
                else:
                    selection = RestoreSelection(entity, choice.version_id, units=choice.units)
                preview = service.preview_restore((selection,), cancelled=cancelled)
            size += sum(len(raw or b"") for item in preview.items for raw in (item.historical, item.current, item.proposed))
            if size > MAX_PREVIEW_BYTES:
                raise HistoryError("Combined comparison exceeds 64 MiB. Select fewer exact images.")
            results.append(BrowserComparisonItem(path_key(choice.image), preview))
        _cancel(cancelled)
        return BrowserComparison(tuple(results))

    def apply(self, comparison, *, choices, cancelled=lambda: False):
        if not isinstance(comparison, BrowserComparison) or not comparison.items:
            raise HistoryError("Compare the exact selected data before restoring.")
        if len(comparison.items) > MAX_IMAGES or len({path_key(item.image) for item in comparison.items}) != len(comparison.items):
            raise HistoryError("Restore each exact image only once within the bounded selection.")
        outcomes = []
        for entry in comparison.items:
            if cancelled():
                outcomes.append(BrowserOutcome(entry.image, "pending", "Cancelled before this image."))
                continue
            try:
                service = self._service(entry.image)
                if service is None:
                    raise HistoryError("Saved history is no longer available.")
                entity = service.store.lookup(entry.image)
                if len(entry.preview.items) != 1 or entry.preview.items[0].entity != entity:
                    raise HistoryError("The comparison does not belong to this exact selected image.")
                item = entry.preview.items[0]
                if self.operation_target and (item.selection.mode != "operation" or item.base.operation_id != self.operation_target):
                    raise HistoryError("The comparison differs from the frozen operation target.")
                result = service.apply_restore(entry.preview, choices=choices.get(str(entry.image), {}), cancelled=cancelled)
                outcomes.extend(BrowserOutcome(entry.image, item.status, item.message, item.version_id) for item in result)
            except (HistoryError, ValueError, OSError) as exc:
                outcomes.append(BrowserOutcome(entry.image, "held", str(exc)))
        return tuple(outcomes)

    def review_choices(self, comparison, *, choices, cancelled=lambda: False):
        if not isinstance(comparison, BrowserComparison) or not comparison.items:
            raise HistoryError("Compare the selected data before reviewing restoration choices.")
        rows, held, missing, changed, text_size = [], [], 0, 0, 0
        for entry in comparison.items:
            _cancel(cancelled)
            service = self._service(entry.image)
            if service is None:
                raise HistoryError("The selected history store is no longer available.")
            selected = choices.get(str(entry.image), {})
            comparisons = service.compare_restore(entry.preview,
                choices={key: value for key, value in selected.items() if value in {"current", "historical"}}, cancelled=cancelled)
            for item in comparisons:
                held.extend(f"{entry.image.name}: {reason}" for reason in item.held)
                for unit in item.units:
                    key = item.entity_id + ":" + unit.unit.id
                    selected_value = selected.get(key)
                    missing += bool(unit.changed and selected_value not in {"current", "historical"})
                    changed += bool(unit.changed and selected_value == "historical")
                    status = "Later overlapping change — explicit choice required" if unit.later_overlap else "Changed" if unit.changed else "Unchanged"
                    if unit.required_units:
                        status += "; required: " + ", ".join(unit.required_units)
                    if item.held:
                        status += "; held: " + "; ".join(item.held)
                    rows.append(BrowserComparisonRow(entry.image, unit.unit.id, key,
                        unit.historical, unit.current, unit.proposed, unit.changed, status))
                    text_size += sum(len(value.encode("utf-8")) for value in (unit.historical, unit.current, unit.proposed))
                    if len(rows) > MAX_COMPARISON_ROWS or text_size > MAX_COMPARISON_TEXT_BYTES:
                        raise HistoryError("Comparison exceeds the display budget. Choose fewer images or narrower data categories.")
        _cancel(cancelled)
        counts = f"{len(comparison.items)} exact images · {changed} units chosen · {missing} choices required."
        if held:
            message = "Restoration held. Review dependencies or keep current values. " + "; ".join(held) + " " + counts
        elif missing:
            message = "Choose historical or current for every changed unit, then review the proposal. " + counts
        elif not changed:
            message = "No units chosen for restoration; current data stays unchanged. " + counts
        else:
            message = "Ready to restore selected data. " + counts + " Unselected categories stay current; each image is a separate transaction."
        return BrowserChoiceReview(tuple(rows), not held and missing == 0 and changed > 0, message)
