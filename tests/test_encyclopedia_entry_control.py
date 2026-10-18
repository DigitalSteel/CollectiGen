"""Stock codec/native API boundary doubles only; no GUI/process is started."""
from hashlib import sha256
from types import SimpleNamespace
import json
import sys
import ctypes

import pytest

from core import manager_launch_owner as native
from suite_core.manager.bootstrap_entry import _NormalWire, MESSAGE_KINDS, EntryHold
from suite_core.encyclopedia.control_entry import (KINDS, check_request, StandaloneControlActor,
    EncyclopediaRootControl, EncyclopediaChildControl)
from suite_core.encyclopedia.entry_binding import (reader_binding, child_binding,
    StandaloneEntryProfile, create_root_entry)
from tests.test_standalone_encyclopedia_update import standalone, add_schema_package
from tests.test_encyclopedia_installation import no_external_actions


def test_stock_wire_restricts_leaf_without_widening_manager():
    document = {'channel_epoch': 'a' * 32}
    channel = object.__new__(native.WindowsControlChannel)
    written = []
    channel.write_frame = lambda raw, **kwargs: written.append(raw)
    proof = native.NativeChannelProof(sha256(native._bytes(document)).hexdigest(), 'a' * 32, 1, 2)
    default = _NormalWire(channel, document, proof)
    assert default.message_kinds is MESSAGE_KINDS
    with pytest.raises(EntryHold):
        default.send('encyclopedia-request', {})
    leaf = _NormalWire(channel, document, proof, message_kinds=KINDS)
    leaf.send('encyclopedia-request', {})
    with pytest.raises(EntryHold):
        leaf.send('manager-update-request', {})
    assert len(written) == 1


@pytest.mark.parametrize('body', [
    {'id': 'a' * 32, 'operation': 'run', 'body': {'argv': ['evil.exe']}},
    {'id': 'a' * 32, 'operation': 'confirm', 'body': {'idle': True}},
    {'id': 'a' * 32, 'operation': 'recover', 'body': {'request_sha256': True}},
    {'id': 'a' * 32, 'operation': 'check', 'body': {'root': 'foreign'}},
])
def test_closed_request_cannot_supply_execution_or_boolean_authority(body):
    with pytest.raises(ValueError):
        check_request(body)


def test_explicit_entry_composes_root_custody_without_reader_launch(standalone, monkeypatch):
    f, old, _ = standalone
    profile = StandaloneEntryProfile(old.binding.root, old.binding.data_root,
        old.binding.settings_path, old.binding.bootstrap_sha256, old.binding.trust_bytes,
        old.binding.trust_sha256, 'beta', old.acquisition)
    old.close()
    from suite_core.encyclopedia import entry_binding as entries
    owner_type = native.DeploymentOwner
    created = []
    def owner_factory(*args, **kwargs):
        owner = owner_type(*args, **kwargs)
        facade = SimpleNamespace(owner=owner, binding=old.binding)
        created.append(stock_native_double(facade, monkeypatch))
        return owner
    monkeypatch.setattr(entries, 'DeploymentOwner', owner_factory)
    backend = create_root_entry(profile)
    try:
        assert backend.owner.fd is not None and backend.owner._creation_entered
        assert backend.owner._standalone_root_custody is not None
        assert created[0][1]['custody_creates'] == 1 and created[0][1]['creates'] == 0
        assert backend.owner._standalone_control_actor is None
        assert backend.owner._standalone_encyclopedia_subject is backend.subject
    finally:
        created[0][1]['members'] = 0
        backend.close()


def test_child_admission_uses_actual_executable_and_signed_context(standalone, monkeypatch):
    _, boot, _ = standalone
    expected = reader_binding(boot.binding)
    monkeypatch.setattr(sys, 'executable', str(expected.selected.program))
    checked, binding = child_binding(expected.restart_arguments,
        trust_bytes=boot.binding.trust_bytes, trust_sha256=boot.binding.trust_sha256)
    assert checked == expected
    monkeypatch.setattr(sys, 'executable', str(boot.binding.bootstrap_program))
    with pytest.raises(ValueError, match='immutable_reader'):
        child_binding(expected.restart_arguments, trust_bytes=boot.binding.trust_bytes,
            trust_sha256=boot.binding.trust_sha256)


def test_raw_actor_or_dto_cannot_enter_stock_standalone_spawn(standalone):
    _, boot, _ = standalone
    admitted = reader_binding(boot.binding)
    with pytest.raises(native.LaunchHold, match='exact_standalone_entry_command'):
        native.WindowsNormalTransport.spawn_standalone(boot.owner, boot.subject, admitted.command, environment={})


@pytest.fixture
def stock_spawn(standalone, monkeypatch):
    _, boot, _ = standalone
    boot.owner.enable_standalone_control(boot.subject)
    return stock_native_double(boot, monkeypatch)


