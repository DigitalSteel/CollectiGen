"""Original install panel/enrollment dispatch with inert owner/service doubles."""
from types import SimpleNamespace
from dataclasses import replace
from threading import Event
from time import monotonic,sleep
from pathlib import Path
import os
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_acquisition_ui import queue_confirmation, toggle_details
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font
from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
from core.encyclopedia_installation import InstalledEncyclopedia,InstallationResult
from core.encyclopedia_connection import ReaderConnection
from core.encyclopedia_connection import ReaderConnectionStore,KEY
from core.encyclopedia_installation import EncyclopediaInstallation
from core.component_acquisition import ComponentAcquisition
from suite_core.discovery.component_staging import ComponentStageAdmission,MAX_ARCHIVE
from core.manager_launch_owner import DeploymentOwner
from suite_core.encyclopedia.bootstrap_entry import StandaloneEncyclopediaBootstrap
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.encyclopedia.root_recovery_ui import create_original_enrollment_dialog
from suite_core.encyclopedia import __main__ as entry,entry_binding,bootstrap_entry


class InstallService:
    install_available=True
    def __init__(self):
        self.calls=[];self.gate=self.entered=None
        self.current=InstalledEncyclopedia(None,'absent','a'*64,'b'*64,False)
        self.offer=SimpleNamespace(release=SimpleNamespace(software_version='0.2.0-alpha.901',
            artifact=SimpleNamespace(size_bytes=10,sha256='c'*64),key_id='synthetic-public-only',
            component_id=SimpleNamespace(value='collectigen.tag-encyclopedia')),locator=None,release_notes='Synthetic notes')
    def discover(self,*,channel,should_cancel):self.calls.append(('discover',channel));return (self.offer,)
    def observe(self):return self.current
    def review(self,offer):
        assert offer is self.offer;self.calls.append(('review',offer));self.last_review=SimpleNamespace(offer=offer,destination='synthetic/reader');return self.last_review
    def install(self,review,*,confirmed,should_cancel,progress):
        assert confirmed and review is self.last_review and review.offer is self.offer;self.calls.append(('install',review))
        if self.entered is not None:self.entered.set();assert self.gate.wait(3)
        if should_cancel():return InstallationResult(self.current,False,'Synthetic cancelled before install')
        self.current=InstalledEncyclopedia(ReaderConnection(Path('synthetic/reader').absolute(),'a'*64,self.offer.release.software_version),
            'activated','d'*64,'e'*64,False,False)
        return InstallationResult(self.current,True,'Synthetic exact connection saved')
    def reconcile_launch(self):return InstallationResult(self.current,True,'Synthetic status')


def fixture_adapter():
    adapter=object.__new__(StandaloneEncyclopediaEnrollment);adapter.channel='stable'
    adapter.owner=object.__new__(DeploymentOwner);adapter._reader_backend=None;adapter.installation=InstallService()
    calls=[];backend=object.__new__(StandaloneEncyclopediaBootstrap)
    def handoff():calls.append('handoff');adapter._reader_backend=backend;return backend
    adapter.handoff_reader=handoff;adapter.close=lambda:(calls.append('close') or 'f'*64)
    return adapter,calls,backend


def settle_dialog(app,dialog):
    end=monotonic()+5
    while monotonic()<end:
        app.processEvents();dialog.poll()
        if not dialog.panel.close_held and dialog.handoff_future is None and dialog.cleanup_future is None:break
        sleep(.005)
    assert not dialog.executor.close_held


def install_visible(app,dialog):
    assert dialog.panel.refresh.isEnabled()
    QTest.mouseClick(dialog.panel.refresh,Qt.LeftButton);settle_dialog(app,dialog)
    queue_confirmation()
    assert dialog.panel.install.isEnabled();QTest.mouseClick(dialog.panel.install,Qt.LeftButton)
    deadline=monotonic()+3
    while dialog.panel.operation=="Reviewing installation" and monotonic()<deadline:
        sleep(.002);QTest.qWait(10);dialog.poll()


def test_saved_standalone_open_remains_available_after_offline_refresh(app,content_font):
    adapter,calls,backend=fixture_adapter();executor=ResourceExecutorOwner()
    dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        install_visible(app,dialog);settle_dialog(app,dialog)
        installed=adapter.installation.current
        def unavailable(**kwargs):raise OSError('synthetic offline')
        adapter.installation.discover=unavailable
        QTest.mouseClick(dialog.panel.refresh,Qt.LeftButton);settle_dialog(app,dialog)
        assert dialog.panel.installed==installed and dialog.open_reader.isVisible() and dialog.open_reader.isEnabled()
        assert 'could not finish' in dialog.panel.status.text() and not calls
        QTest.mouseClick(dialog.open_reader,Qt.LeftButton);settle_dialog(app,dialog)
        assert dialog.backend is backend and calls==['handoff']
    finally:
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


