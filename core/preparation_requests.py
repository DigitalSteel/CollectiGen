from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4
from weakref import WeakMethod

from .background_jobs import BackgroundJobManager, JobProgress


def _snapshot(value: dict) -> str:
    # No live objects, workers or mutable settings survive in a queued request.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class PreparationRequest:
    """Accepted intent, not fresh authority to write when a queue slot opens.

    Snapshots contain serializable settings/options/history IDs and signatures;
    the admission validator must compare them with current canonical authority.
    It must never recapture/rebase stale history silently. No durable replay is
    implied: pending requests remain in this process only.
    """
    id: str
    paths: tuple[str, ...]
    settings_json: str
    options_json: str
    history_json: str
    authority_json: str
    resource: str
    allow_memory_pressure: bool = False

    @classmethod
    def capture(cls, paths, *, settings, options, history, authority,
                resource="gpu", allow_memory_pressure=False):
        scope = tuple(dict.fromkeys(str(Path(path)) for path in paths))
        if not scope:
            raise ValueError("Preparation requires an explicit image scope.")
        if resource not in {"gpu", "io"}:
            raise ValueError("Preparation must use the GPU or IO resource.")
        if "allow_memory_pressure" in options and bool(options["allow_memory_pressure"]) != bool(allow_memory_pressure):
            raise ValueError("The options and request must carry the same task-only memory consent.")
        if not authority:
            raise ValueError("Preparation requires captured authority signatures.")
        return cls(uuid4().hex, scope, _snapshot(settings), _snapshot(options),
                   _snapshot(history), _snapshot(authority), resource,
                   bool(allow_memory_pressure))

    def snapshot(self, name: str) -> dict:
        if name not in {"settings", "options", "history", "authority"}:
            raise ValueError("Unknown preparation snapshot.")
        return json.loads(getattr(self, name + "_json"))


@dataclass(frozen=True)
class PreparationDelivery:
    request_id: str
    state: str
    result: object = None
    detail: str = ""


class PreparationRequestCoordinator:
    """Serial physical work with independent logical requests and delivery.

    Uses the existing manager's exclusive queue, cancellation, progress and
    shutdown. Waiting requests consume no worker thread or AI lease. A worker
    factory is called only after admission validation, separately per request.
    Canonical source/history guards remain the runner's responsibility throughout
    execution; this preflight never replaces those guards.

    UI observers MUST provide a GUI-thread dispatcher and detach on close. Bound
    methods are weakly held; a dispatched notification checks its token again,
    so closing an editor cannot route an old result into its successor. Results
    remain available via take_result even if a surface has disappeared.
    """
    def __init__(self, jobs: BackgroundJobManager, *, ai_work_lease=None):
        self.jobs = jobs
        self.ai_work_lease = ai_work_lease
        self._lock = RLock()
        self._jobs = {}
        self._requests = {}
        self._retired_ids = set()
        self._deliveries = {}
        self._observers = {}

    def submit(self, request: PreparationRequest, *, validate: Callable,
               worker_factory: Callable, priority: int = 0) -> JobProgress:
        # Validate is mandatory: a queued request cannot acquire new authority.
        if not callable(validate) or not callable(worker_factory):
            raise TypeError("Preparation requires admission validation and a worker factory.")
        with self._lock:
            if request.id in self._jobs or request.id in self._retired_ids:
                raise ValueError("This preparation request was already submitted.")

            completed = []

            def work(job):
                if not job.checkpoint():
                    raise InterruptedError()
                reason = validate(request)
                if not isinstance(reason, str):
                    return PreparationDelivery(request.id, "held", detail=
                        "Preparation admission could not be verified; no work started.")
                if reason:
                    return PreparationDelivery(request.id, "held", detail=str(reason))
                if not job.checkpoint():
                    raise InterruptedError()
                leased = False
                try:
                    if request.resource == "gpu" and self.ai_work_lease is not None:
                        leased = self.ai_work_lease.begin(job.name)
                        if not leased:
                            return PreparationDelivery(request.id, "held", detail=
                                "The local AI work lease could not be verified; no model work started.")
                    worker = worker_factory(request)
                    result = worker(job)
                    delivery = PreparationDelivery(request.id, "cancelled" if job.cancel_requested
                                                   else "complete", result=result)
                    completed.append(delivery)
                    return delivery
                finally:
                    if leased:
                        self.ai_work_lease.finish()

            def finished(job):
                delivery = completed[0] if completed else job.result
                if not isinstance(delivery, PreparationDelivery):
                    delivery = PreparationDelivery(request.id, job.state, detail=job.error)
                if job.state == "cancelled":
                    delivery = PreparationDelivery(request.id, "cancelled",
                                                   result=delivery.result, detail=delivery.detail)
                # Isolate retained results from manager history/result clearing.
                delivery = deepcopy(delivery)
                with self._lock:
                    self._deliveries[request.id] = delivery
                    tokens = tuple(self._observers.get(request.id, {}))
                for token in tokens:
                    self._notify(request.id, token)

            job, _token = self.jobs.submit_observed(
                f"Preparing {len(request.paths):,} image(s)", work, finished,
                resource=request.resource, key=f"preparation-request:{request.id}",
                exclusive=True, priority=priority,
            )
            self._jobs[request.id] = job
            self._requests[request.id] = request
            return job

    def observe(self, request_id, callback, *, dispatch):
        if not callable(dispatch):
            raise TypeError("A preparation result observer requires an explicit dispatcher.")
        token = uuid4().hex
        reference = WeakMethod(callback) if getattr(callback, "__func__", None) is not None else callback
        with self._lock:
            if request_id not in self._jobs:
                raise KeyError(request_id)
            self._observers.setdefault(request_id, {})[token] = (reference, dispatch)
            ready = request_id in self._deliveries
        if ready:
            self._notify(request_id, token)
        return token

    def _notify(self, request_id, token):
        with self._lock:
            observer = self._observers.get(request_id, {}).get(token)
        if observer is None:
            return
        _reference, dispatch = observer
        def deliver():
            with self._lock:
                observer = self._observers.get(request_id, {}).pop(token, None)
                value = self._deliveries.get(request_id)
            if observer is None or value is None:
                return
            reference, _dispatch = observer
            callback = reference() if isinstance(reference, WeakMethod) else reference
            if callback is not None:
                callback(deepcopy(value))
        try:
            dispatch(deliver)
        except Exception:
            # A closed dispatcher cannot remove the request's recoverable result.
            self.detach(request_id, token)

    def detach(self, request_id, token):
        with self._lock:
            return self._observers.get(request_id, {}).pop(token, None) is not None

    def cancel(self, request_id):
        with self._lock:
            job = self._jobs[request_id]
        self.jobs.cancel(job.id)

    def request(self, request_id):
        """Resolve original scope/intent without reading mutable page fields."""
        with self._lock:
            return self._requests[request_id]

    def progress(self, request_id):
        with self._lock:
            return self._jobs[request_id]

    def take_result(self, request_id):
        """Consume terminal delivery explicitly; never discard on editor close."""
        with self._lock:
            value = self._deliveries.pop(request_id, None)
            if value is not None:
                self._observers.pop(request_id, None)
            return value

    def forget(self, request_id):
        """Release consumed terminal state; never abandon pending work/results."""
        with self._lock:
            job=self._jobs.get(request_id)
            if job is None:return False
            if job.state in {"queued","running","paused"} or request_id in self._deliveries:
                return False
            self._jobs.pop(request_id,None)
            self._requests.pop(request_id,None)
            self._observers.pop(request_id,None)
            self._retired_ids.add(request_id)
            return True
