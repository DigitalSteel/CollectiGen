"""Generated multi-family receipt handoff; no actual recycle or image operations."""
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog
from core.verified_transfer import TransferRecoveryRequired
from qt_app.workers import CoordinatedCallTask
import qt_app.duplicate_finder_dialog as ui
from test_duplicate_review_write_custody_ui import make,cleanup
from duplicate_custody_test_support import wait_for,capture,qt_test_scope


def setup(scope,monkeypatch,case):
    o=make(scope,monkeypatch,'preflight');o.calls=[];o.known=[];o.boundary=Event();o.boundary_release=Event();o.tasks=[];o.case=case
    def apply(plan,**kwargs):
        o.calls.append(plan.identity)
        if case in {'ambiguous','interrupted','recovery'} and plan.identity==o.second.identity:
            error={'ambiguous':OSError,'interrupted':InterruptedError,'recovery':TransferRecoveryRequired}[case]
            raise error('Generated uncertain second-family acknowledgement')
        o.known.append(plan.identity)
    o.d.state.recycle.apply_recycle=apply
    original=ui.CoordinatedCallTask
    def task_factory(manager,name,work,**kwargs):
        def held(job):
            update=job.update
            def report(current,total=0,message=''):
                update(current,total,message)
                if current==1 and case in {'cancel','authority','refresh','no_report','cancel_before_second'}:
                    o.boundary.set();assert o.boundary_release.wait(10)
            job.update=report
            try:
                result=work(job)
                if case=='cancel_after_final':o.boundary.set();assert o.boundary_release.wait(10)
                return result
            finally:job.update=update
        task=original(manager,name,held,**kwargs);o.tasks.append(task);return task
    monkeypatch.setattr(ui,'CoordinatedCallTask',task_factory)
    return o


def submit(o):o.d._apply_recycle(((o.first,NS(identity=o.first.identity)),(o.second,NS(identity=o.second.identity))),False)
def settle(o):o.boundary_release.set();wait_for(lambda:o.d._task is None);QApplication.processEvents()
def close(o):o.boundary_release.set();cleanup(o)


@pytest.mark.parametrize('case',['success','cancel','authority','ambiguous','interrupted','recovery','cancel_after_final','no_report'])
def test_multi_family_receipt_survives_terminal_state(qt_test_scope,monkeypatch,case):
    o=setup(qt_test_scope,monkeypatch,case)
    try:
        if case=='no_report':o.d.report=None
        submit(o)
        if case in {'cancel','authority','cancel_after_final','no_report'}:
            wait_for(o.boundary.is_set)
            if case in {'cancel','cancel_after_final'}:o.manager.cancel(o.tasks[0].job_id)
            if case=='authority':o.d.frozen_authority_currentness=lambda:False
        settle(o);r=o.d._recycle_outcome
        known=[x.identity for x in r.moved];unknown=[x.family.identity for x in r.unconfirmed];pending=[x.identity for x in r.unattempted]
        if case in {'cancel','authority'}:assert known==[o.first.identity] and not unknown and pending==[o.second.identity]
        elif case in {'ambiguous','interrupted','recovery'}:assert known==[o.first.identity] and unknown==[o.second.identity] and not pending
        else:assert known==[o.first.identity,o.second.identity] and not unknown and not pending
        assert o.changed==[[x.path for x in r.moved]] and not o.finished and o.d.isVisible()
        assert 'Confirmed moved: '+str(len(known)) in o.d.search_status.text()
        assert 'Unconfirmed: '+str(len(unknown)) in o.d.search_status.text()
        assert all('not moved' not in title for title,_ in o.warnings)
        for family in (*r.moved,*r.unattempted):assert family.path in o.d.search_status.toolTip()
        for item in r.unconfirmed:assert item.family.path in o.d.search_status.toolTip()
        if case=='success':assert not o.warnings
        assert r.recovery_required==(case=='recovery')
    finally:close(o)


@pytest.mark.parametrize('route',['close','escape','reject','done','cancel_work'])
def test_partial_boundary_keeps_custody_until_accounted(qt_test_scope,monkeypatch,route):
    o=setup(qt_test_scope,monkeypatch,'cancel')
    try:
        submit(o);wait_for(o.boundary.is_set);task=o.tasks[0]
        if route=='escape':QTest.keyClick(o.d,Qt.Key_Escape)
        elif route=='done':o.d.done(QDialog.Rejected)
        else:getattr(o.d,route)()
        assert o.d.isVisible() and not o.finished and task._observer and not task._cancelled
        o.manager.cancel(task.job_id);settle(o);assert len(o.d._recycle_outcome.moved)==1 and len(o.changed)==1
    finally:close(o)


