"""Source-only original-object contract tests. No native process is created."""
from copy import deepcopy
import ctypes
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from core import manager_launch_owner as native
from suite_core.encyclopedia import custody_entry as custody
from tests.test_encyclopedia_entry_control import stock_spawn
from tests.test_standalone_encyclopedia_update import standalone
from tests.test_encyclopedia_installation import no_external_actions


def test_fresh_job_collision_holds_original_handle_and_lease(stock_spawn):
    boot, state, api = stock_spawn
    def existing(*args):
        ctypes.set_last_error(183)
        return 70
    api.CreateJobObjectW = existing
    with pytest.raises(native.LaunchHold, match='fresh_original_job'):
        boot.launch_current()
    t = boot.owner._standalone_root_custody
    assert t.job == 70 and t.job_state == 'existing_object_hold'
    assert boot.owner.fd is not None and state['creates'] == state['custody_creates'] == 0
    assert 70 not in state['closed']


def test_query_only_custody_ready_precedes_reader_birth(stock_spawn):
    boot, state, api = stock_spawn
    events, rights = [], []
    create, duplicate = api.CreateProcessW, api.DuplicateHandle
    def record_create(*args):
        role = 'custody' if '--encyclopedia-custody' in args[1].value else 'reader'
        if role == 'reader':
            assert boot.owner.native_transport.custody_session.phase == 'ready'
            assert boot.owner.record['standalone_custody']['phase'] == 'ready'
        events.append(role)
        return create(*args)
    def record_duplicate(*args):
        rights.append((args[1], args[4], args[5], args[6]))
        return duplicate(*args)
    api.CreateProcessW, api.DuplicateHandle = record_create, record_duplicate
    boot.launch_current()
    assert events == ['custody', 'reader']
    assert (70, 0x4, True, 0) in rights
    assert all(access in {0x4, 0x1000 | 0x100000} for _, access, _, _ in rights)
    assert len([key for key, value in state['attrs'] if key == 0x2000D]) == 1
    assert boot.owner.children.keys() == {'encyclopedia'}
    assert state['custody_creates'] == state['custody_resumes'] == 1


@pytest.mark.parametrize('failure', ['partial', 'ready', 'duplicate'])
def test_partial_custody_or_lost_ready_prevents_reader_and_retains_lease(stock_spawn, monkeypatch, failure):
    boot, state, api = stock_spawn
    if failure == 'partial':
        create = api.CreateProcessW
        api.CreateProcessW = lambda *args: create(*args) and False
    elif failure == 'ready':
        monkeypatch.setattr(native.WindowsControlChannel, 'read_frame',
            lambda *args, **kwargs: (_ for _ in ()).throw(native.LaunchHold('custody_ready_lost')))
    else:
        duplicate = api.DuplicateHandle
        def failed_duplicate(*args):
            duplicate(*args)
            return False
        api.DuplicateHandle = failed_duplicate
    with pytest.raises(native.LaunchHold):
        boot.launch_current()
    t = boot.owner._standalone_root_custody
    assert t.custody is not None and t.custody_session is not None
    assert boot.owner.fd is not None and boot.owner.marker.exists()
    assert state['creates'] == 0 and t.job == 70
    if failure == 'partial':
        assert t.custody.creation_outputs() == (83, 84, 202)
        assert t.custody.creation_state == 'uncertain' and state['custody_resumes'] == 0
    if failure == 'duplicate':
        assert t.custody_session.job_slot.value == 91
        assert t.custody.raw_endpoints['custody_job'] == 91


def test_lost_custodian_refuses_live_scope_and_drain(stock_spawn):
    boot, state, api = stock_spawn
    boot.launch_current()
    state['custody_code'] = 1
    t = boot.owner.native_transport
    with pytest.raises(native.LaunchHold, match='live_original_custody'):
        t.assert_accounted_live_scope()
    state['members'] = 0
    with pytest.raises(native.LaunchHold, match='live_original_custody'):
        t.close_after_drain()
    assert t.job == 70 and boot.owner.fd is not None


