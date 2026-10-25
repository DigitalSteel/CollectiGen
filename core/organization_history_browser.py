"""Exact selected-operation presentation for the shared History workspace."""
from dataclasses import dataclass, replace

from .organization_history import OrganizationRestoreOutcome


@dataclass(frozen=True)
class OrganizationBrowserPage:
    rows: tuple
    next_cursor: str | None
    issues: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class OrganizationComparison:
    selections: tuple
    previews: tuple

    @property
    def ready(self):
        return bool(self.previews) and all(value.ready for value in self.previews)

    @property
    def needs_confirmation(self):
        return any(value.boundary_decision and value.boundary_decision.requires_explicit_confirmation
            for value in self.previews)


class OrganizationHistoryBrowser:
    """An explicit UI scope; never falls back to latest or all operations."""
    domain_key = "organization"
    label = "File organization"

    def __init__(self, service, *, paths=(), entity_ids=(), operation_targets=(), group_id=""):
        self.service = service
        self.paths = tuple(paths)
        self.entity_ids = tuple(entity_ids)
        self.operation_targets = tuple(operation_targets)
        self.group_id = group_id
        self._available = {}
        self._admitted = None
        if not any((self.paths, self.entity_ids, self.operation_targets, group_id)):
            raise ValueError("Choose exact images or recorded organization operations.")
        if len(self.operation_targets) > 1000 or any(not isinstance(value, str) or not value or len(value) > 128
                for value in self.operation_targets):
            raise ValueError("Choose a bounded exact operation scope.")

    @property
    def scope_text(self):
        if self.operation_targets:
            return f"{len(self.operation_targets):,} exact organization operations"
        if self.group_id:
            return "Recorded organization batch: " + self.group_id
        return f"{len(self.paths) + len(self.entity_ids):,} selected image identities · Complete file families"

    def page(self, cursor=None, *, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError("Organization history loading cancelled.")
        result = self.service.query(paths=self.paths, entity_ids=self.entity_ids,
            group_id=self.group_id, cursor=cursor, limit=50)
        rows = tuple(row for row in result.rows if not self.operation_targets or row.operation_id in self.operation_targets)
        if cursor is None:
            self._available.clear()
            self.invalidate()
        if len(self._available) + len(rows) > 2000:
            raise ValueError("Narrow the selected organization scope before loading more history.")
        self._available.update((row.operation_id, row) for row in rows)
        return OrganizationBrowserPage(rows, result.next_cursor, result.issues, result.status)

    def invalidate(self):
        self._admitted = None

    def preview(self, selections, *, cancelled=lambda: False):
        self.invalidate()
        selections = tuple(selections)
        if not 0 < len(selections) <= 200:
            raise ValueError("Select between one and 200 exact complete families.")
        if len({row.member_id for row in selections}) != len(selections) or any(not row.member_id for row in selections):
            raise ValueError("Select one recorded operation per native image identity.")
        if self.operation_targets and any(row.operation_id not in self.operation_targets for row in selections):
            raise ValueError("A selected operation is outside the fixed history scope.")
        if self.group_id and any(row.group_id != self.group_id for row in selections):
            raise ValueError("A selected operation belongs to another recorded batch.")
        if any(self._available.get(row.operation_id) != row for row in selections):
            raise ValueError("Reload the exact selected operations before comparing their current families.")
        previews = []
        for row in selections:
            if cancelled():
                raise InterruptedError("Organization comparison cancelled before any files changed.")
            previews.append(self.service.preview_restore(row.operation_id, (row.member_id,), cancelled=cancelled))
        self._admitted = OrganizationComparison(selections, tuple(previews))
        return self._admitted

    def apply(self, comparison, *, boundary_confirmed=False, cancelled=lambda: False):
        if comparison is not self._admitted or not isinstance(comparison, OrganizationComparison) or not comparison.ready:
            raise ValueError("Review a complete supported organization comparison before restoring.")
        if len(comparison.selections) != len(comparison.previews) or any(
                (row.operation_id, row.member_id) != (preview.operation_id, preview.member_id)
                for row, preview in zip(comparison.selections, comparison.previews)):
            raise ValueError("The compared exact operation selection changed.")
        if comparison.needs_confirmation and not boundary_confirmed:
            raise PermissionError("Confirm the displayed exact destination and privacy boundary.")
        self.invalidate()
        outcomes = []
        for index, preview in enumerate(comparison.previews):
            if cancelled():
                outcomes.append(OrganizationRestoreOutcome(preview.operation_id, preview.member_id,
                    "cancelled", "Not started; earlier completed families remain recorded."))
                continue
            result = self.service.apply_restore(preview, boundary_confirmed=boundary_confirmed, cancelled=cancelled)
            outcomes.extend(replace(value, previous_path=preview.transfer.image)
                if value.status == "complete" else value for value in result)
            if any(value.status == "recovery_required" for value in result):
                remaining = comparison.previews[index + 1:]
                outcomes.extend(OrganizationRestoreOutcome(value.operation_id, value.member_id, "held",
                    "Not started after an earlier family required exact recovery.") for value in remaining)
                break
        completed = {value.operation_id: value.inverse_operation_id for value in outcomes
            if value.status == "complete" and value.inverse_operation_id}
        self.operation_targets = tuple(completed.get(value, value) for value in self.operation_targets)
        if self.paths:
            moved = {value.previous_path: value.current_path for value in outcomes
                if value.status == "complete" and value.previous_path is not None and value.current_path is not None}
            self.paths = tuple(dict.fromkeys(moved.get(value, value) for value in self.paths))
        return tuple(outcomes)
