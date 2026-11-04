"""Known generated archive custody; inert optional faults and sharing seams."""
from pathlib import Path
from types import SimpleNamespace as NS
import ast,hashlib,importlib.util,os,sys,zipfile
import pytest
from PySide6.QtWidgets import QDialog,QMessageBox,QWidget
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from core import bug_report as reports
from qt_app import bug_report_dialog as candidate
from qt_app.theme import apply_theme
from duplicate_custody_test_support import qt_test_scope,capture

def module(variant):
    if variant=='candidate':return candidate
    name='qt_app._archive_tracking_original20'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,Path(os.environ['CG_REPORT_BASELINE_ROOT'])/'qt_app/bug_report_dialog.py')
        m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
    return sys.modules[name]

def evidence(root):
    saved=[]
    for n in [1,2]:
        draft=reports.BugReportDraft('Generated archive report '+str(n),'Generated observation','Preserve verified result','Generated steps')
        value=reports.prepare_bug_report(draft,{},report_id='CBR-'+str(n)*16,created_at='2026-11-04T12:19:00+00:00')
        saved.append(reports.save_bug_report(value,root/'generated-feedback').directory)
    return saved

def immutable(root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name!='tracking.json'}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def arrange(scope,monkeypatch,tmp_path,variant,fault):
    m=module(variant);saved=evidence(tmp_path);d=m.FeedbackArchiveDialog(tmp_path/'generated-feedback');d.destination.setText(str(tmp_path/'reviewed.zip'));scope.show(d,size=(1920,1080));scope.settle(2)
    exports=[];notes=[];original_export=m.export_feedback_archive;original_note=m.record_report_export
    def export(*a,**k):
        result=original_export(*a,**k);exports.append((result,sha(result.path)));return result
    def note(path,**k):
        notes.append(Path(path))
        if fault is not None and len(notes)==1:raise fault('Generated optional history fault')
        return original_note(path,**k)
    monkeypatch.setattr(m,'export_feedback_archive',export);monkeypatch.setattr(m,'record_report_export',note)
    return NS(d=d,saved=saved,exports=exports,notes=notes,first_id=d._selected()[0].report_id,before=immutable(tmp_path/'generated-feedback'))

@pytest.mark.parametrize('variant',['baseline','candidate'])
@pytest.mark.parametrize('fault',[None,reports.BugReportError,OSError,RuntimeError,ValueError])
def test_known_archive_completion_and_original_preservation(qt_test_scope,monkeypatch,tmp_path,variant,fault):
    o=arrange(qt_test_scope,monkeypatch,tmp_path,variant,fault)
    try:
        escapes=variant=='baseline' and fault in (RuntimeError,ValueError)
        if escapes:
            with pytest.raises(fault):o.d._export()
            assert o.d.result()!=QDialog.Accepted and len(o.notes)==1
        else:
            o.d._export();assert o.d.result()==QDialog.Accepted and len(o.notes)==2
            if fault:assert o.first_id in o.d.tracking_warning and 'Generated optional history fault' in o.d.tracking_warning
            else:assert not o.d.tracking_warning
        assert len(o.exports)==1;result,digest=o.exports[0]
        assert o.d.archive_path==result.path and zipfile.is_zipfile(result.path) and sha(result.path)==digest==result.sha256
        assert immutable(tmp_path/'generated-feedback')==o.before
    finally:o.d.close();o.d.deleteLater();qt_test_scope.settle(2)

class GeneratedAbort(BaseException):pass

@pytest.mark.parametrize('variant',['baseline','candidate'])
def test_nonordinary_abort_is_not_swallowed(qt_test_scope,monkeypatch,tmp_path,variant):
    o=arrange(qt_test_scope,monkeypatch,tmp_path,variant,GeneratedAbort)
    try:
        with pytest.raises(GeneratedAbort):o.d._export()
        assert len(o.exports)==1 and o.d.archive_path==o.exports[0][0].path and o.d.result()!=QDialog.Accepted
        assert immutable(tmp_path/'generated-feedback')==o.before
    finally:o.d.close();o.d.deleteLater();qt_test_scope.settle(2)

@pytest.mark.parametrize('theme',['light','dark'])
def test_visible_export_and_exact_parent_warning_carrier(qt_test_scope,monkeypatch,tmp_path,theme):
    apply_theme(qt_test_scope.app,theme,False);o=arrange(qt_test_scope,monkeypatch,tmp_path,'candidate',RuntimeError);parent=QWidget();messages=[]
    try:
        capture(o.d,theme,'selection');QTest.mouseClick(o.d.export_button,Qt.LeftButton);qt_test_scope.settle(2)
        assert o.d.result()==QDialog.Accepted and o.d.tracking_warning and len(o.exports)==1 and len(o.notes)==2
        # Compile only the exact original parent method. Avoid importing or
        # constructing the full MainWindow and never enumerate its feedback root.
        path=Path(__file__).resolve().parents[1]/'qt_app/main_window.py';tree=ast.parse(path.read_bytes());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='TagManagerWindow');method=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='export_collected_feedback')
        class Box(QMessageBox):
            def exec(self):
                messages.append(self.text());qt_test_scope.show(self,size=(900,500));qt_test_scope.settle(2);capture(self,theme,'known-archive-history-warning');QTest.mouseClick(self.button(QMessageBox.Close),Qt.LeftButton);qt_test_scope.settle(2);return self.result()
        def forbidden(*a,**k):pytest.fail('No clipboard or folder action allowed')
        def dialog_factory(path,owner):
            assert owner is parent
            return NS(exec=lambda:o.d.result(),archive_path=o.d.archive_path,tracking_warning=o.d.tracking_warning)
        env=dict(__file__=str(path),Path=Path,FeedbackArchiveDialog=dialog_factory,QDialog=QDialog,QMessageBox=Box,QApplication=NS(clipboard=forbidden),QDesktopServices=NS(openUrl=forbidden),QUrl=NS(fromLocalFile=forbidden))
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(path),'exec'),env);env['export_collected_feedback'](parent)
        assert len(messages)==1 and str(o.d.archive_path) in messages[0] and 'The archive is valid' in messages[0] and o.first_id in messages[0]
        assert len(o.exports)==1 and immutable(tmp_path/'generated-feedback')==o.before and sha(o.d.archive_path)==o.exports[0][1]
    finally:o.d.close();o.d.deleteLater();parent.close();parent.deleteLater();qt_test_scope.settle(2)
