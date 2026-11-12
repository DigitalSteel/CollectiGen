"""Deferred conflict admission across Dataset shutdown, with a real Qt timer."""
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from qt_app.dataset_page import DatasetPage


def test_queued_conflict_start_after_shutdown_is_inert():
    app=QApplication.instance() or QApplication([])
    owner=SimpleNamespace(_shutdown_requested=False,_conflict_task=None)
    with patch('qt_app.dataset_page.CoordinatedCallTask') as task:
        QTimer.singleShot(0,lambda:DatasetPage._start_conflict_preparation(owner))
        owner._shutdown_requested=True
        app.processEvents()
        task.assert_not_called()
        assert owner._conflict_task is None


def test_active_page_starts_conflicts_once():
    app=QApplication.instance() or QApplication([])
    owner=SimpleNamespace(_shutdown_requested=False,_conflict_task=None,
        state=SimpleNamespace(knowledge=object(),jobs=object()))
    with patch('qt_app.dataset_page.CoordinatedCallTask') as task:
        QTimer.singleShot(0,lambda:DatasetPage._start_conflict_preparation(owner))
        app.processEvents()
        task.assert_called_once()
        task.return_value.start.assert_called_once_with()
        assert owner._conflict_task is task.return_value
        assert owner._conflicts_ready is False
        DatasetPage._start_conflict_preparation(owner)
        task.assert_called_once()
