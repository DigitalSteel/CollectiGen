"""Inactive Manager Databases session-to-preview-to-presentation slice.

Supplied approved releases only. No discovery, database, writer, apply or Qt API.
"""
from dataclasses import dataclass
from datetime import date
from enum import Enum
from hashlib import sha256
import json
from threading import RLock
from uuid import uuid4

from suite_core.knowledge import Snapshot, TagLookup, normalize_term
from suite_core.knowledge.catalog_migration_preview import PackRevision
from suite_core.selected_catalog import BoundCatalogProposal, CatalogBinding

SOFTWARE_VERSION = "0.1.0-alpha.1"
PRESENTATION_INTERFACE_VERSION = 1
RELEASE_DATE = "2026-10-06"


@dataclass(frozen=True)
class CatalogRelease:
    revision: PackRevision
    released_on: str
    whats_new: tuple[str, ...]
    source_ref: str

    def __post_init__(self):
        if (type(self.revision) is not PackRevision or type(self.released_on) is not str
                or type(self.revision.snapshot) is not Snapshot
                or type(self.whats_new) is not tuple
                or any(type(line) is not str or not line.strip() for line in self.whats_new)
                or type(self.source_ref) is not str or not self.source_ref.strip()):
            raise ValueError("An explicit immutable supplied catalog release is required")
        if date.fromisoformat(self.released_on).isoformat() != self.released_on:
            raise ValueError("Use an explicit YYYY-MM-DD release date")
        TagLookup(self.revision.snapshot)

    @property
    def fingerprint(self):
        values = (self.revision.pack_id, self.revision.content_version,
                  self.revision.snapshot.fingerprint, self.released_on, self.whats_new, self.source_ref)
        return sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class DatabaseAction(str, Enum):
    INSTALL_DEFAULT = "install_default_catalog"
    MERGE_UPDATE = "merge_update_existing"


class DatabasePreviewStatus(str, Enum):
    REVIEW_READY = "review_ready"
    REVIEW_REQUIRED = "review_required"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class CatalogConflict:
    reason: str
    tag_ids: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseReview:
    preview_id: int
    binding: CatalogBinding
    releases_token: str
    action: DatabaseAction
    mode: str
    status: DatabasePreviewStatus
    reason: str
    installed_version: str | None
    available_version: str | None
    available_date: str | None
    whats_new: tuple[str, ...]
    installed_source: str | None
    available_source: str | None
    proposal: BoundCatalogProposal | None = None
    additions: tuple = ()
    updates: tuple = ()
    protected_edits: tuple = ()
    retained_local_ids: tuple[str, ...] = ()
    excluded_catalog_ids: tuple[str, ...] = ()
    conflicts: tuple[CatalogConflict, ...] = ()
    assignment_choices: tuple = ()

    @property
    def can_apply(self):
        return False

    @property
    def apply_message(self):
        return "Preview only. No catalog or image assignment has been changed. Native apply and recovery are not connected to this preview."

    @property
    def action_label(self):
        return "Install default catalog" if self.action == DatabaseAction.INSTALL_DEFAULT else "Merge/update existing"

    @property
    def summary_lines(self):
        readiness = {
            DatabasePreviewStatus.REVIEW_READY: "Ready to review",
            DatabasePreviewStatus.REVIEW_REQUIRED: "Decisions need review",
            DatabasePreviewStatus.UNAVAILABLE: "Available catalog not supplied",
            DatabasePreviewStatus.STALE: "Preview is out of date; review again",
            DatabasePreviewStatus.CANCELLED: "Preview cancelled",
            DatabasePreviewStatus.ERROR: "Supplied catalog inputs need correction",
        }
        modes = {"blank": "Keep catalog blank", "own": "Keep own catalog",
                 "default": "Use default catalog", "merged": "Merge/update existing"}
        return (
            self.action_label, readiness[self.status], f"Proposal: {modes[self.mode]}",
            f"Selected catalog: {self.binding.catalog_id}",
            f"Supplied installed default version: {self.installed_version or 'none'}",
            f"Supplied available version: {self.available_version or 'unavailable'}",
            f"Additions: {len(self.additions)}", f"Updates: {len(self.updates)}",
            f"Protected edited records: {len(self.protected_edits)}",
            f"Conflicts: {len(self.conflicts)}", f"Image assignment choices: {len(self.assignment_choices)}",
            self.apply_message,
        )


class _Interrupted(Exception):
    def __init__(self, status): self.status = status


