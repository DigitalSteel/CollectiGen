"""Source entry qualification with inert factories; no native launches."""
from types import SimpleNamespace
import pytest
from suite_core.encyclopedia import __main__ as entry
from suite_core.encyclopedia import bootstrap_entry, entry_binding
from tests.test_encyclopedia_entry_control import standalone,stock_spawn,no_external_actions


@pytest.fixture(autouse=True)
def inert_progress_presenter(monkeypatch):
    # This module tests root ownership/dispatch, including calls from synthetic
    # non-GUI threads. Real painting/delays live in test_encyclopedia_pending_task_ui.
    from suite_core.encyclopedia import pending_task_ui
    from time import sleep
    class InertProgress:
        def phase(self, *args): pass
        def reader_ready(self): pass
        def poll(self): pass
        def finish(self): pass
        def wait(self, future, executor):
            result = future.result()
            while executor.close_held: sleep(.005)
            return result
    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', InertProgress)


def test_startup_failure_persists_safe_internal_trace_before_recovery(tmp_path,monkeypatch):
    import json,logging
    from core.manager_launch_owner import DeploymentOwner,LaunchHold
    from suite_core.encyclopedia.root_holds import OriginalDeploymentHold
    owner=object.__new__(DeploymentOwner);owner.data_root=tmp_path;owner.root=tmp_path
    owner.record={'standalone_update':{'phase':'applying'}}
    secret='synthetic-sensitive-sentinel';seen=[]
    def failed():
        try:raise ValueError(secret+' C:\\Users\\Synthetic\\private.png')
        except ValueError as cause:
            error=LaunchHold('control_channel_idle_timeout');error.encyclopedia_root_owner=owner
            raise error from cause
    def present(hold):
        assert type(hold) is OriginalDeploymentHold and hold.owner is owner and hold.control is None
        assert hold.diagnostic_recorded
        raw=(tmp_path/'logs/tagmanager.log').read_text(encoding='utf-8')
        assert secret not in raw and 'private.png' not in raw
        payload=json.loads(raw.strip().split(' | ',2)[2])
        assert payload['phase']=='compiled_startup' and payload['transaction_phase']=='applying'
        assert payload['errors'][0]['type']=='LaunchHold' and payload['errors'][0]['code']=='control_channel_idle_timeout'
        assert payload['errors'][1]['type']=='ValueError' and payload['errors'][1]['code']=='not_recorded'
        assert any(f['module']==__name__ and f['function']=='failed' and f['line']>0 for f in payload['errors'][0]['frames'])
        seen.append(hold);hold.executor.begin_close();return 7
    monkeypatch.setattr(entry_binding,'start_compiled_control',failed)
    monkeypatch.setattr(bootstrap_entry,'PROFILE',None)
    monkeypatch.setattr(OriginalDeploymentHold,'present',present)
    before=len(entry._HELD_ENTRIES)
    try:assert entry.run([])==7 and len(seen)==1
    finally:
        del entry._HELD_ENTRIES[before:]
        for logger in list(logging.Logger.manager.loggerDict.values()):
            if isinstance(logger,logging.Logger) and logger.name.startswith('collectigen.encyclopedia.root.'):
                for handler in list(logger.handlers):
                    if getattr(handler,'baseFilename','')==str((tmp_path/'logs/tagmanager.log').resolve()):
                        logger.removeHandler(handler);handler.close()


def test_diagnostic_write_failure_preserves_original_error_and_hold(tmp_path,monkeypatch):
    from core import logging_setup
    from suite_core.encyclopedia.root_recovery_ui import persist_root_failure
    error=RuntimeError('secret exception prose')
    control=object();hold=SimpleNamespace(error=error,control=control,
        backend=SimpleNamespace(owner=SimpleNamespace(data_root=tmp_path,root=tmp_path,record={})))
    def failed(*a,**kw):raise OSError('synthetic unavailable diagnostic directory')
    monkeypatch.setattr(logging_setup,'configure_logging',failed)
    assert not persist_root_failure(hold,phase='reader_startup')
    assert hold.error is error and hold.control is control and not hold.diagnostic_recorded


