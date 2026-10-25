"""Review and observe one prepared local run through shared process ownership."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
from itertools import islice
import json
from pathlib import Path
import re
from threading import Event
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from core.training_execution import TrainingExecutionOutcome, TrainingExecutionService
from core.training_process import TrainingProcessPlan, TrainingProcessResult, TrainingRunJournal, _bytes
from core.training_progress import validate_progress
from core.training_metrics import TrainingMetricsSnapshot
from core.training_result import VerifiedTrainingResult
from core.training_trial import (TrainingTrialLimits, load_intent as load_trial_intent,
    load_receipt as load_trial_receipt, receipt_matches_process)
from core.training_resource_guidance import TrialMemoryObservation, load_observation
from .components import bind_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .training_metrics_widget import TrainingMetricsWidget
from .workers import CoordinatedCallTask


SHA = re.compile(r'[0-9a-f]{64}\Z')
RESOURCE = re.compile(r'sha256:[0-9a-f]{64}\Z')
STATES = {'held', 'stopping', 'recovery_required', 'cancelled', 'failed', 'interrupted', 'verification_required',
           'registration_required', 'completed', 'trial_stopped', 'trial_cancelled', 'trial_failed',
           'trial_interrupted', 'trial_ended_before_limit', 'trial_recovery_required'}
TRIAL_STATES = {'trial_stopped', 'trial_cancelled', 'trial_failed', 'trial_interrupted',
                'trial_ended_before_limit', 'trial_recovery_required'}
FIXES = {
    'out_of_memory': 'Review GPU memory use, resolution, batch size, and checkpointing in a new plan before trying again.',
    'nonfinite': 'Review precision and learning settings in a new plan. Retained output has not passed verification.',
    'resource_unavailable': 'Verify or repair the selected trainer and resources in Setup, then prepare a fresh review.',
    'workspace_unavailable': 'Check free storage and access to this run workspace before reviewing a retained checkpoint.',
    'input_changed': 'Recheck the selected package, resources, and configuration before preparing another run.',
    'runtime_failed': 'Review this retained run and its verified checkpoints before deciding how to continue.',
}


@dataclass(frozen=True)
class _Delivery:
    fingerprint: str
    outcome: TrainingExecutionOutcome
    checkpoints: tuple
    trial_limits: TrainingTrialLimits | None = None


def _review(contract):
    """Bounded presentation projection; no files, hashing, or per-image rows."""
    review = contract['review']
    images, estimates = review['images'], review['estimates']
    if type(images['count']) is not int or images['count'] < 1 or type(images['roles']) is not dict:
        raise ValueError('The prepared image scope is unavailable.')
    roles = ', '.join(f'{name}: {count:,}' for name, count in islice(images['roles'].items(), 8))
    if len(images['roles']) > 8: roles += f' · {len(images["roles"]):,} roles total; review the package for all roles'
    resources = contract['resources']
    output = contract['result_binding']['output_relative_path']
    details = {name: deepcopy(value) for name, value in (
        ('configuration', contract['recipe']['configuration']),
        ('trainer_arguments', review['trainer_arguments']), ('command', contract['process']['command']),
        ('checkpoint', review['checkpoint']), ('privacy', review['privacy']),
        ('sampling', review['sampling']), ('resources', review['resources']))}
    return (
        f"{images['count']:,} selected images · {roles}",
        f"Base resource: {resources['base']['id']}\nOutput: {output}",
        f"{estimates['optimizer_steps']:,} optimizer steps · {estimates['epochs']:,} epochs · {estimates['checkpoints']:,} planned checkpoints\nGPU time and peak memory estimates are not supplied by this review.",
        json.dumps(details, ensure_ascii=False, indent=2, allow_nan=False)[:100000],
    )


def _validated_outcome(outcome, plan, contract, fingerprint, contract_raw, trial_limits=None):
    """Check callback bindings in the worker; this never grants new authority."""
    if (not isinstance(outcome, TrainingExecutionOutcome) or outcome.status not in STATES
            or outcome.destination != plan.destination or type(outcome.message) is not str
            or not isinstance(outcome.process, TrainingProcessResult)
            or outcome.process.destination != plan.destination
            or outcome.status not in {'stopping', 'held'} and outcome.process.tree_stopped is not True
            or outcome.status == 'stopping' and outcome.process.tree_stopped is not False
            or outcome.status == 'held' and (outcome.process.status != 'held' or outcome.process.tree_stopped is not False)
            or outcome.failure_code not in {'', *FIXES}):
        raise ValueError('The run returned an invalid or stale outcome. Retain its workspace and review recovery.')
    if outcome.status in {'registration_required', 'completed'}:
        if trial_limits is not None:
            raise ValueError('A measured trial cannot present a registered training result.')
        verified = outcome.verified
        if (not isinstance(verified, VerifiedTrainingResult) or verified.plan.fingerprint != fingerprint
                or verified.contract_json != contract_raw or verified.outcome.tree_stopped is not True
                or len(verified.receipt_json) > 512 * 1024):
            raise ValueError('The verified result belongs to a different review. Retain the workspace.')
        if outcome.status == 'completed' and (type(outcome.resource_id) is not str
                or RESOURCE.fullmatch(outcome.resource_id) is None
                or verified.receipt.get('output', {}).get('resource_id') != outcome.resource_id):
            raise ValueError('The registered resource does not match the verified output.')
    if trial_limits is not None:
        trial_limits.validate(contract['result_binding']['expected_final_step'])
        if outcome.status not in TRIAL_STATES | {'held', 'stopping'}:
            raise ValueError('The trial returned a normal-training outcome.')
        if outcome.status in TRIAL_STATES - {'trial_recovery_required'}:
            receipt = outcome.trial
            if (receipt is None or receipt.process_fingerprint != fingerprint
                    or receipt.execution_contract_sha256 != plan.preview_fingerprint
                    or receipt.process_journal_tip != outcome.process.journal_tip
                    or receipt.limits != trial_limits or receipt.status != outcome.status
                    or not receipt_matches_process(receipt, outcome.process)):
                raise ValueError('The measured trial receipt does not match this stopped run.')
        if outcome.memory is not None and (type(outcome.memory) is not TrialMemoryObservation
                or not outcome.memory.valid_for(plan, contract)):
            # Optional telemetry never invalidates a canonical stopped trial.
            outcome = replace(outcome, memory=None)
    elif outcome.status in TRIAL_STATES or outcome.trial is not None:
        raise ValueError('A trial result has no matching reviewed trial intent.')
    if type(outcome.checkpoints) is not tuple or len(outcome.checkpoints) > 1000:
        raise ValueError('The checkpoint overview exceeds its supported bound. Review the retained run.')
    maximum = contract['result_binding']['expected_final_step']
    binding = {'run_id': plan.run_id, 'process_sha256': fingerprint, 'preview_sha256': plan.preview_fingerprint}
    choices, seen = [], set()
    fields = {'destination', 'binding', 'anchor_sha256', 'sequence', 'name', 'step', 'epoch',
              'checkpoint_receipt_sha256', 'continuation_receipt_sha256'}
    for row in outcome.checkpoints:
        if type(row) is not dict or set(row) != {'sequence', 'step', 'epoch', 'previous'}:
            raise ValueError('A checkpoint selection has changed. Review the retained run.')
        sequence, step, epoch, previous = (row[key] for key in ('sequence', 'step', 'epoch', 'previous'))
        if (type(sequence) is not int or not 0 <= sequence < 1000 or sequence in seen
                or type(step) is not int or not 1 <= step < maximum or type(epoch) is not int or epoch < 1
                or type(previous) is not dict or set(previous) != fields
                or previous['destination'] != str(plan.destination) or previous['binding'] != binding
                or previous['sequence'] != sequence or previous['step'] != step or previous['epoch'] != epoch
                or previous['name'] != f'training-step{step:08d}-state'
                or previous['anchor_sha256'] != outcome.checkpoint_anchor_sha256
                or any(type(previous[name]) is not str or SHA.fullmatch(previous[name]) is None for name in
                       ('anchor_sha256', 'checkpoint_receipt_sha256', 'continuation_receipt_sha256'))):
            raise ValueError('A checkpoint does not match this stopped run. Review recovery before continuing.')
        seen.add(sequence)
        choices.append((f'Step {step:,} · Epoch {epoch:,} · Checkpoint {sequence + 1:,}', deepcopy(previous)))
    return _Delivery(fingerprint, outcome, tuple(choices), trial_limits)


class TrainingRunDialog(FeedbackEnabledDialog):
    reviewPackageRequested = Signal(object)
    resumeRequested = Signal(object)
    retryVerificationRequested = Signal(object)
    registerRequested = Signal(object)
    resourceRegistered = Signal(str)
    generationPlanningRequested = Signal(str)
    checkpointExportRequested = Signal(object)
    validationRequested = Signal(object)
    evaluationRequested = Signal(str)
    _progressReceived = Signal(object, object)
    _metricsReceived = Signal(object, object, object, object)
    feedback_workflow = 'Local training'

    def __init__(self, plan, contract, jobs, registry, *, expected_registry,
                 service=None, goal_summary='', retained=False, retained_tip='', parent=None):
        super().__init__(parent)
        self.plan, self.jobs, self.registry = plan, jobs, registry
        self.contract = deepcopy(contract) if type(contract) is dict else {}
        self.expected_registry = expected_registry
        self.service = service if service is not None else TrainingExecutionService()
        self._task = self._outcome = None
        self._cancel_event = Event()
        self._process_invoked = Event()
        self._close_requested = self._observed_running = self._attempted = False
        self._serial = self._revision = self._step = 0
        self._state, self._phase = 'review', 'Review this prepared run. No training has started.'
        self._restored = bool(retained)
        if type(retained_tip) is not str or retained_tip and SHA.fullmatch(retained_tip) is None:
            raise ValueError('The retained process receipt is invalid.')
        self._retained_tip = retained_tip
        self._trial_limits: TrainingTrialLimits | None = None
        if self._restored:
            self._attempted = True
            self._state, self._phase = 'recovery_required', 'Retained run. Check process recovery before verifying output or choosing a checkpoint.'
        self._started = 0.0
        self._ready = isinstance(plan, TrainingProcessPlan) and type(contract) is dict
        self.setWindowTitle('Review Local Training')
        self.resize(940, 760)
        layout = QVBoxLayout(self)
        self.heading = self._label('Retained Local Training' if self._restored else 'Review and Start Local Training', 'sectionTitle')
        layout.addWidget(self.heading)
        layout.addWidget(self._label(str(goal_summary or 'Train the reviewed LoRA on this computer.')[:1000]))
        self.state_label = self._label(self._phase)
        layout.addWidget(self.state_label)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        review_page = QWidget(); review_layout = QVBoxLayout(review_page)
        self.scope = self._label('A prepared training plan is required.')
        self.resources = self._label('')
        self.estimates = self._label('')
        for label in (self.scope, self.resources, self.estimates): review_layout.addWidget(label)
        self.trial_toggle = QCheckBox('Measure a short trial instead of completing this training run')
        self.trial_toggle.setObjectName('training_trial_toggle')
        review_layout.addWidget(self.trial_toggle)
        self.trial_explanation = self._label(
            'A trial stops this exact local run at the first reviewed step or time limit. '
            'It measures elapsed optimizer-step time after warmup and attempts bounded process GPU-memory polls. '
            'Memory telemetry can be unavailable or miss a higher peak; no trial model is verified or registered.')
        review_layout.addWidget(self.trial_explanation)
        self.trial_steps = QSpinBox()
        self.trial_steps.setRange(2, 10)
        self.trial_steps.setValue(10)
        self.trial_steps.setSuffix(' optimizer steps')
        self.trial_steps.setAccessibleName('Measured trial maximum optimizer steps')
        review_layout.addWidget(self.trial_steps)
        self.trial_seconds = QSpinBox()
        self.trial_seconds.setRange(60, 1200)
        self.trial_seconds.setValue(300)
        self.trial_seconds.setSuffix(' seconds')
        self.trial_seconds.setAccessibleName('Measured trial maximum elapsed seconds')
        review_layout.addWidget(self.trial_seconds)
        self.trial_confirm = QCheckBox('I understand: the trial uses my GPU and registers no model')
        self.trial_confirm.setObjectName('training_trial_confirm')
        review_layout.addWidget(self.trial_confirm)
        self.trial_toggle.toggled.connect(self._trial_controls_changed)
        self.trial_confirm.toggled.connect(self._update_actions)
        self.package_button = QPushButton('Review Selected Package…')
        self.package_button.clicked.connect(lambda: self.reviewPackageRequested.emit(deepcopy(self.contract.get('package', {}))))
        review_layout.addWidget(self.package_button)
        self.workspace = self._label('Workspace: ' + str(plan.destination) if isinstance(plan, TrainingProcessPlan) else 'Prepare a supported trainer and exact package in Setup.')
        review_layout.addWidget(self.workspace)
        review_layout.addWidget(self._label('This retained review preserves the original training scope. Check recovery before verifying output or reviewing a checkpoint for a new run.' if self._restored else
            'Start confirms this exact review. Training uses local compute with normal connectivity. Checkpoints and output remain in this run workspace if it stops.'))
        review_layout.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(review_page)
        self.tabs.addTab(scroll, 'Review')
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName('Exact training configuration, arguments, resources, and privacy')
        self.tabs.addTab(self.details, 'Advanced Details')
        recovery = QWidget(); recovery_layout = QVBoxLayout(recovery)
        self.result_message = self._label('Results and verified recovery choices appear here after the process stops.')
        recovery_layout.addWidget(self.result_message)
        self.failure_help = self._label(''); recovery_layout.addWidget(self.failure_help)
        self.checkpoint_count = self._label('Checkpoint choices appear after the stopped run is checked.'); recovery_layout.addWidget(self.checkpoint_count)
        self.checkpoint_picker = QComboBox()
        self.checkpoint_picker.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.checkpoint_picker.setMinimumContentsLength(28)
        self.checkpoint_picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.checkpoint_picker.setAccessibleName('Verified checkpoint for resume or evaluation export')
        recovery_layout.addWidget(self.checkpoint_picker)
        self.export_checkpoint_button = QPushButton('Export Selected Checkpoint for Evaluation…')
        self.export_checkpoint_button.clicked.connect(self._export_checkpoint)
        recovery_layout.addWidget(self.export_checkpoint_button)
        self.resume_button = QPushButton('Review Resume From Selected Checkpoint')
        self.resume_button.clicked.connect(self._resume); recovery_layout.addWidget(self.resume_button)
        recovery_layout.addWidget(self._label('Resume opens a new plan; Export creates a separate evaluation LoRA. Neither starts training automatically.'))
        actions = QHBoxLayout()
        self.recover_button = self._button('Check Recovery', self._recover, actions)
        self.verify_button = self._button('Retry Output Verification', self._retry_verification, actions)
        self.register_button = self._button('Review Registration', self._register, actions)
        recovery_layout.addLayout(actions)
        self.validation_button = QPushButton('Measure Held-Out Validation…')
        self.validation_button.clicked.connect(lambda: self.validationRequested.emit(self._outcome.verified)
            if self.validation_button.isEnabled() else None)
        recovery_layout.addWidget(self.validation_button)
        self.evaluation_button = QPushButton('Compare Result in Generate…')
        self.evaluation_button.clicked.connect(lambda: self.evaluationRequested.emit(self._outcome.resource_id)
            if self.evaluation_button.isEnabled() else None)
        recovery_layout.addWidget(self.evaluation_button)
        recovery_layout.addStretch(1)
        recovery_layout.addWidget(self._label('Successful verification and registration preserve the package, base resources, settings, and recovery lineage. Output quality still needs assessment.'))
        self.tabs.addTab(recovery, 'Result and Recovery')
        metrics_page = QWidget(); metrics_layout = QVBoxLayout(metrics_page)
        self.metrics_view = TrainingMetricsWidget(metrics_page)
        metrics_layout.addWidget(self.metrics_view)
        self.memory_view = self._label('GPU memory observation: Unknown. A timing trial alone does not prove memory fit.')
        self.memory_view.setObjectName('training_trial_memory_observation')
        metrics_layout.addWidget(self.memory_view)
        self.metrics_warning = self._label('')
        self.metrics_warning.setObjectName('training_metrics_warning')
        metrics_layout.addWidget(self.metrics_warning)
        self.tabs.addTab(metrics_page, 'Measurements')
        self.status = self._label(self._phase)
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide()
        self.cancel_button = QPushButton('Cancel Training'); self.cancel_button.clicked.connect(self._cancel); self.cancel_button.hide()
        layout.addWidget(self.status); layout.addWidget(self.progress); layout.addWidget(self.cancel_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.start_button = QPushButton('Start Local Training'); self.start_button.setProperty('primary', True)
        self.start_button.clicked.connect(self._start); buttons.addButton(self.start_button, QDialogButtonBox.ActionRole)
        self.generation_button = QPushButton('View Result Lineage')
        self.generation_button.clicked.connect(self._plan_generation)
        buttons.addButton(self.generation_button, QDialogButtonBox.ActionRole); self.generation_button.hide()
        layout.addWidget(buttons)
        bind_status_footer(self, self.status, self.progress, self.cancel_button, title='Local training')
        self._timer = QTimer(self); self._timer.setInterval(1000); self._timer.timeout.connect(self._timing)
        self._progressReceived.connect(self._on_progress)
        self._metricsReceived.connect(self._on_metrics)
        if self._ready:
            try:
                scope, resources, estimates, details = _review(self.contract)
                self.scope.setText(scope); self.resources.setText(resources); self.estimates.setText(estimates)
                self.details.setPlainText(details + '\n\nUnsupported choices require a new qualified plan. This dialog does not edit configuration or execute generation.')
                maximum = self.contract['result_binding']['expected_final_step']
                if type(maximum) is int and maximum >= 3:
                    self.trial_steps.setMaximum(min(10, maximum - 1))
                    self.trial_steps.setValue(min(10, maximum - 1))
                else:
                    self.trial_toggle.setEnabled(False)
                    self.trial_toggle.setToolTip('This plan has too few optimizer steps for a bounded trial.')
            except (KeyError, TypeError, ValueError): self._ready = False
        if not self._ready:
            self._state = 'held'; self._phase = 'This run has no complete prepared review. Return to Setup or the package preview.'
            self.state_label.setText(self._phase); self.status.setText(self._phase)
        if self._restored:
            self.trial_toggle.setEnabled(False)
        for button in self.findChildren(QPushButton): button.setAutoDefault(False)
        self._trial_controls_changed()
        self._update_actions()

    @staticmethod
    def _label(text, name=''):
        label = QLabel(text); label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
        if name: label.setObjectName(name)
        return label

    @staticmethod
    def _button(text, callback, layout):
        button = QPushButton(text); button.clicked.connect(callback); layout.addWidget(button)
        return button

    def _trial_controls_changed(self):
        selected = self.trial_toggle.isChecked()
        for widget in (self.trial_explanation, self.trial_steps, self.trial_seconds, self.trial_confirm):
            widget.setVisible(selected)
        if not selected:
            self.trial_confirm.setChecked(False)
        self.start_button.setText('Start Measured Trial' if selected else 'Start Local Training')
        self._update_actions()

    def _start(self):
        if not self._ready or self._attempted or self._task is not None: return
        selected_trial = None
        if self.trial_toggle.isChecked():
            if not self.trial_confirm.isChecked(): return
            selected_trial = TrainingTrialLimits(self.trial_steps.value(), self.trial_seconds.value())
            selected_trial.validate(self.contract['result_binding']['expected_final_step'])
        self._trial_limits = selected_trial
        self.memory_view.setText(
            'GPU memory observation: Pending. Per-process telemetry may be unavailable; fit remains unproven.'
            if selected_trial is not None else
            'GPU memory observation: Not collected for this ordinary run. No memory-fit conclusion is available.')
        self._attempted = True
        self._cancel_event.clear()
        self._process_invoked.clear()
        self._revision = self._step = 0; self._observed_running = False
        self._state, self._phase = ('queued', 'Queued for measured local training trial'
            if selected_trial is not None else 'Queued for reviewed local training')
        def work(job, token):
            fingerprint = self.plan.fingerprint
            raw = _bytes(self.contract)
            if (hashlib.sha256(raw).hexdigest() != self.plan.preview_fingerprint
                    or self.contract['resources']['registry_revision'] != self.expected_registry):
                raise ValueError('The prepared contract or resource registry review changed. Prepare a new run review.')
            if self._cancel_event.is_set() or job.cancel_requested:
                return _Delivery(fingerprint, TrainingExecutionOutcome('cancelled', self.plan.destination,
                    'Cancelled before training started.'), ())
            maximum = self.contract['result_binding']['expected_final_step']
            def progress(record):
                try:
                    if type(record) is dict and record.get('state') in {'starting', 'verifying'} and set(record) == {'state', 'optimizer_step', 'max_train_steps'}:
                        expected_step = 0 if record['state'] == 'starting' else maximum
                        if type(record['max_train_steps']) is not int or record['max_train_steps'] != maximum or type(record['optimizer_step']) is not int or record['optimizer_step'] != expected_step:
                            return
                    else:
                        validate_progress(record, fingerprint, self.plan.preview_fingerprint)
                        if record['max_train_steps'] != maximum: return
                    self._progressReceived.emit(token, deepcopy(record))
                except (TypeError, ValueError, KeyError): return
            def should_cancel():
                if job.cancel_requested: self._cancel_event.set()
                return self._cancel_event.is_set()
            options = dict(expected_registry=self.expected_registry,
                should_cancel=should_cancel, progress_callback=progress)
            if getattr(self.service, 'supports_metrics', False):
                options['metrics_callback'] = lambda snapshot, error='': self._metricsReceived.emit(token, snapshot, error, fingerprint)
                options['trial'] = selected_trial
            elif selected_trial is not None:
                raise ValueError('The selected training service cannot run a bounded measured trial.')
            if getattr(self.service, 'reports_process_invocation', False):
                options['invocation_observer'] = self._process_invoked.set
            else:
                self._process_invoked.set()  # Conservative boundary for older adapters.
            outcome = self.service.execute(self.plan, self.contract, fingerprint, self.jobs, self.registry, **options)
            if _bytes(self.contract) != raw:
                raise ValueError('The prepared contract changed during execution. Retain the workspace and review recovery.')
            return _validated_outcome(outcome, self.plan, self.contract, fingerprint, raw, selected_trial)
        self._launch('Measured training trial' if selected_trial is not None else 'Local training', work)

    def _launch(self, name, operation):
        if self._task is not None: return
        self._serial += 1; token = self._serial
        returned = {}
        def work(job):
            try:
                value = operation(job, token)
                returned['value'] = value
                return value
            except Exception as exc:
                returned['error'] = str(exc)[:2000]
                raise
        task = CoordinatedCallTask(self.jobs, name, work, resource='io')
        self._task = task; self._started = time.monotonic()
        self._timer.start(); self.progress.setRange(0, 0); self.progress.show(); self.cancel_button.show()
        task.signals.complete.connect(lambda value: self._finish(task, returned.get('value', value)))
        task.signals.failed.connect(lambda error: self._finish(task, None, returned.get('error', error)))
        task.signals.cancelled.connect(lambda: self._finish(task, returned.get('value'), returned.get('error', 'Background work stopped. Review retained process recovery.')))
        self._timing(); self._update_actions()
        try: task.start()
        except Exception as exc: self._finish(task, None, str(exc))

    def _finish(self, task, delivery, error=''):
        if self._task is not task: return
        self._task = None; self._timer.stop(); self.progress.hide(); self.cancel_button.hide()
        if not isinstance(delivery, _Delivery):
            needs_recovery = self._process_invoked.is_set() or self._observed_running
            self._state = 'stopping' if needs_recovery else 'recovery_required' if self._restored else 'failed'
            self._phase = ('Process status needs recovery.' if needs_recovery else
                'Retained training evidence needs recovery.' if self._restored else 'Training did not start.')
            self.result_message.setText(str(error)[:2000])
            self.failure_help.setText(FIXES['runtime_failed'] if needs_recovery or self._restored else
                'Review the trainer setup and run locations, then prepare a new training review.')
        else:
            self._outcome = delivery.outcome
            self._trial_limits = delivery.trial_limits
            self._state = self._outcome.status
            if self._state != 'stopping': self._process_invoked.clear()
            if self._state == 'held':
                self._attempted = self._observed_running = False
                self.start_button.setText('Retry Local Training')
            self._phase = self._outcome.message[:2000]
            if self._outcome.trial is not None:
                receipt = self._outcome.trial
                if receipt.seconds_per_step is not None:
                    self._phase += (f' Observed {receipt.measured_steps:,} optimizer steps after warmup '
                        f'at {receipt.seconds_per_step:.2f} seconds per step; '
                        f'rough remaining time {receipt.estimated_remaining_seconds:.0f} seconds. '
                        'This is a timing estimate, not a quality or memory measurement.')
                else:
                    self._phase += ' Too few post-warmup observations were retained for a timing estimate.'
                self.memory_view.setText(self._outcome.memory.display() if self._outcome.memory is not None
                    else 'GPU memory observation: Unknown for this trial. No memory-fit conclusion is available.')
            self.result_message.setText(self._phase)
            self.failure_help.setText(FIXES.get(self._outcome.failure_code, ''))
            self.checkpoint_picker.clear()
            for label, previous in delivery.checkpoints: self.checkpoint_picker.addItem(label, previous)
            self.checkpoint_count.setText(f'{len(delivery.checkpoints):,} verified checkpoints available · Showing all')
            if self._state == 'completed':
                self.heading.setText('LoRA Verified and Registered')
                self.resourceRegistered.emit(self._outcome.resource_id)
            elif self._state in TRIAL_STATES:
                self.heading.setText('Measured Trial Review')
            if self._outcome.metrics is not None:
                self.metrics_view.set_history(self._outcome.metrics)
            if self._outcome.metrics_error:
                self.metrics_warning.setText(self._outcome.metrics_error)
        self.state_label.setText(self._phase); self.status.setText(self._phase)
        self.tabs.setCurrentIndex(2)
        self._update_actions()
        if self._close_requested and self._state != 'stopping': super().reject()

    def _on_progress(self, token, record):
        if token != self._serial or self._task is None: return
        if 'revision' in record:
            if record['revision'] <= self._revision or record['optimizer_step'] < self._step: return
            self._revision = record['revision']; self._step = record['optimizer_step']
        # Parent 'starting' is a pending launch observation, not native
        # process evidence. Only a structured worker record proves execution.
        if 'revision' in record:
            self._observed_running = True
        maximum, step = record['max_train_steps'], record['optimizer_step']
        self.progress.setRange(0, 1000); self.progress.setValue(step * 1000 // maximum)
        if self._cancel_event.is_set():
            self._state, self._phase = 'cancelling', 'Cancelling · Waiting for the trainer to stop'
        elif record['state'] in {'completed', 'verifying'}:
            self._state = 'verifying'
            self._phase = 'Training reached its final step. Waiting for process shutdown and independent output verification.'
        elif record['state'] == 'starting':
            self._state, self._phase = 'starting', 'Starting the local trainer; waiting for observed progress'
        else:
            self._state = record['state']
            self._phase = f"{'Checkpoint retained' if self._state == 'checkpoint' else 'Training'} · Step {step:,} of {maximum:,} · Epoch {record['epoch']:,}"
            if record['loss'] is not None: self._phase += f" · Loss {record['loss']:.6g}"
        self.state_label.setText(self._phase); self._timing()

    def _on_metrics(self, token, snapshot, error, fingerprint):
        if token != self._serial or self._task is None:
            return
        if type(error) is str and error:
            self.metrics_warning.setText(error[:500])
        if (type(snapshot) is TrainingMetricsSnapshot and self.plan is not None
                and snapshot.execution_contract_sha256 == self.plan.preview_fingerprint
                and snapshot.process_fingerprint == fingerprint):
            self.metrics_view.set_history(snapshot)

    def _timing(self):
        text = self._phase
        if self._task is not None: text += f' · Elapsed {int(time.monotonic() - self._started):,}s'
        self.status.setText(text)

    def _cancel(self):
        if self._task is None and self._state != 'stopping': return
        self._cancel_event.set()
        self._state, self._phase = 'cancelling', 'Cancelling · Waiting for the trainer to stop'
        self.state_label.setText('Cancellation requested. Waiting for process death; retained checkpoints remain available.')
        self._timing(); self.cancel_button.setEnabled(False)

    def _recover(self):
        if self._task is not None or self._state not in {'stopping', 'cancelling', 'recovery_required', 'trial_recovery_required'}: return
        self._phase = 'Checking retained process recovery'
        def work(job, token):
            del job, token
            fingerprint, raw = self.plan.fingerprint, _bytes(self.contract)
            tip = self._outcome.process.journal_tip if self._outcome is not None and self._outcome.process is not None else self._retained_tip
            if not tip:
                try: tip = TrainingRunJournal(self.plan.destination).read()[1]
                except Exception as exc:
                    raise ValueError('The recovery journal is unavailable. Process death has not been confirmed; retain this workspace and check recovery again.') from exc
            if type(tip) is not str or SHA.fullmatch(tip) is None:
                raise ValueError('The recovery journal has no valid process receipt. Process death has not been confirmed.')
            result, anchor, checkpoints = self.service.recover(self.plan, self.contract, tip)
            trial_limits = load_trial_intent(self.plan, self.contract)
            metrics = None
            metrics_error = ''
            if getattr(self.service, 'supports_metrics', False):
                try: metrics = self.service.metrics(self.plan, self.contract)
                except (OSError, ValueError, RuntimeError):
                    metrics_error = 'Retained training measurements are unavailable or changed.'
            if trial_limits is not None and result.tree_stopped and result.status != 'recovery_required':
                receipt = load_trial_receipt(self.plan, self.contract)
                if receipt is not None and receipt_matches_process(receipt, result):
                    try: memory = load_observation(self.plan, self.contract)
                    except (OSError, ValueError, RuntimeError): memory = None
                    outcome = TrainingExecutionOutcome(receipt.status, self.plan.destination,
                        'The stopped measured trial and its retained receipt were recovered. No model was registered.',
                        result, checkpoint_anchor_sha256=anchor, checkpoints=checkpoints,
                        failure_code=receipt.failure_code, trial=receipt,
                        metrics=metrics, metrics_error=metrics_error, memory=memory)
                elif getattr(self.service, 'supports_metrics', False):
                    outcome = self.service.finish(self.plan, self.contract, result, self.registry,
                        expected_registry=self.expected_registry, trial=trial_limits, metrics=metrics)
                    outcome = replace(outcome, metrics=metrics, metrics_error=metrics_error)
                else:
                    raise ValueError('The retained measured trial needs its original training service for recovery.')
                return _validated_outcome(outcome, self.plan, self.contract, fingerprint, raw, trial_limits)
            status = 'stopping' if not result.tree_stopped else (result.status if result.status in {'cancelled', 'failed', 'interrupted', 'recovery_required'} else 'verification_required')
            outcome = TrainingExecutionOutcome(status, self.plan.destination,
                (('The trainer has stopped. Review the retained result or choose a checkpoint.' if result.tree_stopped and result.status != 'recovery_required'
                  else 'Process recovery still needs attention. Retain this workspace and check recovery again.')
                 + ' ' + result.message),
                result, checkpoint_anchor_sha256=anchor, checkpoints=checkpoints,
                metrics=metrics, metrics_error=metrics_error)
            if trial_limits is not None:
                outcome = TrainingExecutionOutcome('trial_recovery_required' if result.tree_stopped else 'stopping',
                    self.plan.destination, 'The retained measured trial still needs process recovery. No model was registered.',
                    result, checkpoint_anchor_sha256=anchor, checkpoints=checkpoints,
                    metrics=metrics, metrics_error=metrics_error)
            return _validated_outcome(outcome, self.plan, self.contract, fingerprint, raw, trial_limits)
        self._launch('Check training recovery', work)

    def _resume(self):
        previous = self.checkpoint_picker.currentData()
        if self._trial_limits is None and self._task is None and self._state not in {'stopping', 'cancelling', 'recovery_required'} and type(previous) is dict:
            self.resumeRequested.emit(deepcopy(previous))

    def _retry_verification(self):
        if self._trial_limits is None and self._state == 'verification_required' and self._outcome is not None:
            self.retryVerificationRequested.emit(self._outcome)

    def _export_checkpoint(self):
        previous = self.checkpoint_picker.currentData()
        if self.export_checkpoint_button.isEnabled() and type(previous) is dict:
            self.checkpointExportRequested.emit(deepcopy(previous))

    def _register(self):
        if self._trial_limits is None and self._state == 'registration_required' and self._outcome is not None:
            self.registerRequested.emit(self._outcome.verified)

    def _plan_generation(self):
        if self._trial_limits is None and self._state == 'completed' and self._outcome is not None:
            self.generationPlanningRequested.emit(self._outcome.resource_id)

    def _update_actions(self):
        idle = self._task is None
        self.start_button.setEnabled(idle and self._ready and not self._attempted
            and (not self.trial_toggle.isChecked() or self.trial_confirm.isChecked()))
        self.start_button.setVisible(not self._restored and self._state not in
            {'completed', 'verification_required', 'registration_required', *TRIAL_STATES})
        self.package_button.setEnabled(self._ready)
        self.recover_button.setEnabled(idle and self._state in {'stopping', 'cancelling', 'recovery_required', 'trial_recovery_required'})
        self.verify_button.setEnabled(idle and self._trial_limits is None and self._state == 'verification_required')
        self.register_button.setEnabled(idle and self._trial_limits is None and self._state == 'registration_required')
        self.resume_button.setEnabled(idle and self._trial_limits is None and self._state not in {'stopping', 'cancelling', 'recovery_required'} and self.checkpoint_picker.count() > 0)
        stopped_checkpoint = (idle and self._outcome is not None
            and self._outcome.process.tree_stopped is True
            and self._state not in {'stopping', 'cancelling', 'recovery_required', 'trial_recovery_required'}
            and self.checkpoint_picker.count() > 0)
        self.checkpoint_picker.setEnabled(stopped_checkpoint)
        self.export_checkpoint_button.setEnabled(stopped_checkpoint)
        self.generation_button.setVisible(self._trial_limits is None and self._state == 'completed')
        self.generation_button.setEnabled(idle and self._trial_limits is None and self._state == 'completed')
        self.evaluation_button.setEnabled(self.generation_button.isEnabled())
        self.validation_button.setEnabled(idle and self._trial_limits is None
            and self._outcome is not None and self._outcome.verified is not None
            and self._outcome.process.tree_stopped is True)
        self.cancel_button.setEnabled(not self._cancel_event.is_set() and self._task is not None)

    def reject(self):
        if self._task is not None or self._state in {'stopping', 'cancelling'}:
            self._close_requested = True
            self._cancel()
            self._update_actions()
            return
        super().reject()

    def accept(self):
        self.reject()

    def closeEvent(self, event):
        if self._task is not None or self._state in {'stopping', 'cancelling'}:
            event.ignore(); self.reject()
        else: super().closeEvent(event)
