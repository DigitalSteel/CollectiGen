from __future__ import annotations

import ctypes
import json
import os
import signal
import sys
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from threading import RLock
from typing import Callable,Iterator
from uuid import uuid4


_LEASE_SCHEMA_VERSION=1


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    executable: str
    started: int


@dataclass(frozen=True)
class AIWorkRecovery:
    status: str
    label: str = ""
    pid: int = 0
    message: str = ""

    @property
    def needs_confirmation(self) -> bool:
        return self.status=="active"


def _normalized_path(value: str|Path) -> str:
    try: normalized=str(Path(value).expanduser().resolve())
    except (OSError,RuntimeError): normalized=os.path.abspath(os.fspath(value))
    return os.path.normcase(normalized)


def _windows_kernel32():
    kernel32=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel32.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
    kernel32.OpenProcess.restype=wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes=(
        wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype=wintypes.BOOL
    kernel32.GetProcessTimes.argtypes=(
        wintypes.HANDLE,ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype=wintypes.BOOL
    kernel32.TerminateProcess.argtypes=(wintypes.HANDLE,wintypes.UINT)
    kernel32.TerminateProcess.restype=wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes=(wintypes.HANDLE,wintypes.DWORD)
    kernel32.WaitForSingleObject.restype=wintypes.DWORD
    kernel32.CloseHandle.argtypes=(wintypes.HANDLE,)
    kernel32.CloseHandle.restype=wintypes.BOOL
    return kernel32


def _windows_process_identity(pid: int) -> ProcessIdentity|None:
    kernel32=_windows_kernel32()
    query_limited_information=0x1000
    handle=kernel32.OpenProcess(query_limited_information,False,int(pid))
    if not handle:
        return None
    try:
        size=ctypes.c_ulong(32768)
        executable=ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,0,executable,ctypes.byref(size),
        ):
            return None
        creation=wintypes.FILETIME()
        exit_time=wintypes.FILETIME()
        kernel=wintypes.FILETIME()
        user=wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,ctypes.byref(creation),ctypes.byref(exit_time),
            ctypes.byref(kernel),ctypes.byref(user),
        ):
            return None
        started=(int(creation.dwHighDateTime)<<32)|int(creation.dwLowDateTime)
        return ProcessIdentity(int(pid),_normalized_path(executable.value),started)
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_identity(pid: int) -> ProcessIdentity|None:
    process=Path("/proc")/str(int(pid))
    try:
        executable=_normalized_path((process/"exe").resolve())
        started=int(process.stat().st_ctime_ns)
    except (FileNotFoundError,PermissionError,OSError,RuntimeError):
        return None
    return ProcessIdentity(int(pid),executable,started)


def inspect_process(pid: int) -> ProcessIdentity|None:
    if int(pid)<=0: return None
    if sys.platform=="win32":
        return _windows_process_identity(pid)
    return _posix_process_identity(pid)


def current_process_identity() -> ProcessIdentity|None:
    return inspect_process(os.getpid())


def _same_process(left: ProcessIdentity,right: ProcessIdentity) -> bool:
    return (
        left.pid==right.pid
        and left.started==right.started
        and _normalized_path(left.executable)==_normalized_path(right.executable)
    )


def terminate_verified_process(expected: ProcessIdentity) -> bool:
    """Stop only the exact process identity recorded by CollectiGen."""
    actual=inspect_process(expected.pid)
    if actual is None: return True
    if not _same_process(actual,expected) or expected.pid==os.getpid(): return False
    if sys.platform=="win32":
        kernel32=_windows_kernel32()
        terminate=0x0001; synchronize=0x00100000; query=0x1000
        handle=kernel32.OpenProcess(terminate|synchronize|query,False,expected.pid)
        if not handle: return False
        try:
            actual=inspect_process(expected.pid)
            if actual is None: return True
            if not _same_process(actual,expected): return False
            if not kernel32.TerminateProcess(handle,1): return False
            return kernel32.WaitForSingleObject(handle,10000)==0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(expected.pid,signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError,OSError):
        return False
    return inspect_process(expected.pid) is None


def _read_marker(path: Path) -> dict:
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError,OSError,UnicodeError,json.JSONDecodeError):
        return {}
    return value if isinstance(value,dict) else {}


