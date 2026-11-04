"""Generated in-memory Tag-batch custody; no actual image/history operations."""
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
from qt_app.image_tag_batch_dialog import ImageTagBatchDialog
from qt_app.theme import apply_theme
from core.background_jobs import BackgroundJobManager
from duplicate_custody_test_support import qt_test_scope,wait_for,capture

class Scope:
    """A controlled test seam, never evidence of physical/native retirement."""
    def __init__(self):self.drained = False;self.paused = False
    def pause_admission(self):self.paused = True
    def physically_drained(self):return self.drained


class Manager:
    def __init__(self):self.scopes = [];self.submissions = [];self.cancels = [];self.scope_error = None
    def create_execution_scope(self):
        if self.scope_error is not None:raise self.scope_error
        scope = Scope();self.scopes.append(scope);return scope
    def submit_observed(self, name, worker, listener, *, progress_listener, execution_scope, **kwargs):
        job = NS(id="synthetic-job-" + str(len(self.submissions)), state="queued",
            cancel_requested=False, result=None, error="", admission_unconfirmed=False,
            current=0, total=0, message="")
        def update(current, total, message):
            job.current, job.total, job.message = current, total, message
            progress_listener(job)
        job.update = update
        self.submissions.append(NS(job=job, worker=worker, listener=listener, scope=execution_scope))
        return job, "synthetic-observer"
    def cancel(self, job_id):
        self.cancels.append(job_id)
        next(row.job for row in self.submissions if row.job.id == job_id).cancel_requested = True
    def finish(self, index=-1, *, terminal="complete", drained=True, execute=True):
        row = self.submissions[index]
        if execute:
            row.job.state = "running"
            try:row.job.result = row.worker(row.job)
            except BaseException as error:
                row.job.error = str(error)
                terminal = "failed"
        row.job.state = terminal
        row.scope.drained = drained
        row.listener(row.job)
        return row


@contextmanager
def actor(scope):
    manager=Manager();calls=[];history=[]
    image=Path('generated-unread-image.png')
    preview=NS(primary_tag_id='generated-tag',primary_display='Generated Tag',images=(),related=(),will_add=1,already_present=0,conflicts=0,unavailable_or_stale=0,failed=0,actionable=1,cancelled=False)
    item=NS(image=image,status='changed',error='',added_tags=('Generated Tag',),skipped_related_conflicts=(),version_id='generated-version')
    result=NS(operation_id='generated-operation',history_operation_id='generated-history',items=(item,),changed=1,already_present=0,conflicts_skipped=0,stale=0,failed=0,not_processed=0,cancelled=False,history_interrupted=False)
    state=NS(result=result,error=None)
    def check(*a,**kw):calls.append('preview');return preview
    def apply(*a,**kw):
        calls.append('apply');assert kw['history_operation'].operation_id=='generated-history'
        if state.error:raise state.error
        return state.result
    def prepare(images):history.append(tuple(images));return NS(operation_id='generated-history')
    d=ImageTagBatchDialog(NS(preview=check,apply=apply),(image,),'generated-tag',manager=manager,history_operation_factory=prepare)
    results=[];d.finished.connect(results.append);scope.show(d,size=(960,700));wait_for(lambda:len(manager.submissions)==1)
    manager.finish();d._active_attempt.poll();scope.settle(2);assert d.preview is preview and not d._work_pending();calls.clear()
    try:yield NS(d=d,manager=manager,calls=calls,history=history,preview=preview,state=state,results=results)
    finally:
        if isValid(d):
            for attempt in d._attempts:attempt.timer.stop()
            d._task=d._active_attempt=d._return_hold=d._consuming_attempt=None;d.close();scope.settle(2)

def invoke(d,route):
    if route=='accept':d.accept()
    elif route=='reject':d.reject()
    elif route=='done_accept':d.done(QDialog.Accepted)
    elif route=='done_reject':d.done(QDialog.Rejected)
    elif route=='close':d.close()
    else:QTest.keyClick(d,Qt.Key_Escape)

@pytest.mark.parametrize('route',['accept','reject','done_accept','done_reject','close','escape'])
def test_every_completion_retains_original_task_until_drain(qt_test_scope,route):
    with actor(qt_test_scope) as o:
        d=o.d;d._apply();a=d._active_attempt;invoke(d,route)
        assert d.isVisible() and not o.results and d._task is a.task and o.manager.cancels
        row=o.manager.finish(terminal='cancelled',drained=False);a.poll();invoke(d,route)
        assert a.receipt['value'] is o.state.result and d.result is None and d._task is a.task and not o.results
        row.scope.drained=True;a.poll();assert d.result is o.state.result and d.changed_paths()==(Path('generated-unread-image.png'),)
        d.reject();assert o.results==[QDialog.Rejected] and o.calls==['apply']

