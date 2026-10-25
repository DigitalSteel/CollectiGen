from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core.model_resource_registry import ModelResourceRegistry, ResourceRecoveryRequired, inspect_model_resource
from .modal_feedback import FeedbackEnabledDialog
from .components import bind_status_footer
from .workers import CoordinatedCallTask


@dataclass(frozen=True)
class _RecoveryNeeded:
    message: str


class ModelFamilySelector(QComboBox):
    """Canonical family choices, with explicit unknown/custom declarations."""
    textChanged = Signal(str)

    def __init__(self, value="", parent=None):
        super().__init__(parent)
        from core.model_organization import family_options
        self.addItem("Choose Model Family…", "")
        for family in dict.fromkeys((*family_options("Images"), *family_options("Video"))):
            self.addItem(family, family)
        self.addItem("Unknown / not identified", "Unknown")
        self.addItem("Custom family… (advanced)", "__custom__")
        self._last_family = ""
        self.currentIndexChanged.connect(self._changed)
        self.activated.connect(self._custom)
        self.setText(value)
        self.setToolTip("The base model type, such as Anima or SDXL. Your declaration does not verify architecture or adapter compatibility.")

    def text(self):
        value = self.currentData()
        return "" if value in (None, "__custom__") else str(value)

    def setText(self, value):
        value = str(value).strip()
        index = self.findData(value)
        if index < 0:
            self.addItem(value + " (custom)", value)
            index = self.count() - 1
        self.setCurrentIndex(index)

    def clear(self):
        self.setText("")

    def _changed(self, _):
        if self.currentData() != "__custom__": self._last_family = self.text()
        self.textChanged.emit(self.text())

    def _custom(self, _):
        if self.currentData() != "__custom__": return
        value, accepted = QInputDialog.getText(self, "Custom Model Family",
            "Enter a known family name (up to 160 characters). This remains your unverified declaration.")
        self.setText(value if accepted and value.strip() and len(value) <= 160 else self._last_family)


