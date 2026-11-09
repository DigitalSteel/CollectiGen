"""Bounded, nonmodal browser for saved local-training review pointers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core.training_run_index import (
    MAX_PAGE_SIZE, SHA, TrainingRunIndexError, TrainingRunIndexRecoveryRequired,
    TrainingRunPage, TrainingRunRecord,
)
from .components import EmptyStatePanel, bind_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


PAGE_SIZE = min(100, MAX_PAGE_SIZE)


@dataclass(frozen=True)
class _Delivery:
    token: int
    action: str
    payload: object = None
    error: str = ""
    recovery_required: bool = False


class TrainingHistoryDialog(FeedbackEnabledDialog):
    """Show app-state pointers without inspecting their target workspaces."""

    openRunRequested = Signal(object, str)
    retryRetainedRequested = Signal()
    feedback_workflow = "Local training history"

    def __init__(self, index, jobs, parent=None, *, read_only=False):
        super().__init__(parent)
        self.index, self.jobs = index, jobs
        self._read_only = read_only
        self._task = None
        self._closing = False
        self._cancel_requested = False
        self._serial = 0
        self._offset = 0
        self._next_offset = None
        self._revision = ""
        self._records = ()
        self._needs_recovery = False
        self._history_returns = ()
        self._history_known_changes = ()
        self._history_active_receipt = None
        self._history_start_errors = ()
        self._history_cancel_errors = ()
        self._history_admission_unconfirmed = False
        self._history_projection_held = False
        self._history_source_held = False
        self._history_presenting_refs = None
        self._history_presenting_receipt = None
        self._history_finish_busy = False
        self._history_reading_busy = False
        self._history_result_delivered = False
        self._history_followup_pending = None
        self._history_reload_needed = False
        self._history_intent_busy = False
        self._retained_retry_available = False

        self.setModal(False)
        self.setWindowTitle("Local Training History")
        self.resize(940, 700)
        layout = QVBoxLayout(self)
        from .components import ContextHelpButton
        from .help_content import section_help
        heading = QHBoxLayout()
        heading.addWidget(self._label("Find a saved Training review", "sectionTitle"), 1)
        self.help_button = ContextHelpButton('Saved Training history', section_help('training-saved-history'),
            self, learning_context='training-saved-history', focus_scope=self)
        heading.addWidget(self.help_button)
        layout.addLayout(heading)
        layout.addWidget(self._label(
            "Find and open saved training reviews. This window cannot change the saved list."
            if self._read_only else
            "Find previous local training runs and setup checks. Open a run to review "
            "its result, check recovery, or choose an earlier checkpoint."))

        top = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        top.addStretch(1)
        self.page_summary = self._label("Training history has not loaded yet.")
        layout.addLayout(top)
        layout.addWidget(self.page_summary)

        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["Run", "Kind", "Phase", "Destination"])
        self.runs.setRootIsDecorated(False)
        self.runs.setSelectionMode(QAbstractItemView.SingleSelection)
        self.runs.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.runs.setUniformRowHeights(True)
        header = self.runs.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.runs.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.runs, 1)
        self.empty_state = EmptyStatePanel(
            "Build training data from Dataset or Training Collections, then prepare a local run. "
            "Saved runs and setup checks will appear here.", "No Training History")
        self.empty_state.hide()
        layout.addWidget(self.empty_state, 1)

        pages = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(self._previous)
        pages.addWidget(self.previous_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next)
        pages.addWidget(self.next_button)
        pages.addStretch(1)
        layout.addLayout(pages)

        self.detail = self._label("Select a saved run to see its details.")
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.forget_explanation = self._label(
            "Forget removes only the entry from this list. Retained reviews, checkpoints, "
            "training outputs, and registry data remain in their existing locations.")
        layout.addWidget(self.forget_explanation)

        self.status = self._label("Waiting to read the saved training list.")
        self.retained_status = self._label("")
        self.retained_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.retained_status.hide()
        self.retry_retained_button = QPushButton("Retry Saving Retained Reviews")
        self.retry_retained_button.clicked.connect(self._retry_retained)
        self.retry_retained_button.hide()
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide()
        self.cancel_button = QPushButton("Cancel History Check")
        self.cancel_button.clicked.connect(self._cancel); self.cancel_button.hide()
        layout.addWidget(self.status); layout.addWidget(self.retained_status)
        layout.addWidget(self.retry_retained_button)
        layout.addWidget(self.progress); layout.addWidget(self.cancel_button)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons = buttons
        buttons.rejected.connect(self.reject)
        self.review_return_button = QPushButton('Review Returned Result')
        self.review_return_button.setToolTip('Review a checked result without repeating the saved-list operation.')
        self.review_return_button.clicked.connect(self._review_history_return)
        self.review_return_button.hide()
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(buttons)
        actions.addWidget(self.review_return_button)
        self.open_button = QPushButton("Open Selected")
        self.open_button.setProperty("primary", True)
        self.open_button.clicked.connect(self._open_selected)
        self.forget_button = QPushButton("Forget From This List")
        self.forget_button.clicked.connect(self._forget_selected)
        self.forget_button.setVisible(not self._read_only)
        actions.addWidget(self.forget_button)
        self.recover_button = QPushButton("Recover Saved List")
        self.recover_button.clicked.connect(self._recover_index)
        actions.addWidget(self.recover_button)
        actions.addWidget(self.open_button)
        layout.addLayout(actions)
        bind_status_footer(self, self.status, self.progress, self.cancel_button,
            title="Local training history")
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._update_actions()
        QTimer.singleShot(0, self.refresh)

    @staticmethod
    def _label(text, name=""):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        if name:
            label.setObjectName(name)
        return label

    def refresh(self):
        self._request_page(0)

    def set_retained_index_status(self, message, *, retry=False):
        """Show controller-owned pointer publication failures without hiding the index."""
        self.retained_status.setText(str(message)[:2000])
        self._retained_retry_available = not self._read_only and bool(message) and retry
        self.retained_status.setVisible(bool(message))
        self.retry_retained_button.setVisible(self._retained_retry_available)
        self._update_actions()

    def _request_page(self, offset):
        self._history_intent(lambda: self._request_page_now(offset))

    def _request_page_now(self, offset):
        if self._history_operation_busy() or self._closing or self._needs_recovery:
            return
        if self._history_presenting_refs is not None and any(a is not b for a, b in
                zip(self._history_presenting_refs, (self.index, self.jobs, self.parent()))):
            self._history_source_held = True
            self.page_summary.setText('The original list result is retained. The source changed during display; close and reopen the current list. No reload was submitted.')
            self._update_actions(); return
        if type(offset) is not int or offset < 0:
            return
        index = self.index
        refs = (index, self.jobs, self.parent())
        self._invalidate_page("Reading saved training history…")
        self._launch("page", "Read saved training history",
            lambda _job: index.page(offset=offset, limit=PAGE_SIZE), scope={'refs': refs, 'offset': offset})

    def _launch(self, action, name, operation, *, scope):
        if self._history_operation_busy():
            return
        if any(a is not b for a, b in zip(scope['refs'], (self.index, self.jobs, self.parent()))):
            self._history_source_held = True
            self.page_summary.setText('The saved-list source changed before work started. Close and reopen the current list; no operation was submitted.')
            self._update_actions(); return
        self._serial += 1
        token = self._serial
        self._cancel_requested = False
        returned = {}
        receipt = {'token': token, 'action': action, 'scope': scope,
            'context': self._history_context(), 'delivery': None, 'validated': False,
            'fact': None, 'presented': False, 'outer_error': '', 'presentation_error': '',
            'resume_context': None, 'cancel_requested': False}

        def work(job):
            try:
                if job.cancel_requested:
                    raise InterruptedError("History check cancelled.")
                delivery = _Delivery(token, action, operation(job))
            except TrainingRunIndexRecoveryRequired as exc:
                delivery = _Delivery(token, action, error=str(exc)[:2000], recovery_required=True)
            except Exception as exc:
                delivery = _Delivery(token, action, error=str(exc)[:2000])
            returned["value"] = delivery
            return delivery

        task = CoordinatedCallTask(self.jobs, name, work, resource="io")
        self._task = task
        self._history_active_receipt = receipt
        self._history_admission_unconfirmed = False
        self.status.setText(name)
        self.progress.show(); self.cancel_button.show()
        task.signals.complete.connect(lambda _value: self._finish(task, returned.get("value"), receipt=receipt))
        task.signals.failed.connect(lambda error: self._finish(task, returned.get("value"), error, receipt=receipt))
        task.signals.cancelled.connect(lambda: self._finish(
            task, returned.get("value"), "History check cancelled at a safe boundary.", receipt=receipt))
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            if self._task is task:
                self._history_admission_unconfirmed = True
                self._history_start_errors = (*self._history_start_errors, (task, str(exc)))
                self.page_summary.setText('The history check may have started. Keep this window open until the original work finishes; do not repeat it. ' + str(exc)[:300])
                self.status.setText('Startup unconfirmed; the original history check is still owned.')
                self._update_actions()

    def _finish(self, task, delivery, outer_error="", *, receipt):
        if self._task is not task:
            return
        receipt['delivery'] = delivery; receipt['outer_error'] = str(outer_error)
        self._history_returns = (*self._history_returns, receipt)
        receipt['cancel_requested'] = self._cancel_requested
        self._task = None
        self._history_active_receipt = None
        self._history_admission_unconfirmed = False
        self._history_finish_busy = True
        try:
            self._validate_history_delivery(receipt)
            receipt['validated'] = True
            if not delivery.error and not delivery.recovery_required and delivery.action in {'forget', 'recover'}:
                receipt['fact'] = {'action': delivery.action, 'revision': delivery.payload[0]
                    if delivery.action == 'forget' else delivery.payload,
                    'run_id': receipt['scope']['record'].run_id if delivery.action == 'forget' else None}
                self._history_known_changes = (*self._history_known_changes, receipt)
            self._project_history_return(receipt)
        except Exception as exc:
            self._hold_history_projection(receipt, exc)
        finally:
            self._history_finish_busy = False
            self._cancel_requested = False
            self.progress.hide(); self.cancel_button.hide(); self._update_actions()
        if self._closing: self.done(QDialog.Rejected)
        else: self._flush_history_reload()

    def _hold_history_projection(self, receipt, error):
        receipt['presentation_error'] = str(error)
        self._history_projection_held = True
        summary = ('No actual saved-list result was received. Completion is held; keep the original list and diagnostic. No checked list change is inferred. '
            if receipt['delivery'] is None else 'The actual saved-list return is retained. Its display or binding needs review; no list operation was repeated. ')
        self.page_summary.setText(summary + str(error)[:500])
        self.status.setText('Returned history result held: ' + str(error)[:300])

    def _project_history_return(self, receipt, *, retry=False):
        context = receipt['resume_context'] if retry else receipt['context']
        if not self._history_context_current(context):
            self._history_source_held = True
            raise ValueError('The saved-list source changed; the original return is retained without adoption.')
        if retry: self._validate_history_delivery(receipt)
        previous_cancel = self._cancel_requested
        self._cancel_requested = receipt['cancel_requested']
        try:
            if not self._finish_history_reading():
                raise ValueError('Finish independent reading, then review the returned result or choose Close again. No request will replay.')
            if not self._history_context_current(context):
                self._history_source_held = True
                raise ValueError('The saved-list source changed while finishing reading; no result was adopted.')
            previous_refs = self._history_presenting_refs
            self._history_presenting_refs = context[0]
            self._history_presenting_receipt = receipt
            receipt['projection_skipped'] = False
            try: self._present_history_delivery(None, receipt['delivery'], receipt['outer_error'])
            finally:
                self._history_presenting_refs = previous_refs
                self._history_presenting_receipt = None
            if not self._history_projection_matches(receipt, context):
                self._history_source_held = True
                raise ValueError('The source changed during result display; keep the original return and reopen the current list.')
            receipt['presented'] = True; receipt['presentation_error'] = ''
            self._history_projection_held = False
        finally:
            self._cancel_requested = previous_cancel
            if not receipt['presented'] and not self._history_context_current(context):
                self._history_source_held = True
            receipt['resume_context'] = (None if self._history_source_held else
                self._history_context() if receipt['presented'] else context)

    def _history_projection_matches(self, receipt, context):
        now = self._history_context(); delivery = receipt['delivery']
        if any(a is not b for a, b in zip(now[0], context[0])): return False
        serial, offset, revision, records, next_offset, needs = context[1]
        expected = context[1]
        if not receipt.get('projection_skipped', False):
            if delivery.recovery_required:
                expected = (serial, offset, '', (), None, True)
            elif receipt['cancel_requested'] and delivery.action == 'page':
                expected = (serial, offset, '', (), None, needs)
            elif not delivery.error and delivery.action == 'page':
                page = delivery.payload
                expected = (serial, page.offset, page.revision, page.records, page.next_offset, needs)
            elif not delivery.error and delivery.action == 'recover':
                expected = (serial, offset, revision, records, next_offset, False)
        return now[1] == expected

    def _reviewable_history_return(self):
        if self._history_source_held: return None
        return next((r for r in reversed(self._history_returns) if r['validated'] and not r['presented']
            and r['resume_context'] is not None and self._history_context_current(r['resume_context'])), None)

    def _review_history_return(self):
        if self._task is not None or self._history_finish_busy or self._history_reading_busy or self._history_result_delivered or self._history_intent_busy: return
        receipt = self._reviewable_history_return()
        if receipt is None: return
        self._history_finish_busy = True
        try: self._project_history_return(receipt, retry=True)
        except Exception as exc: self._hold_history_projection(receipt, exc)
        finally:
            self._history_finish_busy = False; self._cancel_requested = False; self._update_actions()
        if self._closing: self.done(QDialog.Rejected)
        else: self._flush_history_reload()

    def _queue_history_reload(self, offset):
        receipt = self._history_presenting_receipt
        if receipt is None or receipt['fact'] is None:
            raise ValueError('No accepted saved-list change owns this followup read.')
        self._history_reload_needed = True
        if self._closing or receipt['cancel_requested']: return
        self._history_followup_pending = (offset, receipt)

    def _abandon_history_reload(self):
        if self._history_followup_pending is not None: self._history_reload_needed = True
        self._history_followup_pending = None

    def _flush_history_reload(self):
        if self._history_intent_busy: return
        pending = self._history_followup_pending
        if pending is None: return
        self._history_followup_pending = None
        offset, receipt = pending
        if (not receipt['presented'] or receipt['cancel_requested'] or self._closing
                or self._history_result_delivered or self._history_projection_held or self._history_source_held): return
        context = receipt['resume_context']
        if not self._history_context_current(context):
            self._history_source_held = True
        elif not self._finish_history_reading(): pass
        elif not self._history_context_current(context): self._history_source_held = True
        else:
            self._request_page(offset)
            self._update_actions(); return
        self._history_reload_needed = True
        self.page_summary.setText('The original saved-list result is retained. The source changed; close and reopen the current list. No reload was submitted.'
            if self._history_source_held else 'The saved-list result is retained. Finish reading and choose Refresh explicitly; closing a reader will not reload the list.')
        self._update_actions()

    def _history_context(self):
        return ((self.index, self.jobs, self.parent()),
            (self._serial, self._offset, self._revision, self._records, self._next_offset, self._needs_recovery))

    def _history_context_current(self, context):
        now = self._history_context()
        return all(a is b for a, b in zip(now[0], context[0])) and now[1] == context[1]

    def _validate_history_delivery(self, receipt):
        delivery = receipt['delivery']
        if (type(delivery) is not _Delivery or type(delivery.token) is not int
                or delivery.token != receipt['token'] or delivery.action != receipt['action']
                or delivery.action not in {'page', 'forget', 'recover'}
                or type(delivery.error) is not str or type(delivery.recovery_required) is not bool):
            raise ValueError('No actual matching typed history delivery was captured.')
        if delivery.error or delivery.recovery_required:
            if delivery.payload is not None: raise ValueError('The history error contains a contradictory result.')
            return
        value = delivery.payload
        if delivery.action == 'page':
            self._validate_page(value)
            if value.offset != receipt['scope']['offset']: raise ValueError('Another history page returned.')
        elif delivery.action == 'forget':
            scope = receipt['scope']; scope['record'].validate(stored=True)
            if (type(scope['revision']) is not str or SHA.fullmatch(scope['revision']) is None
                    or type(value) is not tuple or len(value) != 2
                    or type(value[0]) is not str or SHA.fullmatch(value[0]) is None
                    or type(value[1]) is not int or value[1] != scope['offset']):
                raise ValueError('No exact saved-list removal revision and followup offset returned.')
        elif type(value) is not str or SHA.fullmatch(value) is None:
            raise ValueError('No saved-list recovery revision returned.')

    def _history_busy(self):
        return self._history_intent_busy or self._history_operation_busy()

    def _history_operation_busy(self):
        return (self._task is not None or self._history_projection_held or self._history_source_held
            or self._history_finish_busy or self._history_reading_busy or self._history_result_delivered or self._closing)

    def _present_history_delivery(self, task, delivery, outer_error=""):
        self.progress.hide(); self.cancel_button.hide()
        if self._closing:
            self._history_presenting_receipt['projection_skipped'] = True
            return
        if not isinstance(delivery, _Delivery) or delivery.token != self._serial:
            self.status.setText(str(outer_error or "A stale history result was ignored.")[:2000])
        elif delivery.recovery_required:
            self._needs_recovery = True
            self._invalidate_page(delivery.error or "The saved training list needs recovery.")
        elif self._cancel_requested and delivery.action == "page":
            self._invalidate_page("History check cancelled. Refresh when you are ready.")
        elif delivery.error:
            self.status.setText(delivery.error)
        elif delivery.action == "page":
            self._loaded(delivery.payload)
        elif delivery.action == "forget":
            target_offset = delivery.payload[1]
            self.status.setText("The entry was removed from this list. Its saved files remain available.")
            self._queue_history_reload(target_offset)
            return
        elif delivery.action == "recover":
            self._needs_recovery = False
            self.status.setText("The saved training list recovery returned. A separate page read will reload the list.")
            self._queue_history_reload(0)
            return
        self._cancel_requested = False
        self._update_actions()

    def _loaded(self, page):
        try:
            self._validate_page(page)
        except (TrainingRunIndexError, TypeError, ValueError) as exc:
            self._invalidate_page(str(exc))
            return
        self._offset = page.offset
        self._next_offset = page.next_offset
        self._revision = page.revision
        self._records = page.records
        self._history_reload_needed = False
        self.runs.clear()
        for record in page.records:
            name = record.annotation.name or Path(record.destination).name or record.run_id[:8]
            kind = {"execution": "Training run", "readiness": "Readiness check",
                "anima": "Anima run", "validation": "Held-out validation",
                "preflight": "SDXL dataset preparation"}[record.kind]
            phase = record.phase.replace("_", " ").title()
            item = QTreeWidgetItem([name, kind, phase, record.destination])
            item.setData(0, Qt.UserRole, (record.run_id, record.sequence))
            item.setToolTip(0, record.run_id)
            item.setToolTip(3, record.destination)
            self.runs.addTopLevelItem(item)
        first = min(page.offset + 1, page.total)
        last = min(page.offset + len(page.records), page.total)
        self.page_summary.setText(
            f"Saved reviews: {page.total:,} · Showing {first:,}–{last:,}" if page.records else
            f"Saved reviews: {page.total:,}")
        if not page.records:
            title = "No Runs on This Page" if page.total else "No Training History"
            self.empty_state.title_label.setText(title)
            self.empty_state.setAccessibleName(title)
            if page.total:
                guidance = "The saved list changed. Choose Previous or Refresh to load available runs."
            elif self._read_only:
                guidance = ("No saved runs are available in this workspace. Return to Applications "
                            "to review a training package or compare samples.")
            else:
                guidance = ("Build training data from Dataset or Training Collections, then prepare a local run. "
                            "Saved runs and setup checks will appear here.")
            self.empty_state.setText(guidance)
        self.empty_state.setVisible(not page.records)
        self.runs.setVisible(bool(page.records))
        self.status.setText(
            "Select a saved review to open." if page.records else "")
        self._selection_changed()

    @staticmethod
    def _validate_page(page):
        if (not isinstance(page, TrainingRunPage) or type(page.records) is not tuple
                or len(page.records) > PAGE_SIZE or type(page.total) is not int or page.total < 0
                or type(page.offset) is not int or page.offset < 0
                or page.offset + len(page.records) > page.total
                or (page.next_offset is not None and
                    (type(page.next_offset) is not int or page.next_offset <= page.offset))
                or type(page.revision) is not str or SHA.fullmatch(page.revision) is None):
            raise ValueError("The saved training list returned an invalid bounded page.")
        previous = None
        for record in page.records:
            if not isinstance(record, TrainingRunRecord):
                raise ValueError("The saved training list contains an unsupported pointer.")
            record.validate(stored=True)
            if previous is not None and record.sequence >= previous:
                raise ValueError("The saved training list is not newest first.")
            previous = record.sequence

    def _invalidate_page(self, message):
        self._revision = ""; self._records = (); self._next_offset = None
        self.runs.clear(); self.detail.setText("No saved review is selected.")
        self.empty_state.hide(); self.runs.show()
        self.page_summary.setText("Saved training history is unavailable until this check completes.")
        self.status.setText(str(message)[:2000])
        self._update_actions()

    def _selected(self):
        item = self.runs.currentItem()
        identity = item.data(0, Qt.UserRole) if item is not None else None
        if type(identity) is not tuple or len(identity) != 2:
            return None
        matches = [record for record in self._records
            if (record.run_id, record.sequence) == identity]
        return matches[0] if len(matches) == 1 else None

    def _selection_changed(self):
        record = self._selected()
        if record is None:
            self.detail.setText("Select a saved run to see its details.")
        else:
            kind = {"execution": "Local training run", "readiness": "Local readiness check",
                "anima": "Retained Anima run", "validation": "Held-out validation",
                "preflight": "SDXL dataset preparation"}[record.kind]
            self.detail.setText(
                f"{kind} · {record.phase.replace('_', ' ')}\n"
                + (f"Name: {record.annotation.name}\n" if record.annotation.name else "") +
                f"Run ID: {record.run_id}\nDestination: {record.destination}\n"
                + ("The saved review will be checked before opening."
                   if self._read_only else
                   "The saved review will be checked before recovery or result verification."))
        self._update_actions()

    def _previous(self):
        self._request_page(max(0, self._offset - PAGE_SIZE))

    def _next(self):
        if self._next_offset is not None:
            self._request_page(self._next_offset)

    def _open_selected(self):
        self._history_intent(self._open_selected_now)

    def _open_selected_now(self):
        record = self._selected()
        if not self._history_operation_busy() and record is not None and self._revision:
            self.openRunRequested.emit(record, self._revision)

    def _forget_selected(self):
        if not self._read_only:
            self._history_intent(self._forget_selected_now)

    def _forget_selected_now(self):
        record = self._selected()
        if self._read_only or self._history_operation_busy() or record is None or not self._revision:
            return
        revision, run_id = self._revision, record.run_id
        index = self.index
        refs = (index, self.jobs, self.parent())
        target_offset = max(0, self._offset - PAGE_SIZE) if len(self._records) == 1 else self._offset
        self._invalidate_page(
            "Removing this entry from the list; saved files will remain available…")
        self._launch("forget", "Forget saved training pointer",
            lambda _job: (index.forget(run_id, revision), target_offset),
            scope={'refs': refs, 'record': record, 'revision': revision, 'offset': target_offset})

    def _recover_index(self):
        if not self._read_only:
            self._history_intent(self._recover_index_now)

    def _recover_index_now(self):
        if self._read_only or self._history_operation_busy() or not self._needs_recovery:
            return
        index = self.index
        refs = (index, self.jobs, self.parent())
        self._invalidate_page("Recovering the saved training list…")
        self._launch("recover", "Recover saved training list", lambda _job: index.recover(), scope={'refs': refs})

    def _cancel(self):
        if self._history_finish_busy or self._history_reading_busy or self._history_result_delivered or self._history_intent_busy: return
        if not self._finish_history_reading(): return
        self._request_history_cancel()

    def _request_history_cancel(self):
        if self._task is None or self._cancel_requested:
            return
        self._cancel_requested = True
        if self._task.job_id:
            jobs = self._history_active_receipt['scope']['refs'][1]
            try: jobs.cancel(self._task.job_id)
            except Exception as exc:
                self._history_cancel_errors = (*self._history_cancel_errors, (self._task, str(exc)))
                self.page_summary.setText('The stop request failed; the original history check is still owned. ' + str(exc)[:300])
        self.status.setText("Cancelling after the current saved-list operation finishes…")
        self.cancel_button.setEnabled(False)

    def _update_actions(self):
        idle = not self._history_busy()
        selected = self._selected() if idle else None
        if idle and self._serial and not self._revision:
            recovery_message = ("Training history needs recovery in its owning Training workspace."
                                if self._read_only else "Training history needs recovery.")
            self.page_summary.setText(
                recovery_message if self._needs_recovery else
                "Saved-list result retained. Choose Refresh to load the list when ready." if self._history_reload_needed else
                "Training history unavailable. Choose Refresh to try again.")
        self.refresh_button.setEnabled(idle and not self._needs_recovery)
        self.previous_button.setEnabled(idle and not self._needs_recovery and self._offset > 0)
        self.next_button.setEnabled(idle and not self._needs_recovery and self._next_offset is not None)
        self.open_button.setEnabled(idle and not self._needs_recovery and selected is not None and bool(self._revision))
        self.forget_button.setEnabled(
            not self._read_only and idle and not self._needs_recovery
            and selected is not None and bool(self._revision))
        self.detail.setVisible(selected is not None)
        self.forget_explanation.setVisible(not self._read_only and selected is not None)
        self.previous_button.setVisible(self._offset > 0 or self._next_offset is not None)
        self.next_button.setVisible(self._offset > 0 or self._next_offset is not None)
        can_recover = self._needs_recovery and not self._read_only
        self.recover_button.setVisible(can_recover)
        self.recover_button.setEnabled(idle and can_recover)
        self.runs.setEnabled(idle and not self._needs_recovery)
        self.retry_retained_button.setEnabled(idle and self._retained_retry_available)
        self.cancel_button.setEnabled(not idle)
        reviewable = self._reviewable_history_return() if self._history_projection_held else None
        review_enabled = (reviewable is not None and self._task is None and not self._history_finish_busy
            and not self._history_reading_busy and not self._history_result_delivered and not self._closing)
        self.review_return_button.setVisible(reviewable is not None)
        self.review_return_button.setEnabled(review_enabled)
        primary_button = next((button for button in (self.review_return_button, self.recover_button,
            self.retry_retained_button, self.open_button,
            self.refresh_button if not self._revision or not self._records else None)
            if button is not None
            if not button.isHidden() and button.isEnabled()), None)
        for button in (self.review_return_button, self.recover_button, self.retry_retained_button,
                self.open_button, self.refresh_button):
            primary = button is primary_button
            if bool(button.property('primary')) != primary:
                button.setProperty('primary', primary)
                button.style().unpolish(button); button.style().polish(button)

    def reject(self):
        self.done(QDialog.Rejected)

    def done(self, result):
        if self._history_result_delivered or self._history_reading_busy or self._history_intent_busy: return
        if not self._finish_history_reading(): return
        if self._task is not None or self._history_finish_busy:
            if result == QDialog.Rejected:
                self._closing = True; self._abandon_history_reload()
                self._request_history_cancel()
            else:self.status.setText('History work is still owned. Wait, or choose Close to request cancellation.')
            return
        self._abandon_history_reload()
        self._closing = False; self._history_result_delivered = True
        QDialog.done(self, result)

    def _retry_retained(self):
        if self._retained_retry_available:
            self._history_intent(self.retryRetainedRequested.emit)

    def _history_intent_context(self):
        return (self._history_context(), self._selected(), self.retained_status.text(),
            self._retained_retry_available)

    def _history_intent_context_current(self, context):
        return (self._history_context_current(context[0])
            and context[1:] == self._history_intent_context()[1:])

    def _history_intent(self, action):
        if self._history_busy(): return False
        context = self._history_intent_context()
        self._history_intent_busy = True
        try:
            self._update_actions()
            ready = self._finish_history_reading()
            if not self._history_intent_context_current(context):
                self._history_source_held = True
                self.page_summary.setText('The saved-list source or selection changed while finishing reading. '
                    'Close and reopen the current list; the earlier action was abandoned.')
                return False
            if not ready: return False
            action()
            return True
        finally:
            self._history_intent_busy = False
            self._update_actions()
            self._flush_history_reload()

    def _finish_history_reading(self):
        if self._history_reading_busy: return False
        from .learning_help_search import close_learning_libraries
        self._history_reading_busy = True
        try:
            def readers():
                return tuple(c for c in self.findChildren(QDialog)
                    if c.property('collectigenLearningLibraryOwner') or c.property('collectigenLearningSearchOwner'))
            before = readers()
            try: ready = bool(close_learning_libraries(self)) and not any(c not in before for c in readers())
            except Exception: ready = False
        finally: self._history_reading_busy = False
        if not ready:
            self._closing = False; self._abandon_history_reload()
            self.page_summary.setText('Finish or save independent reading, then choose the history action or Close again. Known list results remain retained; no earlier request will replay.')
        return ready

    def closeEvent(self, event):
        event.ignore(); self.reject()
