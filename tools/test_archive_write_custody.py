"""Generated physical ZIP/history; no actual/default reports or external actions."""
from pathlib import Path
from threading import Event,get_ident
from types import SimpleNamespace as NS
import ast,hashlib,importlib.util,json,os,sys,time,zipfile
import pytest
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QDialog,QMessageBox,QWidget
from PySide6.QtTest import QTest
from core import bug_report as reports
from qt_app import bug_report_dialog as m
from qt_app.theme import apply_theme
from duplicate_custody_test_support import qt_test_scope,capture
from test_archive_discovery_responsiveness import fixture,wait,NoMotion

def immutable(root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name!='tracking.json'}
def snapshot(root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def show(scope,tmp,theme='light'):
    apply_theme(scope.app,theme.split('-')[0],False)
    if theme.endswith('reduced'):scope.app.setStyle(NoMotion('Fusion'))
    root=fixture(tmp/'generated');before=immutable(root);d=m.FeedbackArchiveDialog(root);scope.show(d,size=(1920,1080));wait(scope,lambda:d._discovery_loaded);d.destination.setText(str(tmp/'reviewed.zip'));return d,root,before

def finish(scope,d):wait(scope,lambda:d._completion_delivered)
def cleanup(scope,d):
    if d._write_attempt is None:d.close();d.deleteLater()
    scope.settle(10)
    scope.app.setStyle('Fusion')

def assert_zip(d,root,before):
    assert d.archive_path and zipfile.is_zipfile(d.archive_path) and immutable(root)==before
    assert d._archive_result.sha256==sha(d.archive_path)
    with zipfile.ZipFile(d.archive_path) as z:
        assert z.testzip() is None and any(n.endswith('/report.json') for n in z.namelist())
        for n in z.namelist():
            if n.endswith(('/report.json','/report.md','/privacy-manifest.json')):assert z.read(n)==(root/n).read_bytes()

def parent_warning(scope,d,theme):
    # Original result carrier only, not a full MainWindow or real feedback root.
    path=Path(__file__).resolve().parents[1]/'qt_app/main_window.py';tree=ast.parse(path.read_bytes());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TagManagerWindow');method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='export_collected_feedback');parent=QWidget();messages=[]
    class Box(QMessageBox):
        def exec(self):
            messages.append(self.text());scope.show(self,size=(900,500));scope.settle(10);capture(self,theme,'known-archive-history-warning');QTest.mouseClick(self.button(QMessageBox.Close),Qt.LeftButton);return self.result()
    def forbidden(*a,**k):pytest.fail('No clipboard/folder/provider/upload authority')
    env=dict(__file__=str(path),Path=Path,FeedbackArchiveDialog=lambda path,owner:NS(exec=lambda:d.result(),archive_path=d.archive_path,tracking_warning=d.tracking_warning),QDialog=QDialog,QMessageBox=Box,QApplication=NS(clipboard=forbidden),QDesktopServices=NS(openUrl=forbidden),QUrl=NS(fromLocalFile=forbidden))
    try:
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(path),'exec'),env);env['export_collected_feedback'](parent)
        assert len(messages)==1 and str(d.archive_path) in messages[0] and 'The archive is valid' in messages[0] and d.tracking_warning in messages[0]
    finally:parent.close();parent.deleteLater();scope.settle(10)

def test_published_export_runs_inline_on_gui(qt_test_scope,tmp_path,monkeypatch):
    path=Path(os.environ['CG_REPORT_BASELINE_ROOT'])/'qt_app/bug_report_dialog.py';name='qt_app._write_original22';spec=importlib.util.spec_from_file_location(name,path);old=importlib.util.module_from_spec(spec);sys.modules[name]=old;spec.loader.exec_module(old)
    root=fixture(tmp_path/'generated');before=immutable(root);d=old.FeedbackArchiveDialog(root);qt_test_scope.show(d,size=(1000,760));wait(qt_test_scope,lambda:d._discovery_loaded);d.destination.setText(str(tmp_path/'baseline.zip'));gui=get_ident();ticks=[];observed=[];real=old.export_feedback_archive
    monkeypatch.setattr(old,'export_feedback_archive',lambda *a:(observed.append((get_ident(),bool(ticks))),real(*a))[1]);QTimer.singleShot(0,lambda:ticks.append(True))
    try:d._export();assert observed==[(gui,False)] and d.result()==QDialog.Accepted and immutable(root)==before
    finally:d.close();d.deleteLater();qt_test_scope.settle(10)

