"""Fresh generated source-component consumers; no main app/server/model/secret."""
import json
import os
import subprocess
import sys

import pytest
from test_lmstudio_service_review_join import selected_public_source
from test_lmstudio_manual_consumer_ui import BOOTSTRAP


CHILD = BOOTSTRAP + r'''
import time
from threading import Event,RLock
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QLineEdit
from core.settings import AppSettings
from core.lmstudio_connection_settings import LmStudioConnectionSettings
from core.lmstudio_discovery import DiscoveryError,parse_models
from core.app_state import AppState
from core.background_jobs import BackgroundJobManager
from core.events import EventBus
from core.tag_database import TagDatabase
from core.operation_journal import OperationJournal
from qt_app.ai_run_dialog import RunAIDialog
from qt_app.lmstudio_connection_dialog import LmStudioConnectionDialog
from qt_app.theme import build_stylesheet,_light_palette,_dark_palette
import qt_app.lmstudio_connection_dialog as connection_ui
consumer,scenario,theme=case.split('-')
app=QApplication([])
assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')>=0
app.setStyle('Fusion');app.setFont(QFont('Segoe UI',10))
app.setPalette(_dark_palette() if theme=='dark' else _light_palette())
app.setStyleSheet(build_stylesheet(theme))
service=LmStudioConnectionSettings(profile/'settings.json')
service.save_selection('http://127.0.0.1:1234',expected=service.observe())
baseline=(profile/'settings.json').read_bytes()
marker='generated-ui-marker-7Q8Z'
calls=[];captures=[];entered=Event();release=Event();held=[]
model=dict(key='generated/model',type='llm',display_name='Generated Vision Model',size_bytes=1,
    capabilities=dict(vision=True),loaded_instances=[dict(id='generated-instance',config={})])
def discovery(endpoint,**kwargs):
    calls.append(dict(endpoint=endpoint,has_token='token' in kwargs,token_matches=kwargs.get('token')==marker))
    entered.set()
    if scenario in {'cancel','close'}:release.wait(6)
    else:time.sleep(.15)
    if scenario=='error':raise DiscoveryError(marker)
    if scenario=='echo':
        value=dict(model);value['display_name']=marker
        return parse_models(json.dumps(dict(models=[value])).encode())
    return parse_models(json.dumps(dict(models=[model])).encode())
settings=AppSettings();settings.ai_models_directory=str(profile/'generated-models')
state=AppState.__new__(AppState)
state.settings=settings;state.knowledge=TagDatabase(profile/'data/generated-tags.json')
state.ai=AIInterrogationService();state.jobs=BackgroundJobManager();state.events=EventBus()
state._operation_lock=RLock();state._operation_gate={};state._ai_lease_owners=set()
if consumer=='connection':
    dialog=LmStudioConnectionDialog(service,discovery=discovery)
    token=dialog.auth_token;toggle=dialog.auth_toggle;refresh=dialog.refresh_button
    busy=lambda:dialog._task is not None
else:
    dialog=RunAIDialog(state,settings,[],owner='generated-auth',journal=OperationJournal(profile/'journal.jsonl'),
        lmstudio_connection_service=service,lmstudio_discovery=discovery)
    token=dialog.lm_auth_token;toggle=dialog.lm_auth_toggle;refresh=dialog.lm_refresh
    busy=lambda:dialog._lm_refresh_task is not None
def wait(predicate):
    for _ in range(600):
        QTest.qWait(10)
        if predicate():return
    raise AssertionError('generated_component_did_not_settle')
def tap(widget):
    assert widget.isVisible() and widget.isEnabled()
    QTest.mouseClick(widget,Qt.LeftButton);QTest.qWait(15)
def capture(name):
    app.processEvents()
    target=case_root/(name+'.png')
    assert dialog.width()<=1920 and dialog.height()<=1080
    assert dialog.grab().save(str(target));captures.append(str(target))
dialog.show();wait(lambda:not busy())
# Render the same supported 1080p component extent used by the original source harness.
dialog.resize(980,900) if consumer=='manual' else dialog.resize(720,650)
if consumer=='manual':
    dialog.description_provider.setFocus();QTest.keyClick(dialog.description_provider,Qt.Key_End)
    QTest.keyClick(dialog.description_provider,Qt.Key_Return);QTest.qWait(30)
assert not calls and token.echoMode()==QLineEdit.Password and token.maxLength()==4097
capture('default')
tap(toggle);token.setText(marker)
assert token.displayText()!=marker
capture('authentication')
if scenario=='privacy':
    # Exercise the original Feedback hook, with generated capture and no report submission.
    import qt_app.modal_feedback as feedback
    observed=[]
    def host(**kwargs):
        assert not token.text();observed.append(True)
    feedback._feedback_host=lambda origin:host
    tap(dialog._feedback_button);assert observed and not token.text()
elif scenario=='change':
    if consumer=='connection':dialog.endpoint.setText('http://127.0.0.1:1235')
    else:QTest.keyClick(dialog.description_provider,Qt.Key_Home);QTest.keyClick(dialog.description_provider,Qt.Key_Return)
    assert not token.text() and not calls
elif scenario=='stale':
    if consumer=='manual':
        service.save_selection('http://127.0.0.1:1235',expected=service.observe())
        tap(refresh)
        assert not calls and not token.text() and 'address changed' in dialog.lm_status.text()
    else:
        dialog.endpoint.setText('http://127.0.0.1:1235')
        assert not token.text()
elif scenario=='save':
    if consumer=='connection':
        dialog.model_key.setText('generated-preference');tap(dialog.save_button);wait(lambda:not busy())
        assert not token.text();token.setText(marker);tap(dialog.forget_button);wait(lambda:not busy())
        assert not token.text() and service.observe().selection is None
    else:
        # Entering the separate preferences modal clears the caller draft; no transfer.
        from PySide6.QtCore import QTimer
        def close_child():
            child=next(w for w in app.topLevelWidgets() if isinstance(w,LmStudioConnectionDialog))
            assert not child.auth_token.text();wait(lambda:child._task is None);child.reject()
        QTimer.singleShot(60,close_child);tap(dialog.lm_connection)
        assert not token.text()
elif scenario in {'queued','dispatch'}:
    class Pool:
        def start(self,task):
            held.append(task)
            if scenario=='dispatch':raise RuntimeError(marker)
    if consumer=='connection':
        original=connection_ui.start_qt_delivery_task
        connection_ui.start_qt_delivery_task=lambda pool,task:Pool().start(task)
    else:dialog.pool=Pool()
    tap(refresh);assert not token.text() and not calls
    task=held[0]
    if scenario=='queued':
        if consumer=='connection':tap(dialog.cancel_button)
        else:dialog.reject()
        assert task.callable._token is None if consumer=='manual' else dialog._discovery_invocation._token is None
        task.run();wait(lambda:not busy());assert not calls
    else:
        assert not busy()
        invocation=task.callable if consumer=='manual' else None
        if invocation is not None:assert invocation._token is None
    if consumer=='connection':connection_ui.start_qt_delivery_task=original
else:
    if scenario=='invalid':token.setText('bad token')
    tap(refresh);assert not token.text()
    if scenario=='invalid':assert not calls and not busy()
    else:
        wait(entered.is_set)
        if scenario in {'cancel','close'}:
            QTest.qWait(1150)
            bar=dialog.activity if consumer=='connection' else dialog.lm_activity
            assert bar.isVisible()
            if consumer=='connection':assert bar.minimum()==bar.maximum()==0
            else:
                assert bar.active
                phase=bar._phase;QTest.qWait(160)
                assert bar._phase!=phase if bar._motion_enabled() else not bar._timer.isActive()
            assert dialog._settle.isActive() if consumer=='connection' else dialog._lm_settle.isActive()
        capture('pending')
        if scenario in {'cancel','close'}:
            if consumer=='connection' and scenario=='cancel':tap(dialog.cancel_button)
            else:dialog.reject()
            if consumer=='manual':
                app.processEvents()
                assert dialog.cancel_button.fontMetrics().horizontalAdvance(dialog.cancel_button.text()) < dialog.cancel_button.width()-8
            assert busy();capture('cleanup');release.set()
        wait(lambda:not busy())
        assert len(calls)==1 and calls[0]['has_token'] and calls[0]['token_matches']
        if scenario in {'error','echo'}:
            models=dialog.models if consumer=='connection' else dialog.lm_models
            assert models.count()==1
        elif scenario=='success':
            models=dialog.models if consumer=='connection' else dialog.lm_models
            assert models.count()==2
            tap(refresh);wait(lambda:not busy())
            assert len(calls)==2 and not calls[1]['has_token']
            if consumer=='manual':assert not dialog.run_button.isEnabled() and dialog._lm_dispatch is None
capture('result')
for label in dialog.findChildren(QLineEdit):assert marker not in label.text()
assert marker.encode() not in (profile/'settings.json').read_bytes()
assert marker not in repr(calls)
if scenario not in {'save','stale'}:assert (profile/'settings.json').read_bytes()==baseline
if consumer=='connection':dialog._base=dialog._values()
dialog.reject();QTest.qWait(30)
assert not busy() and not violations
(case_root/'RESULT.json').write_text(json.dumps(dict(passed=True,captures=captures,case=case,
    generated_marker_sha256=hashlib.sha256(marker.encode()).hexdigest(),marker_leaked=False,
    discovery_calls=len(calls),actual_server_or_model=False,source_component_only=True,blocked_operations=violations),indent=2)+'\n')
'''


@pytest.mark.parametrize('consumer', ['connection', 'manual'])
@pytest.mark.parametrize('scenario', ['success','invalid','error','echo','privacy','change','stale','save','queued','dispatch','cancel','close'])
@pytest.mark.parametrize('theme', ['light','dark'])
def test_generated_auth_consumers(selected_public_source,tmp_path,consumer,scenario,theme):
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen')
    result=subprocess.run([sys.executable,'-I','-B','-c',CHILD,str(selected_public_source),str(tmp_path),
        f'{consumer}-{scenario}-{theme}'],cwd=selected_public_source,env=env,capture_output=True,
        text=True,encoding='utf-8',errors='replace',timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (tmp_path/'STDOUT.log').write_text(result.stdout,encoding='utf-8')
    (tmp_path/'STDERR.log').write_text(result.stderr,encoding='utf-8')
    assert result.returncode==0,result.stderr[-2200:]
    receipt=json.loads((tmp_path/'RESULT.json').read_bytes())
    assert receipt['passed'] and not receipt['blocked_operations'] and not receipt['marker_leaked']
