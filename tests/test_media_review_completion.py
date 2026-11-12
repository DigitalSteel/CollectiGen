"""Synthetic media-review completion and queued Close; no native/model execution."""
from pathlib import Path
import pytest
from types import SimpleNamespace
from PySide6.QtWidgets import QDialog
from qt_app.media_library_dialog import MediaLibraryDialog


def state(calls):
    def begin(owner,label,**kwargs):
        calls.append((owner,label));return False,'Scan Ingest folder'
    return SimpleNamespace(begin_long_operation=begin,finish_long_operation=lambda *_:None)


@pytest.mark.parametrize("how",["reject","close"])
def test_close_before_initial_load_never_starts_late_work(tmp_path,qt_test_scope,how):
    calls=[];dialog=MediaLibraryDialog(state(calls),[('Synthetic',tmp_path)])
    dialog.show();getattr(dialog,how)();qt_test_scope.settle(5)
    assert not calls and dialog._closing and not dialog.isVisible()


def test_busy_media_review_names_the_visible_refresh_recovery(tmp_path,qt_test_scope):
    calls=[];dialog=MediaLibraryDialog(state(calls),[('Synthetic',tmp_path)])
    qt_test_scope.show(dialog);qt_test_scope.settle(5)
    assert calls and dialog._task is None
    assert 'Scan Ingest folder' in dialog.status.text()
    assert 'choose Refresh' in dialog.status.text()
    assert dialog.refresh_button.isVisible() and dialog.refresh_button.isEnabled()
    dialog.reject()


def test_closed_player_returns_ready_feedback_and_retains_selection(tmp_path,qt_test_scope):
    calls=[];dialog=MediaLibraryDialog(state(calls),[('Synthetic',tmp_path)])
    qt_test_scope.show(dialog);qt_test_scope.settle(5)
    selected=tmp_path/'synthetic.mp4';record=SimpleNamespace(supported=True)
    dialog._record=record;dialog._selected_path=selected
    player=QDialog(dialog);dialog._player=player
    dialog.status.setText('Checking Video runtime for playback.')
    dialog._clear_player(player)
    assert dialog._player is None and dialog._record is record and dialog._selected_path==selected
    assert 'ready' in dialog.status.text() and 'Checking' not in dialog.status.text()
    assert dialog.play_button.isEnabled()
    dialog.reject()