class DatabaseActionsController:
    """One selected session and supplied base/available release observations.

    installed=False means no default content installed, not no database engine.
    In that case the host supplies an explicit empty prior-default baseline for
    the same pack identity. Local own vocabulary remains the selected session.
    """
    def __init__(self, session, *, base_release, available_release, installed=True):
        self.session = session
        self._lock = RLock()
        self._controller_id = uuid4().hex
        self._sequence = 0
        self._cancelled_through = 0
        self.set_releases(base_release=base_release, available_release=available_release, installed=installed)

    def set_releases(self, *, base_release, available_release, installed=True):
        if (type(base_release) is not CatalogRelease
                or (available_release is not None and type(available_release) is not CatalogRelease)
                or type(installed) is not bool):
            raise ValueError("Supply admitted release observations or explicit unavailable content")
        if not installed and base_release.revision.snapshot.tags:
            raise ValueError("No installed default requires an explicit empty prior-default baseline")
        observed_binding = self.session.binding
        token = sha256(json.dumps((
            self._controller_id, observed_binding.token, base_release.fingerprint,
            available_release.fingerprint if available_release else None, installed,
        )).encode()).hexdigest()
        with self._lock:
            self._base, self._available, self._installed = base_release, available_release, installed
            self._release_binding = observed_binding
            self._releases_token = token
            self._sequence += 1

    def cancel(self):
        """Invalidate current/pending previews; never cancel a native writer."""
        with self._lock:
            self._cancelled_through = self._sequence
            self._sequence += 1

    def preview(self, action, *, mode=None, assignments=(), offers=(), should_continue=None):
        action = DatabaseAction(action)
        mode = mode if mode is not None else ("default" if action == DatabaseAction.INSTALL_DEFAULT else "merged")
        if mode not in {"blank", "own", "default", "merged"}:
            raise ValueError("Choose an explicit supported catalog proposal mode")
        with self._lock:
            self._sequence += 1
            preview_id = self._sequence
            base, available, installed = self._base, self._available, self._installed
            token = self._releases_token
            observed_binding = self._release_binding
        # Every view carries the captured release observations, including stale
        # and cancelled outcomes. Never relabel them with a newer selection.
        binding = observed_binding

        def view(status, reason, **details):
            return DatabaseReview(
                preview_id, binding, token, action, mode, status, reason,
                base.revision.content_version if installed else None,
                available.revision.content_version if available else None,
                available.released_on if available else None,
                available.whats_new if available else (),
                base.source_ref if installed else None,
                available.source_ref if available else None, **details,
            )

        def checkpoint():
            with self._lock:
                if preview_id <= self._cancelled_through:
                    raise _Interrupted(DatabasePreviewStatus.CANCELLED)
                if preview_id != self._sequence or token != self._releases_token:
                    raise _Interrupted(DatabasePreviewStatus.STALE)
            if self.session.binding != binding:
                raise _Interrupted(DatabasePreviewStatus.STALE)
            if should_continue is not None and not should_continue():
                raise _Interrupted(DatabasePreviewStatus.CANCELLED)

        try:
            checkpoint()
            if available is None:
                checkpoint()
                return view(DatabasePreviewStatus.UNAVAILABLE, "available_catalog_not_supplied")
            proposal = self.session.propose(base.revision, available.revision, mode=mode,
                                            assignments=assignments, offers=offers)
            checkpoint()
            if proposal.binding != binding:
                raise _Interrupted(DatabasePreviewStatus.STALE)
            preview = proposal.preview
            conflicts = []
            known_ids = {row.id for row in (*preview.local.tags, *preview.base.snapshot.tags,
                                            *preview.target.snapshot.tags)}
            known_ids.update(row.tag_id for row in preview.changes)
            for reason in preview.holds:
                kind, _, value = reason.partition(":")
                if kind == "catalog_name_collision":
                    ids = tuple(sorted(record.id for record in preview.records
                        if value in {normalize_term(name) for name in (record.display, *record.aliases)}))
                else:
                    ids = (value,) if value in known_ids else ()
                conflicts.append(CatalogConflict(reason, ids))
            choices = tuple(row for row in preview.reviews if row.reason != "unchanged_identity")
            proposed = {row.id for row in preview.records}
            local = {row.id for row in preview.local.tags}
            protected = tuple(change for change in preview.changes if change.protected_fields)
            status = (DatabasePreviewStatus.REVIEW_REQUIRED if conflicts or choices
                      else DatabasePreviewStatus.REVIEW_READY)
            checkpoint()
            return view(status, "detached_review_only", proposal=proposal,
                additions=tuple(change for change in preview.changes if change.reason == "added_default"),
                updates=tuple(change for change in preview.changes if change.reason == "default_update"),
                protected_edits=protected,
                retained_local_ids=tuple(sorted(local & proposed)),
                excluded_catalog_ids=tuple(sorted(local - proposed)),
                conflicts=tuple(conflicts), assignment_choices=choices)
        except _Interrupted as error:
            return view(error.status, error.status.value)
        except Exception:
            return view(DatabasePreviewStatus.ERROR, "supplied_preview_inputs_unavailable_or_invalid")

    def accepts(self, review):
        """Presentation freshness only; not assignment/offer/native apply admission."""
        with self._lock:
            if (review.preview_id != self._sequence or review.releases_token != self._releases_token
                    or review.binding != self._release_binding
                    or review.binding != self.session.binding):
                return False
            if review.proposal is None:
                return True
            if self._available is None:
                return False
            return self.session.accepts_proposal(review.proposal,
                current_base=self._base.revision, current_target=self._available.revision)
