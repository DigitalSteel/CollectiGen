"""Run the exact packaged final-Close caller over inert owners, not a live Root.

Uses the retained caller regression's False/None/True/deliberate-retry cases and
stock Qt warning in light/dark. No actual reader/editor job or library is opened.
Run with the bundled interpreter -I -B and an explicit fresh --output directory.
"""
import argparse,ast,json,sys,types
from pathlib import Path
from types import SimpleNamespace as NS
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent,QFont,QFontDatabase
from PySide6.QtWidgets import QApplication,QWidget,QLabel,QVBoxLayout,QMessageBox
from qt_app.theme import apply_theme

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();assert not args.output.exists();args.output.mkdir(parents=True)
    package_root=Path(sys.executable).resolve().parents[2]
    tree=ast.parse((package_root/'qt_app/main_window.py').read_bytes());owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TagManagerWindow');node=next(n for n in owner.body if isinstance(n,ast.FunctionDef) and n.name=='closeEvent')
    package=types.ModuleType('close_source_seam');package.__path__=[];reading=types.ModuleType('close_source_seam.learning_help_search');reading.close_learning_libraries=lambda owner:True;sys.modules[package.__name__]=package;sys.modules[reading.__name__]=reading
    def build(base,message):
        ns=dict(__name__='close_source_seam.window',__package__='close_source_seam',Base=base,QCloseEvent=QCloseEvent,QMessageBox=message,QTimer=QTimer)
        cls=ast.ClassDef(name='TagManagerWindow',bases=[ast.Name(id='Base',ctx=ast.Load())],keywords=[],body=[node],decorator_list=[]);exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'exact-packaged-final-close-caller','exec'),ns);return ns['TagManagerWindow']
    class Event:
        accepted=False;ignored=False
        def ignore(self):self.ignored=True
    class Base:
        def closeEvent(self,event):event.accepted=True;self.trace.append('base-close')
    def setup(w,result):
        w.trace=[];w.result=result;w._scan_thread=None;w.ui_test=True;w._derived_refresh_event_callback=object();w._long_operation_event_callback=object();w._review_page_preparation=object();w._dataset_page_preparation=object();w._dataset_preparation_shutdown=False
        w._review_page_task=NS(cancel=lambda:w.trace.append('cancel-review'));w._dataset_page_task=NS(cancel=lambda:w.trace.append('cancel-dataset'));w._pending_dataset_page_actions=[object(),object()];w._finish_knowledge_editor_wait=lambda:w.trace.append('finish-editor-wait')
        def drain(**kwargs):
            assert kwargs==dict(wait=True,timeout=2.0);w.trace.append('drain');return w.result
        w.state=NS(shutdown_background_jobs=drain,events=NS(unsubscribe=lambda name,callback:w.trace.append('unsubscribe-'+name)))
        return dict(review=w._review_page_task,dataset=w._dataset_page_task,rprep=w._review_page_preparation,dprep=w._dataset_page_preparation,actions=w._pending_dataset_page_actions,values=tuple(w._pending_dataset_page_actions))
    def retained(w,b):
        assert w._review_page_task is b['review'] and w._dataset_page_task is b['dataset'];assert w._review_page_preparation is b['rprep'] and w._dataset_page_preparation is b['dprep'];assert w._pending_dataset_page_actions is b['actions'] and tuple(b['actions'])==b['values'];assert not w._dataset_preparation_shutdown
    def cleaned(w):
        assert w._review_page_task is None and w._dataset_page_task is None and w._review_page_preparation is None and w._dataset_page_preparation is None;assert w._pending_dataset_page_actions==[] and w._dataset_preparation_shutdown
        for event in ('cancel-review','cancel-dataset','finish-editor-wait','unsubscribe-derived_refresh_changed','unsubscribe-long_operation_changed'):assert w.trace.count(event)==1
        assert w.trace.index('drain')<w.trace.index('cancel-review')
    cases=[]
    for value in (False,None):
        warnings=[];w=build(Base,NS(warning=lambda *args:warnings.append(args)))();b=setup(w,value);event=Event();w.closeEvent(event);assert event.ignored and not event.accepted and len(warnings)==1;retained(w,b);assert w.trace==['drain'];cases.append('retain-'+str(value))
    def unexpected(*args):raise AssertionError('Unexpected warning')
    w=build(Base,NS(warning=unexpected))();setup(w,True);event=Event();w.closeEvent(event);cleaned(w);assert event.accepted and w.trace.count('base-close')==1;cases.append('true-once')
    w=build(Base,NS(warning=lambda *args:None))();b=setup(w,False);event=Event();w.closeEvent(event);retained(w,b);w.result=True;second=Event();w.closeEvent(second);cleaned(w);assert event.ignored and second.accepted and w.trace.count('drain')==2;cases.append('explicit-retry')
    app=QApplication([]);app.setQuitOnLastWindowClosed(False);font=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf');assert font>=0;app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0],10))
    for theme in ('light','dark'):
        apply_theme(app,theme);w=build(QWidget,QMessageBox)();b=setup(w,False);layout=QVBoxLayout(w);layout.addWidget(QLabel('Generated final-Close caller; no real library, reader or background job'));w.resize(850,240);w.show();app.processEvents();errors=[];seen=[];watch=QTimer(w);watch.setSingleShot(True)
        def timeout():
            errors.append('Warning deadline');box=QApplication.activeModalWidget()
            if isinstance(box,QMessageBox):box.reject()
        def inspect():
            box=QApplication.activeModalWidget()
            try:
                assert isinstance(box,QMessageBox) and box.windowTitle()=='Close needs attention';assert 'window remains open' in box.text();retained(w,b);assert box.grab().save(str(args.output/(theme+'.png')));seen.append(True)
            except BaseException as error:errors.append(repr(error))
            if isinstance(box,QMessageBox):box.accept()
        watch.timeout.connect(timeout);watch.start(8000);QTimer.singleShot(0,inspect)
        try:assert not w.close()
        finally:watch.stop()
        assert not errors and seen==[True] and w.isVisible(),errors;retained(w,b);assert w.trace==['drain'];w.result=True;assert w.close();cleaned(w);assert not w.isVisible();w.deleteLater();app.processEvents();cases.append('visible-'+theme)
    result=dict(passed=True,cases=cases,exact_packaged_caller_ast=True,real_Qt_warning=True,actual_worker_reader_or_Root_execution=False,private_data_access=False)
    (args.output/'RESULT.json').write_bytes((json.dumps(result,indent=2)+'\n').encode());print(json.dumps(result));app.quit()

if __name__=='__main__':main()