@pytest.mark.parametrize('stage',['regroup','cache','badges','render','actions','result_text','result_details','settlement','notification'])
def test_refresh_failure_retains_and_notifies_known_receipts_once(qt_test_scope,monkeypatch,stage):
    o=setup(qt_test_scope,monkeypatch,'refresh')
    try:
        submit(o);wait_for(o.boundary.is_set)
        def failure(*a,**k):raise OSError('Generated '+stage+' refresh failure')
        if stage=='regroup':o.d.state.duplicates.regroup=failure
        elif stage=='cache':o.d.state.duplicates.clear_session_reports=failure
        elif stage=='badges':o.d.state.duplicates.record_badge_results=failure
        elif stage=='render':monkeypatch.setattr(o.d,'_render_groups',failure)
        elif stage=='actions':monkeypatch.setattr(o.d,'_update_actions',failure)
        elif stage=='result_text':
            original=o.d.search_status.setText
            monkeypatch.setattr(o.d.search_status,'setText',lambda text:failure() if text.startswith('Confirmed moved:') else original(text))
        elif stage=='result_details':monkeypatch.setattr(o.d.search_status,'setToolTip',failure)
        elif stage=='settlement':monkeypatch.setattr(o.d,'_finish_task',failure)
        else:monkeypatch.setattr(o.d,'changed',NS(emit=failure))
        settle(o);assert len(o.d._recycle_outcome.moved)==2
        assert o.changed==([] if stage=='notification' else [['generated-first.png','generated-second.png']])
        assert o.d._recycle_refresh_errors and o.d._recycle_outcome_notes and not o.d.resolve.isEnabled()
        if stage!='result_text':assert 'Confirmed moved: 2' in o.d.search_status.text()
        # A duplicate terminal signal cannot replay owner notification.
        o.tasks[0].signals.complete.emit(o.d._recycle_outcome);QApplication.processEvents();assert len(o.changed)==int(stage!='notification')
    finally:close(o)


def test_cancelled_before_worker_invocation_reports_all_unattempted(qt_test_scope,monkeypatch):
    o=setup(qt_test_scope,monkeypatch,'success');occupied,release=Event(),Event()
    blocker=CoordinatedCallTask(o.manager,'Generated occupied IO capacity',lambda job:(occupied.set(),release.wait(10)),resource='io');blocker.start()
    try:
        wait_for(occupied.is_set);submit(o);task=o.tasks[0];assert o.manager.get(task.job_id).state=='queued'
        o.manager.cancel(task.job_id);wait_for(lambda:o.d._task is None)
        assert not o.calls and not o.d._recycle_outcome.moved and not o.d._recycle_outcome.unconfirmed and len(o.d._recycle_outcome.unattempted)==2
    finally:release.set();close(o)


def test_started_missing_receipt_stays_unconfirmed(qt_test_scope,monkeypatch):
    o=setup(qt_test_scope,monkeypatch,'success')
    monkeypatch.setattr(ui,'apply_duplicate_recycle_batch',lambda *a,**k:None)
    try:
        submit(o);settle(o);assert len(o.d._recycle_outcome.unconfirmed)==2 and not o.d._recycle_outcome.unattempted and not o.changed
    finally:close(o)


def test_refresh_hold_clears_only_after_successful_fresh_render(qt_test_scope,monkeypatch):
    o=setup(qt_test_scope,monkeypatch,'success');o.d._recycle_refresh_errors=('Generated prior refresh failure',)
    original=o.d._render_groups
    try:
        def failure():raise OSError('Generated fresh rendering failure')
        monkeypatch.setattr(o.d,'_render_groups',failure)
        with pytest.raises(OSError):o.d._search_complete(o.report)
        assert o.d._recycle_refresh_errors
        monkeypatch.setattr(o.d,'_render_groups',original);o.d._search_complete(o.report)
        assert not o.d._recycle_refresh_errors
    finally:close(o)


def test_two_accepted_owners_do_not_reuse_another_batch_receipt(qt_test_scope,monkeypatch):
    first=setup(qt_test_scope,monkeypatch,'cancel_before_second')
    try:
        submit(first);wait_for(first.boundary.is_set)
        # Restore only this test's task factory before constructing the second;
        # both owners then use one stock IO coordinator with independent receipts.
        monkeypatch.setattr(ui,'CoordinatedCallTask',CoordinatedCallTask)
        second=setup(qt_test_scope,monkeypatch,'success');empty=second.manager
        assert empty.shutdown(wait=True,timeout=2)
        second.manager=first.manager;second.d.state.jobs=first.manager
        try:
            submit(second);assert first.tasks[0].job_id!=second.tasks[0].job_id
            assert first.manager.get(second.tasks[0].job_id).state=='queued' and not second.calls
            settle(first);settle(second)
            assert len(first.d._recycle_outcome.moved)==len(second.d._recycle_outcome.moved)==2
            assert first.calls==second.calls==['generated-first','generated-second'] and len(first.changed)==len(second.changed)==1
        finally:close(second)
    finally:close(first)


@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('case',['cancel','ambiguous','refresh'])
def test_visible_partial_accounting(qt_test_scope,monkeypatch,theme,case):
    from qt_app.theme import apply_theme
    o=setup(qt_test_scope,monkeypatch,case);group=theme+'-partial-'+case;apply_theme(o.scope.app,theme,False)
    try:
        submit(o)
        if case in {'cancel','refresh'}:
            wait_for(o.boundary.is_set);QTest.qWait(1100);capture(o.d,group,'01_pending_accounting')
            if case=='cancel':o.manager.cancel(o.tasks[0].job_id)
            else:o.d.state.duplicates.regroup=lambda *a:(_ for _ in ()).throw(OSError('Generated result refresh failure'))
        settle(o);assert o.d._recycle_outcome.moved and len(o.changed)==1;capture(o.d,group,'02_exact_outcome')
    finally:close(o)
