"""Delayed original startup: responsive Qt, exact outcome and worker ownership."""
from threading import Event, get_ident
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer

from suite_core.encyclopedia import __main__ as entry, bootstrap_entry, entry_binding, pending_task_ui
from suite_core.encyclopedia.pending_task_ui import RootTaskProgress
from suite_core.manager.resource_executor import ResourceExecutorOwner


def test_delayed_factory_paints_before_original_outcome(qt_test_scope):
    release = Event()
    gui = get_ident()
    executor = ResourceExecutorOwner()
    progress = RootTaskProgress()
    calls, observations = [], []
    outcome = object()
    def factory():
        calls.append(get_ident())
        assert release.wait(3)
        return outcome
    def inspect():
        try:
            assert progress.dialog.isVisible()
            assert progress.dialog.heading.text() == 'Opening the Reader'
            progress.dialog.close()
            assert progress.dialog.isVisible()
            assert 'Close is waiting' in progress.dialog.guidance.text()
            observations.append('painted while original factory pending')
        finally: release.set()
    progress.phase('Opening the Reader', 'Checking the original installed app.')
    QTimer.singleShot(150, inspect)
    try:
        result, issue = entry._await_original_startup(factory, executor, progress)
        assert result is outcome and issue is None
        assert len(calls) == 1 and calls[0] != gui
        assert observations and not executor.close_held
    finally:
        release.set(); executor.begin_close(); progress.finish()


@pytest.mark.parametrize('factory_fails', [False, True])
def test_presenter_failure_resolves_same_factory_without_losing_owner(qt_test_scope, monkeypatch, factory_fails):
    release = Event()
    executor = ResourceExecutorOwner()
    progress = RootTaskProgress()
    outcome = (object(), object())
    original = RuntimeError('original factory failure')
    original.encyclopedia_root_owner = outcome[0]
    presentation = RuntimeError('presentation failed while startup pending')
    beats, calls = [], []
    def factory():
        calls.append('factory')
        assert release.wait(3)
        if factory_fails: raise original
        return outcome
    def fail(*args): raise presentation
    def settle():
        beats.append('fallback event loop painted')
        release.set()
    monkeypatch.setattr(progress, 'wait', fail)
    QTimer.singleShot(150, settle)
    try:
        if factory_fails:
            with pytest.raises(RuntimeError) as error:
                entry._await_original_startup(factory, executor, progress)
            assert error.value is original
            assert error.value.encyclopedia_root_owner is outcome[0]
        else:
            result, issue = entry._await_original_startup(factory, executor, progress)
            assert result is outcome and issue is presentation
        assert calls == ['factory'] and beats and not executor.close_held
    finally:
        release.set(); executor.begin_close(); progress.finish()


def test_existing_root_hands_actual_startup_executor_to_service(qt_test_scope, monkeypatch):
    gui = get_ident()
    backend, control = object(), object()
    factories, retained = [], []
    class ObservedProgress(RootTaskProgress):
        def __init__(self):
            super().__init__(); retained.append(self)
    def factory():
        factories.append(get_ident())
        return backend, control
    def service(actual_backend, actual_control, *, executor, progress):
        assert get_ident() == gui and actual_backend is backend and actual_control is control
        assert executor is executors[0] and not executor.close_held and not executor.closed
        assert progress is retained[0]
        executor.begin_close(); progress.finish()
        return 23
    executors = []
    class ObservedExecutor(ResourceExecutorOwner):
        def __init__(self):
            super().__init__(); executors.append(self)
    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', ObservedProgress)
    monkeypatch.setattr('suite_core.manager.resource_executor.ResourceExecutorOwner', ObservedExecutor)
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', object())
    monkeypatch.setattr(entry_binding, 'start_compiled_control', factory)
    monkeypatch.setattr(entry, '_run_original_root', service)
    assert entry.run([]) == 23
    assert len(executors) == len(factories) == 1 and factories[0] != gui


