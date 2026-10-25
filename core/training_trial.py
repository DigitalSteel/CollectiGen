"""Bounded measured trials of an exact admitted training run.

A trial uses the normal qualified process and deliberately requests its existing
cancel path. It never verifies or registers a partial checkpoint as a result.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re

from .training_metrics import TrainingMetricsSnapshot
from .training_process import _bytes, _read, _state, _write_new
from .training_source_io import _assert_unredirected


SHA = re.compile(r"[0-9a-f]{64}\Z")
INTENT_SUFFIX = ".trial-intent.json"
RECEIPT_NAME = "training-trial-receipt.json"
MAX_RECEIPT_BYTES = 16 * 1024
STATUSES = {"trial_stopped", "trial_cancelled", "trial_failed",
            "trial_interrupted", "trial_ended_before_limit"}
STOP_REASONS = {"step_limit", "elapsed_limit", "user_cancel", "process_failed",
                "process_interrupted", "ended_before_limit"}


class TrainingTrialError(ValueError):
    pass


@dataclass(frozen=True)
class TrainingTrialLimits:
    max_optimizer_steps: int = 10
    max_elapsed_seconds: int = 300
    warmup_steps: int = 1

    def validate(self, full_steps: int) -> None:
        if (type(full_steps) is not int or full_steps < 3
                or type(self.max_optimizer_steps) is not int
                or not 2 <= self.max_optimizer_steps <= min(10, full_steps - 1)
                or type(self.max_elapsed_seconds) is not int
                or not 60 <= self.max_elapsed_seconds <= 1200
                or type(self.warmup_steps) is not int
                or not 0 <= self.warmup_steps < self.max_optimizer_steps):
            raise TrainingTrialError("training_trial_limits_invalid")


@dataclass(frozen=True)
class TrainingTrialReceipt:
    status: str
    stop_reason: str
    process_fingerprint: str
    execution_contract_sha256: str
    process_journal_tip: str
    metrics_tip: str
    observed_step: int
    observed_records: int
    measured_steps: int
    measured_seconds: float | None
    seconds_per_step: float | None
    estimated_remaining_seconds: float | None
    failure_code: str
    limits: TrainingTrialLimits
    receipt_sha256: str = ""


def receipt_matches_process(receipt: TrainingTrialReceipt, process) -> bool:
    """A measurement receipt cannot override the recovered process outcome."""
    if (type(receipt) is not TrainingTrialReceipt or getattr(process, "tree_stopped", None) is not True
            or receipt.process_journal_tip != getattr(process, "journal_tip", None)):
        return False
    expected = {"trial_stopped": ("cancelled", {"step_limit", "elapsed_limit"}),
                "trial_cancelled": ("cancelled", {"user_cancel"}),
                "trial_failed": ("failed", {"process_failed"}),
                "trial_interrupted": ("interrupted", {"process_interrupted"}),
                "trial_ended_before_limit": ("exited_unverified", {"ended_before_limit"})}
    if receipt.status not in expected:
        return False
    status, reasons = expected[receipt.status]
    return (getattr(process, "status", None) == status and receipt.stop_reason in reasons
            and (status != "exited_unverified" or getattr(process, "exit_code", None) == 0))


def _intent_path(plan) -> Path:
    return Path(plan.root) / (plan.name + INTENT_SUFFIX)


def _receipt_path(plan) -> Path:
    return Path(plan.destination) / RECEIPT_NAME


def _intent(plan, contract, limits: TrainingTrialLimits) -> dict:
    limits.validate(contract["result_binding"]["expected_final_step"])
    return {"schema_version": 1, "kind": "training_trial_intent",
            "process_fingerprint": plan.fingerprint,
            "execution_contract_sha256": plan.preview_fingerprint,
            "limits": asdict(limits)}


def retain_intent(plan, contract, limits: TrainingTrialLimits) -> str:
    """Retain exact choice before process ownership; replays must match."""
    if type(limits) is not TrainingTrialLimits:
        raise TrainingTrialError("training_trial_limits_invalid")
    value = _intent(plan, contract, limits)
    raw = _bytes(value)
    path = _intent_path(plan)
    _assert_unredirected(path)
    if os.path.lexists(path):
        if _read(path, limit=MAX_RECEIPT_BYTES) != raw:
            raise TrainingTrialError("training_trial_intent_changed")
    else:
        _write_new(path, raw)
    return hashlib.sha256(raw).hexdigest()


def load_intent(plan, contract) -> TrainingTrialLimits | None:
    path = _intent_path(plan)
    _assert_unredirected(path)
    if not os.path.lexists(path):
        return None
    raw = _read(path, limit=MAX_RECEIPT_BYTES)
    try:
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != {"schema_version", "kind",
                "process_fingerprint", "execution_contract_sha256", "limits"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "training_trial_intent"
                or value["process_fingerprint"] != plan.fingerprint
                or value["execution_contract_sha256"] != plan.preview_fingerprint
                or type(value["limits"]) is not dict
                or set(value["limits"]) != set(TrainingTrialLimits.__dataclass_fields__)
                or _bytes(value) != raw):
            raise ValueError
        limits = TrainingTrialLimits(**value["limits"])
        limits.validate(contract["result_binding"]["expected_final_step"])
    except (TypeError, KeyError, ValueError) as exc:
        raise TrainingTrialError("training_trial_intent_invalid") from exc
    return limits


def _measurement(snapshot: TrainingMetricsSnapshot | None, full_steps: int,
                 warmup_steps: int) -> tuple[str, int, int, int, float | None, float | None, float | None]:
    if snapshot is None:
        return "", 0, 0, 0, None, None, None
    observed = snapshot.records
    measured_steps, measured_seconds = 0, 0.0
    prior = None
    for row in observed:
        if (prior is not None and prior.optimizer_step >= warmup_steps
                and row.optimizer_step > prior.optimizer_step
                and prior.elapsed_seconds is not None and row.elapsed_seconds is not None):
            measured_steps += row.optimizer_step - prior.optimizer_step
            measured_seconds += row.elapsed_seconds - prior.elapsed_seconds
        prior = row
    step = observed[-1].optimizer_step if observed else 0
    if not measured_steps or measured_seconds <= 0:
        return snapshot.tip, step, len(observed), 0, None, None, None
    pace = measured_seconds / measured_steps
    remaining = max(0, full_steps - step) * pace
    return (snapshot.tip, step, len(observed), measured_steps,
            measured_seconds, pace, remaining)


def retain_receipt(plan, contract, limits: TrainingTrialLimits, process, *,
                   status: str, stop_reason: str, metrics: TrainingMetricsSnapshot | None,
                   failure_code: str = "") -> TrainingTrialReceipt:
    """Seal a stopped trial; unknown/live process state cannot become a result."""
    expected = _intent(plan, contract, limits)
    if (load_intent(plan, contract) != limits
            or type(status) is not str or status not in STATUSES
            or type(stop_reason) is not str or stop_reason not in STOP_REASONS
            or getattr(process, "tree_stopped", None) is not True
            or type(getattr(process, "journal_tip", None)) is not str
            or SHA.fullmatch(process.journal_tip) is None
            or type(failure_code) is not str or len(failure_code) > 100):
        raise TrainingTrialError("training_trial_terminal_evidence_invalid")
    provisional = TrainingTrialReceipt(status, stop_reason, plan.fingerprint,
        plan.preview_fingerprint, process.journal_tip, "", 0, 0, 0,
        None, None, None, failure_code, limits)
    if not receipt_matches_process(provisional, process):
        raise TrainingTrialError("training_trial_process_status_mismatch")
    if metrics is not None and (type(metrics) is not TrainingMetricsSnapshot
                                or metrics.process_fingerprint != plan.fingerprint
                                or metrics.execution_contract_sha256 != plan.preview_fingerprint
                                or metrics.max_train_steps != contract["result_binding"]["expected_final_step"]):
        raise TrainingTrialError("training_trial_metrics_mismatch")
    tip, step, records, counted, seconds, pace, remaining = _measurement(
        metrics, contract["result_binding"]["expected_final_step"], limits.warmup_steps)
    value = {"schema_version": 1, "kind": "training_trial_receipt",
             **expected, "status": status, "stop_reason": stop_reason,
             "process_journal_tip": process.journal_tip, "metrics_tip": tip,
             "observed_step": step, "observed_records": records,
             "measured_steps": counted, "measured_seconds": seconds,
             "seconds_per_step": pace, "estimated_remaining_seconds": remaining,
             "failure_code": failure_code}
    # The intent kind must not collide with the receipt kind.
    value["kind"] = "training_trial_receipt"
    raw = _bytes(value)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise TrainingTrialError("training_trial_receipt_bound")
    path = _receipt_path(plan)
    _assert_unredirected(path)
    if os.path.lexists(path):
        if _read(path, limit=MAX_RECEIPT_BYTES) != raw:
            raise TrainingTrialError("training_trial_receipt_changed")
    else:
        _state(path.parent, directory=True)
        _write_new(path, raw)
    digest = hashlib.sha256(raw).hexdigest()
    return TrainingTrialReceipt(status, stop_reason, plan.fingerprint, plan.preview_fingerprint,
                                process.journal_tip, tip, step, records, counted,
                                seconds, pace, remaining, failure_code, limits, digest)


def load_receipt(plan, contract) -> TrainingTrialReceipt | None:
    """Read a retained receipt for display; process recovery remains separate."""
    path = _receipt_path(plan)
    _assert_unredirected(path)
    if not os.path.lexists(path):
        return None
    raw = _read(path, limit=MAX_RECEIPT_BYTES)
    try:
        value = json.loads(raw)
        limits = load_intent(plan, contract)
        if (type(value) is not dict or set(value) != {"schema_version", "kind",
                "process_fingerprint", "execution_contract_sha256", "limits", "status",
                "stop_reason", "process_journal_tip", "metrics_tip", "observed_step",
                "observed_records", "measured_steps", "measured_seconds",
                "seconds_per_step", "estimated_remaining_seconds", "failure_code"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "training_trial_receipt"
                or value["process_fingerprint"] != plan.fingerprint
                or value["execution_contract_sha256"] != plan.preview_fingerprint
                or limits is None or value["limits"] != asdict(limits)
                or value["status"] not in STATUSES or value["stop_reason"] not in STOP_REASONS
                or type(value["process_journal_tip"]) is not str
                or SHA.fullmatch(value["process_journal_tip"]) is None
                or value["metrics_tip"] != "" and (type(value["metrics_tip"]) is not str
                    or SHA.fullmatch(value["metrics_tip"]) is None)
                or type(value["observed_step"]) is not int
                or not 0 <= value["observed_step"] <= contract["result_binding"]["expected_final_step"]
                or type(value["observed_records"]) is not int or not 0 <= value["observed_records"] <= 10_000
                or value["metrics_tip"] == "" and (value["observed_records"] != 0
                    or value["observed_step"] != 0 or value["measured_steps"] != 0)
                or value["observed_records"] == 0 and value["metrics_tip"] != ""
                or type(value["measured_steps"]) is not int or value["measured_steps"] < 0
                or any(item is not None and (type(item) not in (int, float)
                    or not math.isfinite(item) or item < 0) for item in
                    (value["measured_seconds"], value["seconds_per_step"], value["estimated_remaining_seconds"]))
                or type(value["failure_code"]) is not str or len(value["failure_code"]) > 100
                or _bytes(value) != raw):
            raise ValueError
    except (TypeError, KeyError, ValueError) as exc:
        raise TrainingTrialError("training_trial_receipt_invalid") from exc
    return TrainingTrialReceipt(value["status"], value["stop_reason"], plan.fingerprint,
                                plan.preview_fingerprint, value["process_journal_tip"],
                                value["metrics_tip"], value["observed_step"],
                                value["observed_records"], value["measured_steps"],
                                value["measured_seconds"], value["seconds_per_step"],
                                value["estimated_remaining_seconds"], value["failure_code"],
                                limits, hashlib.sha256(raw).hexdigest())
