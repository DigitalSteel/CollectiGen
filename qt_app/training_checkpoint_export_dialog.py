"""Explicit checkpoint publication and registration, outside the original run."""
from copy import deepcopy
import hashlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialogButtonBox, QFileDialog, QLabel, QPushButton, QVBoxLayout

from core.training_checkpoint_export import export_checkpoint, register_checkpoint_export
from core.training_process import _bytes
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


class TrainingCheckpointExportDialog(FeedbackEnabledDialog):
    resourceRegistered = Signal(str)
    generationRequested = Signal(str)
    evaluationRequested = Signal(str)
    validationRequested = Signal(object)

    def __init__(self, plan, contract, previous, registry, jobs, parent=None):
        super().__init__(parent)
        self.plan,self.contract,self.previous=plan,deepcopy(contract),deepcopy(previous)
        self.registry,self.jobs=registry,jobs
        self.export=None;self.revision='';self.resource_id=''
        self._task=None;self._closing=False
        self.setWindowTitle('Export Training Checkpoint');self.resize(740,450)
        layout=QVBoxLayout(self)
        self.detail=QLabel(f"Step {previous['step']:,} · Epoch {previous['epoch']:,}\n"
            "Export this verified checkpoint as a separate FP32 LoRA for evaluation. "
            "The original run and its recovery snapshot are retained. Output quality is unmeasured.")
        self.detail.setWordWrap(True);self.detail.setTextFormat(Qt.PlainText);layout.addWidget(self.detail)
        self.export_button=QPushButton('Choose New Destination and Export…')
        self.export_button.clicked.connect(self._export);layout.addWidget(self.export_button)
        self.review_button=QPushButton('Review Model Registration')
        self.review_button.clicked.connect(self._review);layout.addWidget(self.review_button)
        self.register_button=QPushButton('Register This Evaluation LoRA')
        self.register_button.clicked.connect(self._register);layout.addWidget(self.register_button)
        self.generate_button=QPushButton('Use in Generate…')
        self.generate_button.clicked.connect(lambda: self.generationRequested.emit(self.resource_id));layout.addWidget(self.generate_button)
        self.evaluation_button=QPushButton('Compare Checkpoint in Generate…')
        self.evaluation_button.clicked.connect(lambda: self.evaluationRequested.emit(self.resource_id)
            if self.evaluation_button.isEnabled() else None);layout.addWidget(self.evaluation_button)
        self.validation_button=QPushButton('Measure Held-Out Validation…')
        self.validation_button.clicked.connect(lambda: self.validationRequested.emit((deepcopy(self.previous), self.export))
            if self.validation_button.isEnabled() else None);layout.addWidget(self.validation_button)
        self.status=QLabel('Choose a new filename outside the retained training run.')
        self.status.setTextFormat(Qt.PlainText);self.status.setWordWrap(True);layout.addWidget(self.status,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self._update()

    def _update(self):
        idle=self._task is None and not self._closing
        self.export_button.setEnabled(idle)
        self.review_button.setEnabled(idle and self.export is not None)
        self.register_button.setEnabled(idle and bool(self.revision) and not self.resource_id)
        self.generate_button.setEnabled(idle and bool(self.resource_id))
        self.evaluation_button.setEnabled(idle and bool(self.resource_id))
        self.validation_button.setEnabled(idle and self.export is not None)

    def _launch(self,label,work,done):
        if self._task is not None or self._closing:return
        task=CoordinatedCallTask(self.jobs,label,work,resource='io');self._task=task
        self.status.setText(label);self._update()
        def finish(value=None,error=''):
            if self._task is not task:return
            self._task=None
            if self._closing:super(TrainingCheckpointExportDialog,self).reject();return
            if error:self.status.setText(str(error)[:2000])
            else:done(value)
            self._update()
        task.signals.complete.connect(lambda value:finish(value))
        task.signals.failed.connect(lambda error:finish(error=error))
        task.signals.cancelled.connect(lambda:finish(error='Checkpoint review cancelled. Retained files are preserved.'))
        try:task.start()
        except Exception as exc:finish(error=str(exc))

    def _export(self):
        path,_=QFileDialog.getSaveFileName(self,'Export Evaluation LoRA','','LoRA (*.safetensors)')
        if not path:return
        self.revision='';self.resource_id='';self.export=None
        def done(value):
            self.export=value
            self.status.setText(f'Export verified: {value.path}\nSHA-256: {value.sha256}\nReview registration to make this checkpoint available in Models and Generate.')
        self._launch('Rechecking checkpoint and exporting LoRA…',lambda job:export_checkpoint(
            self.plan,self.contract,self.previous,path,
            confirmed_selection=hashlib.sha256(_bytes(self.previous)).hexdigest(),
            should_cancel=lambda:job.cancel_requested),done)

    def _review(self):
        if self.export is None:return
        def done(value):
            self.revision=value[1]
            self.status.setText('Confirm registration of the exported checkpoint with its exact package, base model and optimizer step lineage. Compatibility is rechecked by Generate before execution.')
        self._launch('Reading current Models registry…',lambda job:self.registry.snapshot(),done)

    def _register(self):
        if self.export is None or not self.revision:return
        def done(value):
            self.resource_id=value;self.resourceRegistered.emit(value)
            self.status.setText('Evaluation LoRA registered. Add it to a fixed-prompt comparison through Generate.')
        self._launch('Rechecking export and registering exact lineage…',lambda job:register_checkpoint_export(
            self.export,self.plan,self.contract,self.previous,self.registry,
            expected_registry=self.revision,should_cancel=lambda:job.cancel_requested),done)

    def reject(self):
        self._closing=True
        if self._task is not None:self.jobs.cancel(self._task.job_id);return
        super().reject()

    def closeEvent(self,event):
        if self._task is not None:event.ignore();self.reject()
        else:super().closeEvent(event)
