"""Background opening and explicit process recovery for retained run reviews."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPushButton, QVBoxLayout

from core.training_run_recovery import restore_training_context, recover_plan_process, note_outcome
from core.training_process import TrainingProcessService, TrainingRunJournal
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


class TrainingRestoreDialog(FeedbackEnabledDialog):
    restored = Signal(object)

    def __init__(self, index, run_id, revision, jobs, parent=None):
        super().__init__(parent)
        self.index, self.run_id, self.revision, self.jobs = index, run_id, revision, jobs
        self._task = None
        self._closing = False
        self._readiness = None
        self._package_error = ''
        self.setWindowTitle('Open Retained Training Review')
        self.resize(660, 360)
        layout = QVBoxLayout(self)
        self.message = QLabel('Checking the selected review and its exact package…')
        self.message.setTextFormat(Qt.PlainText); self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.destination = QLabel('')
        self.destination.setTextFormat(Qt.PlainText); self.destination.setWordWrap(True)
        self.destination.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.destination)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.recover_button = QPushButton('Check Process Recovery')
        self.recover_button.clicked.connect(self._recover)
        self.recover_button.hide()
        buttons.addButton(self.recover_button, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)
        QTimer.singleShot(0, self._open)

    def _launch(self, label, operation, complete):
        if self._closing or self._task is not None: return
        task = CoordinatedCallTask(self.jobs, label, operation, resource='io')
        self._task = task
        self.recover_button.setEnabled(False)
        task.signals.complete.connect(lambda value: self._finish(task, complete, value))
        task.signals.failed.connect(lambda error: self._finish(task, complete, None, str(error)))
        task.signals.cancelled.connect(lambda: self._finish(task, complete, None, 'Review check cancelled.'))
        try: task.start()
        except Exception as exc: self._finish(task, complete, None, str(exc))

    def _finish(self, task, complete, value, error=''):
        if self._task is not task: return
        self._task = None
        if self._closing: super().reject(); return
        if value is None: self.message.setText(str(error)[:2000])
        else: complete(value)
        self.recover_button.setEnabled(self._readiness is not None)

    def _open(self):
        self._launch('Open retained training review', lambda job:
            restore_training_context(self.index, self.run_id, self.revision,
                should_cancel=lambda: job.cancel_requested, allow_process_only=True), self._opened)

    def _opened(self, context):
        if context.review.record.kind == 'execution' and not context.package_error:
            self.restored.emit(context)
            super().accept()
            return
        self._readiness = context.review
        self._package_error = context.package_error
        if self._package_error:
            self.setWindowTitle('Recover Retained Training Process')
            self.destination.setText('Workspace: ' + str(context.review.plan.destination))
            self.message.setText(self._package_error + ' You can still check process recovery. Output verification and resume remain unavailable until the exact package is restored.')
            self.recover_button.show()
            return
        self.setWindowTitle('Retained Trainer Readiness Check')
        self.destination.setText('Workspace: ' + str(context.review.plan.destination))
        self.message.setText('This is a retained setup check. Check process recovery to reconcile an interrupted trainer. Prepare a fresh training review to check current readiness.')
        self.recover_button.show()

    def _recover(self):
        if self._readiness is None: return
        plan = self._readiness.plan
        def work(job):
            if job.cancel_requested: raise InterruptedError('Recovery check cancelled.')
            tip = self._readiness.record.latest_journal_tip or TrainingRunJournal(plan.destination).read()[1]
            result = recover_plan_process(plan, TrainingProcessService(), tip)
            phase = ('recovery_required' if not result.tree_stopped or result.status == 'recovery_required'
                     else result.status if result.status in {'failed', 'cancelled', 'interrupted'} else 'stopped')
            saved = note_outcome(self.index, plan, phase, result.journal_tip)
            return result, saved
        def done(value):
            from dataclasses import replace
            result, saved = value
            self._readiness = replace(self._readiness,
                record=replace(self._readiness.record, latest_journal_tip=result.journal_tip))
            self.message.setText(('The trainer process has stopped. Its workspace is retained. Restore the exact package and reopen this run before output verification or resume.' if self._package_error else
                'The readiness process has stopped. Its workspace is retained. Prepare a fresh training review to verify current resources.') if result.tree_stopped and result.status != 'recovery_required' else
                'Process recovery still needs attention. Keep this workspace and check recovery again.')
            if not saved:
                self.message.setText(self.message.text() + ' The saved run list needs recovery; keep this workspace and review.')
        self._launch('Check retained trainer process', work, done)

    def reject(self):
        self._closing = True
        if self._task is not None:
            self.jobs.cancel(self._task.job_id)
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None: event.ignore(); self.reject()
        else: super().closeEvent(event)
