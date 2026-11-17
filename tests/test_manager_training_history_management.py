"""Manager-owned list management; synthetic pointers, no trainer or user stores."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from time import monotonic
import hashlib
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from core import training_run_index as storage
from core.training_run_index import TrainingRunIndex, TrainingRunRecord, ScopedTrainingHistory, TrainingRunIndexError
from test_manager_generator_setup_ui import application, capture
from suite_core.tests.workflow_ui_fixtures import workflow_window, trigger_more


def indexed(root, *, foreign=None, preparations=False):
    destination = root / ('preflight-' + 'a'*32) if preparations else root / 'runs/run'
    destination.mkdir(parents=True)
    output = destination / 'synthetic-output.txt'
    output.write_bytes(b'Synthetic retained output; not a model.\n')
    review = destination / ('preflight-result.json' if preparations else 'review.json')
    review.write_bytes(b'{"synthetic_fixture":true}\n')
    registry = root.parent / 'model_resources.json'
    row = TrainingRunRecord('a'*32, 'preflight' if preparations else 'execution', str(review), hashlib.sha256(review.read_bytes()).hexdigest(),
        'c'*64, str(foreign or destination), registry_path='' if preparations else str(registry), registry_revision='' if preparations else 'd'*64,
        phase='prepared' if preparations else 'completed', latest_journal_tip='e'*64)
    index = TrainingRunIndex(root / ('sdxl-preflight-runs.json' if preparations else 'training-runs.json'))
    revision = index.register(row, index.snapshot()[1])
    return index, row, revision, review, output, registry


def interrupted_forget(index, row, revision, monkeypatch):
    original = storage.atomic_verified_bytes
    def crash(path, value, **kwargs):
        if Path(path) == index.path:
            raise OSError('Synthetic crash after pending recovery was sealed')
        return original(path, value, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'atomic_verified_bytes', crash)
        with pytest.raises(OSError, match='Synthetic crash'):
            index.forget(row.run_id, revision)
    assert index.pending.is_file()


def test_scoped_forget_preserves_run_files(tmp_path):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root)
    before = (review.read_bytes(), output.read_bytes())
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=lambda: True)
    history.forget(row.run_id, revision)
    assert index.snapshot()[0] == ()
    assert (review.read_bytes(), output.read_bytes()) == before
    assert not index.pending.exists()


@pytest.mark.parametrize('allowed', [None, lambda: False])
def test_default_or_revoked_scoped_history_refuses_list_writes(tmp_path, allowed):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root)
    before = index.path.read_bytes()
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=allowed)
    with pytest.raises(TrainingRunIndexError, match='read-only'):
        history.forget(row.run_id, revision)
    with pytest.raises(TrainingRunIndexError, match='read-only'):
        history.recover()
    assert index.path.read_bytes() == before


def test_scoped_forget_refuses_stale_revision_and_ended_session(tmp_path, monkeypatch):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root)
    ready = [True]
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=lambda: ready[0])
    with pytest.raises(TrainingRunIndexError, match='changed'):
        history.forget(row.run_id, 'f'*64)
    snapshot = history.snapshot
    def ended():
        value = snapshot(); ready[0] = False; return value
    monkeypatch.setattr(history, 'snapshot', ended)
    before = index.path.read_bytes()
    with pytest.raises(TrainingRunIndexError, match='read-only'):
        history.forget(row.run_id, revision)
    assert index.path.read_bytes() == before


def test_scoped_forget_refuses_foreign_pointer(tmp_path):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root, foreign=tmp_path/'foreign')
    before = index.path.read_bytes()
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=lambda: True)
    with pytest.raises(TrainingRunIndexError, match='outside'):
        history.forget(row.run_id, revision)
    assert index.path.read_bytes() == before


def test_scoped_recovery_finishes_original_pointer_transaction(tmp_path, monkeypatch):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root)
    before = (review.read_bytes(), output.read_bytes())
    interrupted_forget(index, row, revision, monkeypatch)
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=lambda: True)
    history.recover()
    assert index.snapshot()[0] == () and not index.pending.exists()
    assert (review.read_bytes(), output.read_bytes()) == before


def test_scoped_recovery_refuses_foreign_retained_before_state(tmp_path, monkeypatch):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root, foreign=tmp_path/'foreign')
    interrupted_forget(index, row, revision, monkeypatch)
    before = (index.path.read_bytes(), index.pending.read_bytes())
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=lambda: True)
    with pytest.raises(TrainingRunIndexError, match='outside'):
        history.recover()
    assert (index.path.read_bytes(), index.pending.read_bytes()) == before


def test_scoped_recovery_refuses_foreign_retained_after_state(tmp_path, monkeypatch):
    root = tmp_path / 'training'
    index, row, revision, review, output, registry = indexed(root)
    original = storage.atomic_verified_bytes
    def crash(path, value, **kwargs):
        if Path(path) == index.path: raise OSError('Synthetic pending registration')
        return original(path, value, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'atomic_verified_bytes', crash)
        with pytest.raises(OSError, match='Synthetic pending'):
            index.register(replace(row, run_id='b'*32, destination=str(tmp_path/'foreign')), revision)
    before = (index.path.read_bytes(), index.pending.read_bytes())
    history = ScopedTrainingHistory(root, model_registry_path=registry, write_allowed=lambda: True)
    with pytest.raises(TrainingRunIndexError, match='outside'):
        history.recover()
    assert (index.path.read_bytes(), index.pending.read_bytes()) == before


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('action', ['forget', 'recover'])
@pytest.mark.parametrize('preparations', [False, True])
def test_visible_manager_history_list_management(tmp_path, monkeypatch, action, preparations, theme):
    app = application()
    from qt_app.theme import apply_theme
    from PySide6.QtWidgets import QLabel
    apply_theme(app, theme)
    root = tmp_path / ('Suite/Data/Manager/TrainingReview' if preparations else 'Suite/Data/Manager/training')
    index, row, revision, review, output, registry = indexed(root, preparations=preparations)
    before = (review.read_bytes(), output.read_bytes())
    if action == 'recover': interrupted_forget(index, row, revision, monkeypatch)
    app, session, window = workflow_window(tmp_path, 'train')
    errors = []; state = {'done':False, 'clicked':False, 'presentation':False}
    deadline = monotonic() + 20
    def drive():
        if state['done']: return
        dialog = getattr(window, '_training_history_dialog', None)
        try:
            assert monotonic() < deadline, 'History list-management fixture timed out'
            if dialog is not None:
                if not state['presentation']:
                    dialog.resize(1920,1080)
                    texts=[label.text() for label in dialog.findChildren(QLabel)]
                    assert dialog.runs.headerItem().text(0)==('Preparation' if preparations else 'Run')
                    assert ('Reopen a prepared SDXL dataset' if preparations else 'Find a saved Training review') in texts
                    assert dialog.open_button.text()==('Review Selected Preparation' if preparations else 'Open Selected')
                    if preparations:
                        assert 'Select a prepared dataset, then review its saved receipt. Preparation does not start training.' in texts
                    state['presentation']=True
                if not state['clicked']:
                    if action == 'forget':
                        if dialog.runs.topLevelItemCount():
                            selected = dialog.runs.topLevelItem(0)
                            assert selected.text(0)==('SDXL preparation 1' if preparations else 'run')
                            assert selected.toolTip(0)==row.run_id
                            if not selected.isSelected():
                                QTest.mouseClick(dialog.runs.viewport(), Qt.LeftButton, pos=dialog.runs.visualItemRect(selected).center())
                            if preparations and not dialog.details_button.isChecked():
                                QTest.mouseClick(dialog.details_button, Qt.LeftButton)
                        button = dialog.forget_button
                    else: button = dialog.recover_button
                    if button.isVisible() and button.isEnabled():
                        capture(dialog, 'manager-history-' + ('preparations-' if preparations else '') + action + '-' + theme + '-before')
                        QTest.mouseClick(button, Qt.LeftButton); state['clicked'] = True
                elif (dialog._task is None and not index.pending.exists() and index.snapshot()[0] == ()
                      and dialog.page_summary.text() == ('Saved preparations: 0' if preparations else 'Saved reviews: 0') and not dialog._needs_recovery):
                    capture(dialog, 'manager-history-' + ('preparations-' if preparations else '') + action + '-' + theme + '-complete')
                    state['done'] = True; dialog.reject(); return
            QTimer.singleShot(20, drive)
        except BaseException as exc:
            errors.append(exc); state['done'] = True
            if dialog is not None: dialog.reject()
    try:
        QTimer.singleShot(20, drive)
        if preparations:
            assert window.surface.history.isVisible() and window.surface.history.isEnabled()
            QTest.mouseClick(window.surface.history, Qt.LeftButton)
        else:
            trigger_more(window, 'Saved Runs…')
        assert not errors, errors
        assert state['clicked'] and state['done']
        assert index.snapshot()[0] == ()
        assert (review.read_bytes(), output.read_bytes()) == before
    finally:
        state['done'] = True; window.close(); app.processEvents()
