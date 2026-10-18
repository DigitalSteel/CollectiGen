"""Visible source recovery with original-type backend doubles; no native actors."""
from types import SimpleNamespace
from time import monotonic,sleep
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog,QPushButton
from suite_core.encyclopedia import __main__ as entry,entry_binding,bootstrap_entry
from suite_core.encyclopedia.bootstrap_entry import StandaloneEncyclopediaBootstrap
from core.encyclopedia_connection import COMPONENT


@pytest.mark.parametrize('fails',[False,True])
def test_root_dialog_ready_visible_once_error_waits_for_guarded_close(qt_test_scope,tmp_path,monkeypatch,fails):
    from PySide6.QtCore import QTimer
    from suite_core.encyclopedia.root_holds import OriginalRootHold
    from suite_core.encyclopedia.root_recovery_ui import present_original_root_hold
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    backend=object.__new__(StandaloneEncyclopediaBootstrap);backend.request=None;backend.subject=object()
    backend.owner=SimpleNamespace(record={},_standalone_encyclopedia_owned=lambda subject:None)
    backend.binding=SimpleNamespace(root=tmp_path,current=lambda:SimpleNamespace(phase='healthy'))
    calls=[];backend.close=lambda:calls.append('original_close')
    monkeypatch.setattr(StandaloneEncyclopediaBootstrap,'close_held',property(lambda self:False))
    control=SimpleNamespace(close_intent=True,actor=SimpleNamespace(channel=SimpleNamespace(read_fault=None,write_fault=None)))
    hold=OriginalRootHold(backend,control,ResourceExecutorOwner(),RuntimeError('synthetic held root'))
    hold.close_acknowledged=True;seen=[]
    timer=QTimer();timer.setInterval(5)
    def ready(dialog):
        assert dialog.isVisible() and hold.panel.isVisible()
        seen.append(dialog)
        def close_when_settled():
            button=dialog.findChild(QPushButton,'encyclopediaRootRecoveryClose')
            if not hold.panel.close_held and hold.panel.service.can_close and button.isEnabled():
                timer.stop();QTest.mouseClick(button,Qt.LeftButton)
        timer.timeout.connect(close_when_settled);timer.start()
        if fails:raise RuntimeError('synthetic root callback error')
    try:
        if fails:
            with pytest.raises(RuntimeError,match='synthetic root callback error'):
                present_original_root_hold(hold,dialog_ready=ready)
        else:assert present_original_root_hold(hold,dialog_ready=ready)==0
        assert len(seen)==1 and calls==['original_close'] and hold.released.is_set() and hold.executor.closed
    finally:timer.stop()


