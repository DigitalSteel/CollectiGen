"""Own nonmodal package-to-training reviews and their shutdown boundary."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from core.training_process import _bytes
from core.training_workflow import FIELDS, TrainingWorkflowService
from .modal_feedback import FeedbackEnabledDialog
from .training_execution_setup_dialog import TrainingExecutionSetupDialog
from .training_run_dialog import TrainingRunDialog, _validated_outcome
from .workers import CoordinatedCallTask


class _ResumeService:
    """Keep the selected checkpoint and original native receipt immutable."""
    def __init__(self, service, previous, contract):
        self.service = service
        self.previous = deepcopy(previous)
        self.resolved = deepcopy(contract['continuation']['contract'])
        rows = [row for row in contract['process']['inputs']
                if row['purpose'] == 'native_readiness_receipt' and row['slot'] == '0']
        if len(rows) != 1:
            raise ValueError('The original native readiness receipt is unavailable. Retain this run for recovery.')
        self.native = {key: rows[0][key] for key in ('path', 'sha256')}

    @property
    def recovery_required(self): return self.service.recovery_required

    def prepare(self, *args, **kwargs):
        return self.service.prepare(*args, previous=deepcopy(self.previous),
            resume_contract=deepcopy(self.resolved), native_receipt=deepcopy(self.native), **kwargs)

    def recover_preparation(self, **kwargs): return self.service.recover_preparation(**kwargs)


class _RegistrationReview(FeedbackEnabledDialog):
    """Read the current registry off-thread, then confirm that exact revision."""
    confirmed = Signal(str)

    def __init__(self, registry, resource_id, jobs, parent):
        super().__init__(parent)
        self._task = None
        self.revision = ''
        self._closing = False
        self.jobs = jobs
        self.setWindowTitle('Review LoRA Registration')
        self.resize(620, 330)
        layout = QVBoxLayout(self)
        self.message = QLabel('Reading the current model registry…')
        self.message.setWordWrap(True); self.message.setTextFormat(Qt.PlainText)
        layout.addWidget(self.message)
        resource = QLabel('Verified output: ' + resource_id)
        resource.setWordWrap(True); resource.setTextFormat(Qt.PlainText)
        resource.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(resource)
        layout.addWidget(QLabel('Registration rechecks the output and preserves its package, base and run lineage.'))
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        self.register = QPushButton('Register Verified LoRA')
        self.register.setEnabled(False)
        self.register.clicked.connect(self._confirm)
        buttons.addButton(self.register, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)
        self.registry = registry
        QTimer.singleShot(0, self._read)

    def _read(self):
        if self._closing: return
        def work(job):
            if job.cancel_requested: raise InterruptedError('Registration review cancelled.')
            value, revision = self.registry.snapshot()
            return len(value['resources']), revision
        task = CoordinatedCallTask(self.jobs, 'Review model registry for registration', work, resource='io')
        self._task = task
        task.signals.complete.connect(lambda value: self._finish(task, value))
        task.signals.failed.connect(lambda error: self._finish(task, None, error))
        task.signals.cancelled.connect(lambda: self._finish(task, None, 'Registration review cancelled.'))
        try: task.start()
        except Exception as exc: self._finish(task, None, str(exc))

    def _finish(self, task, result, error=''):
        if self._task is not task: return
        self._task = None
        if self._closing: super().reject(); return
        if result is None: self.message.setText(str(error)[:2000]); return
        count, self.revision = result
        self.message.setText(f'{count:,} registered resources. Confirm adding this independently verified LoRA to the current registry.')
        self.register.setEnabled(True)

    def _confirm(self):
        if self._task is None and self.revision:
            self.confirmed.emit(self.revision); super().accept()

    def reject(self):
        self._closing = True
        if self._task is not None:
            self.jobs.cancel(self._task.job_id); return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None: event.ignore(); self.reject()
        else: super().closeEvent(event)


class TrainingWorkflowController(QObject):
    backgroundWorkFinished = Signal()
    workspaceChanged = Signal()
    editorCloseReady = Signal()
    packageReviewRequested = Signal(object)
    resourceRegistered = Signal(str)

    def __init__(self, jobs, parent, *, workflow_factory=None, run_service=None,
                 run_index=None, saved_setup=None, preparation_root=None):
        super().__init__(parent)
        self.jobs = jobs
        from core.training_execution import TrainingExecutionService
        from core.training_execution_readiness import TrainingExecutionReadinessService
        self.run_index, self.saved_setup = run_index, saved_setup
        self.workflow_factory = workflow_factory or (lambda: TrainingWorkflowService(jobs,
            readiness=TrainingExecutionReadinessService(run_index=run_index),
            preparation_root=preparation_root))
        self.run_service = run_service if run_service is not None else (
            TrainingExecutionService(run_index=run_index) if run_index is not None else None)
        self.dialogs = set()
        self._registration_reviews = {}
        self._retained_updates = {}
        self._retained_task = None
        self._retained_error = ''
        self._pointer_task = None
        self._batch_task = None
        self._closing = False
        self._shutdown_notified = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(200)
        self._shutdown_timer.timeout.connect(self._check_shutdown)

    def _show(self, dialog):
        self.dialogs.add(dialog)
        dialog.setWindowModality(Qt.NonModal)
        if hasattr(dialog, 'editorCloseReady'):
            dialog.editorCloseReady.connect(self.editorCloseReady)
        dialog.finished.connect(lambda *_: self._finished(dialog))
        dialog.show()
        self.workspaceChanged.emit()
        return dialog

    def _finished(self, dialog):
        self.dialogs.discard(dialog)
        dialog.deleteLater()
        self.workspaceChanged.emit()
        if self._closing: self._check_shutdown()
        elif not self.has_active_background_work(): self.backgroundWorkFinished.emit()

    def open_reviews(self):
        """Return a stable snapshot of live reviews owned by this shell controller."""
        return tuple(sorted(self.dialogs, key=id))

    def show_review(self, dialog):
        """Focus an existing visible review; never reopen or create one."""
        if self._closing or dialog not in self.dialogs or not dialog.isVisible():
            return False
        dialog.raise_()
        dialog.activateWindow()
        return True

    def prepare(self, package, registry, recipe, *, request=None, previous=None, contract=None):
        if self._closing: return None
        family = package.manifest.get('training_configuration', {}).get('target', {}).get('model_family', '') if isinstance(getattr(package, 'manifest', None), dict) else ''
        if str(family).casefold() == 'anima':
            if previous is not None:
                raise ValueError('Anima checkpoint continuation is not qualified. Prepare a new run instead.')
            from .training_anima_dialog import AnimaTrainingDialog
            dialog = AnimaTrainingDialog(package, registry, self.jobs,
                saved_setup=self.saved_setup, parent=self.parent())
            dialog.trainerSetupRequested.connect(lambda: self._open_trainer_setup_for(dialog))
            dialog.resourceRegistered.connect(self.resourceRegistered)
            if hasattr(dialog, 'reviewRetained'):
                dialog.reviewRetained.connect(lambda payload: self._queue_retained('anima', payload, dialog))
            return self._show(dialog)
        if request is not None:
            if (type(request) is not dict or set(request) != FIELDS
                    or any(type(request[key]) is not str for key in FIELDS - {'checkpoint_interval'})
                    or type(request['checkpoint_interval']) is not int
                    or not 0 <= request['checkpoint_interval'] <= 1_000_000_000):
                raise ValueError('The selected trainer locations are incomplete. Prepare a new review.')
            request = deepcopy(request)
        service = self.workflow_factory()
        if previous is not None: service = _ResumeService(service, previous, contract)
        dialog = TrainingExecutionSetupDialog(package, registry, recipe, self.jobs,
            service=service, saved_setup=self.saved_setup if previous is None else None,
            parent=self.parent())
        if request is not None:
            for key, value in request.items():
                if key == 'checkpoint_interval': dialog.checkpoint_interval.setValue(value)
                else: getattr(dialog, key).setText(value)
        dialog.manualPackageRequested.connect(lambda: self.packageReviewRequested.emit(package))
        dialog.trainerSetupRequested.connect(lambda: self._open_trainer_setup_for(dialog))
        dialog.reviewTrainingRequested.connect(lambda prepared: self._run(
            prepared, package, registry, recipe, deepcopy(dialog.prepared_request)))
        return self._show(dialog)

    def _open_trainer_setup_for(self, dialog):
        """Keep the current preparation review while the canonical Setup opens."""
        shell = self.parent()
        setup_page = getattr(shell, 'setup_page', None)
        if setup_page is None and hasattr(shell, 'navigation'):
            # Main-window sections are lazy. A run started directly from
            # Training must be able to open Setup on its first visit.
            shell.navigation.setCurrentRow(6)
            setup_page = getattr(shell, 'setup_page', None)
        state = getattr(shell, 'state', None)
        if setup_page is None or state is None:
            dialog.status.setText('Open Trainer Setup from the main Setup section, then return to this review.')
            return
        from .training_setup_queue_controller import open_trainer_setup
        open_trainer_setup(state, setup_page, return_review=dialog)
        dialog.status.setText('Trainer Setup is opening. This preparation and its current choices remain open.')

    def history(self):
        if self._closing or self.run_index is None: return None
        from .training_history_dialog import TrainingHistoryDialog
        for dialog in self.dialogs:
            if isinstance(dialog, TrainingHistoryDialog):
                self.show_review(dialog); return dialog
        dialog = TrainingHistoryDialog(self.run_index, self.jobs, self.parent())
        dialog.openRunRequested.connect(self._open_retained)
        if hasattr(dialog, 'retryRetainedRequested'):
            dialog.retryRetainedRequested.connect(self.retry_history_index)
        if self._retained_error:
            dialog.set_retained_index_status(self._retained_error, retry=True)
        return self._show(dialog)

    def _history_index_notice(self, message, *, retry=False):
        from .training_history_dialog import TrainingHistoryDialog
        for dialog in tuple(self.dialogs):
            if isinstance(dialog, TrainingHistoryDialog):
                dialog.set_retained_index_status(message, retry=retry)

    def _queue_retained(self, kind, payload, source):
        if self.run_index is None or self._shutdown_notified:
            return
        try:
            key = (kind, payload['prepared'].plan.run_id)
        except (AttributeError, KeyError, TypeError):
            self._retained_error = 'A retained review could not be added to History. Reopen its saved review manually.'
            self._history_index_notice(self._retained_error, retry=False)
            return
        self._retained_updates[key] = (payload, source)
        if self._retained_error:
            if source in self.dialogs and hasattr(source, 'status'):
                source.status.setText(self._retained_error)
        else:
            self._start_retained_index()

    def _start_retained_index(self):
        if self._retained_task is not None or self._retained_error or not self._retained_updates:
            return
        from core.training_retained_history import upsert_retained_review
        key, (payload, source) = next(iter(self._retained_updates.items()))
        task = CoordinatedCallTask(self.jobs, 'Save retained training review in History',
            lambda _job: upsert_retained_review(self.run_index, key[0], payload), resource='io')
        self._retained_task = task
        def finish(error=''):
            if self._retained_task is not task: return
            self._retained_task = None
            if error:
                self._retained_error = ('History could not save a retained review: ' + str(error)[:1400]
                    + ' The saved review remains available. Retry saving it here or reopen the file directly.')
                self._history_index_notice(self._retained_error, retry=True)
                if source in self.dialogs and hasattr(source, 'status'):
                    source.status.setText(self._retained_error)
            else:
                if self._retained_updates.get(key, (None,))[0] is payload:
                    del self._retained_updates[key]
                self._history_index_notice('', retry=False)
                self._start_retained_index()
            self.workspaceChanged.emit()
            if self._closing: self._check_shutdown()
        task.signals.complete.connect(lambda _record: finish())
        task.signals.failed.connect(lambda error: finish(error))
        task.signals.cancelled.connect(lambda: finish('The index request was cancelled.'))
        try: task.start()
        except Exception as exc: finish(str(exc))

    def retry_history_index(self):
        if self._retained_task is not None or not self._retained_updates or self._closing:
            return
        self._retained_error = ''
        self._history_index_notice('Retrying saved review indexing…')
        self._start_retained_index()

    def experiments(self):
        if self._closing or self.run_index is None: return None
        from core.paths import TRAINING_PLAN_DRAFTS_PATH
        from .training_experiments_dialog import TrainingExperimentsDialog
        for dialog in self.dialogs:
            if isinstance(dialog, TrainingExperimentsDialog):
                self.show_review(dialog); return dialog
        dialog = TrainingExperimentsDialog(self.run_index, self.jobs,
            TRAINING_PLAN_DRAFTS_PATH, self.parent())
        dialog.openRunRequested.connect(self._open_retained)
        dialog.draftCreated.connect(self._open_cloned_draft)
        dialog.evaluationRequested.connect(lambda review: self._open_retained(review.record, dialog._revision))
        dialog.prepareRequested.connect(self._return_to_training_prepare)
        return self._show(dialog)

    def _return_to_training_prepare(self):
        if self._closing:
            return
        from .training_workspace import open_training_workspace
        if open_training_workspace(self.parent()):
            self.parent().training_page.stages.setCurrentIndex(0)

    def open_anima_run(self):
        if self._closing: return None
        from PySide6.QtWidgets import QFileDialog
        from core.model_resource_registry import ModelResourceRegistry
        from core.paths import MODEL_RESOURCES_PATH
        from .training_anima_dialog import AnimaTrainingDialog
        path, _ = QFileDialog.getOpenFileName(self.parent(), 'Open Retained Anima Run', '', 'Anima review (anima-review.json)')
        if not path: return None
        dialog = AnimaTrainingDialog.from_retained_review(path,
            ModelResourceRegistry(MODEL_RESOURCES_PATH), self.jobs, parent=self.parent())
        dialog.resourceRegistered.connect(self.resourceRegistered)
        if hasattr(dialog, 'reviewRetained'):
            dialog.reviewRetained.connect(lambda payload: self._queue_retained('anima', payload, dialog))
        return self._show(dialog)

    def open_validation(self):
        if self._closing: return None
        from PySide6.QtWidgets import QFileDialog
        from .training_validation_dialog import TrainingValidationDialog
        path, _ = QFileDialog.getOpenFileName(self.parent(), 'Open Retained Validation', '', 'Validation review (*.json)')
        if not path: return None
        dialog = TrainingValidationDialog.open_retained(path, jobs=self.jobs, parent=self.parent())
        if hasattr(dialog, 'reviewRetained'):
            dialog.reviewRetained.connect(lambda payload: self._queue_retained('validation', payload, dialog))
        return self._show(dialog)

    def _open_cloned_draft(self, draft):
        if self._closing: return
        from .training_workspace import open_training_workspace
        open_training_workspace(self.parent(), draft=draft)

    def evaluation(self, run_id='', required_resource=''):
        if self._closing: return None
        from core.paths import USER_DATA_DIR
        from core.generation_execution import GenerationExecutionService
        from .training_evaluation_dialog import TrainingEvaluationDialog
        from .generation_workflow import _host, open_generation_workspace
        for dialog in self.dialogs:
            if (isinstance(dialog, TrainingEvaluationDialog) and dialog.run_id == run_id
                    and getattr(dialog, 'required_resource', '') == required_resource):
                self.show_review(dialog); return dialog
        host = _host(self.parent())
        linked = {'generation': None, 'started': False}
        def template():
            current = (linked['generation'] if linked['started'] else
                       getattr(host, '_generation_dialog', None))
            if current is None or current._closed:
                raise ValueError('Set Up Template in Generate, then use Return to Training Comparison before choosing these settings.')
            if not current._seed_input_valid():
                raise ValueError('Finish the seed and numeric fields in Generate first.')
            if required_resource and required_resource not in {
                    row.id for row in current.plan.request.resources if row.role == 'lora'}:
                raise ValueError('Add the selected training result to this Generate template before saving its comparison.')
            return current.plan
        dialog = TrainingEvaluationDialog(self.jobs,
            GenerationExecutionService(USER_DATA_DIR / 'generation', self.jobs), template,
            settings=getattr(getattr(host, 'state', None), 'settings', None),
            run_id=run_id, parent=self.parent())
        dialog.required_resource = required_resource
        def detach_generation():
            current = linked['generation']
            if current is None:
                return
            linked['generation'] = None
            if getattr(current, '_training_comparison_return_owner', None) is dialog:
                current._training_comparison_return_owner = None
                current.training_comparison_return.hide()
            for signal, handler in ((current.training_comparison_return_requested, return_to_review),
                                    (current.workspace_closed, generation_closed)):
                try:
                    signal.disconnect(handler)
                except (RuntimeError, TypeError):
                    pass

        def return_to_review():
            if self._closing or dialog not in self.dialogs:
                detach_generation()
                return
            current = linked['generation']
            if current is None or current._closed:
                generation_closed()
                return
            navigation = getattr(host, 'navigation', None)
            if navigation is not None:
                navigation.setCurrentRow(8)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            if dialog.session is not None:
                dialog.status.setText(
                    'Returned from Generate. This saved comparison remains fixed. '
                    'Refresh verified results or choose a sample to review.'
                )
                dialog.refresh.setFocus()
            else:
                dialog.status.setText(
                    'Returned from Generate. Choose Use Reviewed Generate Settings to freeze '
                    'this request, or Set Up Template in Generate to keep editing.'
                )
                dialog.use_template.setFocus()

        def generation_closed():
            detach_generation()
            if not self._closing and dialog in self.dialogs:
                if dialog.session is not None:
                    dialog.status.setText(
                        'Generate closed. This saved comparison remains fixed. '
                        'Refresh verified results or choose a sample to review.'
                    )
                elif dialog._template_plan is None:
                    dialog.status.setText(
                        'Generate closed without choosing a template. Set Up Template in Generate '
                        'to open a new or saved draft, then return and choose Use Reviewed Generate Settings.'
                    )
                else:
                    dialog.status.setText(
                        'Generate closed. Your previously selected template remains fixed. '
                        'Save this comparison, or reopen Generate to review different settings.'
                    )
                dialog.show()
                dialog.raise_()

        def opened(current):
            if self._closing or dialog not in self.dialogs or current._closed:
                return
            owner = getattr(current, '_training_comparison_return_owner', None)
            if owner is not None and owner is not dialog:
                dialog.status.setText(
                    'Another Training comparison is waiting for this Generate workspace. '
                    'Return to it or close that review before setting up this one.'
                )
                return
            if linked['generation'] is current:
                return
            detach_generation()
            linked['generation'] = current
            current._training_comparison_return_owner = dialog
            current.training_comparison_return_requested.connect(return_to_review)
            current.workspace_closed.connect(generation_closed)
            current.training_comparison_return.show()
            current.training_comparison_return_button.setFocus()
            dialog.hide()
            dialog.status.setText(
                'Review the request in Generate, then choose Return to Training Comparison. '
                'No settings are adopted until you choose Use Reviewed Generate Settings.'
            )

        def open_template():
            linked['started'] = True
            open_generation_workspace(self.parent(), on_opened=opened)

        dialog.templateRequested.connect(open_template)
        dialog.finished.connect(lambda *_: detach_generation())
        dialog.generationRequested.connect(lambda plan: open_generation_workspace(
            self.parent(), force_new=True, initial_plan=plan))
        dialog.batchRequested.connect(lambda selection: self._open_evaluation_batch(dialog, selection))
        return self._show(dialog)

    def _open_evaluation_batch(self, source, selection):
        """Reopen one exact saved comparison group in a fresh Generate plan."""
        if self._closing or source not in self.dialogs or self._batch_task is not None:
            return
        from pathlib import Path
        from core.generation_experiments import load_experiment
        from core.generation_planning import GenerationPlan
        from core.paths import USER_DATA_DIR
        from .generation_workflow import open_generation_workspace
        try:
            group, path, digest = selection
            group_id = group['id']
            selected = Path(path).absolute()
            if selected.parent != (USER_DATA_DIR / 'generation' / 'experiments').absolute():
                raise ValueError('The saved comparison group is outside the generation workspace.')
        except (KeyError, TypeError, ValueError) as exc:
            source.status.setText('The selected comparison group is invalid: ' + str(exc)[:1000]); return
        def work(job):
            if job.cancel_requested: raise InterruptedError('Comparison group opening was cancelled.')
            value, current_digest = load_experiment(selected)
            if current_digest != digest or value['id'] != group_id:
                raise ValueError('The saved comparison group changed. Prepare it again from Evaluation.')
            return GenerationPlan.from_dict(value['candidates'][0]['plan'])
        task = CoordinatedCallTask(self.jobs, 'Open measured-generation comparison plan', work, resource='io')
        self._batch_task = task
        source.status.setText('Checking the exact saved comparison group before opening Generate…')
        def finish(plan=None, error=''):
            if self._batch_task is not task: return
            self._batch_task = None
            if self._closing or source not in self.dialogs:
                self._check_shutdown(); return
            if error:
                source.status.setText(str(error)[:2000]); return
            def opened(generation_dialog):
                if self._closing or source not in self.dialogs: return
                generation_dialog.open_experiment()
                experiment = getattr(generation_dialog, '_experiment_dialog', None)
                if experiment is None or not callable(getattr(experiment, 'open_exact_group', None)):
                    source.status.setText('Generate opened, but exact saved-group review is unavailable. Reopen the saved group from Generate.')
                    return
                if not experiment.open_exact_group(selected, digest, group_id):
                    source.status.setText('Generate is busy. Reopen the saved comparison group there after current work finishes.')
            open_generation_workspace(self.parent(), force_new=True,
                initial_plan=plan, on_opened=opened)
        task.signals.complete.connect(lambda plan: finish(plan))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(error='Comparison group opening was cancelled.'))
        try: task.start()
        except Exception as exc: finish(error=str(exc))

    def _open_retained(self, record, revision):
        if self._closing or self.run_index is None: return None
        if record.kind in {'anima', 'validation'}:
            return self._open_typed_retained(record, revision)
        from .training_restore_dialog import TrainingRestoreDialog
        for dialog in self.dialogs:
            if (isinstance(dialog, (TrainingRunDialog, TrainingRestoreDialog)) and
                    (getattr(getattr(dialog, 'plan', None), 'run_id', None) == record.run_id
                     or getattr(dialog, 'run_id', None) == record.run_id)):
                self.show_review(dialog); return dialog
        dialog = TrainingRestoreDialog(self.run_index, record.run_id, revision, self.jobs, self.parent())
        dialog.restored.connect(self._restored_run)
        return self._show(dialog)

    def _open_typed_retained(self, record, revision):
        from core.training_retained_history import check_retained_pointer
        from .training_anima_dialog import AnimaTrainingDialog
        from .training_validation_dialog import TrainingValidationDialog
        for dialog in self.dialogs:
            if (isinstance(dialog, (AnimaTrainingDialog, TrainingValidationDialog))
                    and getattr(dialog, '_history_pointer', None) == record.run_id):
                self.show_review(dialog); return dialog
        if self._pointer_task is not None:
            self._history_index_notice('A retained review is already opening. Wait for its exact checks.')
            return None
        task = CoordinatedCallTask(self.jobs, 'Check selected retained training pointer',
            lambda _job: check_retained_pointer(self.run_index, record, revision), resource='io')
        self._pointer_task = task
        def finish(selected=None, error=''):
            if self._pointer_task is not task: return
            self._pointer_task = None
            if self._closing:
                self._check_shutdown(); return
            if error:
                self._history_index_notice(str(error)[:2000]); return
            if selected.kind == 'validation':
                dialog = TrainingValidationDialog.open_retained(selected.review_path,
                    selected.review_sha256, jobs=self.jobs, parent=self.parent())
                if hasattr(dialog, 'reviewRetained'):
                    dialog.reviewRetained.connect(lambda payload: self._queue_retained('validation', payload, dialog))
            else:
                from core.model_resource_registry import ModelResourceRegistry
                from core.paths import MODEL_RESOURCES_PATH
                from .training_anima_dialog import anima_service_for_owner
                from core.training_run_index import _read_optional
                from hashlib import sha256
                from pathlib import Path
                class PinnedService:
                    def __init__(self, service): self.service = service
                    def __getattr__(self, key): return getattr(self.service, key)
                    def open_review(self, path):
                        raw = _read_optional(Path(path), limit=256 * 1024)
                        if raw is None or sha256(raw).hexdigest() != selected.review_sha256:
                            raise ValueError('The selected Anima review changed before reopening.')
                        result = self.service.open_review(path)
                        if _read_optional(Path(path), limit=256 * 1024) != raw:
                            raise ValueError('The selected Anima review changed while reopening.')
                        return result
                registry = ModelResourceRegistry(MODEL_RESOURCES_PATH)
                dialog = AnimaTrainingDialog.from_retained_review(selected.review_path,
                    registry, self.jobs, service=PinnedService(anima_service_for_owner(registry, self.parent())),
                    parent=self.parent())
                dialog.resourceRegistered.connect(self.resourceRegistered)
                if hasattr(dialog, 'reviewRetained'):
                    dialog.reviewRetained.connect(lambda payload: self._queue_retained('anima', payload, dialog))
            dialog._history_pointer = selected.run_id
            self._show(dialog)
        task.signals.complete.connect(lambda selected: finish(selected))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(error='Retained review opening was cancelled.'))
        try: task.start()
        except Exception as exc: finish(error=str(exc))
        return None

    def _restored_run(self, context):
        if self._closing: return None
        from types import SimpleNamespace
        return self._run(SimpleNamespace(plan=context.review.plan, contract=context.review.value),
            context.package, context.registry, context.recipe, context.request, retained=True,
            retained_tip=context.review.record.latest_journal_tip)

    def _run(self, prepared, package, registry, recipe, request, *, retained=False, retained_tip=''):
        if self._closing: return None
        dialog = TrainingRunDialog(prepared.plan, prepared.contract, self.jobs, registry,
            expected_registry=prepared.contract['resources']['registry_revision'],
            service=self.run_service, retained=retained, retained_tip=retained_tip, parent=self.parent())
        dialog.reviewPackageRequested.connect(lambda _: self.packageReviewRequested.emit(package))
        dialog.resourceRegistered.connect(self.resourceRegistered)
        dialog.resumeRequested.connect(lambda previous: self._resume(dialog, package, registry, recipe, request, previous))
        dialog.checkpointExportRequested.connect(lambda previous: self._export_checkpoint(dialog, previous))
        dialog.validationRequested.connect(lambda selected: self._validation(dialog, selected))
        dialog.evaluationRequested.connect(lambda resource: self._evaluate_resource(dialog, resource))
        dialog.retryVerificationRequested.connect(lambda _: self._finish_result(dialog, dialog.expected_registry))
        dialog.registerRequested.connect(lambda verified: self._review_registration(dialog, verified))
        dialog.generation_button.setText('View Result Lineage')
        dialog.generationPlanningRequested.connect(lambda _: self._lineage(dialog))
        return self._show(dialog)

    def _export_checkpoint(self, run, previous):
        if self._closing or run not in self.dialogs: return
        from .training_checkpoint_export_dialog import TrainingCheckpointExportDialog
        dialog = TrainingCheckpointExportDialog(run.plan, run.contract, previous,
            run.registry, self.jobs, self.parent())
        dialog.resourceRegistered.connect(self.resourceRegistered)
        dialog.generationRequested.connect(lambda resource: self._open_generation(dialog, resource))
        dialog.evaluationRequested.connect(lambda resource: self._evaluate_resource(dialog, resource))
        dialog.validationRequested.connect(lambda selected: self._validation(dialog, selected))
        return self._show(dialog)

    def _validation(self, source, selected):
        if self._closing or source not in self.dialogs: return None
        from core.paths import USER_DATA_DIR
        from .training_validation_dialog import TrainingValidationDialog
        dialog = TrainingValidationDialog(source.plan, source.contract, selected,
            self.jobs, evaluation_root=USER_DATA_DIR / 'training-validation', parent=self.parent())
        if hasattr(dialog, 'reviewRetained'):
            dialog.reviewRetained.connect(lambda payload: self._queue_retained('validation', payload, dialog))
        return self._show(dialog)

    def _evaluate_resource(self, source, resource_id):
        if self._closing or source not in self.dialogs or not resource_id: return
        from .generation_workflow import open_generation_workspace
        def opened(_dialog):
            if not self._closing and source in self.dialogs:
                self.evaluation(source.plan.run_id, resource_id)
        open_generation_workspace(self.parent(), resource_id=resource_id,
            selected_role='lora', force_new=True, on_opened=opened)

    def _resume(self, run, package, registry, recipe, request, previous):
        selected = deepcopy(request)
        # The user chooses a new destination name; never replace the old run.
        selected['run_name'] = ''
        try: self.prepare(package, registry, recipe, request=selected, previous=previous, contract=run.contract)
        except (KeyError, TypeError, ValueError) as exc: run.status.setText(str(exc)[:2000])

    def _finish_result(self, run, revision, *, expected_outcome=None):
        if (self._closing or run not in self.dialogs or run._task is not None
                or run._outcome is None or run._state not in {'verification_required', 'registration_required'}
                or (expected_outcome is not None and run._outcome is not expected_outcome)): return
        outcome = run._outcome
        run._cancel_event.clear()
        run._phase = 'Verifying retained output and registration'
        def work(job, _token):
            fingerprint, raw = run.plan.fingerprint, _bytes(run.contract)
            try:
                result = run.service.finish(run.plan, run.contract, outcome.process, run.registry,
                    expected_registry=revision,
                    should_cancel=lambda: run._cancel_event.is_set() or job.cancel_requested)
            except (OSError, ValueError, RuntimeError, InterruptedError) as exc:
                result = replace(outcome, message='Retained result review needs attention: ' + str(exc)[:1500])
            return _validated_outcome(result, run.plan, run.contract, fingerprint, raw)
        run._launch('Verify and register retained training output', work)

    def _review_registration(self, run, verified):
        if (self._closing or run not in self.dialogs or run._state != 'registration_required'
                or run._outcome is None or verified is not run._outcome.verified): return
        current = self._registration_reviews.get(run)
        if current is not None:
            self.show_review(current); return
        expected = run._outcome
        review = _RegistrationReview(run.registry, verified.receipt['output']['resource_id'], self.jobs, self.parent())
        self._registration_reviews[run] = review
        run.register_button.setEnabled(False)
        run.finished.connect(review.reject)
        def finished():
            if self._registration_reviews.get(run) is review:
                self._registration_reviews.pop(run)
            if run in self.dialogs: run._update_actions()
        review.finished.connect(finished)
        review.confirmed.connect(lambda revision: self._finish_result(run, revision, expected_outcome=expected))
        self._show(review)

    def _lineage(self, run):
        if self._closing or run._outcome is None or run._outcome.verified is None: return
        resource_id = run._outcome.resource_id
        if run._outcome.verified.receipt.get('output', {}).get('resource_id') != resource_id: return
        dialog = FeedbackEnabledDialog(self.parent())
        dialog.setWindowTitle('Registered LoRA and Lineage'); dialog.resize(780, 650)
        layout = QVBoxLayout(dialog)
        note = QLabel('This registered LoRA can be included in a generation plan. Execution depends on the selected adapter preflight. Training success does not establish image quality.')
        note.setWordWrap(True); layout.addWidget(note)
        text = QPlainTextEdit(); text.setReadOnly(True)
        text.setPlainText(json.dumps(run._outcome.verified.receipt, ensure_ascii=False, indent=2)[:100000])
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        generate = QPushButton('Open Generation Plan')
        generate.setProperty('primary', True)
        generate.clicked.connect(lambda: self._open_generation(dialog, resource_id))
        buttons.addButton(generate, QDialogButtonBox.ActionRole)
        self._show(dialog)

    def _open_generation(self, result_dialog, resource_id):
        if self._closing or result_dialog not in self.dialogs: return
        from .generation_workflow import open_generation_workspace
        def opened(_generation_dialog):
            if not self._closing and result_dialog in self.dialogs:
                result_dialog.accept()
        open_generation_workspace(self.parent(), resource_id=resource_id,
            selected_role="lora", on_opened=opened)

    def has_active_background_work(self):
        return (self._retained_task is not None or self._pointer_task is not None
            or self._batch_task is not None or any(getattr(dialog, '_task', None) is not None
            or getattr(dialog, '_state', '') in {'stopping', 'cancelling'}
            or bool(getattr(getattr(dialog, 'service', None), 'recovery_required', False))
            for dialog in self.dialogs))

    def confirm_editor_close(self):
        """Let dirty owned editors resolve Save/Discard/Cancel before shell shutdown."""
        for dialog in tuple(self.dialogs):
            dirty = getattr(dialog, 'has_unsaved_changes', None)
            if callable(dirty) and dirty():
                request = getattr(dialog, 'request_shell_close', None)
                if callable(request): request()
                else: dialog.reject()
                if dialog in self.dialogs and (dialog.isVisible() or getattr(dialog, '_task', None) is not None):
                    return False
        return True

    def request_background_shutdown(self):
        if not self.confirm_editor_close():
            return True
        self._closing = True
        active = self.has_active_background_work()
        for dialog in tuple(self.dialogs): dialog.reject()
        if active and not self._shutdown_notified: self._shutdown_timer.start()
        return active

    def _check_shutdown(self):
        if self._shutdown_notified or self.has_active_background_work(): return
        self._shutdown_notified = True
        self._shutdown_timer.stop()
        self.backgroundWorkFinished.emit()


def shared_training_workflow(state, parent):
    """One shell-owned controller, shared by canonical Setup and Dataset."""
    current = getattr(state, '_training_workflow_controller', None)
    if current is not None:
        return current
    from core.paths import USER_DATA_DIR
    from core.training_run_index import TrainingRunIndex
    from core.training_setup_resources import SavedTrainingSetupResolver
    current = TrainingWorkflowController(state.jobs, parent,
        run_index=TrainingRunIndex(USER_DATA_DIR / 'training-runs.json'),
        saved_setup=SavedTrainingSetupResolver(USER_DATA_DIR / 'training-setup.json'),
        preparation_root=USER_DATA_DIR / 'training-preparation')
    state._training_workflow_controller = current
    return current
