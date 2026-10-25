"""Opt-in, read-only local metrics. Importing starts no probe or polling."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import csv
import ctypes
import hashlib
import io
import math
import re
import shutil
import subprocess
import sys
import time

from .backend_contracts import ApplicationId, HostId, ResourceGroup, ResourceGroupId, ResourceTelemetry, StableId
from .inventory_view import DetectionState, HardwareObservation

MODULE_VERSION = "0.1.0-alpha.1"
RESOURCE_PROVIDER_INTERFACE = 1
TTL = timedelta(seconds=5)
MAX_GPUS = 16
QUERY = "--query-gpu=uuid,name,memory.total,memory.used,memory.free,utilization.gpu"


@dataclass(frozen=True, slots=True)
class ResourceReading:
    hardware: HardwareObservation
    group: ResourceGroup | None = None
    telemetry: ResourceTelemetry | None = None
    utilization_percent: float | None = None
    used_memory_bytes: int | None = None
    unaccounted_memory_bytes: int | None = None
    reason: str = "measured"

    def state_at(self, now):
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("aware_time_required")
        if self.hardware.state is not DetectionState.OBSERVED:
            return "unsupported" if self.hardware.state is DetectionState.UNSUPPORTED else "unknown"
        if now < self.hardware.observed_at:
            return "future"
        return "measured" if now - self.hardware.observed_at <= self.hardware.valid_for else "stale"

    def current_values(self, now):
        """Presentation seam: never expose retained/future values as current capacity."""
        current = self.state_at(now) == "measured"
        telemetry = self.telemetry if current else None
        available = None if telemetry is None else (
            telemetry.free_vram_bytes if self.hardware.kind == "gpu" else telemetry.free_ram_bytes)
        return {"state": self.state_at(now), "reason": self.reason,
                "utilization_percent": self.utilization_percent if current else None,
                "available_cpu_time_percent": (100 - self.utilization_percent) if current
                    and self.hardware.kind == "cpu" and self.utilization_percent is not None else None,
                "total_memory_bytes": self.hardware.total_memory_bytes if current else None,
                "used_memory_bytes": self.used_memory_bytes if current else None,
                "available_memory_bytes": available,
                "unaccounted_memory_bytes": self.unaccounted_memory_bytes if current else None}


@dataclass(frozen=True, slots=True)
class LocalResourceSnapshot:
    readings: tuple[ResourceReading, ...]
    polling_enabled: bool = False


def windows_metrics(interval=0.1):
    """System-wide CPU busy time and RAM available for reuse; no process enumeration."""
    if sys.platform != "win32":
        raise NotImplementedError
    from ctypes import wintypes

    class Memory(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (key, ctypes.c_ulonglong) for key in
            ("total", "available", "page_total", "page_free", "virtual_total", "virtual_free", "extended")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_times = kernel.GetSystemTimes
    get_times.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
    get_times.restype = wintypes.BOOL
    get_memory = kernel.GlobalMemoryStatusEx
    get_memory.argtypes = [ctypes.POINTER(Memory)]
    get_memory.restype = wintypes.BOOL

    def times():
        values = [wintypes.FILETIME() for _ in range(3)]
        if not get_times(*(ctypes.byref(v) for v in values)):
            raise OSError("cpu_api_failed")
        return tuple((v.dwHighDateTime << 32) | v.dwLowDateTime for v in values)

    before = times()
    time.sleep(interval)
    after = times()
    idle, kernel_delta, user = (b - a for a, b in zip(before, after))
    total = kernel_delta + user  # Windows kernel time includes idle.
    # Each FILETIME counter must advance independently. Kernel includes idle;
    # a positive aggregate can conceal rollback/contradictory per-counter data.
    cpu = None if (total <= 0 or idle < 0 or kernel_delta < 0 or user < 0
                   or idle > kernel_delta) else 100 * (total - idle) / total
    memory = Memory()
    memory.length = ctypes.sizeof(Memory)
    if not get_memory(ctypes.byref(memory)):
        raise OSError("memory_api_failed")
    return cpu, memory.total, memory.available


def _bytes(value):
    # NVIDIA nounits memory values are MiB, not decimal MB.
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("invalid_memory")
    result = int(value) * 1024**2
    if result > 2**63 - 1:
        raise ValueError("memory_bound")
    return result


def parse_nvidia(text):
    """Strict UUID census. Repeated identical identities collapse; contradictions hold."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 16384:
        raise ValueError("output_bound")
    devices = {}
    rows = list(csv.reader(io.StringIO(text), skipinitialspace=True))
    if not rows or len(rows) > MAX_GPUS * 2:
        raise ValueError("device_bound")
    for row in rows:
        if len(row) != 6:
            raise ValueError("invalid_row")
        uuid, name, total, used, free, utilization = (v.strip() for v in row)
        if not re.fullmatch(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid):
            raise ValueError("unverified_device_identity")
        if not name or len(name) > 128 or any(ord(c) < 32 for c in name):
            raise ValueError("invalid_device_label")
        total, used, free = map(_bytes, (total, used, free))
        if total <= 0 or used + free > total:
            raise ValueError("inconsistent_memory")
        utilization = None if utilization in {"N/A", "[N/A]", "[Not Supported]"} else float(utilization)
        if utilization is not None and (not math.isfinite(utilization) or not 0 <= utilization <= 100):
            raise ValueError("invalid_utilization")
        uuid = "GPU-" + uuid[4:].lower()
        values = (name, total, used, free, utilization)
        if uuid in devices and devices[uuid] != values:
            raise ValueError("contradictory_device")
        devices[uuid] = values
    if len(devices) > MAX_GPUS:
        raise ValueError("device_bound")
    return tuple(sorted(devices.items()))