def stock_native_double(boot, monkeypatch):
    """Inert stock original-object API/pipe boundary, never native qualification."""
    import msvcrt
    state = {'creates': 0, 'resumes': 0, 'attrs': [], 'closed': [], 'code': None, 'members': 0, 'next': 100, 'custody_creates': 0, 'custody_resumes': 0, 'custody_code': None, 'custody_frames': []}
    def pipe(reader, writer, security, size):
        state['next'] += 2
        reader._obj.value, writer._obj.value = state['next'], state['next'] + 1
        return True
    def duplicate(source, handle, target, output, access, inherit, flags):
        state['query_access'] = access; output._obj.value = 91 if access == 0x4 else 90; return True
    def initialize(buffer, count, flags, size):
        if buffer is None: size._obj.value = 128
        return True
    def attribute(buffer, flags, key, value, size, previous, required):
        state['attrs'].append((key, tuple(value))); return True
    def create(exe, command, psec, tsec, inherit, flags, environment, cwd, startup, output):
        custody = '--encyclopedia-custody' in command.value
        state['custody_creates' if custody else 'creates'] += 1
        if custody:
            state['custody_code'] = None
        else:
            state['code'] = None
            state['members'] = 1
        actor = boot.owner._standalone_root_custody.custody if custody else boot.owner.native_transport
        assert actor.creation_state == 'uncertain'
        assert flags & 0x4 and not flags & 0x1000000
        output._obj.process, output._obj.thread, output._obj.pid = (83, 84, 202) if custody else (81, 82, 201)
        return True
    def resume(handle):
        state['custody_resumes' if handle == 84 else 'resumes'] += 1; return 0
    api = SimpleNamespace(CreateJobObjectW=lambda *args: 70, GetCurrentProcess=lambda: -1,
        CreatePipe=pipe, DuplicateHandle=duplicate, SetHandleInformation=lambda *args: True,
        InitializeProcThreadAttributeList=initialize, UpdateProcThreadAttribute=attribute,
        DeleteProcThreadAttributeList=lambda *args: None, CreateProcessW=create,
        ResumeThread=resume, CloseHandle=lambda handle: state['closed'].append(handle) or True,
        WaitForSingleObject=lambda handle, timeout: 258 if state['custody_code' if handle == 83 else 'code'] is None else 0,
        GetExitCodeProcess=lambda handle, output: setattr(output._obj, 'value',
            state['custody_code' if handle == 83 else 'code'] or 0) or True)
    def image(handle, flags, output, size):
        output.value = str(boot.binding.bootstrap_program if handle in (-1, 83) else boot.binding.current().selected.program)
        return True
    def times(handle, creation, exit_time, kernel, user):
        creation._obj.dwLowDateTime = 500
        return True
    api.GetProcessId = lambda handle: 302 if handle == -1 else (202 if handle == 83 else 201)
    api.QueryFullProcessImageNameW = image
    api.GetProcessTimes = times
    monkeypatch.setattr(native, '_normal_native_api', lambda: api)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: api)
    monkeypatch.setattr(native.WindowsNormalTransport, 'includes', lambda self, process: process.pid != 202)
    monkeypatch.setattr(native.WindowsNormalTransport, 'job_active_count', lambda self: state['members'])
    monkeypatch.setattr(msvcrt, 'get_osfhandle', lambda fd: 701 if fd == boot.owner.fd else 702)
    monkeypatch.setattr(msvcrt, 'open_osfhandle', lambda handle, flags: 800 + handle)
    monkeypatch.setattr(native.os, 'set_handle_inheritable', lambda *args: None)
    real_close = native.os.close
    monkeypatch.setattr(native.os, 'close', lambda fd: None if fd >= 800 else real_close(fd))
    # Custody wire is an explicit source-only peer double. No observer process
    # or physical closure proof is fabricated; dedicated custody tests exercise
    # the real observer/provider against synthetic original objects.
    def custody_write(channel, raw, **kwargs):
        assert channel.peer_handle == 83
        frame = json.loads(raw[4:])
        state['custody_frames'].append(frame)
    def custody_read(channel, **kwargs):
        sent = state['custody_frames'][-1]
        if sent['kind'] == 'custody-offer':
            context = sent['payload']['binding']['context']
            payload = {'context_sha256': sha256(native._bytes(context)).hexdigest()}
            kind, sequence = 'custody-ready', 1
        else:
            assert sent['kind'] == 'custody-drain'
            state['custody_code'] = 0
            payload = {'originals_closed': True}
            kind, sequence = 'custody-drained', 2
        body = native._bytes(dict(protocol=1, sequence=sequence, kind=kind,
            binding_sha256=sent['binding_sha256'], payload=payload))
        return len(body).to_bytes(4, 'little') + body
    monkeypatch.setattr(native.WindowsControlChannel, 'write_frame', custody_write)
    monkeypatch.setattr(native.WindowsControlChannel, 'read_frame', custody_read)
    return boot, state, api


def test_stock_spawn_registers_original_witness_before_resume_and_retires(stock_spawn):
    boot, state, api = stock_spawn
    selected = boot.launch_current()
    actor = boot.subject.reader.native
    assert type(actor) is StandaloneControlActor and actor.transport.api is api
    assert state['creates'] == state['resumes'] == 1
    assert state['attrs'][-1] == (0x2000D, (70,))
    assert 701 not in actor.transport.inherited_handles
    assert boot.owner.children['encyclopedia'] is actor.process
    assert not boot.poll_reader()
    state['code'] = 0; state['members'] = 0
    assert boot.poll_reader()
    assert boot.owner.native_transport is None and boot.owner.children == {}
    assert actor.transport.job == boot.owner._standalone_root_custody.job == 70
    assert actor.process._handle is None and state['custody_code'] is None
    assert boot.owner.fd is not None


