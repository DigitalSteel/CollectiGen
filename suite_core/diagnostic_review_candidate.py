"""Inactive, non-Qt review/export controller candidate for a future Help host.

No recorder/root/default is constructed. The host owns explicit actions, visible
review and worker lifetime; these synchronous calls must run off the UI thread.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from .diagnostics import ExportPreview, export_preview, MAX_EVENTS, MAX_RECORD_BYTES

_MEMBERS = frozenset({'diagnostics.json', 'runtime.json'})


class PreviewOwner(Protocol):
    def preview(self) -> ExportPreview: ...


class ReviewStatus(str, Enum):
    NOT_REVIEWED = 'not_reviewed'
    UNAVAILABLE = 'unavailable'
    READY = 'ready'
    CANCELLED = 'cancelled'
    FAILED = 'failed'
    EXPORTED = 'exported'


class ReviewIssue(str, Enum):
    NONE = 'none'
    OWNER_MISSING = 'owner_missing'
    REVIEW_REQUIRED = 'review_required'
    PREVIEW_FAILED = 'preview_failed'
    INVALID_PREVIEW = 'invalid_preview'
    SELECTION_REQUIRED = 'selection_required'
    INVALID_SELECTION = 'invalid_selection'
    DESTINATION_CANCELLED = 'destination_cancelled'
    DESTINATION_EXISTS = 'destination_exists'
    EXPORT_FAILED = 'export_failed'


@dataclass(frozen=True)
class ReviewState:
    status: ReviewStatus = ReviewStatus.UNAVAILABLE
    issue: ReviewIssue = ReviewIssue.OWNER_MISSING
    preview: ExportPreview | None = None
    selected: tuple[str, ...] = ()
    selection_confirmed: bool = False
    hashes: tuple[tuple[str, str], ...] = ()

    @property
    def storage_available(self):
        return self.preview.storage_available if self.preview is not None else None

    @property
    def unavailable_files(self):
        return self.preview.unavailable_files if self.preview is not None else None

    @property
    def rejected_records(self):
        return self.preview.rejected_records if self.preview is not None else None


def _immutable_preview(preview):
    if not isinstance(preview, ExportPreview) or type(preview.files) is not tuple:
        return False
    if type(preview.storage_available) is not bool or any(
        type(value) is not int or not 0 <= value <= 2**31
        for value in (preview.unavailable_files, preview.rejected_records)
    ):
        return False
    names = []
    for item in preview.files:
        if type(item) is not tuple or len(item) != 2:
            return False
        name, payload = item
        if type(name) is not str or name not in _MEMBERS or type(payload) is not bytes or len(payload) > MAX_EVENTS * MAX_RECORD_BYTES + 1024:
            return False
        names.append(name)
    return len(names) == len(set(names)) and set(names) == _MEMBERS


class DiagnosticsReviewController:
    """Sequential owner only; no threads, UI, recording, cleanup or implicit I/O."""

    def __init__(self, owner: PreviewOwner | None = None):
        self._owner = owner
        self._state = ReviewState() if owner is None else ReviewState(
            status=ReviewStatus.NOT_REVIEWED, issue=ReviewIssue.REVIEW_REQUIRED)

    @property
    def state(self):
        return self._state

    def refresh(self):
        """Explicit refresh starts a new review and requires a new selection."""
        if self._owner is None:
            self._state = ReviewState()
            return self._state
        try:
            preview = self._owner.preview()
        except Exception:
            self._state = ReviewState(issue=ReviewIssue.PREVIEW_FAILED)
            return self._state
        if not _immutable_preview(preview):
            self._state = ReviewState(issue=ReviewIssue.INVALID_PREVIEW)
            return self._state
        self._state = ReviewState(status=ReviewStatus.READY, issue=ReviewIssue.NONE,
                                  preview=preview)
        return self._state

    def select(self, members: tuple[str, ...]):
        """Explicit selection; an empty tuple deliberately selects manifest only."""
        if self._state.preview is None:
            return self._state
        if type(members) is not tuple or any(type(name) is not str or name not in _MEMBERS
                                             for name in members) or len(set(members)) != len(members):
            self._state = replace(self._state, status=ReviewStatus.FAILED,
                issue=ReviewIssue.INVALID_SELECTION, selection_confirmed=False, hashes=())
        else:
            self._state = replace(self._state, status=ReviewStatus.READY,
                issue=ReviewIssue.NONE, selected=members, selection_confirmed=True, hashes=())
        return self._state

    def export(self, destination: Path | None, *, writer=export_preview):
        """Explicit reviewed export; cancellation/failure retains the exact review."""
        state = self._state
        if state.preview is None:
            return state
        if destination is None:
            self._state = replace(state, status=ReviewStatus.CANCELLED,
                issue=ReviewIssue.DESTINATION_CANCELLED, hashes=())
        elif not state.selection_confirmed:
            self._state = replace(state, status=ReviewStatus.FAILED,
                issue=ReviewIssue.SELECTION_REQUIRED, hashes=())
        else:
            try:
                hashes = writer(state.preview, destination, selected=state.selected)
                if type(hashes) is not dict or set(hashes) != set(state.selected) | {'manifest.json'} or any(
                    type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                    for value in hashes.values()
                ):
                    raise ValueError('Invalid verified export receipt')
                self._state = replace(state, status=ReviewStatus.EXPORTED,
                    issue=ReviewIssue.NONE, hashes=tuple(sorted(hashes.items())))
            except FileExistsError:
                self._state = replace(state, status=ReviewStatus.FAILED,
                    issue=ReviewIssue.DESTINATION_EXISTS, hashes=())
            except Exception:
                self._state = replace(state, status=ReviewStatus.FAILED,
                    issue=ReviewIssue.EXPORT_FAILED, hashes=())
        return self._state
