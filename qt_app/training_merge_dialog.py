"""Read-only Merge input review; never exposes component execution to the UI."""
from pathlib import Path
import json

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout)

from core.training_merge_inspection import MergeInputReview, inspect_merge_inputs
from .components import ContextHelpButton, DisclosureButton
from .dialog_lifetime import clear_learning_action_notice, settle_learning_action
from .job_progress import bind_job_progress
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


HELP = """# Inspect Merge Inputs
Choose two to four model adapter files, then choose Inspect. LoRAs are small
adapters applied to a base model. Inspection reads and hashes your selected
files; it does not load a model, create output or change the originals.

## Understand the result
Family and base-model information comes from file declarations. Matching
declarations or tensor shapes do not prove compatibility, licensing or quality.
Every production Merge operation remains unavailable until separately qualified.
This inspection does not check whether a model works or produces good images.

## Correct or stop a check
Remove or change selected files, then Inspect again. Stop inspection or Close
requests cancellation; wait for the original check to stop, then deliberately
retry or Close. Nothing automatically restarts. Details shows exact content
identities and a bounded target summary, without copying raw metadata.
"""


class TrainingMergeDialog(FeedbackEnabledDialog):
    background_work_finished = Signal()
    feedback_workflow = "Inspect Merge Inputs"

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self._task = None
        self._pending = None
        self._cancel_requested = False
        self._scope = jobs.create_execution_scope()
        self._paths = ()
        self._review = None
        self.setWindowTitle("Inspect Merge Inputs")
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        title = QLabel("Inspect Merge Inputs")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        intro = QLabel("Check model files before planning a merge. This inspection does not create or change a model. LoRAs are small model adapters.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Operation to review"))
        self.operation = QComboBox()
        self.operation.setAccessibleName("Merge operation to review")
        for label, value in (("Combine LoRAs", "combine_loras"),
                ("Blend checkpoints", "blend_checkpoints"),
                ("Bake LoRAs into a checkpoint", "bake_loras")):
            self.operation.addItem(label, value)
        row.addWidget(self.operation, 1)
        self.help_button = ContextHelpButton("Inspect Merge Inputs", HELP, self,
                                             focus_scope=self)
        row.addWidget(self.help_button)
        layout.addLayout(row)
        self.hold = QLabel("Merge execution unavailable — this family/operation has not been qualified. Inspection is read-only.")
        self.hold.setWordWrap(True)
        self.hold.setAccessibleName("Merge execution qualification hold")
        layout.addWidget(self.hold)
        self.files = QTreeWidget()
        self.files.setHeaderLabels(("Selected file", "Declared family", "Inspection"))
        self.files.setRootIsDecorated(False)
        self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.setAccessibleName("Selected Merge input files")
        self.files.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.files.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.files.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.files, 1)
        row = QHBoxLayout()
        self.add_button = QPushButton("Add Files…")
        self.remove_button = QPushButton("Remove Selected")
        self.inspect_button = QPushButton("Inspect")
        self.inspect_button.setProperty("primary", True)
        self.stop_button = QPushButton("Stop inspection")
        for button in (self.add_button, self.remove_button): row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.stop_button)
        row.addWidget(self.inspect_button)
        layout.addLayout(row)
        self.status = QLabel("Choose two to four safetensors files, then Inspect. No output will be created.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setAccessibleName("Merge inspection result and next action")
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Merge inspection technical details")
        self.details.setMaximumHeight(160)
        self.details_button = DisclosureButton("Details", "Hide Details", self.details)
        layout.addWidget(self.details_button)
        layout.addWidget(self.details)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_button = QPushButton("Close")
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.add_button.clicked.connect(self._choose_files)
        self.remove_button.clicked.connect(self._remove_selected)
        self.inspect_button.clicked.connect(self._inspect)
        self.stop_button.clicked.connect(self._stop)
        self.close_button.clicked.connect(self.close)
        self.operation.currentIndexChanged.connect(self._choice_changed)
        self.files.itemSelectionChanged.connect(self._update_actions)
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(50)
        self._drain_timer.timeout.connect(self._deliver_when_drained)
        for button in self.findChildren(QPushButton): button.setAutoDefault(False)
        self._update_actions()

    def _context(self):
        return self._paths, self.operation.currentData()

    def _admit(self, expected=None):
        ready = settle_learning_action(self, self._context, expected_context=expected,
                                       is_current=lambda: self._task is None)
        if ready: clear_learning_action_notice(self)
        return ready

    def _choose_files(self):
        if not self._admit(): return
        context = self._context()
        chosen, _filter = QFileDialog.getOpenFileNames(self, "Choose Merge Input Files", "",
                                                       "Safetensors files (*.safetensors)")
        if not chosen or not self._admit(context): return
        paths = tuple(dict.fromkeys((*self._paths, *(str(Path(p).absolute()) for p in chosen))))
        if len(paths) > 4:
            self.status.setText("Choose at most four files. Remove a selected file before adding more; the current selection was kept.")
            return
        self._paths = paths
        self._choice_changed()

    def _remove_selected(self):
        if not self._admit(): return
        removed = {item.data(0, Qt.UserRole) for item in self.files.selectedItems()}
        self._paths = tuple(path for path in self._paths if path not in removed)
        self._choice_changed()

    def _choice_changed(self, *_):
        if self._task is not None: return
        self._review = None
        self.details.clear()
        self.files.clear()
        for path in self._paths:
            item = QTreeWidgetItem((Path(path).name, "Not inspected", "Choose Inspect"))
            item.setData(0, Qt.UserRole, path)
            item.setToolTip(0, path)
            self.files.addTopLevelItem(item)
        self.status.setText(f"{len(self._paths)} selected. Choose two to four files, then Inspect. Previous results are cleared; nothing starts automatically.")
        self._update_actions()

    def _update_actions(self):
        idle = self._task is None
        self.add_button.setEnabled(idle)
        self.remove_button.setEnabled(idle and bool(self.files.selectedItems()))
        self.operation.setEnabled(idle)
        self.files.setEnabled(idle)
        self.inspect_button.setEnabled(idle and 2 <= len(self._paths) <= 4)
        self.stop_button.setVisible(not idle)
        self.stop_button.setEnabled(not idle and not self._cancel_requested)
        self.details_button.setEnabled(idle and self._review is not None)

    def _inspect(self):
        if not 2 <= len(self._paths) <= 4 or not self._admit(): return
        paths, operation = self._context()
        self._review = None
        self.details.clear()
        self._cancel_requested = False
        self._pending = None
        task = CoordinatedCallTask(self.jobs, "Inspecting Merge input files",
            lambda job: inspect_merge_inputs(paths, operation=operation,
                progress=job.update, should_cancel=lambda: not job.checkpoint()),
            resource="io", execution_scope=self._scope)
        self._task = task
        task.signals.complete.connect(lambda value: self._received(task, value=value))
        task.signals.failed.connect(lambda _error: self._received(task, error=True))
        task.signals.cancelled.connect(lambda: self._received(task, cancelled=True))
        self._binding = bind_job_progress(self, task, title="Inspecting Merge inputs", unit="bytes",
            cancel_button=self.stop_button, drain_guard=self._scope.physically_drained)
        self.status.setText("Reading and hashing the selected files. Originals stay unchanged; no model is loaded.")
        self._update_actions()
        self._drain_timer.start()
        try: task.start()
        except Exception:
            # Unconfirmed admission stays owned until the exact scope drains.
            self._pending = {"error": True}

    def _received(self, task, **pending):
        if task is self._task:
            self._pending = pending
            self._deliver_when_drained()

    def _deliver_when_drained(self):
        if self._task is None or self._pending is None or not self._scope.physically_drained(): return
        pending = self._pending
        self._pending = None
        self._binding.finish(self._task)
        self._task = None
        self._drain_timer.stop()
        if self._cancel_requested or pending.get("cancelled"):
            self.status.setText("Inspection stopped. Originals are unchanged. Choose Inspect to retry or Close; nothing restarts automatically.")
        elif pending.get("error") or not isinstance(pending.get("value"), MergeInputReview):
            self.status.setText("Inspection could not finish. A file may have changed, be unreadable or use an unsupported format. Review the selection and choose Inspect again. No output was created.")
        else:
            review = pending["value"]
            self._review = review
            for index, record in enumerate(review.inputs):
                item = self.files.topLevelItem(index)
                item.setText(1, record.family if record.family != "unknown" else "Unknown")
                item.setText(2, "Needs attention" if record.issues else "Inspected")
            self.status.setText("Input inspection complete. " + (
                " ".join(self._issue_text(issue) for issue in review.issues) if review.issues else
                "No structural conflicts found in the selected adapter declarations.") +
                " Merge execution remains unavailable; no output was created.")
            blocks = []
            for record in review.inputs:
                precision = ", ".join(sorted({row["dtype"] for key, row in json.loads(record.header).items() if key != "__metadata__"}))
                targets = "\n".join(f"  {name}: rank {rank}, {width} → {height}" for name, rank, width, height in record.targets[:12])
                blocks.append(f"{record.path.name}\nContent identity: {record.resource_id}\nDeclared family: {record.family}\nDeclared base SHA-256: {record.base_sha256 or 'Unavailable'}\nInput size: {record.size:,} bytes\nInput precision: {precision}\nTargets ({len(record.targets)}; showing up to 12):\n{targets}")
            self.details.setPlainText("\n\n".join(blocks))
        self._update_actions()
        clear_learning_action_notice(self)
        self.background_work_finished.emit()

    @staticmethod
    def _issue_text(issue):
        if issue.startswith("Checkpoint blending and baking"):
            return "This operation has no qualified model mapping. Inspection can show file declarations only."
        if issue.startswith("This component writes F32"):
            return "An input uses a precision outside the current F32 inspection profile. Review Details or choose another file."
        if issue.startswith("Real-size execution is held"):
            return "The inputs exceed the small-file inspection profile (256 KiB per file). Large-model execution is not qualified."
        return issue

    def _stop(self):
        if self._task is None: return
        # Settle reading before changing cancellation or intent state.
        if not settle_learning_action(self, self._context): return
        clear_learning_action_notice(self)
        self._cancel_requested = True
        if self._task.job_id: self.jobs.cancel(self._task.job_id)
        self.status.setText("Stopping inspection at the next safe read boundary. Wait for the original check to stop, then choose Inspect or Close.")
        self._update_actions()

    def _reading_ready(self):
        if not settle_learning_action(self, self._context): return False
        help_dialog = self.help_button._help_dialog
        if help_dialog is not None:
            from shiboken6 import isValid
            if isValid(help_dialog) and not help_dialog.close(): return False
        clear_learning_action_notice(self)
        return True

    def done(self, result):
        if not self._reading_ready(): return
        if self._task is not None:
            self._stop()
            return
        super().done(result)

    def closeEvent(self, event):
        if not self._reading_ready():
            event.ignore()
        elif self._task is not None:
            self._stop()
            event.ignore()
        else:
            super().closeEvent(event)
