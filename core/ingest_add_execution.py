"""Execute only receipt-backed, explicitly reviewed ordinary Add Images batches."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable
from uuid import uuid4

from .edit_service import MoveHistoryService, MoveRecord
from .ingest_add_batch import AddBatchIntentStore, AddBatchState
from .ingest_import import IngestImportItem, IngestImportPlan
from .media_inventory import inspect_media
from .verified_transfer import TransferRecoveryRequired


@dataclass(frozen=True)
class AddImagesBatchResult:
    parent_id: str
    group_id: str
    action: str
    status: str  # complete, paused, blocked, held, closure_pending
    completed: int
    remaining: int
    held: int
    records: tuple[MoveRecord, ...]
    errors: tuple[str, ...] = ()
    recovery_operation_id: str = ""


class AddImagesBatchExecutor:
    """Keep sealing, reservation and canonical transfers together outside Qt."""

    def __init__(self, moves: MoveHistoryService) -> None:
        self.moves = moves
        self.store = AddBatchIntentStore(moves.journal)

    def run_initial(
        self, plan: IngestImportPlan, action: str, *, origin: str,
        boundary_confirmed: bool, progress: Callable[[int, int, str], None],
        should_pause: Callable[[], bool],
    ) -> AddImagesBatchResult:
        if origin not in {"ordinary_files", "ordinary_folder"}:
            raise ValueError("Protected result handoffs keep their original exact Add Images review.")
        if action not in {"copy", "move"}:
            raise ValueError("Choose an exact Copy or Move Add Images method.")
        if (plan.boundary_decision is not None
                and plan.boundary_decision.blocked_by_default and not boundary_confirmed):
            raise PermissionError("Confirm the current exact Local Only boundary before adding images.")
        ready = plan.ready(action)
        if not ready:
            raise ValueError("No reviewed image families are ready to add.")
        for item in ready:
            self._assert_ordinary_history(item)
        parent_id = self.store.begin(plan, action, "ingest-add-" + uuid4().hex,
                                     should_cancel=should_pause)
        # The original dialog may have visibly skipped duplicates/unsupported
        # entries; the sealed parent contains only its ready selection.
        exact = replace(plan, items=list(ready), unsupported=())
        try:
            bound = self.store.matching_remaining_items(
                parent_id, exact, should_cancel=should_pause,
            )
        except Exception as exc:
            return self._outcome(parent_id, (), "paused" if isinstance(exc, InterruptedError)
                                 else "blocked", (str(exc),))
        return self._execute(parent_id, bound, plan, boundary_confirmed,
                             progress, should_pause)

    def run_remaining(
        self, parent_id: str, reviewed_plan: IngestImportPlan, *,
        boundary_confirmed: bool, progress: Callable[[int, int, str], None],
        should_pause: Callable[[], bool],
    ) -> AddImagesBatchResult:
        state = self.store.read(parent_id)
        if (reviewed_plan.boundary_decision is not None
                and reviewed_plan.boundary_decision.blocked_by_default
                and not boundary_confirmed):
            raise PermissionError("Confirm the fresh exact Local Only boundary before continuing.")
        try:
            bound = self.store.matching_remaining_items(
                parent_id, reviewed_plan, should_cancel=should_pause,
            )
        except Exception as exc:
            return self._outcome(parent_id, (), "paused" if isinstance(exc, InterruptedError)
                                 else "blocked", (str(exc),))
        if not bound and state.remaining == 0:
            try:
                self.store.close_complete(parent_id)
                return self._outcome_closed(state, ())
            except Exception as exc:
                return self._outcome(parent_id, (), "closure_pending", (str(exc),))
        return self._execute(parent_id, bound, reviewed_plan, boundary_confirmed,
                             progress, should_pause)

    def _execute(
        self, parent_id: str, items: tuple[IngestImportItem, ...],
        plan: IngestImportPlan, boundary_confirmed: bool,
        progress: Callable[[int, int, str], None], should_pause: Callable[[], bool],
    ) -> AddImagesBatchResult:
        records: list[MoveRecord] = []
        for index, item in enumerate(items, start=1):
            if should_pause():
                return self._outcome(parent_id, records, "paused")
            state = self.store.read(parent_id)
            sealed = next((row for row in state.items if row.source == item.source), None)
            if state.held or sealed is None or sealed.state != "remaining":
                return self._outcome(parent_id, records, "held",
                                     ("The saved Add Images item changed before transfer.",))
            try:
                self._assert_ordinary_history(item)
                media = inspect_media(item.source, verify_pixels=True)
                if not media.image_ready:
                    raise ValueError(f"{media.status}: {media.detail}")
            except Exception as exc:
                return self._outcome(parent_id, records, "blocked",
                                     (f"{item.source.name}: {exc}",))
            try:
                attempt = self.store.reserve(parent_id, sealed.item_id)
                transfer = item.transfer_for(state.action)
                if transfer is None:
                    raise ValueError("The exact Add Images transfer preview is unavailable.")
                arguments = dict(
                    relative_to=None,
                    reason="Add Images: Copy to Ingest" if state.action == "copy"
                           else "Add Images: Move to Ingest",
                    preview=transfer, collision_policy="error",
                    # Pause and shutdown drain the current family. No user
                    # cancellation is passed into a reserved transaction.
                    should_cancel=lambda: False,
                    boundary_decision=plan.boundary_decision,
                    boundary_confirmed=boundary_confirmed,
                    source_profile_ids=((item.source_profile.stable_id,)
                                        if item.source_profile is not None else ()),
                    destination_profile_id=state.destination_profile_id,
                    group_id=state.group_id,
                    batch_parent_id=parent_id,
                    batch_item_id=attempt.item_id,
                    batch_attempt_id=attempt.attempt_id,
                    batch_item_fingerprint=attempt.fingerprint,
                )
                record = (self.moves.copy if state.action == "copy" else self.moves.move)(
                    item.source, item.destination, **arguments,
                )
                records.append(record)
                confirmed = self.store.read(parent_id)
                current = next(row for row in confirmed.items
                               if row.item_id == sealed.item_id)
                if current.state != "completed" or current.operation_id != record.operation_id:
                    raise ValueError("The completed Add Images family has no exact child receipt.")
            except Exception as exc:
                return self._outcome(parent_id, records, "held",
                    (f"{item.source.name}: {exc}",),
                    getattr(exc, "operation_id", "") if isinstance(exc, TransferRecoveryRequired)
                    else "")
            progress(index, len(items), item.source.name)
        try:
            completed = self.store.close_complete(parent_id)
        except Exception as exc:
            return self._outcome(parent_id, records, "closure_pending", (str(exc),))
        return self._outcome_closed(completed, records)

    def _outcome(
        self, parent_id: str, records: list[MoveRecord] | tuple[MoveRecord, ...],
        status: str, errors: tuple[str, ...] = (), recovery_operation_id: str = "",
    ) -> AddImagesBatchResult:
        state = self.store.read(parent_id)
        if state.held:
            status = "held"
        return AddImagesBatchResult(parent_id, state.group_id, state.action,
            status, state.completed, state.remaining, state.held, tuple(records),
            errors, recovery_operation_id)

    @staticmethod
    def _outcome_closed(
        state: AddBatchState, records: list[MoveRecord] | tuple[MoveRecord, ...],
    ) -> AddImagesBatchResult:
        return AddImagesBatchResult(state.operation_id, state.group_id,
            state.action, "complete", state.completed, 0, 0, tuple(records))

    def _assert_ordinary_history(self, item: IngestImportItem) -> None:
        previous = (self.moves.record_for_path(item.source)
                    if self.moves.history_coordinator else None)
        if previous is not None and (previous.recycle_link or previous.recycle_lifecycle):
            raise ValueError("This linked-history image needs its own exact History action, not a saved Add Images batch.")
