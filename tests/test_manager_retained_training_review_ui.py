"""Manager visible-control handoff decisions; no trainer or user stores."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
import time
import re
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QDialogButtonBox
from core import training_run_index as index_module
from core.training_run_index import TrainingRunRecord, TrainingRunPage, TrainingRunIndexError
from suite_core.manager.window import create_window, unconfigured


@pytest.mark.parametrize('mode', ['open', 'transient', 'stale', 'revoked', 'selection', 'close', 'foreign'])
def test_manager_history_retained_handoff_uses_current_selection(tmp_path, monkeypatch, mode):
    app = QApplication.instance() or QApplication([])
    session = NS(state='ready', admitted=NS(layout=NS(data_root=tmp_path)))
    manager = tmp_path / 'Manager'
    registry = manager / 'model_resources.json'
    record = TrainingRunRecord('a' * 32, 'execution', str(manager / 'training/runs/run.execution-review.json'),
        'b' * 64, 'c' * 64, str(manager / 'training/runs/run'),
        registry_path=str(registry if mode != 'foreign' else tmp_path / 'foreign/models.json'),
        registry_revision='d' * 64, phase='completed', latest_journal_tip='e' * 64, sequence=1)
    other = replace(record, run_id='b' * 32, sequence=2, destination=str(manager / 'training/runs/other'))
    records = (other, record) if mode == 'selection' else (record,)
    revision = 'f' * 64
    entered, release = Event(), Event()
    checks, calls, errors = [], [], []

    class History:
        def page(self, *, offset=0, limit=50):
            return TrainingRunPage(records, len(records), offset, None, revision)
        def snapshot(self):
            return records, revision
        def review_execution_outcome(self, selected, selected_revision):
            checks.append((selected, selected_revision))
            if mode == 'transient' and len(checks) == 1:
                raise RuntimeError('The saved review is temporarily unavailable. Try again.')
            if mode == 'stale':
                raise TrainingRunIndexError('Training history changed. Refresh and select the review again.')
            if mode == 'revoked':
                session.state = 'revoked'
            if mode in {'selection', 'close'}:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError('Generated test did not release the receipt check.')
            return 'exited_unverified', 0, None

    monkeypatch.setattr(index_module, 'ScopedTrainingHistory', lambda *a, **k: History())
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.sdxl_preparation_controller = lambda selected_registry: NS(
        _open_retained=lambda selected, selected_revision: calls.append(
            (selected, selected_revision, selected_registry.path, window._training_history_dialog)))
    window.show()
    item = window.navigation.item(window.page_rows['Applications'])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
        pos=window.navigation.visualItemRect(item).center())
    state = {'done': False, 'selected': False, 'clicked': False, 'changed': False}
    deadline = time.monotonic() + 12

    def drive():
        if state['done']:
            return
        QTimer.singleShot(20, drive)
        dialog = getattr(window, '_training_history_dialog', None)
        if dialog is None:
            return
        try:
            if time.monotonic() > deadline:
                raise TimeoutError(str((mode, state, len(checks), dialog.retained_status.text(), dialog.runs.isEnabled())))
            if not dialog.runs.topLevelItemCount():
                if dialog.page_error.text():
                    raise AssertionError(dialog.page_error.text())
                return
            if not state['selected']:
                row = next(dialog.runs.topLevelItem(i) for i in range(dialog.runs.topLevelItemCount())
                           if dialog.runs.topLevelItem(i).data(0, Qt.UserRole)[0] == record.run_id)
                QTest.mouseClick(dialog.runs.viewport(), Qt.LeftButton,
                    pos=dialog.runs.visualItemRect(row).center())
                state['selected'] = True
            button = next(b for b in dialog.findChildren(QPushButton)
                          if b.text() == 'Open Saved Training Review')
            if mode == 'foreign':
                assert not button.isVisible()
                QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.Close), Qt.LeftButton)
                return
            if not state['clicked']:
                if not button.isEnabled():
                    return
                assert button.isVisible() and button.property('primary') is True
                assert dialog.open_button.property('primary') is False
                state['clicked'] = True
                if mode == 'open':
                    QTest.keyClick(button, Qt.Key_Space)
                else:
                    QTest.mouseClick(button, Qt.LeftButton)
                return
            if mode == 'transient' and len(checks) == 1 and 'temporarily unavailable' in dialog.retained_status.text():
                assert button.isEnabled()
                QTest.mouseClick(button, Qt.LeftButton)
            if mode in {'selection', 'close'} and entered.is_set() and not state['changed']:
                if 'delay_started' not in state:
                    state['delay_started'] = time.monotonic()
                elapsed = re.search(r'Elapsed (\d+)s', dialog.retained_status.text())
                if time.monotonic() - state['delay_started'] < 1.1 or not elapsed or int(elapsed[1]) < 1:
                    return
                assert dialog.progress.isVisible()
                assert dialog.progress.minimum() == dialog.progress.maximum() == 0
                capture = os.environ.get('COLLECTIGEN_RETAINED_UI_CAPTURES')
                if capture:
                    Path(capture).mkdir(parents=True, exist_ok=True)
                    dialog.grab().save(str(Path(capture) / ('receipt-delay-' + mode + '.png')))
                state['changed'] = True
                if mode == 'selection':
                    row = next(dialog.runs.topLevelItem(i) for i in range(dialog.runs.topLevelItemCount())
                               if dialog.runs.topLevelItem(i).data(0, Qt.UserRole)[0] == other.run_id)
                    QTest.mouseClick(dialog.runs.viewport(), Qt.LeftButton,
                        pos=dialog.runs.visualItemRect(row).center())
                else:
                    QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.Close), Qt.LeftButton)
                release.set()
            if mode in {'stale', 'revoked', 'selection'} and checks:
                message = dialog.retained_status.text()
                if any(s in message for s in ('changed', 'no longer ready', 'previous check finished')):
                    QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.Close), Qt.LeftButton)
        except BaseException as exc:
            errors.append(repr(exc)); release.set(); dialog.reject()

    try:
        QTimer.singleShot(20, drive)
        QTest.mouseClick(window.training_history_button, Qt.LeftButton)
        state['done'] = True
        assert not errors, errors
        if mode in {'open', 'transient'}:
            assert calls == [(record, revision, registry, None)]
        else:
            assert not calls
        expected_checks = [(record, revision)] * (2 if mode == 'transient' else 1)
        assert not checks if mode == 'foreign' else checks == expected_checks
    finally:
        state['done'] = True; release.set()
        window.close(); app.processEvents(); QTest.qWait(30)


@pytest.mark.parametrize('close_during_check', [False, True])
def test_retained_restore_delayed_progress_and_close(tmp_path, monkeypatch, close_during_check):
    from core.background_jobs import BackgroundJobManager
    from qt_app import training_restore_dialog as restore_module
    app = QApplication.instance() or QApplication([])
    entered, release = Event(), Event()
    context = NS(review=NS(record=NS(kind='execution')), package_error='')
    def opening(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return context
    monkeypatch.setattr(restore_module, 'restore_training_context', opening)
    jobs = BackgroundJobManager()
    dialog = restore_module.TrainingRestoreDialog(NS(), 'a' * 32, 'b' * 64, jobs)
    restored = []
    dialog.restored.connect(restored.append)
    dialog.show()
    deadline = time.monotonic() + 4
    try:
        while not entered.is_set() and time.monotonic() < deadline:
            QTest.qWait(20)
        assert entered.is_set()
        QTest.qWait(1100)
        while 'Elapsed 0s' in dialog.activity.text() and time.monotonic() < deadline:
            QTest.qWait(50)
        assert dialog.progress.isVisible() and dialog.progress.minimum() == dialog.progress.maximum() == 0
        assert re.search(r'Elapsed [1-9]\d*s', dialog.activity.text())
        capture = os.environ.get('COLLECTIGEN_RETAINED_UI_CAPTURES')
        if capture:
            Path(capture).mkdir(parents=True, exist_ok=True)
            dialog.grab().save(str(Path(capture) / ('restore-delay-' + str(close_during_check) + '.png')))
        if close_during_check:
            box = dialog.findChild(QDialogButtonBox)
            QTest.mouseClick(box.button(QDialogButtonBox.Close), Qt.LeftButton)
            assert dialog.isVisible() and 'Closing' in dialog.message.text()
        release.set()
        while dialog._task is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        assert dialog._task is None and not dialog.isVisible()
        assert restored == ([] if close_during_check else [context])
    finally:
        release.set(); dialog.reject(); jobs.shutdown(wait=True); app.processEvents()


def test_retained_restore_failure_can_close_and_reopen(tmp_path, monkeypatch):
    from core.background_jobs import BackgroundJobManager
    from qt_app import training_restore_dialog as restore_module
    app = QApplication.instance() or QApplication([])
    context = NS(review=NS(record=NS(kind='execution')), package_error='')
    calls = []
    def opening(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError('Saved package unavailable. Restore it and reopen this review.')
        return context
    monkeypatch.setattr(restore_module, 'restore_training_context', opening)
    jobs = BackgroundJobManager()
    dialogs = []
    try:
        first = restore_module.TrainingRestoreDialog(NS(), 'a' * 32, 'b' * 64, jobs)
        dialogs.append(first); first.show()
        deadline = time.monotonic() + 4
        while not calls or first._task is not None:
            assert time.monotonic() < deadline
            QTest.qWait(20)
        assert 'reopen this review' in first.message.text()
        QTest.qWait(50)
        close_button = first.findChild(QDialogButtonBox).button(QDialogButtonBox.Close)
        assert close_button.isVisible() and not close_button.visibleRegion().isEmpty()
        capture = os.environ.get('COLLECTIGEN_RETAINED_UI_CAPTURES')
        if capture:
            Path(capture).mkdir(parents=True, exist_ok=True)
            first.grab().save(str(Path(capture) / 'restore-error.png'))
        QTest.mouseClick(close_button, Qt.LeftButton)
        assert not first.isVisible()
        second = restore_module.TrainingRestoreDialog(NS(), 'a' * 32, 'b' * 64, jobs)
        dialogs.append(second); restored = []; second.restored.connect(restored.append); second.show()
        while not restored:
            assert time.monotonic() < deadline
            QTest.qWait(20)
        assert restored == [context] and len(calls) == 2 and not second.isVisible()
    finally:
        for dialog in dialogs: dialog.reject()
        jobs.shutdown(wait=True); app.processEvents()