def sample_local_resources(owner: ApplicationId, host: HostId, *,
                           system_reader=windows_metrics, which=shutil.which,
                           runner=subprocess.run, clock=lambda: datetime.now(timezone.utc),
                           cpu_interval=0.1, gpu_timeout=2.0):
    """One explicit sample. Injected readers are test seams, not untrusted plugins."""
    if type(owner) is not ApplicationId or type(host) is not HostId:
        raise ValueError("typed_owner_host_required")
    if not 0.05 <= cpu_interval <= 0.5 or not 0.1 <= gpu_timeout <= 3:
        raise ValueError("sampling_bound")
    readings = []

    def reading(kind, label, source, at, *, state=DetectionState.OBSERVED,
                group=None, telemetry=None, utilization=None, used=None, unaccounted=None,
                total=None, reason="measured", identity=None):
        observation = HardwareObservation(owner, host, StableId(identity or f"local.{kind}"),
            StableId(source), at, TTL, state, kind, label, total,
            group.id if group else None)
        readings.append(ResourceReading(observation, group, telemetry, utilization, used, unaccounted, reason))

    try:
        cpu, total, available = system_reader(cpu_interval)
        if (type(total) is not int or type(available) is not int or
                not 0 <= available <= total <= 2**63 - 1 or total == 0):
            raise ValueError("invalid_memory")
        if cpu is not None and (type(cpu) not in (int, float) or not math.isfinite(cpu) or not 0 <= cpu <= 100):
            raise ValueError("invalid_cpu")
    except (NotImplementedError, OSError, ValueError, TypeError) as error:
        at = clock()
        state = DetectionState.UNSUPPORTED if isinstance(error, NotImplementedError) else DetectionState.FAILED
        for kind in ("cpu", "ram"):
            reading(kind, kind.upper(), "windows.system", at, state=state, reason="system_metrics_unavailable")
    else:
        at = clock()
        reading("cpu", "CPU busy time", "windows.system", at, utilization=cpu,
                state=DetectionState.OBSERVED if cpu is not None else DetectionState.FAILED,
                reason="measured" if cpu is not None else "cpu_interval_unavailable")
        group = ResourceGroup(ResourceGroupId("local.ram." + hashlib.sha256(host.value.encode()).hexdigest()[:24]),
                              host, StableId("host.physical.ram"), True)
        telemetry = ResourceTelemetry(group.id, at, TTL, free_ram_bytes=available, total_ram_bytes=total)
        reading("ram", "Physical RAM", "windows.system", at, group=group, telemetry=telemetry,
                total=total, used=total - available, unaccounted=0)
    executable = which("nvidia-smi")
    if executable is None:
        reading("gpu", "GPU metrics", "nvidia.smi", clock(), state=DetectionState.UNSUPPORTED,
                reason="nvidia_tool_missing")
    else:
        try:
            result = runner([executable, QUERY, "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=gpu_timeout, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                raise OSError("driver_query_failed")
            devices = parse_nvidia(result.stdout)
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
            reason = "gpu_timeout" if isinstance(error, subprocess.TimeoutExpired) else "gpu_query_failed"
            reading("gpu", "GPU metrics", "nvidia.smi", clock(), state=DetectionState.FAILED, reason=reason)
        else:
            at = clock()
            for uuid, (name, total, used, free, utilization) in devices:
                # Scope group identity to caller-supplied host, never machine/user names.
                identity = "local.gpu." + hashlib.sha256((host.value + ":" + uuid).encode()).hexdigest()[:32]
                group = ResourceGroup(ResourceGroupId(identity), host, StableId(uuid), True)
                telemetry = ResourceTelemetry(group.id, at, TTL, free_vram_bytes=free, total_vram_bytes=total)
                reading("gpu", name, "nvidia.smi", at, group=group, telemetry=telemetry,
                        utilization=utilization, total=total, used=used, unaccounted=total - used - free,
                        identity=identity)
    return LocalResourceSnapshot(tuple(readings))
