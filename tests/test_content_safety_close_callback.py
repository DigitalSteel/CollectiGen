"""Queued admission uses original Qt/coordinator lifetime guards; no app launch."""
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import delete
from core.background_jobs import BackgroundJobManager
from qt_app.main_window import TagManagerWindow


def window_scope():
    window=QWidget();jobs=BackgroundJobManager();attempts=[];classified=[]
    paths={Path('synthetic.png')}
    def begin(*args,**kwargs):
        attempts.append(args)
        return jobs.try_acquire('Synthetic rating',resource='cpu')
    window.state=SimpleNamespace(jobs=jobs,begin_long_operation=begin)
    window.content_safety=SimpleNamespace(unclassified=lambda values:classified.append(set(values)) or set(values))
    window._content_safety_task=None;window._pending_content_safety_paths=set(paths);window._forced_content_safety_paths=set(paths)
    return window,jobs,attempts,classified,paths


def test_queued_rating_resume_after_close_shutdown_retains_pending_scope(qt_test_scope):
    window,jobs,attempts,classified,paths=window_scope()
    QTimer.singleShot(0,lambda:TagManagerWindow._resume_content_safety(window))
    assert jobs.shutdown(wait=False)
    qt_test_scope.settle()
    assert not attempts and not classified
    assert window._pending_content_safety_paths==paths==window._forced_content_safety_paths
    assert window._content_safety_task is None
    window.close();delete(window)


def test_queued_rating_resume_after_qt_owner_deleted_does_not_touch_state(qt_test_scope):
    window,jobs,attempts,classified,paths=window_scope()
    QTimer.singleShot(0,lambda:TagManagerWindow._resume_content_safety(window))
    delete(window)
    qt_test_scope.settle()
    assert not attempts and not classified and not jobs.shutting_down
    jobs.shutdown(wait=False)


def test_live_rating_resume_still_revalidates_and_attempts_original_admission(qt_test_scope):
    window,jobs,attempts,classified,paths=window_scope()
    window._forced_content_safety_paths.clear()
    def unavailable(*args,**kwargs):attempts.append(args);return False,'Original capacity hold'
    window.state.begin_long_operation=unavailable
    TagManagerWindow._resume_content_safety(window)
    assert len(attempts)==1 and classified==[paths]
    assert window._pending_content_safety_paths==paths and window._content_safety_task is None
    window.close();delete(window);jobs.shutdown(wait=False)
