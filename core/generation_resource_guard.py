"""Privacy-safe, evidence-limited memory observations for local generation.

These helpers do not estimate a model's peak use. A caller may supply a peak
only when it comes from an explicitly qualified measurement for that request.
Current free VRAM is volatile and never proves that a generation will fit.
"""
from __future__ import annotations

import math
import re
import hashlib
import json
from datetime import datetime, timezone
from itertools import islice

_MAX_BYTES = (1 << 64) - 1
_GIB = 1 << 30
OBSERVATION_MAX_AGE_SECONDS = 30
PROFILE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OOM = re.compile(
    r"\bout[\s_-]*of[\s_-]*memory(?:error|exception)?\b|outofmemory(?:error|exception)?|\boom\b|"
    r"\binsufficient\s+(?:gpu|device|video)\s+memory\b|"
    r"\b(?:cuda|hip|mps|vram|gpu)\b[^\n]{0,48}\bmemory\s+(?:allocation\s+)?failed\b",
    re.IGNORECASE,
)


def _unknown(message: str) -> dict:
    return {"status": "unknown", "available_vram_gib": None,
            "total_vram_gib": None, "message": message}


def normalize_swarm_memory(response) -> dict:
    """Project GetServerResourceInfo to one device's byte-based VRAM only.

    GPU names, identifiers, CPU/RAM details and arbitrary response text are
    discarded. Multiple devices stay unknown without an exact device binding.
    """
    if not isinstance(response, dict) or not isinstance(response.get("gpus"), dict):
        return _unknown("GPU VRAM observation is unavailable.")
    gpus = response["gpus"]
    if len(gpus) != 1:
        return _unknown("Exact execution GPU is unknown; VRAM cannot be attributed to one device.")
    gpu = next(iter(gpus.values()))
    if not isinstance(gpu, dict):
        return _unknown("GPU VRAM observation is unavailable.")
    total, used, free = (gpu.get(key) for key in
                         ("total_memory", "used_memory", "free_memory"))
    if (any(type(value) is not int or not 0 <= value <= _MAX_BYTES
            for value in (total, used, free)) or total == 0
            or used > total or free > total or used + free != total):
        return _unknown("GPU VRAM byte counts are unavailable or inconsistent.")
    return {"status": "measured", "available_vram_gib": free / _GIB,
            "total_vram_gib": total / _GIB,
            "message": "Current free VRAM was observed on one GPU; it can change before allocation."}


def assess_memory(machine, required_peak_gib=None) -> dict:
    """Return limited only for zero free VRAM or a qualified explicit peak.

    A positive free amount, even above a supplied peak, is not proof of fit.
    """
    if required_peak_gib is not None and (
            type(required_peak_gib) not in (int, float)
            or not math.isfinite(required_peak_gib) or required_peak_gib <= 0):
        raise ValueError("A qualified peak must be a positive finite GiB value.")
    available = machine.get("available_vram_gib") if isinstance(machine, dict) else None
    total = machine.get("total_vram_gib") if isinstance(machine, dict) else None
    measured = (isinstance(machine, dict) and machine.get("status") == "measured"
                and type(available) in (int, float) and math.isfinite(available)
                and type(total) in (int, float) and math.isfinite(total)
                and total > 0 and 0 <= available <= total)
    if not measured:
        return {"status": "unknown", "available_vram_gib": None,
                "required_peak_gib": required_peak_gib,
                "message": "Current free VRAM is unknown; model fit was not assessed."}
    if available == 0:
        status = "limited"
        message = "Observed free VRAM is zero; GPU allocation cannot be admitted now."
    elif required_peak_gib is not None and available < required_peak_gib:
        status = "limited"
        message = "Observed free VRAM is below the supplied qualified peak requirement."
    else:
        status = "not_proven"
        message = "Free VRAM was observed, but it does not prove this request will fit."
    return {"status": status, "available_vram_gib": available,
            "required_peak_gib": required_peak_gib, "message": message}


def _age(value, now):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        return (now - timestamp).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None


