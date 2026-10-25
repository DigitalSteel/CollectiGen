"""Windows 10+ process-tree ownership for the internal trainer process boundary.

Job membership is installed atomically by CreateProcess, before the suspended
child can execute. Job handles are never inherited. This is lifecycle containment,
not a security sandbox or proof of offline behavior.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import sys
import threading
from contextlib import contextmanager


_EXTERNAL_PROCESS_LAUNCH_LOCK = threading.RLock()


@contextmanager
def _external_dll_directory(api):
    """Exclude the frozen host's bundled DLL directory from an external child.

    SetDllDirectory is process-wide. Serialize reset/create/restore for every
    external launch through this boundary; retain original handles before restore.
    """
    if getattr(sys,'frozen',False) is not True:
        yield
        return
    with _EXTERNAL_PROCESS_LAUNCH_LOCK:
        previous=C.create_unicode_buffer(32768)
        C.set_last_error(0)
        size=api.GetDllDirectoryW(len(previous),previous)
        if size>=len(previous) or (size==0 and C.get_last_error()):
            raise OSError('The frozen host DLL directory could not be saved.')
        try:
            _check(api.SetDllDirectoryW(None))
            yield
        finally:
            _check(api.SetDllDirectoryW(previous.value or None))


class _Security(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", W.LPVOID), ("inherit", W.BOOL)]


class _Startup(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR),
        ("x", W.DWORD), ("y", W.DWORD), ("xsize", W.DWORD), ("ysize", W.DWORD),
        ("xchars", W.DWORD), ("ychars", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
        ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_bytes", W.LPVOID),
        ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class _StartupEx(C.Structure):
    _fields_ = [("startup", _Startup), ("attributes", W.LPVOID)]


class _ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


class _Limits(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong), ("flags", W.DWORD),
        ("min_working", C.c_size_t), ("max_working", C.c_size_t), ("active_limit", W.DWORD),
        ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class _ExtendedLimits(C.Structure):
    _fields_ = [("basic", _Limits), ("io", C.c_ulonglong * 6), ("process_memory", C.c_size_t),
        ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class _Accounting(C.Structure):
    _fields_ = [("user", C.c_longlong), ("kernel", C.c_longlong), ("period_user", C.c_longlong),
        ("period_kernel", C.c_longlong), ("faults", W.DWORD), ("total", W.DWORD),
        ("active", W.DWORD), ("terminated", W.DWORD)]


class NativeObservationError(OSError):
    """Fixed controller-authored diagnostic; never carries module paths."""


def _api():
    if os.name != "nt":
        raise RuntimeError("Contained trainer processes require Windows 10 or newer.")
    api = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([W.LPVOID, W.LPCWSTR], W.HANDLE),
        "OpenJobObjectW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, W.LPVOID, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, W.LPVOID, W.DWORD, W.LPVOID], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "CreatePipe": ([C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), W.LPVOID, W.DWORD], W.BOOL),
        "SetHandleInformation": ([W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
        "InitializeProcThreadAttributeList": ([W.LPVOID, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([W.LPVOID, W.DWORD, C.c_size_t, W.LPVOID, C.c_size_t, W.LPVOID, W.LPVOID], W.BOOL),
        "DeleteProcThreadAttributeList": ([W.LPVOID], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, W.LPVOID, W.LPVOID, W.BOOL, W.DWORD,
            W.LPVOID, W.LPCWSTR, W.LPVOID, C.POINTER(_ProcessInfo)], W.BOOL),
        "ResumeThread": ([W.HANDLE], W.DWORD),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "GetWindowsDirectoryW": ([W.LPWSTR, W.UINT], W.UINT),
        "GetDllDirectoryW": ([W.DWORD, W.LPWSTR], W.DWORD),
        "SetDllDirectoryW": ([W.LPCWSTR], W.BOOL),
        "GetDriveTypeW": ([W.LPCWSTR], W.UINT),
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "IsProcessInJob": ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
        "K32EnumProcessModulesEx": ([W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.DWORD], W.BOOL),
        "K32GetModuleFileNameExW": ([W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD], W.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _check(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


def windows_directory() -> str:
    buffer = C.create_unicode_buffer(32768)
    length = _api().GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("The Windows system directory is unavailable.")
    return buffer.value


def is_local_volume(anchor: str) -> bool:
    return _api().GetDriveTypeW(anchor) in {2, 3, 5, 6}


class WindowsTrainingJob:
    def __init__(self, name: str, *, interactive: bool = False, kill_on_close: bool = True):
        if type(kill_on_close) is not bool:
            raise ValueError('explicit_job_close_policy_required')
        self.interactive = interactive is True
        self.kill_on_close = kill_on_close
        self.api = _api()
        self.handle = _check(self.api.CreateJobObjectW(None, name))
        self._owns_handle = True
        self.process = self.thread = None
        self.stdout = None
        self.pid = None
        self.creation_entered = False
        self.creation_returned = None
        self.creation_command = None
        self.creation_info = None
        self.resume_entered = False
        try:
            if C.get_last_error() == 183:
                raise RuntimeError("The selected process owner already exists.")
            limits = _ExtendedLimits()
            # Training keeps its existing default. A standalone Root supervisor
            # must preserve that Root's own custody if its outer observer exits.
            limits.basic.flags = 0x2000 if kill_on_close else 0  # no breakaway
            _check(self.api.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def active_named(name: str) -> int:
        api = _api()
        handle = api.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
        if not handle:
            error = C.get_last_error()
            if error == 2:
                return 0
            raise C.WinError(error)
        try:
            return WindowsTrainingJob._active(api, handle)
        finally:
            api.CloseHandle(handle)

    @staticmethod
    def _active(api, handle):
        value = _Accounting()
        _check(api.QueryInformationJobObject(handle, 1, C.byref(value), C.sizeof(value), None))
        return value.active

    def active(self) -> int:
        return self._active(self.api, self.handle)

    def process_ids(self):
        """Bounded live Job membership; never discover unrelated processes."""
        class Members(C.Structure):
            _fields_ = [("assigned", W.DWORD), ("count", W.DWORD), ("ids", C.c_size_t * 8)]
        members = Members()
        _check(self.api.QueryInformationJobObject(self.handle, 3, C.byref(members), C.sizeof(members), None))
        if not 0 < members.count <= 8 or members.assigned != members.count:
            raise NativeObservationError("The owned process membership is unavailable or excessive.")
        return frozenset(members.ids[:members.count])

    def module_paths(self, pid, should_cancel=None):
        """Two matching snapshots of one live owned process; paths stay private.

        Does not open module files or enumerate the machine. This loader list
        omits datafile/manual mappings and cannot prove loaded memory bytes.
        """
        if C.sizeof(C.c_void_p) != 8 or pid not in self.process_ids():
            raise NativeObservationError("Native observation requires a live owned process and 64-bit controller.")
        process = _check(self.api.OpenProcess(0x100410, False, pid))  # query, VM read, synchronize
        try:
            member = W.BOOL()
            _check(self.api.IsProcessInJob(process, self.handle, C.byref(member)))
            if not member.value or self.api.WaitForSingleObject(process, 0) != 258:
                raise NativeObservationError("The observed process is no longer a live member.")
            def snapshot():
                modules = (W.HMODULE * 4096)()
                needed = W.DWORD()
                _check(self.api.K32EnumProcessModulesEx(process, modules, C.sizeof(modules), C.byref(needed), 3))
                if not 0 < needed.value <= C.sizeof(modules) or needed.value % C.sizeof(W.HMODULE):
                    raise NativeObservationError("The native module list exceeds its bound.")
                rows, length = [], 0
                buffer = C.create_unicode_buffer(32768)
                for module in modules[:needed.value // C.sizeof(W.HMODULE)]:
                    if should_cancel and should_cancel():
                        raise InterruptedError("Native observation was cancelled.")
                    size = self.api.K32GetModuleFileNameExW(process, module, buffer, len(buffer))
                    length += size
                    if not 0 < size < len(buffer) - 1 or length > 1024 * 1024:
                        raise NativeObservationError("A native module path is unavailable or exceeds its bound.")
                    rows.append((module, buffer.value))
                if len({row[0] for row in rows}) != len(rows):
                    raise NativeObservationError("The native module list repeats a handle.")
                return tuple(rows)
            first, second = snapshot(), snapshot()
            if first != second or self.api.WaitForSingleObject(process, 0) != 258 or pid not in self.process_ids():
                raise NativeObservationError("The native module list changed during observation.")
            main = C.create_unicode_buffer(32768)
            size = self.api.K32GetModuleFileNameExW(process, None, main, len(main))
            paths = tuple(path for _, path in first)
            matches = [path for path in paths if os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(main.value))]
            if not 0 < size < len(main) - 1 or len(matches) != 1:
                raise NativeObservationError("The native executable path is unavailable or inconsistent.")
            return (matches[0], *(path for path in paths if path != matches[0]))
        finally:
            self.api.CloseHandle(process)

    def member(self):
        """Borrow this Job for one synchronous helper; never own its lifetime.

        The caller retains this owner until the member is closed. Atomic job
        assignment and kill-on-owner-close cover both helper and trainer.
        """
        child = object.__new__(WindowsTrainingJob)
        child.api, child.handle = self.api, self.handle
        child.process = child.thread = child.stdout = child.pid = None
        child._owns_handle = False
        return child

    def creation_attributes(self):
        """Root-only optional restrictions; verifier members retain their owner."""
        return ()

    def spawn_suspended(self, command: tuple[str, ...], cwd: str, environment: dict[str, str]):
        import msvcrt
        if self.process is not None:
            raise RuntimeError("This process owner already has a root process.")
        self.creation_command = tuple(command)
        read, write = W.HANDLE(), W.HANDLE()
        security = _Security(C.sizeof(_Security), None, True)
        _check(self.api.CreatePipe(C.byref(read), C.byref(write), C.byref(security), 0))
        attributes = None
        initialized = False
        try:
            _check(self.api.SetHandleInformation(read, 1, 0))
            with open(os.devnull, "rb", buffering=0) as null:
                null_handle = msvcrt.get_osfhandle(null.fileno())
                os.set_handle_inheritable(null_handle, True)
                inherited = (W.HANDLE * 2)(null_handle, write.value)
                jobs = (W.HANDLE * 1)(self.handle)
                extra = tuple(self.creation_attributes())
                size = C.c_size_t()
                self.api.InitializeProcThreadAttributeList(None, 2 + len(extra), 0, C.byref(size))
                attributes = C.create_string_buffer(size.value)
                _check(self.api.InitializeProcThreadAttributeList(attributes, 2 + len(extra), 0, C.byref(size)))
                initialized = True
                _check(self.api.UpdateProcThreadAttribute(attributes, 0, 0x20002, inherited, C.sizeof(inherited), None, None))
                # Atomic job membership removes the create/assign owner-crash gap.
                _check(self.api.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, C.sizeof(jobs), None, None))
                for key, value in extra:
                    _check(self.api.UpdateProcThreadAttribute(attributes, 0, key, C.byref(value), C.sizeof(value), None, None))
                startup = _StartupEx()
                startup.startup.cb = C.sizeof(startup)
                # Desktop readers must receive their ordinary first visible window.
                # Existing trainer/runner callers retain hidden background startup.
                startup.startup.flags = 0x100 if getattr(self, "interactive", False) else 0x100 | 1
                startup.startup.stdin = null_handle
                startup.startup.stdout = startup.startup.stderr = write.value
                startup.attributes = C.cast(attributes, W.LPVOID)
                info = _ProcessInfo()
                self.creation_info = info  # Retain the original native outputs even if a call raises.
                env = C.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0\0")
                args = C.create_unicode_buffer(subprocess.list2cmdline(command))
                with _external_dll_directory(self.api):
                    self.creation_entered = True
                    created = self.api.CreateProcessW(command[0], args, None, None, True,
                        0x4 | 0x8000000 | 0x80000 | 0x400, env, cwd, C.byref(startup), C.byref(info))
                    self.creation_returned = bool(created)
                    # Preserve any outputs before validation or DLL restoration.
                    if created or info.process or info.thread or info.pid:
                        self.process, self.thread, self.pid = info.process, info.thread, info.pid
                    _check(created)
                    # Restoration can fail after successful creation. Retain the
                    # suspended original before leaving the guarded boundary.
                    self.process, self.thread, self.pid = info.process, info.thread, info.pid
            descriptor = msvcrt.open_osfhandle(read.value, os.O_RDONLY | os.O_BINARY)
            read = W.HANDLE()
            self.stdout = os.fdopen(descriptor, "rb", buffering=0)
        finally:
            if initialized:
                self.api.DeleteProcThreadAttributeList(attributes)
            if write.value:
                self.api.CloseHandle(write)
            if read.value:
                self.api.CloseHandle(read)

    def resume(self):
        self.resume_entered = True
        if not self.thread or self.api.ResumeThread(self.thread) == 0xFFFFFFFF:
            raise OSError("The contained process could not start.")
        self.api.CloseHandle(self.thread)
        self.thread = None

    def poll(self) -> int | None:
        if self.process is None:
            return None
        result = self.api.WaitForSingleObject(self.process, 0)
        if result == 258:
            return None
        if result != 0:
            raise OSError("The contained process exit state is unavailable.")
        code = W.DWORD()
        _check(self.api.GetExitCodeProcess(self.process, C.byref(code)))
        return code.value

    def terminate(self):
        _check(self.api.TerminateJobObject(self.handle, 1))

    def close(self):
        # Default training Jobs kill on close, including a suspended root after
        # an exception. Custody supervisors explicitly opt out and must retain
        # their original handles until complete drain; closing is not Stop.
        for name in ("handle", "thread", "process"):
            value = getattr(self, name, None)
            if value:
                if name != "handle" or self._owns_handle:
                    self.api.CloseHandle(value)
                setattr(self, name, None)
        if self.stdout is not None:
            self.stdout.close()
            self.stdout = None