def test_root_callback_failure_early_dialog_exit_waits_then_reports(qt_test_scope,tmp_path,monkeypatch):
    from threading import Thread,Event
    from PySide6.QtWidgets import QApplication
    from suite_core.encyclopedia.root_holds import OriginalRootHold
    from suite_core.encyclopedia.root_recovery_ui import present_original_root_hold
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    backend=object.__new__(StandaloneEncyclopediaBootstrap);backend.request=None;backend.subject=object()
    backend.owner=SimpleNamespace(record={},_standalone_encyclopedia_owned=lambda subject:None)
    backend.binding=SimpleNamespace(root=tmp_path,current=lambda:SimpleNamespace(phase='healthy'))
    calls=[];backend.close=lambda:calls.append('original_close')
    monkeypatch.setattr(StandaloneEncyclopediaBootstrap,'close_held',property(lambda self:False))
    control=SimpleNamespace(close_intent=True,actor=SimpleNamespace(channel=SimpleNamespace(read_fault=None,write_fault=None)))
    hold=OriginalRootHold(backend,control,ResourceExecutorOwner(),RuntimeError('synthetic held root'))
    hold.close_acknowledged=True;waiting=Event();worker=None;original_wait=hold.wait;dialogs=[]
    def wait_original():
        assert not hold.released.is_set()
        waiting.set();return original_wait()
    monkeypatch.setattr(hold,'wait',wait_original)
    def ready(dialog):
        nonlocal worker
        dialogs.append(dialog)
        def settle_original():
            assert waiting.wait(5)
            end=monotonic()+5
            while hold.executor.close_held and monotonic()<end:sleep(.005)
            hold.release_after_recovery(confirmed=True)
        worker=Thread(target=settle_original);worker.start()
        QApplication.exit(0)  # Event-loop exit cannot imply original release.
        raise RuntimeError('retained callback failure after early exit')
    try:
        with pytest.raises(RuntimeError,match='retained callback failure after early exit'):
            present_original_root_hold(hold,dialog_ready=ready)
        assert waiting.is_set() and hold.released.is_set() and calls==['original_close']
        assert hold.executor.closed
    finally:
        if worker is not None:worker.join(5);assert not worker.is_alive()
        # QApplication.exit leaves the test window visible; dispose only after
        # the original qualified release asserted above, not to settle ownership.
        if hold.released.is_set():
            for dialog in dialogs:dialog.hide()
            # This test deliberately exits Qt globally outside app.exec().
            # Restore only that test-owned exit after original release proof.
            from PySide6.QtCore import QTimer
            restored = []
            def restore_exit_state():
                restored.append('application loop delivered cleanup')
                QApplication.exit(0)
            QTimer.singleShot(0, restore_exit_state)
            QApplication.instance().exec()
            assert restored == ['application loop delivered cleanup']