def test_root_session_queries_stock_native_identity_and_has_restricted_wire(stock_spawn, monkeypatch):
    boot, state, api = stock_spawn
    boot.launch_current()
    actor = boot.subject.reader.native
    frames = []
    monkeypatch.setattr(actor.channel, 'write_frame', lambda raw, **kwargs: frames.append(raw))
    session = EncyclopediaRootControl(boot)
    assert session.wire.message_kinds is KINDS and len(frames) == 1
    assert session.document['entry']['pid'] == 201
    assert session.document['launcher']['pid'] == 302
    assert session.wire.proof.source == 'original_inherited_native_handles'
    api.GetProcessId = lambda handle: 999
    with pytest.raises(ValueError, match='original_control_peer'):
        session._live()


def pending_ready_session(stock_spawn,monkeypatch,outcomes):
    """Existing stock wire/identity fixture with controlled initial frame timing."""
    boot,state,api=stock_spawn
    boot.launch_current();actor=boot.subject.reader.native
    sent=[];monkeypatch.setattr(actor.channel,'write_frame',lambda raw,**kw:sent.append(raw))
    root=EncyclopediaRootControl(boot)
    clock=[0.0];reads=[]
    from suite_core.encyclopedia import control_entry
    monkeypatch.setattr(control_entry.time,'monotonic',lambda:clock[0])
    def read(**kwargs):
        reads.append(kwargs);clock[0]+=kwargs['timeout']
        outcome=outcomes.pop(0)
        if isinstance(outcome,BaseException):raise outcome
        raw=native._bytes(dict(protocol=1,sequence=1,kind='ready',
            binding_sha256=root.wire.proof.binding_sha256,
            payload=dict(context_sha256=actor.admitted.context_sha256)))
        return len(raw).to_bytes(4,'little')+raw
    monkeypatch.setattr(actor.channel,'read_frame',read)
    return root,reads,sent,state,api


def test_initial_ready_waits_on_same_wire_after_stock_zero_byte_idle(stock_spawn,monkeypatch):
    root,reads,sent,state,api=pending_ready_session(stock_spawn,monkeypatch,
        [native.LaunchHold('control_channel_idle_timeout') for _ in range(3)]+['ready'])
    original=root.wire;actor=root.actor
    root.accept_ready()
    assert root.ready and root.wire is original and root.actor is actor
    assert len(reads)==4 and len(sent)==1 and state['creates']==1
    assert root.wire.incoming==1 and root.wire.outgoing==1


@pytest.mark.parametrize('reason',['control_channel_timeout','control_channel_eof',
    'control_read_recovery_required','normal_dispatch_aborted'])
def test_initial_ready_does_not_retry_definitive_channel_error(stock_spawn,monkeypatch,reason):
    root,reads,sent,_,_=pending_ready_session(stock_spawn,monkeypatch,
        [native.LaunchHold(reason),'ready'])
    with pytest.raises(native.LaunchHold,match=reason):root.accept_ready()
    assert not root.ready and len(reads)==1 and len(sent)==1 and root.wire.incoming==0


def test_initial_ready_idle_deadline_retains_offer_and_unconsumed_sequence(stock_spawn,monkeypatch):
    root,reads,sent,_,_=pending_ready_session(stock_spawn,monkeypatch,
        [native.LaunchHold('control_channel_idle_timeout') for _ in range(3)])
    with pytest.raises(native.LaunchHold,match='original_control_ready_deadline'):
        root.accept_ready(overall_timeout=3)
    assert not root.ready and len(reads)==3 and len(sent)==1 and root.wire.incoming==0


def test_initial_ready_rechecks_original_peer_after_idle(stock_spawn,monkeypatch):
    root,reads,sent,_,api=pending_ready_session(stock_spawn,monkeypatch,
        [native.LaunchHold('control_channel_idle_timeout'),'ready'])
    read=root.actor.channel.read_frame
    def changed_peer(**kwargs):
        try:return read(**kwargs)
        finally:api.GetProcessId=lambda handle:999
    monkeypatch.setattr(root.actor.channel,'read_frame',changed_peer)
    with pytest.raises(ValueError,match='original_control_peer_changed'):root.accept_ready()
    assert not root.ready and len(reads)==1 and len(sent)==1


def test_initial_ready_zero_idle_label_with_retained_read_fault_cannot_retry(stock_spawn,monkeypatch):
    root,reads,sent,_,_=pending_ready_session(stock_spawn,monkeypatch,
        [native.LaunchHold('control_channel_idle_timeout'),'ready'])
    root.actor.channel.read_fault=dict(bytes_received=2,reason='partial')
    with pytest.raises(native.LaunchHold,match='control_channel_idle_timeout'):root.accept_ready()
    assert not root.ready and len(reads)==1 and len(sent)==1