def review_memory_observation(machine, *, now=None):
    """Age and validate an observation without converting it into request fit.

    The timestamp identifies when the adapter received the observation. It is
    not a guarantee about a generator's internal sensor refresh or future use.
    """
    now = now or datetime.now(timezone.utc)
    machine = machine if isinstance(machine, dict) else {}
    age = _age(machine.get("observed_at"), now)
    reason = ("missing_time" if age is None else "future" if age < 0 else
              "stale" if age > OBSERVATION_MAX_AGE_SECONDS else "current")
    assessed = assess_memory(machine)
    if reason == "current" and assessed["status"] == "unknown":
        reason = "unavailable"
    if reason != "current":
        return {"status": "unknown", "reason": reason, "available_vram_gib": None,
                "total_vram_gib": None,
                "message": ("The GPU memory observation is stale; check the reviewed request again to refresh it."
                            if reason == "stale" else
                            "GPU memory observation has no valid current timestamp; check the reviewed request again."
                            if reason in {"missing_time", "future"} else
                            "Current free GPU memory is unavailable; exact request fit remains unproven.")}
    return {**assessed, "reason": "current", "total_vram_gib": machine["total_vram_gib"],
            "message": ("Observed free GPU memory is zero. Free memory, then use Check Reviewed Request before submitting."
                        if assessed["status"] == "limited" else
                        "Free GPU memory was observed; other work and future allocations remain unknown.")}


def memory_request_key(prepared):
    """Bind a calibration to exact computation, including seed and companions.

    Unlike observed time ranges, a qualified memory record cannot borrow a
    different seed, prompt, conditioning input, component or runtime option.
    Saved advisory recommendations and arbitrary supplied peak fields are not
    an execution authority and do not form this identity.
    """
    if not isinstance(prepared, dict) or not isinstance(prepared.get("payload"), dict):
        return ""
    caps, binding = prepared.get("capabilities"), prepared.get("binding")
    if (not isinstance(caps, dict) or not caps.get("fingerprint")
            or not isinstance(binding, dict) or not binding.get("resource_id")):
        return ""
    return _digest({"payload": {k: v for k, v in prepared["payload"].items()
                                if k not in {"session_id", "extra_metadata"}},
                    "binding": binding, "components": prepared.get("components"),
                    "conditioning": prepared.get("conditioning"),
                    "source_image": prepared.get("source_image"), "mask": prepared.get("mask"),
                    "output_plan": prepared.get("output_plan"),
                    "refinement": prepared.get("refinement"), "runtime": caps["fingerprint"]})


