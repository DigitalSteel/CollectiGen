"""Real source controllers/stock IO under unsigned synthetic crypto/native boundaries.

No signatures, keys, network or executable launch; no observation/service substitution.
"""
from time import monotonic,sleep
from pathlib import Path
from hashlib import sha256
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font
from suite_core.tests.test_encyclopedia_catalog_ui import click,settle
from suite_core.tests.test_encyclopedia_entry_actions_ui import help_tab,answer_modal
from PySide6.QtWidgets import QMessageBox
from tests.test_encyclopedia_enrollment import enrollment
from tests.test_encyclopedia_installation import no_external_actions
from tests.test_encyclopedia_entry_control import standalone,stock_spawn,connected,adapter_exchange
from tests.test_standalone_encyclopedia_update import add_schema_package
from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.encyclopedia.root_recovery_ui import create_original_enrollment_dialog
from suite_core.encyclopedia.__main__ import create_window


def dialog_settle(app,dialog):
    end=monotonic()+10
    while monotonic()<end:
        app.processEvents();dialog.poll()
        if not dialog.panel.close_held and dialog.handoff_future is None and dialog.cleanup_future is None:return
        sleep(.005)
    raise AssertionError(dialog.panel.status.text())


def press(button,app):
    assert button.isVisible() and button.isEnabled()
    QTest.mouseClick(button,Qt.LeftButton);app.processEvents()


def test_actual_enrollment_status_and_retry_save(app,content_font,enrollment,monkeypatch):
    f,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    store=adapter.installation.store;save=store.save
    try:
        press(dialog.panel.refresh,app);dialog_settle(app,dialog)
        press(dialog.panel.review,app);dialog_settle(app,dialog)
        monkeypatch.setattr(store,'save',lambda *a,**kw:(_ for _ in ()).throw(OSError('synthetic publication failure')))
        press(dialog.panel.confirm,app);press(dialog.panel.install,app);dialog_settle(app,dialog)
        selected=dialog.panel.installed.selected
        assert dialog.panel.publication_pending and not dialog.open_reader.isEnabled()
        press(dialog.panel.health_accept,app);dialog_settle(app,dialog)
        assert dialog.panel.publication_pending and dialog.panel.retry_save.isVisible()
        monkeypatch.setattr(store,'save',save)
        press(dialog.panel.retry_save,app);dialog_settle(app,dialog)
        assert dialog.panel.installed.selected==selected and not dialog.panel.publication_pending
        assert dialog.open_reader.isEnabled() and not owner._standalone_reader_creation_entered
        press(dialog.panel.recovery_confirm,app);press(dialog.panel.recover,app);dialog_settle(app,dialog)
    finally:
        dialog.close();dialog_settle(app,dialog)


def test_actual_first_install_recovery_from_visible_controls(app,content_font,enrollment):
    f,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    before=(f.sentinel.read_bytes(),f.database.read_bytes())
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        press(dialog.panel.refresh,app);dialog_settle(app,dialog)
        press(dialog.panel.review,app);dialog_settle(app,dialog)
        press(dialog.panel.confirm,app);press(dialog.panel.install,app);dialog_settle(app,dialog)
        installed=dialog.panel.installed.selected
        press(dialog.panel.health_accept,app);dialog_settle(app,dialog)
        press(dialog.panel.recovery_confirm,app);press(dialog.panel.recover,app);dialog_settle(app,dialog)
        assert dialog.panel.installed.selected is None and not dialog.open_reader.isEnabled()
        assert installed.program.exists() and (f.sentinel.read_bytes(),f.database.read_bytes())==before
        assert not owner._standalone_reader_creation_entered
    finally:
        dialog.close();dialog_settle(app,dialog)


@pytest.mark.parametrize('route',['recover','open'])
def test_actual_installed_close_refusal_preserves_settlement_routes(app,content_font,enrollment,route):
    f,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    before=(f.sentinel.read_bytes(),f.database.read_bytes());lease=owner.fd
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        press(dialog.panel.refresh,app);dialog_settle(app,dialog)
        press(dialog.panel.review,app);dialog_settle(app,dialog)
        press(dialog.panel.confirm,app);press(dialog.panel.install,app);dialog_settle(app,dialog)
        assert not dialog.close();dialog_settle(app,dialog)
        assert not dialog.cleanup_entered and not dialog.panel.closing
        assert dialog.open_reader.isEnabled() and dialog.panel.health_accept.isEnabled()
        assert owner._standalone_enrollment_finalization is None and owner.fd==lease
        if route=='recover':
            press(dialog.panel.recovery_confirm,app);press(dialog.panel.recover,app);dialog_settle(app,dialog)
            dialog.close();dialog_settle(app,dialog)
            assert dialog.released and executor.closed
        else:
            press(dialog.open_reader,app);dialog_settle(app,dialog)
            assert dialog.backend.owner is owner and executor.closed
            assert not owner._standalone_reader_creation_entered
        assert (f.sentinel.read_bytes(),f.database.read_bytes())==before
    finally:
        if not executor.closed:dialog.close();dialog_settle(app,dialog)