def test_unborn_or_foreign_actor_cannot_construct_control_session(standalone):
    _, boot, _ = standalone
    with pytest.raises(ValueError, match='actual_original_control_actor'):
        EncyclopediaRootControl(boot)


def test_original_native_image_mismatch_retains_failed_birth(stock_spawn, monkeypatch):
    boot, state, api = stock_spawn
    api.QueryFullProcessImageNameW = lambda handle, flags, output, size: setattr(output, 'value', str(boot.binding.bootstrap_program)) or True
    with pytest.raises(native.LaunchHold, match='signed_standalone_peer_image'):
        boot.launch_current()
    assert state['resumes'] == 0 and state['creates'] == 1
    assert boot.owner.fd is not None and boot.owner.pending


def test_cleanup_retry_remembers_positively_closed_handles(stock_spawn):
    boot, state, api = stock_spawn
    boot.launch_current(); boot.poll_reader()
    actor = boot.subject.reader.native
    state['code'] = 0; state['members'] = 0
    close = api.CloseHandle
    def fail_job(handle):
        return False if handle == 81 else close(handle)
    api.CloseHandle = fail_job
    with pytest.raises(native.LaunchHold, match='resource_close_failed'):
        boot.poll_reader()
    closed = list(state['closed'])
    assert actor.process._handle == 81 and boot.owner._standalone_control_terminal is not None
    api.CloseHandle = close
    assert boot.poll_reader()
    assert state['closed'].count(81) == closed.count(81) + 1
    assert state['closed'].count(82) == closed.count(82)
    assert boot.owner.fd is not None


def connected(stock_spawn, monkeypatch, *, observe_visible=True):
    boot, state, api = stock_spawn
    boot.launch_current()
    actor = boot.subject.reader.native
    root_to_child, child_to_root = [], []
    monkeypatch.setattr(actor.channel, 'write_frame', lambda raw, **kwargs: root_to_child.append(raw))
    monkeypatch.setattr(actor.channel, 'read_frame', lambda **kwargs: child_to_root.pop(0))
    root = EncyclopediaRootControl(boot)
    selected = boot.binding.current().selected
    child_api = SimpleNamespace(GetCurrentProcess=lambda: -1,
        GetProcessId=lambda handle: 201 if handle == -1 else 302,
        QueryFullProcessImageNameW=lambda handle, flags, image, size: setattr(image, 'value',
            str(selected.program if handle == -1 else boot.binding.bootstrap_program)) or True)
    channel = native.WindowsControlChannel(child_api, 10, 11, 90, epoch=actor.channel.epoch)
    channel.read_frame = lambda **kwargs: root_to_child.pop(0)
    channel.write_frame = lambda raw, **kwargs: child_to_root.append(raw)
    monkeypatch.setattr(native.WindowsControlChannel, 'from_inherited_environment', classmethod(lambda cls: channel))
    monkeypatch.setattr(sys, 'executable', str(selected.program))
    client = EncyclopediaChildControl.from_inherited_entry(actor.admitted.restart_arguments,
        trust_bytes=boot.binding.trust_bytes, trust_sha256=boot.binding.trust_sha256)
    root.accept_ready()
    if observe_visible:
        assert not boot.poll_reader()  # existing synthetic visible-window observation
    return root, client, root_to_child, child_to_root


def close_order_health(stock_spawn,monkeypatch,*,visible):
    """Actual stock READY/RPC/retirement order; native API/UI boundary doubles."""
    root,client,replies,requests=connected(stock_spawn,monkeypatch,observe_visible=False)
    boot,state,api=stock_spawn;reader=boot.subject.reader;selected=reader.selected
    assert root.ready and reader._visible_seen is False and reader._health is None
    events=['ready']
    def observe(pid):
        assert pid==root.actor.process.pid and state['code'] is None
        assert root.actor.process._handle==81
        events.append('visible' if visible else 'not-visible')
        return visible
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window',observe)
    original_write=root.actor.channel.write_frame
    def reply_then_immediate_close(raw,**kwargs):
        frame=json.loads(raw[4:])
        assert frame['kind']=='encyclopedia-reply'
        assert frame['payload']['result']=={'status':'close_requested'}
        assert reader._health is None  # Live visibility alone never grants health.
        events.append('close-ack');original_write(raw,**kwargs)
        state.update(code=0,members=0)  # GUI closes immediately after authenticated ACK.
    monkeypatch.setattr(root.actor.channel,'write_frame',reply_then_immediate_close)
    request={'id':'c'*32,'operation':'close','body':{}}
    client.wire.send('encyclopedia-request',request)
    assert root.handle_next()=={'status':'close_requested'}
    assert client.wire.receive('encyclopedia-reply')['payload']['id']==request['id']
    root.after_reader_close();events.append('drained')
    assert reader.native is None and root.actor.process._handle is None
    assert boot.owner.record['standalone_encyclopedia']['attempt']['outcome']=='normal_close'
    healthy=reader.native_health(selected);events.append('health' if healthy else 'health-held')
    assert root.wire.incoming==2 and root.wire.outgoing==2  # One READY and one Close; no new protocol.
    return events,healthy


def test_ready_visible_immediate_close_ack_then_native_health(stock_spawn,monkeypatch):
    events,healthy=close_order_health(stock_spawn,monkeypatch,visible=True)
    assert events==['ready','visible','close-ack','drained','health'] and healthy is True