@pytest.mark.parametrize('mode',['retry','fault','uncertain_ack','recover','finishhealth'])
@pytest.mark.parametrize('theme',['light','dark'])
def test_actual_entry_visible_recovery_retry_and_guarded_close(qt_test_scope,tmp_path,monkeypatch,mode,theme):
    from pathlib import Path
    from qt_app.theme import apply_theme
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFontDatabase,QFont
    font_id=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id>=0
    apply_theme(QApplication.instance(),theme,False)
    QApplication.instance().setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0],10))
    captures=Path('.tmp/encyclopedia-root-replacement-20261012/recovery-captures04')
    captures.mkdir(parents=True,exist_ok=True)
    scope=qt_test_scope;calls=[];flags={'held':True}
    backend=object.__new__(StandaloneEncyclopediaBootstrap)
    backend.request=object();backend.subject=object()
    backend.owner=SimpleNamespace(record={'standalone_update':{'phase':'applying'}},
        _standalone_encyclopedia_owned=lambda subject:None)
    selected=SimpleNamespace(version='0.1.2')
    backend.binding=SimpleNamespace(root=tmp_path,current=lambda:SimpleNamespace(
        phase='activated' if flags['held'] else 'healthy',selected=selected))
    backend.poll_reader=lambda:True
    backend.close=lambda:calls.append('close')
    monkeypatch.setattr(StandaloneEncyclopediaBootstrap,'close_held',property(lambda self:flags['held']))
    pending=tmp_path/'State'/COMPONENT.value/'connection-pending.json'
    pending.parent.mkdir(parents=True);pending.write_text('{}',encoding='utf-8')
    def retry(request):
        assert request is backend.request
        calls.append('retry');pending.unlink()
        # Inert double for independently verified original health/settlement.
        backend.owner.record={};backend.request=None;flags['held']=False
    backend.retry_connection=retry
    def recover(request,*,confirmed=False):
        assert confirmed and request is backend.request
        calls.append('recover');pending.unlink()
        backend.owner.record['standalone_update']['phase']='recovering'
    backend.recover_update=recover
    if mode=='finishhealth':
        from threading import Event
        from suite_core.encyclopedia import control_entry
        normal_close=Event();reader_started=Event()
        backend.request=SimpleNamespace(current=SimpleNamespace(selected=selected))
        original_request=backend.request
        def settle(subject,request):
            assert normal_close.is_set() and subject is backend.subject and request is original_request
            calls.append('health');flags['held']=False;backend.owner.record={}
            return backend.binding.current()
        backend.owner.settle_standalone_encyclopedia_update=settle
        backend.launch_current=lambda:calls.append('launch')
        successor=SimpleNamespace(close_intent=False,
            actor=SimpleNamespace(channel=SimpleNamespace(read_fault=None)))
        def ready():
            calls.append('ready');reader_started.set()
        def handle():
            assert normal_close.wait(5)
            return {'status':'close_requested'}
        successor.accept_ready=ready;successor.handle_next=handle
        successor.after_reader_close=lambda:backend.finish_recovery_health(original_request)
        monkeypatch.setattr(control_entry,'EncyclopediaRootControl',lambda original:successor)
    control=SimpleNamespace(close_intent=False,
        actor=SimpleNamespace(channel=SimpleNamespace(read_fault={'bytes_received':1} if mode=='fault' else None,
            write_fault={'bytes_written':1} if mode=='uncertain_ack' else None)))
    def fail():
        calls.append('settle');raise RuntimeError('Synthetic saved connection failure')
    control.after_reader_close=fail
    def handle_close():
        control.close_intent=True
        if mode=='uncertain_ack':
            from core.manager_launch_owner import LaunchHold
            calls.append('uncertain_ack')
            raise LaunchHold('control_channel_write_timeout')
        return {'status':'close_requested'}
    control.handle_next=handle_close
    monkeypatch.setattr(bootstrap_entry,'PROFILE',object())
    monkeypatch.setattr(entry_binding,'start_compiled_control',lambda:(backend,control))
    def until(predicate):
        deadline=monotonic()+3
        while not predicate() and monotonic()<deadline:
            scope.settle(1);sleep(.005)
        assert predicate()
    def visible(dialog):
        dialog.show();scope.settle()
        hold=entry._HELD_ENTRIES[-1];panel=hold.panel
        until(lambda:panel.future is None and not hold.executor.close_held)
        assert not panel.install.isVisible() and not panel.refresh.isVisible()
        close=dialog.findChild(QPushButton,'encyclopediaRootRecoveryClose')
        assert dialog.grab().save(str(captures/f'{theme}-{mode}-held.png'))
        assert not close.isEnabled()
        QTest.keyClick(dialog,Qt.Key_Escape)
        assert dialog.isVisible() and not hold.released.is_set()
        assert not dialog.close() and dialog.isVisible()
        if mode in ('fault','uncertain_ack'):
            QTest.mouseClick(panel.health_accept,Qt.LeftButton)
            until(lambda:panel.future is None and not hold.executor.close_held)
            assert not panel.retry_save.isEnabled() and not panel.recover.isEnabled()
            assert 'unavailable' in panel.status.text()
            assert dialog.grab().save(str(captures/f'{theme}-unavailable.png'))
            assert calls==(['uncertain_ack'] if mode=='uncertain_ack' else ['settle'])
            if mode=='uncertain_ack':assert not hold.close_acknowledged
            # Dispose this synthetic modal only; original hold remains unreleased.
            dialog.hide();return 0
        if mode in ('recover','finishhealth'):
            assert not panel.recover.isEnabled()
            QTest.mouseClick(panel.recovery_confirm,Qt.LeftButton)
            assert panel.recover.isEnabled()
            QTest.mouseClick(panel.recover,Qt.LeftButton)
            until(lambda:panel.future is None and not hold.executor.close_held)
            assert not panel.recover.isEnabled() and not close.isEnabled()
            assert 'health' in panel.status.text() and calls==['settle','recover']
            if mode=='finishhealth':
                assert panel.run_recovery_reader.isEnabled()
                QTest.mouseClick(panel.run_recovery_reader,Qt.LeftButton)
                until(reader_started.is_set)
                assert panel.close_held and not close.isEnabled()
                # Visible inert reader substitute; original native health remains a double.
                reader=QDialog(dialog)
                from PySide6.QtWidgets import QVBoxLayout
                layout=QVBoxLayout(reader);ordinary=QPushButton('Close reader normally')
                layout.addWidget(ordinary)
                def closed():
                    successor.close_intent=True;normal_close.set();reader.hide()
                ordinary.clicked.connect(closed);reader.show();scope.settle()
                QTest.mouseClick(ordinary,Qt.LeftButton)
                until(lambda:panel.future is None and not hold.executor.close_held)
                until(close.isEnabled)
                assert backend.request is None and 'settled' in panel.status.text()
                QTest.mouseClick(close,Qt.LeftButton)
                assert hold.released.is_set() and not dialog.isVisible()
                return 0
            assert dialog.grab().save(str(captures/f'{theme}-recover-health-pending.png'))
            dialog.hide();return 0
        assert panel.retry_save.isEnabled()
        QTest.mouseClick(panel.retry_save,Qt.LeftButton)
        until(lambda:panel.future is None and not hold.executor.close_held)
        until(close.isEnabled)
        assert 'settled' in panel.status.text()
        assert dialog.grab().save(str(captures/f'{theme}-settled.png'))
        QTest.mouseClick(close,Qt.LeftButton)
        assert not dialog.isVisible() and hold.released.is_set()
        return 0
    monkeypatch.setattr(QDialog,'exec',visible)
    if mode not in ('retry','finishhealth'):
        monkeypatch.setattr(entry.OriginalRootHold,'wait',lambda self:1)
    before=len(entry._HELD_ENTRIES)
    try:
        assert entry.run([])==(0 if mode in ('retry','finishhealth') else 1)
        assert calls==(['settle','retry','close'] if mode=='retry' else
            ['settle','recover','launch','ready','health','close'] if mode=='finishhealth' else
            ['settle','recover'] if mode=='recover' else
            ['uncertain_ack'] if mode=='uncertain_ack' else ['settle'])
    finally:
        for hold in entry._HELD_ENTRIES[before:]:
            if getattr(hold,'panel',None) is not None:hold.panel.timer.stop()
            if getattr(hold,'dialog',None) is not None:hold.dialog.deleteLater()
        del entry._HELD_ENTRIES[before:]
        QFontDatabase.removeApplicationFont(font_id)