class ModelResourceDialog(FeedbackEnabledDialog):
    """Explicit model registration/selection through shared IO coordination."""
    feedback_workflow = "Training base model selection"
    PAGE_SIZE = 100

    def __init__(self, registry: ModelResourceRegistry, jobs, family: str, parent=None):
        super().__init__(parent)
        self.registry, self.jobs = registry, jobs
        self.selected_resource_id = ""
        self._task = None
        self._closing = False
        self._inspection = None
        self._snapshot_token = ""
        self._rows = []
        self._page = 0
        self.setWindowTitle("Choose Local Base Model")
        self.resize(820, 610)
        layout = QVBoxLayout(self)
        intro = QLabel("Choose an exact local model for this training plan.")
        intro.setObjectName("sectionTitle")
        layout.addWidget(intro)
        explanation = QLabel("CollectiGen reads the selected file to verify its SHA-256 identity and tensor container. It does not load the model. Family and trainer compatibility remain separate checks.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        row = QHBoxLayout()
        self.inspect_button = QPushButton("Inspect File…")
        self.inspect_button.clicked.connect(self._choose_file)
        row.addWidget(self.inspect_button)
        row.addWidget(QLabel("Model Family"))
        self.family = ModelFamilySelector(family)
        self.family.setAccessibleName("Model Family")
        row.addWidget(self.family, 1)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(self._load)
        row.addWidget(self.reload_button)
        layout.addLayout(row)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search registered models by name, family or exact hash")
        self.search.setAccessibleName("Search registered models")
        self.search.textChanged.connect(self._filter_changed)
        self.search.returnPressed.connect(self._filter_changed)
        layout.addWidget(self.search)
        self.count = QLabel("No registered model results yet.")
        layout.addWidget(self.count)
        self.models = QTreeWidget()
        self.models.setHeaderLabels(["Registered Model", "Declared Family", "Locations"])
        self.models.setRootIsDecorated(False)
        self.models.setColumnWidth(0, 340)
        self.models.setColumnWidth(1, 200)
        self.models.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.models, 1)
        pages = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.next = QPushButton("Next")
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        pages.addWidget(self.previous)
        pages.addWidget(self.next)
        pages.addStretch(1)
        layout.addLayout(pages)
        self.detail = QLabel("No selected model. Inspect a file or choose a registered entry.")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        bind_status_footer(self,self.status,title="Model registry")
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.use_button = QPushButton("Verify and Use Model")
        self.use_button.setProperty("primary", True)
        self.use_button.setAutoDefault(False)
        self.use_button.clicked.connect(self._use)
        buttons.addButton(self.use_button, QDialogButtonBox.ActionRole)
        self.recover_button = QPushButton("Recover Registry")
        self.recover_button.setAutoDefault(False)
        self.recover_button.clicked.connect(self._recover)
        buttons.addButton(self.recover_button, QDialogButtonBox.ActionRole)
        self.recover_button.hide()
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.family.textChanged.connect(self._update_actions)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._update_actions()
        QTimer.singleShot(0, self._load)

    def _run(self, name, callable_, complete, *, unit="items"):
        if self._task is not None:
            return
        def run(job):
            try:
                return callable_(job)
            except ResourceRecoveryRequired as exc:
                return _RecoveryNeeded(str(exc))
        task = CoordinatedCallTask(self.jobs, name, run, resource="io")
        self._task = task
        self.status.setText(name)
        from .job_progress import bind_job_progress
        binding = bind_job_progress(self, task, title=name, unit=unit, cancel_button=getattr(self, "cancel_work_button", None))
        task.signals.complete.connect(lambda result: self._finish(task, complete, result))
        task.signals.failed.connect(lambda error: self._finish(task, self._failed, error))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed, "Stopped at a safe boundary. Reload to see any completed registration."))
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            binding.finish(task)
            task.cancel()
            self._finish(task, self._failed, f"Could not start work: {exc}. Retry when background capacity is available.")

    def _finish(self, task, complete, result):
        if self._task is not task:
            return
        self._task = None
        if self._closing:
            super().reject()
            return
        if isinstance(result, _RecoveryNeeded):
            self._failed(result.message)
            self.recover_button.show()
        else:
            complete(result)
        self._update_actions()

    @staticmethod
    def _progress(job):
        def progress(done, total, label):
            # Qt's integer signals are signed 32-bit; model files often exceed
            # that range in bytes. Publish bounded MiB progress instead.
            units = max(1, (total + 2**20 - 1) // 2**20)
            job.update(units if done == total else done // 2**20, units, label)
        return progress

    def _load(self):
        if self._closing or self._task is not None:
            return
        self._inspection = None
        self.models.clear()
        self.detail.setText("No selected model.")
        def read(job):
            snapshot, token = self.registry.snapshot()
            rows = []
            for resource_id, record in snapshot["resources"].items():
                if job.cancel_requested:
                    raise InterruptedError("Reading registered models was cancelled.")
                declarations = [d for d in record["declarations"] if d.get("role") == "checkpoint"]
                if not declarations:
                    continue
                name = Path(record["locations"][0]["path"]).name if record["locations"] else resource_id
                families = ", ".join(sorted({d["family"] for d in declarations}))
                rows.append((name, families, len(record["locations"]), resource_id, f"{name} {families} {resource_id}".casefold()))
            rows.sort(key=lambda row: (row[0].casefold(), row[3]))
            return rows, token
        self._run("Reading registered model identities", read, self._loaded)

    def _loaded(self, result):
        self._rows, self._snapshot_token = result
        self._page = 0
        self.recover_button.hide()
        self.status.setText("Registered identities are cached. Use verifies the exact local bytes again.")
        self._render()

    def _filter_changed(self):
        self._page = 0
        self._render()

    def _change_page(self, delta):
        self._page = max(0, self._page + delta)
        self._render()

    def _render(self):
        query = self.search.text().strip().casefold()
        rows = [row for row in self._rows if not query or query in row[4]]
        self._filtered_count = len(rows)
        self._page = min(self._page, max(0, (len(rows) - 1) // self.PAGE_SIZE))
        start = self._page * self.PAGE_SIZE
        self.models.clear()
        for name, family, count, resource_id, _search_text in rows[start:start + self.PAGE_SIZE]:
            item = QTreeWidgetItem([name, family, str(count)])
            item.setData(0, Qt.UserRole, resource_id)
            item.setToolTip(0, resource_id)
            self.models.addTopLevelItem(item)
        self.count.setText(f"{len(rows):,} registered base model(s) · Showing {min(start + 1, len(rows)):,}–{min(start + self.PAGE_SIZE, len(rows)):,}")
        self.previous.setEnabled(self._page > 0 and self._task is None)
        self.next.setEnabled(start + self.PAGE_SIZE < len(rows) and self._task is None)
        self._update_actions()

    def _selection_changed(self):
        self._inspection = None
        item = self.models.currentItem()
        self.detail.setText(str(item.data(0, Qt.UserRole)) + "\nStored identity; local availability will be rechecked." if item else "No selected model.")
        self._update_actions()

    def _choose_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Inspect Local Base Model", "", "Safe tensor model (*.safetensors)")
        if filename:
            self.inspect_path(filename)

    def inspect_path(self, filename):
        self.models.clearSelection()
        self.models.setCurrentItem(None)
        self._inspection = None
        self.detail.setText("Inspecting the selected file; no model has been registered.")
        def inspect(job):
            _, token = self.registry.snapshot()
            result = inspect_model_resource(filename, progress=self._progress(job), should_cancel=lambda: job.cancel_requested)
            return result, token
        self._run("Inspecting selected model", inspect, self._inspected, unit="MiB")

    def _inspected(self, result):
        self._inspection, self._snapshot_token = result
        item = self._inspection
        self.detail.setText(f"{item.path.name}\n{item.resource_id}\n{item.size:,} bytes · {item.container['tensor_count']:,} tensor(s) · Container verified\nRegistering stores this local location and your declared family. It does not change the model or prove compatibility.")
        self.status.setText("Ready to register this identity and use it in the training plan.")

    def _use(self):
        inspection = self._inspection
        family = self.family.text().strip()
        token = self._snapshot_token
        item = self.models.currentItem()
        resource_id = item.data(0, Qt.UserRole) if item else ""
        def apply(job):
            progress, cancel = self._progress(job), lambda: job.cancel_requested
            if inspection is not None:
                return self.registry.register(inspection, role="checkpoint", family=family,
                    expected_registry=token, progress=progress, should_cancel=cancel)
            result = self.registry.resolve(resource_id, role="checkpoint", family=family, progress=progress, should_cancel=cancel)
            if result.status != "identity_verified":
                raise RuntimeError(result.reason)
            return result.resource_id
        self._run("Verifying selected base model", apply, self._used, unit="MiB")

    def _used(self, resource_id):
        self.selected_resource_id = resource_id
        self.accept()

    def _recover(self):
        self._run("Recovering model registry", lambda _job: self.registry.recover(), lambda _: self._load())

    def _failed(self, message):
        self._inspection = None
        self.models.clearSelection()
        self.models.setCurrentItem(None)
        self.detail.setText("No verified model is selected.")
        self.status.setText(str(message) + " Reload the registry or use Inspect File… to choose an available copy.")
        self.recover_button.hide()

    def _update_actions(self, *_):
        idle = self._task is None
        self.use_button.setText("Register and Use Model" if self._inspection else "Verify and Use Model")
        self.use_button.setEnabled(idle and bool(self.family.text().strip()) and (self._inspection is not None or self.models.currentItem() is not None))
        for widget in (self.inspect_button, self.reload_button, self.search, self.models, self.family, self.recover_button):
            widget.setEnabled(idle)
        self.previous.setEnabled(idle and self._page > 0)
        self.next.setEnabled(idle and (self._page + 1) * self.PAGE_SIZE < getattr(self, "_filtered_count", 0))

    def reject(self):
        self._closing = True
        if self._task is not None:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping after the current safe file or registry boundary…")
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
