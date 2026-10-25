"""Single-owner fake lifecycle worker; Qt never executes controller operations.

Default disabled. Trusted injected fixture factory only. No native backend,
Qt object, process control, blocking UI join or automatic dispatch retry.
"""
from dataclasses import dataclass, replace, field
from datetime import datetime, timedelta, timezone
from queue import Queue, Empty, Full
from threading import Thread, Event, Lock, get_ident
from uuid import uuid4
from suite_core.manager.lifecycle_controller import (LifecycleController, CardBinding, CleanupEvidence,
    FixtureOffer, OwnedProcess, State, OWNER)
from suite_core.manager.ollama_inventory import OllamaScope, DiscoveryReport, ReadOnlyOllamaClient


@dataclass(frozen=True)
class PreparedScope:
    binding: CardBinding
    nonce: object
    fingerprint: str
    verified_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class CardResult:
    binding: CardBinding
    state: str
    reason: str
    prepared: PreparedScope | None
    observed_at: datetime | None


@dataclass(frozen=True)
class WorkerSnapshot:
    state: State
    occupied: bool
    reason: str
    offer: FixtureOffer | None
    owned: OwnedProcess | None
    cleanup: CleanupEvidence | None
    observed_at: datetime | None
    observation_serial: int
    exit_code: int | None
    cards: tuple
    inventory: DiscoveryReport | None = None
    module: object | None = None


@dataclass(frozen=True)
class WorkTicket:
    bridge_id: str
    request_id: int
    generation: int
    operation: str
    binding: CardBinding | None = None
    prepared: PreparedScope | None = None
    expected_session: tuple | None = None
    inventory_previous: DiscoveryReport | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WorkerResult:
    ticket: WorkTicket
    sequence: int
    finished_at: datetime
    snapshot: WorkerSnapshot
    observation_accepted: bool
    cancelled: bool = False
    error: str | None = None
    metadata_completion: object = field(default=None, repr=False)


@dataclass(frozen=True)
class LifecycleAdmission:
    """Content-free owner admission; opaque identities must never be exported."""
    bridge_id: str
    request_id: int
    generation: int
    operation: str
    expected_session: tuple
    observation_serial: int
    terminal: bool
    succeeded: bool


class WallClock:
    def now(self): return datetime.now(timezone.utc)


def idle_snapshot(reason="No async provider; real dispatch disabled."):
    return WorkerSnapshot(State.UNKNOWN, False, reason, None, None, None, None, 0, None, ())


