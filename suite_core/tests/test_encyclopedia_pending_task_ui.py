"""Delayed root-task presentation; synthetic workers, no native app launch."""
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer

from suite_core.encyclopedia.pending_task_ui import RootTaskProgress
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font
from suite_core.tests.test_encyclopedia_setup_presentation_ui import restore_application_theme


def test_delayed_task_paints_elapsed_close_wait_and_completion(qt_test_scope):
    clock = [10.0]
    progress = RootTaskProgress(clock=lambda: clock[0])
    release = Event()
    executor = ResourceExecutorOwner()
    future = executor.submit(lambda: release.wait(3))
    checks = []
    progress.phase('Installing the Reader update', 'Verifying the exact app files.')

    def inspect():
        try:
            assert progress.dialog.isVisible()
            assert progress.dialog.heading.text() == 'Installing the Reader update'
            clock[0] = 75
            progress.poll()
            assert '1m 05s' in progress.dialog.elapsed.text()
            progress.dialog.close()
            assert progress.dialog.isVisible() and not future.done()
            assert 'Close is waiting' in progress.dialog.guidance.text()
            checks.append('responsive while worker pending')
        finally:
            release.set()

    QTimer.singleShot(100, inspect)
    try:
        assert progress.wait(future, executor) is True
        assert checks == ['responsive while worker pending']
        assert not executor.close_held
    finally:
        release.set()
        progress.finish()


def test_success_waits_for_physical_worker_settlement(qt_test_scope):
    progress = RootTaskProgress()
    future = Future(); future.set_result('acknowledged')
    executor = SimpleNamespace(close_held=True)
    observations = []
    progress.phase('Finishing Reader Close', 'Waiting for background resources.')

    def settle():
        observations.append(progress.dialog.isVisible())
        executor.close_held = False

    QTimer.singleShot(100, settle)
    try:
        assert progress.wait(future, executor) == 'acknowledged'
        assert observations == [True]
    finally:
        progress.finish()


def test_error_returns_to_retained_recovery_without_claiming_drain(qt_test_scope):
    progress = RootTaskProgress()
    future = Future(); future.set_exception(ValueError('synthetic failure'))
    executor = SimpleNamespace(close_held=True)
    try:
        with pytest.raises(ValueError, match='synthetic failure'):
            progress.wait(future, executor)
        assert executor.close_held
    finally:
        progress.finish()


def test_phase_change_and_reader_ready_hide_only_presentation(qt_test_scope):
    progress = RootTaskProgress()
    try:
        progress.phase('Restoring the previous Reader', 'Keeping catalogs and images.')
        progress.poll()
        assert progress.dialog.isVisible()
        progress.phase('Opening the Reader', 'Preparing the selected app.')
        progress.poll()
        assert progress.dialog.heading.text() == 'Opening the Reader'
        progress.reader_ready(); progress.poll()
        assert not progress.dialog.isVisible() and progress.started is None
    finally:
        progress.finish()
    assert progress.dialog.close() is True
    assert not progress.dialog.isVisible()
    progress.phase('Late retained callback', 'Must not reopen completed UI')
    progress.poll()
    assert not progress.dialog.isVisible()


def test_presentation_failure_does_not_lose_original_worker(qt_test_scope, monkeypatch):
    progress = RootTaskProgress()
    future = Future()
    executor = SimpleNamespace(close_held=True)

    def fail():
        raise RuntimeError('synthetic presentation failure')

    monkeypatch.setattr(progress, 'poll', fail)
    try:
        with pytest.raises(RuntimeError, match='presentation failure'):
            progress.wait(future, executor)
        assert not future.done() and executor.close_held
    finally:
        progress.finish()


def test_cleanup_failure_restores_application_quit_policy(qt_test_scope, monkeypatch):
    progress = RootTaskProgress()
    progress.phase('Finishing Reader Close', 'Waiting for retained work.')
    progress.poll()
    original = progress.dialog.activity.set_active
    def fail(*args):
        raise RuntimeError('synthetic invalid widget')
    monkeypatch.setattr(progress.dialog.activity, 'set_active', fail)
    try:
        with pytest.raises(RuntimeError, match='invalid widget'):
            progress.finish()
        assert progress.finished and progress.dialog.retired
        assert progress.app.quitOnLastWindowClosed() == progress.previous_quit
    finally:
        monkeypatch.setattr(progress.dialog.activity, 'set_active', original)
        progress.finish()