def test_factory_owner_failure_retains_actual_startup_executor(qt_test_scope, monkeypatch):
    from core.manager_launch_owner import DeploymentOwner
    owner = object.__new__(DeploymentOwner)
    original = RuntimeError('owned startup failed')
    original.encyclopedia_root_owner = owner
    executors, holds = [], []
    class ObservedExecutor(ResourceExecutorOwner):
        def __init__(self):
            super().__init__(); executors.append(self)
    def factory(): raise original
    def present(hold, *, phase):
        assert hold.owner is owner and hold.executor is executors[0] and hold.error is original
        assert phase == 'compiled_startup'
        holds.append(hold)
        hold.executor.begin_close()
        return 7
    monkeypatch.setattr('suite_core.manager.resource_executor.ResourceExecutorOwner', ObservedExecutor)
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', object())
    monkeypatch.setattr(entry_binding, 'start_compiled_control', factory)
    monkeypatch.setattr(entry, '_present_root_failure', present)
    before = len(entry._HELD_ENTRIES)
    try:
        assert entry.run([]) == 7
        assert len(executors) == len(holds) == 1
    finally: del entry._HELD_ENTRIES[before:]


def test_enrollment_open_reuses_worker_and_paints_startup(qt_test_scope, monkeypatch):
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    from suite_core.encyclopedia import root_recovery_ui
    adapter = object.__new__(StandaloneEncyclopediaEnrollment)
    backend, control = object(), object()
    executor = ResourceExecutorOwner()
    release = Event()
    monitors, seen = [], []
    class ObservedProgress(RootTaskProgress):
        def __init__(self): super().__init__(); monitors.append(self)
    def launch(actual):
        assert actual is backend and release.wait(3)
        return backend, control
    def inspect():
        try:
            assert monitors[0].dialog.isVisible()
            assert monitors[0].dialog.heading.text() == 'Opening the Reader'
            seen.append('visible startup')
        finally: release.set()
    def service(actual_backend, actual_control, **kwargs):
        assert actual_backend is backend and actual_control is control
        assert kwargs['executor'] is executor and not executor.closed and not executor.close_held
        executor.begin_close(); kwargs['progress'].finish(); return 11
    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', ObservedProgress)
    monkeypatch.setattr(root_recovery_ui, 'present_original_enrollment', lambda *args: backend)
    monkeypatch.setattr(entry_binding, 'start_original_reader', launch)
    monkeypatch.setattr(entry, '_run_original_root', service)
    QTimer.singleShot(150, inspect)
    before = len(entry._HELD_ENTRIES)
    try:
        assert entry._run_original_enrollment(adapter, executor=executor) == 11 and seen
    finally:
        release.set(); executor.begin_close()
        for monitor in monitors: monitor.finish()
        del entry._HELD_ENTRIES[before:]


def test_enrollment_success_after_presentation_failure_retains_exact_control(qt_test_scope, monkeypatch):
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    from suite_core.encyclopedia import root_recovery_ui
    adapter = object.__new__(StandaloneEncyclopediaEnrollment)
    backend, control = object(), object()
    executor = ResourceExecutorOwner()
    error = RuntimeError('startup progress failed')
    def fail(*args): raise error
    def present(hold, *, phase):
        assert hold.backend is backend and hold.control is control
        assert hold.executor is executor and hold.error is error
        assert phase == 'reader_startup_progress'
        executor.begin_close(); return 5
    monkeypatch.setattr(root_recovery_ui, 'present_original_enrollment', lambda *args: backend)
    monkeypatch.setattr(entry_binding, 'start_original_reader', lambda actual: (actual, control))
    monkeypatch.setattr(RootTaskProgress, 'wait', fail)
    monkeypatch.setattr(entry, '_present_root_failure', present)
    before = len(entry._HELD_ENTRIES)
    try: assert entry._run_original_enrollment(adapter, executor=executor) == 5
    finally: executor.begin_close(); del entry._HELD_ENTRIES[before:]