@pytest.mark.parametrize('theme',['light','dark','light-reduced'])
@pytest.mark.parametrize('phase',['archive','history'])
def test_physical_write_keeps_input_and_completion_custody(qt_test_scope,tmp_path,monkeypatch,theme,phase):
    d,root,before=show(qt_test_scope,tmp_path,theme);name='export_feedback_archive' if phase=='archive' else '_record_archive_exports';real=getattr(m,name);entered=Event();release=Event();calls=[];threads=[];feedback=[];gui=get_ident();finished=[]
    def held(*a):calls.append(a);threads.append(get_ident());entered.set();assert release.wait(10);return real(*a)
    monkeypatch.setattr(m,name,held);monkeypatch.setattr(m.FeedbackEnabledDialog,'_request_feedback',lambda self:feedback.append(True));d.finished.connect(finished.append)
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);wait(qt_test_scope,entered.is_set);attempt=d._write_attempt;chosen=d.destination.text();selection=d._selected();assert threads[0]!=gui
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));wait(qt_test_scope,lambda:bool(ticks));QTest.qWait(1100)
        assert d.write_activity.active and int(d.write_phase.text().split('Elapsed ')[1].split('s')[0])>=1 and 'Total not yet known' in d.write_phase.text()
        assert not d.destination.isEnabled() and not d.report_list.isEnabled() and not d.export_button.isEnabled() and not d.browse_button.isEnabled()
        for action in [lambda:d.done(QDialog.Accepted),lambda:d.done(QDialog.Rejected),d.accept,d.reject,d._export]:action()
        QTest.keyClick(d,Qt.Key_Escape);assert not d.close() and d.isVisible() and not finished and not attempt.task.is_cancelled()
        d._set_all(Qt.Unchecked);assert d._selected()==selection and d.destination.text()==chosen
        wait(qt_test_scope,lambda:d._feedback_button is not None);QTest.mouseClick(d._feedback_button,Qt.LeftButton);assert feedback==[True]
        assert bool(d._archive_result)==(phase=='history')
        if phase=='history':assert_zip(d,root,before)
        if theme.endswith('reduced'):assert not d.write_activity._timer.isActive()
        capture(d,theme+'-'+phase,'working');release.set();finish(qt_test_scope,d)
        assert finished==[QDialog.Accepted] and len(calls)==1 and not d.tracking_warning;assert_zip(d,root,before)
        d._export();d.reject();d.accept();assert finished==[QDialog.Accepted] and len(calls)==1
    finally:release.set();wait(qt_test_scope,lambda:d._write_attempt is None);cleanup(qt_test_scope,d)

@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('fault',[OSError,RuntimeError])
def test_known_zip_survives_optional_history_failure(qt_test_scope,tmp_path,monkeypatch,theme,fault):
    d,root,before=show(qt_test_scope,tmp_path,theme);QTest.mouseClick(d.select_all_button,Qt.LeftButton);real=m.record_report_export;calls=[];finished=[];d.finished.connect(finished.append)
    def record(directory,**kw):
        calls.append(directory)
        if len(calls)==1:raise fault('Generated optional history failure')
        return real(directory,**kw)
    monkeypatch.setattr(m,'record_report_export',record)
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);finish(qt_test_scope,d);assert_zip(d,root,before)
        assert len(calls)==2 and finished==[QDialog.Accepted] and 'Generated optional history failure' in d.tracking_warning
        rows=[json.loads((p/'tracking.json').read_bytes()) for p in sorted(root.iterdir())];assert 'export_history' not in rows[0] or rows[0]['export_history']==[]
        assert len(rows[1]['export_history'])==2
        if fault is RuntimeError:parent_warning(qt_test_scope,d,theme)
    finally:cleanup(qt_test_scope,d)

