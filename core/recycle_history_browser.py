"""Exact retained Recycle records in the shared organization comparison view."""
from dataclasses import replace
from pathlib import Path

from .organization_history import OrganizationHistoryRow, OrganizationRestoreOutcome
from .organization_history_browser import OrganizationBrowserPage, OrganizationComparison
from .verified_transfer import TransferRecoveryRequired


class RecycleHistoryBrowser:
    domain_key = "recycle"
    label = "Recycle recovery"

    def __init__(self, service, record_ids, *, action="restore_origin", destination=None, destination_profile=None, initial_previews=None):
        ids = tuple(dict.fromkeys(record_ids))
        if not ids or len(ids) > 10000 or any(not isinstance(value, str) or not value or len(value) > 128 for value in ids):
            raise ValueError("Choose a bounded exact Recycle record scope.")
        if action not in {"restore_origin", "route_ingest", "route_library"}:
            raise ValueError("Choose a supported Recycle review action.")
        self.service, self.record_ids = service, ids
        self.destination, self.destination_profile = destination, destination_profile
        self._actions = dict.fromkeys(ids, action)
        self._available, self._record_for_operation = {}, {}
        self._admitted = None
        self._initial_previews = dict(initial_previews or {})
        if any(key not in ids for key in self._initial_previews):
            raise ValueError("Initial Recycle review contains a record outside the exact scope.")

    @property
    def scope_text(self):
        return f"{len(self.record_ids):,} exact retained Recycle records · Complete file families"

    def invalidate(self):
        self._admitted = None

    def _preview_record(self, record_id, cancelled):
        return self.service.preview_history(record_id, action=self._actions[record_id],
            destination=self.destination, destination_profile=self.destination_profile, should_cancel=cancelled)

    def page(self, cursor=None, *, cancelled=lambda: False):
        if cursor is None:
            self._available.clear()
            self._record_for_operation.clear()
            self.invalidate()
            offset = 0
        elif not isinstance(cursor, str) or not cursor.isdecimal() or int(cursor) % 50 or not 0 < int(cursor) < len(self.record_ids):
            raise ValueError("Reload this exact Recycle scope before continuing.")
        else:
            offset = int(cursor)
        if len(self._available) >= 2000:
            raise ValueError("Narrow the selected Recycle scope before loading more records.")
        rows = []
        for record_id in self.record_ids[offset:offset + 50]:
            if cancelled():
                raise InterruptedError("Recycle history loading cancelled.")
            record = self.service.record(record_id)  # Display only; preview owns all write authority.
            preview = (self._initial_previews.pop(record_id) if record_id in self._initial_previews
                else self._preview_record(record_id, cancelled))
            operation = preview.operation_id if preview is not None and preview.operation_id else record_id
            member_id = preview.member_id if preview is not None else ""
            reason = ("; ".join(preview.held) if preview is not None else
                "Native history unavailable for this legacy record. Its explicit Recycle recovery stays separate.")
            row = OrganizationHistoryRow(operation, member_id, self._actions[record_id],
                str(record.recycled_at), reason or "Exact retained family and placement",
                Path(record.original_path), Path(record.current_path), record.status,
                "recycle_provider" if member_id else "unavailable")
            if operation in self._record_for_operation and self._record_for_operation[operation] != record_id:
                raise ValueError("Recycle records disagree about their exact operation; recovery is required.")
            self._available[operation] = row
            self._record_for_operation[operation] = record_id
            rows.append(row)
        end = offset + len(rows)
        return OrganizationBrowserPage(tuple(rows), str(end) if end < len(self.record_ids) else None, (), "available")

    def preview(self, selections, *, cancelled=lambda: False):
        self.invalidate()
        selections = tuple(selections)
        if not 0 < len(selections) <= 200 or len({row.member_id for row in selections}) != len(selections):
            raise ValueError("Select one operation per family, up to 200 exact Recycle families.")
        if any(not row.member_id or self._available.get(row.operation_id) != row for row in selections):
            raise ValueError("Reload the exact selected Recycle records before comparing.")
        previews = []
        for row in selections:
            if cancelled():
                raise InterruptedError("Recycle comparison cancelled before any file changed.")
            preview = self._preview_record(self._record_for_operation[row.operation_id], cancelled)
            if preview is not None and preview.held and not preview.member_id:
                raise ValueError("; ".join(preview.held))
            if preview is None or (preview.operation_id, preview.member_id) != (row.operation_id, row.member_id):
                raise ValueError("The selected Recycle placement changed; reload and review again.")
            previews.append(preview)
        self._admitted = OrganizationComparison(selections, tuple(previews))
        return self._admitted

    def apply(self, comparison, *, boundary_confirmed=False, cancelled=lambda: False):
        if comparison is not self._admitted or comparison is None or not comparison.ready:
            raise ValueError("Review the exact current Recycle comparison before applying it.")
        if comparison.needs_confirmation and not boundary_confirmed:
            raise PermissionError("Confirm the displayed complete families and privacy boundary.")
        self.invalidate()
        outcomes = []
        for index, preview in enumerate(comparison.previews):
            record_id = self._record_for_operation[preview.operation_id]
            if cancelled():
                result = (OrganizationRestoreOutcome(preview.operation_id, preview.member_id, "cancelled",
                    "Not started; earlier completed families remain recorded.", recycle_record_id=record_id),)
            else:
                try:
                    result = self.service.apply_history(preview, boundary_confirmed=boundary_confirmed, should_cancel=cancelled)
                except InterruptedError:
                    result = (OrganizationRestoreOutcome(preview.operation_id, preview.member_id, "cancelled", recycle_record_id=record_id),)
                except TransferRecoveryRequired as error:
                    result = (OrganizationRestoreOutcome(preview.operation_id, preview.member_id, "recovery_required", str(error), recycle_record_id=record_id),)
                except Exception as error:
                    result = (OrganizationRestoreOutcome(preview.operation_id, preview.member_id, "held", str(error), recycle_record_id=record_id),)
            for value in result:
                if value.status == "complete":
                    value = replace(value, previous_path=preview.transfer.image)
                    self._actions[record_id] = ("restore_placement" if self._actions[record_id] == "return_to_recycle"
                        else "return_to_recycle")
                outcomes.append(value)
            if any(value.status == "recovery_required" for value in result):
                outcomes.extend(OrganizationRestoreOutcome(value.operation_id, value.member_id, "held",
                    "Not started after an earlier family required exact recovery.",
                    recycle_record_id=self._record_for_operation[value.operation_id]) for value in comparison.previews[index + 1:])
                break
        return tuple(outcomes)
