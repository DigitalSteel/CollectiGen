"""Pure close-ownership contract. No child control, I/O, waits or worker backend."""
from dataclasses import dataclass
from enum import Enum
import math
from suite_core.manager.lifecycle_controller import State


class CloseRole(Enum):
    MANAGER_OWNED = "manager-owned"
    SHARED_OBSERVER = "shared-observer"
    UNKNOWN = "unknown"


class CloseState(Enum):
    READY = "ready"
    DETACH_SHARED = "detach-shared-observer"
    PENDING = "pending"
    HELD = "held"
    CANCELLED = "cancelled"
    CLOSED = "closed"


@dataclass(frozen=True)
class CloseContext:
    session_key: tuple | None
    state: State
    occupied: bool
    identity_known: bool
    fresh: bool
    worker_pending: bool = False
    cleanup_held: bool = False


@dataclass(frozen=True)
class CloseDecision:
    state: CloseState
    allow_close: bool
    reason: str
    request_id: int
    deadline: float | None
    child_control_authorized: bool = False


@dataclass(frozen=True)
class CloseUpdate:
    """Future bridge completion envelope, tied to one close request/session."""
    request_id: int
    sequence: int
    session_key: tuple | None
    context: CloseContext
    observed_mono: float


class CloseCoordinator:
    """Default owned-session preservation; shared observer detach is explicit.

    The deadline bounds the pending-close decision, not a syscall/worker lifetime.
    Expiry latches a hold: a late completion cannot silently close the window.
    Cancel and a deliberate fresh close are required to resume after expiry.
    No role grants process/service termination or readiness authority.
    """
    def __init__(self, *, role=CloseRole.MANAGER_OWNED, timeout_seconds=5):
        if type(role) is not CloseRole:
            raise ValueError("typed_close_role_required")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("bounded_close_deadline_required")
        self._role = role
        self.timeout = timeout_seconds
        self.request_id = 0
        self.pending = False
        self.closed = False
        self.deadline = None
        self.session_key = None
        self.last_mono = None
        self.last_sequence = 0
        self.expired = False
        self.scope_held = False
        # Future worker owner supplies a detached boolean; never a join callback.
        self.worker_pending = False
        self.decision = CloseDecision(CloseState.CANCELLED, False, "No close requested.", 0, None)

    @property
    def role(self): return self._role

    def _decision(self, state, reason, allow=False):
        self.decision = CloseDecision(state, allow, reason, self.request_id, self.deadline)
        return self.decision

    def _time(self, now):
        if (type(now) not in (int, float) or not math.isfinite(now) or now < 0
            or self.last_mono is not None and now < self.last_mono):
            return False
        self.last_mono = now
        return True

    def _valid(self, context):
        if type(context) is not CloseContext or type(context.state) is not State:
            return False
        if any(type(getattr(context, name)) is not bool for name in
               ("occupied", "identity_known", "fresh", "worker_pending", "cleanup_held")):
            return False
        key = context.session_key
        return key is None or (type(key) is tuple and len(key) == 2
            and all(type(value) is str and bool(value) for value in key))

    def request(self, context, now):
        if self.closed:
            return self.decision
        if not self._time(now):
            return self._decision(CloseState.HELD, "Close clock invalid; ownership retained.")
        if not self._valid(context):
            return self._decision(CloseState.HELD, "Close facts malformed; ownership retained.")
        if not self.pending:
            self.request_id += 1
            self.pending = True
            self.deadline = now + self.timeout
            self.session_key = context.session_key
            self.last_sequence = 0
            self.expired = False
            self.scope_held = False
        return self._evaluate(context, now)

    def _evaluate(self, context, now):
        if context.session_key != self.session_key:
            self.scope_held = True
            return self._decision(CloseState.HELD, "Session changed during close; cancel and review the new session.")
        if self.scope_held:
            return self._decision(CloseState.HELD, "Session changed during close; cancel and review the new session.")
        if now >= self.deadline:
            self.expired = True
        if self.expired:
            return self._decision(CloseState.HELD, "Close deadline reached; no exit inferred. Cancel pending close, then retry after reviewing completion.")
        if context.worker_pending:
            return self._decision(CloseState.PENDING, "Finishing the current status check before closing Manager; the app remains supervised.")
        if context.cleanup_held:
            return self._decision(CloseState.HELD, "Cleanup remains held; retain ownership and review before closing.")
        if self.role is CloseRole.SHARED_OBSERVER:
            return self._decision(CloseState.DETACH_SHARED, "Close this observer only; shared service state is retained and no stop is requested.", True)
        if self.role is CloseRole.UNKNOWN and (context.occupied or context.session_key is not None):
            return self._decision(CloseState.HELD, "Session ownership unknown; no close or child-control authority.")
        if context.session_key is None and not context.occupied:
            return self._decision(CloseState.READY, "No owned session or worker remains; Manager can close.", True)
        if not context.fresh or not context.identity_known or context.state is State.UNKNOWN:
            return self._decision(CloseState.HELD, "Owned or ambiguous session retained; ownership, completion or fresh status is unknown.")
        if context.occupied:
            return self._decision(CloseState.PENDING, "Manager-owned session stays supervised; waiting for its confirmed completion without stopping it.")
        if context.state in (State.EXITED, State.FAILED):
            return self._decision(CloseState.READY, "Exact owned session completion confirmed; Manager can close.", True)
        return self._decision(CloseState.HELD, "Session status contradicts completion; ownership retained.")

    def observe(self, update, now):
        """Reject foreign/replayed updates; late completion cannot release expiry."""
        if (self.closed or not self.pending or type(update) is not CloseUpdate
            or type(update.request_id) is not int or update.request_id != self.request_id
            or type(update.sequence) is not int or update.sequence <= self.last_sequence
            or update.session_key != self.session_key or not self._valid(update.context)
            or update.context.session_key != self.session_key):
            return False
        if not self._time(now):
            self._decision(CloseState.HELD, "Close clock invalid; ownership retained.")
            return False
        if (type(update.observed_mono) not in (int, float) or not math.isfinite(update.observed_mono)
            or not 0 <= now - update.observed_mono <= self.timeout):
            self._decision(CloseState.HELD, "Close update stale or future; completion not inferred.")
            return False
        self.last_sequence = update.sequence
        self._evaluate(update.context, now)
        return True

    def cancel(self):
        if self.closed: return False
        self.pending = False
        self.request_id += 1 # invalidate all queued completions for the old intent
        self.deadline = None
        self.expired = False
        self.scope_held = False
        self.last_sequence = 0
        self._decision(CloseState.CANCELLED, "Close cancelled; existing session remains supervised.")
        return True

    def finish(self):
        if not self.decision.allow_close: return False
        self.closed = True
        self.pending = False
        self._decision(CloseState.CLOSED, self.decision.reason, True)
        return True