def test_factory_failure_shows_retained_original_owner_without_cleanup_claim(qt_test_scope,monkeypatch):
    from core.manager_launch_owner import DeploymentOwner
    owner=object.__new__(DeploymentOwner)
    def fail():
        error=RuntimeError('Synthetic custody-ready factory failure')
        error.encyclopedia_root_owner=owner
        raise error
    monkeypatch.setattr(bootstrap_entry,'PROFILE',object())
    monkeypatch.setattr(entry_binding,'start_compiled_control',fail)
    monkeypatch.setattr(entry.OriginalRootHold,'wait',lambda self:1)
    def visible(dialog):
        dialog.show();qt_test_scope.settle()
        hold=entry._HELD_ENTRIES[-1];panel=hold.panel
        deadline=monotonic()+3
        while (panel.future is not None or hold.executor.close_held) and monotonic()<deadline:
            qt_test_scope.settle(1);sleep(.005)
        assert type(hold) is entry.OriginalDeploymentHold and hold.owner is owner
        assert not hasattr(hold,'backend') and hold.control is None and not hold.released.is_set()
        assert 'Startup stopped' in panel.status.text()
        assert not panel.retry_save.isEnabled() and not panel.recover.isEnabled()
        assert not dialog.close() and dialog.isVisible()
        dialog.hide();return 0
    monkeypatch.setattr(QDialog,'exec',visible)
    before=len(entry._HELD_ENTRIES)
    try:
        assert entry.run([])==1
    finally:
        for hold in entry._HELD_ENTRIES[before:]:
            hold.panel.timer.stop();hold.dialog.deleteLater()
        del entry._HELD_ENTRIES[before:]


