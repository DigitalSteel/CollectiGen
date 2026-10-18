"""Bounded memory-evidence consumers; no native instrumentation or discovery.

Persisted measurements are untrusted observations. Only an explicitly supplied
native verifier can attest their provenance and the current execution identity.
The shipped verifier is unavailable and performs no filesystem, process or GPU IO.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Protocol

from .generation_resource_guard import memory_request_key, review_memory_observation

TRACE_SCHEMA = "collectigen.generation-memory-trace.v1"
STORED_SCHEMA = "collectigen.retained-memory-evidence.v1"
MAX_RECORD_BYTES = 512 * 1024
MAX_EVENTS = 2048
MAX_RECORDS_PER_JOB = 8
MAX_PROVIDER_RECORDS = 256
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PHASES = ("begin", "load", "diffusion", "decode", "finish", "end")
_MAX_ELAPSED_NS = 15 * 60 * 1_000_000_000


def _encoded(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Memory evidence must contain finite bounded JSON values.") from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Memory evidence exceeds the bounded record size.")
    if json.loads(raw) != value:
        raise ValueError("Memory evidence must preserve exact JSON types on read-back.")
    return raw


def evidence_digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Memory evidence needs an aware timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Memory evidence needs an aware timestamp.") from exc
    if result.tzinfo is None:
        raise ValueError("Memory evidence needs an aware timestamp.")
    return result


def retain_measurement(measurement, prepared, output_sha256):
    """Freeze bounded raw evidence; do not endorse any producer qualification."""
    if (not isinstance(measurement, dict)
            or not isinstance(measurement.get("schema_id"), str)
            or not 1 <= len(measurement["schema_id"]) <= 128):
        raise ValueError("Memory evidence needs a bounded schema identifier.")
    request_key = memory_request_key(prepared)
    if (not request_key or measurement.get("request_fingerprint") != request_key
            or not _hash(output_sha256) or measurement.get("output_sha256") != output_sha256):
        raise ValueError("Memory evidence does not match this exact request and verified output.")
    events = measurement.get("events")
    if isinstance(events, list) and len(events) > MAX_EVENTS:
        raise ValueError("Memory evidence exceeds the bounded event count.")
    result = {"schema_id": STORED_SCHEMA, "measurement": deepcopy(measurement),
              "measurement_sha256": evidence_digest(measurement)}
    _encoded(result)
    return result


@dataclass(frozen=True)
class NativeMemoryAttestation:
    """Trusted verifier result, never deserialized from a retained record.

    The verifier must independently validate native high-water coverage,
    including non-allocator usage, and loaded artifacts/device/driver identity.
    Hashes or flags asserted by a producer are not that verification.
    """
    request_fingerprint: str
    measurement_sha256: str
    runtime_fingerprint: str
    hardware_fingerprint: str
    measurement_process_fingerprint: str
    current_process_fingerprint: str
    reported_gpu_fingerprint: str
    verified_at: str
    capability: str = "unavailable"


class NativeMemoryVerifier(Protocol):
    available: bool

    def verify(self, prepared, observation, retained) -> NativeMemoryAttestation | None: ...


class UnavailableNativeMemoryVerifier:
    available = False

    def verify(self, prepared, observation, retained):
        return None


def qualify_measurement(retained, prepared, observation, attestation, *, now=None):
    """Validate a trusted attestation against raw evidence; never infer one."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(attestation, NativeMemoryAttestation):
        raise ValueError("Native memory verification is unavailable.")
    if (attestation.capability != "process_allocation_high_water_v1"
            or any(not _hash(getattr(attestation, key)) for key in (
                "request_fingerprint", "measurement_sha256", "runtime_fingerprint",
                "hardware_fingerprint", "measurement_process_fingerprint",
                "current_process_fingerprint", "reported_gpu_fingerprint"))):
        raise ValueError("Process-wide native memory coverage is unsupported.")
    age = (now - _timestamp(attestation.verified_at)).total_seconds()
    current = review_memory_observation(observation, now=now)
    if (not 0 <= age <= 30 or current["reason"] != "current"
            or attestation.reported_gpu_fingerprint != observation.get("gpu_fingerprint")):
        raise ValueError("Current native identity or observation is stale or unavailable.")
    if not isinstance(retained, dict) or retained.get("schema_id") != STORED_SCHEMA:
        raise ValueError("Retained memory evidence uses an unsupported schema.")
    _encoded(retained)
    trace = retained.get("measurement")
    measured_hash = evidence_digest(trace)
    if (not isinstance(trace, dict) or trace.get("schema_id") != TRACE_SCHEMA
            or retained.get("measurement_sha256") != measured_hash
            or attestation.measurement_sha256 != measured_hash
            or trace.get("request_fingerprint") != memory_request_key(prepared)
            or attestation.request_fingerprint != trace.get("request_fingerprint")
            or trace.get("stages_fingerprint") != evidence_digest(prepared.get("plan", {}).get("request", {}).get("stages", []))):
        raise ValueError("Memory measurement identity does not match the exact request.")
    for key, actual in (("runtime_fingerprint", attestation.runtime_fingerprint),
                        ("hardware_fingerprint", attestation.hardware_fingerprint),
                        ("process_fingerprint", attestation.measurement_process_fingerprint),
                        ("gpu_fingerprint", attestation.reported_gpu_fingerprint)):
        if trace.get(key) != actual:
            raise ValueError("Memory measurement runtime, process or device identity changed.")
    if (trace.get("scope") != "request_process" or trace.get("method") != "native_high_water"
            or trace.get("coverage") != "complete_request" or trace.get("completed") is not True
            or trace.get("successful") is not True or type(trace.get("dropped_events")) is not int
            or trace["dropped_events"] != 0 or type(trace.get("counter_resets")) is not int
            or trace["counter_resets"] != 0 or type(trace.get("competing_gpu_jobs")) is not int
            or trace["competing_gpu_jobs"] != 0):
        raise ValueError("Memory measurement is incomplete or not attributable to this request.")
    start, end = _timestamp(trace.get("started_at")), _timestamp(trace.get("completed_at"))
    elapsed = (end - start).total_seconds()
    if not 0 < elapsed <= 900 or not 0 <= (now - end).total_seconds() <= 30 * 86400:
        raise ValueError("Memory measurement time is stale, future or inconsistent.")
    events = trace.get("events")
    if not isinstance(events, list) or len(events) != len(_PHASES):
        raise ValueError("Memory measurement must cover every required phase.")
    previous_time, previous_peak = -1, 0
    for phase, event in zip(_PHASES, events):
        if not isinstance(event, dict):
            raise ValueError("Memory measurement phase is malformed.")
        ticks, peak = event.get("elapsed_ns"), event.get("peak_bytes")
        if (event.get("phase") != phase or type(ticks) is not int
                or not previous_time < ticks <= _MAX_ELAPSED_NS
                or type(peak) is not int or not previous_peak <= peak <= (1 << 64) - 1
                or event.get("process_fingerprint") != trace["process_fingerprint"]
                or event.get("gpu_fingerprint") != trace["gpu_fingerprint"]):
            raise ValueError("Memory measurement phase order, counter or attribution changed.")
        previous_time, previous_peak = ticks, peak
    if (events[0]["elapsed_ns"] != 0 or previous_peak == 0
            or abs(previous_time / 1e9 - elapsed) > .001
            or previous_peak / (1 << 30) > current["total_vram_gib"]):
        raise ValueError("Memory measurement counters do not cover the complete request.")
    context = {"identity_verified": True, "request_fingerprint": attestation.request_fingerprint,
               "runtime_fingerprint": attestation.runtime_fingerprint,
               "hardware_fingerprint": attestation.hardware_fingerprint,
               "reported_gpu_fingerprint": attestation.reported_gpu_fingerprint}
    record = {"schema_id": "collectigen.generation-memory-evidence.v1", "qualified": True,
              "kind": "measured", "scope": "request_process", "coverage": "complete_request",
              "completed": True, "successful": True, "competing_gpu_jobs": 0,
              "request_fingerprint": attestation.request_fingerprint,
              "runtime_fingerprint": attestation.runtime_fingerprint,
              "hardware_fingerprint": attestation.hardware_fingerprint,
              "stages_fingerprint": trace["stages_fingerprint"], "measurement_sha256": measured_hash,
              "observed_at": trace["completed_at"], "peak_vram_gib": previous_peak / (1 << 30)}
    return context, record


