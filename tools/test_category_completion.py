"""Generated callback readiness only;no autosave or store IO or Learning role."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog,QWidget,QLabel,QVBoxLayout,QPushButton
from shiboken6 import isValid,delete
from qt_app.category_workspace import CategoryDetailsDialog
from duplicate_custody_test_support import qt_test_scope,capture
ROUTES=['accept','reject','done_accept','done_reject','close','escape']

@contextmanager
def actor(scope):
    state=SimpleNamespace(ready=False,calls=0)
    def can_close():state.calls+=1;return state.ready
    d=CategoryDetailsDialog(can_close);results=[];d.finished.connect(results.append)
    scope.show(d,size=(500,200))
    try:yield SimpleNamespace(d=d,state=state,results=results,callback=can_close)
    finally:
        if isValid(d):
            state.ready=True;d._can_close=can_close;d.close();scope.settle(2)


def invoke(d,route):
    if route=='accept':d.accept()
    elif route=='reject':d.reject()
    elif route=='done_accept':d.done(QDialog.Accepted)
    elif route=='done_reject':d.done(QDialog.Rejected)
    elif route=='close':d.close()
    else:QTest.keyClick(d,Qt.Key_Escape)


@pytest.mark.parametrize('route',ROUTES)
def test_every_route_waits_for_original_readiness_and_delivers_once(qt_test_scope,route):
    with actor(qt_test_scope) as o:
        invoke(o.d,route)
        assert o.d.isVisible() and o.state.calls==1 and not o.results
        o.state.ready=True;invoke(o.d,route)
        expected=QDialog.Accepted if route in {'accept','done_accept'} else QDialog.Rejected
        assert not o.d.isVisible() and o.results==[expected] and o.state.calls==2
        o.d.accept();o.d.reject();o.d.done(QDialog.Accepted)
        assert o.results==[expected] and o.state.calls==2


def test_callback_error_retains_owner_and_explicit_retry(qt_test_scope):
    with actor(qt_test_scope) as o:
        def fail():raise RuntimeError('Synthetic readiness unavailable')
        o.d._can_close=fail
        with pytest.raises(RuntimeError,match='readiness'):o.d.done(QDialog.Rejected)
        assert o.d.isVisible() and not o.results and not o.d._completion_active
        o.d._can_close=o.callback;o.state.ready=True;o.d.reject()
        assert o.results==[QDialog.Rejected] and o.state.calls==1

@pytest.mark.parametrize('change',['callback','parent','destroyed','reentrant'])
def test_original_callback_and_owner_survive_decision(qt_test_scope,change):
    with actor(qt_test_scope) as o:
        d=o.d;parent=QWidget()
        def callback():
            o.state.calls+=1
            if change=='callback':d._can_close=lambda:True
            elif change=='parent':d.setParent(parent)
            elif change=='destroyed':delete(d)
            else:d.accept();d.reject();d.done(QDialog.Rejected)
            return True
        d._can_close=callback;d.done(QDialog.Accepted)
        assert o.results==([QDialog.Accepted] if change=='reentrant' else [])
        assert o.state.calls==1
        if isValid(d) and change=='parent':d.setParent(None)

@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('route',['done_accept','close'])
def test_visible_callback_readiness_wait_and_retry(qt_test_scope,theme,route):
    from qt_app.theme import apply_theme
    apply_theme(qt_test_scope.app,theme,False)
    with actor(qt_test_scope) as o:
        o.d.setWindowTitle('Category Details — generated readiness component')
        layout=QVBoxLayout(o.d);label=QLabel('Generated callback-only check: save readiness is pending.');label.setWordWrap(True);layout.addWidget(label)
        button=QPushButton('Close');button.clicked.connect(o.d.reject);layout.addWidget(button)
        group=theme+'-'+route
        invoke(o.d,route);assert o.d.isVisible() and not o.results
        QTest.qWait(200);capture(o.d,group,'01_pending_readiness')
        o.state.ready=True;label.setText('Generated callback is ready. Retry Close deliberately; nothing is saved by this fixture.')
        capture(o.d,group,'02_ready_for_deliberate_completion')
        invoke(o.d,route);assert len(o.results)==1 and not o.d.isVisible()

def test_close_event_error_is_not_admitted(qt_test_scope):
    from PySide6.QtGui import QCloseEvent
    with actor(qt_test_scope) as o:
        def fail():raise RuntimeError('Generated close readiness failure')
        o.d._can_close=fail;event=QCloseEvent()
        with pytest.raises(RuntimeError,match='close readiness'):
            o.d.closeEvent(event)
        assert not event.isAccepted()
        assert o.d.isVisible() and not o.results and not o.d._completion_active
