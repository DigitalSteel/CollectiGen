"""Detached fake-only Manager seam. Default denial; no native provider/backend.

Pure host contracts are package-relative. Explicit fixture bindings do not
verify files, provide a native runner or consume historical approval receipts.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import sys

from .host_contracts import (LaunchPlan, OwnedProcess, State, OWNER, COMPONENT, ARGV, aware,
    ApprovalGate, ExecutionPermit, DeploymentBinding)
from suite_core.installation import ComponentId
from .selected_package_launch import module_launch_contract, validate_package_payload, pin


@dataclass(frozen=True)
class CardBinding:
    component_id: ComponentId
    version: str | None
    archive_pin: str | None
    source_plan_pin: str | None
    source_ref: str
    observed_at: datetime
    generation: int


@dataclass(frozen=True)
class FixtureOffer:
    binding: CardBinding
    plan: LaunchPlan
    permit: ExecutionPermit
    verified_at: datetime
    eligible: bool = False


@dataclass(frozen=True)
class CleanupEvidence:
    """An accepted error belongs to one exact owned identity, not a UI phrase."""
    identity: OwnedProcess
    observed_at: datetime
    error: str


class LifecycleController:
    """Injection is deliberately fixture-only until a real async host is reviewed.

    offer_provider and runner are trusted, quick, in-memory fixture adapters.
    Native disk/verification/process work must never be supplied to this seam.
    An ambiguous dispatch remains occupied; only exact-session terminal status
    releases that hold. Refresh never drops the retained runner/session.
    """
    def __init__(self, *, fixture_mode=False, offer_provider=None, runner=None, gate=None, deployment_binding=None, failure_observer=None, interpreter_admission=None):
        self._failure_observer = failure_observer
        self._deployment = deployment_binding
        self._interpreter_admission = interpreter_admission
        self.fixture_mode = fixture_mode is True
        self.provider = offer_provider
        self.runner = runner
        self.gate = gate if gate is not None else ApprovalGate()
        self.generation = 0
        self.bindings = ()
        self.session_offer = None
        self.owned = None
        self.occupied = False
        self.state = State.UNKNOWN
        self.reason = "No lifecycle provider; fresh exact-scope approval required."
        self.observed_at = None
        self.exit_code = None
        self.attempted = set()
        self._methods = None
        self._terminal = None
        self._cleanup_evidence = None
        self.observation_serial = 0

    @property
    def cleanup_evidence(self): return self._cleanup_evidence

    @property
    def cleanup_held(self): return self._cleanup_evidence is not None

    def _report_failure(self, boundary, error):
        # Explicit owner-thread observer only; its return grants no authority.
        if self._failure_observer is None:
            return
        try:
            self._failure_observer(boundary, error)
        except Exception:
            pass  # Diagnostic failure cannot change the canonical lifecycle hold.

    @property
    def fixture_enabled(self):
        return (self.fixture_mode and callable(self.provider)
                and getattr(self.runner, "simulation_only", None) is True)

    def replace_catalog(self, entries):
        self.generation += 1
        self.bindings = tuple(self._binding(entry) for entry in entries)

    def _binding(self, entry):
        package = entry.candidate_package
        return CardBinding(entry.component_id, getattr(package, "software_version", None),
            getattr(package, "archive_sha256", None), getattr(package, "source_plan_sha256", None),
            entry.source_ref, entry.observed_at, self.generation)

    def binding_for(self, entry):
        binding = self._binding(entry)
        return binding if binding in self.bindings else None

    def _offer(self, binding, now):
        aware(now)
        if not self.fixture_enabled:
            raise ValueError("No lifecycle provider; fresh exact-scope approval required.")
        if self.cleanup_held:
            raise ValueError("Known session cleanup remains held; accepted same-session clearance required.")
        if self.occupied:
            raise ValueError("Owned or ambiguous session held; no second dispatch.")
        if type(binding) is not CardBinding or binding not in self.bindings:
            raise ValueError("Card changed or refreshed; current binding required.")
        deployment = self._deployment
        if type(deployment) is not DeploymentBinding:
            raise ValueError("Explicit synthetic deployment binding required; production lifecycle unavailable.")
        offer = self.provider(binding, now)
        if type(offer) is not FixtureOffer or offer.binding != binding or offer.eligible is not True:
            raise ValueError("Exact eligible fixture deployment not supplied.")
        aware(offer.verified_at)
        if not timedelta(0) <= now - offer.verified_at <= timedelta(seconds=30):
            raise ValueError("Deployment observation stale or future.")
        plan, permit = offer.plan, offer.permit
        arguments, entrypoint = module_launch_contract(binding.component_id,
            interpreter_admission=self._interpreter_admission, package_root=deployment.root)
        if type(plan) is not LaunchPlan or type(permit) is not ExecutionPermit:
            raise ValueError("Typed launch plan and execution permit required.")
        validate_package_payload(deployment, self._interpreter_admission)
        if entrypoint not in dict(deployment.payload_pins):
            raise ValueError("Source entrypoint required.")
        entrypoint_pin = dict(deployment.payload_pins)[entrypoint]
        pin(entrypoint_pin); pin(plan.entrypoint_pin)
        if (type(plan) is not LaunchPlan or type(permit) is not ExecutionPermit
            or type(binding.component_id) is not ComponentId
            or binding.component_id != plan.component_id or plan.component_id != deployment.component_id
            or binding.version != deployment.version or binding.archive_pin != deployment.archive_pin
            or binding.source_plan_pin != deployment.source_plan_pin
            or plan.owner != OWNER or plan.root != deployment.root or plan.program != deployment.program
            or plan.arguments != deployment.arguments or plan.arguments != arguments or plan.archive_pin != binding.archive_pin
            or plan.entrypoint_pin != entrypoint_pin
            or plan.snapshot is not None or plan.shell is not False or plan.open_supported is not False
            or type(plan.fingerprint) is not str or len(plan.fingerprint) != 64
            or permit.fingerprint != plan.fingerprint):
            raise ValueError("Component/version/candidate/deployment binding changed.")
        aware(permit.approved_at); aware(permit.expires_at)
        if not permit.approved_at <= now < permit.expires_at <= permit.approved_at + timedelta(minutes=5):
            raise ValueError("Approval expired or future.")
        if permit.nonce in self.attempted or permit.nonce in self.gate.used:
            raise ValueError("Approval/session already used; no automatic retry.")
        if self.gate.independently_reviewed is not True or not callable(self.gate.approval_lookup):
            raise ValueError("Independent review and unused fixture approval required.")
        if self.gate.approval_lookup(permit) is not True:
            raise ValueError("Unused scoped fixture approval not confirmed.")
        return offer

    def availability(self, binding, now):
        try:
            self._offer(binding, now)
            return True, "Fake lifecycle fixture only; no real process launch."
        except Exception as error:
            return False, str(error)[:240] or "Lifecycle held."

    def dispatch(self, binding, now):
        try:
            offer = self._offer(binding, now)
            self.gate.consume(offer.permit, offer.plan, now)
        except Exception as error:
            self._report_failure("dispatch_admission", error)
            self.reason = str(error)[:240] or "Lifecycle held."
            return False
        self.attempted.add(offer.permit.nonce)
        self.session_offer = offer
        self.occupied = True
        self.owned = None
        self.exit_code = None
        self._terminal = None
        self.state = State.STARTING
        self.reason = "Starting fake owned session; UI readiness unknown."
        self.observed_at = now
        try:
            self._methods = {name: getattr(self.runner, name) for name in ("start", "poll", "status")}
            if any(not callable(method) or getattr(method, "__self__", None) is not self.runner
                   for method in self._methods.values()):
                raise ValueError("Bound fixture runner methods required.")
            self._methods["start"](offer.permit, now)
            self._accept_status(now)
            return True
        except Exception as error:
            self._report_failure("dispatch_execution", error)
            self._hold("Dispatch outcome unknown; retained session blocks retry.")
            return False

    def _hold(self, reason):
        self.state = State.UNKNOWN
        self.reason = reason
        self.occupied = True

    def _same_methods(self):
        for name, method in self._methods.items():
            current = getattr(self.runner, name)
            if (getattr(current, "__self__", None) is not getattr(method, "__self__", None)
                or getattr(current, "__func__", None) is not getattr(method, "__func__", None)):
                raise ValueError("Runner binding changed.")

    def _accept_status(self, now):
        self._same_methods()
        offer = self.session_offer
        identity = self.runner.owned
        if (type(identity) is not OwnedProcess or identity.owner != OWNER
            or identity.session != offer.permit.nonce
            or identity.plan_fingerprint != offer.plan.fingerprint
            or type(identity.pid) is not int or identity.pid <= 0):
            raise ValueError("Exact owned identity not established.")
        aware(identity.created_at)
        if not offer.permit.approved_at <= identity.created_at <= now:
            raise ValueError("Owned creation time changed.")
        if self.owned is not None and identity != self.owned:
            raise ValueError("Owned identity changed.")
        if self.cleanup_held and identity != self._cleanup_evidence.identity:
            raise ValueError("Cleanup evidence session identity changed.")
        status = self._methods["status"]()
        if type(status) is not dict or type(status.get("active")) is not bool:
            raise ValueError("Malformed detached status.")
        state = State(status.get("state"))
        active = status["active"]
        code = status.get("exit_code")
        terminal = state in (State.EXITED, State.FAILED)
        if (terminal and (active or type(code) is not int)
            or not terminal and (not active or code is not None)
            or state is State.EXITED and code != 0):
            raise ValueError("Contradictory completion status.")
        if self._terminal is not None and (state, code) != self._terminal:
            raise ValueError("Terminal session cannot be resurrected.")
        if terminal:
            self._terminal = (state, code)
        self.owned = identity
        self.observation_serial += 1 # only after full identity/status admission
        if status.get("cleanup_error") is not None:
            error = status["cleanup_error"]
            self._cleanup_evidence = CleanupEvidence(identity, now,
                error if type(error) is str else "malformed_cleanup_error_present")
            self.exit_code = code
            self.observed_at = now
            self._hold("Owned exit/status observed but cleanup held; review before further dispatch.")
            return
        if self.cleanup_held:
            # Missing fields, failed polls, stale/foreign identities or malformed
            # status cannot acknowledge cleanup. All admission above must pass.
            if "cleanup_error" not in status:
                self.observed_at = now
                self._hold("Known session cleanup remains held; affirmative same-session clearance missing.")
                return
            self._cleanup_evidence = None # explicit None in accepted same-session status
        self.state = state
        self.occupied = active
        self.exit_code = code
        self.reason = ("Fake owned process " + state.value + "; UI readiness unknown."
            if not terminal else "Fake owned session " + state.value + f"; exit code {code}.")
        self.observed_at = now

    def poll(self, now):
        aware(now)
        if self.session_offer is None:
            return
        try:
            if self.observed_at is not None and now < self.observed_at:
                raise ValueError("Status clock regressed.")
            self._same_methods()
            self._methods["poll"](now)
            self._accept_status(now)
        except Exception as error:
            self._report_failure("observation", error)
            self._hold("Owned observation unknown; retained session blocks retry.")

    def projection(self, binding, now):
        aware(now)
        ready, gate_reason = self.availability(binding, now)
        current = self.session_offer is not None and self.session_offer.binding == binding and binding in self.bindings
        fresh = (self.observed_at is not None
            and timedelta(0) <= now - self.observed_at <= timedelta(seconds=30))
        state = self.state.value if current and fresh else "unknown"
        if self.session_offer is not None and not current:
            reason = "Previous exact session retained separately; current card has no session authority."
        elif current and not fresh:
            reason = "Session status stale; readiness unknown."
        else:
            reason = self.reason if current else gate_reason
        return {"state": state, "reason": reason, "launch_enabled": ready,
            "ui_ready": "unknown", "open_supported": False, "installed": False,
            "simulation_only": self.fixture_enabled,
            "cleanup_held": self.cleanup_held if current else False,
            "cleanup_session": self._cleanup_evidence.identity.session.value if current and self.cleanup_held else None,
            "owned_pid": self.owned.pid if current and fresh and self.owned else None,
            "exit_code": self.exit_code if current and fresh else None}

    @staticmethod
    def disposable_factory(binding):
        """Explicit disposable branch for the same Manager worker; defaults unchanged."""
        from .module_lifecycle_candidate import (DisposableModuleBinding, DisposableBatchAuthority,
            LocalPackageProvider, ManagerModuleController, bounded)
        from hashlib import sha256
        from datetime import timezone
        if (type(binding) is not DisposableModuleBinding or type(binding.authority) is not DisposableBatchAuthority
            or type(binding.provider) is not LocalPackageProvider or not callable(binding.runner_factory)
            or bounded(binding.root, binding.authority.root) != binding.authority.root or binding.provider._offers != binding.authority.offers
            or sha256(binding.trust_bytes).hexdigest() != binding.authority.trust_pin):
            raise ValueError('explicit_disposable_binding_required')
        def factory():
            gate = binding.authority.bind(datetime.now(timezone.utc))
            return ManagerModuleController(enabled=True, root=binding.root, provider=binding.provider,
                trust_bytes=binding.trust_bytes, runner=binding.runner_factory(), gate=gate, sentinels=binding.sentinels)
        return factory