def test_visible_target_health_failure_then_recovery_allows_distinct_restored_run(qt_test_scope,tmp_path,monkeypatch):
    from suite_core.encyclopedia.root_recovery_ui import OriginalRootRecoveryService
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    calls=[];flags={'held':True,'drained':True};selected=object()
    backend=object.__new__(StandaloneEncyclopediaBootstrap)
    original=SimpleNamespace(current=SimpleNamespace(selected=selected))
    backend.request=original;backend.subject=object()
    backend.owner=SimpleNamespace(record={'standalone_update':{'phase':'applying'}},
        _standalone_encyclopedia_owned=lambda subject:None)
    backend.binding=SimpleNamespace(root=tmp_path,current=lambda:SimpleNamespace(
        phase='activated' if flags['held'] else 'healthy',selected=selected))
    backend.poll_reader=lambda:flags['drained']
    monkeypatch.setattr(StandaloneEncyclopediaBootstrap,'close_held',property(lambda self:flags['held']))
    def control():
        return SimpleNamespace(close_intent=True,actor=SimpleNamespace(channel=SimpleNamespace(
            read_fault=None,write_fault=None)))
    hold=entry.OriginalRootHold(backend,control(),ResourceExecutorOwner(),RuntimeError('original held transaction'))
    hold.close_acknowledged=True
    def fail_health():
        calls.append('target-health-failed');raise RuntimeError('Synthetic target health failed after normal drain')
    backend._installation=SimpleNamespace(confirm_health=fail_health)
    def recover(request,*,confirmed=False):
        assert confirmed and request is original and hold.close_acknowledged and flags['drained']
        calls.append('recover');backend.owner.record['standalone_update']['phase']='recovering'
    backend.recover_update=recover
    def settled(subject,request):
        assert subject is backend.subject and request is original and flags['drained'] and hold.close_acknowledged
        calls.append('recovery-health');flags['held']=False;backend.owner.record={}
        return backend.binding.current()
    backend.owner.settle_standalone_encyclopedia_update=settled
    def resume():
        recovering=backend.owner.record['standalone_update']['phase']=='recovering'
        calls.append('restored-run' if recovering else 'target-run')
        # Inert positive native Close/drain receipt for this distinct attempt.
        hold.control=control();hold.close_acknowledged=True;flags['drained']=True
        if recovering:backend.finish_recovery_health(original)
        else:backend.finish_target_health(original)
    hold.resume_original_reader=resume
    service=OriginalRootRecoveryService(hold)
    panel=EncyclopediaAcquisitionPanel(service,hold.executor,recovery_only=True)
    qt_test_scope.show(panel)
    def until_done():
        deadline=monotonic()+3
        while panel.close_held and monotonic()<deadline:
            qt_test_scope.settle(1);sleep(.005)
        assert not panel.close_held
    QTest.mouseClick(panel.health_accept,Qt.LeftButton);until_done()
    QTest.mouseClick(panel.run_recovery_reader,Qt.LeftButton);until_done()
    assert service.reader_entered and service.reader_attempt_control is hold.control
    QTest.mouseClick(panel.health_accept,Qt.LeftButton);until_done()
    QTest.mouseClick(panel.recovery_confirm,Qt.LeftButton)
    QTest.mouseClick(panel.recover,Qt.LeftButton);until_done()
    assert panel.run_recovery_reader.isEnabled() and not service.reader_entered
    QTest.mouseClick(panel.run_recovery_reader,Qt.LeftButton);until_done()
    assert service.can_close and backend.request is None
    assert calls==['target-run','target-health-failed','recover','restored-run','recovery-health']
    panel.timer.stop();hold.executor.begin_close()
