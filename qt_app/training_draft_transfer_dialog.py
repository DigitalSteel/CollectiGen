"""Move canonical Training drafts between Manager and the Dataset editor."""
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QDialogButtonBox, QFileDialog, QLabel, QPushButton, QVBoxLayout
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask
from core.training_draft_transfer import export_training_draft, import_training_draft
from core.training_plan_drafts import TrainingPlanDraftStore


class TrainingDraftTransferDialog(FeedbackEnabledDialog):
    def __init__(self, jobs, store_path, parent=None, *, importing=False, selected=None):
        super().__init__(parent)
        self.jobs, self.store_path = jobs, Path(store_path)
        self.importing, self.imported_draft = importing, None
        self._task, self._closing = None, False
        self._drafts = ()
        self._selected = selected
        self.setWindowTitle("Import Training Draft" if importing else "Edit a Cloned Training Draft")
        self.resize(620, 260)
        layout = QVBoxLayout(self)
        intro = QLabel("Import a draft exported by Suite Manager, then review and edit it with your current Dataset libraries. A fresh saved copy preserves the original."
            if importing else "Export this saved draft, then open Organizer → Dataset → Library Actions → Import Training Draft. Review its images and edit its settings there; the original experiment is preserved.")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.choice = QComboBox(); self.choice.setAccessibleName("Saved Training draft")
        self.choice.setVisible(not importing); layout.addWidget(self.choice)
        self.status = QLabel("Choose a draft file." if importing else "Reading saved drafts…")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.transfer_button = QPushButton("Import Draft…" if importing else "Export for Organizer…")
        self.transfer_button.clicked.connect(self._transfer)
        self.transfer_button.setEnabled(importing)
        buttons.addButton(self.transfer_button, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)
        if not importing:
            QTimer.singleShot(0, self._load)

    def _load(self):
        def read(job):
            store = TrainingPlanDraftStore(self.store_path)
            if store.load_error: raise ValueError(store.load_error)
            return store.all_drafts()
        self._launch("load", read)

    def _transfer(self):
        if self._task is not None or self._closing: return
        if self.importing:
            path, _ = QFileDialog.getOpenFileName(self, "Import Training Draft", "", "Training draft (*.json)")
            if path:
                self._launch("import", lambda job: import_training_draft(path,
                    TrainingPlanDraftStore(self.store_path), should_cancel=lambda: job.cancel_requested))
        else:
            draft = next((d for d in self._drafts if d.draft_id == self.choice.currentData()), None)
            if draft is None: return
            path, _ = QFileDialog.getSaveFileName(self, "Export a New Training Draft", "training-draft.json", "Training draft (*.json)")
            if path:
                self._launch("export", lambda job: export_training_draft(draft, path,
                    should_cancel=lambda: job.cancel_requested))

    def _launch(self, action, operation):
        if self._task is not None or self._closing: return
        result = {}
        def work(job):
            if job.cancel_requested: raise InterruptedError("Draft transfer cancelled.")
            result["value"] = operation(job)
            return result["value"]
        task = CoordinatedCallTask(self.jobs, "Training draft: " + action, work, resource="io")
        self._task = task; self.transfer_button.setEnabled(False)
        self.status.setText("Reading saved drafts…" if action == "load" else "Saving draft…")
        task.signals.complete.connect(lambda value: self._finish(task, action, result.get("value", value)))
        task.signals.failed.connect(lambda error: self._finish(task, action, error=error))
        task.signals.cancelled.connect(lambda: self._finish(task, action, result.get("value"), "Draft transfer cancelled."))
        try: task.start()
        except Exception as exc: self._finish(task, action, error=str(exc))

    def _finish(self, task, action, value=None, error=""):
        if task is not self._task: return
        self._task = None
        if action == "import" and value is not None:
            self.imported_draft = value
        if self._closing:
            super().reject(); return
        if error and value is None:
            self.status.setText(str(error)[:2000])
        elif action == "load":
            self._drafts = value
            self.choice.clear()
            for draft in value: self.choice.addItem(draft.name, draft.draft_id)
            if self._selected is not None:
                self.choice.setCurrentIndex(max(0, self.choice.findData(self._selected.draft_id)))
            self.status.setText("Choose a draft to export." if value else "Clone a saved execution run in Training Experiments first.")
        elif action == "import":
            self.accept(); return
        else:
            self.status.setText("Draft exported to " + str(value) + ". Import it in Organizer to edit its settings.")
        self.transfer_button.setEnabled(self.importing or bool(self._drafts))

    def reject(self):
        if self._task is not None:
            self._closing = True
            if self._task.job_id: self.jobs.cancel(self._task.job_id)
            self.status.setText("Finishing the current draft operation before closing…")
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else: super().closeEvent(event)
