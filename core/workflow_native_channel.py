"""Opt-in Generate/Train endpoints for the original suspended owned Job spawn.

Uses stock anonymous WindowsControlChannel pipes. No process, Root lease,
transport impersonation, listener, or execution authority is created here.
The caller retains this object before prepare() performs any native allocation.
"""
import ctypes
from ctypes import wintypes as W
from hashlib import sha256
import os
from pathlib import Path
import re

from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


class WorkflowImageScope:
    """Lifetime enrollment of the stock retained-image scope, never a new cache.

    A provisional native channel epoch is retained through offer/grant failure.
    Authenticated helper correlation is attached once available. Only the owning
    channel or positive original lifecycle settlement disposes these handles.
    """
    def __init__(self, channel, component_id, *, custodian, owner_generation=None, attempt_id=None):
        from core.manager_launch_owner import WindowsControlChannel
        require(type(channel) is WindowsControlChannel and custodian is not None
                and component_id in WORKFLOW_EXECUTABLES
                and type(channel.epoch) is str and re.fullmatch('[0-9a-f]{32}', channel.epoch),
                'original_workflow_image_channel_required')
        require(getattr(channel, '_workflow_image_owner', None) is None,
                'workflow_image_scope_overlap')
        for value in (owner_generation, attempt_id):
            require(value is None or type(value) is str and re.fullmatch('[0-9a-f]{32}', value),
                    'workflow_image_lifetime_required')
        self.channel, self.component_id, self.custodian = channel, component_id, custodian
        self.epoch = channel.epoch
        self.owner_generation, self.attempt_id, self.reservation = owner_generation, attempt_id, None
        self.closed = False
        # Stock begin refuses any existing Organizer/staging scope or images.
        with channel.io_lock:
            channel._begin_image_scope(self.epoch)
            channel._workflow_image_owner = self

    def check(self):
        require(not self.closed and self.channel.epoch == self.epoch
                and getattr(self.channel, '_workflow_image_owner', None) is self
                and self.channel._image_scope == self.epoch,
                'same_original_workflow_image_scope_required')

    def bind(self, owner_generation, attempt_id, reservation):
        self.check()
        require(all(type(value) is str and re.fullmatch('[0-9a-f]{32}', value)
                    for value in (owner_generation, attempt_id, reservation))
                and self.owner_generation in (None, owner_generation)
                and self.attempt_id in (None, attempt_id)
                and self.reservation in (None, reservation), 'workflow_image_lifetime_changed')
        self.owner_generation, self.attempt_id, self.reservation = owner_generation, attempt_id, reservation
        return self

    def close(self):
        with self.channel.io_lock:
            if self.closed:
                return
            self.check()
            # Failed file-handle close retains this exact enrollment for recovery.
            self.channel._end_image_scope(self.epoch, workflow_owner=self)
            self.channel._workflow_image_owner = None
            self.closed = True