def test_drain_requires_ack_and_exit_before_job_or_lease_retirement(stock_spawn, monkeypatch):
    boot, state, api = stock_spawn
    boot.launch_current()
    state['code'] = 0; state['members'] = 0
    assert boot.poll_reader()
    anchor = boot.owner._standalone_root_custody
    session = anchor.custody_session
    read = session.wire.channel.read_frame
    monkeypatch.setattr(session.wire.channel, 'read_frame',
        lambda **kwargs: (_ for _ in ()).throw(native.LaunchHold('custody_drain_ack_lost')))
    with pytest.raises(native.LaunchHold, match='drain_ack_lost'):
        boot.close()
    assert anchor.job == 70 and boot.owner.fd is not None
    assert session.drain_sent and not session.resources_closed
    sent = len(state['custody_frames'])
    monkeypatch.setattr(session.wire.channel, 'read_frame', lambda **kwargs: read(**kwargs))
    boot.close()
    assert len(state['custody_frames']) == sent  # original reply, no resend
    assert session.resources_closed and anchor.job is None
    assert boot.owner.record['standalone_custody']['phase'] == 'drained'
    assert boot.owner.fd is None  # only final root Close releases the lease


def custody_ack_before_exit(stock_spawn,monkeypatch,*,outcome):
    """Original-handle/stock-wire doubles; ACK precedes process exit observation."""
    boot,state,api=stock_spawn
    boot.launch_current();state.update(code=0,members=0);assert boot.poll_reader()
    anchor=boot.owner._standalone_root_custody;session=anchor.custody_session
    read=session.wire.channel.read_frame
    def acknowledged_but_running(**kwargs):
        raw=read(**kwargs);state['custody_code']=None;return raw
    monkeypatch.setattr(session.wire.channel,'read_frame',acknowledged_but_running)
    original_wait=api.WaitForSingleObject;waits=[]
    original_exit=api.GetExitCodeProcess
    if outcome=='query_failure':
        api.GetExitCodeProcess=lambda handle,output:False if handle==83 else original_exit(handle,output)
    def wait(handle,timeout):
        if handle==83 and timeout>0:
            waits.append((handle,timeout))
            assert session.drain_sent and session.drain_acknowledged
            assert not session.resources_closed and not session.exit_confirmed
            assert session.child.process._handle==83 and anchor.job==70 and boot.owner.fd is not None
            assert len([f for f in state['custody_frames'] if f['kind']=='custody-drain'])==1
            if outcome in ('zero','query_failure'):state['custody_code']=0
            elif outcome=='nonzero':state['custody_code']=7
        return original_wait(handle,timeout)
    api.WaitForSingleObject=wait
    return boot,state,anchor,session,waits


def test_positive_custody_ack_waits_same_original_handle_for_actual_zero_exit(stock_spawn,monkeypatch):
    boot,state,anchor,session,waits=custody_ack_before_exit(stock_spawn,monkeypatch,outcome='zero')
    boot.close()
    assert waits==[(83,5000)] and session.exit_confirmed and session.resources_closed
    assert anchor.job is None and boot.owner.fd is None
    assert boot.owner.record['standalone_custody']['phase']=='drained'


@pytest.mark.parametrize('outcome',['timeout','nonzero','query_failure'])
def test_custody_ack_timeout_nonzero_or_query_failure_retains_original_resources(stock_spawn,monkeypatch,outcome):
    boot,state,anchor,session,waits=custody_ack_before_exit(stock_spawn,monkeypatch,outcome=outcome)
    expected=OSError if outcome=='query_failure' else native.LaunchHold
    with pytest.raises(expected):boot.close()
    assert waits==[(83,5000)] and session.drain_sent and session.drain_acknowledged
    assert not session.exit_confirmed and not session.resources_closed
    assert session.child.process._handle==83 and anchor.job==70 and boot.owner.fd is not None
    assert 83 not in state['closed'] and 70 not in state['closed']
    assert boot.owner.record['standalone_custody']['phase']=='drain-requested'
    assert len([f for f in state['custody_frames'] if f['kind']=='custody-drain'])==1


def test_timeout_then_explicit_same_custody_retry_does_not_replay_ack_or_drain(stock_spawn,monkeypatch):
    boot,state,anchor,session,waits=custody_ack_before_exit(stock_spawn,monkeypatch,outcome='timeout')
    with pytest.raises(native.LaunchHold):boot.close()
    frames=len(state['custody_frames']);state['custody_code']=0
    boot.close()
    assert len(state['custody_frames'])==frames and waits==[(83,5000),(83,5000)]
    assert session.resources_closed and boot.owner.fd is None and anchor.job is None