@pytest.mark.parametrize('failure_point', ['wait', 'finish'])
def test_confirmed_setup_cleanup_does_not_become_live_owner_hold(qt_test_scope, monkeypatch, failure_point):
    from suite_core.tests.test_encyclopedia_enrollment_ui import fixture_adapter
    from suite_core.encyclopedia import root_recovery_ui
    adapter, calls, _ = fixture_adapter()
    executor = ResourceExecutorOwner()
    original = RuntimeError('enrollment window failed')
    secondary = RuntimeError('cleanup progress failed')
    monitors = []
    class Progress(RootTaskProgress):
        def __init__(self): super().__init__(); monitors.append(self)
        def wait(self, future, actual_executor):
            if failure_point == 'wait': raise secondary
            return super().wait(future, actual_executor)
        def finish(self):
            super().finish()
            if failure_point == 'finish': raise secondary
    def fail(*args): raise original
    monkeypatch.setattr(root_recovery_ui, 'present_original_enrollment', fail)
    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', Progress)
    monkeypatch.setattr(entry, '_present_root_failure', lambda *args, **kwargs: pytest.fail('Completed adapter cleanup became a live hold'))
    before = len(entry._HELD_ENTRIES)
    try:
        with pytest.raises(RuntimeError) as caught:
            entry._run_original_enrollment(adapter, executor=executor)
        assert caught.value is original
        assert calls == ['close'] and executor.closed and not executor.close_held
        assert any('progress' in note for note in original.__notes__)
    finally:
        executor.begin_close()
        for monitor in monitors: RootTaskProgress.finish(monitor)
        del entry._HELD_ENTRIES[before:]


def test_interrupted_nested_loop_keeps_same_startup_responsive(qt_test_scope, monkeypatch):
    from PySide6.QtCore import QEventLoop
    release = Event()
    executor = ResourceExecutorOwner()
    progress = RootTaskProgress()
    issue = RuntimeError('presentation interrupted')
    calls, observed = [], []
    result = object()
    def factory():
        calls.append('factory'); assert release.wait(3); return result
    def fail(*args): raise issue
    def resolve():
        try:
            assert progress.dialog.isVisible() and not progress.dialog.retired
            progress.dialog.close()
            assert progress.dialog.isVisible()
            assert 'Close is waiting' in progress.dialog.guidance.text()
            observed.append('application event loop responsive')
        finally: release.set()
    monkeypatch.setattr(progress, 'wait', fail)
    monkeypatch.setattr(QEventLoop, 'exec', lambda self: 0)
    progress.phase('Opening the Reader', 'Checking the original installed app.')
    progress.poll()
    QTimer.singleShot(150, resolve)
    try:
        actual, error = entry._await_original_startup(factory, executor, progress)
        assert actual is result and error is issue
        assert observed and calls == ['factory'] and not executor.close_held
        assert not progress.finished and not progress.dialog.retired
        # The same presenter survives startup for the caller's recovery handoff.
        progress.phase('Startup needs attention', 'The original startup issue is retained.')
        progress.poll()
        assert progress.dialog.heading.text() == 'Startup needs attention'
        next_loop = QEventLoop()
        delivered = []
        def next_tick():
            delivered.append('recovery loop processed events')
            next_loop.quit()
        QTimer.singleShot(0, next_tick)
        # Restore only our forced interruption before testing the next loop.
        monkeypatch.undo()
        next_loop.exec()
        assert delivered == ['recovery loop processed events']
    finally: release.set(); executor.begin_close(); progress.finish()


def test_unavailable_qt_still_resolves_eventual_original_outcome(qt_test_scope, monkeypatch, capsys):
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QApplication
    executor = ResourceExecutorOwner()
    progress = RootTaskProgress()
    issue = RuntimeError('presentation unavailable')
    calls = []
    result = object()
    def factory():
        calls.append('factory'); Event().wait(.15); return result
    def fail(*args): raise issue
    monkeypatch.setattr(progress, 'wait', fail)
    monkeypatch.setattr(QEventLoop, 'exec', lambda self: 0)
    monkeypatch.setattr(QApplication, 'exec', lambda self: 0)
    try:
        actual, error = entry._await_original_startup(factory, executor, progress)
        assert actual is result and error is issue and calls == ['factory']
        assert not executor.close_held
        assert 'startup supervision remains held' in capsys.readouterr().err
    finally: executor.begin_close(); progress.finish()