def test_never_visible_normal_close_drain_cannot_grant_health(stock_spawn,monkeypatch):
    root,client,_,_=connected(stock_spawn,monkeypatch,observe_visible=False)
    boot,state,api=stock_spawn;reader=boot.subject.reader;selected=reader.selected
    observed=[]
    def never_visible(pid):
        assert state['code'] is None and pid==root.actor.process.pid
        observed.append(pid);return False
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window',never_visible)
    result=exchange(root,client,'close')
    assert result=={'status':'held','reason':'operation_held','operation':'close'}
    assert len(observed)==1 and root.close_intent is False
    assert state['code'] is None and reader._health is None and reader._visible_seen is False
    # Even a later zero exit/empty Job cannot invent visibility or an ACK.
    state.update(code=0,members=0)
    assert boot.poll_reader() and reader.native_health(selected) is False
    assert reader._health is None and len(observed)==1
    with pytest.raises(ValueError,match='authenticated_normal_control_close_required'):
        root.after_reader_close()


def test_close_visibility_observation_failure_holds_without_positive_ack(stock_spawn,monkeypatch):
    root,client,_,_=connected(stock_spawn,monkeypatch,observe_visible=False)
    reader=root.backend.subject.reader
    def unavailable(pid):raise OSError('synthetic observation failure')
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window',unavailable)
    assert exchange(root,client,'close')=={'status':'held','reason':'operation_held','operation':'close'}
    assert root.close_intent is False and reader._visible_seen is False and reader._health is None


def test_exit_during_live_visibility_probe_cannot_acknowledge_close(stock_spawn,monkeypatch):
    root,client,_,_=connected(stock_spawn,monkeypatch,observe_visible=False)
    boot,state,api=stock_spawn;reader=boot.subject.reader
    def exits_during_observation(pid):
        assert state['code'] is None
        state.update(code=0,members=0)
        return True  # Stale pixels alone cannot establish live original-peer visibility.
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window',exits_during_observation)
    client.wire.send('encyclopedia-request',{'id':'d'*32,'operation':'close','body':{}})
    with pytest.raises(ValueError,match='original_control_peer_changed'):root.handle_next()
    assert root.close_intent is False and root.wire.outgoing==1  # Offer only; no positive ACK.
    assert reader._visible_seen is False and reader._health is None
    assert boot.poll_reader() and reader.native_health(reader.selected) is False


def test_real_stock_handshake_checks_both_native_peers_before_commands(stock_spawn, monkeypatch):
    root, client, _, _ = connected(stock_spawn, monkeypatch)
    assert root.ready and client.wire.proof.source == 'original_inherited_native_handles'
    assert root.wire.document == client.wire.document
    assert client.wire.message_kinds is KINDS
    assert root.wire.incoming == client.wire.incoming == 1


def test_root_rejects_replayed_id_and_stale_binding(stock_spawn, monkeypatch):
    root, client, replies, requests = connected(stock_spawn, monkeypatch)
    payload = {'id': 'a' * 32, 'operation': 'status', 'body': {}}
    client.wire.send('encyclopedia-request', payload)
    assert root.handle_next()['status'] == 'waiting'
    client.wire.send('encyclopedia-request', payload)
    with pytest.raises(ValueError, match='replayed_or_exhausted'):
        root.handle_next()


def test_stock_wire_rejects_foreign_binding_and_sequence(stock_spawn, monkeypatch):
    root, client, replies, requests = connected(stock_spawn, monkeypatch)
    client.wire.send('encyclopedia-request', {'id': 'b' * 32, 'operation': 'status', 'body': {}})
    frame = json.loads(requests[0][4:])
    frame['binding_sha256'] = '0' * 64
    raw = json.dumps(frame).encode(); requests[0] = len(raw).to_bytes(4, 'little') + raw
    with pytest.raises(EntryHold, match='stale_or_foreign'):
        root.handle_next()


def test_foreign_client_witness_cannot_complete_native_admission(stock_spawn, monkeypatch):
    boot, state, api = stock_spawn
    boot.launch_current(); actor = boot.subject.reader.native
    frames = []
    monkeypatch.setattr(actor.channel, 'write_frame', lambda raw, **kwargs: frames.append(raw))
    EncyclopediaRootControl(boot)
    wrong = SimpleNamespace(GetCurrentProcess=lambda: -1, GetProcessId=lambda handle: 999,
        QueryFullProcessImageNameW=lambda handle, flags, image, size: setattr(image, 'value', str(boot.binding.bootstrap_program)) or True)
    channel = native.WindowsControlChannel(wrong, None, None, 90, epoch=actor.channel.epoch)
    channel.read_frame = lambda **kwargs: frames.pop(0)
    monkeypatch.setattr(native.WindowsControlChannel, 'from_inherited_environment', classmethod(lambda cls: channel))
    monkeypatch.setattr(sys, 'executable', str(actor.admitted.selected.program))
    with pytest.raises(native.LaunchHold, match='native_entry_offer_mismatch'):
        EncyclopediaChildControl.from_inherited_entry(actor.admitted.restart_arguments,
            trust_bytes=boot.binding.trust_bytes, trust_sha256=boot.binding.trust_sha256)