@pytest.fixture
def observer(stock_spawn, monkeypatch):
    boot, state, api = stock_spawn
    boot.launch_current()
    session = boot.owner._standalone_root_custody.custody_session
    document = deepcopy(session.document)
    phase = {'bootstrap_exited': False, 'active': 1, 'assigned': 1, 'count': 1,
        'pids': (201,), 'closed': [], 'writes': []}
    child_api = SimpleNamespace(**vars(api))
    child_api.GetProcessId = lambda handle: 202 if handle == -1 else 302
    child_api.QueryFullProcessImageNameW = lambda handle, flags, output, size: setattr(
        output, 'value', str(boot.binding.bootstrap_program)) or True
    child_api.WaitForSingleObject = lambda handle, timeout: 0 if phase['bootstrap_exited'] else 258
    child_api.CloseHandle = lambda handle: phase['closed'].append(handle) or True
    def query(job, kind, output, size, returned):
        assert job == 91
        if kind == 1:
            output._obj.active = phase['active']
        else:
            assert kind == 3
            output._obj.assigned, output._obj.count = phase['assigned'], phase['count']
            for i, pid in enumerate(phase['pids']): output._obj.ids[i] = pid
        return True
    child_api.QueryInformationJobObject = query
    channel = native.WindowsControlChannel(child_api, 10, 11, 90, owns_peer=True,
        epoch=document['channel_epoch'])
    channel.write_frame = lambda raw, **kwargs: phase['writes'].append(json.loads(raw[4:]))
    result = custody.CustodyObserver(channel, 91, document)
    phase.update(active=0, assigned=0, count=0, pids=())
    result.accept_ready()
    phase.update(active=1, assigned=1, count=1, pids=(201,))
    return result, boot, state, phase


def test_original_root_exit_and_complete_zero_job_required(observer):
    o, boot, state, phase = observer
    with pytest.raises(native.LaunchHold, match='bootstrap_exit'):
        o.recover_failed()
    phase['bootstrap_exited'] = True
    with pytest.raises(native.LaunchHold, match='job_still_live'):
        o.recover_failed()
    phase.update(active=0, assigned=1, count=0, pids=())
    with pytest.raises(native.LaunchHold, match='complete_original'):
        o.recover_failed()
    assert o.job == 91 and o.bootstrap_handle == 90 and phase['closed'] == []
    assert boot.owner.marker.exists()


