"""Durable, bounded observations of a training run; never result authority.

The qualified worker owns its latest progress record. This parent-side history
retains only records actually observed by the controller, including their exact
source digests. Missing optimizer steps remain missing observations.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from .training_progress import validate_progress
from .training_process import _read, _state
from .training_source_io import _assert_unredirected


NAME = "training-metrics.json"
KIND = "training_observed_metrics"
MAX_RECORDS = 10_000
MAX_BYTES = 4 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_FIELDS = {"previous_digest", "source_sha256", "revision", "state",
                  "optimizer_step", "epoch", "loss", "elapsed_seconds",
                  "checkpoint_sequence", "missed_steps_before",
                  "missed_revisions_before", "digest"}
_FILE_FIELDS = {"schema_version", "kind", "process_fingerprint",
                "execution_contract_sha256", "max_train_steps", "records", "tip"}
_ZERO = "0" * 64


class TrainingMetricsError(ValueError):
    """An observation history is unavailable, stale, full, or altered."""


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


@dataclass(frozen=True)
class TrainingMetric:
    revision: int
    state: str
    optimizer_step: int
    epoch: int
    loss: float | None
    elapsed_seconds: float | None
    checkpoint_sequence: int | None
    missed_steps_before: int
    missed_revisions_before: int
    source_sha256: str


@dataclass(frozen=True)
class TrainingMetricsSnapshot:
    process_fingerprint: str
    execution_contract_sha256: str
    max_train_steps: int
    records: tuple[TrainingMetric, ...]
    tip: str

    @property
    def observed_gaps(self) -> bool:
        return any(row.missed_steps_before or row.missed_revisions_before
                   for row in self.records)

    @property
    def final_progress_observed(self) -> bool:
        return bool(self.records and self.records[-1].state == "completed"
                    and self.records[-1].optimizer_step == self.max_train_steps)


class TrainingMetricsHistory:
    """Single-controller, exact-run history in a bounded atomic sidecar.

    ``root`` is the owned run destination, not the worker's ``work`` folder.
    Reopening verifies the entire canonical hash chain. This is an observation
    store, and its integrity does not substitute for a process/result receipt.
    """

    def __init__(self, root: str | Path, process_fingerprint: str,
                 execution_contract_sha256: str, max_steps: int):
        if (not isinstance(root, (str, Path)) or type(process_fingerprint) is not str
                or SHA.fullmatch(process_fingerprint) is None
                or type(execution_contract_sha256) is not str
                or SHA.fullmatch(execution_contract_sha256) is None
                or type(max_steps) is not int or not 1 <= max_steps <= 1_000_000_000):
            raise TrainingMetricsError("training_metrics_binding_invalid")
        self.path = Path(root).absolute() / NAME
        self.process_fingerprint = process_fingerprint
        self.execution_contract_sha256 = execution_contract_sha256
        self.max_steps = max_steps
        self._last_file_digest: str | None = None
        try:
            _state(self.path.parent, directory=True)
            _assert_unredirected(self.path)
            if self.path.exists():
                _, raw = self._read()
                self._last_file_digest = hashlib.sha256(raw).hexdigest()
            else:
                initial = self._empty()
                raw = _bytes(initial)
                self._write_new(raw)
                self._last_file_digest = hashlib.sha256(raw).hexdigest()
        except (OSError, RuntimeError, ValueError) as exc:
            raise TrainingMetricsError("training_metrics_unavailable_or_changed") from exc

    def _empty(self) -> dict:
        return {"schema_version": 1, "kind": KIND,
                "process_fingerprint": self.process_fingerprint,
                "execution_contract_sha256": self.execution_contract_sha256,
                "max_train_steps": self.max_steps, "records": [], "tip": _ZERO}

    def _write_new(self, raw: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(self.path, flags, 0o600), "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if _read(self.path, limit=MAX_BYTES) != raw:
            raise TrainingMetricsError("training_metrics_write_failed")

    def _read(self) -> tuple[dict, bytes]:
        raw = _read(self.path, limit=MAX_BYTES)
        try:
            value = json.loads(raw)
            if (_bytes(value) != raw or type(value) is not dict or set(value) != _FILE_FIELDS
                    or value["schema_version"] != 1 or type(value["schema_version"]) is not int
                    or value["kind"] != KIND
                    or value["process_fingerprint"] != self.process_fingerprint
                    or value["execution_contract_sha256"] != self.execution_contract_sha256
                    or value["max_train_steps"] != self.max_steps or type(value["max_train_steps"]) is not int
                    or type(value["records"]) is not list or len(value["records"]) > MAX_RECORDS):
                raise ValueError
            previous, step, revision, epoch, elapsed = _ZERO, 0, 0, 0, None
            previous_state, checkpoint_sequence = None, None
            for row in value["records"]:
                if type(row) is not dict or set(row) != _RECORD_FIELDS:
                    raise ValueError
                unsigned = {key: val for key, val in row.items() if key != "digest"}
                if (row["previous_digest"] != previous or type(row["digest"]) is not str
                        or row["digest"] != _digest(unsigned)
                        or type(row["source_sha256"]) is not str or SHA.fullmatch(row["source_sha256"]) is None
                        or type(row["revision"]) is not int or row["revision"] <= revision
                        or type(row["state"]) is not str
                        or row["state"] not in {"starting", "running", "checkpoint", "completed"}
                        or type(row["optimizer_step"]) is not int or not step <= row["optimizer_step"] <= self.max_steps
                        or type(row["epoch"]) is not int or row["epoch"] < epoch
                        or row["state"] == "starting" and row["optimizer_step"] != 0
                        or row["state"] == "starting" and revision != 0
                        or previous_state == "completed"
                        or row["state"] == "completed" and row["optimizer_step"] != self.max_steps
                        or row["loss"] is not None and (type(row["loss"]) not in (int, float)
                            or not math.isfinite(row["loss"]) or row["loss"] < 0)
                        or row["elapsed_seconds"] is not None and (type(row["elapsed_seconds"]) not in (int, float)
                            or not math.isfinite(row["elapsed_seconds"]) or row["elapsed_seconds"] < 0
                            or elapsed is not None and row["elapsed_seconds"] < elapsed)
                        or row["checkpoint_sequence"] is not None and (type(row["checkpoint_sequence"]) is not int
                            or not 0 <= row["checkpoint_sequence"] < 1000)
                        or checkpoint_sequence is not None and (row["checkpoint_sequence"] is None
                            or row["checkpoint_sequence"] < checkpoint_sequence)
                        or row["state"] in {"checkpoint", "completed"} and row["checkpoint_sequence"] is None
                        or type(row["missed_steps_before"]) is not int
                        or row["missed_steps_before"] != max(0, row["optimizer_step"] - step - 1)
                        or type(row["missed_revisions_before"]) is not int
                        or row["missed_revisions_before"] != row["revision"] - revision - 1):
                    raise ValueError
                previous, step, revision, epoch = row["digest"], row["optimizer_step"], row["revision"], row["epoch"]
                previous_state, checkpoint_sequence = row["state"], row["checkpoint_sequence"]
                if row["elapsed_seconds"] is not None:
                    elapsed = row["elapsed_seconds"]
            if value["tip"] != previous:
                raise ValueError
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise TrainingMetricsError("training_metrics_invalid_or_changed") from exc
        return value, raw

    def snapshot(self) -> TrainingMetricsSnapshot:
        try:
            value, raw = self._read()
            if self._last_file_digest is not None and hashlib.sha256(raw).hexdigest() != self._last_file_digest:
                raise TrainingMetricsError("training_metrics_changed_outside_owner")
        except (OSError, RuntimeError, ValueError) as exc:
            raise TrainingMetricsError("training_metrics_unavailable_or_changed") from exc
        records = tuple(TrainingMetric(**{key: row[key] for key in TrainingMetric.__dataclass_fields__})
                        for row in value["records"])
        return TrainingMetricsSnapshot(self.process_fingerprint, self.execution_contract_sha256,
                                       self.max_steps, records, value["tip"])

    def observe(self, validated_progress: dict, *, elapsed_seconds: float | None = None) -> TrainingMetricsSnapshot:
        try:
            value = validate_progress(validated_progress, self.process_fingerprint,
                                      self.execution_contract_sha256)
        except (ValueError, TypeError) as exc:
            raise TrainingMetricsError("training_metrics_source_invalid") from exc
        if value["max_train_steps"] != self.max_steps:
            raise TrainingMetricsError("training_metrics_source_mismatch")
        if elapsed_seconds is not None and (type(elapsed_seconds) not in (int, float)
                                            or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
            raise TrainingMetricsError("training_metrics_elapsed_invalid")
        current = self.snapshot()
        source_sha256 = _digest(value)
        if current.records and value["revision"] == current.records[-1].revision:
            if source_sha256 == current.records[-1].source_sha256:
                return current
            raise TrainingMetricsError("training_metrics_source_changed")
        if current.records and value["revision"] < current.records[-1].revision:
            raise TrainingMetricsError("training_metrics_source_stale")
        if len(current.records) >= MAX_RECORDS:
            raise TrainingMetricsError("training_metrics_full")
        if current.records and current.records[-1].state == "completed":
            raise TrainingMetricsError("training_metrics_already_completed")
        previous = current.records[-1] if current.records else None
        last_elapsed = next((row.elapsed_seconds for row in reversed(current.records)
                             if row.elapsed_seconds is not None), None)
        if (previous and (value["optimizer_step"] < previous.optimizer_step
                          or value["epoch"] < previous.epoch
                          or value["state"] == "starting"
                          or previous.checkpoint_sequence is not None
                          and (value["checkpoint_sequence"] is None
                               or value["checkpoint_sequence"] < previous.checkpoint_sequence)
                          or elapsed_seconds is not None and last_elapsed is not None
                          and elapsed_seconds < last_elapsed)):
            raise TrainingMetricsError("training_metrics_position_regressed")
        unsigned = {"previous_digest": current.tip, "source_sha256": source_sha256,
                    "revision": value["revision"], "state": value["state"],
                    "optimizer_step": value["optimizer_step"], "epoch": value["epoch"],
                    "loss": value["loss"], "elapsed_seconds": elapsed_seconds,
                    "checkpoint_sequence": value["checkpoint_sequence"],
                    "missed_steps_before": max(0, value["optimizer_step"] - (previous.optimizer_step if previous else 0) - 1),
                    "missed_revisions_before": value["revision"] - (previous.revision if previous else 0) - 1}
        row = {**unsigned, "digest": _digest(unsigned)}
        try:
            document, old_raw = self._read()
            if hashlib.sha256(old_raw).hexdigest() != self._last_file_digest:
                raise TrainingMetricsError("training_metrics_changed_outside_owner")
            document["records"].append(row)
            document["tip"] = row["digest"]
            raw = _bytes(document)
            if len(raw) > MAX_BYTES:
                raise TrainingMetricsError("training_metrics_full")
            temporary = self.path.with_name("." + self.path.name + "." + uuid4().hex + ".tmp")
            _assert_unredirected(temporary)
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                if _read(temporary, limit=MAX_BYTES) != raw or _read(self.path, limit=MAX_BYTES) != old_raw:
                    raise TrainingMetricsError("training_metrics_changed_outside_owner")
                _assert_unredirected(self.path)
                os.replace(temporary, self.path)
                if _read(self.path, limit=MAX_BYTES) != raw:
                    raise TrainingMetricsError("training_metrics_write_failed")
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._last_file_digest = hashlib.sha256(raw).hexdigest()
        except (OSError, RuntimeError, ValueError) as exc:
            raise TrainingMetricsError("training_metrics_write_failed") from exc
        return self.snapshot()
