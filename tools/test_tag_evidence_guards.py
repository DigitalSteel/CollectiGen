"""Packaged F15 callback/label checks with generated data and inert producers.

No actual KnowledgePage, library/index query, transaction or Root/Reader is
constructed. Run with bundled Python -I -B and a fresh --output directory.
"""
import argparse,ast,json,sys
from pathlib import Path
from types import SimpleNamespace as NS
from PySide6.QtCore import QObject,Signal,QTimer,Qt
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLabel
from qt_app.theme import apply_theme

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();assert not args.output.exists();args.output.mkdir(parents=True)
    root=Path(sys.executable).resolve().parents[2];tree=ast.parse((root/'qt_app/knowledge_page.py').read_bytes());owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='KnowledgePage');methods={n.name:n for n in owner.body if isinstance(n,ast.FunctionDef)}
    class Signals(QObject):
        complete=Signal(object);failed=Signal(str)
    tasks=[];queries=[]
    class Task:
        def __init__(self,*args,**kwargs):self.signals=Signals();self.started=0;tasks.append(self)
        def start(self):self.started+=1
        def cancel(self):raise AssertionError('Unexpected previous-task cancellation')
    ns=dict(CoordinatedCallTask=Task,AsyncCallTask=Task,normalize_tag=lambda value:str(value).casefold().replace(' ','_'))
    cls=ast.ClassDef(name='PackagedTagEvidenceCaller',bases=[],keywords=[],body=[methods['_load_tag_examples'],methods['_tag_examples_failed']],decorator_list=[]);exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'packaged-tag-evidence-caller','exec'),ns);Caller=ns['PackagedTagEvidenceCaller']
    app=QApplication([]);app.setQuitOnLastWindowClosed(False);font=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf');assert font>=0;app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0],10));errors=[];previous_hook=sys.excepthook;sys.excepthook=lambda kind,error,tb:errors.append(kind.__name__+': '+str(error));cases=[]
    try:
        for transport in ('pool','coordinated'):
            for scenario in ('current','generation','changed-tag','removed-tag'):
                record=NS(id='generated_tag',display='Generated Tag');page=Caller();page.state=NS(dataset=NS(index=object()));page.tags=NS(tags={record.id:record});page.current_tag_id=record.id;page._tag_gallery_generation=0;page._tag_gallery_task=None;page._tag_names=lambda item:[item.display];page._background_jobs=object() if transport=='coordinated' else None;pool=[];page.pool=NS(start=lambda task:pool.append(task));feedback=[];loading=[];page.tag_gallery=NS(set_loading=lambda context:loading.append(context),set_unavailable=lambda **kwargs:None,set_error=lambda message,context:feedback.append((message,context)))
                def query(*a,**kw):queries.append(True);raise AssertionError('Real query is outside this fixture')
                page._evidence_queries=NS(query=query,request_key=lambda *a:'generated-request');page._load_tag_examples(record);task=tasks[-1];assert page._tag_gallery_task is task and len(loading)==1
                assert (task.started==1 and not pool) if transport=='coordinated' else (task.started==0 and pool==[task])
                sentinel=object()
                if scenario=='generation':page._tag_gallery_generation+=1
                elif scenario=='changed-tag':page.current_tag_id='other_tag'
                elif scenario=='removed-tag':page.tags.tags.clear()
                if scenario!='current':page._tag_gallery_task=sentinel
                before=dict(page.tags.tags);delivered=[]
                def emit():task.signals.failed.emit('Generated example failure');delivered.append(True)
                QTimer.singleShot(0,emit);app.processEvents();assert delivered==[True] and not errors,errors
                if scenario=='current':assert feedback==[('Generated example failure','Examples using “Generated Tag”')] and page._tag_gallery_task is None
                else:assert not feedback and page._tag_gallery_task is sentinel
                assert page.tags.tags==before;cases.append(transport+'-'+scenario)
        # Use the exact original construction statement, not a replacement label.
        builder=methods['_build_tags'];assignment=next(n for n in builder.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Attribute) and t.attr=='tag_provenance' for t in n.targets));statement=[n for n in builder.body if n.lineno==assignment.lineno];constructor=compile(ast.fix_missing_locations(ast.Module(body=statement,type_ignores=[])),'packaged-original-provenance-label','exec')
        for theme in ('light','dark'):
            apply_theme(app,theme)
            for kind in ('empty','markup'):
                holder=NS();exec(constructor,dict(self=holder,QLabel=QLabel,Qt=Qt));label=holder.tag_provenance;assert label.textFormat()==Qt.PlainText and not label.openExternalLinks()
                raw='No recorded provenance' if kind=='empty' else 'Source <b>literal</b> &amp; unknown <img src="generated://example.png"> · future source fields retained'
                label.setText(raw);window=QWidget();layout=QVBoxLayout(window);layout.addWidget(QLabel('Knowledge sources — original read-only provenance summary'));layout.addWidget(label);window.resize(1100,260);window.show();app.processEvents();assert label.text()==raw;assert window.grab().save(str(args.output/(theme+'-'+kind+'.png')));window.close();window.deleteLater();app.processEvents();cases.append('literal-'+theme+'-'+kind)
        assert not queries and not errors
        result=dict(passed=True,cases=cases,both_existing_callback_transports=True,real_queued_Qt_signal_delivery=True,current_error_released=True,stale_generation_changed_or_removed_tag_preserves_current_task=True,original_label_explicit_plaintext=True,literal_markup_and_empty_source_preserved=True,query_producers='inert fixtures; canonical query not executed',actual_page_store_transaction_or_Root_execution=False,native_resource_access_instrumented=False,private_data_access=False)
        (args.output/'RESULT.json').write_bytes((json.dumps(result,indent=2)+'\n').encode());print(json.dumps(result))
    finally:sys.excepthook=previous_hook;app.quit()

if __name__=='__main__':main()
