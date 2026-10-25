"""Attempt-local Windows pipe resources for the original Manager owner.

This module creates no processes and confers no hook/SDK/update authority.
Endpoint names and peer PIDs are discovery, never deployment trust. A caller
must authenticate the retained peer and transaction before exchanging authority.
"""
import ctypes
from ctypes import wintypes as W
import math
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from core.manager_launch_owner import LaunchHold, _normal_native_api


@dataclass(frozen=True)
class StockHookBootstrapAnchor:
    """Independent installed-file identity or explicit expected build pin.

    Installed measurement follows normal frozen entry's trusted-deployment rule.
    It establishes neither publisher authenticity nor resistance to adversarial
    replacement of the installation. A peer/env hash is never an anchor.
    """
    code_root: Path
    data_root: Path
    settings_path: Path
    bootstrap_sha256: str
    provenance: str = 'explicit_expected_pin'


def installed_hook_bootstrap_anchor(settings_relative, *, expected_bootstrap_sha256=None):
    """Derive deployment ONLY from this running frozen canonical hook image.

    Settings is a bounded locator inside that deployment's fixed portable Data;
    an authenticated original root must independently agree before transfer.
    Explicit pin mismatch never falls back to measuring current installed bytes.
    """
    import sys
    import hashlib
    from core.manager_stock_apply import physical, readback
    if getattr(sys, 'frozen', False) is not True:
        raise LaunchHold('running_frozen_stock_hook_required')
    exe = physical(sys.executable)
    if tuple(exe.parts[-4:]) != ('Apps', 'Manager', 'current', 'CollectiGenManager.exe'):
        raise LaunchHold('canonical_running_stock_hook_required')
    code = exe.parents[3]
    data = physical(str(code / 'Data'))
    if (type(settings_relative) is not str or not settings_relative
            or Path(settings_relative).is_absolute() or '..' in Path(settings_relative).parts):
        raise LaunchHold('fixed_portable_settings_locator_required')
    settings = physical(str(data / settings_relative))
    if not settings.is_relative_to(data) or not settings.is_file():
        raise LaunchHold('fixed_portable_settings_locator_required')
    bootstrap = physical(str(code / 'CollectiGen.exe'))
    if expected_bootstrap_sha256 is not None:
        if (type(expected_bootstrap_sha256) is not str
                or not re.fullmatch('[0-9a-f]{64}', expected_bootstrap_sha256)):
            raise LaunchHold('explicit_bootstrap_pin_required')
        digest, provenance = expected_bootstrap_sha256, 'explicit_expected_pin'
        readback(bootstrap, expected=digest)  # hold on mismatch, no fallback
    else:
        before = bootstrap.stat()
        if not bootstrap.is_file() or before.st_size > 256 * 1024 * 1024:
            raise LaunchHold('installed_bootstrap_measurement_bound')
        hasher = hashlib.sha256()
        with bootstrap.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        after = bootstrap.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise LaunchHold('installed_bootstrap_measurement_changed')
        digest, provenance = hasher.hexdigest(), 'trusted_installed_deployment_measurement'
    anchor = StockHookBootstrapAnchor(code, data, settings, digest, provenance)
    _check_anchor(anchor)
    return anchor


@dataclass(frozen=True)
class StockHookPeerProof:
    server_pid: int
    creation_filetime: int
    bootstrap_sha256: str
    source: str = 'stock_hook_kernel_pipe_query'


def _check_anchor(anchor):
    from core.manager_stock_apply import physical, readback
    if (type(anchor) is not StockHookBootstrapAnchor or type(anchor.bootstrap_sha256) is not str
            or not re.fullmatch('[0-9a-f]{64}', anchor.bootstrap_sha256)
            or anchor.provenance not in {'explicit_expected_pin', 'trusted_installed_deployment_measurement'}):
        raise LaunchHold('independent_stock_hook_bootstrap_anchor_required')
    code = physical(str(anchor.code_root))
    data = physical(str(anchor.data_root))
    settings = physical(str(anchor.settings_path))
    if (data != code / 'Data' or not settings.is_relative_to(data) or not settings.is_file()):
        raise LaunchHold('independent_stock_hook_deployment_required')
    readback(code / 'CollectiGen.exe', expected=anchor.bootstrap_sha256)
    return code / 'CollectiGen.exe'