class AsyncLifecycleBridge:
    """UI facade caches only immutable results; one thread owns the controller.

    Caller transfers fixture-controller ownership through a factory, invoked on
    the worker. No controller reference or Qt callback crosses into the UI.
    Cancellation revokes pending intent, never kills a worker/owned process.
    Shutdown is irreversible and requires an actual stopped thread plus final
    acknowledgement. Cancel Close can keep an inert stopped facade visible.
    """
    def __init__(self, *, fixture_mode=False, controller_factory=None, clock=None,
                 ollama_scope=None, ollama_factory=None, admission_observer=None, metadata_binding=None, disposable_module_binding=None):
        from .metadata_binding import ManagerMetadataBinding
        if metadata_binding is not None and (type(metadata_binding) is not ManagerMetadataBinding
                or ollama_scope is not None or ollama_factory is not None or fixture_mode
                or disposable_module_binding is not None):
            raise ValueError('exclusive_metadata_binding_required')
        self._metadata_binding = metadata_binding
        self._metadata_intent = None
        self._metadata_summary = None
        self._admission_observer = admission_observer
        self._module_binding = disposable_module_binding
        self._module_enabled = disposable_module_binding is not None
        self._module_snapshot = None
        self._module_held = False
        if self._module_enabled:
            if fixture_mode or controller_factory is not None or ollama_scope is not None or ollama_factory is not None:
                raise ValueError('exclusive_disposable_worker_required')
            controller_factory = LifecycleController.disposable_factory(disposable_module_binding)
        self.bridge_id = uuid4().hex
        if (ollama_scope is None) != (ollama_factory is None) or (ollama_scope is not None
            and (type(ollama_scope) is not OllamaScope or not callable(ollama_factory))):
            raise ValueError("explicit_ollama_scope_factory_required")
        self._fixture_enabled = fixture_mode is True and callable(controller_factory)
        self._ollama_scope = metadata_binding.scope if metadata_binding is not None else ollama_scope
        self._ollama_factory = ollama_factory
        self._ollama_report = None; self.ollama_error = None
        self.enabled = self._fixture_enabled or ollama_factory is not None or metadata_binding is not None or self._module_enabled
        self.generation = 0
        self.bindings = ()
        self._clock = clock if clock is not None else WallClock()
        self._commands = Queue(maxsize=32)
        self._results = Queue(maxsize=64)
        self._stop = Event(); self._overflow = Event()
        self._owner_exited = Event(); self._admission_lock = Lock()
        self._pending = {}; self._next_id = 0; self._last_sequence = 0
        self._last_observation_serial = 0; self._init_pending = self.enabled
        self._terminal_authority = None
        self._snapshot = idle_snapshot(); self._cards = ()
        self._cleanup = None; self._potential_dispatch = False
        self._faulted = False; self._final_ack = not self.enabled
        self.stopping = False; self.closed = False
        self._thread = None
        self._init_ticket = WorkTicket(self.bridge_id, 0, 0, "init")
        self._stop_ticket = WorkTicket(self.bridge_id, -1, 0, "quiescent")
        self._metadata_worker_factory = LifecycleController
        if metadata_binding is not None:
            self._init_pending = False  # No worker or config/endpoint read before explicit Refresh.
        if self.enabled and metadata_binding is None:
            if not self._fixture_enabled and not self._module_enabled: controller_factory = LifecycleController
            self._thread = Thread(target=self._worker, args=(controller_factory,),
                name="ManagerModuleOwner" if self._module_enabled else "ManagerReadOnlyOwner", daemon=not self._module_enabled)
            self._thread.start()

    @property
    def fixture_enabled(self): return self._fixture_enabled
    @property
    def ollama_scope(self): return self._ollama_scope
    @property
    def ollama_report(self): return self._ollama_report
    @property
    def state(self): return self._snapshot.state
    @property
    def occupied(self): return self._snapshot.occupied or self._potential_dispatch or (self._faulted and self._fixture_enabled) or (self._module_enabled and (self._module_held or self._init_pending))
    @property
    def reason(self): return self._snapshot.reason
    @property
    def owned(self): return self._snapshot.owned
    @property
    def session_offer(self): return self._snapshot.offer
    @property
    def session_key(self):
        if self._module_enabled:
            identity = self._module_snapshot.owned if self._module_snapshot is not None else None
            return None if identity is None else (identity.session.value, identity.plan_fingerprint)
        for ticket, cancel in self._pending.values():
            if ticket.operation == "dispatch" and ticket.prepared is not None:
                return (ticket.prepared.nonce.value, ticket.prepared.fingerprint)
        if self.session_offer is not None:
            return (self.session_offer.permit.nonce.value, self.session_offer.plan.fingerprint)
        return None
    @property
    def observed_at(self): return self._snapshot.observed_at
    @property
    def cleanup_evidence(self): return self._cleanup
    @property
    def cleanup_held(self): return self._cleanup is not None or (self._module_enabled and (self._module_held or self._module_snapshot is not None and (self._module_snapshot.pending_transaction or self._module_snapshot.ambiguous)))
    @property
    def worker_pending(self):
        return self._init_pending or bool(self._pending) or (self.stopping and not self.quiescent)
    @property
    def quiescent(self):
        return self._final_ack and not self._pending and (self._thread is None or not self._thread.is_alive())

    @property
    def ollama_pending(self):
        return any(ticket.operation == "ollama_refresh" for ticket, cancel in self._pending.values())

    @property
    def can_refresh_ollama(self):
        return (self.ollama_scope is not None and self.enabled and not self.worker_pending
            and not self.stopping and not self.closed and not self._faulted
            and not self._owner_exited.is_set())

    @property
    def ollama_recovery_hint(self):
        if self.ollama_scope is None: return "Ollama connection has not been supplied; inventory is unknown."
        if self._faulted or self.stopping or self.closed or self._owner_exited.is_set():
            if self._fixture_enabled and self.occupied:
                return "Inventory refresh is unavailable while the existing session remains unresolved. Review its status before closing Manager."
            return "Inventory worker is unavailable. Reopen Manager to refresh the selected connection after resolving its configuration."
        if self.ollama_pending: return "Refresh pending. Cancel refresh discards this attempt."
        if self.worker_pending: return "Another check is finishing; refresh will be available afterwards."
        if self.ollama_error is not None:
            return "Refresh failed. Choose Refresh Ollama inventory to retry; retained observations keep their original dates."
        if self.ollama_report is not None and any(row.failure_code is not None for row in self.ollama_report.observations):
            return "Some metadata could not be read. Choose Refresh Ollama inventory to retry; retained observations keep their original dates."
        return ""

    def refresh_ollama(self):
        if not self.can_refresh_ollama: return False
        return self._submit("ollama_refresh") is not None

    def cancel_ollama_refresh(self):
        return any(self.cancel(ticket.request_id) for ticket, cancel in tuple(self._pending.values())
            if ticket.operation == "ollama_refresh")

    def _metadata_current_unlocked(self):
        intent = self._metadata_intent
        if (intent is None or intent.ticket.generation != self.generation or self.stopping or self.closed
                or self._pending.get(intent.ticket.request_id, (None, None))[0] != intent.ticket):
            return None
        return intent

    def _metadata_current(self):
        with self._admission_lock:
            return self._metadata_current_unlocked()

    def _binding(self, entry):
        package = entry.candidate_package
        return CardBinding(entry.component_id, getattr(package, "software_version", None),
            getattr(package, "archive_sha256", None), getattr(package, "source_plan_sha256", None),
            entry.source_ref, entry.observed_at, self.generation)

    def binding_for(self, entry):
        binding = self._binding(entry)
        return binding if binding in self.bindings else None

    def replace_catalog(self, entries):
        self.generation += 1
        self.bindings = tuple(self._binding(entry) for entry in entries)
        self._cards = ()
        for ticket, cancel in tuple(self._pending.values()):
            if ticket.operation in ("dispatch", "poll", "ollama_refresh") and ticket.generation < self.generation:
                cancel.set()
        if self._metadata_binding is not None:
            return None  # Metadata-only host has no lifecycle/catalog worker work.
        return self._submit("catalog", entries=tuple(entries))

    def _submit(self, operation, *, entries=(), binding=None, prepared=None):
        # Exit seals admission atomically before retiring the remaining queue.
        with self._admission_lock:
            return self._submit_live(operation, entries=entries, binding=binding, prepared=prepared)

    def _submit_live(self, operation, *, entries=(), binding=None, prepared=None):
        if self._metadata_binding is not None and operation != 'ollama_refresh':
            return None
        if not self.enabled or self.stopping or self.closed or self._faulted or self._owner_exited.is_set():
            return None
        if len(self._pending) >= 32:
            self._faulted = True
            self._snapshot = replace(self._snapshot, reason="Async request capacity held; no retry or inferred exit.")
            return None
        self._next_id += 1
        expected = (prepared.nonce.value, prepared.fingerprint) if prepared is not None else self.session_key
        # Only facade-admitted immutable evidence is prior authority for a read.
        previous = self.ollama_report if operation == "ollama_refresh" else None
        ticket = WorkTicket(self.bridge_id, self._next_id, self.generation, operation, binding, prepared, expected, previous)
        cancel = Event()
        if operation == 'ollama_refresh' and self._metadata_binding is not None:
            self._metadata_intent = self._metadata_binding.pending(ticket)
        self._pending[ticket.request_id] = (ticket, cancel)
        try: self._commands.put_nowait((ticket, cancel, entries))
        except Full:
            self._pending.pop(ticket.request_id)
            self._faulted = True
            self._snapshot = replace(self._snapshot, reason="Async request queue held; dispatch was not enqueued.")
            return None
        if operation == 'ollama_refresh' and self._metadata_binding is not None and self._thread is None:
            self._init_pending = True
            try:
                self._thread = Thread(target=self._worker, args=(self._metadata_worker_factory,),
                    name='ManagerReadOnlyOwner', daemon=True)
                self._thread.start()
            except Exception:
                cancel.set(); self._pending.pop(ticket.request_id, None)
                self._commands.get_nowait()  # Metadata-only: this is its sole queued intent.
                self._metadata_intent = None; self._thread = None
                self._init_pending = False; self._faulted = True; self._final_ack = True
                self._owner_exited.set(); self.ollama_error = 'metadata_worker_start_failed'
                return None
        return ticket.request_id

    def cancel(self, request_id):
        row = self._pending.get(request_id)
        if row is None: return False
        row[1].set()
        self._cards = ()
        return True

    def dispatch(self, binding, now):
        if not self._fixture_enabled: return False
        projection = self.projection(binding, now)
        if not projection["launch_enabled"]: return False
        row = next(item for item in self._cards if item.binding == binding)
        request_id = self._submit("dispatch", binding=binding, prepared=row.prepared)
        if request_id is None: return False
        self._potential_dispatch = True # held before worker begins; repeated clicks denied
        self._snapshot = replace(self._snapshot, reason="Fake dispatch queued; owned outcome pending.")
        return True

    def poll(self, now):
        self.drain(now)
        if self._module_enabled:
            if (not self.worker_pending and not self.stopping and self._module_snapshot is not None
                and self._module_snapshot.child_active):
                self.request_module('poll')
            return
        if not self.worker_pending and not self.stopping and self._snapshot.offer is not None:
            self._submit("poll")

    def projection(self, binding, now):
        row = next((item for item in self._cards if item.binding == binding), None)
        current = binding in self.bindings and row is not None
        fresh = current and (row.observed_at is None or timedelta(0) <= now-row.observed_at <= timedelta(seconds=30))
        prepared = row.prepared if current else None
        ready = (prepared is not None and timedelta(0) <= now-prepared.verified_at <= timedelta(seconds=30)
            and now < prepared.expires_at and not self.worker_pending and not self.stopping
            and not self.closed and not self.occupied and not self.cleanup_held)
        same_session = self.session_offer is not None and self.session_offer.binding == binding
        reason = row.reason if current else self.reason
        if self.worker_pending: reason = "Lifecycle check pending on the fake worker; UI remains available."
        elif current and not fresh: reason = "Worker observation stale; readiness unknown."
        return {"state": row.state if fresh else "unknown", "reason": reason,
            "launch_enabled": bool(ready), "ui_ready": "unknown", "open_supported": False,
            "installed": False, "simulation_only": self._fixture_enabled,
            "cleanup_held": self.cleanup_held if same_session else False,
            "cleanup_session": self._cleanup.identity.session.value if same_session and self.cleanup_held else None,
            "owned_pid": self.owned.pid if same_session and fresh and self.owned else None,
            "exit_code": self._snapshot.exit_code if same_session and fresh else None}

    def request_shutdown(self):
        if self.stopping: return
        self.stopping = True; self._cards = ()
        for ticket, cancel in tuple(self._pending.values()): cancel.set()
        if self._metadata_binding is not None and self._thread is None:
            self._final_ack = True; self._init_pending = False; self._owner_exited.set()
        self._stop.set() # worker returns after in-flight call and drains cancelled intents

    def finish_ui_closed(self):
        if not self.quiescent: return False
        self.closed = True; self._cards = ()
        return True

    def drain(self, now):
        changed = False
        if self._overflow.is_set():
            self._faulted = True
            self._snapshot = replace(self._snapshot, reason="Worker output overflow held; ownership not inferred.")
        for _ in range(64):
            try: result = self._results.get_nowait()
            except Empty: break
            changed = self.accept_result(result, now) or changed
        return changed

    def accept_result(self, result, now):
        """Association/ordering is separate from positive observation authority."""
        if self._module_enabled:
            return self._accept_module_result(result, now)
        if self.closed or type(result) is not WorkerResult or type(result.ticket) is not WorkTicket:
            return False
        ticket = result.ticket
        if type(ticket.request_id) is not int or type(ticket.generation) is not int: return False
        pending = self._pending.get(ticket.request_id)
        locally_cancelled = pending is not None and pending[1].is_set()
        expected = (self._init_ticket if ticket.operation == "init" else
            self._stop_ticket if ticket.operation == "quiescent" else
            self._pending.get(ticket.request_id, (None, None))[0])
        if (ticket != expected or ticket.bridge_id != self.bridge_id or type(result.sequence) is not int
            or result.sequence <= self._last_sequence or type(result.snapshot) is not WorkerSnapshot
            or type(result.cancelled) is not bool or type(result.observation_accepted) is not bool):
            return False
        snapshot = result.snapshot
        if snapshot.inventory is not None and (self.ollama_scope is None
            or not self.ollama_scope.accepts(snapshot.inventory)): return False
        if (type(snapshot.state) is not State or type(snapshot.occupied) is not bool
            or type(snapshot.cards) is not tuple or any(type(row) is not CardResult for row in snapshot.cards)
            or type(snapshot.observation_serial) is not int or snapshot.observation_serial < 0):
            return False
        offer, identity, cleanup = snapshot.offer, snapshot.owned, snapshot.cleanup
        if offer is not None and type(offer) is not FixtureOffer: return False
        if offer is not None:
            key = (offer.permit.nonce.value, offer.plan.fingerprint)
            if ticket.operation in ("dispatch", "poll") and key != ticket.expected_session: return False
            if ticket.operation == "dispatch" and offer.binding != ticket.binding: return False
            if snapshot.state in (State.EXITED, State.FAILED):
                if snapshot.occupied or type(snapshot.exit_code) is not int or identity is None: return False
                if snapshot.state is State.EXITED and snapshot.exit_code != 0: return False
            elif snapshot.state is not State.UNKNOWN and (not snapshot.occupied or snapshot.exit_code is not None): return False
        if any(type(row.binding) is not CardBinding or row.binding.generation != ticket.generation for row in snapshot.cards): return False
        if identity is not None:
            if (type(identity) is not OwnedProcess or offer is None or identity.owner != OWNER
                or identity.session != offer.permit.nonce or identity.plan_fingerprint != offer.plan.fingerprint
                or type(identity.pid) is not int or identity.pid <= 0): return False
            try:
                if not offer.permit.approved_at <= identity.created_at <= result.finished_at: return False
            except (TypeError, ValueError): return False
            if self.owned is not None and self.session_offer is not None and offer.permit.nonce == self.session_offer.permit.nonce and identity != self.owned:
                return False
        if cleanup is not None and (type(cleanup) is not CleanupEvidence or cleanup.identity != identity): return False
        if self._cleanup is not None and identity is not None and identity != self._cleanup.identity:
            return False
        try:
            fresh = (type(result.finished_at) is datetime and result.finished_at.tzinfo is not None
                and timedelta(0) <= now-result.finished_at <= timedelta(seconds=30))
            observed_fresh = snapshot.observed_at is not None and timedelta(0) <= now-snapshot.observed_at <= timedelta(seconds=30)
        except (TypeError, ValueError): return False
        metadata_admitted = True
        if self._metadata_binding is not None and ticket.operation == 'ollama_refresh':
            metadata_admitted = False
            if (fresh and not result.cancelled and not locally_cancelled
                    and ticket.generation == self.generation and result.error is None):
                try:
                    with self._admission_lock:
                        report, metadata_summary = self._metadata_binding.consume(result.metadata_completion,
                            current_pending=self._metadata_current_unlocked, cancelled=lambda: pending[1].is_set())
                        if snapshot.inventory != report:
                            raise ValueError('metadata_result_report_mismatch')
                        self._metadata_summary = metadata_summary
                        metadata_admitted = True
                except Exception:
                    self.ollama_error = 'metadata_completion_refused'
        self._last_sequence = result.sequence
        self._pending.pop(ticket.request_id, None)
        if self._metadata_binding is not None and ticket.operation == 'ollama_refresh':
            self._metadata_intent = None
        if ticket.operation == "init": self._init_pending = False
        if ticket.operation == "quiescent": self._final_ack = True
        if ticket.operation == "ollama_refresh":
            if (fresh and not result.cancelled and not locally_cancelled
                and ticket.generation == self.generation and result.error is None
                and snapshot.inventory is not None and metadata_admitted):
                self._ollama_report = snapshot.inventory
                self.ollama_error = None
            elif result.error is not None:
                self.ollama_error = result.error
        elif result.error is not None and self.ollama_scope is not None:
            self.ollama_error = result.error
        # Discard rejected candidate evidence from the retained facade snapshot.
        # Ordering/acknowledgements can never make it previous-report authority.
        snapshot = replace(snapshot, inventory=self.ollama_report)
        if cleanup is not None: self._cleanup = cleanup # negative fact retained even for old/cancelled view
        positive = (result.observation_accepted and fresh and observed_fresh and not result.cancelled and not locally_cancelled
            and ticket.generation == self.generation and snapshot.observation_serial > self._last_observation_serial)
        if positive:
            self._last_observation_serial = snapshot.observation_serial
            self._terminal_authority = ((snapshot.observation_serial, identity, snapshot.state,
                snapshot.exit_code, snapshot.observed_at) if snapshot.state in (State.EXITED, State.FAILED) else None)
            if self._cleanup is not None and cleanup is None and identity == self._cleanup.identity:
                self._cleanup = None
        terminal = snapshot.state in (State.EXITED, State.FAILED)
        terminal_admitted = self._terminal_authority == (snapshot.observation_serial, identity,
            snapshot.state, snapshot.exit_code, snapshot.observed_at)
        if terminal and not terminal_admitted:
            # Catalog/final acknowledgements may echo rejected worker state.
            # Receipt ordering is not terminal-completion authority.
            self._snapshot = replace(snapshot, state=State.UNKNOWN, occupied=True,
                exit_code=None, cards=(), reason="Terminal observation not admitted; exact owned session retained.")
        elif not fresh or (result.observation_accepted and not observed_fresh):
            self._snapshot = replace(snapshot, state=State.UNKNOWN,
                occupied=self.occupied or offer is not None, reason="Async observation stale/future; retained ownership held.")
        else:
            self._snapshot = snapshot
        if ticket.operation == "dispatch": self._potential_dispatch = False
        if result.error is not None and offer is None and ticket.operation != "ollama_refresh": self._faulted = True
        if ticket.generation == self.generation and not result.cancelled and not locally_cancelled and fresh and (not terminal or terminal_admitted):
            self._cards = snapshot.cards
        # Use the executed admission decision, never consumption or retained view.
        if (positive and ticket.operation in ("dispatch", "poll") and offer is not None
            and identity is not None):
            signal = LifecycleAdmission(self.bridge_id, ticket.request_id, ticket.generation,
                ticket.operation, ticket.expected_session, snapshot.observation_serial,
                terminal and terminal_admitted,
                terminal and terminal_admitted and snapshot.state is State.EXITED
                and result.error is None and self._cleanup is None and not snapshot.occupied)
            if self._admission_observer is not None:
                try:
                    self._admission_observer(signal)
                except Exception:
                    pass # Diagnostic failure cannot change canonical admission.
        return True

    def _worker(self, factory):
        if self._module_enabled:
            return self._module_worker(factory)
        controller = None; sequence = 0; current_ticket = None; init_published = False
        metadata_completion = None
        inventory_client = None; inventory_report = None
        last_snapshot = replace(idle_snapshot("Worker outcome unknown."), occupied=self._fixture_enabled)
        def capture(cards=True):
            if controller is None: return replace(idle_snapshot("Worker initialization held."), occupied=self._fixture_enabled)
            rows = []
            if cards:
                for binding in controller.bindings:
                    projection = controller.projection(binding, self._clock.now())
                    prepared = None
                    if projection["launch_enabled"]:
                        offer = controller._offer(binding, self._clock.now())
                        prepared = PreparedScope(binding, offer.permit.nonce, offer.plan.fingerprint,
                            offer.verified_at, offer.permit.expires_at)
                    rows.append(CardResult(binding, projection["state"], projection["reason"], prepared, controller.observed_at))
            return WorkerSnapshot(controller.state, controller.occupied, controller.reason, controller.session_offer,
                controller.owned, controller.cleanup_evidence, controller.observed_at,
                controller.observation_serial, controller.exit_code, tuple(rows), inventory_report)
        def publish(ticket, snapshot, accepted=False, cancelled=False, error=None):
            nonlocal sequence, current_ticket, init_published, last_snapshot
            sequence += 1
            last_snapshot = snapshot
            if ticket == current_ticket: current_ticket = None
            if ticket.operation == "init": init_published = True
            result = WorkerResult(ticket, sequence, self._clock.now(), snapshot, accepted, cancelled, error,
                metadata_completion if ticket.operation == 'ollama_refresh' else None)
            try: self._results.put_nowait(result)
            except Full: self._overflow.set()
        def safe_capture():
            try: return capture(False)
            except Exception:
                return replace(last_snapshot, state=State.UNKNOWN, occupied=self._fixture_enabled, exit_code=None,
                    cards=(), reason="Worker capture failed; ownership retained.")
        failure_text = None
        try:
            candidate = factory()
            if type(candidate) is not LifecycleController or (self._fixture_enabled and not candidate.fixture_enabled):
                raise ValueError("explicit_fake_controller_required")
            controller = candidate
            if self._ollama_factory is not None:
                inventory_client = self._ollama_factory()
                if not self.ollama_scope.client_matches(inventory_client):
                    raise ValueError("ollama_client_scope_mismatch")
            publish(self._init_ticket, capture(False))
            while True:
                if self._stop.is_set() and self._commands.empty(): break
                try: ticket, cancel, entries = self._commands.get(timeout=0.05)
                except Empty: continue
                current_ticket = ticket
                inventory_report = None # operation output only, never a worker evidence cache
                metadata_completion = None
                if cancel.is_set() or self._stop.is_set():
                    publish(ticket, capture(False), cancelled=True)
                    continue
                at = self._clock.now(); prior_cleanup = controller.cleanup_evidence
                prior_serial = controller.observation_serial
                error = None
                try:
                    if ticket.operation == "catalog":
                        controller.generation = ticket.generation-1
                        controller.replace_catalog(entries)
                    elif ticket.operation == "dispatch":
                        offer = controller._offer(ticket.binding, at)
                        if cancel.is_set() or self._stop.is_set():
                            publish(ticket, capture(False), cancelled=True)
                            continue
                        if ticket.prepared is None or (offer.permit.nonce, offer.plan.fingerprint) != (ticket.prepared.nonce, ticket.prepared.fingerprint):
                            raise ValueError("prepared_scope_changed_before_dispatch")
                        provider = controller.provider
                        controller.provider = lambda binding, now: offer if binding == ticket.binding else provider(binding, now)
                        try: controller.dispatch(ticket.binding, at)
                        finally: controller.provider = provider
                    elif ticket.operation == "poll": controller.poll(at)
                    elif ticket.operation == "ollama_refresh":
                        if self._metadata_binding is not None:
                            metadata_completion = self._metadata_binding.work(ticket,
                                current_pending=self._metadata_current, cancelled=cancel.is_set)
                            inventory_report = metadata_completion._report
                        else:
                            inventory_report = inventory_client.discover(ticket.inventory_previous)
                        duration = self._clock.now()-at
                        if not timedelta(0) <= duration <= timedelta(seconds=3*self.ollama_scope.limits.total_seconds):
                            raise ValueError("ollama_refresh_deadline")
                    else: raise ValueError("unknown_worker_operation")
                    elapsed = self._clock.now()-at
                    accepted = controller.observation_serial > prior_serial
                    cancelled = cancel.is_set()
                    if (elapsed < timedelta(0) or elapsed > timedelta(seconds=30) or cancelled) and accepted:
                        if prior_cleanup is not None and not controller.cleanup_held:
                            controller._cleanup_evidence = prior_cleanup
                        controller._hold("Cancelled or delayed observation held; exact owned session retained.")
                        accepted = False
                    snapshot = capture(not cancelled)
                    # Admission during capture also belongs to this operation.
                    # A cancellation there cannot publish cached clearance.
                    elapsed = self._clock.now()-at
                    cancelled = cancel.is_set()
                    if accepted and (cancelled or elapsed < timedelta(0) or elapsed > timedelta(seconds=30)):
                        if prior_cleanup is not None and not controller.cleanup_held:
                            controller._cleanup_evidence = prior_cleanup
                        controller._hold("Cancelled or delayed result held; exact owned session retained.")
                        accepted = False
                        snapshot = capture(False)
                    publish(ticket, snapshot, accepted, cancelled)
                except Exception as failure:
                    if ticket.operation == "ollama_refresh":
                        inventory_report = None; metadata_completion = None
                    error = ("ollama_refresh_failed" if ticket.operation == "ollama_refresh" else
                        type(failure).__name__ + ": " + str(failure)[:160])
                    if prior_cleanup is not None and not controller.cleanup_held:
                        controller._cleanup_evidence = prior_cleanup
                    if ticket.operation in ("dispatch", "poll"):
                        controller._hold("Async operation outcome unknown; no retry or inferred cleanup.")
                    publish(ticket, replace(capture(False), reason=error), error=error)
        except Exception as failure:
            failure_text = ("ollama_worker_initialization_failed" if self.ollama_scope is not None and not self._fixture_enabled
                else type(failure).__name__ + ": " + str(failure)[:160])
            fault_snapshot = replace(safe_capture(), state=State.UNKNOWN, occupied=self._fixture_enabled, exit_code=None,
                cards=(), reason="Fixture worker failed; ownership retained.")
            if not init_published:
                publish(self._init_ticket, fault_snapshot, error=failure_text)
            if current_ticket is not None:
                publish(current_ticket, fault_snapshot, cancelled=True, error=failure_text)
        finally:
            def exit_snapshot():
                snapshot = safe_capture()
                if failure_text is not None:
                    snapshot = replace(snapshot, state=State.UNKNOWN, occupied=self._fixture_enabled, exit_code=None,
                        cards=(), reason="Worker failed; retained ownership remains held.")
                return snapshot
            with self._admission_lock:
                self._owner_exited.set()
                # Every admitted intent gets its own ordered retirement receipt.
                while True:
                    try: ticket, cancel, entries = self._commands.get_nowait()
                    except Empty: break
                    publish(ticket, exit_snapshot(), cancelled=True, error=failure_text)
            publish(self._stop_ticket, exit_snapshot(), error=failure_text)

    @property
    def module_enabled(self): return self._module_enabled

    @property
    def module_snapshot(self): return self._module_snapshot

    @property
    def module_versions(self):
        return () if not self._module_enabled else tuple((item.offer_id, item.version) for item in self._module_binding.authority.offers)

    def request_module(self, action, offer_id=None):
        if (not self._module_enabled or self.worker_pending or self.stopping or self.closed
            or action not in {'install', 'start', 'health', 'recover', 'reopen', 'poll'}):
            return None
        if self._module_held and action not in {'poll', 'recover'}: return None
        if action in {'install', 'reopen'} and offer_id not in {'A', 'B', 'C'}: return None
        return self._submit('module_' + action, entries=(offer_id,))

    def cancel_module_operation(self):
        if not self._module_enabled or self.stopping or self.closed: return False
        for ticket, cancel in tuple(self._pending.values()):
            if ticket.operation.startswith('module_'): cancel.set()
        # Ordered compensation also covers cancellation after worker publication.
        return self._submit('module_cancel') is not None

    def begin_module_close(self):
        if self._module_enabled: self.request_shutdown()

    def module_close_context(self, now, *, extra_worker_pending=False):
        from .close_policy import CloseContext
        module = self._module_snapshot
        fresh = module is not None and timedelta(0) <= now-module.observed_at <= timedelta(seconds=30)
        return CloseContext(self.session_key, self.state, self.occupied,
            module is not None and module.owned is not None, bool(fresh),
            self.worker_pending or extra_worker_pending, self.cleanup_held)

    def _accept_module_result(self, result, now):
        from .module_lifecycle_candidate import ModuleSnapshot
        if self.closed or type(result) is not WorkerResult or type(result.ticket) is not WorkTicket: return False
        ticket = result.ticket
        pending = self._pending.get(ticket.request_id)
        expected = (self._init_ticket if ticket.operation == 'init' else self._stop_ticket
                    if ticket.operation == 'quiescent' else None if pending is None else pending[0])
        if (ticket != expected or ticket.bridge_id != self.bridge_id or type(result.sequence) is not int
            or result.sequence <= self._last_sequence or type(result.snapshot) is not WorkerSnapshot
            or type(result.cancelled) is not bool or type(result.observation_accepted) is not bool): return False
        module = result.snapshot.module
        if type(module) is not ModuleSnapshot: return False
        if (module.owned != result.snapshot.owned or module.observed_at != result.snapshot.observed_at
            or module.serial != result.snapshot.observation_serial
            or type(module.phase) is not str or type(module.reason) is not str): return False
        if (type(module.active) is not tuple or type(module.serial) is not int or module.serial < 0
            or any(type(getattr(module, field)) is not bool for field in
                ('child_active', 'pending_transaction', 'ready', 'ambiguous', 'lease_held'))): return False
        if module.owned is not None:
            if (type(module.owned) is not OwnedProcess or module.owned.owner != OWNER
                or type(module.owned.pid) is not int or module.owned.pid <= 0): return False
            identity = (module.owned.session.value, module.owned.plan_fingerprint)
            if ticket.operation not in {'module_start', 'module_reopen', 'quiescent', 'init', 'catalog'} and ticket.expected_session is not None and identity != ticket.expected_session:
                return False
        if module.child_active and (module.owned is None or module.exit_code is not None): return False
        if not module.child_active and module.owned is not None and not module.ambiguous and type(module.exit_code) is not int: return False
        expected_state = (State.UNKNOWN if module.ambiguous else State.RUNNING if module.child_active else
            State.EXITED if module.owned is not None and module.exit_code == 0 else
            State.FAILED if module.owned is not None and module.exit_code is not None else State.UNKNOWN)
        if result.snapshot.state is not expected_state or result.snapshot.occupied is not (module.child_active or module.ambiguous): return False
        try:
            fresh = timedelta(0) <= now-result.finished_at <= timedelta(seconds=30) and timedelta(0) <= now-module.observed_at <= timedelta(seconds=30)
        except (TypeError, ValueError): return False
        cancelled = result.cancelled or pending is not None and pending[1].is_set()
        current = ticket.generation == self.generation or ticket.operation == 'quiescent'
        self._last_sequence = result.sequence
        self._pending.pop(ticket.request_id, None)
        if ticket.operation == 'init': self._init_pending = False
        # Cancelled/stale positive results cannot clear ownership or update UI success.
        accepted = fresh and current and result.observation_accepted and not cancelled and result.error is None
        if accepted:
            self._module_snapshot = module
            self._snapshot = result.snapshot
            self._module_held = False
        else:
            self._module_held = True
            if result.error is not None:
                reason = f'Module operation failed ({result.error}); refresh exact owned status.'
                if module.phase == 'held': reason = module.reason + ' ' + reason
            elif cancelled:
                reason = 'Module operation cancelled; refresh exact owned status.'
            elif not fresh or not current:
                reason = 'Stale module result held; refresh exact owned status.'
            else:
                reason = 'Module observation held; refresh exact owned status.'
            self._snapshot = replace(self._snapshot, state=State.UNKNOWN, occupied=True, reason=reason)
        # Final teardown has its own current exact receipt and never echoes a stale result.
        if (ticket.operation == 'quiescent' and accepted and module.phase == 'closed'
            and not module.lease_held and not module.pending_transaction and not module.child_active and not module.ambiguous):
            self._final_ack = True
            self._module_snapshot, self._snapshot, self._module_held = module, result.snapshot, False
        return True

    def _module_worker(self, factory):
        from .module_lifecycle_candidate import ModuleSnapshot, ManagerModuleController, OperationCancelled
        import time
        controller = None
        sequence = 0
        close_fault = False
        last = ModuleSnapshot('initializing', 'Disposable owner starting.', (), None, False, None,
            False, False, False, False, self._clock.now(), 0)
        def capture():
            nonlocal last
            last = controller.worker_snapshot(self._clock.now())
            return last
        def retain_owner(reason):
            nonlocal last
            # A failed filesystem snapshot cannot erase the already acquired owner.
            observed = None if controller is None else controller._observed
            last = replace(last, phase='held', reason=reason, ready=False, ambiguous=True,
                owned=None if controller is None else controller._identity,
                child_active=observed is not None and observed.active,
                exit_code=None if observed is None else observed.exit_code,
                pending_transaction=controller is not None and controller._pending_offer is not None,
                lease_held=controller is not None and controller._lease is not None and controller._lease.held,
                observed_at=self._clock.now())
            return last
        def publish(ticket, module, *, cancelled=False, error=None):
            nonlocal sequence
            sequence += 1
            state = (State.UNKNOWN if module.ambiguous else State.RUNNING if module.child_active else
                     State.EXITED if module.owned is not None and module.exit_code == 0 else
                     State.FAILED if module.owned is not None and module.exit_code is not None else State.UNKNOWN)
            snapshot = WorkerSnapshot(state, module.child_active or module.ambiguous, module.reason,
                None, module.owned, None, module.observed_at, module.serial, module.exit_code, (), None, module)
            result = WorkerResult(ticket, sequence, self._clock.now(), snapshot, True, cancelled, error)
            try: self._results.put_nowait(result)
            except Full: self._overflow.set()
        def perform(action, offer_id=None, cancel=None):
            now = self._clock.now()
            if action == 'poll':
                if controller._identity is not None: controller.poll()
                return
            if action == 'install': prepared = controller.prepare_install(offer_id)
            elif action == 'start': prepared = controller.prepare_launch()
            elif action == 'reopen': prepared = controller.prepare_reopen(offer_id)
            elif action == 'health': prepared = controller.prepare_health()
            elif action == 'recover': prepared = controller.prepare_recovery()
            else: raise ValueError('finite_module_operation_required')
            if cancel is not None and cancel.is_set():
                controller._prepared = None
                raise OperationCancelled('cancelled_before_admission')
            permit = self._module_binding.authority.issue(prepared, self._clock.now())
            if cancel is not None and cancel.is_set():
                controller._prepared = None
                raise OperationCancelled('cancelled_before_consumption')
            now = self._clock.now()  # preparation/approval callback time cannot extend permit validity
            method = {'install': controller.install, 'start': controller.launch, 'reopen': controller.launch,
                      'health': controller.acknowledge_health, 'recover': controller.recover}[action]
            if action == 'install': method(prepared, permit, now, cancel=cancel)
            else: method(prepared, permit, now)
            if action in {'start', 'reopen'}:
                controller.poll()  # publish actual direct-child status, not dispatch acknowledgement
        def compensate():
            if controller._identity is not None:
                observation = controller.poll()
                if observation.active: return False
            if controller._ambiguous: return False
            if controller._pending_offer is not None: perform('recover')
            return True
        try:
            acquired = factory()
            if type(acquired) is not ManagerModuleController: raise ValueError('typed_disposable_controller_required')
            controller = acquired
            publish(self._init_ticket, capture())
        except Exception as error:
            publish(self._init_ticket, retain_owner('Disposable owner initialization held; refresh or close for checked cleanup.'),
                error=type(error).__name__)
            # Before acquisition there is no owner. After acquisition, keep this exact
            # controller on its worker until close_owner acknowledges physical release.
        while True:
            if self._stop.is_set():
                while True:
                    try: ticket, cancel, entries = self._commands.get_nowait()
                    except Empty: break
                    publish(ticket, last, cancelled=True)
                if controller is None:
                    closed = replace(last, phase='closed', reason='Unconfigured owner stopped.', lease_held=False,
                        ambiguous=False, observed_at=self._clock.now())
                elif close_fault:
                    time.sleep(0.05); continue  # retain exact owner; no recovery retry/kill
                else:
                    try:
                        if not compensate():
                            time.sleep(0.05); continue
                        try: last = capture()
                        except Exception:
                            retain_owner('Current module status unavailable; checking exact owner cleanup.')
                        # close_owner independently checks binding, data, child and
                        # transaction drain; a broken active record alone cannot leak it.
                        if controller.close_owner() is not True or controller._lease.held:
                            raise ValueError('physical_owner_release_unacknowledged')
                        closed = replace(last, phase='closed', reason=controller.reason, lease_held=False,
                            child_active=False, pending_transaction=False, ready=False, ambiguous=False,
                            observed_at=self._clock.now())
                    except Exception as error:
                        close_fault = True
                        publish(self._stop_ticket, retain_owner('Close recovery/ownership held; exact owner retained.'),
                            error=type(error).__name__)
                        continue
                with self._admission_lock: self._owner_exited.set()
                publish(self._stop_ticket, closed)
                return
            try: ticket, cancel, entries = self._commands.get(timeout=0.05)
            except Empty: continue
            if cancel.is_set():
                publish(ticket, last, cancelled=True); continue
            try:
                if ticket.operation == 'catalog': pass
                elif ticket.operation == 'module_cancel':
                    # Cancellation never stops a child. Keep this intent until its exact exit.
                    while not compensate():
                        time.sleep(0.05)
                    controller.reason = 'Cancelled operation recovered; code retained and user data unchanged.'
                elif ticket.operation.startswith('module_'):
                    action = ticket.operation.removeprefix('module_')
                    perform(action, entries[0] if entries else None, cancel)
                else: raise ValueError('module_worker_operation_required')
                if cancel.is_set():
                    while not compensate(): time.sleep(0.05)
                publish(ticket, capture(), cancelled=cancel.is_set())
            except OperationCancelled:
                try:
                    while not compensate(): time.sleep(0.05)
                    publish(ticket, capture(), cancelled=True)
                except Exception as error:
                    publish(ticket, retain_owner('Cancellation recovery held; exact owner retained.'), cancelled=True, error=type(error).__name__)
            except Exception as error:
                try: last = capture()
                except Exception: retain_owner('Current module status unavailable; exact owner retained.')
                publish(ticket, last, error=type(error).__name__)
