"""Generated metadata only; stock read tasks, inert writes and owner lifetime."""
from pathlib import Path
from threading import Event,get_ident
import importlib.util,json,os,sys,time
import pytest
from PySide6.QtCore import Qt,QTimer,QCoreApplication,QEvent
from PySide6.QtWidgets import QDialogButtonBox,QProxyStyle,QStyle
from PySide6.QtTest import QTest
from shiboken6 import isValid
from core import bug_report as reports
from qt_app import bug_report_dialog as m
from qt_app.theme import apply_theme
from duplicate_custody_test_support import qt_test_scope,capture

def wait(scope,predicate,timeout=5):
    end=time.monotonic()+timeout
    while not predicate() and time.monotonic()<end:scope.settle(10)
    assert predicate(),'Generated discovery did not settle within bound'

def fixture(root,kind='valid'):
    if kind=='empty':return root
    root.mkdir()
    if kind=='invalid':
        d=root/('CBR-'+'3'*16);d.mkdir();(d/'report.json').write_bytes(b'Generated malformed metadata');return root
    for n in [1,2]:
        p=reports.prepare_bug_report(reports.BugReportDraft('Generated saved report '+str(n),'Generated context','Generated desired result','Generated steps'),{},report_id='CBR-'+str(n)*16,created_at='2026-11-04T13:19:00+00:00')
        d=root/p.report_id;d.mkdir();(d/'report.json').write_bytes(reports._pretty_json(p.report));(d/'report.md').write_bytes(p.human_readable().encode());(d/'privacy-manifest.json').write_bytes(reports._pretty_json(p.privacy_manifest));(d/'tracking.json').write_bytes(reports._pretty_json({'export_history':[{'exported_at':'generated'}] if n==2 else []}));(d/'attachments').mkdir();(d/'attachments/generated-untouched.bin').write_bytes(b'Generated untouched payload')
    return root

def snapshot(root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}

@pytest.fixture(autouse=True)
def no_writes(monkeypatch):
    def forbidden(*a,**k):pytest.fail('No report/export/history/provider write permitted in discovery checks')
    for name in ['save_bug_report','export_feedback_archive','record_report_export']:monkeypatch.setattr(m,name,forbidden)

def show(scope,root):
    d=m.FeedbackArchiveDialog(root);scope.show(d,size=(1920,1080));return d

def close(scope,d,release=None,task=None):
    if isValid(d):d.close()
    if release:release.set()
    if task:wait(scope,task.is_finished)
    if isValid(d):d.deleteLater()
    scope.settle(10)

def test_published_constructor_reads_inline_before_review(qt_test_scope,tmp_path,monkeypatch):
    path=Path(os.environ['CG_REPORT_BASELINE_ROOT'])/'qt_app/bug_report_dialog.py';name='qt_app._discovery_original21';spec=importlib.util.spec_from_file_location(name,path);old=importlib.util.module_from_spec(spec);sys.modules[name]=old;spec.loader.exec_module(old)
    root=fixture(tmp_path/'generated');before=snapshot(root);gui=get_ident();observed=[];ticks=[];QTimer.singleShot(0,lambda:ticks.append(True))
    def discovery(directory):observed.append((get_ident(),bool(ticks)));return reports.discover_feedback_reports(directory)
    monkeypatch.setattr(old,'discover_feedback_reports',discovery);d=old.FeedbackArchiveDialog(root)
    try:assert observed==[(gui,False)] and d.report_list.count()==2 and snapshot(root)==before
    finally:close(qt_test_scope,d)

class NoMotion(QProxyStyle):
    def styleHint(self,hint,option=None,widget=None,returnData=None):
        if hint==QStyle.SH_Widget_Animate:return 0
        return super().styleHint(hint,option,widget,returnData)