class _Overlapped(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
               ('offset', W.DWORD), ('offset_high', W.DWORD), ('event', W.HANDLE)]


class _Security(ctypes.Structure):
    _fields_ = [('length', W.DWORD), ('descriptor', W.LPVOID), ('inherit', W.BOOL)]


def _api(api=None):
    api = _normal_native_api() if api is None else api
    signatures = {
        'CreateNamedPipeW': ([W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
                             W.DWORD, W.DWORD, ctypes.POINTER(_Security)], W.HANDLE),
        'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD,
                        W.HANDLE], W.HANDLE),
        'CreateEventW': ([W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
        'ConnectNamedPipe': ([W.HANDLE, ctypes.POINTER(_Overlapped)], W.BOOL),
        'ReadFile': ([W.HANDLE, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD),
                      ctypes.POINTER(_Overlapped)], W.BOOL),
        'WriteFile': ([W.HANDLE, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD),
                       ctypes.POINTER(_Overlapped)], W.BOOL),
        'CancelIoEx': ([W.HANDLE, ctypes.POINTER(_Overlapped)], W.BOOL),
        'GetOverlappedResult': ([W.HANDLE, ctypes.POINTER(_Overlapped),
                                 ctypes.POINTER(W.DWORD), W.BOOL], W.BOOL),
        'LocalFree': ([W.HLOCAL], W.HLOCAL),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes, getattr(api, name).restype = args, result
    return api