def exchange(root, client, operation, body=None):
    import uuid
    payload = {'id': uuid.uuid4().hex, 'operation': operation, 'body': body or {}}
    client.wire.send('encyclopedia-request', payload)
    result = root.handle_next()
    reply = client.wire.receive('encyclopedia-reply')['payload']
    assert reply == {'id': payload['id'], 'result': result}
    return result


def adapter_exchange(root, client):
    from core.encyclopedia_entry_adapter import EncyclopediaEntryUpdateCheck
    original_send=client.wire.send
    def send(kind,payload):
        original_send(kind,payload)
        if kind=='encyclopedia-request':root.handle_next()
    client.wire.send=send
    return EncyclopediaEntryUpdateCheck(client)


def test_actual_adapter_control_confirm_status_cancel_route(standalone, stock_spawn, monkeypatch):
    f,boot,_=standalone
    root,client,_,_=connected(stock_spawn,monkeypatch)
    add_schema_package(f,'0.1.0-alpha.9')
    adapter=adapter_exchange(root,client)
    notice=adapter.check(); assert notice.status=='available' and notice.route=='standalone'
    pending=adapter.confirm_update(notice,confirmed=True)
    assert boot.request is not None and boot.owner.record['standalone_update']['phase']=='waiting_for_close'
    status=adapter.status(); assert status.phase=='waiting_for_close' and status.request_sha256==pending.request_sha256
    assert adapter.pending_request is pending
    assert adapter.cancel_update(pending).status=='cancelled_before_apply'
    assert boot.request is None and adapter.status().request_sha256 is None
    assert not root.close_intent


def test_actual_adapter_reopen_status_recovery_and_close_route(standalone, stock_spawn, monkeypatch):
    boot,request,(root,client,_,_)=activate_over_control(standalone,stock_spawn,monkeypatch)
    adapter=adapter_exchange(root,client)
    status=adapter.status(); assert status.phase=='applying' and status.request_sha256==request['request_sha256']
    pending=adapter.pending_request
    assert adapter.recover_update(pending,confirmed=True).status=='recovery_waiting_for_close'
    assert root.after_close_operation=='recover' and not root.close_intent
    assert adapter.request_close() and root.close_intent
    stock_spawn[1].update(code=0,members=0)
    root.after_reader_close()
    assert boot.owner.record['standalone_update']['phase']=='recovering'


def test_actual_adapter_publication_retry_without_pending_intent_is_held(standalone, stock_spawn, monkeypatch):
    boot, request, (root, client, _, _) = activate_over_control(standalone, stock_spawn, monkeypatch)
    adapter = adapter_exchange(root, client)
    assert adapter.status().request_sha256 == request['request_sha256']
    pending = adapter.pending_request
    with pytest.raises(ValueError, match='original_root_request_action_held'):
        adapter.retry_connection(pending)
    assert adapter.pending_request is pending and not root.close_intent
    assert boot.request is not None and boot.owner.record['standalone_update']['phase'] == 'applying'


def activate_over_control(standalone, stock_spawn, monkeypatch):
    f, boot, _ = standalone
    root, client, _, _ = connected(stock_spawn, monkeypatch)
    add_schema_package(f, '0.1.0-alpha.9')
    offer = exchange(root, client, 'check')
    assert offer['status'] == 'available'
    request = exchange(root, client, 'confirm', {'manifest_sha256': offer['manifest_sha256']})
    assert request['status'] == 'waiting_for_close'
    exchange(root, client, 'close')
    stock_spawn[1].update(code=0, members=0)
    assert root.after_reader_close().installed.phase == 'activated'
    stock_spawn[1].update(code=None, members=1)
    return boot, request, connected(stock_spawn, monkeypatch)


def test_control_A_close_apply_B_close_finishes_target_health(standalone, stock_spawn, monkeypatch):
    boot, request, (root, client, _, _) = activate_over_control(standalone, stock_spawn, monkeypatch)
    exchange(root, client, 'close')
    stock_spawn[1].update(code=0, members=0)
    assert root.after_reader_close().phase == 'healthy'
    assert boot.request is None and 'standalone_update' not in boot.owner.record


def test_control_recovery_B_close_A_close_finishes_prior_health(standalone, stock_spawn, monkeypatch):
    boot, request, (root, client, _, _) = activate_over_control(standalone, stock_spawn, monkeypatch)
    assert exchange(root, client, 'recover', {'request_sha256': request['request_sha256']})['status'] == 'recovery_waiting_for_close'
    exchange(root, client, 'close')
    stock_spawn[1].update(code=0, members=0)
    root.after_reader_close()
    assert boot.owner.record['standalone_update']['phase'] == 'recovering'
    stock_spawn[1].update(code=None, members=1)
    root, client, _, _ = connected(stock_spawn, monkeypatch)
    exchange(root, client, 'close')
    stock_spawn[1].update(code=0, members=0)
    root.after_reader_close()
    assert boot.request is None and 'standalone_update' not in boot.owner.record
    assert boot.binding.current().selected.version == '0.1.0-alpha.8'


