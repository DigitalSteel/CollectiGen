"""Explicit synthetic native component fixtures; no applications/processes launched.

These exercise actual handle-list construction and endpoint ownership against
inert APIs. They do not qualify Windows inheritance or packaged acceptance.
"""
from contextlib import nullcontext
import ctypes
from ctypes import wintypes as W
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from core import manager_launch_owner as native_owner
from core import training_process_windows as training
from core.ollama_owned_windows import JobChild, WindowsOwnedProcess
from core.workflow_native_channel import WorkflowNativeChannel
from suite_core.installation import InstallMode, SuiteLayout
from suite_core.manager.bootstrap_entry import EntryHelperAdapter, _NormalWire, _wire_bytes
from suite_core.workflow_modules.session import MESSAGE_KINDS


class InertApi:
    def __init__(self):
        self.next_handle = 1000
        self.pipe_calls = 0
        self.fail_pipe = None
        self.closed = []
        self.flags = []
        self.duplicates = []
        self.handle_list = None
        self.job_list = None
        self.create_calls = 0
        self.raise_after_outputs = False

    def handle(self):
        self.next_handle += 1
        return self.next_handle

    def CreatePipe(self, reader, writer, security, size):
        self.pipe_calls += 1
        ctypes.cast(reader, ctypes.POINTER(W.HANDLE))[0] = self.handle()
        ctypes.cast(writer, ctypes.POINTER(W.HANDLE))[0] = self.handle()
        return self.pipe_calls != self.fail_pipe

    def SetHandleInformation(self, handle, mask, flags):
        self.flags.append((getattr(handle, 'value', handle), mask, flags))
        return True

    def GetCurrentProcess(self):
        return -1

    def DuplicateHandle(self, source_process, source, destination_process, output, access, inherit, options):
        self.duplicates.append((source_process, source, destination_process, access, inherit, options))
        ctypes.cast(output, ctypes.POINTER(W.HANDLE))[0] = self.handle()
        return True

    def CloseHandle(self, handle):
        self.closed.append(getattr(handle, 'value', handle))
        return True

    def InitializeProcThreadAttributeList(self, attributes, count, flags, size):
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = 128
        return True

    def UpdateProcThreadAttribute(self, attributes, flags, key, value, size, prior, returned):
        if key == 0x20002:
            self.handle_list = tuple(value)
        if key == 0x2000D:
            self.job_list = tuple(value)
        return True

    def DeleteProcThreadAttributeList(self, attributes):
        return None

    def CreateProcessW(self, program, args, process_security, thread_security, inherit, flags,
                       environment, cwd, startup, output):
        self.create_calls += 1
        info = ctypes.cast(output, ctypes.POINTER(training._ProcessInfo)).contents
        info.process, info.thread, info.pid = 5001, 5002, 77
        self.startup_flags = ctypes.cast(startup, ctypes.POINTER(training._StartupEx)).contents.startup.flags
        if self.raise_after_outputs:
            raise OSError('synthetic failure after native output assignment')
        return True


@pytest.fixture
def endpoints(tmp_path, monkeypatch):
    api = InertApi()
    descriptors = []
    closed_descriptors = []
    fake_crt = SimpleNamespace()
    def open_handle(handle, flags):
        value = 10000 + len(descriptors)
        descriptors.append((value, handle, flags))
        return value
    fake_crt.open_osfhandle = open_handle
    fake_crt.get_osfhandle = lambda descriptor: 9001
    monkeypatch.setitem(sys.modules, 'msvcrt', fake_crt)
    monkeypatch.setattr(native_owner, '_normal_native_api', lambda: api)
    real_close = os.close
    monkeypatch.setattr(os, 'close', lambda fd: closed_descriptors.append(fd) if fd >= 10000 else real_close(fd))
    native = WindowsOwnedProcess.__new__(WindowsOwnedProcess)
    native.job, native.logs = None, {}
    cap = EntryHelperAdapter.__new__(EntryHelperAdapter)
    cap.native, cap.role_base, cap.program_pin = native, 'owned-generate', 'a' * 64
    layout = SuiteLayout(tmp_path / 'suite', tmp_path / 'suite/Data', InstallMode.PORTABLE)
    root_channel = native_owner.WindowsControlChannel(api, 20001, 20002, 901, epoch='2' * 32)
    cap.session = SimpleNamespace(binding=SimpleNamespace(owner_generation='1' * 32),
                                  admitted=SimpleNamespace(layout=layout), wire=SimpleNamespace(channel=root_channel))
    # Explicit synthetic helper reservation boundary; no Root authority asserted.
    cap._slot = lambda token: {'attempt': SimpleNamespace(identity='5' * 32)}
    native.helper_capability = cap
    native.helper_reservation = '4' * 32
    control = native.bind_workflow_control('collectigen.generate')
    job = training.WindowsTrainingJob.__new__(training.WindowsTrainingJob)
    job.api, job.handle, job._owns_handle = api, 7001, True
    job.process = job.thread = job.pid = job.stdout = None
    job.interactive = True
    native.job = job
    command = (str(layout.code_root / 'Apps/collectigen.generate/0.1.0-alpha.1/Generate.exe'), '--synthetic')
    cwd = str(Path(command[0]).parent)
    def prepare():
        return control.prepare(job, command, cwd, {'SYSTEMROOT': 'C:\\Windows'})
    return SimpleNamespace(api=api, native=native, control=control, job=job, cap=cap,
        command=command, cwd=cwd, prepare=prepare, descriptors=descriptors,
        closed_descriptors=closed_descriptors, layout=layout)