def _remove_marker(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def recover_ai_work(
    path: str|Path,app_root: str|Path,*,terminate: bool=False,
    process_inspector: Callable[[int],ProcessIdentity|None]|None=None,
    process_terminator: Callable[[ProcessIdentity],bool]|None=None,
) -> AIWorkRecovery:
    """Recover a prior AI lease without ever targeting an unverified process."""
    marker_path=Path(path)
    if not marker_path.is_file(): return AIWorkRecovery("none")
    payload=_read_marker(marker_path)
    try:
        expected=ProcessIdentity(
            int(payload["pid"]),str(payload["executable"]),int(payload["process_started"]),
        )
        label=str(payload.get("label","AI analysis"))
        valid=(
            int(payload.get("schema_version",0))==_LEASE_SCHEMA_VERSION
            and bool(payload.get("lease_id"))
            and expected.pid>0 and expected.started>0
            and _normalized_path(payload["app_root"])==_normalized_path(app_root)
        )
    except (KeyError,TypeError,ValueError,OSError):
        valid=False
        expected=ProcessIdentity(0,"",0); label=""
    if not valid:
        cleared=_remove_marker(marker_path)
        return AIWorkRecovery(
            "stale_cleared" if cleared else "cleanup_failed",
            message="Invalid interrupted AI state was cleared." if cleared else
            "Invalid interrupted AI state could not be cleared.",
        )
    inspector=process_inspector or inspect_process
    actual=inspector(expected.pid)
    if actual is None or not _same_process(actual,expected):
        cleared=_remove_marker(marker_path)
        return AIWorkRecovery(
            "stale_cleared" if cleared else "cleanup_failed",label,expected.pid,
            "Interrupted AI state from the previous session was cleared." if cleared else
            "Interrupted AI state from the previous session could not be cleared.",
        )
    if not terminate:
        return AIWorkRecovery(
            "active",label,expected.pid,
            "AI work from a prior CollectiGen session is still running.",
        )
    terminator=process_terminator or terminate_verified_process
    if terminator(expected) and _remove_marker(marker_path):
        return AIWorkRecovery(
            "terminated",label,expected.pid,
            "Prior CollectiGen AI work was stopped and its interrupted state was cleared.",
        )
    return AIWorkRecovery(
        "cleanup_failed",label,expected.pid,
        "Prior CollectiGen AI work could not be stopped safely.",
    )


class AIWorkLease:
    """Durably identify AI work owned by this exact CollectiGen process."""

    def __init__(
        self,path: str|Path,app_root: str|Path,*,
        identity_provider: Callable[[],ProcessIdentity|None]|None=None,
    ) -> None:
        self.path=Path(path); self.app_root=Path(app_root)
        self._identity_provider=identity_provider or current_process_identity
        self._identity: ProcessIdentity|None=None
        self._lease_id=""
        self._depth=0
        self._label=""
        self._state="active"
        self._lock=RLock()
        self.last_error=""

    def _payload(self) -> dict:
        identity=self._identity
        if identity is None: return {}
        now=datetime.now(timezone.utc).isoformat()
        return {
            "schema_version":_LEASE_SCHEMA_VERSION,
            "lease_id":self._lease_id,
            "pid":identity.pid,
            "process_started":identity.started,
            "executable":identity.executable,
            "app_root":str(self.app_root.resolve()),
            "label":self._label,
            "state":self._state,
            "updated_at":now,
        }

    def _write(self) -> bool:
        payload=self._payload()
        if not payload: return False
        temporary=self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary.write_text(
                json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8",
            )
            os.replace(temporary,self.path)
            self.last_error=""
            return True
        except OSError as exc:
            self.last_error=str(exc)
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
            return False

    def begin(self,label: str) -> bool:
        with self._lock:
            if self._depth:
                self._depth+=1
                return True
            identity=self._identity_provider()
            if identity is None:
                self.last_error="The current process identity could not be verified."
                return False
            self._identity=identity
            self._lease_id=uuid4().hex
            self._label=str(label or "AI analysis")
            self._state="active"
            if not self._write():
                self._identity=None; self._lease_id=""; self._label=""
                return False
            self._depth=1
            return True

    def request_stop(self) -> None:
        with self._lock:
            if not self._depth: return
            self._state="stopping"
            self._write()

    def finish(self) -> None:
        with self._lock:
            if not self._depth: return
            self._depth-=1
            if self._depth: return
            payload=_read_marker(self.path)
            if payload.get("lease_id")==self._lease_id:
                _remove_marker(self.path)
            self._identity=None; self._lease_id=""; self._label=""; self._state="active"

    @contextmanager
    def tracked(self,label: str) -> Iterator[None]:
        started=self.begin(label)
        try:
            yield
        finally:
            if started: self.finish()