def test_standalone_update_cancel_and_stale_review_need_new_confirmation(app,content_font):
    adapter,calls,_backend=fixture_adapter();service=adapter.installation;executor=ResourceExecutorOwner()
    dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        install_visible(app,dialog);settle_dialog(app,dialog)
        service.current=replace(service.current,phase='healthy')
        service.offer.release.software_version='0.2.0-alpha.902'
        QTest.mouseClick(dialog.panel.refresh,Qt.LeftButton);settle_dialog(app,dialog)
        assert dialog.panel.install.text()=='Update'
        queue_confirmation(False);QTest.mouseClick(dialog.panel.install,Qt.LeftButton);settle_dialog(app,dialog)
        assert service.current.selected.version=='0.2.0-alpha.901'
        cancelled_review=service.last_review;original_install=service.install;seen=[]
        def stale(review,**kwargs):
            assert review is service.last_review and kwargs['confirmed'];seen.append(review)
            raise ValueError('synthetic_stale_review')
        service.install=stale
        queue_confirmation();QTest.mouseClick(dialog.panel.install,Qt.LeftButton);settle_dialog(app,dialog)
        assert len(seen)==1 and seen[0] is not cancelled_review
        assert dialog.panel.offer is None and dialog.panel.reviewed is None
        assert service.current.selected.version=='0.2.0-alpha.901'
        service.install=original_install
        QTest.mouseClick(dialog.panel.refresh,Qt.LeftButton);settle_dialog(app,dialog)
        queue_confirmation();QTest.mouseClick(dialog.panel.install,Qt.LeftButton);settle_dialog(app,dialog)
        assert service.last_review is not seen[0] and service.current.selected.version=='0.2.0-alpha.902'
    finally:
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


def test_standalone_retry_save_never_reinstalls_or_opens_early(app,content_font):
    adapter,calls,_backend=fixture_adapter();service=adapter.installation;original_install=service.install
    def unsaved(review,**kwargs):
        result=original_install(review,**kwargs)
        service.current=replace(result.installed,connection_pending=True)
        return InstallationResult(service.current,False,'Synthetic save failure; original install retained')
    def retry():
        service.calls.append(('retry-save',None));service.current=replace(service.current,connection_pending=False)
        return InstallationResult(service.current,True,'Synthetic exact connection saved')
    service.install=unsaved;service.retry_connection=retry;executor=ResourceExecutorOwner()
    dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        install_visible(app,dialog);settle_dialog(app,dialog)
        assert dialog.panel.retry_save.isVisible() and dialog.panel.retry_save.isEnabled()
        assert dialog.panel.retry_save.property('primary') is True
        assert not dialog.open_reader.isEnabled() and not calls
        QTest.mouseClick(dialog.panel.retry_save,Qt.LeftButton);settle_dialog(app,dialog)
        assert sum(kind=='install' for kind,*_ in service.calls)==1
        assert sum(kind=='retry-save' for kind,*_ in service.calls)==1
        assert dialog.open_reader.isEnabled() and not calls
        assert dialog.open_reader.property('primary') is True
    finally:
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


def test_standalone_pending_launch_keeps_open_primary_and_holds_new_update(app,content_font):
    adapter,calls,_backend=fixture_adapter();executor=ResourceExecutorOwner()
    dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        install_visible(app,dialog);settle_dialog(app,dialog)
        adapter.installation.offer.release.software_version='0.2.0-alpha.902'
        QTest.mouseClick(dialog.panel.refresh,Qt.LeftButton);settle_dialog(app,dialog)
        assert not dialog.panel.install.isVisible() and not dialog.panel.install.isEnabled()
        assert dialog.open_reader.isVisible() and dialog.open_reader.isEnabled()
        assert dialog.open_reader.property('primary') is True and 'close its window normally' in dialog.panel.status.text()
        assert not calls
    finally:
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