@pytest.mark.parametrize('theme',['light','dark','light-reduced'])
def test_stock_read_task_keeps_review_responsive(qt_test_scope,tmp_path,monkeypatch,theme):
    apply_theme(qt_test_scope.app,theme.split('-')[0],False)
    if theme.endswith('reduced'):qt_test_scope.app.setStyle(NoMotion('Fusion'))
    root=fixture(tmp_path/'generated');before=snapshot(root);release=Event();started=Event();threads=[];gui=get_ident();feedback=[];ticks=[]
    def discovery(directory):threads.append(get_ident());started.set();assert release.wait(5);return reports.discover_feedback_reports(directory)
    monkeypatch.setattr(m,'discover_feedback_reports',discovery);monkeypatch.setattr(m.FeedbackEnabledDialog,'_request_feedback',lambda self:feedback.append(True));d=show(qt_test_scope,root);task=d._discovery_task
    try:
        wait(qt_test_scope,started.is_set);task=d._discovery_task;assert threads==[threads[0]] and threads[0]!=gui
        assert not d._discovery_loaded and not d.export_button.isEnabled() and d.discovery_activity.active
        assert 'Total not yet known' in d.discovery_phase.text() and 'No saved' not in d.discovery_note.text()
        QTimer.singleShot(0,lambda:ticks.append(True));wait(qt_test_scope,lambda:bool(ticks));wait(qt_test_scope,lambda:d._feedback_button is not None)
        QTest.mouseClick(d._feedback_button,Qt.LeftButton);assert feedback==[True]
        chosen=str(tmp_path/'chosen.zip');d.destination.setText(chosen)
        if theme.endswith('reduced'):assert not d.discovery_activity._timer.isActive()
        capture(d,theme,'loading');release.set();wait(qt_test_scope,lambda:d._discovery_loaded)
        assert d.report_list.count()==2 and [d.report_list.item(i).checkState() for i in range(2)]==[Qt.Checked,Qt.Unchecked]
        assert d.destination.text()==chosen and d.export_button.isEnabled() and not d.discovery_activity.active and snapshot(root)==before and not Path(chosen).exists()
        capture(d,theme,'loaded')
    finally:
        close(qt_test_scope,d,release,task)
        if theme.endswith('reduced'):qt_test_scope.app.setStyle('Fusion')

@pytest.mark.parametrize('kind',['empty','invalid'])
def test_completed_empty_and_invalid_are_not_loading(qt_test_scope,tmp_path,kind):
    apply_theme(qt_test_scope.app,'light',False);root=fixture(tmp_path/'generated',kind);before=snapshot(root);d=show(qt_test_scope,root)
    try:
        wait(qt_test_scope,lambda:d._discovery_loaded);assert not d.export_button.isEnabled() and not d.discovery_activity.active and d.report_list.count()==0
        assert d.invalid_note.isVisible()==(kind=='invalid') and not d.retry_button.isVisible() and snapshot(root)==before
        if kind=='empty':assert not root.exists() and 'No saved feedback' in d.discovery_note.text()
        else:assert 'No reports are available' in d.discovery_note.text()
        capture(d,kind,'completed')
    finally:close(qt_test_scope,d)

@pytest.mark.parametrize('theme',['light','dark'])
def test_unavailable_then_explicit_retry_preserves_destination(qt_test_scope,tmp_path,monkeypatch,theme):
    apply_theme(qt_test_scope.app,theme,False);root=fixture(tmp_path/'generated');before=snapshot(root);calls=[]
    def discovery(directory):
        calls.append(directory)
        if len(calls)==1:raise OSError('Generated metadata access refusal')
        return reports.discover_feedback_reports(directory)
    monkeypatch.setattr(m,'discover_feedback_reports',discovery);d=show(qt_test_scope,root);chosen=str(tmp_path/'chosen.zip');d.destination.setText(chosen)
    try:
        wait(qt_test_scope,lambda:d.retry_button.isVisible());assert len(calls)==1 and not d._discovery_loaded and not d.export_button.isEnabled() and 'No saved' not in d.discovery_note.text();capture(d,theme,'unavailable')
        QTest.mouseClick(d.retry_button,Qt.LeftButton);wait(qt_test_scope,lambda:d._discovery_loaded);assert len(calls)==2 and d.destination.text()==chosen and d.report_list.count()==2 and snapshot(root)==before;capture(d,theme,'retried')
    finally:close(qt_test_scope,d)

