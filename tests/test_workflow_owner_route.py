"""Synthetic protocol component evidence; no apps, native handles or packages run."""
from hashlib import sha256
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from core.manager_launch_owner import LaunchHold, WindowsControlChannel
from core.ollama_owned_windows import BorrowedProcess, HelperAttempt, JobChild, WindowsOwnedProcess
from core.workflow_connection import WorkflowConnection
from core.workflow_native_channel import WorkflowNativeChannel
from core.workflow_root_selection import RootWorkflowSelection
from suite_core.installation import InstallMode, SuiteLayout
from suite_core.manager.bootstrap_entry import EntryHelperAdapter, NormalEntrySession, _wire_bytes, decode_control_frame
from suite_core.workflow_modules.owner_route import WorkflowControlActor
from suite_core.workflow_modules.session import MESSAGE_KINDS

_ORIGINAL_READ_FRAME = WindowsControlChannel.read_frame


@pytest.fixture
def route(tmp_path, monkeypatch):
    layout = SuiteLayout(tmp_path / 'suite', tmp_path / 'suite/Data', InstallMode.PORTABLE)
    selected = WorkflowConnection('collectigen.generate', layout.code_root / 'Apps/collectigen.generate/0.1.0-alpha.1',
                                  '0.1.0-alpha.1', 'a' * 64, 'b' * 64, 'synthetic')
    selection = RootWorkflowSelection(layout.code_root, layout.data_root, layout.data_root / 'Manager/settings.json',
                                      selected, b'manifest', b'signature', b'trust')
    monkeypatch.setattr(RootWorkflowSelection, 'helper_snapshot', lambda self: (self._context(), 'c' * 64))
    manager = {'pid': 10, 'creation_filetime': 100, 'executable_sha256': 'd' * 64}
    root = {'pid': 9, 'creation_filetime': 90, 'executable_sha256': 'e' * 64}
    child_identity = {'pid': 11, 'creation_filetime': 110, 'executable_sha256': 'c' * 64}
    api = SimpleNamespace(waits={101: 258, 102: 258})
    api.WaitForSingleObject = lambda handle, timeout: api.waits[handle]
    root_channel = WindowsControlChannel(api, 1, 2, 101, epoch='2' * 32)
    channel = WindowsControlChannel(api, 3, 4, 102, epoch='3' * 32)
    root_channel._synthetic_local, root_channel._synthetic_peer = manager, root
    channel._synthetic_local, channel._synthetic_peer = manager, child_identity
    monkeypatch.setattr(WindowsControlChannel, 'local_witness', lambda self: self._synthetic_local.copy())
    monkeypatch.setattr(WindowsControlChannel, 'peer_witness', lambda self: self._synthetic_peer.copy())
    incoming, sent = [], []
    def read_frame(self, timeout=1, *, idle_timeout=None):
        if not incoming:
            raise LaunchHold('control_channel_idle_timeout')
        return incoming.pop(0)
    monkeypatch.setattr(WindowsControlChannel, 'read_frame', read_frame)
    monkeypatch.setattr(WindowsControlChannel, 'write_frame', lambda self, raw, timeout=1: sent.append(
        decode_control_frame(raw, message_kinds=MESSAGE_KINDS)))
    session = NormalEntrySession.__new__(NormalEntrySession)
    session.state = 'ready'
    session.binding = SimpleNamespace(owner_generation='1' * 32, channel_epoch='2' * 32)
    session.admitted = SimpleNamespace(layout=layout)
    session.workflow_selections = {selection.component_id: selection}
    session.wire = SimpleNamespace(channel=root_channel, document={'entry': manager, 'launcher': root},
                                   proof=SimpleNamespace(binding_sha256='f' * 64))
    native = WindowsOwnedProcess.__new__(WindowsOwnedProcess)
    native.start_failed = False
    job = SimpleNamespace(handle=700, process=102, pid=11, thread=None, active=lambda: 0,
                          poll=lambda: None)
    native.job = job
    child = JobChild(job)
    native.logs = {child: None}
    native.helper_reservation = '4' * 32
    native.helper_accounting_held = True
    native.drained = SimpleNamespace(is_set=lambda: True)
    native.drain = None
    cap = EntryHelperAdapter.__new__(EntryHelperAdapter)
    cap.native, cap.session, cap.role_base = native, session, 'owned-generate'
    cap.workflow_selection, cap.program_pin = selection, 'c' * 64
    cap.reviewed = MappingProxyType(selection._context())
    cap.generation = ('1' * 32, '2' * 32, 'f' * 64)
    native.helper_capability = cap
    context = MappingProxyType({**dict(cap.reviewed), 'helper_attempt_id': '5' * 32})
    attempt = HelperAttempt('5' * 32, context)
    native.helper_attempt, native._helper_attempt_capability = attempt, cap
    slot = {'attempt': attempt, 'reservation': native.helper_reservation, 'phase': 'registered',
            'borrowed': BorrowedProcess(102, 11),
            'context_sha256': sha256(json.dumps(dict(context), sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    cap.attempts = {attempt.identity: slot}
    control = WorkflowNativeChannel(native, selection.component_id)
    control.channel, control.child, control.job = channel, child, job
    control.command = selection.command
    native.workflow_control = control
    actor = WorkflowControlActor(control, selection)
    def request(kind, extra=None, *, sequence=None, pin=None):
        raw = _wire_bytes({'protocol': 1, 'kind': kind, 'sequence': sequence or actor.wire.incoming + 1,
            'binding_sha256': pin or actor.wire.proof.binding_sha256,
            'payload': {**actor._scope(), **(extra or {})}})
        incoming.append(len(raw).to_bytes(4, 'little') + raw)
    return SimpleNamespace(actor=actor, request=request, incoming=incoming, sent=sent, native=native,
        slot=slot, control=control, child=child, job=job, api=api, channel=channel, session=session)


def ready(f):
    f.actor.start()
    f.request('workflow-ready')
    assert f.actor.handle_next()
    assert f.actor.state == 'ready'


def test_first_partial_frame_fault_retains_pre_hold_state_and_original_ownership(route, monkeypatch):
    f = route
    ready(f)
    def partial_frame(channel, timeout=1, *, idle_timeout=None):
        channel._read_progress=4
        channel.read_fault={'bytes_received':4,'reason':'LaunchHold'}
        raise LaunchHold('control_channel_timeout')
    monkeypatch.setattr(WindowsControlChannel,'read_frame',partial_frame)
    with pytest.raises(LaunchHold,match='control_channel_timeout'):
        f.actor.handle_next(timeout=.25)
    first=dict(f.actor.first_failure)
    assert first['phase']=='receive' and first['state']=='ready'
    assert first['error']=='control_channel_timeout' and first['read_bytes']==4
    assert first['read_fault'] and not first['local_drain']
    f.actor._hold(RuntimeError('synthetic private error text'))
    assert f.actor.first_failure==first
    assert f.actor.state=='held' and not f.actor.grant_release_authorized
    assert f.native.helper_accounting_held


def test_unknown_peer_failure_diagnostic_uses_only_finite_tokens(route):
    f=route
    ready(f)
    f.actor._hold(RuntimeError('synthetic secret path and prompt'))
    assert f.actor.first_failure['error']=='other'
    assert 'synthetic secret' not in str(f.actor.first_failure)


def test_offer_and_ready_use_original_reserved_lifetime(route):
    f = route
    ready(f)
    assert f.sent[0]['payload']['workspace']['grant_id'] == f.native.helper_reservation
    assert f.sent[0]['payload']['workspace']['attempt_id'] == f.native.helper_attempt.identity
    assert f.sent[0]['sequence'] == 1 and f.sent[1]['sequence'] == 2
    assert f.actor.grant_release_authorized is False
    assert f.actor.handle_next() is False  # Intact idle does not change readiness.
    assert f.actor.state == 'ready'


@pytest.mark.parametrize('bad', ['sequence', 'pin', 'grant'])
def test_foreign_or_stale_request_holds_original_owner(route, bad):
    f = route
    ready(f)
    f.request('workflow-check', {'grant_id': '9' * 32} if bad == 'grant' else None,
              sequence=1 if bad == 'sequence' else None, pin='e' * 64 if bad == 'pin' else None)
    with pytest.raises((ValueError, RuntimeError)):
        f.actor.handle_next()
    assert f.actor.state == 'held'
    assert f.native.helper_accounting_held
    assert f.control.child is f.child
    assert not f.actor.is_drained


def test_return_is_finite_and_only_records_navigation(route):
    f = route
    ready(f)
    f.request('workflow-return', {'target': 'models'})
    f.actor.handle_next()
    assert f.actor.requested_return == 'models'
    assert f.native.helper_reservation == '4' * 32
    f.request('workflow-return', {'target': 'execute'})
    with pytest.raises(RuntimeError, match='finite_ready_workflow_return_required'):
        f.actor.handle_next()


def test_manager_close_denies_new_work_without_terminating_child(route):
    f = route
    ready(f)
    f.actor.begin_close()
    f.request('workflow-check')
    f.actor.handle_next()
    assert f.sent[-1]['payload']['state'] == 'closing'
    assert f.job.process == 102
    assert f.native.helper_accounting_held


def test_local_claim_and_expected_exit_still_wait_for_native_settlement(route):
    f = route
    ready(f)
    f.request('workflow-closing')
    f.actor.handle_next()
    f.request('workflow-drained-report', {'local_drained': True})
    f.actor.handle_next()
    assert f.actor.local_drain_claimed and not f.actor.is_drained
    assert f.slot['phase'] == 'registered'
    f.api.waits[102] = 0
    f.job.poll = lambda: 0
    assert f.actor.handle_next() is False
    assert f.actor.waiting_for_native_drain and not f.actor.is_drained
    assert f.actor.error is None
    with pytest.raises(RuntimeError, match='original_workflow_native_settlement_required'):
        f.actor.close_after_native()
    # Explicitly simulate all original native.close postconditions; no real release.
    f.control.closed = True
    f.native.logs.clear()
    f.child._handle = None
    f.job.handle = f.job.process = None
    f.native.helper_reservation = None
    f.native.helper_accounting_held = False
    f.slot['phase'] = 'drained'
    assert f.actor.close_after_native()
    assert f.actor.is_drained
    assert not f.actor.grant_release_authorized


def test_unexpected_exit_is_not_eof_success(route):
    f = route
    ready(f)
    f.api.waits[102] = 0
    f.job.poll = lambda: 0
    with pytest.raises(RuntimeError, match='original_workflow_child_peer_lost'):
        f.actor.handle_next()
    assert f.actor.state == 'held'
    assert not f.actor.is_drained


def test_settings_return_records_exact_draft_without_releasing_writer(route):
    f = route
    ready(f)
    draft = dict(relative_path='generation/drafts/' + 'a' * 32 + '.json', sha256='b' * 64)
    f.request('workflow-return', {'target': 'settings', 'resume_draft': draft})
    f.actor.handle_next()
    assert f.actor.requested_return == 'settings' and f.actor.requested_resume_draft == draft
    assert f.sent[-1]['payload']['resume_draft'] == draft
    assert f.actor.state == 'ready' and not f.actor.is_drained
    assert f.native.helper_accounting_held and f.slot['phase'] == 'registered'


def test_acknowledged_drain_exit_during_read_retains_native_settlement(route, monkeypatch):
    f = route
    ready(f)
    f.request('workflow-closing')
    f.actor.handle_next()
    f.request('workflow-drained-report', {'local_drained': True})
    f.actor.handle_next()
    def exit_during_read(_channel, timeout=1, *, idle_timeout=None):
        f.api.waits[102] = 0
        f.job.poll = lambda: 0
        f.channel.read_fault = {'bytes_received': 0, 'reason': 'LaunchHold'}
        raise LaunchHold('control_channel_eof_or_unavailable')
    monkeypatch.setattr(WindowsControlChannel, 'read_frame', exit_during_read)
    assert f.actor.handle_next() is False
    assert f.actor.waiting_for_native_drain and f.actor.error is None
    assert not f.actor.is_drained and f.native.helper_accounting_held
    assert f.channel.read_fault is not None


def test_root_peer_loss_and_changed_registration_refuse_new_work(route):
    f = route
    ready(f)
    f.api.waits[101] = 0
    with pytest.raises(RuntimeError, match='original_workflow_root_peer_lost'):
        f.actor.handle_next()
    assert f.actor.state == 'held'
    assert f.native.helper_accounting_held


def test_actor_checkpoints_reuse_enrolled_images_and_keep_scopes_on_peer_failure(route, monkeypatch):
    from core import manager_launch_owner as native_owner
    from core.workflow_native_channel import WorkflowImageScope
    f = route
    root_channel = f.session.wire.channel
    f.api.GetCurrentProcess = lambda: 10
    births = {10: root_channel._synthetic_local, 101: root_channel._synthetic_peer,
              102: f.channel._synthetic_peer}
    reads = []
    class SyntheticRetainedImage:
        """Only the physical image-read boundary is synthetic in this protocol test."""
        def __init__(self, api, handle, epoch):
            self.api, self.handle, self.epoch = api, handle, epoch
            reads.append((handle, epoch))
        def identity(self, api, handle, epoch):
            assert (api, handle, epoch) == (self.api, self.handle, self.epoch)
            return births[handle].copy()
        def close(self):
            return None
    monkeypatch.setattr(native_owner, '_RetainedChannelImage', SyntheticRetainedImage)
    monkeypatch.setattr(WindowsControlChannel, 'local_witness',
                        lambda self: self._image_witness('local', self.api.GetCurrentProcess()))
    monkeypatch.setattr(WindowsControlChannel, 'peer_witness',
                        lambda self: self._image_witness('peer', self.peer_handle))
    scopes = [WorkflowImageScope(channel, 'collectigen.generate', custodian=f.native)
              for channel in (root_channel, f.channel)]
    for scope in scopes:
        scope.bind('1' * 32, '5' * 32, '4' * 32)
    ready(f)
    for _ in range(50):
        f.request('workflow-check')
        assert f.actor.handle_next()
    assert len(reads) == 4  # Two exact endpoint images on each original channel.
    f.api.waits[101] = 0
    with pytest.raises(RuntimeError, match='original_workflow_root_peer_lost'):
        f.actor.handle_next()
    assert all(not scope.closed for scope in scopes)
    assert f.native.helper_accounting_held


@pytest.fixture
def timed_native_route(route, monkeypatch):
    """Actual framing/wire/actor; only OS bytes and monotonic time are synthetic."""
    import core.manager_launch_owner as native
    import msvcrt
    f = route
    ready(f)
    state = SimpleNamespace(now=0.0, stream=bytearray(), reads=[], available_at=None,
                            header_delay=.02, body_at=None)
    def sleep(seconds):
        target = state.now + seconds
        if state.available_at is not None and state.now < state.available_at < target:
            target = state.available_at
        state.now = target
    def peek(handle, buffer, size, read, available, remaining):
        visible = state.available_at is not None and state.now >= state.available_at
        count = len(state.stream) if visible else 0
        if state.reads and state.body_at is not None and state.now < state.body_at:
            count = 0
        available._obj.value = count
        return True
    def read(handle, count):
        data = bytes(state.stream[:count])
        del state.stream[:count]
        state.reads.append(len(data))
        if len(state.reads) == 1:
            state.now += state.header_delay
        return data
    monkeypatch.setattr(WindowsControlChannel, 'read_frame', _ORIGINAL_READ_FRAME)
    monkeypatch.setattr(native.time, 'monotonic', lambda: state.now)
    monkeypatch.setattr(native.time, 'sleep', sleep)
    monkeypatch.setattr(native.os, 'read', read)
    monkeypatch.setattr(msvcrt, 'get_osfhandle', lambda fd: fd)
    f.api.PeekNamedPipe = peek
    def queue():
        f.request('workflow-check')
        state.stream.extend(f.incoming.pop())
        state.available_at = state.now + .249
    state.queue = queue
    return f, state


def test_real_frame_survives_idle_poll_boundary_after_quiet_cycles(timed_native_route):
    f, timing = timed_native_route
    for _ in range(3):
        assert f.actor.handle_next(timeout=.25) is False
        assert f.actor.state == 'ready' and f.channel.read_fault is None
    timing.queue()
    sent = len(f.sent)
    assert f.actor.handle_next(timeout=.25) is True
    assert timing.reads[0] == 4 and len(timing.reads) == 2
    assert f.actor.wire.incoming == 2 and len(f.sent) == sent + 1
    assert f.sent[-1]['kind'] == 'workflow-check-ack'
    assert f.channel.read_fault is None and f.actor.state == 'ready'
    assert f.native.helper_accounting_held and not f.actor.grant_release_authorized


@pytest.mark.parametrize('late_body', [False, True])
def test_real_incomplete_frame_holds_without_ack_replay_or_release(timed_native_route, late_body):
    f, timing = timed_native_route
    timing.queue()
    if late_body:
        timing.body_at = timing.available_at + 1.1
    else:
        del timing.stream[4:]
    sent = len(f.sent)
    with pytest.raises(LaunchHold, match='control_channel_timeout'):
        f.actor.handle_next(timeout=.25)
    assert 1.249 <= timing.now < 1.28
    assert f.channel.read_fault['bytes_received'] == 4
    assert f.actor.state == 'held' and f.actor.first_failure['phase'] == 'receive'
    assert f.actor.wire.incoming == 1 and len(f.sent) == sent
    assert f.native.helper_accounting_held and not f.actor.grant_release_authorized
    assert not f.actor.is_drained and timing.reads == [4]
    timing.now += 1  # Even a now-complete late body cannot be replayed or ACKed.
    with pytest.raises(LaunchHold, match='control_read_recovery_required'):
        f.channel.read_frame(timeout=1, idle_timeout=.25)
    assert timing.reads == [4] and len(f.sent) == sent


def test_default_total_frame_deadline_remains_unchanged(timed_native_route):
    f, timing = timed_native_route
    timing.queue()
    with pytest.raises(LaunchHold, match='control_channel_timeout'):
        f.channel.read_frame(timeout=.25)
    assert timing.reads == [4] and f.channel.read_fault['bytes_received'] == 4


@pytest.mark.parametrize('idle', [0, -1, float('nan'), float('inf'), True, 2])
def test_invalid_idle_budget_refuses_before_consuming_bytes(timed_native_route, idle):
    f, timing = timed_native_route
    timing.queue()
    with pytest.raises(LaunchHold, match='bounded_channel_idle_wait_required'):
        f.channel.read_frame(timeout=1, idle_timeout=idle)
    assert timing.reads == [] and f.channel.read_fault is None