@pytest.mark.parametrize('theme',['light','dark'])
def test_standalone_disclosure_geometry_and_cancelled_review(app,content_font,theme,tmp_path):
    from qt_app.theme import apply_theme
    from PySide6.QtGui import QFont, QPixmap, QPainter
    from PySide6.QtWidgets import QPushButton
    apply_theme(app,theme,False);app.setFont(QFont('Segoe UI',10))
    adapter,calls,_backend=fixture_adapter();executor=ResourceExecutorOwner()
    dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    output=Path(os.environ.get('ENCYCLOPEDIA_ACQUISITION_CAPTURE',str(tmp_path/'visuals')))
    output.mkdir(parents=True,exist_ok=True)
    def capture(name):
        canvas=QPixmap(1920,1080);canvas.fill(dialog.palette().window().color())
        painter=QPainter(canvas);painter.drawPixmap((1920-dialog.width())//2,(1080-dialog.height())//2,dialog.grab());painter.end()
        assert canvas.save(str(output/(theme+'-standalone-'+name+'.png')))
        for button in dialog.findChildren(QPushButton):
            if button.isVisible():
                position=button.mapTo(dialog,button.rect().topLeft())
                assert position.x()>=0 and position.x()+button.width()<=dialog.width()
                assert position.y()>=0 and position.y()+button.height()<=dialog.height()
    try:
        QTest.mouseClick(dialog.panel.refresh,Qt.LeftButton);settle_dialog(app,dialog)
        queue_confirmation(False);QTest.mouseClick(dialog.panel.install,Qt.LeftButton);settle_dialog(app,dialog)
        assert [x[0] for x in adapter.installation.calls]==['discover','review'] and not calls
        assert not dialog.panel.details.isVisible() and not dialog.open_reader.isVisible()
        capture('collapsed')
        toggle_details(dialog.panel.technical);assert dialog.panel.details.isVisible()
        capture('expanded')
        assert dialog.panel.status.text()=='Installation cancelled. Your installation is unchanged.'
        QTest.mouseClick(dialog.close_button,Qt.LeftButton);settle_dialog(app,dialog)
        assert calls==['close'] and executor.closed
    finally:
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


@pytest.mark.parametrize('theme',['light','dark'])
def test_visible_first_install_then_same_owner_handoff(app,content_font,theme,tmp_path):
    from qt_app.theme import apply_theme
    from PySide6.QtGui import QFont
    apply_theme(app,theme,False);app.setFont(QFont('Segoe UI',10))
    adapter,calls,backend=fixture_adapter();executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor)
    dialog.show();app.processEvents()
    try:
        assert not dialog.open_reader.isEnabled() and not calls
        install_visible(app,dialog);settle_dialog(app,dialog)
        assert adapter.installation.calls[0]==('discover','stable') and not calls
        assert dialog.open_reader.isEnabled()
        assert 'Open the reader' in dialog.panel.status.text()
        assert 'Manager' not in dialog.panel.status.text()
        captures=tmp_path/'visuals'
        captures.mkdir(parents=True,exist_ok=True)
        assert dialog.grab().save(str(captures/('enrollment-'+theme+'.png')))
        QTest.mouseClick(dialog.open_reader,Qt.LeftButton);settle_dialog(app,dialog)
        assert dialog.backend is backend and calls==['handoff'] and executor.closed
        assert not dialog.isVisible()
    finally:
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


def test_visible_close_during_install_drains_and_cleans_original_owner(app,content_font):
    adapter,calls,_backend=fixture_adapter();adapter.installation.entered=Event();adapter.installation.gate=Event()
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        install_visible(app,dialog);assert adapter.installation.entered.wait(1)
        assert not dialog.close() and executor.close_held and not calls
        adapter.installation.gate.set();settle_dialog(app,dialog)
        assert calls==['close'] and dialog.released and executor.closed and not dialog.isVisible()
    finally:
        adapter.installation.gate.set()
        if not executor.closed:dialog.close();settle_dialog(app,dialog)


def test_close_during_handoff_does_not_launch_or_release_then_requires_explicit_continue(app,content_font):
    adapter,calls,backend=fixture_adapter();entered=Event();gate=Event();original=adapter.handoff_reader
    def held():entered.set();assert gate.wait(3);return original()
    adapter.handoff_reader=held;executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor)
    dialog.show();app.processEvents()
    try:
        install_visible(app,dialog);settle_dialog(app,dialog)
        QTest.mouseClick(dialog.open_reader,Qt.LeftButton);assert entered.wait(1)
        assert not dialog.close();gate.set();settle_dialog(app,dialog)
        assert dialog.backend is backend and dialog.isVisible() and not executor.closed and calls==['handoff']
        assert not dialog.panel.refresh.isEnabled() and not dialog.panel.install.isEnabled()
        assert not dialog.close() and calls==['handoff']
        QTest.mouseClick(dialog.open_reader,Qt.LeftButton);app.processEvents()
        assert not dialog.isVisible() and executor.closed and calls==['handoff']
    finally:
        gate.set()
        if not executor.closed:executor.begin_close()