def test_archive_failure_retains_originals_and_same_admission_for_explicit_retry(observer, monkeypatch):
    o, boot, state, phase = observer
    phase.update(bootstrap_exited=True, active=0, assigned=0, count=0, pids=())
    raw = boot.owner.marker.read_bytes()
    boot.owner.abandon()  # simulated root death; original marker is retained
    preserve = Path.read_bytes
    def fail_archive(path):
        if path.name.endswith('.retired.json'):
            raise OSError('synthetic archive readback failure')
        return preserve(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', fail_archive)
        with pytest.raises(OSError, match='archive readback'):
            o.recover_failed()
    admission = o.admission
    assert o.recovery_attempted and o.step() == 'held'  # no automatic archival replay
    assert o.job == 91 and o.channel.peer_handle == 90 and not o.originals_closed
    assert phase['closed'] == [] and boot.owner.marker.read_bytes() == raw
    with pytest.raises(native.LaunchHold):
        native.DeploymentOwner(boot.owner.root, data_root=boot.owner.data_root).acquire()
    # Synthetic descriptors are not native OS fds; preserve this boundary double.
    monkeypatch.setattr(o.channel, 'close', lambda: None)
    result = o.recover_failed()
    assert o.admission is admission and Path(result.archived_marker).read_bytes() == raw
    assert phase['closed'] == [91, 90] and o.originals_closed
    assert not boot.owner.marker.exists()


def test_changed_custody_role_or_lifecycle_cannot_issue_proof(observer):
    o, boot, state, phase = observer
    phase.update(bootstrap_exited=True, active=0, assigned=0, count=0, pids=())
    record = json.loads(boot.owner.raw)
    record['standalone_custody']['context']['owner'] = '0' * 32
    raw = native._bytes(record)
    boot.owner.marker.write_bytes(raw)
    boot.owner.abandon()
    with pytest.raises(native.LaunchHold, match='custody_context'):
        o.recover_failed()
    assert boot.owner.marker.read_bytes() == raw and o.job == 91 and o.admission is None


def test_replaced_original_job_cannot_issue_proof(observer):
    o, boot, state, phase = observer
    phase.update(bootstrap_exited=True, active=0, assigned=0, count=0, pids=())
    o.job = 92
    with pytest.raises(native.LaunchHold, match='bootstrap_exit'):
        o.recover_failed()
    assert phase['closed'] == [] and boot.owner.marker.exists()


def test_normal_observer_drain_retries_only_unclosed_original(observer, monkeypatch):
    o, boot, state, phase = observer
    phase.update(active=0, assigned=0, count=0, pids=())
    reads = []
    def receive(*args, **kwargs):
        reads.append(True)
        return {'payload': {}}
    monkeypatch.setattr(o.wire, 'receive', receive)
    monkeypatch.setattr(o.channel, 'close', lambda: None)
    close = o.channel.api.CloseHandle
    o.channel.api.CloseHandle = lambda handle: False if handle == 90 else close(handle)
    with pytest.raises(native.LaunchHold, match='bootstrap_close_failed'):
        o.step()
    assert o.draining and o.job is None and o.channel.peer_handle == 90
    assert phase['closed'] == [91]
    o.channel.api.CloseHandle = close
    assert o.step() == 'closed' and len(reads) == 1
    assert phase['closed'] == [91, 90]
    assert [f['kind'] for f in phase['writes']] == ['custody-ready', 'custody-drained']


def test_successful_archive_cleanup_retry_does_not_reissue_physical_proof(observer, monkeypatch):
    o, boot, state, phase = observer
    phase.update(bootstrap_exited=True, active=0, assigned=0, count=0, pids=())
    boot.owner.abandon()
    close = o.channel.api.CloseHandle
    o.channel.api.CloseHandle = lambda handle: False if handle == 90 else close(handle)
    with pytest.raises(native.LaunchHold, match='bootstrap_close_failed'):
        o.recover_failed()
    disposition = o.disposition
    assert disposition is not None and o.job is None and phase['closed'] == [91]
    monkeypatch.setattr(o.recovery, 'archive_failed_normal',
        lambda *args: (_ for _ in ()).throw(AssertionError('must reuse disposition')))
    monkeypatch.setattr(o.channel, 'close', lambda: None)
    o.channel.api.CloseHandle = close
    assert o.recover_failed() is disposition
    assert phase['closed'] == [91, 90]


def test_custody_loss_at_reader_native_create_boundary_holds_before_birth(stock_spawn):
    boot, state, api = stock_spawn
    attribute = api.UpdateProcThreadAttribute
    def lost_at_job_assignment(*args):
        result = attribute(*args)
        if args[2] == 0x2000D:
            state['custody_code'] = 1
        return result
    api.UpdateProcThreadAttribute = lost_at_job_assignment
    with pytest.raises(native.LaunchHold, match='live_original_custody'):
        boot.launch_current()
    t = boot.owner.native_transport
    assert t.creation_state == 'not_attempted' and state['creates'] == 0
    assert t.custody.process._handle == 83 and t.job == 70 and boot.owner.fd is not None


def test_reader_original_retained_before_attribute_retirement_can_fail(stock_spawn):
    boot, state, api = stock_spawn
    delete = api.DeleteProcThreadAttributeList
    def fail_reader_attributes(*args):
        if state['creates']:
            raise OSError('synthetic reader attribute retirement failure')
        return delete(*args)
    api.DeleteProcThreadAttributeList = fail_reader_attributes
    with pytest.raises(OSError, match='attribute retirement'):
        boot.launch_current()
    t = boot.owner.native_transport
    assert t.creation_state == 'born' and t.process._handle == 81
    assert boot.owner.children['encyclopedia'] is t.process
    assert state['resumes'] == 1 and boot.owner.fd is not None


@pytest.mark.parametrize('changed_binding', [False, True])
def test_compiled_inherited_entry_checks_original_offer_before_ready(observer, monkeypatch, changed_binding):
    o, boot, state, phase = observer
    document = deepcopy(o.document)
    frame = {'protocol': 1, 'sequence': 1, 'kind': 'custody-offer',
        'binding_sha256': ('0' * 64 if changed_binding else sha256(native._bytes(document)).hexdigest()),
        'payload': {'binding': document}}
    body = native._bytes(frame)
    monkeypatch.setattr(o.channel, 'read_frame', lambda **kwargs: len(body).to_bytes(4, 'little') + body)
    monkeypatch.setattr(native.WindowsControlChannel, 'from_inherited_environment',
        classmethod(lambda cls: o.channel))
    monkeypatch.setattr(sys, 'executable', str(boot.binding.bootstrap_program))
    monkeypatch.setattr(sys, 'argv', [str(boot.binding.bootstrap_program), '--encyclopedia-custody'])
    monkeypatch.setenv('COLLECTIGEN_CUSTODY_JOB', '91')
    retained = []
    monkeypatch.setattr(custody, '_HELD_CUSTODIANS', retained)
    writes = len(phase['writes'])
    phase.update(active=0, assigned=0, count=0, pids=())
    if changed_binding:
        with pytest.raises(native.LaunchHold, match='offer_binding'):
            custody.CustodyObserver.from_inherited_entry(bootstrap_sha256=boot.binding.bootstrap_sha256)
        assert len(phase['writes']) == writes
        assert retained[0]['job'] == 91 and retained[0]['channel'] is o.channel
    else:
        admitted = custody.CustodyObserver.from_inherited_entry(bootstrap_sha256=boot.binding.bootstrap_sha256)
        assert admitted.ready and admitted.job == 91 and admitted.bootstrap_handle == 90
    assert phase['closed'] == []


def test_uncertain_original_bootstrap_wait_never_reaches_drain(observer):
    o, boot, state, phase = observer
    o.channel.api.WaitForSingleObject = lambda *args: 0xffffffff
    with pytest.raises(native.LaunchHold, match='wait_uncertain'):
        o.step()
    assert phase['closed'] == [] and not o.draining


def test_shared_manager_birth_keeps_atomic_job_list_without_custody_requirement(stock_spawn):
    boot, state, api = stock_spawn
    t = native.WindowsNormalTransport(owner=boot.owner)
    process, channel = t._birth_suspended((str(boot.binding.current().selected.program),), environment={})
    assert process is t.process and channel is t.channel and t.custody is None
    assert state['creates'] == 1 and state['custody_creates'] == state['resumes'] == 0
    assert state['attrs'][-1] == (0x2000D, (70,))
    assert boot.owner.pending == [{'role': 'manager', 'process': process}]
    assert process.thread == 82 and t.creation_state == 'born'


@pytest.mark.parametrize('argv', [[], ['--other'], ['--encyclopedia-custody', '--extra']])
def test_wrong_fixed_route_refuses_before_native_intake(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', ['TagEncyclopedia.exe', *argv])
    monkeypatch.setattr(native.WindowsControlChannel, 'from_inherited_environment',
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError('must refuse before intake'))))
    with pytest.raises(native.LaunchHold, match='fixed_compiled_custody_argv'):
        custody.CustodyObserver.from_inherited_entry(bootstrap_sha256='a' * 64)


