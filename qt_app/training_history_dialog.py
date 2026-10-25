"""Bounded, nonmodal browser for saved local-training review pointers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel,
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

    def __init__(self, index, jobs, parent=None):
        super().__init__(parent)
        self.index, self.jobs = index, jobs
        self._task = None
        self._closing = False
        self._cancel_requested = False
        self._serial = 0
        self._offset = 0
        self._next_offset = None
        self._revision = ""
        self._records = ()
        self._needs_recovery = False

        self.setModal(False)
        self.setWindowTitle("Local Training History")
        self.resize(940, 700)
        layout = QVBoxLayout(self)
        heading = self._label("Local Training History", "sectionTitle")
        layout.addWidget(heading)
        layout.addWidget(self._label(
            "Find previous local training runs and setup checks. Open a run to review "
            "its result, check recovery, or choose an earlier checkpoint."))

        top = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        top.addStretch(1)
        self.page_summary = self._label("Training history has not loaded yet.")
        top.addWidget(self.page_summary)
        layout.addLayout(top)

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
        self.retry_retained_button.clicked.connect(self.retryRetainedRequested)
        self.retry_retained_button.hide()
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide()
        self.cancel_button = QPushButton("Cancel History Check")
        self.cancel_button.clicked.connect(self._cancel); self.cancel_button.hide()
        layout.addWidget(self.status); layout.addWidget(self.retained_status)
        layout.addWidget(self.retry_retained_button)
        layout.addWidget(self.progress); layout.addWidget(self.cancel_button)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(buttons)
        self.open_button = QPushButton("Open Selected")
        self.open_button.setProperty("primary", True)
        self.open_button.clicked.connect(self._open_selected)
        self.forget_button = QPushButton("Forget From This List")
        self.forget_button.clicked.connect(self._forget_selected)
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
        self.retained_status.setVisible(bool(message))
        self.retry_retained_button.setVisible(bool(message) and retry)
        self.retry_retained_button.setEnabled(bool(message) and retry)

    def _request_page(self, offset):
        if self._task is not None or self._closing or self._needs_recovery:
            return
        if type(offset) is not int or offset < 0:
            return
        self._invalidate_page("Reading saved training history…")
        self._launch("page", "Read saved training history",
            lambda _job: self.index.page(offset=offset, limit=PAGE_SIZE))

    def _launch(self, action, name, operation):
        if self._task is not None:
            return
        self._serial += 1
        token = self._serial
        self._cancel_requested = False
        returned = {}

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
        self.status.setText(name)
        self.progress.show(); self.cancel_button.show()
        task.signals.complete.connect(lambda value: self._finish(task, returned.get("value", value)))
        task.signals.failed.connect(lambda error: self._finish(task, returned.get("value"), error))
        task.signals.cancelled.connect(lambda: self._finish(
            task, returned.get("value"), "History check cancelled at a safe boundary."))
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            self._finish(task, None, str(exc))

    def _finish(self, task, delivery, outer_error=""):
        if self._task is not task:
            return
        self._task = None
        self.progress.hide(); self.cancel_button.hide()
        if self._closing:
            super().reject()
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
            self._request_page(target_offset)
            return
        elif delivery.action == "recover":
            self._needs_recovery = False
            self.status.setText("The saved training list was recovered. Reloading it now.")
            self._request_page(0)
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
        self.runs.clear()
        for record in page.records:
            name = record.annotation.name or Path(record.destination).name or record.run_id[:8]
            kind = {"execution": "Training run", "readiness": "Readiness check",
                "anima": "Anima run", "validation": "Held-out validation"}[record.kind]
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
            self.empty_state.setText(
                "The saved list changed. Choose Previous or Refresh to load available runs." if page.total else
                "Build training data from Dataset or Training Collections, then prepare a local run. "
                "Saved runs and setup checks will appear here.")
        self.empty_state.setVisible(not page.records)
        self.runs.setVisible(bool(page.records))
        self.status.setText(
            "Choose a saved review to open. Its saved files will be checked before use." if page.records else "")
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
                "anima": "Retained Anima run", "validation": "Held-out validation"}[record.kind]
            self.detail.setText(
                f"{kind} · {record.phase.replace('_', ' ')}\n"
                + (f"Name: {record.annotation.name}\n" if record.annotation.name else "") +
                f"Run ID: {record.run_id}\nDestination: {record.destination}\n"
                "The saved review will be checked before recovery or result verification.")
        self._update_actions()

    def _previous(self):
        self._request_page(max(0, self._offset - PAGE_SIZE))

    def _next(self):
        if self._next_offset is not None:
            self._request_page(self._next_offset)

    def _open_selected(self):
        record = self._selected()
        if self._task is None and record is not None and self._revision:
            self.openRunRequested.emit(record, self._revision)

    def _forget_selected(self):
        record = self._selected()
        if self._task is not None or record is None or not self._revision:
            return
        revision, run_id = self._revision, record.run_id
        target_offset = max(0, self._offset - PAGE_SIZE) if len(self._records) == 1 else self._offset
        self._invalidate_page(
            "Removing this entry from the list; saved files will remain available…")
        self._launch("forget", "Forget saved training pointer",
            lambda _job: (self.index.forget(run_id, revision), target_offset))

    def _recover_index(self):
        if self._task is not None or not self._needs_recovery:
            return
        self._invalidate_page("Recovering the saved training list…")
        self._launch("recover", "Recover saved training list", lambda _job: self.index.recover())

    def _cancel(self):
        if self._task is None:
            return
        self._cancel_requested = True
        if self._task.job_id:
            self.jobs.cancel(self._task.job_id)
        self.status.setText("Cancelling after the current saved-list operation finishes…")
        self.cancel_button.setEnabled(False)

    def _update_actions(self):
        idle = self._task is None
        selected = self._selected() if idle else None
        if idle and self._serial and not self._revision:
            self.page_summary.setText(
                "Training history needs recovery." if self._needs_recovery else
                "Training history unavailable. Choose Refresh to try again.")
        self.refresh_button.setEnabled(idle and not self._needs_recovery)
        self.previous_button.setEnabled(idle and not self._needs_recovery and self._offset > 0)
        self.next_button.setEnabled(idle and not self._needs_recovery and self._next_offset is not None)
        self.open_button.setEnabled(idle and not self._needs_recovery and selected is not None and bool(self._revision))
        self.forget_button.setEnabled(idle and not self._needs_recovery and selected is not None and bool(self._revision))
        self.detail.setVisible(selected is not None)
        self.forget_explanation.setVisible(selected is not None)
        self.previous_button.setVisible(self._offset > 0 or self._next_offset is not None)
        self.next_button.setVisible(self._offset > 0 or self._next_offset is not None)
        self.recover_button.setVisible(self._needs_recovery)
        self.recover_button.setEnabled(idle and self._needs_recovery)
        self.runs.setEnabled(idle and not self._needs_recovery)
        self.cancel_button.setEnabled(not idle)

    def reject(self):
        if self._task is not None:
            self._closing = True
            self._cancel()
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else:
            super().closeEvent(event)
