"""Trainer Setup presentation in the Manager profile; stock services own work."""
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QProgressDialog, QPushButton, QWidget)
from shiboken6 import isValid

from qt_app.training_setup_dialog import TrainingSetupDialog, TrainingSetupPreview, profile_overview
from qt_app.training_setup_queue_controller import ManagerTrainerSetupController, QueuedTrainerSetup
from qt_app.training_setup_resources_dialog import TrainingSetupResourcesDialog
from qt_app.workers import CoordinatedCallTask


class ManagerTrainerSetupDialog(TrainingSetupDialog):
    def __init__(self, owner, profile, jobs, *, managed, generations, held_reason='', overview=None, parent=None):
        controller = ManagerTrainerSetupController(owner, profile, managed_available=managed)
        super().__init__(profile, jobs, parent=parent, controller=controller,
            overview=overview or profile_overview(profile), generations=generations, held_reason=held_reason,
            goal_summary='Install or reuse local trainer software, then select it for SDXL preparation.',
            machine_fit_summary='Software health, activation and model/recipe readiness are separate reviews. Setup never starts training.')
        self.owner = owner
        self.saved_panel = QWidget()
        row = QHBoxLayout(self.saved_panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel('Saved setup operations'))
        self.saved_operations = QComboBox()
        self.saved_operations.setAccessibleName('Saved Trainer Setup operations')
        row.addWidget(self.saved_operations, 1)
        self.review_saved_button = QPushButton('Review Saved Operation')
        self.review_saved_button.clicked.connect(self.review_saved)
        row.addWidget(self.review_saved_button)
        self.cancel_saved_button = QPushButton('Cancel Saved Operation')
        self.cancel_saved_button.clicked.connect(self.cancel_saved)
        row.addWidget(self.cancel_saved_button)
        self.layout().insertWidget(3, self.saved_panel)
        self.refresh_saved_operations()
        self.saved_operations.currentIndexChanged.connect(self._update_actions)
        self.operationFinished.connect(lambda *_: self.refresh_saved_operations())
        if any(op.state in {'waiting', 'running'} for op in owner.queue.snapshot()):
            self.held.setText('An unfinished Trainer Setup operation is retained. Review it before starting another; resuming requires confirmation.')
            self.review_saved_button.setDefault(True)
        active = next((op for op in owner.queue.snapshot() if op.state == 'running'), None)
        self._reattach_identity = active.id if active is not None else None

    def _finish(self, task, completed, result):
        super()._finish(task, completed, result)
        identity = self._reattach_identity
        if (identity is not None and not self._closing and self._task is None
                and self._queued is None and self._profile_sha256):
            self._reattach_identity = None
            self.saved_operations.setCurrentIndex(self.saved_operations.findData(identity))
            self.review_saved()

    def _update_actions(self, *_):
        super()._update_actions()
        if not hasattr(self, 'saved_operations'):
            return
        idle = self._task is None and self._queued is None and not self._closing and bool(self._profile_sha256)
        identity = self.saved_operations.currentData()
        selected = next((row for row in self.owner.queue.snapshot() if row.id == identity), None)
        pending = selected is not None and selected.state in {'waiting', 'running'}
        self.saved_operations.setEnabled(idle)
        self.review_saved_button.setEnabled(idle and selected is not None)
        self.review_saved_button.setDefault(idle and pending)
        self.cancel_saved_button.setEnabled(idle and pending)

    def refresh_saved_operations(self):
        selected = self.saved_operations.currentData()
        self.saved_operations.clear()
        rows = sorted(self.owner.queue.snapshot(), key=lambda row: row.sequence, reverse=True)
        for row in rows[:1000]:
            self.saved_operations.addItem((row.label + ' - ' + row.state)[:220], row.id)
        if selected is not None:
            index = self.saved_operations.findData(selected)
            if index >= 0:
                self.saved_operations.setCurrentIndex(index)
        self.saved_panel.setVisible(bool(rows))
        self.review_saved_button.setEnabled(bool(rows))

    def cancel_saved(self):
        if self._task is not None:
            self.status.setText('Finish the current review before cancelling saved work.')
            return
        identity = self.saved_operations.currentData()
        if not identity:
            return
        if self._queued is not None:
            if identity != self._queued.operation_id:
                self.status.setText('The original displayed operation must settle first.')
                return
            self._cancel()
            return
        _accepted, message = self.owner.queue.cancel_operation(identity)
        self.status.setText(message)
        self.refresh_saved_operations()

    def review_saved(self):
        if (self._task is not None or self._queued is not None or self._closing
                or not self._profile_sha256):
            return
        identity = self.saved_operations.currentData()
        if not identity:
            return
        self._preview = self._candidate = None
        self._candidate_reference = None
        self.use_button.hide()
        self._review_active = True
        self._show_review()
        self._set_stage(1)
        self.preview_title.setText('Reviewing saved Trainer Setup')
        self.preview_summary.setText('Reading the original operation. Resuming unfinished software setup requires confirmation.')
        def read(job):
            return self.controller.review_retained(identity, **self._callbacks(job))
        def show(result):
            if isinstance(result, QueuedTrainerSetup):
                self._set_stage(3 if result.action in {'activate', 'rollback'} else 2, result.action)
                self._applied(result.action, result)
            else:
                self._set_stage(3 if result.action in {'activate', 'rollback'} else 1, result.action)
                self._prepared(result)
        self._run('Reviewing the retained Trainer Setup operation', read, show)