def test_binding_is_inert_and_finite(endpoints):
    f = endpoints
    assert f.api.pipe_calls == 0
    assert f.control.channel is None
    with pytest.raises(RuntimeError, match='finite_workflow_control_component_required'):
        WorkflowNativeChannel(f.native, 'collectigen.arbitrary')


def test_exact_three_control_handles_and_query_only_parent_copy(endpoints):
    f = endpoints
    env = f.prepare()
    inherited = f.control.inherited_handles(f.job, f.command, f.cwd, env)
    assert len(set(inherited)) == 3
    assert f.job.handle not in inherited
    assert tuple(f.api.duplicates[0][3:]) == (0x101000, True, 0)
    assert int(env['COLLECTIGEN_CONTROL_READ']) == inherited[0]
    assert int(env['COLLECTIGEN_CONTROL_WRITE']) == inherited[1]
    assert int(env['COLLECTIGEN_LAUNCHER_QUERY']) == inherited[2]
    assert f.control.channel.transport is None
    assert f.control.channel.peer_handle is None


def test_altered_environment_or_command_cannot_inherit_endpoints(endpoints):
    f = endpoints
    env = f.prepare()
    with pytest.raises(RuntimeError, match='original_workflow_spawn_binding_required'):
        f.control.inherited_handles(f.job, f.command, f.cwd, {**env, 'OTHER': 'changed'})
    with pytest.raises(RuntimeError, match='original_workflow_spawn_binding_required'):
        f.control.inherited_handles(f.job, (f.command[0], '--other'), f.cwd, env)


def test_unadmitted_helper_role_refused_before_allocations(endpoints):
    f = endpoints
    f.cap.role_base = 'owned-encyclopedia'
    with pytest.raises(RuntimeError, match='original_typed_workflow_helper_required'):
        f.prepare()
    assert f.api.pipe_calls == 0


def test_injected_control_environment_refused_before_allocations(endpoints):
    f = endpoints
    with pytest.raises(RuntimeError, match='foreign_control_environment_refused'):
        f.control.prepare(f.job, f.command, f.cwd, {'collectigen_control_read': '123'})
    assert f.api.pipe_calls == 0


def test_partial_pipe_outputs_remain_retained_for_explicit_no_creation_cleanup(endpoints):
    f = endpoints
    f.api.fail_pipe = 1
    with pytest.raises(RuntimeError, match='workflow_control_pipe_creation_failed'):
        f.prepare()
    retained = [slot.value for slot in f.control.slots.values() if slot.value]
    assert len(retained) == 2
    assert not f.api.closed
    f.control.close_uncreated()
    assert set(retained).issubset(f.api.closed)
    assert f.job.handle == 7001
    assert f.native.helper_reservation is not None


def test_creation_uncertainty_forbids_no_creation_cleanup(endpoints):
    f = endpoints
    f.prepare()
    f.control.enter_creation(f.job)
    with pytest.raises(RuntimeError, match='workflow_control_creation_uncertain'):
        f.control.close_uncreated()
    assert not f.control.closed
    assert f.control.channel is not None
    assert not f.api.closed


def bind_original_child(f):
    f.control.enter_creation(f.job)
    f.job.process, f.job.thread, f.job.pid = 5001, 5002, 77
    child = JobChild(f.job)
    f.native.logs[child] = None
    f.control.bind_child(child)
    return child


def test_original_child_binding_closes_only_parent_copies_of_inherited_endpoints(endpoints):
    f = endpoints
    env = f.prepare()
    inherited = f.control.inherited_handles(f.job, f.command, f.cwd, env)
    child = bind_original_child(f)
    assert f.control.child is child
    assert f.control.channel.peer_handle == child._handle
    assert set(f.api.closed) == set(inherited)
    assert child._handle not in f.api.closed
    assert f.job.handle not in f.api.closed
    assert not f.closed_descriptors


