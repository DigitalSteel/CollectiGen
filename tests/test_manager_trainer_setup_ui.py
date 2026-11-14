"""Synthetic visible Manager trainer routes; no downloads or native trainer runs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QMainWindow, QProgressDialog

from core.background_jobs import BackgroundJobManager
from core.manager_trainer_setup import ManagerTrainerSetup
from core.settings import AppSettings
from core.trainer_environment import TrainerGeneration, TrainerHealth
from qt_app import training_setup_queue_controller as queue_ui
from qt_app.training_setup_dialog import TrainingSetupOverview, TrainingSetupPreview
from suite_core.manager import trainer_setup_ui as ui
from suite_core.manager.window import create_window, unconfigured
from test_manager_generator_setup_ui import application, wait_until, capture
from test_manager_trainer_setup import FakeExecutor, FakeQueue, GENERATION, operation


OVERVIEW = TrainingSetupOverview('Synthetic Python / pinned trainer / CUDA',
    'Synthetic reviewed software cost and storage', 'Synthetic source and license evidence',
    'No model, dataset or native software is accessed.', 3, 'Synthetic license')


@pytest.fixture
def trainer(tmp_path, monkeypatch):
    app = application()
    jobs = BackgroundJobManager()
    executor = FakeExecutor(tmp_path / 'manager-training')
    executor.result = TrainerGeneration(GENERATION, executor.root / 'software', executor.profile_value.sha256,
        TrainerHealth(True, 'Synthetic health only'), 1.0, 'f' * 64)
    monkeypatch.setattr(ui, 'profile_overview', lambda profile: OVERVIEW)
    monkeypatch.setattr(queue_ui, 'profile_overview', lambda profile: OVERVIEW)
    owner = ManagerTrainerSetup(jobs, executor.root, AppSettings(), executor=executor,
        profile_path=tmp_path / 'profile.json', catalog_path=tmp_path / 'catalog.json',
        source_root=tmp_path / 'source', session_allowed=lambda: True, protected_roots=lambda: (),
        queue_factory=FakeQueue)
    dialogs = []
    yield NS(app=app, jobs=jobs, owner=owner, executor=executor, dialogs=dialogs, tmp=tmp_path)
    for dialog in dialogs:
        dialog._queue_timer.stop()
        dialog.reject()
    jobs.shutdown(wait=True)
    app.processEvents()


def row(identity, state, action='import', selection=None, *, sequence=1):
    value = operation(identity, state, {'action': action, 'selection': selection, 'review_sha256': 'c' * 64})
    value.label, value.sequence, value.error = 'Synthetic ' + action, sequence, ''
    return value


def show(trainer, *, managed=True):
    h = trainer
    dialog = ui.ManagerTrainerSetupDialog(h.owner, h.executor.profile(), h.jobs,
        managed=managed, generations=((GENERATION, 'Synthetic retained generation'),))
    h.dialogs.append(dialog)
    dialog.resize(1080, 790)
    dialog.show()
    # Stock Setup schedules its initial profile read on the next event turn.
    # Waiting only for an empty task slot returns before that read starts.
    wait_until(h.app, lambda: dialog._profile_sha256 == h.executor.profile_value.sha256
               and dialog._task is None)
    return dialog


def click(h, button):
    assert button.isVisible() and button.isEnabled()
    QTest.mouseClick(button, Qt.LeftButton)
    h.app.processEvents()


def advanced(h, dialog, name):
    dialog.tabs.setCurrentIndex(1)
    item = next(dialog.advanced_tasks.item(index) for index in range(dialog.advanced_tasks.count())
        if dialog.advanced_tasks.item(index).text() == name)
    QTest.mouseClick(dialog.advanced_tasks.viewport(), Qt.LeftButton,
        pos=dialog.advanced_tasks.visualItemRect(item).center())
    h.app.processEvents()
    assert dialog.advanced_tasks.currentItem() is item


def test_all_stock_actions_are_reachable_with_managed_catalog(trainer):
    h = trainer
    dialog = show(h)
    assert dialog.managed_button.isVisible() and dialog.managed_button.isEnabled()
    capture(dialog, 'trainer-manager-first-open')
    advanced(h, dialog, 'Existing Software')
    assert dialog.existing_button.isVisible() and dialog.existing_button.isEnabled()
    assert dialog.manual_button.isVisible() and dialog.manual_button.isEnabled()
    advanced(h, dialog, 'Offline Transfer')
    assert all(button.isVisible() and button.isEnabled() for button in (dialog.import_button, dialog.export_button))
    capture(dialog, 'trainer-manager-offline-transfer')
    advanced(h, dialog, 'Recovery')
    assert all(button.isVisible() and button.isEnabled() for button in (dialog.repair_button, dialog.rollback_button))
    capture(dialog, 'trainer-manager-recovery')
    # Activation is intentionally reached after a verified generation, rather
    # than advertised as an unrelated default action.
    dialog._applied('managed', h.executor.result)
    dialog._update_actions()
    click(h, dialog.use_button)
    wait_until(h.app, lambda: dialog._task is None)
    assert dialog._preview.action == 'activate'
    assert dialog.confirm_button.isVisible() and dialog.confirm_button.isEnabled()
    assert not h.owner.queue.enqueued
    assert not any(call[0] == 'execute' for call in h.executor.calls)
    capture(dialog, 'trainer-manager-activation-review')


def test_missing_managed_catalog_keeps_other_stock_routes_available(trainer):
    h = trainer
    dialog = show(h, managed=False)
    assert not dialog.managed_button.isVisible()
    advanced(h, dialog, 'Offline Transfer')
    assert dialog.import_button.isEnabled() and dialog.export_button.isEnabled()
    advanced(h, dialog, 'Recovery')
    assert dialog.repair_button.isEnabled() and dialog.rollback_button.isEnabled()
    assert not h.owner.queue.enqueued


def test_saved_operation_visible_review_requires_separate_confirmation(trainer):
    h = trainer
    original = row('original', 'failed', selection=str(h.tmp / 'offline-pack.zip'))
    h.owner.queue.rows = [original]
    dialog = show(h)
    assert dialog.saved_operations.currentData() == original.id
    assert dialog._preview is None
    click(h, dialog.review_saved_button)
    wait_until(h.app, lambda: dialog._task is None and dialog._preview is not None)
    assert isinstance(dialog._preview, TrainingSetupPreview)
    assert dialog._preview.action == 'import'
    assert dialog.confirm_button.isVisible() and dialog.confirm_button.isEnabled()
    assert 'Review' in dialog.status.text()
    assert h.owner.queue.enqueued == [] and h.owner.queue.cancelled == []
    assert not any(call[0] == 'execute' for call in h.executor.calls)
    capture(dialog, 'trainer-manager-retained-confirmation')


def test_saved_waiting_cancel_targets_selected_original_without_enqueue(trainer):
    h = trainer
    first = row('first-waiting', 'waiting', selection=str(h.tmp / 'first-pack.zip'), sequence=1)
    selected = row('selected-waiting', 'waiting', selection=str(h.tmp / 'selected-pack.zip'), sequence=2)
    h.owner.queue.rows = [first, selected]
    dialog = show(h)
    dialog.saved_operations.setCurrentIndex(dialog.saved_operations.findData(selected.id))
    assert 'unfinished' in dialog.held.text()
    assert dialog.review_saved_button.isDefault()
    click(h, dialog.cancel_saved_button)
    assert h.owner.queue.cancelled == [selected.id]
    assert selected.state == 'cancelled' and first.state == 'waiting'
    assert dialog._preview is None and dialog._queued is None
    assert not h.owner.queue.enqueued
    assert not any(call[0] == 'execute' for call in h.executor.calls)


def test_running_operation_reattaches_exact_row_and_modal_close_preserves_queue(trainer):
    h = trainer
    original = row('original-running', 'running', selection=str(h.tmp / 'offline-pack.zip'))
    h.owner.queue.rows = [original]
    dialog = show(h)
    wait_until(h.app, lambda: dialog._queued is not None and dialog._task is None)
    assert dialog._queued.operation_id == original.id
    assert dialog.controller.queue is h.owner.queue
    dialog.reject()
    h.app.processEvents()
    assert not dialog.isVisible()
    assert h.owner.queue.rows == [original] and original.state == 'running'
    assert not h.owner.queue.enqueued and not h.owner.queue.cancelled
    reopened = show(h)
    wait_until(h.app, lambda: reopened._queued is not None)
    assert reopened._queued.operation_id == original.id
    assert reopened.controller.queue is dialog.controller.queue
    capture(reopened, 'trainer-manager-original-running-operation')


def test_completed_saved_operation_reviews_original_result_without_requeue(trainer):
    h = trainer
    original = row('original-complete', 'complete', 'activate', GENERATION)
    h.owner.queue.rows = [original]
    dialog = show(h)
    click(h, dialog.review_saved_button)
    wait_until(h.app, lambda: dialog._queued is not None or dialog._candidate is not None)
    wait_until(h.app, lambda: dialog._task is None and dialog._candidate is not None)
    assert dialog._candidate_reference == original.params
    assert dialog._candidate[0] == 'activate'
    assert dialog._candidate[1] is h.executor.result
    assert dialog.use_button.text() == 'Use This Trainer'
    selected = []
    dialog.preparationSelected.connect(selected.append)
    click(h, dialog.use_button)
    assert selected[0]['review_reference'] == original.params
    assert selected[0]['result'] is h.executor.result
    assert not h.owner.queue.enqueued and not h.owner.queue.cancelled


def test_manual_export_does_not_select_or_execute_trainer(trainer):
    h = trainer
    dialog = show(h)
    selected = []
    dialog.preparationSelected.connect(selected.append)
    advanced(h, dialog, 'Existing Software')
    click(h, dialog.manual_button)
    assert selected == [{'mode': 'manual'}]
    assert not h.owner.queue.enqueued
    assert not any(call[0] == 'execute' for call in h.executor.calls)


def normal_window(h):
    session = NS(state='ready', admitted=NS(layout=NS(data_root=h.tmp / 'suite')))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session,
        training_draft_settings_path=h.tmp / 'manager' / 'generation-settings.json')
    window.resize(1920, 1080)
    window.show()
    window.generation_state()
    h.app.processEvents()
    return window


def test_normal_manager_training_control_invokes_bound_current_session(trainer, monkeypatch):
    h = trainer
    calls = []
    monkeypatch.setattr(ui, 'open_manager_trainer_setup',
        lambda window, state, settings_path, *, allowed: calls.append((window, state, settings_path, allowed())) or False)
    window = normal_window(h)
    window_jobs = window.state.jobs
    try:
        window.navigation.setCurrentRow(window.page_rows['Training'])
        assert window.trainer_setup_button.text() == 'Set Up Trainer\u2026'
        capture(window, 'trainer-manager-normal-entry')
        click(h, window.trainer_setup_button)
        assert len(calls) == 1 and calls[0][0] is window and calls[0][1] is window.state
        assert calls[0][2] == h.tmp / 'manager' / 'generation-settings.json' and calls[0][3]
    finally:
        window.close()
        window_jobs.shutdown(wait=True)


def test_manager_close_retains_original_native_owner_after_modal_is_gone(trainer):
    h = trainer
    window = normal_window(h)
    window_jobs = window.state.jobs
    window._manager_trainer_setup = h.owner
    original = row('original-running', 'running', 'activate', GENERATION)
    h.owner.queue.rows = [original]
    h.executor.process.has_retained_owners = True
    try:
        window.close()
        h.app.processEvents()
        assert window.isVisible() and h.owner.close_held
        assert window._manager_trainer_setup is h.owner
        assert h.owner.queue.cancelled == [original.id]
        assert 'reopen Set Up Trainer' in window.statusBar().currentMessage()
        capture(window, 'trainer-manager-close-retains-native-owner')
    finally:
        h.executor.process.has_retained_owners = False
        window.close()
        window_jobs.shutdown(wait=True)


def test_manager_close_holds_original_loading_task_and_requests_exact_cancellation(trainer, monkeypatch):
    h = trainer
    window = normal_window(h)
    window_jobs = window.state.jobs
    original = NS(job_id='original-trainer-loader')
    window._trainer_setup_loading_task = original
    modal = QDialog(window)
    modal.show()
    window._trainer_setup_dialog = modal
    cancelled = []
    monkeypatch.setattr(window.state.jobs, 'cancel', cancelled.append)
    try:
        window.close()
        h.app.processEvents()
        assert window.isVisible()
        assert window._trainer_setup_loading_task is original
        assert window._trainer_setup_close_requested
        assert cancelled == [original.job_id]
        assert not modal.isVisible()
        assert 'finish, then close Manager again' in window.statusBar().currentMessage()
    finally:
        window._trainer_setup_loading_task = None
        window._trainer_setup_dialog = None
        modal.deleteLater()
        window.close()
        window_jobs.shutdown(wait=True)


class TaskSignals(QObject):
    complete = Signal(object)
    failed = Signal(object)
    cancelled = Signal()


class DeferredTask:
    instances = []
    immediate = False

    def __init__(self, jobs, name, work, **kwargs):
        self.work, self.signals = work, TaskSignals()
        self.job_id = 'synthetic-loader'
        self.__class__.instances.append(self)

    def start(self):
        if self.immediate:
            self.deliver()

    def deliver(self):
        result = self.work(NS(cancel_requested=False))
        self.signals.complete.emit(result)


def test_loading_cancel_preserves_late_retained_observer_until_settlement(trainer, monkeypatch):
    h = trainer
    import core.manager_trainer_setup as owner_module
    DeferredTask.instances.clear()
    monkeypatch.setattr(ui, 'CoordinatedCallTask', DeferredTask)
    monkeypatch.setattr(owner_module, 'ManagerTrainerSetup', lambda *args, **kwargs: h.owner)
    monkeypatch.setattr(h.owner, 'describe', lambda **kwargs: (h.executor.profile(), True, (), ''))
    cancelled = []
    window = QMainWindow()
    window._trainer_setup_dialog = None
    window._trainer_setup_close_requested = False
    state = NS(jobs=NS(cancel=cancelled.append), settings=AppSettings(), generation_data_root=h.tmp / 'manager')
    monkeypatch.setattr(QProgressDialog, 'exec', lambda dialog: dialog.reject())
    assert not ui.open_manager_trainer_setup(window, state, h.tmp / 'absent-settings.json', allowed=lambda: True)
    task = DeferredTask.instances[-1]
    assert window._trainer_setup_loading_task is task
    assert cancelled == [task.job_id]
    assert window._trainer_setup_dialog is None
    # The late owner may contain restored queue/native ownership. Cancellation
    # must dismiss presentation without discarding that observer.
    task.deliver()
    assert window._trainer_setup_loading_task is None
    assert window._manager_trainer_setup is h.owner
    assert not h.owner.queue.enqueued
    window.deleteLater()


@pytest.mark.parametrize('mode,resource_result,expected', (
    ('manual', QDialog.Rejected, False), ('managed', QDialog.Rejected, False),
    ('managed', QDialog.Accepted, True)))
def test_helper_preserves_manual_and_saved_resource_return_contract(trainer, monkeypatch, mode, resource_result, expected):
    h = trainer
    import core.manager_trainer_setup as owner_module
    DeferredTask.instances.clear()
    monkeypatch.setattr(DeferredTask, 'immediate', True)
    monkeypatch.setattr(ui, 'CoordinatedCallTask', DeferredTask)
    monkeypatch.setattr(owner_module, 'ManagerTrainerSetup', lambda *args, **kwargs: h.owner)
    monkeypatch.setattr(h.owner, 'describe', lambda **kwargs: (h.executor.profile(), True, (), ''))
    reference = {'action': 'activate', 'selection': GENERATION}
    resources = []

    class Choice(QDialog):
        preparationSelected = Signal(object)

        def __init__(self, *args, **kwargs):
            super().__init__(kwargs.get('parent'))

        def exec(self):
            self.preparationSelected.emit({'mode': mode, **({'review_reference': reference} if mode != 'manual' else {})})
            return QDialog.Accepted

    class Resources(QDialog):
        def __init__(self, owner, reviewed, settings_path, jobs, **kwargs):
            super().__init__(kwargs.get('parent'))
            resources.append((owner, reviewed, settings_path, jobs, kwargs))

        def exec(self):
            return resource_result

    monkeypatch.setattr(ui, 'ManagerTrainerSetupDialog', Choice)
    monkeypatch.setattr(ui, 'TrainingSetupResourcesDialog', Resources)
    window = QMainWindow()
    window._trainer_setup_dialog = None
    state = NS(jobs=h.jobs, settings=AppSettings(), generation_data_root=h.tmp / 'manager')
    path = h.tmp / 'absent-generation-settings.json'
    assert ui.open_manager_trainer_setup(window, state, path, allowed=lambda: True) is expected
    assert window._manager_trainer_setup is h.owner
    assert window._trainer_setup_dialog is None
    assert len(resources) == (0 if mode == 'manual' else 1)
    if resources:
        owner, reviewed, settings_path, jobs, options = resources[0]
        assert owner is h.owner and reviewed is reference and jobs is h.jobs
        assert settings_path == path.with_name('training-setup.json')
        assert options['default_output'] == str(state.generation_data_root / 'training/runs')
        assert options['validate_paths'].__self__ is h.owner
    assert not h.owner.queue.enqueued
    window.deleteLater()