def open_manager_trainer_setup(window, state, settings_path, *, allowed):
    """Preserve the original modal return contract and asynchronous metadata read."""
    if (window._trainer_setup_dialog is not None
            or getattr(window, '_trainer_setup_loading_task', None) is not None):
        return False
    from core.manager_trainer_setup import ManagerTrainerSetup
    from core.model_selection import protected_model_roots
    from core.paths import PROGRAM_ROOT
    from core.settings import AppSettings
    from core.training_source_io import read_stable_training_source

    settings_path = Path(settings_path)
    def protected():
        if not allowed():
            raise InterruptedError('The original Manager Trainer Setup session ended.')
        if not settings_path.exists():
            return protected_model_roots(state.settings)
        raw, _ = read_stable_training_source(settings_path, max_bytes=16 * 1024 * 1024)
        return protected_model_roots(AppSettings._from_bytes(raw))

    loader = QProgressDialog('Opening Trainer Setup and retained software generations...', 'Cancel', 0, 0, window)
    loader.setWindowTitle('Trainer Setup')
    loader.setWindowModality(Qt.WindowModal)
    loader.setMinimumDuration(0)
    loader.setAutoClose(False); loader.setAutoReset(False)
    result = []
    stopped = [False]
    window._trainer_setup_close_requested = False
    window._trainer_setup_dialog = loader

    def load(job):
        owner = getattr(window, '_manager_trainer_setup', None)
        if owner is None or (owner.closing and not owner.close_held):
            tools = PROGRAM_ROOT / 'tools'
            owner = ManagerTrainerSetup(state.jobs, state.generation_data_root / 'training/setup', state.settings,
                profile_path=tools / 'trainer_runtime_profile.json',
                catalog_path=tools / 'trainer_runtime_catalog.json',
                source_root=tools / 'trainer-sources/sd-scripts',
                session_allowed=allowed, protected_roots=protected)
        if owner.closing:
            owner.resume_review()
        description = owner.describe(should_cancel=lambda: stopped[0] or job.cancel_requested)
        return owner, description, profile_overview(description[0])

    task = CoordinatedCallTask(state.jobs, 'Opening Trainer Setup', load, resource='io')
    window._trainer_setup_loading_task = task
    def settle(value=None, error=''):
        if window._trainer_setup_loading_task is not task:
            return
        window._trainer_setup_loading_task = None
        if value is not None and getattr(window, 'state', state) is state:
            window._manager_trainer_setup = value[0]
        if value is not None and not stopped[0] and allowed():
            result.append(value)
        if error and not stopped[0]:
            window.statusBar().showMessage('Trainer Setup unavailable: ' + str(error)[:1000])
        if isValid(loader):
            loader.accept()
        if window._trainer_setup_close_requested:
            QTimer.singleShot(0, window.close)
    def cancel():
        stopped[0] = True
        if task.job_id:
            state.jobs.cancel(task.job_id)
    loader.canceled.connect(cancel)
    task.signals.complete.connect(lambda value: settle(value))
    task.signals.failed.connect(lambda error: settle(error=error))
    task.signals.cancelled.connect(lambda: settle())
    try:
        try:
            task.start()
        except Exception as error:
            settle(error=str(error))
        if window._trainer_setup_loading_task is task:
            loader.exec()
    finally:
        if window._trainer_setup_loading_task is task:
            cancel()
        window._trainer_setup_dialog = None
        loader.deleteLater()
    if not result or stopped[0] or not allowed():
        return False
    loaded = result[0]
    owner, (profile, managed, generations, issue) = loaded[:2]
    overview = loaded[2] if len(loaded) > 2 else None
    window._manager_trainer_setup = owner
    dialog = ManagerTrainerSetupDialog(owner, profile, state.jobs, managed=managed,
        generations=generations, held_reason=issue, overview=overview, parent=window)
    selected = {}
    dialog.preparationSelected.connect(selected.update)
    window._trainer_setup_dialog = dialog
    try:
        dialog.exec()
        reference = selected.get('review_reference')
        if reference and allowed():
            resources = TrainingSetupResourcesDialog(owner, reference, settings_path.with_name('training-setup.json'),
                state.jobs, default_output=str(state.generation_data_root / 'training/runs'),
                parent=window, validate_paths=owner.validate_paths)
            window._trainer_setup_dialog = resources
            try:
                if resources.exec() == QDialog.Accepted:
                    window.statusBar().showMessage('Trainer Setup saved. Use Load Saved Trainer when preparing an SDXL dataset.')
                    return True
            finally:
                resources.deleteLater()
    finally:
        window._trainer_setup_dialog = None
        dialog.deleteLater()
        if window._trainer_setup_close_requested:
            QTimer.singleShot(0, window.close)
    return False