def test_parent_orientation_proof_reuses_stock_wire_without_root_transport(endpoints, monkeypatch):
    from hashlib import sha256
    f = endpoints
    f.prepare()
    bind_original_child(f)
    manager = {'pid': 10, 'creation_filetime': 100, 'executable_sha256': 'b' * 64}
    child = {'pid': 77, 'creation_filetime': 200, 'executable_sha256': 'a' * 64}
    monkeypatch.setattr(native_owner.WindowsControlChannel, 'local_witness', lambda self: manager)
    monkeypatch.setattr(native_owner.WindowsControlChannel, 'peer_witness', lambda self: child)
    admitted = SimpleNamespace(layout=f.layout, settings_path=f.layout.data_root / 'Manager/generation-settings.json',
                               restart_arguments=('--synthetic',))
    document = {'protocol': 1, 'owner_generation': '1' * 32, 'channel_epoch': f.control.channel.epoch,
        'deployment_root': str(f.layout.code_root), 'data_root': str(f.layout.data_root),
        'settings_path': str(admitted.settings_path), 'mode': 'portable',
        'argv_sha256': sha256(b'["--synthetic"]').hexdigest(), 'entry': child, 'launcher': manager}
    proof = f.control.parent_proof(document, admitted, expected_child_image_sha256='a' * 64)
    wire = _NormalWire(f.control.channel, document, proof, message_kinds=MESSAGE_KINDS)
    assert wire.proof.binding_sha256 == sha256(_wire_bytes(document)).hexdigest()
    assert wire.channel.transport is None
    with pytest.raises(RuntimeError, match='native_workflow_parent_offer_mismatch'):
        f.control.parent_proof({**document, 'owner_generation': '9' * 32}, admitted,
                               expected_child_image_sha256='a' * 64)
    with pytest.raises(RuntimeError, match='independent_workflow_child_pin_required'):
        f.control.parent_proof(document, admitted, expected_child_image_sha256='e' * 64)


def test_endpoint_cleanup_requires_actual_original_drain(endpoints):
    f = endpoints
    f.prepare()
    child = bind_original_child(f)
    f.job.poll = lambda: None
    f.job.active = lambda: 1
    f.native.drained = SimpleNamespace(is_set=lambda: True)
    f.native.drain = None
    with pytest.raises(RuntimeError, match='workflow_control_original_drain_required'):
        f.control.close_after_drain(child)
    assert not f.closed_descriptors
    f.job.poll = lambda: 0
    f.job.active = lambda: 0
    f.control.close_after_drain(child)
    assert f.control.closed
    assert len(f.closed_descriptors) == 2
    assert f.job.handle == 7001
    assert child._handle == 5001
    assert f.native.helper_reservation is not None


@pytest.mark.parametrize('uncertain', [False, True])
def test_stock_spawn_handle_allowlist_and_partial_outputs_are_retained(endpoints, monkeypatch, uncertain):
    f = endpoints
    env = f.prepare()
    expected = f.control.inherited_handles(f.job, f.command, f.cwd, env)
    f.api.raise_after_outputs = uncertain
    monkeypatch.setattr(training, '_external_dll_directory', lambda api: nullcontext())
    monkeypatch.setattr(os, 'set_handle_inheritable', lambda handle, value: None)
    monkeypatch.setattr(os, 'fdopen', lambda descriptor, mode, buffering=0: io.BytesIO())
    if uncertain:
        with pytest.raises(OSError, match='synthetic failure'):
            f.job.spawn_suspended(f.command, f.cwd, env, workflow_control=f.control)
    else:
        f.job.spawn_suspended(f.command, f.cwd, env, workflow_control=f.control)
    assert f.api.create_calls == 1
    assert len(f.api.handle_list) == 5
    assert f.api.handle_list[2:] == expected
    assert f.api.job_list == (7001,)
    assert 7001 not in f.api.handle_list
    assert f.api.startup_flags == 0x100
    assert f.job.process == 5001 and f.job.thread == 5002 and f.job.pid == 77
    assert f.job._workflow_process_info.process == 5001
    assert f.control.creation_entered
    assert not f.control.closed


def test_workflow_scopes_bind_original_grant_and_survive_creation_uncertainty(endpoints):
    f = endpoints
    f.prepare()
    parent = f.control.manager_image_scope
    child = f.control.image_scope
    assert parent.channel is f.cap.session.wire.channel
    for scope in (parent, child):
        assert (scope.owner_generation, scope.attempt_id, scope.reservation) == ('1' * 32, '5' * 32, '4' * 32)
        assert scope.channel._workflow_image_owner is scope
    f.control.enter_creation(f.job)
    with pytest.raises(RuntimeError, match='workflow_control_creation_uncertain'):
        f.control.close_uncreated()
    assert not parent.closed and not child.closed


def test_module_control_drain_does_not_release_manager_root_scope(endpoints):
    f = endpoints
    f.prepare()
    child = bind_original_child(f)
    f.job.poll = lambda: 0
    f.job.active = lambda: 0
    f.native.drained = SimpleNamespace(is_set=lambda: True)
    f.native.drain = None
    f.control.close_after_drain(child)
    assert f.control.image_scope.closed
    assert not f.control.manager_image_scope.closed  # Original helper ACK still pending.
    assert f.native.helper_reservation == '4' * 32


def test_existing_stage_scope_is_not_stolen_by_workflow_prepare(endpoints):
    f = endpoints
    channel = f.cap.session.wire.channel
    channel._begin_image_scope('8' * 32)
    with pytest.raises(native_owner.LaunchHold, match='fresh_original_organizer_image_scope_required'):
        f.prepare()
    assert channel._image_scope == '8' * 32
    assert f.api.pipe_calls == 0