def test_contradictory_bootstrap_context_refuses_native_proof(observer):
    o, boot, state, phase = observer
    changed = deepcopy(o.document)
    changed['context']['bootstrap']['creation_filetime'] += 1
    with pytest.raises(native.LaunchHold, match='original_custody_native_peer'):
        custody.CustodyObserver(o.channel, 91, changed)
    assert phase['closed'] == []


def drain_frame_for(observer):
    body = native._bytes({'protocol': 1, 'sequence': 2, 'kind': 'custody-drain',
        'binding_sha256': observer.wire.proof.binding_sha256, 'payload': {}})
    return len(body).to_bytes(4, 'little') + body


def test_acknowledged_drain_retries_real_endpoint_close_without_resending(observer, monkeypatch):
    o, boot, state, phase = observer
    phase.update(active=0, assigned=0, count=0, pids=())
    o.wire.incoming = 1  # source factory has already admitted the offer
    reads = []
    def read(**kwargs):
        reads.append(True)
        return drain_frame_for(o)
    monkeypatch.setattr(o.channel, 'read_frame', read)
    close_calls = []
    real_close = native.os.close
    def close(fd):
        if fd not in (10, 11):
            return real_close(fd)
        close_calls.append(fd)
        if fd == 11 and close_calls.count(11) == 1:
            raise OSError('synthetic writer endpoint close failure')
    monkeypatch.setattr(native.os, 'close', close)
    with pytest.raises(OSError, match='writer endpoint close'):
        o.step()  # real wire.send succeeds; real channel.close partly retires fds
    assert o.drain_reply_entered and o.drain_reply_sent
    assert o.channel.reader is None and o.channel.writer == 11
    assert [f['kind'] for f in phase['writes']] == ['custody-ready', 'custody-drained']
    assert o.wire.outgoing == 2 and phase['closed'] == [91, 90]
    assert o.step() == 'closed'
    assert o.channel.reader is o.channel.writer is None and o.hold is None
    assert close_calls == [10, 11, 11] and len(reads) == 1
    assert o.wire.outgoing == 2 and len(phase['writes']) == 2
    assert o.step() == 'closed' and close_calls == [10, 11, 11]