class GenerationMemoryEvidenceProvider:
    def __init__(self, history_reader, verifier=None):
        self.history_reader = history_reader
        self.verifier = verifier if verifier is not None else UnavailableNativeMemoryVerifier()

    def __call__(self, prepared, observation):
        unavailable = {"context": {}, "records": [], "reason": "native_verifier_unavailable"}
        if getattr(self.verifier, "available", False) is not True:
            return unavailable  # Even history IO is unnecessary without a verifier.
        try:
            rows = self.history_reader(prepared)
        except Exception:
            return {**unavailable, "reason": "history_unavailable"}
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_PROVIDER_RECORDS:
            return {**unavailable, "reason": "history_exceeds_bound"}
        context, records, process = {}, [], None
        for retained in rows:
            try:
                proof = self.verifier.verify(deepcopy(prepared), deepcopy(observation), deepcopy(retained))
                current, record = qualify_measurement(retained, prepared, observation, proof)
            except Exception:
                # Preserve one non-authoritative row so Readiness distinguishes
                # retained but unusable evidence from absence. Never return raw data.
                records.append({"qualified": False})
                continue
            if context and (context != current or process != proof.current_process_fingerprint):
                return {**unavailable, "reason": "identity_changed_during_review"}
            context, process = current, proof.current_process_fingerprint
            records.append(record)
        return {"context": context, "records": records, "reason": "reviewed"}