def test_actual_startup_ready_failure_hands_same_control_to_recovery(stock_spawn,monkeypatch):
    from core import manager_launch_owner as native
    from suite_core.encyclopedia.control_entry import EncyclopediaRootControl
    from suite_core.encyclopedia.root_recovery_ui import OriginalRootRecoveryService
    boot,state,_=stock_spawn;sent=[];seen=[];lease=boot.owner.fd
    original_write=native.WindowsControlChannel.write_frame;original_read=native.WindowsControlChannel.read_frame
    def reader_channel(channel):return channel is getattr(getattr(boot.subject.reader,'native',None),'channel',None)
    def write(self,raw,**kw):
        if reader_channel(self):sent.append(raw)
        else:return original_write(self,raw,**kw)
    monkeypatch.setattr(native.WindowsControlChannel,'write_frame',write)
    def failed_read(self,**kw):
        if reader_channel(self):raise native.LaunchHold('control_channel_eof')
        return original_read(self,**kw)
    monkeypatch.setattr(native.WindowsControlChannel,'read_frame',failed_read)
    monkeypatch.setattr(entry_binding,'start_compiled_control',lambda:entry_binding.start_original_reader(boot))
    monkeypatch.setattr(bootstrap_entry,'PROFILE',None)
    def present(hold):
        control=hold.error.encyclopedia_root_control
        assert type(control) is EncyclopediaRootControl and hold.control is control
        assert control.backend is boot and control.actor.session is control and hold.backend is boot
        assert not control.ready and not control.close_intent and not hold.close_acknowledged
        assert control.wire.incoming==0 and control.wire.outgoing==1 and len(sent)==1
        assert state['creates']==1 and boot.owner.fd==lease and boot.request is None
        service=OriginalRootRecoveryService(hold);status=service.reconcile_launch()
        assert not service.startup_only and 'could not close safely' in status.reason
        assert not service.can_close and not service.can_retry and not service.can_recover and not service.can_run_reader
        with pytest.raises(RuntimeError,match='physical worker drain'):hold.release_after_recovery(confirmed=True)
        assert hold.diagnostic_recorded
        seen.append(hold);hold.executor.begin_close();return 7
    monkeypatch.setattr(entry.OriginalRootHold,'present',present)
    before=len(entry._HELD_ENTRIES);compiled_before=len(entry_binding._HELD_COMPILED_ENTRIES)
    try:assert entry.run([])==7 and len(seen)==1
    finally:
        del entry._HELD_ENTRIES[before:];del entry_binding._HELD_COMPILED_ENTRIES[compiled_before:]


def test_inactive_dispatch_preserves_renderer_arguments(monkeypatch):
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', None)
    monkeypatch.setattr(entry_binding, 'start_compiled_control', lambda: None)
    monkeypatch.setattr(entry, 'main', lambda **kwargs: kwargs)
    assert entry.run(['--theme', 'dark']) == {'argv': ('--theme', 'dark')}


def test_inactive_custody_refused_before_renderer(monkeypatch):
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', None)
    monkeypatch.setattr(entry, 'main', lambda **kwargs: pytest.fail('renderer entered'))
    with pytest.raises(ValueError, match='profile_unavailable'):
        entry.run(['--encyclopedia-custody'])


def test_custody_dispatch_does_not_send_ready_twice(monkeypatch):
    from suite_core.encyclopedia import custody_entry
    calls=[]
    class Admitted:
        def accept_ready(self):pytest.fail('Factory already admitted READY')
        def run(self):calls.append('run');return 0
    def admitted(*,bootstrap_sha256):
        assert bootstrap_sha256=='a'*64;calls.append('factory-ready');return Admitted()
    monkeypatch.setattr(custody_entry.CustodyObserver,'from_inherited_entry',admitted)
    monkeypatch.setattr(bootstrap_entry,'PROFILE',SimpleNamespace(bootstrap_sha256='a'*64))
    monkeypatch.setattr(entry,'main',lambda **kwargs:pytest.fail('renderer entered'))
    assert entry.run(['--encyclopedia-custody'])==0 and calls==['factory-ready','run']