def test_actual_begun_close_finalization_keeps_choices_guarded(app,content_font,enrollment,monkeypatch):
    _,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    original=owner._close_standalone_custody
    from core.manager_launch_owner import LaunchHold
    monkeypatch.setattr(owner,'_close_standalone_custody',lambda *a:(_ for _ in ()).throw(LaunchHold('enrollment_close_waits_for_handoff_or_recovery')))
    try:
        dialog.close();dialog_settle(app,dialog)
        assert owner._standalone_enrollment_finalization is not None and dialog.cleanup_entered
        assert dialog.panel.closing and not dialog.open_reader.isEnabled()
        assert not dialog.panel.health_accept.isEnabled() and not dialog.resume_installation()
        monkeypatch.setattr(owner,'_close_standalone_custody',original)
        dialog.close();dialog_settle(app,dialog)
        assert dialog.released and executor.closed
    finally:
        monkeypatch.setattr(owner,'_close_standalone_custody',original)
        if not executor.closed:dialog.close();dialog_settle(app,dialog)


@pytest.mark.parametrize('fails',[False,True])
def test_actual_enrollment_dialog_ready_once_and_guarded_error(app,content_font,enrollment,fails):
    from suite_core.encyclopedia.root_recovery_ui import present_original_enrollment
    from PySide6.QtCore import QTimer
    _,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    executor=ResourceExecutorOwner();calls=[]
    def ready(dialog):
        assert dialog.isVisible() and dialog.panel.isVisible()
        calls.append(dialog)
        QTimer.singleShot(0,dialog.close)
        if fails:raise RuntimeError('synthetic dialog probe failure')
    if fails:
        with pytest.raises(RuntimeError,match='synthetic dialog probe failure'):
            present_original_enrollment(adapter,executor,dialog_ready=ready)
    else:assert present_original_enrollment(adapter,executor,dialog_ready=ready) is None
    assert len(calls)==1 and calls[0].released and executor.closed
    assert owner._standalone_enrollment_closed is not None


def test_actual_failed_target_health_visible_recovery(app,content_font,standalone,stock_spawn,monkeypatch):
    from tests.test_encyclopedia_entry_control import activate_over_control,exchange
    from suite_core.encyclopedia.root_holds import OriginalRootHold
    from suite_core.encyclopedia.root_recovery_ui import OriginalRootRecoveryService
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    boot,_,(root,client,_,_)=activate_over_control(standalone,stock_spawn,monkeypatch)
    before=standalone[0].sentinel.read_bytes();lease=boot.owner.fd
    exchange(root,client,'close');stock_spawn[1].update(code=17,members=0)
    with pytest.raises(ValueError,match='normal_control_reader_exit') as failure:root.after_reader_close()
    executor=ResourceExecutorOwner();hold=OriginalRootHold(boot,root,executor,failure.value)
    hold.close_acknowledged=True  # The real Close reply was received above.
    service=OriginalRootRecoveryService(hold)
    panel=EncyclopediaAcquisitionPanel(service,executor,recovery_only=True);panel.show();app.processEvents()
    def wait_panel():
        end=monotonic()+10
        while panel.close_held and monotonic()<end:app.processEvents();panel.poll();sleep(.005)
        assert not panel.close_held,panel.status.text()
    try:
        press(panel.health_accept,app);wait_panel()
        assert service.can_recover and not service.can_close
        press(panel.recovery_confirm,app);press(panel.recover,app);wait_panel()
        assert service.recovery_entered and boot.binding.current().selected.version=='0.1.0-alpha.8'
        assert boot.owner.fd==lease and standalone[0].sentinel.read_bytes()==before
        assert boot.owner.record['standalone_update']['phase']=='recovering'
        stock_spawn[1].update(code=None,members=1)
        prior_root,prior_client,_,_=connected(stock_spawn,monkeypatch)
        exchange(prior_root,prior_client,'close');stock_spawn[1].update(code=0,members=0)
        assert prior_root.after_reader_close().phase=='healthy' and boot.request is None
    finally:
        panel.timer.stop();panel.close();executor.begin_close()


