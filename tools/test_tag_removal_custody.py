"""Actual removal dialog with generated memory operations;no Tag/image/history IO."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from threading import Event
import os
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from shiboken6 import isValid
from qt_app.image_tag_removal_dialog import ImageTagRemovalDialog
from qt_app.modal_feedback import FeedbackEnabledDialog
from qt_app.theme import apply_theme
from core.background_jobs import BackgroundJobManager
from test_tag_batch_custody import Manager
from duplicate_custody_test_support import qt_test_scope,wait_for,capture


@contextmanager
def actor(scope):
    manager=Manager();calls=[];history=[];reviews=[];image=Path('generated-unread-removal.png')
    item=NS(image=image,status='will_remove',values=('Generated Tag',),error='')
    preview=NS(targets=(NS(display='Generated Tag',stable_id='generated-tag'),),items=(item,),ready=True,cancelled=False,will_remove=1,already_absent=0,unavailable=0,failed=0)
    saved=NS(image=image,status='changed',removed_values=('Generated Tag',),error='')
    result=NS(operation_id='generated-operation',history_operation_id='generated-history',items=(saved,),changed=1,already_absent=0,failed=0,not_processed=0,cancelled=False,history_interrupted=False,complete=True)
    state=NS(result=result,error=None)
    def check(*a,**kw):calls.append('preview');return preview
    def apply(*a,**kw):
        calls.append('apply');assert kw['history_operation'].operation_id=='generated-history'
        if state.error:raise state.error
        return state.result
    def prepare(images):history.append(tuple(images));return NS(operation_id='generated-history')
    d=ImageTagRemovalDialog(NS(preview=check,apply=apply),(image,),('generated-tag',),manager=manager,history_operation_factory=prepare,history_opener=lambda images,identity:reviews.append((images,identity)))
    results=[];notifications=[];trace=[]
    d.finished.connect(lambda result:(results.append(result),trace.append(('result',result))))
    d.background_work_finished.connect(lambda:(notifications.append(True),trace.append(('background',True))))
    scope.show(d,size=(960,700));wait_for(lambda:len(manager.submissions)==1);a=d._active_attempt;manager.finish();a.poll();scope.settle(2)
    assert d.preview is preview and not d._work_pending();calls.clear();notifications.clear();trace.clear()
    try:yield NS(d=d,manager=manager,calls=calls,history=history,reviews=reviews,preview=preview,state=state,results=results,notifications=notifications,trace=trace)
    finally:
        if isValid(d):
            for a in d._attempts:a.timer.stop()
            d._task=d._active_attempt=d._return_hold=d._consuming_attempt=None;d._background_return_pending=False;d._background_notification_needed=False
            if d.isVisible():d.reject();scope.settle(2)


def invoke(d,route):
    if route=='accept':d.accept()
    elif route=='reject':d.reject()
    elif route=='done_accept':d.done(QDialog.Accepted)
    elif route=='done_reject':d.done(QDialog.Rejected)
    elif route=='close':d.close()
    else:QTest.keyClick(d,Qt.Key_Escape)


@pytest.mark.parametrize('route',['accept','reject','done_accept','done_reject','close','escape'])
def test_completion_cancel_keeps_original_facts_and_notification_until_drain(qt_test_scope,route):
    with actor(qt_test_scope) as o:
        d=o.d;d._apply();a=d._active_attempt;invoke(d,route)
        assert d.isVisible() and not o.results and not o.notifications and d._task is a.task and o.manager.cancels
        row=o.manager.finish(terminal='cancelled',drained=False);a.poll();assert a.receipt['value'] is o.state.result and d.result is None
        assert not o.results and not o.notifications and d._task is a.task
        row.scope.drained=True;a.poll();assert d.result is o.state.result and d.changed_paths()==(Path('generated-unread-removal.png'),)
        assert not d.completed and not o.results and len(o.notifications)==1
        d.reject();a.poll();assert o.results==[QDialog.Rejected] and len(o.notifications)==1 and o.calls==['apply']


@pytest.mark.parametrize('terminal',['complete','cancelled','failed'])
def test_completed_raw_return_delivers_result_before_one_notification(qt_test_scope,terminal):
    with actor(qt_test_scope) as o:
        o.d._apply();a=o.d._active_attempt;row=o.manager.finish(terminal=terminal,drained=False);a.poll()
        assert a.receipt['value'] is o.state.result and o.d.result is None and not o.trace
        row.scope.drained=True;a.poll();assert o.d.result is o.state.result and o.d.completed
        assert o.d.details.topLevelItem(0).text(1)=='Removed'
        assert o.trace==[('result',QDialog.Accepted),('background',True)] and o.calls==['apply']
        a.poll();assert o.trace==[('result',QDialog.Accepted),('background',True)]


def test_partial_saved_facts_do_not_accept_global_tag_removal(qt_test_scope):
    with actor(qt_test_scope) as o:
        o.state.result.complete=False;o.d._apply();a=o.d._active_attempt;o.manager.finish();a.poll()
        assert o.d.result is o.state.result and not o.d.completed and not o.results and o.d.isVisible()
        assert o.d.changed_paths()==(Path('generated-unread-removal.png'),) and o.notifications==[True]
        o.d.reject();assert o.results==[QDialog.Rejected] and o.notifications==[True]


@pytest.mark.parametrize('case',['write-error','no-return','presentation-error'])
def test_unknown_or_undisplayed_return_stays_owned_without_retry(qt_test_scope,case):
    with actor(qt_test_scope) as o:
        if case=='write-error':o.state.error=RuntimeError('Generated interrupted acknowledgement')
        if case=='presentation-error':o.state.result=object()
        o.d._apply();a=o.d._active_attempt;o.manager.finish(terminal='cancelled',execute=case!='no-return');qt_test_scope.settle(2);a.poll()
        assert o.d._return_hold is a and o.d.has_active_background_work() and o.d.isVisible()
        assert o.d.heading.text()=='Original image operation needs review' and not o.d.cancel_button.isEnabled()
        o.d._open_history();assert o.reviews==[((Path('generated-unread-removal.png'),),'generated-history')] and not o.results
        count=len(o.manager.submissions);o.d._apply();o.d.reject();assert len(o.manager.submissions)==count and not o.results and not o.notifications


@pytest.mark.parametrize('case',['reentry','error','owner-change'])
def test_history_preparation_cannot_submit_successor(qt_test_scope,case):
    with actor(qt_test_scope) as o:
        def prepare(images):
            o.history.append(tuple(images))
            if case=='error':raise RuntimeError('Generated History factory failed')
            if case=='reentry':o.d._apply()
            if case=='owner-change':o.d.service=object()
            return NS(operation_id='generated-history')
        o.d._history_operation_factory=prepare;count=len(o.manager.submissions);o.d._apply()
        assert len(o.history)==1 and len(o.manager.submissions)==count and not o.calls
        assert (o.d._history_preparation_hold if case=='error' else o.d._return_hold) is not None


def test_missing_manager_refuses_before_history(qt_test_scope):
    with actor(qt_test_scope) as o:
        o.d.manager=None;o.d._apply();assert not o.history and not o.calls;o.d.close();assert o.results==[QDialog.Rejected]


def test_scope_error_refuses_before_history(qt_test_scope):
    with actor(qt_test_scope) as o:
        o.manager.scope_error=RuntimeError('Generated scope refusal');o.d._apply();assert not o.history and not o.calls


@pytest.mark.parametrize('case',['unconfirmed','throw-after-register'])
def test_uncertain_admission_retains_original_without_positive_retirement(qt_test_scope,case):
    with actor(qt_test_scope) as o:
        original=o.manager.submit_observed
        def submit(*a,**kw):
            job,observer=original(*a,**kw);job.admission_unconfirmed=True
            if case=='throw-after-register':raise RuntimeError('Generated uncertain submission')
            return job,observer
        o.manager.submit_observed=submit;o.d._apply();a=o.d._active_attempt;o.d.reject()
        assert a is not None and not a.scope.physically_drained() and o.d.has_active_background_work()
        assert not o.calls and not o.results and not o.notifications


def test_refused_base_return_cannot_publish_completion_notification(qt_test_scope,monkeypatch):
    with actor(qt_test_scope) as o:
        o.d._apply();a=o.d._active_attempt
        with monkeypatch.context() as m:
            m.setattr(FeedbackEnabledDialog,'done',lambda self,result:None)
            o.manager.finish();a.poll();assert o.d._background_return_pending and o.d.has_active_background_work()
            assert o.d.isVisible() and not o.results and not o.notifications
        o.d.accept();assert o.trace==[('result',QDialog.Accepted),('background',True)]


def test_original_python_memory_worker_keeps_cancelled_return_until_scope_drain(qt_test_scope):
    with actor(qt_test_scope) as o:
        m=BackgroundJobManager();entered=Event();release=Event()
        try:
            o.d.manager=m
            def work(*a,**kw):entered.set();assert release.wait(4);return o.state.result
            o.d.service.apply=work;o.d._apply();a=o.d._active_attempt;wait_for(entered.is_set);o.d.reject()
            assert not o.results and not o.notifications and o.d.has_active_background_work()
            release.set();wait_for(a.scope.physically_drained);a.poll();assert o.d.result is o.state.result and not o.d.completed
        finally:release.set();assert m.shutdown(wait=True,timeout=4)


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('state',['busy','held','complete','partial'])
def test_visible_generated_removal_custody(qt_test_scope,theme,state):
    apply_theme(qt_test_scope.app,theme,False)
    with actor(qt_test_scope) as o:
        captured=[]
        if state=='complete':
            original=o.d.accept
            def observe_return():
                assert o.d.isVisible() and o.d.completed
                assert o.d.details.topLevelItem(0).text(1)=='Removed'
                if os.environ.get('CG_DUPLICATE_CAPTURE_DIR'):capture(o.d,theme,state)
                captured.append(True);original()
            o.d.accept=observe_return
        if state=='partial':o.state.result.complete=False
        if state=='held':o.state.error=RuntimeError('Generated acknowledgement unavailable')
        o.d._apply();a=o.d._active_attempt
        if state!='busy':o.manager.finish();a.poll()
        if state=='complete':assert captured==[True] and o.results==[QDialog.Accepted]
        else:
            assert o.d.isVisible() and not o.results
            if state=='busy':assert 'Removing' in o.d.heading.text()
            if state=='held':assert not o.d.cancel_button.isEnabled()
            if os.environ.get('CG_DUPLICATE_CAPTURE_DIR'):capture(o.d,theme,state)