def test_control_abnormal_target_exit_cannot_accept_health(standalone, stock_spawn, monkeypatch):
    boot, request, (root, client, _, _) = activate_over_control(standalone, stock_spawn, monkeypatch)
    exchange(root, client, 'close')
    stock_spawn[1].update(code=17, members=0)
    with pytest.raises(ValueError, match='normal_control_reader_exit'):
        root.after_reader_close()
    assert boot.request is not None and boot.owner.record['standalone_update']['phase'] == 'applying'


def test_child_waits_across_idle_intervals_without_resending(stock_spawn, monkeypatch):
    root, client, replies, requests = connected(stock_spawn, monkeypatch)
    waits = []
    def delayed(**kwargs):
        waits.append(kwargs['timeout'])
        if len(waits) < 3:
            raise native.LaunchHold('control_channel_idle_timeout')
        root.handle_next()
        return replies.pop(0)
    client.wire.channel.read_frame = delayed
    assert client.request('status')['status'] == 'waiting'
    assert len(waits) == 3 and max(waits) <= 1
    assert client.wire.outgoing == 2 and not requests and client.outstanding is None


def test_child_resumes_late_correlated_reply_without_new_request(stock_spawn, monkeypatch):
    root, client, replies, requests = connected(stock_spawn, monkeypatch)
    import suite_core.encyclopedia.control_entry as control
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(control.time, 'monotonic', lambda: next(ticks))
    client.wire.channel.read_frame = lambda **kwargs: (_ for _ in ()).throw(native.LaunchHold('control_channel_idle_timeout'))
    with pytest.raises(native.LaunchHold, match='reply_pending_timeout'):
        client.request('status', overall_timeout=1)
    original = client.outstanding
    assert len(requests) == 1
    with pytest.raises(ValueError, match='reply_still_pending'):
        client.request('check')
    assert client.outstanding is original and len(requests) == 1
    root.handle_next()
    client.wire.channel.read_frame = lambda **kwargs: replies.pop(0)
    monkeypatch.setattr(control.time, 'monotonic', lambda: 10)
    assert client.resume_outstanding()['status'] == 'waiting'
    assert client.outstanding is None and client.wire.outgoing == 2


def test_root_returns_correlated_holds_for_rejected_operations(stock_spawn, monkeypatch):
    root, client, _, _ = connected(stock_spawn, monkeypatch)
    payload = {'id': 'c' * 32, 'operation': 'status', 'body': {'arbitrary_path': 'ignored'}}
    client.wire.send('encyclopedia-request', payload)
    assert root.handle_next()['reason'] == 'unsupported_operation_or_body'
    assert client.wire.receive('encyclopedia-reply')['payload']['id'] == payload['id']
    result = exchange(root, client, 'confirm', {'manifest_sha256': '0' * 64})
    assert result == {'status': 'held', 'reason': 'operation_held', 'operation': 'confirm'}


def test_root_returns_correlated_hold_for_non_string_operation(stock_spawn, monkeypatch):
    root, client, _, _ = connected(stock_spawn, monkeypatch)
    payload = {'id': 'd' * 32, 'operation': ['status'], 'body': {}}
    client.wire.send('encyclopedia-request', payload)
    assert root.handle_next()['reason'] == 'unsupported_operation_or_body'
    assert client.wire.receive('encyclopedia-reply')['payload']['id'] == payload['id']


def test_control_close_cannot_settle_a_different_attempt(stock_spawn, monkeypatch):
    boot, state, _ = stock_spawn
    root, client, _, _ = connected(stock_spawn, monkeypatch)
    exchange(root, client, 'close')
    boot.owner.record['standalone_encyclopedia']['attempt']['generation'] += 1
    state.update(code=0, members=0)
    with pytest.raises(ValueError, match='exact_original_control_reader_attempt'):
        root.after_reader_close()
    assert boot.owner.native_transport is not None


def test_compiled_root_starts_original_backend_before_ready_control(monkeypatch):
    from suite_core.encyclopedia import bootstrap_entry, entry_binding, control_entry
    events = []
    class Backend:
        def launch_current(self): events.append('launch')
    backend = Backend()
    factory = object()
    backend.owner = SimpleNamespace(_standalone_control_factory=factory)
    backend.subject = SimpleNamespace(reader=SimpleNamespace(factory=factory))
    monkeypatch.setattr(bootstrap_entry, 'StandaloneEncyclopediaBootstrap', Backend)
    class Control:
        def __init__(self, owner):
            assert owner is backend
            events.append('control')
        def accept_ready(self): events.append('ready')
    monkeypatch.setattr(bootstrap_entry, 'from_compiled_profile', lambda: backend)
    monkeypatch.setattr(control_entry, 'EncyclopediaRootControl', Control)
    result = entry_binding.start_compiled_control()
    assert result[0] is backend and events == ['launch', 'control', 'ready']


