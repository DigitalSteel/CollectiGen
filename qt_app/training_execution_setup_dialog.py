"""Choose and prepare one exact local SDXL training run for review."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from threading import Event
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from core.training_execution_preparation import TrainingExecutionPreparation
from core.training_process import TrainingProcessPlan, _bytes
from .components import bind_status_footer, refresh_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


MAX_IMAGES = 50_000
RUN_NAME = re.compile(r'[^\\/:*?"<>|\x00-\x1f]+\Z')
RESERVED = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z', re.IGNORECASE)


@dataclass(frozen=True)
class _Delivery:
    token: int
    request: dict
    result: object = None
    error: str = ''


@dataclass(frozen=True)
class _SavedSetupDelivery:
    token: int
    generation: int
    snapshot: dict
    result: object = None
    error: str = ''


def _summary(package):
    """Bounded projection of an already verified, in-memory package manifest."""
    if not getattr(package, 'valid', False) or type(getattr(package, 'manifest', None)) is not dict:
        raise ValueError('Choose a verified training package before preparing training.')
    manifest = package.manifest
    candidate, counts = manifest.get('candidate_plan'), manifest.get('counts')
    if type(candidate) is not dict or type(counts) is not dict:
        raise ValueError('The verified package summary is incomplete.')
    goal = candidate.get('goal')
    goal = str(goal.get('text') or '').strip() if type(goal) is dict else ''
    values = {}
    for name in ('included_images', 'training', 'validation', 'holdout'):
        value = counts.get(name)
        if type(value) is not int or value < 0:
            raise ValueError('The verified package role counts are invalid.')
        values[name] = value
    if not 1 <= values['included_images'] <= MAX_IMAGES or sum(
            values[name] for name in ('training', 'validation', 'holdout')) != values['included_images']:
        raise ValueError('This package is outside the supported 1 to 50,000 image scope.')
    return goal[:2000] or 'Train a reviewed SDXL LoRA from this package.', values


def _path_text(value):
    text = str(value or '').strip()
    if not text or '\x00' in text or not Path(text).is_absolute():
        raise ValueError('Choose absolute locations for every software, tokenizer, and output field.')
    return str(Path(text))


def _request(values, checkpoint_interval):
    result = {name: _path_text(values[name]) for name in (
        'source_root', 'environment_root', 'python_base_root', 'python',
        'tokenizers_root', 'output_root')}
    name = str(values['run_name'] or '').strip()
    if (not name or len(name) > 80 or RUN_NAME.fullmatch(name) is None or name in {'.', '..'}
            or name.endswith(('.', ' ')) or RESERVED.fullmatch(name) is not None):
        raise ValueError('Use a safe run name of at most 80 characters without reserved filename characters.')
    if type(checkpoint_interval) is not int or not 0 <= checkpoint_interval <= 1_000_000_000:
        raise ValueError('Checkpoint interval must be automatic or a positive step count.')
    result['run_name'] = name
    result['checkpoint_interval'] = checkpoint_interval
    return result


def _validated(result, request):
    """Validate callback bindings without re-running preparation on the GUI thread."""
    if not isinstance(result, TrainingExecutionPreparation) or not isinstance(result.plan, TrainingProcessPlan):
        raise ValueError('Training preparation returned an unsupported result.')
    plan, contract = result.plan, result.contract
    if type(contract) is not dict or hashlib.sha256(_bytes(contract)).hexdigest() != plan.preview_fingerprint:
        raise ValueError('The prepared review does not match its execution plan.')
    workspace, process, resources = contract.get('workspace'), contract.get('process'), contract.get('resources')
    if type(workspace) is not dict or type(process) is not dict or type(resources) is not dict:
        raise ValueError('The prepared review is incomplete.')
    destination = Path(request['output_root']) / request['run_name']
    if (plan.destination != destination or workspace.get('root') != request['output_root']
            or workspace.get('name') != request['run_name'] or not plan.command
            or plan.command[0] != request['python'] or process.get('command') != list(plan.command)):
        raise ValueError('The prepared review belongs to different selected locations.')
    inventory_roots = {}
    for row in process.get('inventories', ()):
        if type(row) is dict and row.get('purpose') in {
                'source_inventory', 'environment_inventory', 'python_base_inventory'}:
            inventory_roots[row['purpose']] = row.get('root')
    expected = {'source_inventory': request['source_root'],
        'environment_inventory': request['environment_root'],
        'python_base_inventory': request['python_base_root']}
    tokenizers = resources.get('tokenizers')
    if inventory_roots != expected or type(tokenizers) is not list or len(tokenizers) != 2:
        raise ValueError('The prepared software or tokenizer selection changed.')
    tokenizer_paths = {row.get('role'): row.get('path') for row in tokenizers if type(row) is dict}
    root = Path(request['tokenizers_root'])
    if tokenizer_paths != {1: str(root / 'tokenizer'), 2: str(root / 'tokenizer_2')}:
        raise ValueError('The prepared tokenizers belong to a different selection.')
    if not Path(result.preparation_path).is_absolute() or type(result.receipt_hashes) is not dict:
        raise ValueError('The preparation receipt is incomplete.')
    return result


class TrainingExecutionSetupDialog(FeedbackEnabledDialog):
    """Preparation-only bridge from a verified package to explicit run review."""

    preparationReady = Signal(object)
    reviewTrainingRequested = Signal(object)
    trainerSetupRequested = Signal()
    manualPackageRequested = Signal()
    feedback_workflow = 'Local training preparation'

    def __init__(self, package, registry, recipe, jobs, *, service, saved_setup=None, parent=None):
        super().__init__(parent)
        self.package, self.registry, self.recipe, self.jobs, self.service = package, registry, recipe, jobs, service
        self.saved_setup = saved_setup
        self.preparation = None
        self._prepared_request = None
        self._task = None
        self._cancel_event = Event()
        self._closing = False
        self._serial = 0
        self._edit_generation = 0
        self._applying_saved_setup = False
        self._started = 0.0
        self._phase = 'Choose a saved local trainer setup or enter the locations you want to check.'
        try:
            goal, counts = _summary(package)
            self._ready = True
            held = ''
        except ValueError as exc:
            goal = 'Prepare local SDXL training from a verified package.'
            counts = {'included_images': 0, 'training': 0, 'validation': 0, 'holdout': 0}
            self._ready, held = False, str(exc)

        self.setWindowTitle('Prepare Local Training')
        self.resize(940, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label('Prepare Local Training', 'sectionTitle'))
        self.goal_label = self._label(goal); layout.addWidget(self.goal_label)
        self.package_summary = self._label(
            f"{counts['included_images']:,} selected images · {counts['training']:,} training · "
            f"{counts['validation']:,} validation · {counts['holdout']:,} holdout")
        layout.addWidget(self.package_summary)
        layout.addWidget(self._label(
            'Check the selected local trainer software, original SDXL tokenizers, GPU, and package. '
            'This runs local native checks with normal connectivity, uploads nothing, and does not start training or download software.'))
        self.held = self._label(held); layout.addWidget(self.held)

        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        setup = QWidget(); setup_layout = QVBoxLayout(setup)
        saved_group = QGroupBox('Saved local trainer')
        saved_layout = QVBoxLayout(saved_group)
        self.saved_setup_status = self._label(
            'Check for a saved local trainer setup, or open Trainer Setup to choose one.'
            if callable(saved_setup) else
            'No saved training setup is connected. You can enter locations manually.')
        saved_layout.addWidget(self.saved_setup_status)
        self.saved_setup_button = QPushButton('Use Saved Training Setup')
        self.saved_setup_button.clicked.connect(self._load_saved_setup)
        saved_layout.addWidget(self.saved_setup_button)
        self.open_trainer_setup_button = QPushButton('Open Trainer Setup…')
        self.open_trainer_setup_button.clicked.connect(self.trainerSetupRequested)
        self.open_trainer_setup_button.setVisible(callable(saved_setup))
        saved_layout.addWidget(self.open_trainer_setup_button)
        setup_layout.addWidget(saved_group)
        setup_layout.addWidget(self._label(
            'Review the SDXL tokenizer folder, where to save training runs, and a name for this run.'))
        form = QFormLayout()
        self._choose_buttons = []
        self.tokenizers_root = self._location(form, 'SDXL tokenizer folder', directory=True)
        self.output_root = self._location(form, 'Save training runs in', directory=True)
        self.run_name = QLineEdit(); self.run_name.setPlaceholderText('e.g. portrait-lora-01')
        self.run_name.setAccessibleName('Safe training run name'); form.addRow('Run name', self.run_name)
        setup_layout.addLayout(form); setup_layout.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame); scroll.setWidget(setup)
        self.tabs.addTab(scroll, 'Setup')

        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        advanced_layout.addWidget(self._label(
            'These exact software locations are normally filled from the saved local trainer. Change them only when reviewing an existing installation.'))
        technical_group = QGroupBox('Technical software locations')
        technical_form = QFormLayout(technical_group)
        self.source_root = self._location(technical_form, 'Trainer source root', directory=True)
        self.environment_root = self._location(technical_form, 'Environment package root', directory=True)
        self.python_base_root = self._location(technical_form, 'Standalone Python base root', directory=True)
        self.python = self._location(technical_form, 'Python executable', directory=False)
        advanced_layout.addWidget(technical_group)
        advanced_layout.addWidget(self._label(
            'Automatic checkpointing chooses at most five full checkpoints. Experts may set an exact step interval from 1 to 1,000 saves; unsupported settings remain held by preparation.'))
        interval_row = QHBoxLayout(); interval_row.addWidget(self._label('Checkpoint interval'))
        self.checkpoint_interval = QSpinBox(); self.checkpoint_interval.setRange(0, 1_000_000_000)
        self.checkpoint_interval.setSpecialValueText('Automatic (up to 5 checkpoints)')
        self.checkpoint_interval.setAccessibleName('Checkpoint interval in optimizer steps; zero is automatic')
        interval_row.addWidget(self.checkpoint_interval, 1); advanced_layout.addLayout(interval_row)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName('Preparation authority and selected scope')
        self.details.setPlainText(
            'Preparation verifies exact selected software inventories, the Python base, device and native readiness evidence, '
            'the verified package, selected base and original tokenizers, and reserves one output workspace.\n\n'
            'Training, model acquisition, uploads, and generation remain separate actions.')
        advanced_layout.addWidget(self.details, 1); self.tabs.addTab(advanced, 'Advanced')

        self.status = self._label(self._phase)
        self.progress = QProgressBar(); self.progress.hide()
        self.cancel_button = QPushButton('Cancel Current Check'); self.cancel_button.clicked.connect(self._cancel); self.cancel_button.hide()
        layout.addWidget(self.status); layout.addWidget(self.progress); layout.addWidget(self.cancel_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close); self.buttons.rejected.connect(self.reject)
        self.prepare_button = QPushButton('Check and Prepare Training'); self.prepare_button.setProperty('primary', True)
        self.prepare_button.clicked.connect(self._prepare); self.buttons.addButton(self.prepare_button, QDialogButtonBox.ActionRole)
        self.review_button = QPushButton('Review Training Run'); self.review_button.clicked.connect(self._review)
        self.review_button.setProperty('primary', True)
        self.buttons.addButton(self.review_button, QDialogButtonBox.ActionRole); self.review_button.hide()
        self.recover_button = QPushButton('Check Preparation Recovery'); self.recover_button.clicked.connect(self._recover)
        self.buttons.addButton(self.recover_button, QDialogButtonBox.ActionRole); self.recover_button.hide()
        self.manual_button = QPushButton('Use Manual Training Package'); self.manual_button.clicked.connect(self._manual)
        self.buttons.addButton(self.manual_button, QDialogButtonBox.ActionRole)
        layout.addWidget(self.buttons)
        bind_status_footer(self, self.status, self.progress, self.cancel_button, title='Training preparation')
        self._timer = QTimer(self); self._timer.setInterval(1000); self._timer.timeout.connect(self._timing)
        for button in self.findChildren(QPushButton): button.setAutoDefault(False)
        for name in ('source_root', 'environment_root', 'python_base_root', 'python',
                     'tokenizers_root', 'output_root', 'run_name'):
            getattr(self, name).textChanged.connect(self._selection_changed)
        self.checkpoint_interval.valueChanged.connect(self._selection_changed)
        self._update_actions()

    @property
    def prepared_request(self):
        """A copy of the exact request bound to the current preparation."""
        return deepcopy(self._prepared_request)

    @staticmethod
    def _label(text, name=''):
        label = QLabel(text); label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
        if name: label.setObjectName(name)
        return label

    def _location(self, form, label, *, directory):
        edit = QLineEdit(); edit.setAccessibleName(label)
        button = QPushButton('Choose…')
        def choose():
            value = (QFileDialog.getExistingDirectory(self, label, edit.text()) if directory else
                     QFileDialog.getOpenFileName(self, label, edit.text(), 'Python executable (*.exe);;All files (*)')[0])
            if value: edit.setText(value)
        button.clicked.connect(choose)
        row = QHBoxLayout(); row.addWidget(edit, 1); row.addWidget(button)
        form.addRow(label, row)
        self._choose_buttons.append(button)
        return edit

    def _values(self):
        return {name: getattr(self, name).text() for name in (
            'source_root', 'environment_root', 'python_base_root', 'python',
            'tokenizers_root', 'output_root', 'run_name')}

    def _current_request(self):
        return _request(self._values(), self.checkpoint_interval.value())

    def _request_snapshot(self):
        values = self._values()
        values['checkpoint_interval'] = self.checkpoint_interval.value()
        return values

    def _selection_changed(self, *_):
        if self._applying_saved_setup:
            return
        self._edit_generation += 1
        had_preparation = self.preparation is not None or self._prepared_request is not None
        self.preparation = None
        self._prepared_request = None
        self.review_button.hide()
        if self.saved_setup_status.property('loaded'):
            self.saved_setup_status.setText(
                'Saved setup loaded; your current changes will be checked when you prepare training.')
        if had_preparation:
            self._phase = 'Selections changed. Check and prepare training again before review.'
            self.status.setText(self._phase)
        self._update_actions()

    def _load_saved_setup(self):
        if (self._task is not None or not callable(self.saved_setup)
                or bool(getattr(self.service, 'recovery_required', False))):
            return
        self._cancel_event.clear(); self._serial += 1; token = self._serial
        generation, snapshot = self._edit_generation, self._request_snapshot()
        self.saved_setup_status.setText('Loading the saved training setup…')
        self.saved_setup_status.setProperty('loaded', False)
        def operation(job):
            try:
                if self._cancel_event.is_set() or job.cancel_requested:
                    raise InterruptedError('cancelled')
                should_cancel, progress = self._callbacks(job)
                result = self.saved_setup(should_cancel=should_cancel, progress=progress)
                if self._cancel_event.is_set() or job.cancel_requested:
                    raise InterruptedError('cancelled')
                return _SavedSetupDelivery(token, generation, snapshot, result)
            except Exception as exc:
                return _SavedSetupDelivery(token, generation, snapshot, error=str(exc)[:2000])
        self._launch('Load saved training setup', operation)

    @staticmethod
    def _validate_saved_setup(result):
        fields = {'source_root', 'environment_root', 'python_base_root', 'python',
                  'tokenizers_root', 'output_root', 'run_name', 'checkpoint_interval'}
        if type(result) is not dict or set(result) != fields:
            raise ValueError('The saved training setup is incomplete or unsupported.')
        validated = _request(result, result['checkpoint_interval'])
        if validated != result:
            raise ValueError('The saved training setup contains noncanonical locations or values.')
        return validated

    def _finish_saved_setup(self, delivery, outer_error=''):
        if not isinstance(delivery, _SavedSetupDelivery):
            message = str(outer_error or 'The saved training setup could not be loaded.')[:2000]
            self.saved_setup_status.setText(message)
            self._phase = message
            return
        if self._cancel_event.is_set():
            message = 'Saved training setup loading was cancelled. Current choices were kept.'
            self.saved_setup_status.setText(message); self._phase = message
            return
        if delivery.error:
            self.saved_setup_status.setText(f'Saved training setup unavailable: {delivery.error}')
            self._phase = 'The saved training setup could not be loaded. Open Trainer Setup to choose one, or use Advanced for manual locations. Current choices were kept.'
            return
        if (delivery.token != self._serial or delivery.generation != self._edit_generation
                or delivery.snapshot != self._request_snapshot()):
            message = 'Selections changed while the saved setup loaded. Current choices were kept.'
            self.saved_setup_status.setText(message); self._phase = message
            return
        try:
            request = self._validate_saved_setup(delivery.result)
        except (KeyError, TypeError, ValueError) as exc:
            self.saved_setup_status.setText(f'Saved training setup unavailable: {exc}')
            self._phase = 'The saved training setup could not be loaded. Current choices were kept.'
            return
        pending_output = self.output_root.text()
        pending_run_name = self.run_name.text()
        self._applying_saved_setup = True
        try:
            for name in ('source_root', 'environment_root', 'python_base_root', 'python',
                         'tokenizers_root', 'output_root', 'run_name'):
                current = pending_output if name == 'output_root' else (
                    pending_run_name if name == 'run_name' else ''
                )
                getattr(self, name).setText(current or request[name])
            self.checkpoint_interval.setValue(request['checkpoint_interval'])
        finally:
            self._applying_saved_setup = False
        self._edit_generation += 1
        self.preparation = None; self._prepared_request = None; self.review_button.hide()
        self.saved_setup_status.setProperty('loaded', True)
        self.saved_setup_status.setText(
            'Saved local trainer loaded. Your entered run name and output folder were kept where set. '
            'Exact software locations are under Advanced; review the run before preparing.')
        self._phase = 'Saved training setup loaded. Check the selections, then prepare training.'

    def trainer_setup_saved(self):
        """Offer an explicit reload after Setup saved, without changing this run."""
        self.saved_setup_status.setProperty('loaded', False)
        self.saved_setup_status.setText(
            'Trainer Setup was saved. Choose Use Saved Training Setup to load it into this run; '
            'your entered run choices are still here.'
        )
        self._phase = 'Trainer Setup saved. Load it into this run when ready.'
        self.status.setText('Setup saved')
        refresh_status_footer(self)
        self._update_actions()

    def trainer_setup_cancelled(self):
        """Keep this run editable after leaving trainer resources unsaved."""
        self.saved_setup_status.setProperty('loaded', False)
        self.saved_setup_status.setText(
            'Trainer Setup was not saved. Your run choices are still here. '
            'Open Trainer Setup to try again, or use Advanced for manual locations.'
        )
        self._phase = 'Trainer Setup not saved. Current run choices kept.'
        self.status.setText('Setup not saved')
        refresh_status_footer(self)
        self._update_actions()

    def _callbacks(self, job):
        def should_cancel():
            return self._cancel_event.is_set() or job.cancel_requested
        def progress(done, total, label):
            if type(done) is not int or type(total) is not int: return
            message = str(label)[:400]
            if total > 0:
                job.update(max(0, min(1000, done * 1000 // total)), 1000, message)
            else:
                job.update(0, 0, message)
        return should_cancel, progress

    def _prepare(self):
        if self._task is not None or not self._ready or bool(getattr(self.service, 'recovery_required', False)):
            return
        try: request = self._current_request()
        except ValueError as exc:
            self.status.setText(str(exc)); return
        self.preparation = None; self._prepared_request = None
        self.review_button.hide(); self._cancel_event.clear()
        self._serial += 1; token = self._serial
        def operation(job):
            should_cancel, progress = self._callbacks(job)
            try:
                result = self.service.prepare(self.package, self.registry, self.recipe, deepcopy(request),
                    should_cancel=should_cancel, progress=progress)
                return _Delivery(token, request, result)
            except Exception as exc:
                return _Delivery(token, request, error=str(exc)[:2000])
        self._launch('Check and prepare local training', operation)

    def _recover(self):
        if self._task is not None or not bool(getattr(self.service, 'recovery_required', False)): return
        self._cancel_event.clear(); self._serial += 1; token = self._serial
        def operation(job):
            should_cancel, progress = self._callbacks(job)
            try:
                stopped = self.service.recover_preparation(should_cancel=should_cancel, progress=progress)
                return _Delivery(token, {}, stopped)
            except Exception as exc:
                return _Delivery(token, {}, error=str(exc)[:2000])
        self._launch('Check training preparation recovery', operation)

    def _launch(self, name, operation):
        returned = {}
        def work(job):
            value = operation(job); returned['value'] = value; return value
        task = CoordinatedCallTask(self.jobs, name, work, resource='io')
        self._task = task; self._started = time.monotonic(); self._phase = name
        self.progress.setRange(0, 0); self.progress.show(); self.cancel_button.show(); self._timer.start()
        task.signals.progress.connect(self._on_progress)
        task.signals.complete.connect(lambda value: self._finish(task, returned.get('value', value)))
        task.signals.cancelled.connect(lambda: self._finish(task, returned.get('value'), 'Preparation cancelled.'))
        task.signals.failed.connect(lambda error: self._finish(task, returned.get('value'), error))
        self._timing(); self._update_actions()
        try: task.start()
        except Exception as exc: self._finish(task, None, str(exc))

    def _finish(self, task, delivery, outer_error=''):
        if self._task is not task: return
        self._task = None; self._timer.stop(); self.progress.hide(); self.cancel_button.hide()
        if isinstance(delivery, _SavedSetupDelivery) or task.name == 'Load saved training setup':
            self._finish_saved_setup(delivery, outer_error)
            self.status.setText(self._phase); self._cancel_event.clear(); self._update_actions()
            if self._closing and not bool(getattr(self.service, 'recovery_required', False)):
                super().reject()
            return
        recovery = bool(getattr(self.service, 'recovery_required', False))
        if recovery:
            self._phase = 'Preparation process status needs recovery. Check recovery before closing or preparing again.'
            if isinstance(delivery, _Delivery) and delivery.error: self._phase += ' ' + delivery.error
        elif not isinstance(delivery, _Delivery):
            self._phase = str(outer_error or 'Training preparation failed.')[:2000]
        elif delivery.error:
            self._phase = ('Preparation cancelled.' if self._cancel_event.is_set() else delivery.error)[:2000]
        elif delivery.request == {}:  # recovery result
            self._phase = ('Preparation recovery is complete. You may check and prepare training again.'
                           if delivery.result is True else
                           'Process death was not confirmed. Check preparation recovery again.')
        else:
            try:
                current = self._current_request()
                if delivery.token != self._serial or current != delivery.request:
                    raise ValueError('Selections changed while preparation ran. Check and prepare the current choices again.')
                self.preparation = _validated(delivery.result, delivery.request)
                self._prepared_request = deepcopy(delivery.request)
                self._phase = 'Ready to review.'
                self.preparationReady.emit(self.preparation)
            except (KeyError, TypeError, ValueError) as exc:
                self.preparation = None; self._prepared_request = None; self._phase = str(exc)[:2000]
        self.status.setText(self._phase); self._cancel_event.clear(); self._update_actions()
        if self._closing and not bool(getattr(self.service, 'recovery_required', False)): super().reject()

    def _on_progress(self, current, total, message):
        if self._task is None: return
        if total > 0: self.progress.setRange(0, total); self.progress.setValue(max(0, min(total, current)))
        else: self.progress.setRange(0, 0)
        self._phase = str(message or self._phase)[:400]; self._timing()

    def _timing(self):
        text = self._phase
        if self._task is not None: text += f' · Elapsed {int(time.monotonic() - self._started):,}s'
        self.status.setText(text)

    def _cancel(self):
        if self._task is None: return
        self._cancel_event.set()
        if self._task.job_id: self.jobs.cancel(self._task.job_id)
        self._phase = 'Cancelling · Waiting for the preparation process to stop'
        self.cancel_button.setEnabled(False); self._timing()

    def _review(self):
        if self._task is None and self.preparation is not None and not bool(getattr(self.service, 'recovery_required', False)):
            try: current = self._current_request()
            except ValueError as exc:
                self._selection_changed(); self.status.setText(str(exc)); return
            if self._prepared_request is None or current != self._prepared_request:
                self._selection_changed(); return
            self.reviewTrainingRequested.emit(self.preparation); super().accept()

    def _manual(self):
        if self._task is None and not bool(getattr(self.service, 'recovery_required', False)):
            self.manualPackageRequested.emit(); super().reject()

    def _update_actions(self):
        busy, recovery = self._task is not None, bool(getattr(self.service, 'recovery_required', False))
        try:
            self._current_request()
            complete_selection = True
        except ValueError:
            complete_selection = False
        self.saved_setup_button.setEnabled(callable(self.saved_setup) and not busy and not recovery)
        self.open_trainer_setup_button.setEnabled(callable(self.saved_setup) and not busy and not recovery)
        self.prepare_button.setVisible(self.preparation is None and not recovery)
        self.prepare_button.setEnabled(self._ready and complete_selection and not busy and not recovery)
        self.prepare_button.setToolTip(
            'Load a saved trainer setup or enter all required locations and a safe run name first.'
            if not complete_selection else 'Check the exact selected resources before reviewing a training run.')
        self.manual_button.setEnabled(not busy and not recovery)
        self.review_button.setVisible(self.preparation is not None and not recovery)
        self.review_button.setEnabled(self.preparation is not None and not busy and not recovery)
        self.recover_button.setVisible(recovery)
        self.recover_button.setEnabled(recovery and not busy)
        self.cancel_button.setEnabled(busy and not self._cancel_event.is_set())
        for name in ('source_root', 'environment_root', 'python_base_root', 'python',
                     'tokenizers_root', 'output_root', 'run_name'):
            getattr(self, name).setEnabled(not busy and not recovery)
        for button in self._choose_buttons: button.setEnabled(not busy and not recovery)
        self.checkpoint_interval.setEnabled(not busy and not recovery)

    def reject(self):
        if self._task is not None:
            self._closing = True; self._cancel(); return
        if bool(getattr(self.service, 'recovery_required', False)):
            self.status.setText('Confirm preparation process death before closing this review.'); return
        super().reject()

    def accept(self):
        self._review()

    def closeEvent(self, event):
        if self._task is not None or bool(getattr(self.service, 'recovery_required', False)):
            event.ignore(); self.reject()
        else: super().closeEvent(event)