@pytest.mark.parametrize('phase',['archive','history'])
@pytest.mark.parametrize('complete_first',[False,True])
def test_dispatch_error_after_effect_entry_retains_result(qt_test_scope,tmp_path,monkeypatch,phase,complete_first):
    d,root,before=show(qt_test_scope,tmp_path);real_start=m.start_qt_delivery_task;name='export_feedback_archive' if phase=='archive' else '_record_archive_exports';real=getattr(m,name);entered=Event();release=Event();calls=[];attempts=[]
    def effect(*a):
        calls.append(a);entered.set()
        if not complete_first:assert release.wait(10)
        return real(*a)
    def dispatch(pool,task):
        attempts.append(task);real_start(pool,task)
        if len(attempts)==(1 if phase=='archive' else 2):
            assert entered.wait(3)
            if complete_first:assert task.wait(3)
            raise RuntimeError('Generated post-entry dispatch error')
    monkeypatch.setattr(m,name,effect);monkeypatch.setattr(m,'start_qt_delivery_task',dispatch)
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);wait(qt_test_scope,entered.is_set)
        if not complete_first:assert d._write_attempt is not None and not d.close() and not attempts[-1].is_cancelled()
        release.set();finish(qt_test_scope,d);assert_zip(d,root,before);assert len(calls)==1 and not d.tracking_warning
    finally:release.set();wait(qt_test_scope,lambda:d._write_attempt is None);cleanup(qt_test_scope,d)

def test_denied_dispatch_fences_late_queued_write(qt_test_scope,tmp_path,monkeypatch):
    d,root,before=show(qt_test_scope,tmp_path);queued=[];writes=[]
    def dispatch(pool,task):queued.append(task);raise RuntimeError('Generated pre-entry dispatch refusal')
    monkeypatch.setattr(m,'start_qt_delivery_task',dispatch);monkeypatch.setattr(m,'export_feedback_archive',lambda *a:writes.append(a));chosen=Path(d.destination.text());original=snapshot(root)
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);assert d._write_attempt is None and 'did not start' in d.status.text() and d.export_button.isEnabled();capture(d,'denied','no-effects')
        queued[0].run();qt_test_scope.settle(20);assert not writes and not chosen.exists() and snapshot(root)==original and d.archive_path is None
        assert d.close()
    finally:cleanup(qt_test_scope,d)

def test_history_dispatch_denial_keeps_verified_zip(qt_test_scope,tmp_path,monkeypatch):
    d,root,before=show(qt_test_scope,tmp_path);real=m.start_qt_delivery_task;tasks=[];history=[]
    def dispatch(pool,task):
        tasks.append(task)
        if len(tasks)==2:raise RuntimeError('Generated history admission refusal')
        real(pool,task)
    monkeypatch.setattr(m,'start_qt_delivery_task',dispatch);monkeypatch.setattr(m,'_record_archive_exports',lambda *a:history.append(a))
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);finish(qt_test_scope,d);assert_zip(d,root,before);assert 'history admission refusal' in d.tracking_warning
        tasks[1].run();qt_test_scope.settle(20);assert not history and d.result()==QDialog.Accepted
    finally:cleanup(qt_test_scope,d)

