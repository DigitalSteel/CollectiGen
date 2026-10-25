"""Optional, process-bound GPU memory observations for short training trials.

These samples are a high-water of polls, not a peak allocation or fit proof.
Telemetry is deliberately separate from trainer receipts and result authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Event, Lock, Thread

from .training_process import TrainingRunJournal, _bytes, _read, _write_new
from .training_source_io import _assert_unredirected


NAME = "training-gpu-memory.json"
MAX_BYTES = 4096
SHA = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class TrialMemoryObservation:
    process_fingerprint: str
    execution_contract_sha256: str
    device_receipt_sha256: str
    runtime_profile: str
    sampled_high_water_bytes: int | None
    sample_count: int
    observed_at_utc: str

    def valid_for(self, plan, contract) -> bool:
        row = _device_input(contract)
        return (row is not None and self.process_fingerprint == plan.fingerprint
            and self.execution_contract_sha256 == plan.preview_fingerprint
            and self.device_receipt_sha256 == row["sha256"]
            and self.runtime_profile == contract.get("qualification", {}).get("runtime_profile")
            and type(self.sample_count) is int and 0 <= self.sample_count <= 2400
            and ((self.sample_count == 0 and self.sampled_high_water_bytes is None and self.observed_at_utc == "")
                or (self.sample_count > 0 and type(self.sampled_high_water_bytes) is int
                    and 0 <= self.sampled_high_water_bytes <= 128 * 1024**3
                    and _utc_valid(self.observed_at_utc))))

    def display(self) -> str:
        if self.sample_count == 0:
            return ("GPU memory observation unavailable for this exact trial. "
                "No peak requirement or memory-fit conclusion is available.")
        return (f"Sampled trainer-process GPU memory high-water: "
            f"{self.sampled_high_water_bytes / 1024**3:.2f} GiB across {self.sample_count} polls "
            f"(last sample {self.observed_at_utc}). This is a sampled lower bound: polls can miss "
            "a higher peak, and process telemetry may omit shared or offloaded memory; "
            "this is not a no-OOM guarantee or a qualified fit estimate.")


def _utc_valid(value):
    if type(value) is not str or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    except (ValueError, TypeError, AttributeError):
        return False


def _device_input(contract):
    try:
        rows = [row for row in contract["process"]["inputs"] if row["purpose"] == "device_receipt"]
        if len(rows) != 1 or type(rows[0]["sha256"]) is not str or not SHA.fullmatch(rows[0]["sha256"]):
            return None
        return rows[0]
    except (KeyError, TypeError):
        return None


def _device_identity(contract):
    row = _device_input(contract)
    if row is None:
        return None
    try:
        raw = _read(Path(row["path"]), limit=64 * 1024)
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            return None
        value = json.loads(raw)
        device = value["observation"]
        if (value.get("kind") != "sdxl_device_observation" or device["device_type"] != "cuda"
                or device["cuda_index"] != 0 or type(device["name"]) is not str
                or type(device["total_memory_bytes"]) is not int):
            return None
        return device["name"], device["total_memory_bytes"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _csv(command):
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result = subprocess.run([executable, command, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.0, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or len(result.stdout) > 8192:
            return None
        return [tuple(part.strip() for part in line.split(",")) for line in result.stdout.splitlines()]
    except (OSError, subprocess.SubprocessError):
        return None


def _native_sample(pid, device_name, total_bytes):
    """Require one matching physical GPU and one exact owned process context."""
    devices = _csv("--query-gpu=uuid,name,memory.total")
    if devices is None or len(devices) != 1 or len(devices[0]) != 3:
        return None
    uuid, name, total_mib = devices[0]
    try:
        if (name.casefold() != device_name.casefold()
                or abs(int(total_mib) * 1024**2 - total_bytes) > 64 * 1024**2):
            return None
    except (ValueError, TypeError):
        return None
    rows = _csv("--query-compute-apps=pid,gpu_uuid,used_gpu_memory")
    if rows is None:
        return None
    values = []
    for row in rows:
        if len(row) != 3:
            continue
        try:
            if int(row[0]) == pid and row[1] == uuid:
                amount = int(row[2])
                if amount < 0:
                    return None
                values.append(amount)
        except (ValueError, TypeError):
            continue  # WDDM often reports N/A; absence is unknown, not zero.
    return sum(values) * 1024**2 if values else None


class TrialMemorySampler:
    """Best-effort daemon sampler; it never participates in process control."""
    def __init__(self, plan, contract, *, sample_source=None):
        self.plan, self.contract = plan, contract
        self.device = _device_identity(contract)
        self.sample_source = sample_source or _native_sample
        self._stop = Event()
        self._lock = Lock()
        self._thread = None
        self._count = 0
        self._high = None
        self._last = ""

    def start(self):
        if self.device is None or self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="training-trial-memory", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set() and self._count < 2400:
            try:
                rows, _tip = TrainingRunJournal(self.plan.destination).read()
                running = [row for row in rows if row["state"] == "running"]
                pid = (running[-1]["details"].get("pid")
                    if running and rows[-1]["state"] == "running" else None)
                if type(pid) is int and 0 < pid <= 2**32 - 1:
                    value = self.sample_source(pid, *self.device)
                    if type(value) is int and 0 <= value <= 128 * 1024**3:
                        after, _tip = TrainingRunJournal(self.plan.destination).read()
                        again = [row for row in after if row["state"] == "running"]
                        if not again or after[-1]["state"] != "running" or again[-1]["details"].get("pid") != pid:
                            continue  # The process changed while telemetry was queried.
                        with self._lock:
                            self._count += 1
                            self._high = value if self._high is None else max(self._high, value)
                            self._last = datetime.now(timezone.utc).isoformat()
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                pass  # Telemetry failure cannot affect owned process or cancel.
            self._stop.wait(0.5)

    def stop(self) -> TrialMemoryObservation:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.5)  # Both native queries have a one-second deadline.
        with self._lock:
            count, high, last = self._count, self._high, self._last
        row = _device_input(self.contract)
        return TrialMemoryObservation(self.plan.fingerprint, self.plan.preview_fingerprint,
            row["sha256"] if row else "", self.contract.get("qualification", {}).get("runtime_profile", ""),
            high, count, last)


def retain_observation(plan, contract, observation):
    """Exclusive optional display evidence; failure must not affect run recovery."""
    if type(observation) is not TrialMemoryObservation or not observation.valid_for(plan, contract):
        raise ValueError("training_memory_observation_stale")
    raw = _bytes({"schema_version": 1, "kind": "training_trial_gpu_memory", **asdict(observation)})
    if len(raw) > MAX_BYTES:
        raise ValueError("training_memory_observation_bound")
    path = Path(plan.destination) / NAME
    _assert_unredirected(path)
    if os.path.lexists(path):
        if _read(path, limit=MAX_BYTES) != raw:
            raise ValueError("training_memory_observation_changed")
    else:
        _write_new(path, raw)
    return observation


def load_observation(plan, contract):
    path = Path(plan.destination) / NAME
    _assert_unredirected(path)
    if not os.path.lexists(path):
        return None
    raw = _read(path, limit=MAX_BYTES)
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {"schema_version", "kind", *TrialMemoryObservation.__dataclass_fields__}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value["kind"] != "training_trial_gpu_memory" or _bytes(value) != raw):
        raise ValueError("training_memory_observation_invalid")
    observed = TrialMemoryObservation(**{key: value[key] for key in TrialMemoryObservation.__dataclass_fields__})
    if not observed.valid_for(plan, contract):
        raise ValueError("training_memory_observation_stale")
    return observed
