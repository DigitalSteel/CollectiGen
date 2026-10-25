"""Small, standard-library-only contracts for approved backend integrations.

These types grant no connection, execution or installation authority. They do
not implement a registry, scheduler or distributed reservation. Consumer-owned
adapters must preserve requests and report honest unknown/stale observations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

from .version import BACKEND_CONTRACT_VERSION

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")

@dataclass(frozen=True, slots=True)
class StableId:
    value: str
    def __post_init__(self):
        if not isinstance(self.value, str) or not _ID.fullmatch(self.value):
            raise ValueError("A stable identifier needs 1-160 unambiguous characters")

@dataclass(frozen=True, slots=True)
class ApplicationId(StableId): pass
@dataclass(frozen=True, slots=True)
class BackendId(StableId): pass
@dataclass(frozen=True, slots=True)
class HostId(StableId): pass
@dataclass(frozen=True, slots=True)
class CapabilityId(StableId): pass
@dataclass(frozen=True, slots=True)
class ModelProfileId(StableId): pass
@dataclass(frozen=True, slots=True)
class ResourceGroupId(StableId): pass
@dataclass(frozen=True, slots=True)
class TaskId(StableId): pass
@dataclass(frozen=True, slots=True)
class JobId(StableId): pass
@dataclass(frozen=True, slots=True)
class LeaseId(StableId): pass
@dataclass(frozen=True, slots=True)
class OperationId(StableId): pass
@dataclass(frozen=True, slots=True)
class CorrelationId(StableId): pass
@dataclass(frozen=True, slots=True)
class FeatureId(StableId): pass
@dataclass(frozen=True, slots=True)
class ConnectionId(StableId): pass

def _typed(value, expected):
    if type(value) is not expected:
        raise TypeError(f"Expected {expected.__name__}")

def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be explicit")

def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Observations require timezone-aware timestamps")

def _count(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Measurements must be non-negative integers or unknown")

def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

def _json_value(value, seen=None):
    seen = set() if seen is None else seen
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float:
        json.dumps(value, allow_nan=False)
        return
    if type(value) is list:
        if id(value) in seen: raise ValueError("Settings cannot contain cycles")
        seen.add(id(value))
        for item in value: _json_value(item,seen)
        seen.remove(id(value))
        return
    if type(value) is dict and all(type(key) is str for key in value):
        if id(value) in seen: raise ValueError("Settings cannot contain cycles")
        seen.add(id(value))
        for item in value.values(): _json_value(item,seen)
        seen.remove(id(value))
        return
    raise TypeError("Settings require finite JSON values and string keys")

@dataclass(frozen=True, slots=True)
class FrozenSettings:
    """Canonical immutable JSON; decoding returns a detached mutable copy."""
    canonical_json: str = field(repr=False)
    def __post_init__(self):
        if not isinstance(self.canonical_json, str): raise TypeError("JSON text required")
        value = json.loads(self.canonical_json)
        if type(value) is not dict: raise TypeError("Settings must be a JSON object")
        _json_value(value)
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False)
        if canonical != self.canonical_json: raise ValueError("Use canonical settings JSON")
    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]):
        value = dict(values)
        _json_value(value)
        return cls(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False))
    def to_mapping(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)
    @property
    def fingerprint(self):
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: ModelProfileId
    family: str
    model_reference: str
    revision: str
    runtime_settings: FrozenSettings
    def __post_init__(self):
        _typed(self.id, ModelProfileId); _typed(self.runtime_settings, FrozenSettings)
        for label in ("family", "model_reference", "revision"): _text(getattr(self,label), label)
    @property
    def fingerprint(self):
        return _digest([self.id.value, self.family, self.model_reference,
            self.revision, self.runtime_settings.canonical_json])

class BackendState(str, Enum):
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    READY = "ready"
    BUSY = "busy"

class ProviderCancellation(str, Enum):
    UNSUPPORTED = "unsupported"
    QUEUE_ONLY = "queue_only"
    BEST_EFFORT = "best_effort"
    JOB_SCOPED = "job_scoped"

@dataclass(frozen=True, slots=True)
class CapabilitySupport:
    id: CapabilityId
    protocols: frozenset[str]
    structured_output: bool | None = None
    context_token_limit: int | None = None
    output_token_limit: int | None = None
    queue_removal: bool = False
    provider_cancellation: ProviderCancellation = ProviderCancellation.UNSUPPORTED
    def __post_init__(self):
        _typed(self.id,CapabilityId); _typed(self.provider_cancellation,ProviderCancellation)
        if type(self.protocols) is not frozenset or not self.protocols: raise ValueError("Declare immutable protocol support")
        for protocol in self.protocols: _text(protocol,"protocol")
        if self.structured_output is not None and type(self.structured_output) is not bool: raise TypeError("Structured support is true, false or unknown")
        if type(self.queue_removal) is not bool: raise TypeError("Queue removal support must be explicit")
        _count(self.context_token_limit); _count(self.output_token_limit)

@dataclass(frozen=True, slots=True)
class BackendInstance:
    id: BackendId
    host_id: HostId
    adapter_id: str
    adapter_version: str
    connection_ref: ConnectionId
    capabilities: tuple[CapabilitySupport, ...]
    model_profiles: tuple[ModelProfile, ...]
    resource_groups: frozenset[ResourceGroupId]
    state: BackendState = BackendState.UNKNOWN
    contract_version: int = BACKEND_CONTRACT_VERSION
    def __post_init__(self):
        _typed(self.id, BackendId); _typed(self.host_id, HostId); _typed(self.state, BackendState)
        for label in ("adapter_id", "adapter_version"): _text(getattr(self,label), label)
        _typed(self.connection_ref,ConnectionId)
        if type(self.contract_version) is not int or self.contract_version != BACKEND_CONTRACT_VERSION:
            raise ValueError("Unsupported backend interface version")
        if type(self.capabilities) is not tuple or type(self.resource_groups) is not frozenset or type(self.model_profiles) is not tuple:
            raise TypeError("Backend collections must be immutable")
        for item in self.capabilities: _typed(item, CapabilitySupport)
        for item in self.resource_groups: _typed(item, ResourceGroupId)
        for item in self.model_profiles: _typed(item, ModelProfile)
        if len({item.id for item in self.model_profiles}) != len(self.model_profiles):
            raise ValueError("Duplicate model-profile identity")
        if len({item.id for item in self.capabilities}) != len(self.capabilities): raise ValueError("Duplicate capability identity")

@dataclass(frozen=True, slots=True)
class ResourceGroup:
    """Explicit resource identity; backend URL/app name is not a GPU identity."""
    id: ResourceGroupId
    host_id: HostId
    device_reference: StableId | None
    identity_verified: bool = False
    def __post_init__(self):
        _typed(self.id,ResourceGroupId); _typed(self.host_id,HostId)
        if self.device_reference is not None: _typed(self.device_reference,StableId)
        if type(self.identity_verified) is not bool: raise TypeError("Resource identity verification must be explicit")
        if self.identity_verified and self.device_reference is None: raise ValueError("Verified identity requires a resource reference")

@dataclass(frozen=True, slots=True)
class PrivatePayloadRef:
    """Opaque app-owned reference, not prompt/output contents or a remote URL."""
    owner_application: ApplicationId
    reference: StableId
    fingerprint: str
    media_type: str
    def __post_init__(self):
        _typed(self.owner_application,ApplicationId); _typed(self.reference,StableId)
        if not isinstance(self.fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.fingerprint): raise ValueError("Payload needs a SHA-256 fingerprint")
        _text(self.media_type,"media_type")

@dataclass(frozen=True, slots=True)
class ApprovedRoute:
    """Caller approval assertion, not an authentication token.

    Adapters revalidate approval_ref against app-owned actual approvals. A changed
    endpoint binding is not authorized by retaining the backend identifier.
    """
    owner_application: ApplicationId
    feature: FeatureId
    backend_id: BackendId
    connection_ref: ConnectionId
    capability: CapabilityId
    model_profile_fingerprint: str
    data_audience_ref: StableId
    approval_ref: StableId
    def __post_init__(self):
        for label,kind in (("owner_application",ApplicationId),("feature",FeatureId),("backend_id",BackendId),
                          ("capability",CapabilityId),("data_audience_ref",StableId),("approval_ref",StableId),("connection_ref",ConnectionId)):
            _typed(getattr(self,label),kind)
        if not isinstance(self.model_profile_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.model_profile_fingerprint): raise ValueError("Approval needs model-profile fingerprint")

class WorkClass(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"

@dataclass(frozen=True, slots=True)
class ResourceRequirements:
    estimated_vram_bytes: int | None = None
    estimated_ram_bytes: int | None = None
    reserve_vram_bytes: int = 0
    estimate_basis: str = "unknown"
    uses_gpu: bool = True
    def __post_init__(self):
        _count(self.estimated_vram_bytes); _count(self.estimated_ram_bytes); _count(self.reserve_vram_bytes)
        if self.reserve_vram_bytes is None: raise ValueError("Reserve must be explicit")
        _text(self.estimate_basis, "estimate_basis")
        if type(self.uses_gpu) is not bool: raise TypeError("Execution resource must be explicit")

@dataclass(frozen=True, slots=True)
class TaskRequest:
    id: TaskId
    owner_application: ApplicationId
    capability: CapabilityId
    model_profile: ModelProfile
    options: FrozenSettings
    scope_fingerprint: str
    resources: ResourceRequirements
    operation_id: OperationId
    correlation_id: CorrelationId
    feature: FeatureId
    payload: PrivatePayloadRef
    approved_routes: tuple[ApprovedRoute, ...]
    created_at: datetime
    deadline_at: datetime
    protocol: str
    requires_structured_output: bool = False
    context_tokens: int | None = None
    output_tokens: int | None = None
    work_class: WorkClass = WorkClass.FOREGROUND
    priority: int = 0
    pinned_backend: BackendId | None = None
    memory_pressure_override: bool = False
    def __post_init__(self):
        for label,kind in (("id",TaskId),("owner_application",ApplicationId),("capability",CapabilityId),
                          ("model_profile",ModelProfile),("options",FrozenSettings),("resources",ResourceRequirements),
                          ("operation_id",OperationId),("correlation_id",CorrelationId),("feature",FeatureId),
                          ("payload",PrivatePayloadRef),("work_class",WorkClass)):
            _typed(getattr(self,label),kind)
        if self.pinned_backend is not None: _typed(self.pinned_backend,BackendId)
        if not isinstance(self.scope_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.scope_fingerprint):
            raise ValueError("Scope needs a SHA-256 fingerprint from its owning app")
        if type(self.memory_pressure_override) is not bool: raise TypeError("Override must be a task-specific boolean")
        _time(self.created_at); _time(self.deadline_at)
        if self.deadline_at <= self.created_at: raise ValueError("Deadline must follow request creation")
        if type(self.priority) is not int: raise TypeError("Priority must be an integer")
        _text(self.protocol,"protocol")
        if type(self.requires_structured_output) is not bool: raise TypeError("Structured-output requirement must be explicit")
        _count(self.context_tokens); _count(self.output_tokens)
        if self.payload.owner_application != self.owner_application: raise ValueError("Payload belongs to a different app")
        if type(self.approved_routes) is not tuple or not self.approved_routes: raise ValueError("Request needs explicit approved routes")
        for route in self.approved_routes:
            _typed(route,ApprovedRoute)
            if (route.owner_application,route.feature,route.capability,route.model_profile_fingerprint) != (self.owner_application,self.feature,self.capability,self.model_profile.fingerprint):
                raise ValueError("Route approval does not match this feature/profile/app")
        if self.pinned_backend is not None and not any(route.backend_id == self.pinned_backend for route in self.approved_routes): raise ValueError("Pinned backend is not approved")
    @property
    def fingerprint(self):
        return _digest([self.id.value,self.owner_application.value,self.capability.value,
            self.model_profile.fingerprint,self.options.canonical_json,self.scope_fingerprint,
            self.resources.estimated_vram_bytes,self.resources.estimated_ram_bytes,
            self.resources.reserve_vram_bytes,self.resources.estimate_basis,self.resources.uses_gpu,
            self.pinned_backend.value if self.pinned_backend else None,self.memory_pressure_override,
            self.operation_id.value,self.correlation_id.value,self.feature.value,
            self.payload.reference.value,self.payload.fingerprint,self.payload.media_type,
            [[route.backend_id.value,route.connection_ref.value,route.data_audience_ref.value,route.approval_ref.value] for route in self.approved_routes],
            self.created_at.isoformat(),self.deadline_at.isoformat(),self.work_class.value,self.priority,
            self.protocol,self.requires_structured_output,self.context_tokens,self.output_tokens])

def backend_matches_request(backend: BackendInstance, request: TaskRequest) -> bool:
    """Compatibility only. This neither proves readiness nor reserves capacity."""
    if backend.contract_version != BACKEND_CONTRACT_VERSION: return False
    capability = next((item for item in backend.capabilities if item.id == request.capability),None)
    if capability is None or request.protocol not in capability.protocols: return False
    if request.requires_structured_output and capability.structured_output is not True: return False
    for requested,limit in ((request.context_tokens,capability.context_token_limit),(request.output_tokens,capability.output_token_limit)):
        if requested is not None and (limit is None or requested > limit): return False
    return ((request.pinned_backend is None or request.pinned_backend == backend.id)
        and any(route.backend_id == backend.id and route.connection_ref == backend.connection_ref for route in request.approved_routes)
        and any(profile.fingerprint == request.model_profile.fingerprint for profile in backend.model_profiles))

@dataclass(frozen=True, slots=True)
class ResourceTelemetry:
    group_id: ResourceGroupId
    observed_at: datetime
    valid_for: timedelta
    free_vram_bytes: int | None = None
    total_vram_bytes: int | None = None
    free_ram_bytes: int | None = None
    total_ram_bytes: int | None = None
    active_jobs: int | None = None
    queued_jobs: int | None = None
    def __post_init__(self):
        _typed(self.group_id,ResourceGroupId); _time(self.observed_at)
        if not isinstance(self.valid_for,timedelta) or self.valid_for.total_seconds() <= 0:
            raise ValueError("Telemetry validity must be positive")
        for label in ("free_vram_bytes","total_vram_bytes","free_ram_bytes","total_ram_bytes","active_jobs","queued_jobs"):
            _count(getattr(self,label))
        for free,total in ((self.free_vram_bytes,self.total_vram_bytes),(self.free_ram_bytes,self.total_ram_bytes)):
            if free is not None and total is not None and free > total: raise ValueError("Free capacity exceeds total")
    def fresh_at(self, now: datetime) -> bool:
        _time(now)
        return timedelta(0) <= now-self.observed_at <= self.valid_for
    def verified_idle_at(self, now: datetime) -> bool:
        return self.fresh_at(now) and self.active_jobs == 0 and self.queued_jobs == 0
    def verifies_headroom(self, required: ResourceRequirements, now: datetime) -> bool:
        if not self.fresh_at(now): return False
        if required.uses_gpu:
            if required.estimated_vram_bytes is None or self.free_vram_bytes is None: return False
            if self.free_vram_bytes < required.estimated_vram_bytes+required.reserve_vram_bytes: return False
        return (required.estimated_ram_bytes is not None and self.free_ram_bytes is not None
            and self.free_ram_bytes >= required.estimated_ram_bytes)

class JobState(str, Enum):
    UNKNOWN = "unknown"
    HELD = "held"
    ADMITTED = "admitted"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    OUTPUT_READY = "output_ready"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SUCCEEDED = "succeeded"

class FailureKind(str, Enum):
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"
    RESOURCE_PRESSURE = "resource_pressure"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    UNSUPPORTED_CANCEL = "unsupported_cancel"
    AMBIGUOUS_SUBMISSION = "ambiguous_submission"

class RetryDisposition(str, Enum):
    NEVER = "never"
    EXPLICIT_RECHECK = "explicit_recheck"
    QUERY_EXISTING = "query_existing_no_resubmit"

@dataclass(frozen=True, slots=True)
class TechnicalFailure:
    kind: FailureKind
    detail_code: StableId
    retry: RetryDisposition = RetryDisposition.NEVER
    def __post_init__(self):
        _typed(self.kind,FailureKind); _typed(self.detail_code,StableId); _typed(self.retry,RetryDisposition)
        if self.kind == FailureKind.AMBIGUOUS_SUBMISSION and self.retry != RetryDisposition.QUERY_EXISTING:
            raise ValueError("Ambiguous submission requires lookup, not resubmission")

@dataclass(frozen=True, slots=True)
class JobStatus:
    id: JobId
    task_id: TaskId
    owner_application: ApplicationId
    backend_id: BackendId
    connection_ref: ConnectionId
    state: JobState
    observed_at: datetime
    phase: str
    request_fingerprint: str
    operation_id: OperationId
    correlation_id: CorrelationId
    failure: TechnicalFailure | None = None
    completed_units: int | None = None
    total_units: int | None = None
    def __post_init__(self):
        for label,kind in (("id",JobId),("task_id",TaskId),("owner_application",ApplicationId),("backend_id",BackendId),("connection_ref",ConnectionId),("state",JobState),
                          ("operation_id",OperationId),("correlation_id",CorrelationId)):
            _typed(getattr(self,label),kind)
        _time(self.observed_at)
        if not isinstance(self.phase,str) or not _ID.fullmatch(self.phase): raise ValueError("Phase needs a content-free stable label")
        _count(self.completed_units); _count(self.total_units)
        if (self.completed_units is None) != (self.total_units is None): raise ValueError("Progress needs both counts or neither")
        if self.completed_units is not None and self.completed_units > self.total_units: raise ValueError("Completed units exceed total")
        if not isinstance(self.request_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.request_fingerprint): raise ValueError("Status needs request fingerprint")
        if self.failure is not None: _typed(self.failure,TechnicalFailure)
    @property
    def terminal(self) -> bool:
        return self.state in (JobState.CANCELLED,JobState.FAILED,JobState.TIMED_OUT,JobState.SUCCEEDED)

@dataclass(frozen=True, slots=True)
class CancellationRequest:
    job_id: JobId
    task_id: TaskId
    owner_application: ApplicationId
    backend_id: BackendId
    request_fingerprint: str
    operation_id: OperationId
    def __post_init__(self):
        for label,kind in (("job_id",JobId),("task_id",TaskId),("owner_application",ApplicationId),("backend_id",BackendId),("operation_id",OperationId)):
            _typed(getattr(self,label),kind)
        if not isinstance(self.request_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.request_fingerprint): raise ValueError("Cancellation needs request fingerprint")
    def matches(self, status: JobStatus) -> bool:
        return (self.job_id,self.task_id,self.owner_application,self.backend_id,self.request_fingerprint,self.operation_id) == (status.id,status.task_id,status.owner_application,status.backend_id,status.request_fingerprint,status.operation_id)

@dataclass(frozen=True, slots=True)
class CancellationOutcome:
    status: JobStatus
    supported: bool
    acknowledged: bool
    guarantee: ProviderCancellation
    def __post_init__(self):
        _typed(self.status,JobStatus); _typed(self.guarantee,ProviderCancellation)
        if type(self.supported) is not bool or type(self.acknowledged) is not bool: raise TypeError("Cancellation support/ack must be explicit")
        if self.acknowledged and not self.supported: raise ValueError("Unsupported cancellation cannot be acknowledged")
        if self.acknowledged and self.guarantee == ProviderCancellation.UNSUPPORTED: raise ValueError("An unsupported guarantee cannot acknowledge cancellation")
        if self.acknowledged and self.guarantee == ProviderCancellation.QUEUE_ONLY and self.status.state != JobState.CANCELLED:
            raise ValueError("Queue removal acknowledgement requires a cancelled job")

@dataclass(frozen=True, slots=True)
class ResultReceipt:
    """Provider completion/evidence references; never app-level delivery acceptance."""
    status: JobStatus
    model_profile_fingerprint: str
    raw_response: PrivatePayloadRef | None = None
    artifacts: tuple[PrivatePayloadRef, ...] = ()
    finish_reason: str | None = None
    actual_model_reference: str | None = None
    actual_model_revision: str | None = None
    effective_options: FrozenSettings | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    queue_seconds: float | None = None
    load_seconds: float | None = None
    inference_seconds: float | None = None
    def __post_init__(self):
        _typed(self.status,JobStatus)
        if not isinstance(self.model_profile_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.model_profile_fingerprint): raise ValueError("Result needs model-profile fingerprint")
        if type(self.artifacts) is not tuple: raise TypeError("Artifact references must be immutable")
        for ref in (*self.artifacts, *((self.raw_response,) if self.raw_response is not None else ())):
            _typed(ref,PrivatePayloadRef)
            if ref.owner_application != self.status.owner_application: raise ValueError("Output belongs to a different app")
        if self.effective_options is not None: _typed(self.effective_options,FrozenSettings)
        _count(self.input_tokens); _count(self.output_tokens)
        for value in (self.queue_seconds,self.load_seconds,self.inference_seconds):
            if value is not None:
                if type(value) not in (float,int) or value < 0: raise ValueError("Timing is nonnegative or unknown")
                json.dumps(value,allow_nan=False)
    def matches_request(self, request: TaskRequest) -> bool:
        """Match admitted identity and reported execution evidence, not delivery.

        Unknown actual model/options stay unknown; callers requiring that evidence
        must hold acceptance. A reported mismatch is never an exact match.
        """
        return ((self.status.task_id,self.status.owner_application,self.status.request_fingerprint,self.status.operation_id,self.status.correlation_id)
            == (request.id,request.owner_application,request.fingerprint,request.operation_id,request.correlation_id)
            and self.model_profile_fingerprint == request.model_profile.fingerprint
            and (self.actual_model_reference is None or self.actual_model_reference == request.model_profile.model_reference)
            and (self.actual_model_revision is None or self.actual_model_revision == request.model_profile.revision)
            and (self.effective_options is None or self.effective_options == request.options)
            and any(route.backend_id == self.status.backend_id and route.connection_ref == self.status.connection_ref for route in request.approved_routes))

@dataclass(frozen=True, slots=True)
class ResourceLease:
    id: LeaseId
    task_id: TaskId
    owner_application: ApplicationId
    request_fingerprint: str
    groups: frozenset[ResourceGroupId]
    issued_at: datetime
    expires_at: datetime
    def __post_init__(self):
        _typed(self.id,LeaseId); _typed(self.task_id,TaskId); _typed(self.owner_application,ApplicationId)
        if type(self.groups) is not frozenset or not self.groups: raise ValueError("Lease needs immutable resource groups")
        for group in self.groups: _typed(group,ResourceGroupId)
        if not isinstance(self.request_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.request_fingerprint): raise ValueError("Lease needs request fingerprint")
        _time(self.issued_at); _time(self.expires_at)
        if self.expires_at <= self.issued_at: raise ValueError("Lease expiry must follow issue")
    def valid_for(self, request: TaskRequest, now: datetime) -> bool:
        _time(now)
        return (self.issued_at <= now < self.expires_at and self.task_id == request.id
            and self.owner_application == request.owner_application and self.request_fingerprint == request.fingerprint)
    def matches_status(self, status: JobStatus) -> bool:
        """Identity check only; coordinator must also check its recorded job ID."""
        return (self.task_id, self.owner_application, self.request_fingerprint) == (status.task_id, status.owner_application, status.request_fingerprint)
    def covers_backend(self, backend: BackendInstance) -> bool:
        return bool(backend.resource_groups) and self.groups == backend.resource_groups

@dataclass(frozen=True, slots=True)
class AdmissionHold:
    request_fingerprint: str
    reason: str
    groups: frozenset[ResourceGroupId]
    def __post_init__(self):
        if not isinstance(self.request_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",self.request_fingerprint): raise ValueError("Hold needs request fingerprint")
        _text(self.reason,"reason")
        if type(self.groups) is not frozenset: raise TypeError("Hold groups must be immutable")
        for group in self.groups: _typed(group,ResourceGroupId)

class BackendRegistry(Protocol):
    def instances(self) -> tuple[BackendInstance, ...]: ...
    def resource_groups(self) -> tuple[ResourceGroup, ...]: ...

class ResourceCoordinator(Protocol):
    """Reservation implementations must serialize claims across resource aliases.

    Lease expiry alone is not permission to evict or reuse an active job's GPU.
    An explicit override does not authorize external cancellation/unloading.
    Bind one exact job ID/backend/connection and request identity before execution.
    A bound lease releases only after that same job proves terminal and drained;
    timeout/failure with ambiguous provider activity is not drain evidence. Only
    an unbound reservation can release without terminal status. Release is
    idempotent and must reject forged, foreign or changed leases/statuses.
    """
    def reserve(self, request: TaskRequest, backend: BackendInstance) -> ResourceLease | AdmissionHold: ...
    def bind_job(self, lease: ResourceLease, status: JobStatus) -> bool: ...
    def release(self, lease: ResourceLease, terminal_status: JobStatus | None = None) -> bool: ...

class JobProvider(Protocol):
    """Adapters validate approved references and manager-issued leases, bind the
    exact job before execution, preserve payload/options, and refuse unsupported
    cancellation. Lost submission responses require lookup, not silent replay.
    Lease groups must exactly cover the chosen backend, with verified physical
    identities. Status/result lookups are owner-authorized by the adapter's bound
    caller context. Queue-only cancellation cannot stop starting/running work.
    """
    def submit(self, request: TaskRequest, lease: ResourceLease) -> JobStatus: ...
    def status(self, job_id: JobId) -> JobStatus: ...
    def result(self, job_id: JobId) -> ResultReceipt: ...
    def cancel(self, request: CancellationRequest) -> CancellationOutcome: ...