def test_uncertain_start_retains_feedback_until_synthetic_settlement(qt_test_scope):
    """Controlled close_held fixture; does not qualify real ambiguous thread start."""
    clock = [0.0]
    progress = RootTaskProgress(clock=lambda: clock[0])
    future = Future()
    error = RuntimeError('Local measurement worker start unconfirmed')
    future.set_exception(error)
    calls, observed = [], []
    executor = SimpleNamespace(close_held=True)
    executor.submit = lambda factory: (calls.append('submit') or future)
    def inspect():
        try:
            assert progress.dialog.isVisible()
            assert progress.dialog.heading.text() == 'Startup needs attention'
            assert 'do not start another copy' in progress.dialog.detail.text()
            clock[0] = 65.0; progress.poll()
            assert '1m 05s' in progress.dialog.elapsed.text()
            observed.append('retained feedback')
        finally: executor.close_held = False
    QTimer.singleShot(200, inspect)
    try:
        with pytest.raises(RuntimeError) as caught:
            entry._await_original_startup(lambda: pytest.fail('No factory replay'), executor, progress)
        assert caught.value is error and calls == ['submit'] and observed
    finally: executor.close_held = False; progress.finish()


def test_real_enrollment_open_retires_ui_worker_before_startup_handoff(qt_test_scope, monkeypatch):
    """Real dialog/Open contract over an injected already-handed-off backend."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from suite_core.tests.test_encyclopedia_enrollment_ui import fixture_adapter
    from suite_core.encyclopedia import root_recovery_ui
    adapter, calls, backend = fixture_adapter()
    old_worker = ResourceExecutorOwner()
    control = object()
    original_present = root_recovery_ui.present_original_enrollment
    seen, monitors, painted = [], [], []
    release = Event()
    class ObservedProgress(RootTaskProgress):
        def __init__(self): super().__init__(); monitors.append(self)
    def ready(dialog):
        adapter._reader_backend = backend
        dialog.backend = backend
        dialog.poll()
        assert dialog.open_reader.isVisible() and dialog.open_reader.isEnabled()
        QTest.mouseClick(dialog.open_reader, Qt.LeftButton)
        assert old_worker.closed and not old_worker.close_held
    def present(actual, worker):
        assert actual is adapter and worker is old_worker
        return original_present(actual, worker, dialog_ready=ready)
    def service(actual_backend, actual_control, *, executor, progress):
        assert actual_backend is backend and actual_control is control
        assert executor is not old_worker and old_worker.closed
        assert not executor.closed and not executor.close_held
        seen.append(executor)
        executor.begin_close(); progress.finish(); return 19
    def launch(actual):
        assert actual is backend and release.wait(3)
        return actual, control
    def inspect():
        try:
            assert monitors[0].dialog is not None and monitors[0].dialog.isVisible()
            assert monitors[0].dialog.heading.text() == 'Opening the Reader'
            painted.append('startup visible after real dialog closes')
        finally: release.set()
    monkeypatch.setattr(root_recovery_ui, 'present_original_enrollment', present)
    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', ObservedProgress)
    monkeypatch.setattr(entry_binding, 'start_original_reader', launch)
    monkeypatch.setattr(entry, '_run_original_root', service)
    monkeypatch.setattr(entry, '_present_root_failure', lambda *a, **k: pytest.fail('Startup used the retired enrollment worker'))
    before = len(entry._HELD_ENTRIES)
    QTimer.singleShot(200, inspect)
    try:
        assert entry._run_original_enrollment(adapter, executor=old_worker) == 19
        assert len(seen) == 1 and not calls and painted
    finally:
        release.set(); old_worker.begin_close()
        for monitor in monitors: RootTaskProgress.finish(monitor)
        del entry._HELD_ENTRIES[before:]