def _logon_security(api, resources):
    """DACL grants SYSTEM and the current logon; clients cannot add instances."""
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
    adv.OpenProcessToken.restype = W.BOOL
    adv.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD,
                                        ctypes.POINTER(W.DWORD)]
    adv.GetTokenInformation.restype = W.BOOL
    adv.ConvertSidToStringSidW.argtypes = [W.LPVOID, ctypes.POINTER(W.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = W.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        W.LPCWSTR, W.DWORD, ctypes.POINTER(W.LPVOID), W.LPVOID]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = W.BOOL
    class SidAndAttributes(ctypes.Structure):
        _fields_ = [('sid', W.LPVOID), ('attributes', W.DWORD)]
    class TokenGroups(ctypes.Structure):
        _fields_ = [('count', W.DWORD), ('groups', SidAndAttributes * 1)]
    token, text, descriptor = W.HANDLE(), W.LPWSTR(), W.LPVOID()
    resources.update(token=token, text=text, descriptor=descriptor)
    if not adv.OpenProcessToken(api.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise LaunchHold('stock_pipe_logon_token_unavailable')
    needed = W.DWORD()
    adv.GetTokenInformation(token, 2, None, 0, ctypes.byref(needed))
    if not ctypes.sizeof(TokenGroups) <= needed.value <= 65536:
        raise LaunchHold('stock_pipe_token_bound')
    buffer = ctypes.create_string_buffer(needed.value)
    resources['buffer'] = buffer
    if not adv.GetTokenInformation(token, 2, buffer, len(buffer), ctypes.byref(needed)):
        raise LaunchHold('stock_pipe_logon_query_failed')
    groups = TokenGroups.from_buffer(buffer)
    offset = TokenGroups.groups.offset
    if not 1 <= groups.count <= (len(buffer) - offset) // ctypes.sizeof(SidAndAttributes):
        raise LaunchHold('stock_pipe_logon_groups_bound')
    entries = (SidAndAttributes * groups.count).from_buffer(buffer, offset)
    logons = [entry for entry in entries if entry.attributes & 0xc0000000 == 0xc0000000]
    if len(logons) != 1 or not adv.ConvertSidToStringSidW(logons[0].sid, ctypes.byref(text)):
        raise LaunchHold('stock_pipe_exact_logon_required')
    if not re.fullmatch(r'S-1-5-5-\d+-\d+', text.value):
        raise LaunchHold('stock_pipe_logon_sid_required')
    # Specific client rights omit FILE_CREATE_PIPE_INSTANCE (bit 4).
    sddl = f'D:P(A;;GA;;;SY)(A;;0x0012019b;;;{text.value})'
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise LaunchHold('stock_pipe_restricted_dacl_failed')
    return descriptor

def _deadline(timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise LaunchHold('bounded_stock_pipe_wait_required')
    return time.monotonic() + timeout


class StockHookPipe:
    """Retain native outputs/buffers before fallible calls, cancel before close.

    The owner retains this object BEFORE create_server/open_client. A failed or
    cancelled partial frame is never replayed. Unsettled overlapped operations
    keep their buffer, event and endpoint reachable until explicit settlement.
    """
    def __init__(self, name, *, api=None):
        if type(name) is not str or not re.fullmatch(
                r'\\\\\.\\pipe\\CollectiGen\.StockHook\.[0-9a-f]{32}', name):
            raise LaunchHold('exact_local_stock_pipe_name_required')
        self.name, self.api = name, _api() if api is None else api
        self.handle = None
        self.descriptor = None
        self.security_resources = {}
        self.operation = None
        self.state = 'new'
        self.fault = None
        self.server = False
        self.peer_query = None
        self.peer_proof = None
        self.peer_admission_started = False
        self.anchor = None

    @classmethod
    def fresh(cls, *, api=None):
        return cls('\\\\.\\pipe\\CollectiGen.StockHook.' + uuid.uuid4().hex, api=api)

    def create_server(self):
        if self.state != 'new':
            raise LaunchHold('stock_pipe_creation_no_replay')
        self.state = 'creation_uncertain'
        self.descriptor = _logon_security(self.api, self.security_resources)
        security = _Security(ctypes.sizeof(_Security), self.descriptor, False)
        self.handle = self.api.CreateNamedPipeW(self.name,
            3 | 0x00080000 | 0x40000000, 8, 1, 65540, 65540, 5000, ctypes.byref(security))
        if self.handle in (None, 0, ctypes.c_void_p(-1).value):
            self.handle = None
            raise LaunchHold('stock_pipe_first_instance_creation_failed')
        self.server, self.state = True, 'listening'
        self._free_descriptor()
        return self

    def open_client(self, *, anchor=None):
        if self.state != 'new':
            raise LaunchHold('stock_pipe_creation_no_replay')
        if anchor is not None:
            _check_anchor(anchor)  # independently pinned fixed file BEFORE contacting endpoint
            self.anchor = anchor
        self.state = 'creation_uncertain'
        # Identification SQOS: an untrusted server cannot impersonate the client.
        self.handle = self.api.CreateFileW(self.name, 0x0012019b, 0, None, 3,
                                          0x40000000 | 0x00100000 | 0x00010000, None)
        if self.handle in (None, 0, ctypes.c_void_p(-1).value):
            self.handle = None
            raise LaunchHold('stock_pipe_client_open_failed')
        self.state = 'connected'
        return self

    def _free_descriptor(self):
        resources = self.security_resources
        for name in ('text', 'descriptor', 'token'):
            value = resources.get(name)
            if value:
                if name == 'token':
                    failed = not self.api.CloseHandle(value)
                else:
                    failed = bool(self.api.LocalFree(ctypes.cast(value, W.HLOCAL)))
                if failed:
                    raise LaunchHold('stock_pipe_' + name + '_resource_held')
                # Positive cleanup is one-shot, including partial cleanup retries.
                resources[name] = None
        self.descriptor = None
        resources.clear()

    def _finish(self):
        operation = self.operation
        if operation is None:
            return
        if not operation['settled']:
            raise LaunchHold('stock_pipe_operation_unsettled')
        if not self.api.CloseHandle(operation['overlapped'].event):
            self.fault = 'event_retirement'
            raise LaunchHold('stock_pipe_event_resource_held')
        self.operation = None

    def settle_cancelled(self, *, timeout=1):
        deadline = _deadline(timeout)
        operation = self.operation
        if operation is None:
            return
        if not operation['settled']:
            # Cancellation is not settlement; always observe completion afterward.
            self.api.CancelIoEx(self.handle, ctypes.byref(operation['overlapped']))
            wait = max(0, int((deadline - time.monotonic()) * 1000))
            if self.api.WaitForSingleObject(operation['overlapped'].event, wait) != 0:
                raise LaunchHold('stock_pipe_cancel_completion_held')
            count = W.DWORD()
            ok = self.api.GetOverlappedResult(self.handle, ctypes.byref(operation['overlapped']),
                                             ctypes.byref(count), False)
            if not ok and ctypes.get_last_error() != 995:
                raise LaunchHold('stock_pipe_cancel_result_held')
            operation['settled'] = True
        self._finish()

    def _io(self, kind, buffer, size, deadline, cancelled):
        if self.operation is not None or self.fault is not None:
            raise LaunchHold('stock_pipe_recovery_required')
        if cancelled() or time.monotonic() >= deadline:
            self.fault = 'cancelled_or_timeout'
            raise LaunchHold('stock_pipe_cancelled_or_timeout')
        event = self.api.CreateEventW(None, True, False, None)
        if not event:
            raise LaunchHold('stock_pipe_event_creation_failed')
        overlapped = _Overlapped(event=event)
        operation = dict(overlapped=overlapped, buffer=buffer, settled=False, kind=kind)
        self.operation = operation  # before any native I/O can reference its memory
        count = W.DWORD()
        try:
            if kind == 'connect':
                ok = self.api.ConnectNamedPipe(self.handle, ctypes.byref(overlapped))
            else:
                ok = getattr(self.api, kind)(self.handle, buffer, size, ctypes.byref(count),
                                             ctypes.byref(overlapped))
            error = 0 if ok else ctypes.get_last_error()
            if kind == 'connect' and not ok and error == 535:
                operation['settled'] = True  # client connected before ConnectNamedPipe
                return 0
            if not ok and error != 997:
                operation['settled'] = True  # documented synchronous failure, no pending I/O
                raise LaunchHold('stock_pipe_io_failed')
            if not ok:
                while True:
                    if cancelled() or time.monotonic() >= deadline:
                        raise LaunchHold('stock_pipe_cancelled_or_timeout')
                    wait = min(25, max(0, int((deadline - time.monotonic()) * 1000)))
                    state = self.api.WaitForSingleObject(event, wait)
                    if state == 0:
                        break
                    if state != 258:
                        raise LaunchHold('stock_pipe_wait_failed')
            ok = self.api.GetOverlappedResult(self.handle, ctypes.byref(overlapped),
                                             ctypes.byref(count), False)
            if not ok:
                if ctypes.get_last_error() != 996:
                    operation['settled'] = True
                raise LaunchHold('stock_pipe_completion_failed')
            operation['settled'] = True
            if count.value > size:
                raise LaunchHold('stock_pipe_completion_count_invalid')
            return count.value
        except BaseException as error:
            self.fault = type(error).__name__
            if not operation['settled']:
                try:
                    self.settle_cancelled()
                except BaseException:
                    pass  # reachable operation/buffer/event must survive uncertainty
            raise
        finally:
            if self.operation is not None and self.operation['settled']:
                self._finish()

    def connect(self, *, timeout=5, cancelled=lambda: False):
        if self.state != 'listening':
            raise LaunchHold('stock_pipe_listening_required')
        self._io('connect', None, 0, _deadline(timeout), cancelled)
        self.state = 'connected'
        return self

    def peer_pid(self):
        if self.state != 'connected':
            raise LaunchHold('stock_pipe_connection_required')
        value = W.ULONG()
        name = 'GetNamedPipeClientProcessId' if self.server else 'GetNamedPipeServerProcessId'
        if not getattr(self.api, name)(self.handle, ctypes.byref(value)) or not value.value:
            raise LaunchHold('stock_pipe_kernel_peer_unavailable')
        return value.value

    def authenticate_root(self, anchor):
        """Hook only: endpoint PID -> retained query, exact independent pin/path.

        This proof is intentionally incompatible with NativeChannelProof. It
        grants no inherited-handle authority, update/Job control or SDK callback.
        The later registration ACK must also bind transaction/attempt/stage.
        """
        from core.manager_launch_owner import _native_witness
        if (self.server or self.state != 'connected' or self.peer_admission_started
                or anchor is not self.anchor or self.anchor is None):
            raise LaunchHold('single_hook_only_peer_admission_required')
        expected = _check_anchor(anchor)  # trust independently checked before any peer query
        self.peer_admission_started = True
        pid = self.peer_pid()
        self.peer_query = self.api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not self.peer_query:
            raise LaunchHold('stock_hook_root_query_unavailable')
        witness = _native_witness(self.api, self.peer_query)
        size, image = W.DWORD(32768), ctypes.create_unicode_buffer(32768)
        if not self.api.QueryFullProcessImageNameW(self.peer_query, 0, image, ctypes.byref(size)):
            raise LaunchHold('stock_hook_root_path_query_failed')
        if (Path(image.value) != expected or witness['pid'] != pid
                or witness['executable_sha256'] != anchor.bootstrap_sha256
                or witness['creation_filetime'] <= 0
                or self.api.WaitForSingleObject(self.peer_query, 0) != 258
                or self.peer_pid() != pid):
            raise LaunchHold('stock_hook_root_anchor_or_lifetime_changed')
        _check_anchor(anchor)
        self.peer_proof = StockHookPeerProof(pid, witness['creation_filetime'],
                                             witness['executable_sha256'])
        return self.peer_proof

    def verify_root(self, anchor, proof):
        from core.manager_launch_owner import _native_witness
        if (type(proof) is not StockHookPeerProof or proof is not self.peer_proof or not self.peer_query
                or anchor is not self.anchor):
            raise LaunchHold('original_hook_peer_proof_required')
        _check_anchor(anchor)
        witness = _native_witness(self.api, self.peer_query)
        if (witness != dict(pid=proof.server_pid, creation_filetime=proof.creation_filetime,
                            executable_sha256=proof.bootstrap_sha256)
                or self.peer_pid() != proof.server_pid
                or self.api.WaitForSingleObject(self.peer_query, 0) != 258):
            raise LaunchHold('stock_hook_root_peer_changed')
        return proof

    def write_frame(self, raw, *, timeout=5, cancelled=lambda: False):
        from core.manager_launch_owner import WindowsControlChannel
        WindowsControlChannel._bounded(raw)
        if self.state != 'connected':
            raise LaunchHold('stock_pipe_connection_required')
        deadline, offset = _deadline(timeout), 0
        while offset < len(raw):
            buffer = ctypes.create_string_buffer(raw[offset:])
            count = self._io('WriteFile', buffer, len(raw) - offset, deadline, cancelled)
            if not count:
                self.fault = 'zero_write'
                raise LaunchHold('stock_pipe_zero_write')
            offset += count

    def read_frame(self, *, timeout=5, cancelled=lambda: False):
        if self.state != 'connected':
            raise LaunchHold('stock_pipe_connection_required')
        deadline = _deadline(timeout)
        def exact(size):
            result = bytearray()
            while len(result) < size:
                buffer = ctypes.create_string_buffer(size - len(result))
                count = self._io('ReadFile', buffer, len(buffer), deadline, cancelled)
                if not count:
                    self.fault = 'eof'
                    raise LaunchHold('stock_pipe_eof')
                result.extend(buffer.raw[:count])
            return bytes(result)
        prefix = exact(4)
        size = int.from_bytes(prefix, 'little')
        if not 1 <= size <= 65536:
            self.fault = 'frame_bound'
            raise LaunchHold('stock_pipe_frame_bound')
        return prefix + exact(size)

    def close(self):
        if self.operation is not None:
            raise LaunchHold('stock_pipe_operation_unsettled')
        self._free_descriptor()
        if self.peer_query:
            if not self.api.CloseHandle(self.peer_query):
                raise LaunchHold('stock_hook_root_query_resource_held')
            self.peer_query = None
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise LaunchHold('stock_pipe_endpoint_resource_held')
            self.handle = None
        self.state = 'closed'
