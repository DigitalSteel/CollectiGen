"""Original generated custody cases applicable to public host; no Learning route."""
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox
from shiboken6 import isValid
import qt_app.duplicate_preflight_dialog as ui
from qt_app.workers import CoordinatedCallTask
import test_duplicate_review_write_custody_ui as custody
from duplicate_custody_test_support import wait_for,capture,qt_test_scope

def make(scope,monkeypatch,purpose='preparation'):
    o=custody.make(scope,monkeypatch,'preflight');registered=NS();suppressed=[];real=QTimer.singleShot
    def scheduled(*args):
        if getattr(args[-1],'__name__','')=='start_check':suppressed.append(True);return
        return real(*args)
    with monkeypatch.context() as m:
        m.setattr(QTimer,'singleShot',scheduled)
        context=ui.DuplicatePreflightContext(o.d.state,NS(incoming_paths=(),libraries=()),lambda:True,lambda p:True,registered)
        parent=ui.DuplicatePreflightDialog(context,purpose=purpose)
    assert suppressed==[True]
    scope.show(parent,size=(960,640));o.d.setParent(parent,o.d.windowFlags());o.d.show();parent._review_dialog=o.d
    finished=[];parent.finished.connect(finished.append)
    return NS(o=o,d=parent,registered=registered,finished=finished,scope=scope)


def cleanup(p):
    o=p.o;o.release.set();assert o.manager.shutdown(wait=True,timeout=2);QApplication.processEvents()
    p.d._review_dialog=None;custody.cleanup(o);p.d.close()


@pytest.fixture
def owner(qt_test_scope,monkeypatch):
    p=make(qt_test_scope,monkeypatch)
    try:yield p
    finally:cleanup(p)


def complete(p,route):
    if route=='done':p.d.done(QDialog.Accepted)
    elif route=='escape':QTest.keyClick(p.d,Qt.Key_Escape)
    else:getattr(p.d,route)()


def assert_retained(p):
    assert p.d.isVisible() and not p.d._closed and p.d._freshness_timer.isActive()
    assert not p.finished and p.registered._duplicate_preflight_dialog is p.d


@pytest.mark.parametrize('purpose',['preparation','gallery'])
@pytest.mark.parametrize('kind',['mark','recycle'])
@pytest.mark.parametrize('route',['reject','accept','done','close','escape'])
def test_pending_nested_write_refuses_every_outer_result(qt_test_scope,monkeypatch,purpose,kind,route):
    p=make(qt_test_scope,monkeypatch,purpose);o=p.o
    try:
        task=custody.start(o,kind);p.d._pending_action='generated-no-replay'
        complete(p,route);assert_retained(p);assert p.d._pending_action=='generated-no-replay'
        assert not o.finished and task._observer and not task._cancelled and o.d.isVisible()
        custody.finish(o);assert_retained(p);assert o.effects==[kind] and o.attempts==[kind]
        if kind=='mark':assert o.saved==[o.report]
        else:assert o.changed==[[str(o.second.path)]] and 'Confirmed moved: 1' in o.d.search_status.text()
        complete(p,route);assert p.d._closed and not p.d._freshness_timer.isActive() and p.registered._duplicate_preflight_dialog is None
        expected=QDialog.Accepted if route in {'done','accept'} else QDialog.Rejected
        assert p.finished==[expected] and len(o.finished)==1 and not p.d._pending_action
        p.d.done(QDialog.Rejected);assert p.finished==[expected] and o.attempts==[kind]
    finally:cleanup(p)


@pytest.mark.parametrize('location',['parent','review'])
@pytest.mark.parametrize('route',['reject','accept','done','close','escape'])
def test_readonly_observer_still_detaches_on_deliberate_outer_completion(owner,location,route):
    p=owner;o=p.o
    def work(job):o.entered.set();assert o.release.wait(10);return o.report
    task=CoordinatedCallTask(o.manager,'Generated read-only outer control',work,resource='cpu')
    if location=='parent':p.d._task=task;p.d._task_phase='scope'
    else:o.d._task=task
    task.start();wait_for(o.entered.is_set);complete(p,route)
    assert len(p.finished)==1 and p.d._closed and not task._observer and task._cancelled
    assert p.registered._duplicate_preflight_dialog is None
    o.release.set();assert o.manager.shutdown(wait=True,timeout=2)


def test_completion_does_not_clear_a_replacement_registered_owner(owner):
    p=owner;replacement=object();p.registered._duplicate_preflight_dialog=replacement;p.d.reject()
    assert p.finished==[QDialog.Rejected] and p.registered._duplicate_preflight_dialog is replacement


@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('kind',['mark','recycle'])
def test_visible_outer_wait_preserves_nested_result(owner,theme,kind):
    from qt_app.theme import apply_theme
    p=owner;o=p.o;apply_theme(p.scope.app,theme,False);custody.start(o,kind);QTest.qWait(1100)
    p.d.reject();assert_retained(p);wait_for(lambda:'Wait for Duplicate Review, then retry this action.' in p.d._window_status_footer.context_summary.completeText());capture(p.d,theme+'-preflight-'+kind,'01_owner_waiting');capture(o.d,theme+'-preflight-'+kind,'02_nested_waiting')
    custody.finish(o);assert_retained(p);assert o.effects==[kind];capture(o.d,theme+'-preflight-'+kind,'03_known_result')
    p.d.close();assert p.finished==[QDialog.Rejected] and o.attempts==[kind]