def test_inactive_compiled_entry_preserves_legacy_renderer_without_root(monkeypatch):
    from suite_core.encyclopedia import bootstrap_entry, entry_binding
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', None)
    assert entry_binding.start_compiled_control() is None
    seen = []
    assert entry_binding.render_compiled_child(lambda **args: seen.append(args) or 17,
        ('--theme', 'dark')) == 17
    assert seen == [{'argv': ('--theme', 'dark')}]
    with pytest.raises(ValueError, match='compiled_standalone_profile_unavailable'):
        entry_binding.render_compiled_child(lambda **args: pytest.fail('No renderer before admission'),
            ('--encyclopedia-code-root', 'untrusted-locator'))


def test_compiled_child_admits_before_renderer_and_keeps_exact_arguments(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from suite_core.encyclopedia import bootstrap_entry, entry_binding, control_entry
    events = []
    profile = entry_binding.StandaloneEntryProfile(tmp_path, tmp_path/'data', tmp_path/'settings.json',
        'a'*64, b'synthetic-public-trust', 'b'*64, 'beta', None)
    child = SimpleNamespace(admitted=SimpleNamespace(renderer_arguments=('--theme', 'light'),
        settings_path=tmp_path/'admitted-settings.json'))
    def admitted(argv, **trust):
        assert argv == ('--encyclopedia-code-root', 'locator', '--theme', 'light')
        assert trust == {'trust_bytes': profile.trust_bytes, 'trust_sha256': profile.trust_sha256}
        events.append('admitted')
        return child
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', profile)
    monkeypatch.setattr(control_entry.EncyclopediaChildControl, 'from_inherited_entry', admitted)
    def renderer(**values):
        assert events == ['admitted']
        assert values == {'argv': ('--theme', 'light'), 'settings_path': child.admitted.settings_path,
            'entry_control': child}
        events.append('renderer')
        return 19
    assert entry_binding.render_compiled_child(renderer,
        ('--encyclopedia-code-root', 'locator', '--theme', 'light')) == 19
    assert events == ['admitted', 'renderer']


def test_compiled_start_failure_retains_original_root(monkeypatch):
    from suite_core.encyclopedia import bootstrap_entry, entry_binding
    class Backend:
        def launch_current(self): raise RuntimeError('synthetic start failure')
    backend = Backend()
    factory = object()
    backend.owner = SimpleNamespace(_standalone_control_factory=factory)
    backend.subject = SimpleNamespace(reader=SimpleNamespace(factory=factory))
    monkeypatch.setattr(bootstrap_entry, 'StandaloneEncyclopediaBootstrap', Backend)
    monkeypatch.setattr(bootstrap_entry, 'from_compiled_profile', lambda: backend)
    monkeypatch.setattr(entry_binding, '_HELD_COMPILED_ENTRIES', [])
    with pytest.raises(RuntimeError, match='synthetic start failure') as caught:
        entry_binding.start_compiled_control()
    assert caught.value.encyclopedia_root_owner is backend
    assert entry_binding._HELD_COMPILED_ENTRIES == [backend]


def test_initial_ready_failure_retains_already_constructed_control(monkeypatch):
    from suite_core.encyclopedia import bootstrap_entry, entry_binding, control_entry
    events=[];created=[]
    class Backend:
        def launch_current(self):events.append('launch')
    backend=Backend();factory=object()
    backend.owner=SimpleNamespace(_standalone_control_factory=factory)
    backend.subject=SimpleNamespace(reader=SimpleNamespace(factory=factory))
    class Control:
        def __init__(self,owner):assert owner is backend;created.append(self)
        def accept_ready(self):raise native.LaunchHold('original_control_ready_deadline')
    monkeypatch.setattr(bootstrap_entry,'StandaloneEncyclopediaBootstrap',Backend)
    monkeypatch.setattr(control_entry,'EncyclopediaRootControl',Control)
    monkeypatch.setattr(entry_binding,'_HELD_COMPILED_ENTRIES',[])
    with pytest.raises(native.LaunchHold,match='original_control_ready_deadline') as caught:
        entry_binding.start_original_reader(backend)
    assert caught.value.encyclopedia_root_owner is backend
    assert caught.value.encyclopedia_root_control is created[0]
    assert entry_binding._HELD_COMPILED_ENTRIES==[backend,created[0]]
    assert events==['launch'] and len(created)==1


def test_compiled_renderer_failure_retains_original_child_control(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from suite_core.encyclopedia import bootstrap_entry, entry_binding, control_entry
    profile = entry_binding.StandaloneEntryProfile(tmp_path, tmp_path/'data', tmp_path/'settings.json',
        'a'*64, b'synthetic-public-trust', 'b'*64, 'beta', None)
    child = SimpleNamespace(admitted=SimpleNamespace(renderer_arguments=(), settings_path=tmp_path/'settings.json'))
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', profile)
    monkeypatch.setattr(entry_binding, '_HELD_COMPILED_ENTRIES', [])
    monkeypatch.setattr(control_entry.EncyclopediaChildControl, 'from_inherited_entry', lambda *args, **kw: child)
    def failed(**kwargs): raise RuntimeError('synthetic renderer failure')
    with pytest.raises(RuntimeError, match='synthetic renderer failure') as caught:
        entry_binding.render_compiled_child(failed, ('--encyclopedia-code-root', 'locator'))
    assert caught.value.encyclopedia_child_control is child
    assert entry_binding._HELD_COMPILED_ENTRIES == [child]