def test_actual_enrollment_install_selection_and_open_boundary(app,content_font,enrollment):
    f,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    before=(f.sentinel.read_bytes(),f.database.read_bytes());lease=owner.fd
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        press(dialog.panel.refresh,app);dialog_settle(app,dialog)
        press(dialog.panel.review,app);dialog_settle(app,dialog)
        press(dialog.panel.confirm,app);press(dialog.panel.install,app);dialog_settle(app,dialog)
        selected=dialog.panel.installed.selected
        assert isinstance(selected.directory,Path) and selected.program.exists()
        assert str(selected.directory) in dialog.panel.details.toPlainText()
        assert not dialog.panel.publication_pending and dialog.open_reader.isEnabled()
        assert owner.fd==lease and not owner._standalone_reader_creation_entered
        assert (f.sentinel.read_bytes(),f.database.read_bytes())==before
        press(dialog.open_reader,app);dialog_settle(app,dialog)
        backend=dialog.backend
        assert backend.owner is owner and owner.fd==lease and backend.update_check.channel=='beta'
        assert not owner._standalone_reader_creation_entered and executor.closed
        with pytest.raises(Exception):adapter.installation.observe()
    finally:
        if not executor.closed:dialog.close();dialog_settle(app,dialog)


def test_actual_update_apply_and_recovery_via_visible_adapter(app,content_font,standalone,stock_spawn,monkeypatch):
    f,boot,_=standalone;before=f.sentinel.read_bytes();lease=boot.owner.fd
    add_schema_package(f,'0.1.0-alpha.9')
    root,client,_,_=connected(stock_spawn,monkeypatch);adapter_exchange(root,client)
    window=create_window(settings_path=f.settings,entry_control=client);window.resize(1920,1080);window.show();app.processEvents();help_tab(window,app)
    click(window,'Check for Updates');settle(app,window.catalog_host)
    answer_modal(app,QMessageBox.Yes);click(window,'Review Available Update');settle(app,window.catalog_host)
    request=boot.request;assert request is not None and boot.owner.record['standalone_update']['phase']=='waiting_for_close'
    assert not window.close();settle(app,window.catalog_host)
    assert root.close_intent
    stock_spawn[1].update(code=0,members=0)
    applied=root.after_reader_close()
    assert applied.installed.selected.version=='0.1.0-alpha.9' and boot.owner.fd==lease
    assert f.sentinel.read_bytes()==before
    import json
    before=b'{"corrected_after_target_apply":true,"future_schema":[1,2,3]}\n'
    f.sentinel.write_bytes(before)
    settings=json.loads(f.settings.read_text(encoding='utf-8'))
    settings['correction_after_target_apply']={'keep':True}
    f.settings.write_text(json.dumps(settings),encoding='utf-8')
    stock_spawn[1].update(code=None,members=1)
    target_root,target_client,_,_=connected(stock_spawn,monkeypatch);adapter_exchange(target_root,target_client)
    target=create_window(settings_path=f.settings,entry_control=target_client);target.resize(1920,1080);target.show();app.processEvents();help_tab(target,app)
    click(target,'Check Update / Recovery Status');settle(app,target.catalog_host)
    assert target.catalog_host.entry_status.phase=='applying' and boot.request is request
    answer_modal(app,QMessageBox.Yes);click(target,'Recover Interrupted Update');settle(app,target.catalog_host)
    assert target_root.after_close_operation=='recover'
    assert not target.close();settle(app,target.catalog_host)
    stock_spawn[1].update(code=0,members=0)
    recovered=target_root.after_reader_close()
    assert recovered.installed.selected.version=='0.1.0-alpha.8' and boot.owner.fd==lease
    assert f.sentinel.read_bytes()==before
    assert json.loads(f.settings.read_text(encoding='utf-8'))['correction_after_target_apply']=={'keep':True}
    stock_spawn[1].update(code=None,members=1)
    prior_root,prior_client,_,_=connected(stock_spawn,monkeypatch);adapter_exchange(prior_root,prior_client)
    prior=create_window(settings_path=f.settings,entry_control=prior_client);prior.show();app.processEvents()
    assert not prior.close();settle(app,prior.catalog_host)
    stock_spawn[1].update(code=0,members=0)
    healthy=prior_root.after_reader_close()
    assert healthy.phase=='healthy' and boot.request is None and 'standalone_update' not in boot.owner.record
    assert f.sentinel.read_bytes()==before