def test_uncertain_drain_send_remains_held_and_is_never_replayed(observer, monkeypatch):
    o, boot, state, phase = observer
    phase.update(active=0, assigned=0, count=0, pids=())
    o.wire.incoming = 1
    monkeypatch.setattr(o.channel, 'read_frame', lambda **kwargs: drain_frame_for(o))
    writes = []
    def partial_write(raw, **kwargs):
        writes.append(raw)
        raise native.LaunchHold('synthetic partial acknowledgement write')
    monkeypatch.setattr(o.channel, 'write_frame', partial_write)
    with pytest.raises(native.LaunchHold, match='partial acknowledgement'):
        o.step()
    assert o.drain_reply_entered and not o.drain_reply_sent
    assert o.step() == 'held' and len(writes) == 1 and o.wire.outgoing == 1
    assert o.channel.reader == 10 and o.channel.writer == 11


@pytest.mark.parametrize('failed_query_kind', [1, 3])
def test_consumed_authenticated_drain_retries_original_job_query_without_second_request(observer, monkeypatch, failed_query_kind):
    o, boot, state, phase = observer
    phase.update(active=0, assigned=0, count=0, pids=())
    o.wire.incoming = 1
    reads = []
    def read(**kwargs):
        reads.append(True)
        if len(reads) > 1:
            raise native.LaunchHold('control_channel_idle_timeout')
        return drain_frame_for(o)
    monkeypatch.setattr(o.channel, 'read_frame', read)
    original_query = o.channel.api.QueryInformationJobObject
    queries = []; failed = []
    def query(job, kind, *args):
        queries.append((job, kind))
        assert job == 91
        if kind == failed_query_kind and not failed:
            failed.append(True)
            return False
        return original_query(job, kind, *args)
    monkeypatch.setattr(o.channel.api, 'QueryInformationJobObject', query)
    endpoint_closes = []; real_close = native.os.close
    def close(fd):
        if fd in (10, 11):
            endpoint_closes.append(fd)
        else:
            real_close(fd)
    monkeypatch.setattr(native.os, 'close', close)
    with pytest.raises((native.LaunchHold, OSError)):
        o.step()
    assert o.wire.incoming == 2 and o.wire.received_pending is None
    assert o.drain_received
    assert not o.draining and phase['closed'] == [] and endpoint_closes == []
    assert [f['kind'] for f in phase['writes']] == ['custody-ready']
    assert o.step() == 'closed'  # recovered original query; root sends no second request
    assert len(reads) == 1 and all(job == 91 for job, _ in queries)
    assert phase['closed'] == [91, 90] and endpoint_closes == [10, 11]
    assert o.originals_closed and o.drain_reply_sent
    assert [f['kind'] for f in phase['writes']] == ['custody-ready', 'custody-drained']
    assert o.step() == 'closed'
    assert len(reads) == 1 and endpoint_closes == [10, 11]


def test_authenticated_drain_with_nonempty_payload_does_not_latch_or_query(observer, monkeypatch):
    o, boot, state, phase = observer
    o.wire.incoming = 1
    frame = json.loads(drain_frame_for(o)[4:]); frame['payload'] = {'untrusted': True}
    raw = native._bytes(frame)
    monkeypatch.setattr(o.channel, 'read_frame', lambda **kwargs: len(raw).to_bytes(4, 'little') + raw)
    queries = []
    monkeypatch.setattr(o.channel.api, 'QueryInformationJobObject', lambda *a: queries.append(a) or True)
    with pytest.raises(native.LaunchHold, match='drain_scope_required'):
        o.step()
    assert not o.drain_received and not o.draining and queries == []
    assert phase['closed'] == [] and [f['kind'] for f in phase['writes']] == ['custody-ready']


def test_received_drain_keeps_reobserving_original_live_job_before_settlement(observer, monkeypatch):
    o, boot, state, phase = observer
    o.wire.incoming = 1
    reads = []
    monkeypatch.setattr(o.channel, 'read_frame', lambda **kwargs: reads.append(True) or drain_frame_for(o))
    monkeypatch.setattr(o.channel, 'close', lambda: None)
    with pytest.raises(native.LaunchHold, match='drain_scope_required'):
        o.step()  # original job still has its reader; receiving intent is not drain evidence
    assert o.drain_received and not o.draining and phase['closed'] == []
    with pytest.raises(native.LaunchHold, match='drain_scope_required'):
        o.step()
    assert len(reads) == 1 and phase['closed'] == []
    phase.update(active=0, assigned=0, count=0, pids=())
    assert o.step() == 'closed' and len(reads) == 1
    assert phase['closed'] == [91, 90]