def test_factory_owner_failure_uses_typed_original_supervision(monkeypatch):
    from core.manager_launch_owner import DeploymentOwner
    owner=object.__new__(DeploymentOwner);error=RuntimeError('Synthetic original custody factory failure')
    error.encyclopedia_root_owner=owner
    def failed():raise error
    monkeypatch.setattr(entry_binding,'start_compiled_control',failed)
    monkeypatch.setattr(bootstrap_entry,'PROFILE',object())
    seen=[]
    def supervised(hold):
        assert hold.owner is owner and hold.error is error and not hasattr(hold,'backend')
        assert not hold.executor.closed and not hold.released.is_set()
        with pytest.raises(RuntimeError,match='owner-only'):
            hold.release_after_recovery(confirmed=True)
        seen.append(hold);return 1
    monkeypatch.setattr(entry.OriginalDeploymentHold,'present',supervised)
    before=len(entry._HELD_ENTRIES)
    try:
        assert entry.run([])==1 and entry._HELD_ENTRIES[-1] is seen[0]
    finally:
        seen[0].executor.begin_close();del entry._HELD_ENTRIES[before:]


def test_module_entry_factory_failure_uses_same_shared_hold_type(monkeypatch):
    """Execute actual -m entry body with inert factory/owner/presenter only."""
    import runpy,sys
    from core.manager_launch_owner import DeploymentOwner
    from suite_core.encyclopedia.root_holds import OriginalDeploymentHold
    from suite_core.encyclopedia.root_recovery_ui import OriginalRootRecoveryService
    owner=object.__new__(DeploymentOwner);error=RuntimeError('Synthetic module-entry factory failure')
    error.encyclopedia_root_owner=owner
    def failed():raise error
    monkeypatch.setattr(entry_binding,'start_compiled_control',failed)
    monkeypatch.setattr(bootstrap_entry,'PROFILE',None)
    monkeypatch.setattr(sys,'argv',['synthetic-module-entry'])
    seen=[]
    def present(hold):
        assert type(hold) is OriginalDeploymentHold
        service=OriginalRootRecoveryService(hold)
        assert service.original_owner is owner and service.backend is None and service.startup_only
        assert 'Startup stopped' in service.reconcile_launch().reason
        assert not service.can_close and not service.can_retry and not service.can_recover
        with pytest.raises(RuntimeError,match='owner-only'):
            hold.release_after_recovery(confirmed=True)
        seen.append(hold);hold.executor.begin_close();return 7
    monkeypatch.setattr(OriginalDeploymentHold,'present',present)
    with pytest.warns(RuntimeWarning,match='found in sys.modules'):
        with pytest.raises(SystemExit) as exit:
            runpy.run_module('suite_core.encyclopedia.__main__',run_name='__main__')
    assert exit.value.code==7 and len(seen)==1 and seen[0].owner is owner


def test_compiled_root_refuses_unforwarded_options(monkeypatch):
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', object())
    monkeypatch.setattr(entry_binding, 'start_compiled_control', lambda: pytest.fail('factory entered'))
    with pytest.raises(ValueError, match='arguments_unqualified'):
        entry.run(['--theme', 'dark'])


def test_original_root_health_before_close():
    calls = []
    backend = SimpleNamespace(request=None, close_held=False,
        binding=SimpleNamespace(current=lambda: SimpleNamespace(phase='activated')),
        poll_reader=lambda: True,
        finish_initial_health=lambda: calls.append('health'),
        close=lambda: calls.append('close'))
    control = SimpleNamespace(close_intent=False,
        after_reader_close=lambda: calls.append('settle'))
    def handle():
        calls.append('request')
        control.close_intent = True
        return {'status':'close_requested'}
    control.handle_next = handle
    assert entry._run_original_root(backend, control) == 0
    assert calls == ['request', 'settle', 'health', 'close']