def test_actual_entry_dispatch_selects_enrollment_without_tuple_unpack(monkeypatch):
    adapter,calls,_backend=fixture_adapter();seen=[]
    monkeypatch.setattr(bootstrap_entry,'PROFILE',SimpleNamespace(channel='stable'))
    monkeypatch.setattr(entry_binding,'start_compiled_control',lambda:adapter)
    def enrollment(value,*,executor,startup_error):
        assert not executor.close_held and not executor.closed and startup_error is None
        seen.append(value);executor.begin_close();return 7
    monkeypatch.setattr(entry,'_run_original_enrollment',enrollment)
    monkeypatch.setattr(entry,'_run_original_root',lambda *args:pytest.fail('installed tuple route entered'))
    assert entry.run([])==7 and seen==[adapter] and not calls


def test_presentation_failure_closes_unhanded_original_setup_on_worker(monkeypatch):
    from suite_core.encyclopedia import root_recovery_ui
    adapter,calls,_backend=fixture_adapter();failure=RuntimeError('synthetic presenter construction failure')
    def failed(*args):raise failure
    monkeypatch.setattr(root_recovery_ui,'present_original_enrollment',failed)
    with pytest.raises(RuntimeError,match='presenter construction'):entry._run_original_enrollment(adapter)
    assert calls==['close']


def test_completed_install_handoff_uses_existing_start_reader_not_new_owner(monkeypatch):
    from suite_core.encyclopedia import root_recovery_ui
    adapter,calls,backend=fixture_adapter();seen=[];control=object()
    monkeypatch.setattr(root_recovery_ui,'present_original_enrollment',lambda a,e:backend)
    monkeypatch.setattr(entry_binding,'start_original_reader',lambda b:(seen.append(b) or (b,control)))
    def service(b,c,*,executor,progress):
        assert not executor.close_held and not executor.closed
        executor.begin_close();progress.finish();return b,c
    monkeypatch.setattr(entry,'_run_original_root',service)
    assert entry._run_original_enrollment(adapter)==(backend,control) and seen==[backend] and not calls


def test_reader_start_failure_retains_same_backend_without_enrollment_close(monkeypatch):
    from suite_core.encyclopedia import root_recovery_ui
    adapter,calls,backend=fixture_adapter();seen=[]
    monkeypatch.setattr(root_recovery_ui,'present_original_enrollment',lambda a,e:backend)
    def failed(value):assert value is backend;raise RuntimeError('synthetic reader admission failure')
    monkeypatch.setattr(entry_binding,'start_original_reader',failed)
    def held(hold):
        assert hold.backend is backend and hold.control is None and not hold.executor.closed
        seen.append(hold);return 9
    monkeypatch.setattr(entry.OriginalRootHold,'present',held)
    assert entry._run_original_enrollment(adapter)==9 and not calls and len(seen)==1
    seen[0].executor.begin_close()


def test_failed_presentation_cleanup_retains_original_owner(monkeypatch):
    from suite_core.encyclopedia import root_recovery_ui
    adapter,calls,_backend=fixture_adapter();seen=[]
    def failed(*args):raise RuntimeError('synthetic presentation failure')
    def close_failed():calls.append('close');raise RuntimeError('synthetic original cleanup hold')
    adapter.close=close_failed
    monkeypatch.setattr(root_recovery_ui,'present_original_enrollment',failed)
    def held(hold):
        assert hold.owner is adapter.owner and not hold.executor.close_held
        seen.append(hold);return 10
    monkeypatch.setattr(entry.OriginalDeploymentHold,'present',held)
    assert entry._run_original_enrollment(adapter)==10 and calls==['close'] and len(seen)==1
    seen[0].executor.begin_close()


def original_installation(tmp_path):
    cache=tmp_path/'actual-cache';cache.mkdir()
    def deny(*args,**kwargs):raise AssertionError('No network or native transaction allowed in real-type UI fixture')
    acquisition=ComponentAcquisition(cache_root=cache,cache_owner='real-type-ui-fixture',opener=deny,
        host_version='0.2.0-alpha.1',max_artifact_bytes=MAX_ARCHIVE)
    store=ReaderConnectionStore(tmp_path/'synthetic-settings.json')
    return EncyclopediaInstallation(acquisition,store,admission=ComponentStageAdmission(deny))