@pytest.mark.parametrize('terminal',['complete','cancelled','failed'])
def test_returned_facts_survive_terminal_kind(qt_test_scope,terminal):
    with actor(qt_test_scope) as o:
        o.d._apply();a=o.d._active_attempt;row=o.manager.finish(terminal=terminal,drained=False);a.poll()
        assert a.receipt['value'] is o.state.result and o.d.result is None
        row.scope.drained=True;a.poll();assert o.d.result is o.state.result and o.d._history_operation_id=='generated-history'
        a.poll();assert o.calls==['apply']

@pytest.mark.parametrize('case',['write-error','no-return','presentation-error'])
def test_missing_or_undisplayed_return_stays_owned(qt_test_scope,case):
    with actor(qt_test_scope) as o:
        if case=='write-error':o.state.error=RuntimeError('Generated interrupted acknowledgement')
        if case=='presentation-error':o.state.result=object()
        o.d._apply();a=o.d._active_attempt;o.manager.finish(terminal='cancelled',execute=case!='no-return')
        if case=='no-return':qt_test_scope.settle(2)  # Deliver the queued terminal notice before checking a no-return receipt.
        a.poll()
        assert o.d._return_hold is a and o.d.has_active_background_work() and o.d.isVisible()
        assert o.d.heading.text()=='Original image operation needs review' and 'repeat' in o.d.summary.text().lower()
        assert not o.d.cancel_button.isEnabled()
        count=len(o.manager.submissions);o.d._apply();o.d.reject();assert len(o.manager.submissions)==count and not o.results

@pytest.mark.parametrize('case',['reentry','error','owner-change'])
def test_history_preparation_retains_custody_without_submission(qt_test_scope,case):
    with actor(qt_test_scope) as o:
        def prepare(images):
            o.history.append(tuple(images))
            if case=='error':raise RuntimeError('Generated History factory failed')
            if case=='reentry':o.d._apply()
            if case=='owner-change':o.d.service=object()
            return NS(operation_id='generated-history')
        o.d.history_operation_factory=prepare;count=len(o.manager.submissions);o.d._apply()
        assert len(o.history)==1 and len(o.manager.submissions)==count and not o.calls
        assert (o.d._history_preparation_hold if case=='error' else o.d._return_hold) is not None
        if case=='error':o.d.reject();assert o.results==[QDialog.Rejected]

def test_missing_manager_refuses_before_history(qt_test_scope):
    with actor(qt_test_scope) as o:
        o.d.manager=None;o.d._apply();assert not o.history and not o.calls
        o.d.close();assert o.results==[QDialog.Rejected]

def test_scope_error_refuses_before_history(qt_test_scope):
    with actor(qt_test_scope) as o:
        o.manager.scope_error=RuntimeError('Generated scope refusal');o.d._apply()
        assert not o.history and not o.calls and o.d.isVisible()

def test_original_python_worker_return_waits_for_physical_scope(qt_test_scope):
    with actor(qt_test_scope) as o:
        m=BackgroundJobManager();returned=Event();release=Event();d=o.d
        try:
            d.manager=m
            def finish(progress,cancelled):returned.set();assert release.wait(4);return o.state.result
            d.service.apply=finish
            # Preserve the original service API while using a real Python worker.
            d.service.apply=lambda *a,**kw:finish(kw.get('progress'),kw.get('should_cancel'))
            d._apply();a=d._active_attempt;wait_for(returned.is_set);d.reject();assert not o.results and d.has_active_background_work()
            release.set();wait_for(a.scope.physically_drained);a.poll();assert d.result is o.state.result
        finally:release.set();assert m.shutdown(wait=True,timeout=4)

@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('state',['busy','held','result'])
def test_visible_generated_custody_states(qt_test_scope,theme,state):
    apply_theme(qt_test_scope.app,theme,False)
    with actor(qt_test_scope) as o:
        o.d._apply();a=o.d._active_attempt
        if state!='busy':
            if state=='held':o.state.error=RuntimeError('Generated acknowledgement unavailable')
            o.manager.finish();a.poll()
        assert o.d.isVisible() and not o.results
        if state=='busy':assert 'Saving' in o.d.heading.text()
        if state=='held':assert not o.d.cancel_button.isEnabled()
        if os.environ.get('CG_DUPLICATE_CAPTURE_DIR'):capture(o.d,theme,state)