class WorkflowNativeChannel:
    def __init__(self, native, component_id):
        require(type(component_id) is str and component_id in WORKFLOW_EXECUTABLES,
                'finite_workflow_control_component_required')
        self.native, self.component_id = native, component_id
        self.api = self.channel = self.job = self.child = None
        self.slots, self.descriptors = {}, {}
        self.creation_entered = False
        self.closed = False
        self.command = self.cwd = self.environment = None
        self.image_scope = self.manager_image_scope = None

    def _original(self):
        from core.ollama_owned_windows import WindowsOwnedProcess
        from suite_core.manager.bootstrap_entry import EntryHelperAdapter
        cap = getattr(self.native, 'helper_capability', None)
        require(type(self.native) is WindowsOwnedProcess
                and getattr(self.native, 'workflow_control', None) is self
                and type(cap) is EntryHelperAdapter and cap.native is self.native
                and cap.role_base == 'owned-' + self.component_id.removeprefix('collectigen.'),
                'original_typed_workflow_helper_required')
        return cap

    def prepare(self, job, command, cwd, environment):
        """Allocate retained endpoints after original helper reservation, before birth."""
        import msvcrt
        from uuid import uuid4
        from core.manager_launch_owner import WindowsControlChannel, _normal_native_api
        from core.training_process_windows import WindowsTrainingJob, _Security
        cap = self._original()
        require(self.job is None and self.api is None and not self.closed
                and type(job) is WindowsTrainingJob and job is self.native.job
                and job.process is None and self.native.helper_reservation is not None,
                'single_reserved_workflow_control_required')
        require(Path(command[0]).name == WORKFLOW_EXECUTABLES[self.component_id],
                'exact_workflow_control_executable_required')
        require(len(environment) <= 124 and not any(
                    key.upper().startswith(('COLLECTIGEN_CONTROL_', 'COLLECTIGEN_LAUNCHER_QUERY'))
                    for key in environment), 'foreign_control_environment_refused')
        slot = cap._slot(self.native.helper_reservation)
        manager_channel = cap.session.wire.channel
        scope = getattr(manager_channel, '_workflow_image_owner', None)
        if scope is None:
            scope = WorkflowImageScope(manager_channel, self.component_id, custodian=self.native,
                                       owner_generation=cap.session.binding.owner_generation)
        require(type(scope) is WorkflowImageScope and scope.custodian is self.native
                and scope.component_id == self.component_id, 'original_manager_workflow_image_scope_required')
        self.manager_image_scope = scope
        scope.bind(cap.session.binding.owner_generation, slot['attempt'].identity, self.native.helper_reservation)
        self.job = job
        self.command, self.cwd = tuple(command), str(cwd)
        self.api = _normal_native_api()
        self.slots = {name: W.HANDLE() for name in
                      ('child_read', 'parent_write', 'parent_read', 'child_write', 'parent_query')}
        security = _Security(ctypes.sizeof(_Security), None, True)
        for reader, writer in (('child_read', 'parent_write'), ('parent_read', 'child_write')):
            # Slots exist before each fallible call, including partial outputs.
            ok = self.api.CreatePipe(ctypes.byref(self.slots[reader]), ctypes.byref(self.slots[writer]),
                                     ctypes.byref(security), 65540)
            require(ok, 'workflow_control_pipe_creation_failed')
        for name in ('parent_read', 'parent_write'):
            require(self.api.SetHandleInformation(self.slots[name], 1, 0),
                    'workflow_parent_endpoint_inheritance_refused')
        # Only query/synchronize. Never duplicate a Job, Root lease, or DUP_HANDLE access.
        current = self.api.GetCurrentProcess()
        ok = self.api.DuplicateHandle(current, current, current, ctypes.byref(self.slots['parent_query']),
                                      0x1000 | 0x100000, True, 0)
        require(ok and self.slots['parent_query'].value, 'workflow_parent_query_creation_failed')
        for name, mode in (('parent_read', os.O_RDONLY), ('parent_write', os.O_WRONLY)):
            descriptor = msvcrt.open_osfhandle(self.slots[name].value, mode | os.O_BINARY | os.O_NOINHERIT)
            self.descriptors[name] = descriptor
            self.slots[name].value = None  # CRT now owns exactly this original.
        epoch = uuid4().hex
        self.channel = WindowsControlChannel(self.api, self.descriptors['parent_read'],
            self.descriptors['parent_write'], None, epoch=epoch)
        self.descriptors.clear()  # Channel owns both descriptors; retained on self.
        self.image_scope = WorkflowImageScope(self.channel, self.component_id, custodian=self)
        self.image_scope.bind(cap.session.binding.owner_generation, slot['attempt'].identity,
                              self.native.helper_reservation)
        self.environment = dict(environment,
            COLLECTIGEN_CONTROL_READ=str(self.slots['child_read'].value),
            COLLECTIGEN_CONTROL_WRITE=str(self.slots['child_write'].value),
            COLLECTIGEN_LAUNCHER_QUERY=str(self.slots['parent_query'].value),
            COLLECTIGEN_CONTROL_EPOCH=epoch)
        return dict(self.environment)

    def inherited_handles(self, job, command, cwd, environment):
        self._original()
        require(job is self.job and job is self.native.job and self.channel is not None
                and not self.creation_entered and not self.closed
                and tuple(command) == self.command and str(cwd) == self.cwd
                and environment == self.environment, 'original_workflow_spawn_binding_required')
        values = tuple(self.slots[name].value for name in ('child_read', 'child_write', 'parent_query'))
        require(all(type(value) is int and value > 0 for value in values)
                and len(set(values)) == 3 and job.handle not in values,
                'exact_workflow_inherited_handles_required')
        return values

    def enter_creation(self, job):
        require(job is self.job and not self.creation_entered and self.channel is not None,
                'single_workflow_creation_required')
        self.creation_entered = True

    def bind_child(self, child):
        """Bind the original retained suspended child; never open by PID."""
        self._original()
        require(self.creation_entered and self.child is None and child in self.native.logs
                and child.job is self.job and child._handle == self.job.process
                and child._handle is not None, 'retained_original_workflow_child_required')
        self.child = child
        self.channel.peer_handle = child._handle  # borrowed, never closed by the channel
        self._close_child_copies()
        return self.channel

    def _close_child_copies(self):
        for name in ('child_read', 'child_write', 'parent_query'):
            slot = self.slots.get(name)
            if slot is not None and slot.value:
                require(self.api.CloseHandle(slot), 'workflow_inherited_copy_close_failed')
                slot.value = None

    def parent_proof(self, document, admitted, *, expected_child_image_sha256):
        """Parent orientation of stock proof, with independently admitted exact child pin.

        WindowsControlChannel.qualify_binding assumes transport=None means CHILD.
        A Manager endpoint must not fabricate a Root transport to flip orientation.
        Reuse its native witnesses and proof type, explicitly checking parent/child.
        """
        from core.manager_launch_owner import NativeChannelProof
        from suite_core.manager.bootstrap_entry import _binding_document, _wire_bytes
        cap = self._original()
        require(not self.closed and self.native.helper_reservation is not None
                and not getattr(self.native, 'start_failed', False)
                and self.child is not None and self.child in self.native.logs
                and self.child._handle == self.channel.peer_handle
                and self.child.job is self.job and self.job.process == self.child._handle,
                'original_workflow_parent_channel_required')
        _binding_document(document)
        require(type(expected_child_image_sha256) is str
                and re.fullmatch('[0-9a-f]{64}', expected_child_image_sha256)
                and expected_child_image_sha256 == cap.program_pin,
                'independent_workflow_child_pin_required')
        local, peer = self.channel.local_witness(), self.channel.peer_witness()
        import json
        require(document['launcher'] == local and document['entry'] == peer
                and document['owner_generation'] == cap.session.binding.owner_generation
                and admitted.layout.code_root == cap.session.admitted.layout.code_root
                and admitted.layout.data_root == cap.session.admitted.layout.data_root
                and admitted.settings_path == admitted.layout.data_root / 'Manager/generation-settings.json'
                and tuple(admitted.restart_arguments) == self.command[1:]
                and peer['executable_sha256'] == expected_child_image_sha256
                and document['channel_epoch'] == self.channel.epoch
                and document['deployment_root'] == str(admitted.layout.code_root)
                and document['data_root'] == str(admitted.layout.data_root)
                and document['settings_path'] == str(admitted.settings_path)
                and document['mode'] == 'portable' and admitted.layout.mode.value == 'portable'
                and document['argv_sha256'] == sha256(json.dumps(admitted.restart_arguments,
                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                'native_workflow_parent_offer_mismatch')
        return NativeChannelProof(sha256(_wire_bytes(document)).hexdigest(), self.channel.epoch,
                                  local['creation_filetime'], peer['creation_filetime'])

    def _close_endpoints(self):
        if self.channel is not None:
            self.channel.close()
        for name, descriptor in tuple(self.descriptors.items()):
            os.close(descriptor)
            del self.descriptors[name]
        for slot in self.slots.values():
            if slot.value:
                require(self.api.CloseHandle(slot), 'workflow_control_endpoint_close_failed')
                slot.value = None
        self.closed = True

    def close_uncreated(self):
        """Only proven no-entry cleanup. Does not settle helper/grant accounting."""
        require(not self.creation_entered and self.child is None
                and (self.job is None or self.job.process is None),
                'workflow_control_creation_uncertain')
        self._close_endpoints()

    def close_after_drain(self, child):
        """Close endpoints only after original process, Job and stdout drain."""
        require(child is self.child and child in self.native.logs and child.job is self.job
                and child.poll() is not None and self.job.active() == 0
                and self.native.drained.is_set()
                and (self.native.drain is None or not self.native.drain.is_alive()),
                'workflow_control_original_drain_required')
        self._close_endpoints()