@pytest.mark.parametrize('failure',[False,True])
@pytest.mark.parametrize('destroy',[False,True])
def test_running_close_ignores_late_result_and_failure(qt_test_scope,tmp_path,monkeypatch,failure,destroy):
    root=fixture(tmp_path/'generated');before=snapshot(root);started=Event();release=Event()
    def discovery(directory):
        started.set();assert release.wait(5)
        if failure:raise ValueError('Generated late metadata failure')
        return reports.discover_feedback_reports(directory)
    monkeypatch.setattr(m,'discover_feedback_reports',discovery);d=show(qt_test_scope,root);wait(qt_test_scope,started.is_set);task=d._discovery_task
    try:
        QTest.mouseClick(d.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel),Qt.LeftButton);assert d._discovery_closed and task.is_cancelled() and d._discovery_task is None
        if destroy:d.deleteLater();QCoreApplication.sendPostedEvents(d,QEvent.DeferredDelete);qt_test_scope.settle(10);assert not isValid(d)
        release.set();wait(qt_test_scope,task.is_finished);task.signals.complete.emit(((),('Generated stale result',)));task.signals.failed.emit('Generated stale error');qt_test_scope.settle(20)
        assert not d._discovery_loaded and d._reports==() and snapshot(root)==before
    finally:close(qt_test_scope,d,release,task)

def test_queued_close_discards_without_running_read(qt_test_scope,tmp_path,monkeypatch):
    root=fixture(tmp_path/'generated');before=snapshot(root);queued=[];calls=[];monkeypatch.setattr(m,'start_qt_delivery_task',lambda pool,task:queued.append(task));monkeypatch.setattr(m,'discover_feedback_reports',lambda path:calls.append(path))
    d=show(qt_test_scope,root)
    try:
        wait(qt_test_scope,lambda:bool(queued));d.close();queued[0].run();assert queued[0].is_finished() and not calls and not d._discovery_loaded and snapshot(root)==before
    finally:close(qt_test_scope,d)

@pytest.mark.parametrize('after',[False,True])
def test_dispatch_uncertainty_retry_rejects_replaced_signals(qt_test_scope,tmp_path,monkeypatch,after):
    root=fixture(tmp_path/'generated');before=snapshot(root);real=m.start_qt_delivery_task;attempts=[];old_started=Event();old_release=Event();new_started=Event();new_release=Event();reads=[]
    def discovery(directory):
        reads.append(directory)
        if after and len(reads)==1:old_started.set();assert old_release.wait(5)
        else:new_started.set();assert new_release.wait(5)
        return reports.discover_feedback_reports(directory)
    def dispatch(pool,task):
        attempts.append(task)
        if len(attempts)==1:
            if after:real(pool,task);assert old_started.wait(2)
            raise RuntimeError('Generated ambiguous dispatch')
        real(pool,task)
    monkeypatch.setattr(m,'discover_feedback_reports',discovery);monkeypatch.setattr(m,'start_qt_delivery_task',dispatch);d=show(qt_test_scope,root)
    try:
        wait(qt_test_scope,lambda:d.retry_button.isVisible());old=attempts[0];assert old.is_cancelled() and not d._discovery_loaded
        QTest.mouseClick(d.retry_button,Qt.LeftButton);wait(qt_test_scope,new_started.is_set);old.signals.complete.emit(((),('Generated stale',)));old.signals.failed.emit('Generated stale');qt_test_scope.settle(20);assert not d._discovery_loaded and d._discovery_outcome is None
        new_release.set();wait(qt_test_scope,lambda:d._discovery_loaded);assert d.report_list.count()==2 and len(attempts)==2 and len(reads)==(2 if after else 1) and snapshot(root)==before
    finally:
        old_release.set();new_release.set()
        if attempts and not after:attempts[0].run()
        for task in attempts:wait(qt_test_scope,task.is_finished)
        close(qt_test_scope,d)

@pytest.mark.parametrize('success',[False,True])
def test_signal_receipt_is_not_task_finished(qt_test_scope,tmp_path,monkeypatch,success):
    root=fixture(tmp_path/'generated');before=snapshot(root);queued=[];monkeypatch.setattr(m,'start_qt_delivery_task',lambda pool,task:queued.append(task));d=show(qt_test_scope,root)
    try:
        wait(qt_test_scope,lambda:bool(queued));task=queued[0]
        if success:task.signals.complete.emit(reports.discover_feedback_reports(root))
        else:task.signals.failed.emit('Generated metadata refusal')
        wait(qt_test_scope,lambda:d._discovery_outcome is not None);assert not d._discovery_loaded and not d.export_button.isEnabled() and not task.is_finished()
        task._finished.set();wait(qt_test_scope,lambda:d._discovery_loaded if success else d.retry_button.isVisible());assert d._discovery_loaded==success and snapshot(root)==before
    finally:
        if queued:queued[0]._finished.set()
        close(qt_test_scope,d)