def test_progress_cleanup_failure_cannot_bypass_original_retention(monkeypatch):
    from suite_core.encyclopedia import pending_task_ui
    from time import monotonic, sleep
    class BrokenProgress(pending_task_ui.RootTaskProgress):
        def finish(self):
            raise RuntimeError('synthetic cleanup failure')
    monkeypatch.setattr(pending_task_ui, 'RootTaskProgress', BrokenProgress)
    backend = SimpleNamespace(request=None, close_held=False,
        poll_reader=lambda: True, close=lambda: None)
    def fail():
        raise ValueError('original settlement failure')
    control = SimpleNamespace(close_intent=False, after_reader_close=fail)
    def handle():
        control.close_intent = True
        return {'status': 'close_requested'}
    control.handle_next = handle
    before = len(entry._HELD_ENTRIES)
    def recover(hold, *, phase):
        assert entry._HELD_ENTRIES[-1] is hold and hold.backend is backend
        assert str(hold.error) == 'original settlement failure'
        assert str(hold.progress_cleanup_error) == 'synthetic cleanup failure'
        assert hold.error.encyclopedia_root_owner is backend
        assert hold.error.encyclopedia_root_executor is hold.executor
        deadline = monotonic() + 3
        while hold.executor.close_held and monotonic() < deadline:
            sleep(.005)
        hold.release_after_recovery(confirmed=True)
        return 0
    monkeypatch.setattr(entry, '_present_root_failure', recover)
    try:
        assert entry._run_original_root(backend, control) == 0
    finally:
        del entry._HELD_ENTRIES[before:]


def test_root_observes_original_reader_before_close_ack_and_after_drain():
    calls=[];control=SimpleNamespace(close_intent=False)
    def observe():
        calls.append('observe-drained' if control.close_intent else 'observe-running')
        return control.close_intent
    backend=SimpleNamespace(request=None,close_held=False,poll_reader=observe,
        binding=SimpleNamespace(current=lambda:SimpleNamespace(phase='healthy')),
        close=lambda:calls.append('original-close'))
    def handle():
        assert calls==['observe-running']
        calls.extend(['receive-close','positive-close-reply'])
        control.close_intent=True
        return {'status':'close_requested'}
    control.handle_next=handle;control.after_reader_close=lambda:calls.append('settle')
    assert entry._run_original_root(backend,control)==0
    assert calls==['observe-running','receive-close','positive-close-reply',
        'observe-drained','settle','original-close']


@pytest.mark.parametrize('stage', ['settlement', 'partial_frame'])
def test_failed_settlement_keeps_actual_entry_live_until_explicit_recovery(monkeypatch,stage):
    from suite_core.encyclopedia import root_recovery_ui
    monkeypatch.setattr(root_recovery_ui, 'present_original_root_hold', lambda hold: hold.wait())
    from threading import Thread
    from time import monotonic, sleep
    calls = []
    backend = SimpleNamespace(close_held=True, poll_reader=lambda: True,
        close=lambda: calls.append('close'))
    def fail():
        calls.append(stage)
        if stage=='partial_frame':
            from core.manager_launch_owner import LaunchHold
            raise LaunchHold('control_channel_timeout')
        raise ValueError('settlement failure')
    control = SimpleNamespace(close_intent=False, after_reader_close=fail)
    def handle():
        if stage=='partial_frame':return fail()
        control.close_intent=True
        return {'status':'close_requested'}
    control.handle_next=handle
    before = len(entry._HELD_ENTRIES)
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', object())
    monkeypatch.setattr(entry_binding, 'start_compiled_control', lambda: (backend,control))
    outcome=[]
    thread=Thread(target=lambda:outcome.append(entry.run([])))
    thread.start()
    deadline=monotonic()+3
    while len(entry._HELD_ENTRIES)==before and monotonic()<deadline:
        sleep(.005)
    hold=entry._HELD_ENTRIES[-1]
    try:
        assert hold.backend is backend and hold.control is control
        assert thread.is_alive() and outcome==[] and calls==[stage]
        with pytest.raises(RuntimeError,match='recovery'):
            hold.release_after_recovery(confirmed=True)
        assert calls==[stage]
        backend.close_held=False  # Test double for separately completed original recovery.
        while hold.executor.close_held and monotonic()<deadline:
            sleep(.005)
        hold.release_after_recovery(confirmed=True)
        thread.join(3)
        assert not thread.is_alive() and outcome==[0]
        assert calls==[stage,'close']
    finally:
        backend.close_held=False
        if thread.is_alive():
            hold.release_after_recovery(confirmed=True)
            thread.join(3)
        del entry._HELD_ENTRIES[before:]


