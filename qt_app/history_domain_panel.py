"""Provider-specific transactions inside the one History & Versions workspace."""
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
    QTableWidget, QTableWidgetItem, QPlainTextEdit, QCheckBox, QSplitter)

from .components import bind_status_footer
from .workers import CoordinatedCallTask


@dataclass(frozen=True)
class DomainDelivery:
    serial: int
    value: object = None
    error: str = ""


class HistoryDomainPanel(QWidget):
    """Shared bounded task lifetime; concrete panels retain typed domain choices."""
    busyChanged = Signal(bool)
    closeRequested = Signal()
    restorationFinished = Signal(object)

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self._task = None
        self._serial = 0
        self._closing = False
        self._cancel_requested = False
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.status = self.label("History has not loaded yet.")
        self.body.addWidget(self.status)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.cancel = QPushButton("Cancel Safely", self)
        self.cancel.clicked.connect(self.cancel_work)
        self.cancel.hide()
        bind_status_footer(self, self.status, self.progress, self.cancel,
            title="History & Versions", follow_visibility=True)

    @staticmethod
    def label(text):
        result = QLabel(text)
        result.setTextFormat(Qt.PlainText)
        result.setWordWrap(True)
        return result

    def launch(self, action, operation, consume, *, writes=False):
        if self._task is not None or self._closing:
            return
        self._serial += 1
        serial = self._serial
        self._cancel_requested = False
        returned = {}
        def work(job):
            try:
                value = DomainDelivery(serial, operation(lambda: job.cancel_requested))
            except Exception as exc:
                value = DomainDelivery(serial, error=str(exc))
            returned["delivery"] = value
            return value
        task = CoordinatedCallTask(self.jobs, "History: " + action, work, resource="io")
        self._task = task
        def finished(value=None, error=""):
            if self._task is not task:
                return
            self._task = None
            delivery = returned.get("delivery", value)
            if not isinstance(delivery, DomainDelivery) or delivery.serial != self._serial:
                self.status.setText(error or "An outdated result was ignored.")
            elif delivery.error:
                self.status.setText(delivery.error)
            elif self._cancel_requested and action != "restore" and not writes:
                self.invalidate()
                self.status.setText("Cancelled; no comparison was authorized.")
            else:
                try:
                    consume(delivery.value)
                except Exception as exc:
                    self.status.setText("The completed result needs review: " + str(exc))
            self.update_actions()
            self.busyChanged.emit(False)
        task.signals.complete.connect(lambda value: finished(value))
        task.signals.failed.connect(lambda error: finished(error=str(error)))
        task.signals.cancelled.connect(lambda: finished(error="Cancelled at a safe boundary."))
        self.status.setText("Working: " + action)
        self.update_actions()
        self.busyChanged.emit(True)
        try:
            task.start()
        except Exception as exc:
            finished(error=str(exc))

    def cancel_work(self):
        self._cancel_requested = True
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)

    def shutdown(self):
        self._closing = True
        self.cancel_work()
        self.update_actions()


