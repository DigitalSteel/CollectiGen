"""Original-owner atomic Job member creation with optional fresh normal channel.

No new owner, lease, updater CLI, successor selection or termination policy.
Caller retains this object before create and registers the suspended original
before resume. Every uncertain native output remains reachable; no replay.
"""
import ctypes
from ctypes import wintypes as W
import os
import subprocess
import uuid

from core.manager_launch_owner import LaunchHold, RetainedNativeProcess, WindowsControlChannel, WindowsNormalTransport
from core.training_process_windows import _Security, _StartupEx, _ProcessInfo


class WindowsMaintenanceMember:
    def __init__(self, transport, role, *, normal_channel=False):
        if type(transport) is not WindowsNormalTransport or transport.owner.native_transport is not transport:
            raise LaunchHold('same_original_native_transport_required')
        if role not in {'updater', 'replacement', 'repair_updater', 'restored', 'fresh_restored'}:
            raise LaunchHold('bounded_original_maintenance_role_required')
        if type(normal_channel) is not bool or normal_channel != (role in {'replacement', 'restored', 'fresh_restored'}):
            raise LaunchHold('exact_original_member_channel_role_required')
        self.transport, self.owner, self.api, self.role = transport, transport.owner, transport.api, role
        self.normal_channel = normal_channel
        self.process = RetainedNativeProcess(self.api, None)
        self.info = _ProcessInfo()
        self.raw, self.slots, self.descriptors = {}, {}, {}
        self.attributes = None
        self.attributes_initialized = False
        self.null_stream = None
        self.channel = None
        self.job_source_handle = None
        self.state = 'new'

    def create(self, argv, *, environment, cwd, shell=False, allow_elevation=False):
        import msvcrt
        if (self.state != 'new' or type(argv) is not tuple or not argv or shell is not False
                or allow_elevation is not False or type(environment) is not dict
                or not all(type(k) is str and type(v) is str and '\0' not in k + v and '=' not in k
                           for k, v in environment.items())):
            raise LaunchHold('single_exact_native_member_creation_required')
        self.owner._owned()
        if self.role in self.owner.children or self.owner.pending or not self.transport.job:
            raise LaunchHold('original_native_member_scope_required')
        self.state = 'preparing'
        self.owner.pending.append(dict(role=self.role, process=self.process))
        inherited = []
        env = dict(environment)
        # No inherited normal/root witnesses from the exited initial Manager.
        stale = {'COLLECTIGEN_CONTROL_READ', 'COLLECTIGEN_CONTROL_WRITE',
                 'COLLECTIGEN_LAUNCHER_QUERY', 'COLLECTIGEN_CONTROL_EPOCH'}
        if self.normal_channel:
            stale.add('COLLECTIGEN_STOCK_HOOK_CONTEXT')
        for key in tuple(env):
            if key.upper() in stale:
                del env[key]
        try:
            self.null_stream = open(os.devnull, 'r+b', buffering=0)
            null_handle = msvcrt.get_osfhandle(self.null_stream.fileno())
            os.set_handle_inheritable(null_handle, True)
            inherited.append(null_handle)
            current = self.api.GetCurrentProcess()
            query = W.HANDLE()
            self.slots['query'] = query
            if self.normal_channel:
                source, access = current, 0x1000 | 0x100000
            else:
                source, access = self.transport.job, 4  # Job query only, copied into original updater
            ok = self.api.DuplicateHandle(current, source, current, ctypes.byref(query), access, True, 0)
            if query.value:
                self.raw['query'] = query.value
            if not ok or not query.value:
                raise LaunchHold('native_member_query_transfer_uncertain')
            inherited.append(query.value)
            if self.normal_channel:
                security = _Security(ctypes.sizeof(_Security), None, True)
                for prefix in ('to_child', 'from_child'):
                    reader, writer = W.HANDLE(), W.HANDLE()
                    self.slots[prefix + '_read'], self.slots[prefix + '_write'] = reader, writer
                    ok = self.api.CreatePipe(ctypes.byref(reader), ctypes.byref(writer), ctypes.byref(security), 65540)
                    for suffix, slot in (('_read', reader), ('_write', writer)):
                        if slot.value:
                            self.raw[prefix + suffix] = slot.value
                    if not ok:
                        raise LaunchHold('native_member_control_pipe_uncertain')
                for name, mode, flags in (('from_child_read', 'reader', os.O_RDONLY),
                                           ('to_child_write', 'writer', os.O_WRONLY)):
                    handle = self.raw[name]
                    if not self.api.SetHandleInformation(handle, 1, 0):
                        raise LaunchHold('native_member_parent_inheritance_hold')
                    fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY | os.O_NOINHERIT)
                    self.descriptors[mode] = fd
                    del self.raw[name]
                    self.slots[name].value = None
                inherited.extend((self.raw['to_child_read'], self.raw['from_child_write']))
                epoch = uuid.uuid4().hex
                self.channel = WindowsControlChannel(self.api, self.descriptors['reader'], self.descriptors['writer'],
                    None, transport=self.transport, epoch=epoch)
                env.update(COLLECTIGEN_CONTROL_READ=str(self.raw['to_child_read']),
                    COLLECTIGEN_CONTROL_WRITE=str(self.raw['from_child_write']),
                    COLLECTIGEN_LAUNCHER_QUERY=str(query.value), COLLECTIGEN_CONTROL_EPOCH=epoch)
            else:
                self.job_source_handle = query.value
            if msvcrt.get_osfhandle(self.owner.fd) in inherited:
                raise LaunchHold('lease_inheritance_forbidden')
            handles = (W.HANDLE * len(inherited))(*inherited)
            jobs = (W.HANDLE * 1)(self.transport.job)
            size = ctypes.c_size_t()
            self.api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise LaunchHold('native_member_attribute_bound')
            self.attributes = ctypes.create_string_buffer(size.value)
            if not self.api.InitializeProcThreadAttributeList(self.attributes, 2, 0, ctypes.byref(size)):
                raise LaunchHold('native_member_attributes_unavailable')
            self.attributes_initialized = True
            for key, value in ((0x20002, handles), (0x2000d, jobs)):
                if not self.api.UpdateProcThreadAttribute(self.attributes, 0, key, value, ctypes.sizeof(value), None, None):
                    raise LaunchHold('atomic_original_job_member_unavailable')
            startup = _StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100 | 1
            startup.startup.stdin = startup.startup.stdout = startup.startup.stderr = null_handle
            startup.attributes = ctypes.cast(self.attributes, W.LPVOID)
            block = ctypes.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(env.items())) + '\0\0')
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            self.state = 'creation_uncertain'  # outputs/pending retained before entering kernel
            try:
                ok = self.api.CreateProcessW(argv[0], command, None, None, True,
                    0x4 | 0x8000000 | 0x80000 | 0x400, block, cwd,
                    ctypes.byref(startup), ctypes.byref(self.info))
            finally:
                self.process._handle, self.process.thread, self.process._pid = self.info.process, self.info.thread, self.info.pid
                if self.channel is not None:
                    self.channel.peer_handle = self.info.process
            if not ok or not all((self.info.process, self.info.thread, self.info.pid)):
                raise LaunchHold('native_member_creation_uncertain')
            self.state = 'suspended'
            self.settle_parent_copies()
            return self.process
        finally:
            if self.attributes_initialized:
                self.api.DeleteProcThreadAttributeList(self.attributes)
                self.attributes_initialized = False

    def settle_parent_copies(self):
        for name, value in tuple(self.raw.items()):
            if not self.api.CloseHandle(value):
                raise LaunchHold('native_member_parent_resource_held')
            del self.raw[name]
            self.slots[name].value = None
        if self.null_stream is not None:
            self.null_stream.close()
            self.null_stream = None

    def resume_registered(self):
        if self.state != 'suspended' or self.owner.children.get(self.role) is not self.process:
            raise LaunchHold('registered_original_member_required')
        if not self.transport.includes(self.process):
            raise LaunchHold('original_member_job_changed')
        self.state = 'resume_uncertain'
        self.process.resume()
        self.state = 'running'

    def close_after_exit(self):
        if self.state == 'closed':
            return True
        if self.process.poll() is None:
            raise LaunchHold('live_original_member_resources_retained')
        self.settle_parent_copies()
        if self.channel is not None:
            self.channel.close()
            self.descriptors.clear()
        self.process.close_after_exit()
        self.state = 'closed'
        return True