def test_accepted_queue_and_early_signals_do_not_release_close(qt_test_scope,tmp_path,monkeypatch):
    d,root,before=show(qt_test_scope,tmp_path);tasks=[];monkeypatch.setattr(m,'start_qt_delivery_task',lambda pool,task:tasks.append(task))
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);tasks[0].signals.complete.emit('Generated stale value');tasks[0].signals.failed.emit('Generated stale error');qt_test_scope.settle(20)
        assert not d.close() and d._archive_result is None and not tasks[0].is_finished() and not tasks[0].is_cancelled()
        tasks[0].run();wait(qt_test_scope,lambda:len(tasks)==2);assert_zip(d,root,before);assert not d.close() and not tasks[1].is_cancelled()
        tasks[1].run();finish(qt_test_scope,d);assert d.result()==QDialog.Accepted
    finally:
        for task in tasks:
            if not task.is_finished():task.run()
        wait(qt_test_scope,lambda:d._write_attempt is None);cleanup(qt_test_scope,d)

@pytest.mark.parametrize('transport',['missing','failed'])
def test_retained_result_survives_signal_transport(qt_test_scope,tmp_path,monkeypatch,transport):
    d,root,before=show(qt_test_scope,tmp_path);tasks=[];monkeypatch.setattr(m,'start_qt_delivery_task',lambda pool,task:tasks.append(task))
    def delivery(signal,*a):
        if transport=='failed':raise RuntimeError('Generated signal transport failure')
    monkeypatch.setattr(m.AsyncCallTask,'_emit',staticmethod(delivery))
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton)
        for index in [0,1]:
            wait(qt_test_scope,lambda:len(tasks)>index)
            if transport=='failed':
                with pytest.raises(RuntimeError,match='signal transport'):tasks[index].run()
            else:tasks[index].run()
        finish(qt_test_scope,d);assert_zip(d,root,before);assert not d.tracking_warning
    finally:cleanup(qt_test_scope,d)

@pytest.mark.parametrize('theme',['light','dark'])
def test_unconfirmed_creation_retains_destination_and_prevents_replay(qt_test_scope,tmp_path,monkeypatch,theme):
    d,root,before=show(qt_test_scope,tmp_path,theme);real=m.export_feedback_archive;calls=[]
    def uncertain(*a):calls.append(a);real(*a);raise RuntimeError('Generated missing terminal receipt')
    monkeypatch.setattr(m,'export_feedback_archive',uncertain);chosen=Path(d.destination.text())
    try:
        QTest.mouseClick(d.export_button,Qt.LeftButton);wait(qt_test_scope,lambda:d._write_uncertain)
        assert chosen.is_file() and zipfile.is_zipfile(chosen) and d.archive_path is None and not d.export_button.isEnabled()
        assert 'does not prove that no file exists' in d.status.text();capture(d,theme,'unconfirmed-creation');d._export();d.accept();assert len(calls)==1 and not d._completion_delivered and immutable(root)==before
        assert d.close() and d.result()==QDialog.Rejected
    finally:cleanup(qt_test_scope,d)

def test_existing_destination_is_never_replaced(qt_test_scope,tmp_path):
    d,root,before=show(qt_test_scope,tmp_path);chosen=Path(d.destination.text());chosen.write_bytes(b'Generated existing output');original=snapshot(root)
    try:QTest.mouseClick(d.export_button,Qt.LeftButton);wait(qt_test_scope,lambda:d._write_uncertain);assert chosen.read_bytes()==b'Generated existing output' and snapshot(root)==original and not d.archive_path
    finally:cleanup(qt_test_scope,d)

class GeneratedAbort(BaseException):pass
@pytest.mark.parametrize('phase',['archive','history'])
def test_nonordinary_abort_is_retained_and_propagated(phase,monkeypatch):
    def abort(*a,**kw):raise GeneratedAbort('Generated nonordinary boundary')
    if phase=='history':
        monkeypatch.setattr(m,'record_report_export',abort);call=lambda:m._record_archive_exports((NS(directory=Path('generated'),report_id='generated'),),NS(archive_id='generated',sha256='generated'))
    else:call=abort
    attempt=m._ArchiveWriteAttempt(call)
    with pytest.raises(GeneratedAbort):attempt.run()
    assert attempt.snapshot()[1][0]=='unconfirmed' and not attempt.deny_before_entry('Generated late denial')
