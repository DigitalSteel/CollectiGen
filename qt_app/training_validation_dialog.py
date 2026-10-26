"""Retained review for a separate stopped-run SDXL held-out measurement."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.training_validation import (TrainingValidationError, TrainingValidationService,
    MAX_REQUEST, _qualification_matches_request, _read, qualified_profile,
    request_for_checkpoint_export, request_for_verified, restore_prepared,
    TrainingValidationOutcome)
from core.training_result import VerifiedTrainingResult
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


class ValidationLossChart(QWidget):
    """Mean epsilon denoising MSE at two fixed timesteps, never quality."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = ()
        self.setMinimumHeight(190)
        self.setAccessibleName('Held-out denoising MSE by timestep')

    def set_report(self, report):
        rows = report['values'] if report else ()
        self.values = tuple(math.fsum(row['mse_by_timestep'][index] for row in rows) / len(rows)
                            for index in range(2)) if rows else ()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(58, 22, -24, -35)
        foreground = self.palette().text().color()
        painter.setPen(QPen(foreground, 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.bottomLeft(), rect.topLeft())
        if not self.values:
            painter.drawText(rect, Qt.AlignCenter, 'No measured validation loss yet')
            return
        ceiling = max(self.values) * 1.15 or 1.0
        points = []
        for index, value in enumerate(self.values):
            x = rect.left() + rect.width() * index
            y = rect.bottom() - rect.height() * value / ceiling
            points.append((int(x), int(y)))
            painter.drawText(int(x) - 30, rect.bottom() + 6, 60, 22,
                             Qt.AlignCenter, str((250, 750)[index]))
        painter.setPen(QPen(QColor('#3e89d3'), 3))
        painter.drawLine(*points[0], *points[1])
        painter.setBrush(QColor('#3e89d3'))
        for point in points:
            painter.drawEllipse(point[0] - 4, point[1] - 4, 8, 8)
        painter.setPen(QPen(foreground, 1))
        painter.drawText(0, 4, self.width(), 18, Qt.AlignCenter,
                         'Mean denoising MSE · lower is a smaller noise-prediction error')


class TrainingValidationDialog(FeedbackEnabledDialog):
    reviewRetained = Signal(object)

    @classmethod
    def open_retained(cls, review_path, review_sha256=None, jobs=None, *, service=None, parent=None):
        """Reopen an exact saved review without the original training dialog.

        Review and process receipts are read by the coordinated background task.
        This entrypoint only rechecks/reports; it never starts GPU evaluation.
        """
        if jobs is None:
            raise TypeError('A background job manager is required to reopen validation.')
        path = Path(review_path).absolute()
        dialog = cls(None, {'review': {'images': {'roles': {}}}}, None, jobs,
            evaluation_root=path.parent, service=service,
            retained_review=(path, review_sha256), parent=parent)
        dialog.detail.setText('Reopening the exact retained Validation review and process receipt.')
        dialog.status.setText('Checking the retained evaluation off the UI thread…')
        QTimer.singleShot(0, dialog._recover)
        return dialog

    def __init__(self, plan, contract, selected, jobs, *, evaluation_root,
                 service=None, retained_review=None, parent=None):
        super().__init__(parent)
        self.plan, self.contract, self.selected = plan, deepcopy(contract), selected
        self.jobs, self.service = jobs, service or TrainingValidationService()
        self.evaluation_root = Path(evaluation_root).absolute()
        self.retained_review = retained_review
        self._request = None
        self._prepared = None
        self._report = None
        self._task = None
        self._closing = False
        self._phase = 'new'
        self._ever_started = False
        self._last_outcome_seen = False
        self.setWindowTitle('Held-out validation')
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        self.scope = QLabel('Measure a stopped SDXL LoRA against package images marked Validation. '
            'This is deterministic denoising MSE, not training loss or an image-quality score.')
        self.scope.setWordWrap(True); self.scope.setTextFormat(Qt.PlainText)
        layout.addWidget(self.scope)
        declared = contract.get('review', {}).get('images', {}).get('roles', {}).get('validation', 0)
        self.detail = QLabel(f'Images in the frozen Validation role: {declared:,} · '
            'two fixed timesteps (250, 750) · deterministic seed per image. '
            'The exact package and model will be rechecked before execution.')
        self.detail.setWordWrap(True); self.detail.setTextFormat(Qt.PlainText)
        layout.addWidget(self.detail)
        actions = QHBoxLayout(); layout.addLayout(actions)
        self.check_button = QPushButton('Check Readiness')
        self.check_button.clicked.connect(self._check); actions.addWidget(self.check_button)
        self.prepare_button = QPushButton('Prepare Evaluation Review')
        self.prepare_button.clicked.connect(self._prepare); actions.addWidget(self.prepare_button)
        self.start_button = QPushButton('Start Evaluation')
        self.start_button.clicked.connect(self._start); actions.addWidget(self.start_button)
        recovery_actions = QHBoxLayout(); recovery_actions.addStretch(); layout.addLayout(recovery_actions)
        self.cancel_button = QPushButton('Cancel Evaluation')
        self.cancel_button.clicked.connect(self._cancel); recovery_actions.addWidget(self.cancel_button)
        self.recover_button = QPushButton('Reopen / Check Recovery')
        self.recover_button.clicked.connect(self._recover); recovery_actions.addWidget(self.recover_button)
        self.status = QLabel('Check readiness. GPU work starts only after an exact review is prepared and Start is selected.')
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        self.status.setAccessibleName('Held-out evaluation status')
        layout.addWidget(self.status)
        self.chart = ValidationLossChart(self); layout.addWidget(self.chart)
        self.summary = QLabel('No validation measurement yet.')
        self.summary.setWordWrap(True); self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(('Validation item', 'MSE at 250', 'MSE at 750'))
        self.table.setAccessibleName('Per-image held-out denoising MSE')
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self._update()

    def _update(self):
        idle = self._task is None and not self._closing
        self.check_button.setEnabled(idle and self.retained_review is None)
        self.prepare_button.setEnabled(idle and self._request is not None
                                       and self._prepared is None and self._phase == 'checked')
        self.start_button.setEnabled(idle and self._prepared is not None
                                     and not self._prepared.candidate and self._phase == 'prepared')
        self.cancel_button.setEnabled(self._task is not None)
        self.recover_button.setEnabled(idle and (self.retained_review is not None or self._prepared is not None))

    def _launch(self, label, call, done):
        if self._task is not None or self._closing:
            return
        task = CoordinatedCallTask(self.jobs, label, call, resource='io')
        self._task = task
        self.status.setText(label + '…')
        self._update()
        def finish(value=None, error=''):
            if self._task is not task:
                return
            self._task = None
            if error:
                self.status.setText(error[:2000])
            else:
                try:
                    done(value)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.status.setText(str(exc)[:2000])
            self._update()
            if self._closing:
                if self._phase == 'recovery_required' or self._ever_started and not self._last_outcome_seen:
                    self._closing = False; self._update()
                    self.status.setText('The process outcome is uncertain. Keep this review and check recovery.')
                else:
                    super(TrainingValidationDialog, self).reject()
        task.signals.complete.connect(lambda value: finish(value))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(error='Evaluation request cancelled; retained evidence remains available.'))
        try:
            task.start()
        except Exception as exc:
            finish(error=str(exc))

    def _source_request(self, job):
        cancel = lambda: job.cancel_requested
        if type(self.selected) is VerifiedTrainingResult:
            return request_for_verified(self.plan, self.contract, self.selected, should_cancel=cancel)
        previous, export = self.selected
        return request_for_checkpoint_export(self.plan, self.contract, previous, export, should_cancel=cancel)

    def _check(self):
        if not self.check_button.isEnabled(): return
        def work(job):
            request = self._source_request(job)
            profile = qualified_profile()
            if profile is None:
                return request, 'unqualified'
            try:
                _qualification_matches_request(profile, request, self.contract)
            except TrainingValidationError:
                return request, 'selection_mismatch'
            return request, 'qualified'
        def done(value):
            self._request, readiness = value
            count = len(self._request['cases'])
            first = self._request['cases'][0]['seed']
            self.detail.setText(f'{count:,} exact Validation image(s) · timesteps 250 and 750 · '
                f'first per-image seed {first} · each image uses a stable seed bound to this run and item.')
            messages = {
                'qualified': 'Native held-out evaluator is qualified for this selection. Prepare the exact process review.',
                'unqualified': 'Held-out data is ready, but this evaluator has no independent native qualification. GPU start is unavailable.',
                'selection_mismatch': 'The native evaluator qualification does not cover this selected base, runtime, or software inventory. GPU start is unavailable.',
            }
            self.status.setText(messages[readiness])
            self._phase = 'checked' if readiness == 'qualified' else 'unqualified'
        self._launch('Checking package, model, and evaluator qualification', work, done)

    def _prepare(self):
        if not self.prepare_button.isEnabled() or self._phase != 'checked': return
        selected = self.selected
        inventory = {row['purpose']: row for row in self.contract['process']['inventories']}
        if set(inventory) != {'source_inventory', 'environment_inventory', 'python_base_inventory', 'package_inventory'}:
            self.status.setText('The original software inventory selection is incomplete.'); return
        name = 'heldout-' + uuid4().hex[:16]
        def work(job):
            return self.service.prepare(self.plan, self.contract, selected,
                root=self.evaluation_root, name=name, interpreter=self.plan.command[0],
                source_root=inventory['source_inventory']['root'],
                source_inventory=inventory['source_inventory']['manifest_path'],
                environment_root=inventory['environment_inventory']['root'],
                environment_inventory=inventory['environment_inventory']['manifest_path'],
                python_base_root=inventory['python_base_inventory']['root'],
                python_base_inventory=inventory['python_base_inventory']['manifest_path'],
                package_inventory=inventory['package_inventory']['manifest_path'],
                should_cancel=lambda: job.cancel_requested)
        def done(prepared):
            if prepared.candidate or prepared.request.get('qualification') != 'product':
                raise ValueError('A native candidate cannot authorize product evaluation.')
            self._prepared = prepared; self._phase = 'prepared'
            self.status.setText(f'Exact evaluation prepared: {prepared.plan.fingerprint}. '
                f'Retained review: {prepared.review_path}. Start explicitly to measure the selected stopped model.')
            self._publish_review(None)
        self._launch('Preparing exact held-out process review', work, done)

    def _start(self):
        if not self.start_button.isEnabled(): return
        self._phase = 'running'
        self._ever_started = True
        self._last_outcome_seen = False
        prepared = self._prepared
        self._launch('Measuring held-out denoising MSE', lambda job: self.service.execute(
            prepared, self.jobs, confirmed_fingerprint=prepared.plan.fingerprint,
            should_cancel=lambda: job.cancel_requested), self._accept_outcome)

    def _accept_outcome(self, outcome):
        if type(outcome) is not TrainingValidationOutcome or outcome.process is None:
            raise ValueError('The evaluator returned no process-owned outcome.')
        if (self._prepared is None or outcome.process.destination != self._prepared.plan.destination
                or self._prepared.candidate and outcome.status == 'complete'):
            raise ValueError('The evaluator returned another process or promoted a candidate.')
        self._last_outcome_seen = True
        self._phase = 'prepared' if outcome.status == 'held' else outcome.status
        self.status.setText(outcome.message)
        if outcome.report is not None:
            self._set_report(outcome.report)
        self._publish_review(outcome)

    def _publish_review(self, outcome):
        if (self._prepared is not None and self._prepared.review_path is not None
                and not self._prepared.candidate):
            self.reviewRetained.emit({'prepared': self._prepared,
                'outcome': outcome, 'registered': False})

    def _set_report(self, report):
        self._report = report
        self.chart.set_report(report)
        self.summary.setText(f"{len(report['values']):,} Validation image(s) · mean epsilon denoising MSE "
            f"{report['mean_mse']:.6g} · elapsed {report['elapsed_seconds']:.1f} s · "
            f"peak CUDA allocation {report['peak_cuda_memory_bytes'] / (1024**2):.1f} MiB. "
            'This is a held-out noise-prediction measurement, not image quality.')
        self.table.setRowCount(len(report['values']))
        for index, row in enumerate(report['values']):
            for column, value in enumerate((row['stable_id'], *row['mse_by_timestep'])):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()

    def _recover(self):
        if not self.recover_button.isEnabled(): return
        def work(job):
            del job
            prepared = self._prepared
            resolved_review = self.retained_review
            if prepared is None:
                path, expected_sha256 = resolved_review
                if expected_sha256 is None:
                    expected_sha256 = hashlib.sha256(_read(path, limit=MAX_REQUEST)).hexdigest()
                prepared = restore_prepared(path, expected_sha256)
                resolved_review = (path, expected_sha256)
            return prepared, self.service.recover(prepared), resolved_review
        def done(value):
            self._prepared = value[0]
            self.retained_review = value[2]
            self._accept_outcome(value[1])
        self._launch('Checking retained evaluator process and report', work, done)

    def _cancel(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)
            self.status.setText('Cancellation requested. Waiting for the owned process tree to stop or report recovery.')

    def reject(self):
        if self._task is not None:
            self._closing = True
            self._cancel()
            self._update()
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else:
            super().closeEvent(event)
