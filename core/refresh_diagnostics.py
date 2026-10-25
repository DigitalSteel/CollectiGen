"""Bounded, content-free diagnostics for derived refresh work.

The trace stays in memory.  It deliberately accepts only fixed vocabulary so a
diagnostic receipt cannot accidentally retain image paths, Tag names, or other
user-provided content.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from re import fullmatch
from threading import RLock
from time import perf_counter
from typing import Iterator
from uuid import uuid4


_ACTIONS = frozenset({
    "refresh_request", "image_data_changed", "library_data_changed",
    "knowledge_changed", "tag_policy_changed", "visible_work_resume",
})
_CHANGE_CLASSES = frozenset({
    "unknown", "image_evidence", "library_membership", "tag_identity",
    "tag_policy", "relationship",
})
_EXPANSION_REASONS = frozenset({
    "none", "explicit_all", "incomplete_dependency_contract",
    "unsupported_dependency_kind", "missing_dependency_ids",
    "library_coverage_repair", "dependency_matches",
    "dependency_cache_unavailable",
})
_PHASES = frozenset({
    "scope", "cache_read", "derive", "review", "dependency", "workflow",
    "checkpoint",
})
_COUNTERS = frozenset({
    "derivation_reused", "derivation_computed", "previous_rows_loaded",
    "review_images", "dependency_images", "workflow_images", "checkpoint_commits",
    "cache_rows_written", "source_stat_probes", "metadata_reads", "metadata_writes",
    "failed_images", "skipped_images",
})
_END_STATES = frozenset({"unknown", "complete", "cancelled", "failed", "skipped"})
_SAFE_JOB_ID = (
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_MAX_SCOPE_SAMPLES = 8
_MAX_REQUEST_GROUPS = 32


def _allowed(value: object, values: frozenset[str]) -> str:
    """Return a fixed diagnostic value, never an arbitrary caller string."""
    normalized = str(value).strip().lower() if value is not None else "unknown"
    return normalized if normalized in values else "unknown"


def _count(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        return None
    return value


class RefreshTrace:
    """Thread-safe in-memory aggregate for one refresh request."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._created_at = perf_counter()
        self._token = str(uuid4())
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._request: dict[str, object] | None = None
        self._requests: dict[tuple[str, str, str], dict[str, int | str]] = {}
        self._dropped_request_count = 0
        self._job_id: str | None = None
        self._state: str | None = None
        self._counters: dict[str, int] = {}
        self._phases: dict[str, dict[str, float | int]] = {}
        self._scopes: list[dict[str, int | bool | None]] = []
        self._dropped_scope_samples = 0
        self._expansion_reasons: list[str] = []

    def add_request(
        self,
        *,
        action_code: object = "refresh_request",
        change_class: object = "unknown",
        requested_count: int | None,
        expansion_reason: object = "none",
    ) -> None:
        request = {
            "action_code": _allowed(action_code, _ACTIONS),
            "change_class": _allowed(change_class, _CHANGE_CLASSES),
            "requested_count": _count(requested_count),
            "expansion_reason": _allowed(expansion_reason, _EXPANSION_REASONS),
        }
        key = (
            str(request["action_code"]), str(request["change_class"]),
            str(request["expansion_reason"]),
        )
        with self._lock:
            self._request = request
            aggregate = self._requests.get(key)
            if aggregate is None:
                if len(self._requests) >= _MAX_REQUEST_GROUPS:
                    self._dropped_request_count += 1
                    return
                aggregate = {
                    "action_code": key[0], "change_class": key[1],
                    "expansion_reason": key[2], "request_count": 0,
                    "requested_item_count": 0, "all_scope_request_count": 0,
                }
                self._requests[key] = aggregate
            aggregate["request_count"] = int(aggregate["request_count"]) + 1
            if request["requested_count"] is None:
                aggregate["all_scope_request_count"] = int(aggregate["all_scope_request_count"]) + 1
            else:
                aggregate["requested_item_count"] = (
                    int(aggregate["requested_item_count"]) + int(request["requested_count"])
                )

    def bind_job(self, job_id: object) -> None:
        candidate = str(job_id).strip() if job_id is not None else ""
        with self._lock:
            self._job_id = candidate if fullmatch(_SAFE_JOB_ID, candidate) else "unknown"

    def start(self) -> None:
        with self._lock:
            if self._started_at is None:
                self._started_at = perf_counter()

    def expansion_reason(self, code: object) -> None:
        """Record one observed fixed-vocabulary expansion cause without a request."""
        reason = _allowed(code, _EXPANSION_REASONS)
        with self._lock:
            if reason not in self._expansion_reasons:
                self._expansion_reasons.append(reason)

    def count(self, name: object, amount: int = 1) -> None:
        if not isinstance(name, str) or name not in _COUNTERS or type(amount) is not int or amount < 0:
            return
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    @contextmanager
    def phase(self, name: object) -> Iterator[None]:
        phase_name = name if isinstance(name, str) and name in _PHASES else None
        started_at = perf_counter()
        try:
            yield
        finally:
            if phase_name is not None:
                elapsed = perf_counter() - started_at
                with self._lock:
                    aggregate = self._phases.setdefault(
                        phase_name, {"seconds": 0.0, "calls": 0},
                    )
                    aggregate["seconds"] = float(aggregate["seconds"]) + elapsed
                    aggregate["calls"] = int(aggregate["calls"]) + 1

    def scope(
        self, *, requested_count: int | None, final_count: int, broad: bool,
    ) -> None:
        final = _count(final_count)
        if final is None:
            return
        sample = {
            "requested_count": _count(requested_count),
            "final_count": final,
            "broad": bool(broad),
        }
        with self._lock:
            if len(self._scopes) < _MAX_SCOPE_SAMPLES:
                self._scopes.append(sample)
            else:
                self._dropped_scope_samples += 1

    def finish(self, state: object) -> None:
        with self._lock:
            if self._finished_at is None:
                self._finished_at = perf_counter()
                self._state = _allowed(state, _END_STATES)

    def snapshot(self) -> dict[str, object]:
        """Return an immutable JSON-safe copy of only measured aggregates."""
        with self._lock:
            result: dict[str, object] = {"token": self._token}
            if self._request is not None:
                result["request"] = dict(self._request)
            if self._requests or self._dropped_request_count:
                result["requests"] = [dict(value) for value in self._requests.values()]
                result["total_request_count"] = sum(
                    int(value["request_count"]) for value in self._requests.values()
                ) + self._dropped_request_count
                result["dropped_request_count"] = self._dropped_request_count
            if self._job_id is not None:
                result["job_id"] = self._job_id
            if self._started_at is not None:
                result["queue_seconds"] = self._started_at - self._created_at
            if self._finished_at is not None:
                result["state"] = self._state
                result["duration_seconds"] = self._finished_at - (
                    self._started_at if self._started_at is not None else self._created_at
                )
            if self._counters:
                result["counters"] = dict(self._counters)
            if self._phases:
                result["phases"] = {
                    name: {"seconds": float(value["seconds"]), "calls": int(value["calls"])}
                    for name, value in self._phases.items()
                }
            if self._scopes or self._dropped_scope_samples:
                result["scopes"] = list(self._scopes)
                result["dropped_scope_samples"] = self._dropped_scope_samples
            if self._expansion_reasons:
                result["expansion_reasons"] = list(self._expansion_reasons)
            return deepcopy(result)