def test_root_zero_byte_idle_keeps_same_control_until_close():
    from core.manager_launch_owner import LaunchHold
    calls=[]
    backend=SimpleNamespace(request=None,close_held=False,
        binding=SimpleNamespace(current=lambda:SimpleNamespace(phase='healthy')),
        poll_reader=lambda:True,close=lambda:calls.append('close'))
    control=SimpleNamespace(close_intent=False,after_reader_close=lambda:calls.append('settle'))
    def handle():
        calls.append('handle')
        if calls.count('handle')==1:
            raise LaunchHold('control_channel_idle_timeout')
        control.close_intent=True
        return {'status':'close_requested'}
    control.handle_next=handle
    assert entry._run_original_root(backend,control)==0
    assert calls==['handle','handle','settle','close']


def test_presenter_failure_keeps_original_hold(monkeypatch):
    from suite_core.encyclopedia import root_recovery_ui
    hold=entry.OriginalRootHold(object(),object(),object(),RuntimeError('original'))
    def fail(original):
        assert original is hold
        raise RuntimeError('presenter failure')
    monkeypatch.setattr(root_recovery_ui,'present_original_root_hold',fail)
    hold.wait=lambda:42  # Finite substitute for retained live supervision only.
    assert hold.present()==42 and str(hold.presentation_error)=='presenter failure'


@pytest.mark.parametrize('path',['normal_driver','held_release'])
def test_backend_close_failure_preserves_executor_for_status_and_retry(monkeypatch,path):
    from time import monotonic,sleep
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from suite_core.encyclopedia import root_recovery_ui
    calls=[]
    def close():
        calls.append('close')
        if calls.count('close')==1:raise RuntimeError('original closure pending')
    backend=SimpleNamespace(request=None,close_held=False,poll_reader=lambda:True,
        binding=SimpleNamespace(current=lambda:SimpleNamespace(phase='healthy')),close=close)
    def recover(hold):
        assert hold.backend is backend and not hold.executor.closed
        future=hold.executor.submit(lambda:calls.append('status'))
        future.result(timeout=3)
        deadline=monotonic()+3
        while hold.executor.close_held and monotonic()<deadline:sleep(.005)
        hold.release_after_recovery(confirmed=True)
        assert hold.executor.closed and hold.released.is_set()
        return 0
    if path=='held_release':
        hold=entry.OriginalRootHold(backend,object(),ResourceExecutorOwner(),RuntimeError('held'))
        with pytest.raises(RuntimeError,match='closure pending'):
            hold.release_after_recovery(confirmed=True)
        recover(hold)
    else:
        control=SimpleNamespace(close_intent=False,after_reader_close=lambda:None)
        def handle():
            control.close_intent=True;return {'status':'close_requested'}
        control.handle_next=handle
        before=len(entry._HELD_ENTRIES)
        monkeypatch.setattr(root_recovery_ui,'present_original_root_hold',recover)
        assert entry._run_original_root(backend,control)==0
        del entry._HELD_ENTRIES[before:]
    assert calls==['close','status','close']