def test_real_installation_service_constructs_panel_without_invented_capability_attributes(app,tmp_path,content_font):
    adapter,calls,_backend=fixture_adapter();adapter.installation=original_installation(tmp_path)
    assert type(adapter.installation) is EncyclopediaInstallation and not hasattr(adapter.installation,'can_recover')
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        assert dialog.panel.service is adapter.installation
        assert not dialog.panel.recover.isVisible() and not dialog.panel.health_accept.isVisible()
        toggle_details(dialog.panel.technical)
        assert not dialog.panel.health_accept.isVisible()
        assert not dialog.open_reader.isEnabled() and not calls
    finally:dialog.close();settle_dialog(app,dialog)


def test_real_reader_connection_path_delivery_and_pending_recovery(app,tmp_path,content_font,monkeypatch):
    import json
    adapter,calls,_backend=fixture_adapter();service=original_installation(tmp_path);adapter.installation=service
    directory=tmp_path/'reader';directory.mkdir()
    selected=ReaderConnection(directory,'a'*64,'0.2.0-alpha.901')
    # Exact synthetic saved-settings fixture, never a live install or user catalog.
    service.store.settings.path.write_text(json.dumps({KEY:selected.mapping()}),encoding='utf-8')
    installed=InstalledEncyclopedia(selected,'activated','b'*64,'c'*64,False,False)
    state={'installed':installed}
    # Only native filesystem observation is doubled; real service/reconciliation/DTO/store/panel execute.
    monkeypatch.setattr(service,'observe',lambda:state['installed'])
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    try:
        toggle_details(dialog.panel.technical)
        QTest.mouseClick(dialog.panel.health_accept,Qt.LeftButton);settle_dialog(app,dialog)
        assert dialog.panel.installed is installed and not dialog.panel.publication_pending
        assert str(directory) in dialog.panel.details.toPlainText()
        assert 'could not finish' not in dialog.panel.status.text() and dialog.open_reader.isEnabled()
        assert dialog.panel.status.text()=='Open the reader, then close its window normally to finish setup.'
        state['installed']=InstalledEncyclopedia(selected,'interrupted','d'*64,'e'*64,True,False)
        toggle_details(dialog.panel.technical)
        QTest.mouseClick(dialog.panel.health_accept,Qt.LeftButton);settle_dialog(app,dialog)
        assert dialog.panel.recover.isVisible() and dialog.panel.recovery_confirm.isVisible()
        assert not dialog.open_reader.isEnabled()
        QTest.mouseClick(dialog.panel.recovery_confirm,Qt.LeftButton)
        assert dialog.panel.recover.isEnabled() and 'needs attention' in dialog.panel.status.text()
        assert 'needs recovery' in dialog.panel.details.toPlainText()
    finally:dialog.close();settle_dialog(app,dialog)


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('connection_saved,pending,connection_pending,phase',[
    (False,False,True,'activated'),(True,True,False,'activated'),
    (True,False,False,'healthy'),(True,True,False,'interrupted'),
])
def test_standalone_status_preserves_connection_and_recovery_reasons(
        app,content_font,connection_saved,pending,connection_pending,phase,theme):
    from qt_app.theme import apply_theme
    apply_theme(app,theme)
    adapter,calls,_backend=fixture_adapter()
    selected=ReaderConnection(Path('synthetic/reader').absolute(),'a'*64,'0.2.0-alpha.901')
    installed=InstalledEncyclopedia(selected,phase,'b'*64,'c'*64,pending,connection_pending)
    reason='Synthetic connection or recovery requirement; preserve exact diagnostic.'
    adapter.installation.reconcile_launch=lambda:InstallationResult(installed,connection_saved,reason)
    executor=ResourceExecutorOwner();dialog=create_original_enrollment_dialog(adapter,executor)
    dialog.show();app.processEvents()
    try:
        toggle_details(dialog.panel.technical)
        QTest.mouseClick(dialog.panel.health_accept,Qt.LeftButton);settle_dialog(app,dialog)
        assert reason in dialog.panel.details.toPlainText()
        if not connection_saved or pending or connection_pending:
            assert reason not in dialog.panel.status.text()
            assert ('Retry Save' in dialog.panel.status.text()
                    or 'needs attention' in dialog.panel.status.text())
        assert "Manager" not in dialog.panel.status.text()
        output=os.environ.get('ENCYCLOPEDIA_PRESENTATION_CAPTURE')
        if output:
            dest=Path(output);dest.mkdir(parents=True,exist_ok=True)
            assert dialog.grab().save(str(dest/f'{theme}-status-{phase}-{int(connection_saved)}-{int(pending)}-{int(connection_pending)}.png'))
        assert dialog.panel.installed is installed and not calls
    finally:dialog.close();settle_dialog(app,dialog)