def assess_memory_evidence(stages, context, observation, records=(), *, now=None):
    """Interpret qualified caller evidence; never qualify a raw measurement.

    An evidence provider must independently verify native runtime artifacts and
    hardware/driver identity. Swarm's reported capability/device hashes alone do
    not supply that authority. Legacy or incomplete records remain unqualified;
    this function neither migrates them nor writes a calibration result.
    """
    now = now or datetime.now(timezone.utc)
    current = review_memory_observation(observation, now=now)
    observation = observation if isinstance(observation, dict) else {}
    context = context if isinstance(context, dict) else {}
    available = current["available_vram_gib"]
    result = {"evidence": "unknown", "assessment": "not_proven",
              "peak_vram_gib": None, "observed_peak_range_gib": None, "available_vram_gib": available,
              "reason": "no_qualified_profile", "qualified_samples": 0,
              "unqualified_samples": 0,
              "message": "No qualified exact-request memory profile exists. Use a reviewed smaller base or proceed knowing fit is unproven."}
    rows = list(islice(records, 257)) if isinstance(records, (list, tuple)) else []
    bounded = len(rows) <= 256
    required = ("request_fingerprint", "runtime_fingerprint", "hardware_fingerprint")
    exact_context = (bounded and isinstance(stages, (list, tuple))
                     and context.get("identity_verified") is True
                     and all(isinstance(context.get(key), str) and _SHA256.fullmatch(context[key])
                             for key in required)
                     and bool(observation.get("gpu_fingerprint"))
                     and context.get("reported_gpu_fingerprint") == observation.get("gpu_fingerprint"))
    stage_key = _digest(stages)
    matches = []
    for row in rows:
        age = _age(row.get("observed_at"), now) if isinstance(row, dict) else None
        qualified = (exact_context and isinstance(row, dict)
            and row.get("schema_id") == "collectigen.generation-memory-evidence.v1"
            and row.get("qualified") is True
            and all(row.get(key) == context[key] for key in required)
            and row.get("stages_fingerprint") == stage_key
            and age is not None and 0 <= age <= PROFILE_MAX_AGE_SECONDS
            and type(row.get("peak_vram_gib")) in (int, float)
            and math.isfinite(row["peak_vram_gib"]) and row["peak_vram_gib"] > 0
            and (current["total_vram_gib"] is None or row["peak_vram_gib"] <= current["total_vram_gib"]))
        kind = row.get("kind") if isinstance(row, dict) else None
        if qualified and kind == "measured":
            qualified = (row.get("scope") == "request_process"
                         and row.get("coverage") == "complete_request"
                         and row.get("completed") is True and row.get("successful") is True
                         and type(row.get("competing_gpu_jobs")) is int
                         and row["competing_gpu_jobs"] == 0
                         and isinstance(row.get("measurement_sha256"), str)
                         and bool(_SHA256.fullmatch(row["measurement_sha256"])))
        elif qualified and kind == "estimated":
            qualified = (row.get("scope") == "estimate" and isinstance(row.get("basis"), str)
                         and 0 < len(row["basis"].strip()) <= 1024)
        else:
            qualified = False
        if qualified:
            matches.append(row)
        else:
            result["unqualified_samples"] += 1
    if matches:
        # Conflicting exact records use the highest qualified bound, never the
        # first or most optimistic record. Estimates cannot become admission limits.
        measured = [row for row in matches if row["kind"] == "measured"]
        chosen = measured or matches
        peak = max(float(row["peak_vram_gib"]) for row in chosen)
        result.update(evidence="measured" if measured else "estimated",
                      peak_vram_gib=peak, qualified_samples=len(chosen), reason="applicable")
        result["message"] = ("Qualified process measurements match this exact request, runtime and hardware; future fit is still unproven."
                             if measured else "An exact-context estimate is available; it is provisional and cannot grant or refuse memory admission.")
        if measured:
            result["observed_peak_range_gib"] = [min(float(row["peak_vram_gib"]) for row in measured), peak]
        competing = observation.get("competing_gpu_jobs")
        if type(competing) is not int or competing < 0:
            result.update(reason="competing_work_unknown",
                          message=result["message"] + " Other GPU work is not attributed; no peak-to-free-memory comparison is admitted.")
        elif competing:
            result.update(reason="competing_work",
                          message=result["message"] + " Competing GPU work is active; review it and observe memory again.")
        elif measured and available is not None and peak + max(1.5, peak * .2) > available:
            result.update(assessment="limited", reason="qualified_requirement",
                          message="Observed free GPU memory is below the qualified exact-request peak plus reserve. Preview smaller work, or free memory and check the reviewed request again.")
    elif rows:
        result.update(reason="unqualified_evidence",
                      message="Retained memory records are unqualified, stale or do not match this exact request, runtime and hardware. Keep them as history; refresh qualification before using a peak limit.")
    if not bounded:
        result.update(reason="incomplete_review", message="Memory evidence exceeds the bounded review. No partial profile selection is used for admission.")
    if current["status"] == "limited":
        result.update(assessment="limited", reason="zero_free", message=current["message"])
    elif current["reason"] != "current":
        result.update(assessment="not_proven", reason=current["reason"],
                      message=result["message"] + " Refresh the memory observation before using a peak comparison.")
    if result["observed_peak_range_gib"] is not None:
        low, high = result["observed_peak_range_gib"]
        result["message"] += (f" Observed exact-request peaks: {low:.2f}–{high:.2f} GiB across "
            f"{result['qualified_samples']} qualified sample(s). This is retained history, not a predicted range or a guarantee; "
            f"when a comparison is applicable, admission uses the highest peak plus {max(1.5, high * .2):.2f} GiB reserve.")
    return result


def classify_memory_failure(error) -> bool:
    """Recognize explicit OOM error evidence without returning raw server text."""
    if isinstance(error, MemoryError):
        return True
    if isinstance(error, BaseException):
        values = (type(error).__name__, str(error))
    elif isinstance(error, str):
        values = (error,)
    elif isinstance(error, dict):
        keys = ("error", "error_id", "message", "type", "exception_type")
        values = [error.get(key) for key in keys]
        nested = error.get("error")
        if isinstance(nested, dict):
            values.extend(nested.get(key) for key in keys)
    else:
        return False
    return any(isinstance(value, str) and bool(_OOM.search(value[:4096])) for value in values)


