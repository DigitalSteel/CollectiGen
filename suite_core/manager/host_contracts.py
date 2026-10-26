"""Pure Manager identities and deny-by-default fixture gate; no deployment or native provider."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from suite_core.backend_contracts import ApplicationId, StableId
from suite_core.installation import ComponentId
OWNER = ApplicationId("suite-manager")
COMPONENT = ComponentId("collectigen.tag-encyclopedia")
ARGV = ("-B", "-E", "-s", "-m", "suite_core.encyclopedia")


def require(value, reason):
    if not value: raise ValueError(reason)

def aware(value):
    require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None, "aware_time_required")

@dataclass(frozen=True)
class LaunchPlan:
    component_id: ComponentId
    owner: ApplicationId
    root: str
    program: str
    arguments: tuple
    archive_pin: str
    entrypoint_pin: str
    fingerprint: str
    snapshot: object = None
    shell: bool = False
    open_supported: bool = False

@dataclass(frozen=True)
class OwnedProcess:
    session: StableId
    owner: ApplicationId
    plan_fingerprint: str
    pid: int
    created_at: datetime

class State(str, Enum):
    UNKNOWN = "unknown"
    STARTING = "starting"
    RUNNING = "running" # confirmed process only; no UI readiness/activation proof
    CLOSING = "closing"
    EXITED = "exited"
    FAILED = "failed"

@dataclass(frozen=True)
class ExecutionPermit:
    nonce: StableId
    fingerprint: str
    approved_at: datetime
    expires_at: datetime
    approval_reference: str
    exact_child_fallback: bool = False

class ApprovalGate:
    """Host-owned review/approval boundary, never reconstructed from old launch logs."""
    def __init__(self, *, independently_reviewed=False, approval_lookup=None):
        self.independently_reviewed = independently_reviewed
        self.approval_lookup = approval_lookup
        self.used = set()

    def consume(self, permit, plan, now):
        require(self.independently_reviewed is True and callable(self.approval_lookup), "independent_review_and_new_approval_required")
        require(type(permit) is ExecutionPermit and type(permit.nonce) is StableId, "exact_execution_permit_required")
        aware(permit.approved_at); aware(permit.expires_at)
        require(permit.approved_at <= now < permit.expires_at <= permit.approved_at + timedelta(minutes=5), "approval_expired_or_future")
        require(type(permit.fingerprint) is str and permit.fingerprint == plan.fingerprint
            and type(permit.approval_reference) is str and bool(permit.approval_reference)
            and type(permit.exact_child_fallback) is bool, "approval_binding_changed")
        require(permit.nonce not in self.used and self.approval_lookup(permit) is True, "new_scoped_approval_not_confirmed")
        self.used.add(permit.nonce)

@dataclass(frozen=True)
class DeploymentBinding:
    """Explicit synthetic binding only, not a filesystem attestation or runtime approval."""
    component_id: ComponentId
    version: str
    root: str
    program: str
    archive_pin: str
    source_plan_pin: str
    payload_pins: tuple
    arguments: tuple