def test_interrupted_event_loop_cannot_block_gui_on_unfinished_future(qt_test_scope, monkeypatch):
    from suite_core.encyclopedia import pending_task_ui
    progress = RootTaskProgress()
    future = Future()
    executor = SimpleNamespace(close_held=True)
    monkeypatch.setattr(pending_task_ui.QEventLoop, 'exec', lambda self: 0)
    try:
        with pytest.raises(RuntimeError, match='before original task settlement'):
            progress.wait(future, executor)
        assert not future.done() and executor.close_held
    finally:
        progress.finish()


def test_real_root_entry_paints_during_final_resource_close(qt_test_scope, monkeypatch):
    from suite_core.encyclopedia import __main__ as entry, pending_task_ui
    from threading import get_ident
    gui_thread = get_ident()
    release = Event()
    observations = []
    monitors = []

    class ObservedProgress(RootTaskProgress):
        def __init__(self):
            super().__init__()
            monitors.append(self)

    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', ObservedProgress)
    control = SimpleNamespace(close_intent=False, after_reader_close=lambda: None)
    def handle():
        control.close_intent = True
        return {'status': 'close_requested'}
    control.handle_next = handle
    def close():
        assert get_ident() != gui_thread
        release.wait(3)
    backend = SimpleNamespace(request=None, close_held=False,
        binding=SimpleNamespace(current=lambda: SimpleNamespace(phase='healthy')),
        poll_reader=lambda: True, close=close)
    def inspect():
        try:
            monitor = monitors[0]
            observations.append(monitor.dialog.isVisible())
            assert monitor.dialog.heading.text() == 'Finishing Reader Close'
        finally:
            release.set()
    QTimer.singleShot(200, inspect)
    try:
        assert entry._run_original_root(backend, control) == 0
        assert observations == [True]
    finally:
        release.set()


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_pending_progress_layout_and_close_guidance(app, restore_application_theme, content_font, theme):
    import os
    from pathlib import Path
    from PySide6.QtWidgets import QLabel
    from PySide6.QtTest import QTest
    from qt_app.theme import apply_theme
    apply_theme(app, theme)
    clock = [0.0]
    progress = RootTaskProgress(clock=lambda: clock[0])
    output = os.environ.get('ENCYCLOPEDIA_PENDING_CAPTURE')
    try:
        for name, heading, detail in (
            ('preparing', 'Preparing Tag Encyclopedia', 'Checking the app installation and preparing its next window.'),
            ('install', 'Installing the Reader update', 'Downloading, verifying and preparing the update. The Reader will open when it is ready.'),
            ('recovery', 'Restoring the previous Reader', 'Restoring and checking the retained app version. Your catalogs and images are kept.'),
            ('cleanup', 'Finishing Reader Close', 'Finishing saved state and releasing the app’s background resources safely.'),
            ('setup-cleanup', 'Finishing Reader setup', 'Closing the original setup and its background resources safely.'),
            ('supervision-held', 'Startup needs attention', 'Waiting for the original startup worker to settle. Keep this window open; do not start another copy.'),
            ('opening', 'Opening the Reader', 'Checking the selected Reader and preparing its window.'),
        ):
            progress.phase(heading, detail); progress.poll()
            clock[0] += 65; progress.poll(); app.processEvents(); QTest.qWait(50)
            dialog = progress.dialog
            assert '1m 05s' in dialog.elapsed.text() and dialog.elapsed.isVisible()
            assert dialog.width() <= 900 and dialog.height() <= 450
            for label in dialog.findChildren(QLabel):
                if label.isVisible():
                    assert label.height() >= label.minimumSizeHint().height()
                    point = label.mapTo(dialog, label.rect().bottomRight())
                    assert point.x() < dialog.width() and point.y() < dialog.height()
            if output:
                target = Path(output); target.mkdir(parents=True, exist_ok=True)
                assert dialog.grab().save(str(target / f'{name}-{theme}.png'))
        dialog.close(); app.processEvents()
        assert dialog.isVisible() and 'Close is waiting' in dialog.guidance.text()
        if output:
            assert dialog.grab().save(str(Path(output) / f'close-wait-{theme}.png'))
    finally:
        progress.finish()
