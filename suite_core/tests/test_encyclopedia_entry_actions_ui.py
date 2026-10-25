"""Visible reader actions over exact adapter and doubled original RPC endpoint."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import pytest
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox
from suite_core.tests.test_encyclopedia import app,fixture
from suite_core.tests.test_encyclopedia_catalog_ui import click,settle
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font
from suite_core.encyclopedia.__main__ import create_window
from suite_core.encyclopedia.control_entry import EncyclopediaChildControl


def child_endpoint():
    child=object.__new__(EncyclopediaChildControl)
    child.outstanding=None;child.outstanding_sent=False;child.wire=SimpleNamespace(channel=SimpleNamespace(read_fault=None))
    calls=[];state={'phase':None,'pin':None,'fail_confirm':False,'held_status':False}
    available=dict(status='available',reason='signed fixture notice',version='0.2.0-alpha.902',manifest_sha256='a'*64,
        package_sha256='b'*64,notes='Synthetic update notes',route='standalone')
    def request(operation,body,*,overall_timeout):
        calls.append((operation,dict(body)))
        if operation=='check':return available
        if operation=='confirm':
            if state['fail_confirm']:
                child.outstanding={'id':'1'*32,'operation':operation,'body':dict(body)};child.outstanding_sent=True
                raise TimeoutError('synthetic late original reply')
            state.update(phase='waiting_for_close',pin='c'*64);return dict(status='waiting_for_close',request_sha256='c'*64)
        if operation=='status':
            if state['held_status']:return dict(status='held',reason='synthetic retained hold',operation='status')
            return dict(status='idle' if state['pin'] is None else 'waiting',phase=state['phase'],current_version='0.2.0-alpha.901',request_sha256=state['pin'])
        if operation=='cancel':state.update(phase=None,pin=None);return dict(status='cancelled_before_apply')
        if operation=='recover':return dict(status='recovery_waiting_for_close')
        if operation=='retry-connection':return dict(status='saved')
        if operation=='close':return dict(status='close_requested')
        raise AssertionError(operation)
    def resume(*,overall_timeout):
        assert child.outstanding['id']=='1'*32 and child.outstanding['body']=={'manifest_sha256':'a'*64}
        calls.append(('resume',dict(child.outstanding['body'])));child.outstanding=None;child.outstanding_sent=False
        state.update(phase='waiting_for_close',pin='c'*64);return dict(status='waiting_for_close',request_sha256='c'*64)
    child.request=request;child.resume_outstanding=resume
    return child,calls,state


def help_tab(window,app):
    tabs=window.bar.tabs;index=next(i for i in range(tabs.count()) if tabs.tabData(i)=='Help')
    QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(index).center());app.processEvents()


def answer_modal(app,answer):
    def answer_now():
        modals=[w for w in app.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible()]
        if not modals:
            QTimer.singleShot(10,answer_now)
            return
        modal=modals[0]
        QTest.mouseClick(modal.button(answer),Qt.LeftButton)
    QTimer.singleShot(0,answer_now)


def close_reader(window,app):
    window.close();settle(app,window.catalog_host)


@pytest.mark.parametrize('theme',['light','dark'])
def test_visible_confirm_cancel_and_request_identity(app,tmp_path,content_font,theme):
    from qt_app.theme import apply_theme
    from PySide6.QtGui import QFont
    apply_theme(app,theme,False);app.setFont(QFont('Segoe UI',10))
    child,calls,state=child_endpoint();window=create_window(settings_path=tmp_path/'settings.json',entry_control=child)
    window.resize(1920,1080);window.show();app.processEvents();help_tab(window,app);host=window.catalog_host
    try:
        click(window,'Check for Updates');settle(app,host)
        notice=host.entry_notice;assert notice is host.entry_check.retained_notice
        answer_modal(app,QMessageBox.No);click(window,'Review Available Update');assert [op for op,_ in calls]==['check']
        answer_modal(app,QMessageBox.Yes);click(window,'Review Available Update');settle(app,host)
        assert calls[-1]==('confirm',{'manifest_sha256':notice.manifest_sha256})
        request=host.entry_request;assert request is host.entry_check.pending_request
        assert 'Close this reader normally' in window.status.text()
        captures=Path('.tmp/encyclopedia-root-replacement-20261012/entry-ui44/visuals');captures.mkdir(parents=True,exist_ok=True)
        assert window.grab().save(str(captures/('update-'+theme+'.png')))
        assert not host.review_entry_update()
        click(window,'Cancel Pending Update');settle(app,host)
        assert calls[-1]==('cancel',{'request_sha256':request.request_sha256}) and host.entry_request is None
        assert not (tmp_path/'settings.json').exists()
    finally:close_reader(window,app)


def test_visible_authenticated_status_recovery_and_save_retry(app,tmp_path,content_font):
    child,calls,state=child_endpoint();state.update(phase='applying',pin='d'*64)
    window=create_window(settings_path=tmp_path/'settings.json',entry_control=child);window.resize(1920,1080);window.show();app.processEvents();help_tab(window,app);host=window.catalog_host
    try:
        click(window,'Check Update / Recovery Status');settle(app,host)
        retained=host.entry_request;assert retained is host.entry_check.pending_request
        host.entry_request=replace(retained)
        assert not host.retry_entry_connection();host.entry_request=retained
        click(window,'Retry Saving Reader Connection');settle(app,host)
        assert calls[-1]==('retry-connection',{'request_sha256':'d'*64})
        answer_modal(app,QMessageBox.No);click(window,'Recover Interrupted Update');assert calls[-1][0]=='retry-connection'
        answer_modal(app,QMessageBox.Yes);click(window,'Recover Interrupted Update');settle(app,host)
        assert calls[-1]==('recover',{'request_sha256':'d'*64}) and host.entry_recovery_confirmed
        assert not host.recover_entry_update()
        state['held_status']=True;click(window,'Check Update / Recovery Status');settle(app,host)
        assert host.entry_request is retained and 'held' in window.status.text()
    finally:close_reader(window,app)


def test_late_confirm_reply_resumes_same_request_during_close(app,tmp_path,content_font):
    child,calls,state=child_endpoint();state['fail_confirm']=True
    window=create_window(settings_path=tmp_path/'settings.json',entry_control=child);window.resize(1920,1080);window.show();app.processEvents();help_tab(window,app);host=window.catalog_host
    try:
        click(window,'Check for Updates');settle(app,host)
        answer_modal(app,QMessageBox.Yes);click(window,'Review Available Update')
        for _ in range(100):app.processEvents();QTest.qWait(5)
        assert host.entry_action_error and child.outstanding is not None
        QTest.mouseClick(window.cancel_work_button,Qt.LeftButton);app.processEvents()
        assert 'reply is still retained' in window.status.text() and child.outstanding['id']=='1'*32
        assert not window.close() and host._closing
        click(window,'Resume Pending Update Request')
        settle(app,host)
        assert [op for op,_ in calls].count('confirm')==1 and [op for op,_ in calls].count('resume')==1
        assert host.entry_check.close_admitted and host.executor.closed
    finally:
        if not host.executor.closed:close_reader(window,app)


def test_unimplemented_rpc_actions_stay_unavailable(app,tmp_path,monkeypatch):
    from core.encyclopedia_entry_adapter import EncyclopediaEntryUpdateCheck
    child,_calls,_state=child_endpoint();monkeypatch.setattr(EncyclopediaEntryUpdateCheck,'confirm_update',None)
    window=create_window(settings_path=tmp_path/'settings.json',entry_control=child)
    try:assert not window.catalog_host.entry_actions_available() and not window.catalog_host.entry_commands()
    finally:close_reader(window,app)


def test_saved_reader_feedback_preserves_observation_hold(app,tmp_path,monkeypatch):
    window=create_window(settings_path=tmp_path/'settings.json');host=window.catalog_host;publication=fixture()
    host.controller.choices=(SimpleNamespace(native_adapter=SimpleNamespace(root=tmp_path,directory_pin=(1,2)),transactions=SimpleNamespace(destination=tmp_path/'snapshot.json')),)
    host.controller.index=0;host.controller.result=SimpleNamespace(publication=publication)
    host.controller.error='synthetic post-save observation hold'
    monkeypatch.setattr(window,'open_publication',lambda *args,**kwargs:True)
    try:
        host.poll();assert 'observations are held' in window.status.text() and 'do not apply again' in window.status.text()
        assert window.status.toolTip()==host.controller.error
    finally:close_reader(window,app)