class OrganizationHistoryPanel(HistoryDomainPanel):
    def __init__(self, browser, jobs, parent=None):
        super().__init__(jobs, parent)
        self.browser = browser
        self._cursor = None
        self._rows = []
        self._selected = {}
        self._comparison = None
        self._loaded = False
        self._rendering = False
        self.body.addWidget(self.label(browser.scope_text))
        self.body.addWidget(self.label("Choose an exact recorded operation, then review its original, current and proposed locations. "
            "The complete current image family moves together; later saved data is preserved. Copies require retained Recycle recovery."))
        controls = QHBoxLayout()
        self.reload_button = QPushButton("Reload History")
        self.reload_button.clicked.connect(lambda: self.load(reset=True))
        self.more = QPushButton("Load More")
        self.more.clicked.connect(lambda: self.load())
        controls.addWidget(self.reload_button)
        controls.addWidget(self.more)
        controls.addStretch(1)
        self.counts = self.label("No records loaded")
        controls.addWidget(self.counts)
        self.body.addLayout(controls)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Image / exact operation", "Recorded time", "Action", "Coverage / reason"))
        self.tree.header().setSectionResizeMode(QHeaderView.Stretch)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(150)
        self.tree.itemChanged.connect(self.selected_changed)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Family member", "Recorded source image", "Current location", "Proposed location", "Data / boundary"))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.table)
        split.setSizes([320, 280])
        self.body.addWidget(split, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(145)
        self.detail.setPlaceholderText("Compare selected operations to review complete family and privacy consequences.")
        self.body.addWidget(self.detail)
        self.confirm = QCheckBox("I confirm the displayed complete families, destination roles and privacy boundary")
        self.confirm.toggled.connect(self.update_actions)
        self.confirm.hide()
        self.body.addWidget(self.confirm)
        actions = QHBoxLayout()
        self.clear = QPushButton("Clear Selected Operations")
        self.clear.clicked.connect(self.clear_selection)
        self.compare_button = QPushButton("Compare Selected Operations")
        self.compare_button.clicked.connect(self.compare)
        self.apply_button = QPushButton("Apply Reviewed Locations" if browser.domain_key == "recycle" else "Restore Selected Locations")
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self.apply)
        close = QPushButton("Close")
        close.clicked.connect(self.closeRequested)
        actions.addWidget(self.clear)
        actions.addStretch(1)
        actions.addWidget(self.compare_button)
        actions.addWidget(close)
        actions.addWidget(self.apply_button)
        self.body.addLayout(actions)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.update_actions()

    def activate(self):
        if not self._loaded:
            self.load(reset=True)

    def invalidate(self):
        self._comparison = None
        self.browser.invalidate()
        self.table.setRowCount(0)
        self.detail.clear()
        self.confirm.setChecked(False)
        self.confirm.hide()
        self.update_actions()

    def clear_selection(self):
        self._selected.clear()
        self.invalidate()
        self.render()

    def load(self, *, reset=False):
        if self._task is not None or self._closing:
            return
        if reset:
            self._rows = []
            self._selected.clear()
            self._cursor = None
            self.invalidate()
            self.render()
        elif self._cursor is None:
            return
        self.launch("browse organization", lambda cancelled: self.browser.page(self._cursor, cancelled=cancelled), self.loaded)

    def loaded(self, page):
        self._loaded = True
        self._rows.extend(page.rows)
        self._cursor = page.next_cursor
        self.render()
        self.status.setText(" · ".join(page.issues) or ("More recorded operations remain; Load More continues this exact scope."
            if self._cursor else "Recorded scope loaded. Choose exact operations to compare." if self._rows else
            "No recorded organization operations in this scope. No missing history was inferred."))

    def render(self):
        self._rendering = True
        self.tree.clear()
        for row in self._rows:
            name = row.current_path.name if row.current_path else row.member_id
            coverage = {"native_relocation": "Recorded image family", "copy_recycle_provider": "Copy with Recycle recovery",
                "recycle_provider": "Recorded Recycle lifecycle"}.get(
                row.coverage, row.coverage.replace("_", " "))
            item = QTreeWidgetItem((name + " · " + row.operation_id[:12], row.timestamp,
                row.operation_kind.replace("_", " "), coverage + " · " + row.reason))
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(0, "Operation: " + row.operation_id + "\nImage identity: " + row.member_id)
            item.setToolTip(3, coverage + "\n" + row.reason)
            if row.member_id and row.status != "reversed" and row.coverage in {"native_relocation", "copy_recycle_provider", "recycle_provider"}:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if self._selected.get(row.member_id) == row else Qt.Unchecked)
            self.tree.addTopLevelItem(item)
        self._rendering = False
        self.counts.setText(f"{len(self._rows):,} operations loaded · {len(self._selected):,} complete families selected")

    def selected_changed(self, item, _column):
        if self._rendering:
            return
        row = item.data(0, Qt.UserRole)
        if item.checkState(0) == Qt.Checked:
            self._selected[row.member_id] = row
        elif self._selected.get(row.member_id) == row:
            self._selected.pop(row.member_id, None)
        self.invalidate()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.render)

    def compare(self):
        selections = tuple(self._selected.values())
        self.invalidate()
        self.launch("compare organization", lambda cancelled: self.browser.preview(selections, cancelled=cancelled), self.compared)

    def compared(self, comparison):
        self._comparison = comparison
        self.table.setRowCount(sum(len(preview.family) for preview in comparison.previews))
        details = []
        index = 0
        for row, preview in zip(comparison.selections, comparison.previews):
            details.append("Operation: " + row.operation_id)
            details.extend(preview.held)
            if preview.boundary_decision:
                details.append(f"{preview.source_role} → {preview.destination_role}: {preview.boundary_decision.message}")
            for member in preview.family:
                values = (member.current_path.name, str(row.original_path), str(member.current_path),
                    str(member.proposed_path), "Later saved data preserved" if member.newer_data else "Recorded family bytes preserved")
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value[:500])
                    item.setToolTip(value)
                    self.table.setItem(index, column, item)
                details.append(str(member.current_path) + "\n  Current SHA-256: " + member.current_sha256)
                index += 1
        self.detail.setPlainText("\n".join(details)[:24000])
        self.confirm.setChecked(False)
        self.confirm.setVisible(comparison.needs_confirmation)
        self.status.setText("Review every complete family and its destination before applying."
            if comparison.ready else "Some selected operations are held. Review the reasons; no restoration is enabled.")

    def apply(self):
        if self._comparison is None or not self._comparison.ready:
            return
        comparison = self._comparison
        confirmed = self.confirm.isChecked()
        self._comparison = None
        self.launch("restore", lambda cancelled: self.browser.apply(comparison,
            boundary_confirmed=confirmed, cancelled=cancelled), self.restored)

    def restored(self, outcomes):
        self._comparison = None
        self.confirm.hide()
        self.detail.setPlainText("\n".join(value.operation_id + ": " + value.status + ". " + value.message +
            ("\nNew operation: " + value.inverse_operation_id if value.inverse_operation_id else "") for value in outcomes))
        self.status.setText(" · ".join(f"{status}: {sum(row.status == status for row in outcomes)}"
            for status in dict.fromkeys(row.status for row in outcomes)) + ". Reload to inspect the new inverse operations.")
        self.restorationFinished.emit(outcomes)

    def update_actions(self, *_args):
        idle = self._task is None and not self._closing
        for widget in (self.reload_button, self.tree, self.clear):
            widget.setEnabled(idle)
        self.more.setEnabled(idle and self._cursor is not None and len(self._rows) < 2000)
        self.compare_button.setEnabled(idle and bool(self._selected))
        self.confirm.setEnabled(idle)
        self.apply_button.setEnabled(idle and self._comparison is not None and self._comparison.ready
            and (not self._comparison.needs_confirmation or self.confirm.isChecked()))
        self.cancel.setVisible(not idle and not self._closing)
        self.progress.setVisible(not idle)