def device_fingerprint(response):
    """Hash bounded identity fields, retaining no device name or raw response."""
    if normalize_swarm_memory(response)["status"] != "measured":
        return ""
    key, gpu = next(iter(response["gpus"].items()))
    name = gpu.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        return ""
    return _digest([str(key)[:128], name, gpu["total_memory"]])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def request_cost_key(prepared):
    """Exact computational inputs, except seed; never persist prompt/input bytes here.

    Prompt changes invalidate observations, as do companions, masks, reference
    bytes, all native options, finishing and runtime capabilities. These are
    observed costs of comparable requests, not a peak-memory admission profile.
    """
    if not isinstance(prepared, dict) or not isinstance(prepared.get("payload"), dict):
        return ""
    caps = prepared.get("capabilities", {})
    if not isinstance(caps, dict) or not caps.get("fingerprint"):
        return ""
    payload = {k: v for k, v in prepared["payload"].items()
               if k not in {"seed", "session_id", "extra_metadata"}}
    return _digest({"payload": payload, "binding": prepared.get("binding"),
                    "components": prepared.get("components"),
                    "output_plan": prepared.get("output_plan"),
                    "refinement": prepared.get("refinement"),
                    "runtime": caps["fingerprint"]})


def summarize_job_costs(prepared, observation, receipts, *, count=1):
    """Bounded same-request historical ranges; no extrapolation or fit promise."""
    if type(count) is not int or not 1 <= count <= 32:
        raise ValueError("Estimate one to 32 sequential requests.")
    key = request_cost_key(prepared)
    observation = observation if isinstance(observation, dict) else {}
    gpu = observation.get("gpu_fingerprint")
    matches = []
    for receipt in list(receipts)[:256]:
        if not isinstance(receipt, dict):
            continue
        sample = receipt.get("observed_cost", {})
        if (not key or not gpu or receipt.get("state") != "verified"
                or not isinstance(sample, dict) or sample.get("request_key") != key
                or sample.get("gpu_fingerprint") != gpu
                or request_cost_key(receipt.get("prepared")) != key):
            continue
        seconds, size = sample.get("elapsed_seconds"), sample.get("retained_bytes")
        if (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0
                or type(size) is not int or not 0 < size <= _MAX_BYTES):
            continue
        matches.append(sample)
    if not matches:
        return {"status": "unknown", "samples": 0,
                "message": "Time and retained-file size are unknown: no completed observation matches this request, runtime and GPU."}
    times = [row["elapsed_seconds"] for row in matches]
    sizes = [row["retained_bytes"] for row in matches]
    low, high = min(times) * count, max(times) * count
    size_low, size_high = min(sizes) * count, max(sizes) * count
    peaks = [row.get("sampled_device_used_gib") for row in matches]
    peaks = [p for p in peaks if type(p) in (int, float) and math.isfinite(p) and p >= 0]
    return {"status": "observed", "samples": len(matches), "count": count,
            "seconds_range": [low, high], "retained_bytes_range": [size_low, size_high],
            "sampled_device_used_gib": max(peaks) if peaks else None,
            "message": (f"Based on {len(matches)} matching completed run(s): about {low:.0f}–{high:.0f} seconds "
                        f"and {size_low / (1 << 20):.1f}–{size_high / (1 << 20):.1f} MiB retained for {count} sequential request(s). "
                        "Observed range, not a guarantee; excludes queue wait, runtime startup, server copies and temporary disk space. "
                        + (f"Highest sampled whole-device use: {max(peaks):.2f} GiB; sampling may miss peaks and includes other apps. " if peaks else "")
                        + "Exact request peak and memory fit remain unproven.")}


def summarize_group_costs(prepared_requests, observation, receipts):
    """Sum independently matched requests; never scale across changed settings."""
    if not isinstance(prepared_requests, (tuple, list)) or not 1 <= len(prepared_requests) <= 32:
        raise ValueError("Review one to 32 sequential requests.")
    cache, estimates = {}, []
    for prepared in prepared_requests:
        key = request_cost_key(prepared)
        if key not in cache:
            cache[key] = summarize_job_costs(prepared, observation, receipts)
        estimates.append(cache[key])
    matched = sum(row["status"] == "observed" for row in estimates)
    if matched != len(estimates):
        return {"status": "unknown", "matched": matched, "count": len(estimates),
                "message": f"Queue time and retained-file size are unknown: {matched} of {len(estimates)} candidates have matching completed observations. Jobs run one at a time."}
    times = [sum(row["seconds_range"][i] for row in estimates) for i in (0, 1)]
    sizes = [sum(row["retained_bytes_range"][i] for row in estimates) for i in (0, 1)]
    return {"status": "observed", "matched": matched, "count": len(estimates),
            "seconds_range": times, "retained_bytes_range": sizes,
            "message": f"Observed queue estimate: {times[0]:.0f}–{times[1]:.0f} seconds; {sizes[0] / (1 << 20):.1f}–{sizes[1] / (1 << 20):.1f} MiB retained, one job at a time. Each candidate matched prior observations. Excludes queue wait, startup, server copies and temporary storage; memory fit remains unproven."}
