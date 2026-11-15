"""Generated original source widgets/services; no server/model/published app."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from test_lmstudio_service_review_join import selected_public_source, CHILD as ORIGINAL

BOOTSTRAP=ORIGINAL.split("image=case_root/")[0]
CHILD=BOOTSTRAP+r'''
from threading import RLock
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton,QComboBox
from core.settings import AppSettings
from core.lmstudio_connection_settings import LmStudioConnectionSettings
from core.lmstudio_discovery import parse_models
from core.app_state import AppState
from core.background_jobs import BackgroundJobManager
from core.events import EventBus
from core.tag_database import TagDatabase
from core.operation_journal import OperationJournal
from qt_app.ai_run_dialog import RunAIDialog
from qt_app.theme import build_stylesheet,_light_palette,_dark_palette
app=QApplication([]);assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')>=0
app.setStyle('Fusion');app.setFont(QFont('Segoe UI',10))
theme='dark' if case in {'success','cancel','error','selection'} else 'light'
app.setPalette(_dark_palette() if theme=='dark' else _light_palette())
app.setStyleSheet(build_stylesheet(theme))
settings=AppSettings()
settings.ai_models_directory=str(profile/"generated-models")
knowledge=TagDatabase(profile/'data/generated-tags.json')
state=AppState.__new__(AppState)
state.settings=settings;state.knowledge=knowledge;state.ai=AIInterrogationService()
state.jobs=BackgroundJobManager();state.events=EventBus()
state._operation_lock=RLock();state._operation_gate={};state._ai_lease_owners=set()
service=LmStudioConnectionSettings(profile/'settings.json')
service.save_selection('http://127.0.0.1:1234',preferred_model_key='generated/model',expected=service.observe())
if case=='no_vision':service.forget_selection(expected=service.observe())
image=case_root/'generated.png'
with Image.new('RGB',(3,2),(20,40,60)) as value:value.save(image)
record=ImageRecord(image)
record.annotation.plain_language=['An existing confirmed fixture sentence.']
record.annotation.source['generated_raw_evidence']={'raw':'generated-exact-evidence'}
record.modified=True;record.save_sidecar(force=True)
original=image.read_bytes();saved=record.sidecar_path.read_bytes()
calls=[];requests=[];captures=[]
model=dict(key='generated/model',type='llm',display_name='Generated Vision Model',size_bytes=1024,
    capabilities=dict(vision=True),loaded_instances=[dict(id='generated-instance',config=dict(context_length=2048))])
def discover(endpoint,**kwargs):
    calls.append(endpoint)
    import time;time.sleep(.12)
    if case=='refresh_close':
        import time
        deadline=time.monotonic()+3
        while not kwargs['cancelled']() and time.monotonic()<deadline:time.sleep(.01)
    if case=='no_vision' and len(calls)==2:raise RuntimeError('generated_discovery_failure')
    values=[model]
    if case=='no_vision':
        import copy
        bad=copy.deepcopy(model);bad['capabilities']['vision']=False;values=[bad]
    if case=='selection':
        import copy
        bad=copy.deepcopy(model);bad['key']='generated/no-vision';bad['capabilities']['vision']=False;bad['loaded_instances']=[]
        other=copy.deepcopy(model);other['key']='generated/multiple';other['loaded_instances']=[dict(id='instance-a',config={}),dict(id='instance-b',config={})]
        values=[bad,other,model]
    return parse_models(json.dumps(dict(models=values)).encode())
description='A distinct synthetic sculpture stands outdoors.'
def dispatch(**kwargs):
    requests.append(True)
    assert kwargs.pop('endpoint')=='http://127.0.0.1:1234'
    if case=='cancel':
        import time
        deadline=time.monotonic()+3
        while not kwargs['cancelled']() and time.monotonic()<deadline:time.sleep(.01)
        raise DescriptionTransportError('description_cancelled','outcome_unknown')
    if case=='error' and len(requests)==1:
        raise DescriptionTransportError('description_io_timeout','outcome_unknown',cleanup_failed=True)
    import time;time.sleep(.18)
    if case=='success':assert kwargs['prompt']=='Describe this generated scene.'
    request=encode_description(**{k:v for k,v in kwargs.items() if k!='cancelled'})
    raw=json.dumps(dict(model_instance_id='generated-instance',output=[dict(type='message',content=description)])).encode()
    return decode_description(raw,request)
def wait(predicate):
    for _ in range(2000):
        QTest.qWait(10)
        if predicate():return
    task=getattr(dialog,'_task',None)
    (case_root/'SETTLEMENT_DIAGNOSTIC.json').write_text(json.dumps(dict(task_state=getattr(task,'state',None),finished=task.is_finished() if task is not None else None,analysis_delivery=dialog._lm_analysis_outcome is not None,timer_active=dialog._lm_settle.isActive(),violations=violations)))
    raise AssertionError('generated_ui_did_not_settle')
def tap(widget):
    assert widget.isVisible() and widget.isEnabled(),widget.objectName() or widget.text()
    QTest.mouseClick(widget,Qt.LeftButton);QTest.qWait(30)
def choose(combo,last=True):
    assert combo.isVisible() and combo.isEnabled()
    combo.setFocus(Qt.TabFocusReason)
    QTest.keyClick(combo,Qt.Key_End if last else Qt.Key_Home);QTest.qWait(30)
def capture(widget,name):
    app.processEvents();dest=case_root/(name+'.png');assert widget.grab().save(str(dest))
    assert widget.width()<=1920 and widget.height()<=1080
    captures.append(dict(name=name,path=dest.name,width=widget.width(),height=widget.height(),theme=theme))
dialog=RunAIDialog(state,settings,[image],owner='generated',journal=OperationJournal(profile/'generated-journal.jsonl'),
    lmstudio_connection_service=service,lmstudio_discovery=discover,lmstudio_dispatch=None if case=='blocked' else dispatch)
dialog.show();QTest.qWait(40);dialog.resize(980,900);QTest.qWait(30)
assert dialog.description_provider.currentData()=='local' and not calls and not requests
local=(dialog.tags.isChecked(),dialog.descriptions.isChecked(),dialog.confirm_tags.isChecked())
capture(dialog,'local-default')
choose(dialog.description_provider)
assert not calls and not requests and dialog.descriptions.isChecked() and not dialog.tags.isChecked()
assert not dialog.run_button.isEnabled()
capture(dialog,'external-before-refresh')
if case=='switch_back':
    choose(dialog.description_provider,last=False)
    assert (dialog.tags.isChecked(),dialog.descriptions.isChecked(),dialog.confirm_tags.isChecked())==local
    assert not calls and not requests
    dialog.reject()
elif case=='preferences':
    def preferences(save):
        from qt_app.lmstudio_connection_dialog import LmStudioConnectionDialog
        editor=next(w for w in app.topLevelWidgets() if isinstance(w,LmStudioConnectionDialog) and w.isVisible())
        wait(lambda:editor._task is None)
        if save:
            editor.model_key.setFocus();editor.model_key.selectAll();QTest.keyClicks(editor.model_key,'generated/changed')
            tap(editor.save_button);wait(lambda:editor._task is None)
            assert service.observe().selection.preferred_model_key=='generated/changed'
        else:assert editor.model_key.text()=='generated/changed'
        tap(editor.close_button)
    QTimer.singleShot(60,lambda:preferences(True));tap(dialog.lm_connection)
    QTimer.singleShot(60,lambda:preferences(False));tap(dialog.lm_connection)
    assert not calls and not requests;dialog.reject()
else:
    if case=='no_vision':
        assert 'No connection saved' in dialog.lm_status.text()
        service.save_selection('http://127.0.0.1:1234',expected=service.observe())
    tap(dialog.lm_refresh)
    refresh_task=dialog._lm_refresh_task
    if case=='refresh_close':
        capture(dialog,'metadata-pending')
        tap(dialog.cancel_button)
        wait(lambda:dialog._lm_refresh_task is None)
        assert not dialog.isVisible() and not requests
    else:
        wait(lambda:dialog._lm_refresh_task is None)
        (case_root/'REFRESH_DIAGNOSTIC.json').write_text(json.dumps(dict(status=dialog.lm_status.text(),count=dialog.lm_models.count(),error=refresh_task.error,violations=violations)))
        assert len(calls)==1 and dialog.lm_models.currentData() is None
        assert not dialog.run_button.isEnabled()
        if case=='selection':assert dialog.lm_models.count()==4
        if case!='no_vision':choose(dialog.lm_models)
        capture(dialog,'instance-selected')
        if case=='no_vision':
            assert dialog.lm_models.count()==1 and not dialog.run_button.isEnabled()
            assert 'No loaded image-capable' in dialog.lm_status.text() and not requests
            tap(dialog.lm_refresh);wait(lambda:dialog._lm_refresh_task is None)
            assert 'unavailable' in dialog.lm_status.text() and not requests
            capture(dialog,'refresh-unavailable')
            tap(dialog.lm_refresh);wait(lambda:dialog._lm_refresh_task is None)
            assert len(calls)==3 and 'No loaded image-capable' in dialog.lm_status.text()
            assert not requests;dialog.reject()
        elif case=='blocked':
            tap(dialog.lm_options);capture(dialog,'advanced-options');tap(dialog.lm_options)
            assert not dialog.run_button.isEnabled() and 'not available' in dialog.lm_status.text()
            assert not requests;dialog.reject()
        elif case=='stale':
            service.save_selection('http://127.0.0.1:1235',expected=service.observe())
            tap(dialog.run_button)
            assert not requests and not state._operation_gate and not dialog.run_button.isEnabled()
            capture(dialog,'stale-refused');dialog.reject()
        else:
            if case=='success':
                tap(dialog.lm_options);assert dialog.lm_advanced.isVisible()
                dialog.lm_prompt.setFocus();dialog.lm_prompt.selectAll();QTest.keyClicks(dialog.lm_prompt,'Describe this generated scene.')
                tap(dialog.lm_options);assert not dialog.lm_advanced.isVisible()
            import faulthandler
            trace=(case_root/'THREAD_TRACE.log').open('w')
            faulthandler.dump_traceback_later(4,file=trace)
            tap(dialog.run_button)
            capture(dialog,'request-pending')
            if case=='cancel':
                wait(lambda:len(requests)==1);tap(dialog.cancel_button)
            wait(lambda:dialog._task is None)
            assert not state._operation_gate and not state._ai_lease_owners
            capture(dialog,'result')
            result=dialog.result_data
            assert result is not None and len(requests)==1
            if case in {'cancel','error'}:
                outcome=result.outcomes[0]
                assert outcome.external_request_outcome.submission_state=='outcome_unknown'
                assert 'server work may continue' in outcome.detail()
                assert record.sidecar_path.read_bytes()==saved
                if case=='error':
                    assert outcome.external_request_outcome.cleanup_failed
                    def retry():
                        child=next(w for w in app.topLevelWidgets() if isinstance(w,RunAIDialog) and w is not dialog and w.isVisible())
                        child.resize(980,900);QTest.qWait(30)
                        assert child.description_provider.currentData()=='lmstudio' and not child.run_button.isEnabled()
                        tap(child.lm_refresh);wait(lambda:child._lm_refresh_task is None)
                        choose(child.lm_models);tap(child.run_button);wait(lambda:child._task is None)
                        assert child.result_data.description_model_passes==1
                        capture(child,'retry-result');tap(child.cancel_button)
                    QTimer.singleShot(60,retry);tap(dialog.retry_button)
                    assert dialog.latest_retry_result.description_model_passes==1 and len(requests)==2
                    assert len(ImageRecord(image).annotation.ai_evidence)==1
                dialog.reject()
            else:
                assert result.description_model_passes==1 and result.review_paths==[image]
                loaded=ImageRecord(image)
                assert loaded.annotation.plain_language==record.annotation.plain_language
                assert len(loaded.annotation.ai_evidence)==1
                tap(dialog.review_button)
                assert dialog.review_requested and dialog.review_paths_requested==(image,)
                from qt_app.image_data_editor import ImageDataEditor
                editor=ImageDataEditor(loaded,settings=settings,knowledge=knowledge,allow_ai=False,image_knowledge_only=True)
                editor.show();QTest.qWait(50);editor.resize(1400,950);QTest.qWait(30)
                index=editor.tabs.indexOf(editor.description_tab)
                QTest.mouseClick(editor.tabs.tabBar(),Qt.LeftButton,pos=editor.tabs.tabBar().tabRect(index).center())
                QTest.qWait(30)
                tree=editor.description_suggestions
                assert tree.topLevelItemCount()>=1
                QTest.mouseClick(tree.viewport(),Qt.LeftButton,pos=tree.visualItemRect(tree.topLevelItem(0)).center())
                capture(editor,'review')
                def edited():
                    for widget in app.topLevelWidgets():
                        if widget.windowTitle()=='Edit confirmed descriptions':
                            for button in widget.findChildren(QPushButton):
                                if button.text()=='Use Edited Result':QTest.mouseClick(button,Qt.LeftButton);return
                    raise AssertionError('Expected original description decision')
                QTimer.singleShot(50,edited);tap(editor.confirm_description_suggestions)
                assert description in editor.prose.toPlainText()
                assert record.annotation.plain_language[0] in editor.prose.toPlainText()
                tap(editor.save_button);QTest.qWait(100)
                reopened=ImageRecord(image)
                assert description in reopened.annotation.plain_language
                assert record.annotation.plain_language[0] in reopened.annotation.plain_language
                assert reopened.annotation.source['generated_raw_evidence']==record.annotation.source['generated_raw_evidence']
                editor.close();QTest.qWait(40)
                fresh=ImageDataEditor(reopened,settings=settings,knowledge=knowledge,allow_ai=False,image_knowledge_only=True)
                fresh.show();QTest.qWait(40);fresh.resize(1400,950);QTest.qWait(30)
                index=fresh.tabs.indexOf(fresh.description_tab)
                QTest.mouseClick(fresh.tabs.tabBar(),Qt.LeftButton,pos=fresh.tabs.tabBar().tabRect(index).center())
                assert description in fresh.prose.toPlainText()
                capture(fresh,'saved-reopen');fresh.close();QTest.qWait(40)
assert image.read_bytes()==original and not violations
assert dialog._lm_refresh_task is None and dialog._task is None
(case_root/'RESULT.json').write_text(json.dumps(dict(case=case,passed=True,captures=captures,
    discovery_count=len(calls),request_count=len(requests),profile_paths_bound=True,
    source_component_only=True,actual_server_or_model=False,blocked_operations=violations),indent=2)+'\n')
print('generated_source_ui_passed')
'''

@pytest.mark.parametrize('case',['success','blocked','switch_back','selection','stale','cancel','error','refresh_close','preferences','no_vision'])
def test_manual_consumer_generated_source(selected_public_source,tmp_path,case):
    env={key:value for key,value in os.environ.items() if not key.startswith(('TAG_MANAGER_UI_TEST','TAG_MANAGER_PYTEST'))}
    env['QT_QPA_PLATFORM']='offscreen'
    completed=subprocess.run([sys.executable,'-I','-B','-c',CHILD,str(selected_public_source),str(tmp_path),case],
        cwd=selected_public_source,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=60,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (tmp_path/'STDOUT.log').write_text(completed.stdout,encoding='utf-8')
    (tmp_path/'STDERR.log').write_text(completed.stderr,encoding='utf-8')
    assert completed.returncode==0,completed.stderr[-2500:]
    result=json.loads((tmp_path/'RESULT.json').read_bytes())
    assert result['passed'] and not result['blocked_operations']
