"""Read-only job facts and owner-supplied explanations; no queue or policy engine."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from ..backend_contracts import (
    AdmissionHold, ApplicationId, BackendInstance, CancellationOutcome, FailureKind,
    FrozenSettings, JobState, JobStatus, ProviderCancellation, ResourceGroup, ResourceGroupId, StableId, TaskRequest,
)
from ..discovery.swarm_readonly import DiscoveryState
from .status_view import Freshness, MetadataSnapshot, ResourceSnapshot, _freshness

MODULE_ID = "collectigen.suite-job-availability-view"
MODULE_VERSION = "0.1.0-alpha.2"
JOB_VIEW_VERSION = 1
MAX_JOBS = 128
MAX_FACTS = 32
MAX_VALIDITY = timedelta(seconds=30)


def _require(value, code):
    if not value: raise ValueError(code)


def _observation(at, validity):
    _require(isinstance(at,datetime) and at.tzinfo is not None and at.utcoffset() is not None,"aware_timestamp_required")
    _require(type(validity) is timedelta and timedelta(0)<validity<=MAX_VALIDITY,"invalid_observation_validity")


def _binding(status):
    return (status.id,status.task_id,status.owner_application,status.backend_id,status.connection_ref,
            status.request_fingerprint,status.operation_id,status.correlation_id)


def _request_matches(status,request):
    return (status.task_id,status.owner_application,status.request_fingerprint,status.operation_id,status.correlation_id)==(
        request.id,request.owner_application,request.fingerprint,request.operation_id,request.correlation_id) and any(
        route.backend_id==status.backend_id and route.connection_ref==status.connection_ref for route in request.approved_routes)


@dataclass(frozen=True, slots=True)
class ProviderControlEvidence:
    backend: BackendInstance
    source_ref: StableId
    observed_at: datetime
    valid_for: timedelta
    qualification_ref: StableId | None = None

    def __post_init__(self):
        _require(type(self.backend) is BackendInstance and type(self.source_ref) is StableId,"invalid_control_evidence")
        _require(self.qualification_ref is None or type(self.qualification_ref) is StableId,"invalid_control_qualification")
        _require(len(self.backend.capabilities)<=64 and len(self.backend.model_profiles)<=128
                 and len(self.backend.resource_groups)<=16,"backend_evidence_bound")
        _observation(self.observed_at,self.valid_for)


class ExplanationKind(str, Enum):
    RESOURCE_IN_USE = "resource_in_use"
    WAITING_FOR_RESOURCE = "waiting_for_resource"
    MODEL_ABSENT = "model_absent"
    INCOMPATIBLE = "incompatible"
    RESOURCE_VALUES_UNKNOWN = "resource_values_unknown"
    MEMORY_HOLD = "memory_hold"
    BACKEND_UNAVAILABLE = "backend_unavailable"


@dataclass(frozen=True, slots=True)
class AvailabilityFact:
    """Explicit host/adapter mapping, never parsed from AdmissionHold.reason text."""
    kind: ExplanationKind
    binding: JobStatus
    source_ref: StableId | None
    observed_at: datetime
    valid_for: timedelta
    detail_code: StableId | None = None
    backend: BackendInstance | None = None
    resource_group: ResourceGroup | None = None
    resource: ResourceSnapshot | None = None
    hold: AdmissionHold | None = field(default=None, repr=False)
    metadata: MetadataSnapshot | None = None
    profile_fingerprint: str | None = None
    catalog_complete: bool = False
    profile_present: bool | None = None
    mismatch_confirmed: bool | None = None
    estimate_source_ref: StableId | None = None
    requirement_request_fingerprint: str | None = None
    workload_ref: StableId | None = None

    def __post_init__(self):
        _require(type(self.kind) is ExplanationKind and type(self.binding) is JobStatus,"invalid_availability_fact")
        for value in (self.source_ref,self.detail_code,self.estimate_source_ref,self.workload_ref):
            _require(value is None or type(value) is StableId,"invalid_fact_reference")
        for value,kind in ((self.backend,BackendInstance),(self.resource_group,ResourceGroup),
                           (self.resource,ResourceSnapshot),(self.hold,AdmissionHold),(self.metadata,MetadataSnapshot)):
            _require(value is None or type(value) is kind,"invalid_native_fact")
        _require(type(self.catalog_complete) is bool and self.profile_present in (None,True,False)
                 and (self.profile_present is None or type(self.profile_present) is bool)
                 and (self.mismatch_confirmed is None or type(self.mismatch_confirmed) is bool),"invalid_fact_assertion")
        for value in (self.profile_fingerprint,self.requirement_request_fingerprint):
            _require(value is None or (type(value) is str and len(value)==64 and all(c in "0123456789abcdef" for c in value)),"invalid_fact_fingerprint")
        if self.backend is not None:
            _require(len(self.backend.capabilities)<=64 and len(self.backend.model_profiles)<=128
                     and len(self.backend.resource_groups)<=16,"backend_evidence_bound")
        if self.resource is not None:
            telemetry = self.resource.telemetry
            _require(all(value is None or value<=2**63-1 for value in (
                telemetry.free_vram_bytes,telemetry.total_vram_bytes,telemetry.free_ram_bytes,
                telemetry.total_ram_bytes,telemetry.active_jobs,telemetry.queued_jobs)),"resource_projection_bound")
        _observation(self.observed_at,self.valid_for)


@dataclass(frozen=True, slots=True)
class JobViewSource:
    request: TaskRequest = field(repr=False)
    submitted: JobStatus | None
    status: JobStatus | None
    source_ref: StableId | None
    valid_for: timedelta
    control: ProviderControlEvidence | None = None
    cancellation: CancellationOutcome | None = None
    cancellation_source_ref: StableId | None = None
    facts: tuple[AvailabilityFact, ...] = ()

    def __post_init__(self):
        _require(type(self.request) is TaskRequest,"task_request_required")
        _require(all(value is None or type(value) is JobStatus for value in (self.submitted,self.status)),"invalid_job_status")
        _require(self.source_ref is None or type(self.source_ref) is StableId,"invalid_job_source")
        _require(self.control is None or type(self.control) is ProviderControlEvidence,"invalid_control_source")
        _require(self.cancellation is None or type(self.cancellation) is CancellationOutcome,"invalid_cancel_outcome")
        _require(self.cancellation_source_ref is None or type(self.cancellation_source_ref) is StableId,"invalid_cancel_source")
        _require(type(self.facts) is tuple and len(self.facts)<=MAX_FACTS
                 and all(type(item) is AvailabilityFact for item in self.facts),"availability_fact_bound")
        _observation(self.request.created_at,self.valid_for)
        _require(len(self.request.approved_routes)<=16 and abs(self.request.priority)<=2**63-1
                 and len(self.request.options.canonical_json)<=16384
                 and len(self.request.model_profile.runtime_settings.canonical_json)<=16384,"request_projection_bound")
        _require(all(value is None or value<=2**63-1 for value in (
            self.request.resources.estimated_vram_bytes,self.request.resources.estimated_ram_bytes,
            self.request.resources.reserve_vram_bytes)),"requirement_projection_bound")
        for status in (self.submitted,self.status,None if self.cancellation is None else self.cancellation.status):
            if status is not None:
                _require(all(value is None or value<=2**63-1 for value in (status.completed_units,status.total_units)),"progress_projection_bound")


@dataclass(frozen=True, slots=True)
class ReasonView:
    kind: ExplanationKind
    established: bool
    evidence_state: str
    source_ref: StableId | None
    observed_at: datetime
    freshness: Freshness
    detail_code: StableId | None
    resource_group_id: ResourceGroupId | None
    resource: ResourceSnapshot | None = None
    resource_freshness: Freshness = Freshness.MISSING
    estimated_vram_bytes: int | None = None
    reserve_vram_bytes: int | None = None
    estimate_source_ref: StableId | None = None
    workload_ref: StableId | None = None

    def as_ui(self):
        telemetry=None if self.resource is None else self.resource.telemetry
        return {"category":self.kind.value,"established":self.established,"evidence_state":self.evidence_state,
            "source_ref":None if self.source_ref is None else self.source_ref.value,
            "observed_at":self.observed_at.isoformat(),"freshness":self.freshness.value,
            "detail_code":None if self.detail_code is None else self.detail_code.value,
            "resource_group_id":None if self.resource_group_id is None else self.resource_group_id.value,
            "resource_observed_at":None if telemetry is None else telemetry.observed_at.isoformat(),
            "resource_source_ref":None if self.resource is None else self.resource.source_ref.value,
            "resource_freshness":self.resource_freshness.value,
            "observed_free_vram_bytes":telemetry.free_vram_bytes if self.established and self.kind is ExplanationKind.MEMORY_HOLD else None,
            "estimated_vram_bytes":self.estimated_vram_bytes,"reserve_vram_bytes":self.reserve_vram_bytes,
            "estimate_source_ref":None if self.estimate_source_ref is None else self.estimate_source_ref.value,
            "workload_ref":None if self.workload_ref is None else self.workload_ref.value,
            "activity_owner":None,"admission_decision_computed":False}


def _reason(fact,source,now):
    request,submitted=source.request,source.submitted
    freshness=_freshness(fact.observed_at,fact.valid_for,now)
    state="reason_not_established";known=False;resource=None;rf=Freshness.MISSING
    estimate=reserve=None;estimate_source=workload=None
    scope=submitted is not None and _binding(fact.binding)==_binding(submitted)
    backend=fact.backend
    target=backend is not None and submitted is not None and (backend.id,backend.connection_ref)==(submitted.backend_id,submitted.connection_ref)
    group=fact.resource_group
    group_scope=target and group is not None and group.host_id==backend.host_id and group.id in backend.resource_groups
    native=fact.resource
    if scope and group_scope and native is not None and native.telemetry.group_id==group.id:
        resource=native;rf=_freshness(native.telemetry.observed_at,native.telemetry.valid_for,now)
    eligible=scope and fact.source_ref is not None and freshness is Freshness.FRESH
    if eligible:
        live_group=resource is not None and group.identity_verified and rf is Freshness.FRESH
        hold=fact.hold
        hold_scope=(target and hold is not None and hold.request_fingerprint==request.fingerprint
                    and hold.groups<=backend.resource_groups and group is not None and group.id in hold.groups)
        if fact.kind in (ExplanationKind.RESOURCE_IN_USE,ExplanationKind.WAITING_FOR_RESOURCE):
            busy=live_group and ((native.telemetry.active_jobs or 0)>0 or (native.telemetry.queued_jobs or 0)>0)
            known=bool(busy and (fact.kind is ExplanationKind.RESOURCE_IN_USE or hold_scope))
        elif fact.kind is ExplanationKind.RESOURCE_VALUES_UNKNOWN:
            required=() if resource is None else (
                native.telemetry.active_jobs,native.telemetry.queued_jobs,
                *((native.telemetry.free_vram_bytes,native.telemetry.total_vram_bytes) if request.resources.uses_gpu else ()),
                *((native.telemetry.free_ram_bytes,native.telemetry.total_ram_bytes)
                  if not request.resources.uses_gpu or request.resources.estimated_ram_bytes is not None else ()))
            known=bool(group_scope and (resource is None or not group.identity_verified
                       or rf is not Freshness.FRESH or any(value is None for value in required)))
        elif fact.kind is ExplanationKind.MODEL_ABSENT:
            known=bool(target and fact.catalog_complete and fact.profile_present is False
                       and fact.profile_fingerprint==request.model_profile.fingerprint and fact.detail_code is not None)
        elif fact.kind is ExplanationKind.INCOMPATIBLE:
            known=bool(target and fact.mismatch_confirmed is True and fact.detail_code is not None
                       and fact.profile_fingerprint==request.model_profile.fingerprint
                       and any(cap.id==request.capability for cap in backend.capabilities))
        elif fact.kind is ExplanationKind.MEMORY_HOLD:
            known=bool(live_group and hold_scope and fact.detail_code is not None
                       and fact.estimate_source_ref is not None and fact.workload_ref is not None
                       and fact.requirement_request_fingerprint==request.fingerprint
                       and request.resources.uses_gpu and request.resources.estimated_vram_bytes is not None
                       and native.telemetry.free_vram_bytes is not None)
            if known:
                estimate=request.resources.estimated_vram_bytes;reserve=request.resources.reserve_vram_bytes
                estimate_source=fact.estimate_source_ref;workload=fact.workload_ref
        elif fact.kind is ExplanationKind.BACKEND_UNAVAILABLE:
            meta=fact.metadata
            known=bool(meta is not None and (meta.owner,meta.connection_ref)==(request.owner_application,submitted.connection_ref)
                       and _freshness(meta.observed_at,meta.valid_for,now) is Freshness.FRESH
                       and (meta.state is DiscoveryState.UNAVAILABLE or
                            meta.failure_kind in (FailureKind.OFFLINE,FailureKind.UNAVAILABLE,FailureKind.TRANSPORT)))
        if known:state="supplied_scoped_evidence"
    return ReasonView(fact.kind,known,state,fact.source_ref if scope else None,fact.observed_at,
                      freshness,fact.detail_code if known else None,group.id if scope and group_scope else None,
                      resource,rf,estimate,reserve,estimate_source,workload)


@dataclass(frozen=True, slots=True)
class _BoundProjection:
    identity: JobStatus | None
    request: TaskRequest = field(repr=False)
    status: JobStatus | None
    source_ref: StableId | None
    freshness: Freshness
    declared_guarantee: ProviderCancellation | None
    control_qualified: bool
    cancel_eligible: bool | None
    cancellation: CancellationOutcome | None
    cancellation_freshness: Freshness
    reasons: tuple[ReasonView, ...]
    control: ProviderControlEvidence | None
    valid_for: timedelta
    cancellation_source_ref: StableId | None

    def as_ui(self):
        request=self.request;last=self.status or self.identity
        current=self.status is not None and self.freshness is Freshness.FRESH
        state=self.status.state if current else JobState.UNKNOWN
        outcome=self.cancellation;ack_fresh=self.cancellation_freshness is Freshness.FRESH
        best_effort=outcome is not None and outcome.guarantee is ProviderCancellation.BEST_EFFORT
        may_finish=None
        if best_effort and current:
            if self.status.terminal or state is JobState.OUTPUT_READY:may_finish=False
            elif ack_fresh and outcome.supported and outcome.acknowledged:may_finish=True
        return {"interface_version":JOB_VIEW_VERSION,"requesting_app":request.owner_application.value,
            "feature":request.feature.value,"task_id":request.id.value,"operation_id":request.operation_id.value,
            "correlation_id":request.correlation_id.value,"request_fingerprint":request.fingerprint,
            "job_id":None if self.identity is None else self.identity.id.value,
            "backend_id":None if self.identity is None else self.identity.backend_id.value,
            "connection_ref":None if self.identity is None else self.identity.connection_ref.value,
            "requested_profile_id":request.model_profile.id.value,"requested_profile_fingerprint":request.model_profile.fingerprint,
            "current_status":state.value,"last_observed_status":None if last is None else last.state.value,
            "phase":None if last is None else last.phase,"observed_at":None if last is None else last.observed_at.isoformat(),
            "source_ref":None if self.source_ref is None else self.source_ref.value,"freshness":self.freshness.value,
            "valid_for_seconds":self.valid_for.total_seconds(),
            "completed_units":None if last is None else last.completed_units,"total_units":None if last is None else last.total_units,
            "progress_is_current":current,"completion_percent":None,"eta_seconds":None,"runtime_seconds":None,
            "declared_priority":request.priority,"work_class":request.work_class.value,"queue_position":None,"effective_rank":None,
            "provider_complete":True if state is JobState.SUCCEEDED else None,"app_acceptance":None,"delivery":None,
            "declared_cancellation":None if self.declared_guarantee is None else self.declared_guarantee.value,
            "control_source_ref":None if self.control is None else self.control.source_ref.value,
            "control_observed_at":None if self.control is None else self.control.observed_at.isoformat(),
            "control_valid_for_seconds":None if self.control is None else self.control.valid_for.total_seconds(),
            "control_qualification_ref":None if self.control is None or self.control.qualification_ref is None else self.control.qualification_ref.value,
            "control_behavior_qualified":self.control_qualified,"cancel_eligible_for_observed_state":self.cancel_eligible,
            "controls_enabled":False,"control_disabled_reason":"read_only_inspection",
            "cancel_acknowledged":None if outcome is None else outcome.acknowledged,
            "cancel_supported_outcome":None if outcome is None else outcome.supported,
            "cancel_outcome_guarantee":None if outcome is None else outcome.guarantee.value,
            "cancel_observed_at":None if outcome is None else outcome.status.observed_at.isoformat(),
            "cancel_freshness":self.cancellation_freshness.value,
            "cancellation_source_ref":None if self.cancellation_source_ref is None else self.cancellation_source_ref.value,
            "queued_removal_confirmed":True if outcome is not None and ack_fresh and self.control_qualified and self.declared_guarantee is ProviderCancellation.QUEUE_ONLY and state is JobState.CANCELLED and outcome.acknowledged
                and outcome.guarantee is ProviderCancellation.QUEUE_ONLY and outcome.status.state is JobState.CANCELLED else None,
            "best_effort_guarantee_recorded":None if outcome is None else best_effort,
            "provider_may_finish":may_finish,
            "resource_drained":None,"lease_released":None,"automatic_retry":False,
            "existing_binding_query_hint":self.identity is not None and last is not None and last.failure is not None
                and last.failure.kind is FailureKind.AMBIGUOUS_SUBMISSION,
            "failure_kind":None if last is None or last.failure is None else last.failure.kind.value,
            "failure_detail_code":None if last is None or last.failure is None else last.failure.detail_code.value,
            "availability": [reason.as_ui() for reason in self.reasons]}


@dataclass(frozen=True, slots=True)
class JobRow:
    """Detached immutable technical projection: never retains requests or payloads."""
    data: FrozenSettings = field(repr=False)

    def as_ui(self):
        return self.data.to_mapping()


def project_job(source,*,now):
    _require(type(source) is JobViewSource,"job_view_source_required");_observation(now,MAX_VALIDITY)
    request,submitted,status=source.request,source.submitted,source.status
    if submitted is not None:_require(_request_matches(submitted,request),"submitted_request_mismatch")
    if status is not None:_require(submitted is not None and _binding(status)==_binding(submitted),"job_binding_mismatch")
    if source.cancellation is not None:
        _require(submitted is not None and _binding(source.cancellation.status)==_binding(submitted),"cancel_binding_mismatch")
        _require(source.cancellation.status.observed_at>=submitted.observed_at,"cancel_predates_binding")
    if status is not None:
        _require(status.observed_at>=submitted.observed_at,"status_predates_binding")
    freshness=Freshness.MISSING if status is None or source.source_ref is None else _freshness(status.observed_at,source.valid_for,now)
    guarantee=None;qualified=False;eligible=None
    control=source.control
    if control is not None:
        _require(submitted is not None and (control.backend.id,control.backend.connection_ref)==(
            submitted.backend_id,submitted.connection_ref),"control_binding_mismatch")
        capability=next((cap for cap in control.backend.capabilities if cap.id==request.capability),None)
        if capability is not None:
            guarantee=capability.provider_cancellation
            qualified=control.qualification_ref is not None and _freshness(control.observed_at,control.valid_for,now) is Freshness.FRESH and (guarantee is not ProviderCancellation.QUEUE_ONLY or capability.queue_removal)
            if freshness is Freshness.FRESH:
                if guarantee is ProviderCancellation.UNSUPPORTED and _freshness(control.observed_at,control.valid_for,now) is Freshness.FRESH:eligible=False
                elif qualified:
                    eligible=(capability.queue_removal and status.state is JobState.QUEUED) if guarantee is ProviderCancellation.QUEUE_ONLY else (
                        status.state in (JobState.ADMITTED,JobState.QUEUED,JobState.STARTING,JobState.RUNNING))
    cancellation=source.cancellation
    cf=Freshness.MISSING if cancellation is None or source.cancellation_source_ref is None else _freshness(
        cancellation.status.observed_at,source.valid_for,now)
    projection=_BoundProjection(submitted,request,status,source.source_ref,freshness,guarantee,qualified,eligible,
                  cancellation,cf,tuple(_reason(fact,source,now) for fact in source.facts),control,source.valid_for,source.cancellation_source_ref)
    return JobRow(FrozenSettings.from_mapping(projection.as_ui()))


def project_queue(sources,allowed_apps,*,now):
    _require(type(sources) is tuple and len(sources)<=MAX_JOBS and all(type(item) is JobViewSource for item in sources),"queue_snapshot_bound")
    _require(type(allowed_apps) is frozenset and len(allowed_apps)<=64 and all(type(item) is ApplicationId for item in allowed_apps),"authorized_app_scope_required")
    scoped=tuple(source for source in sources if source.request.owner_application in allowed_apps)
    keys=[(source.request.owner_application,source.request.id) for source in scoped]
    jobs=[(source.submitted.backend_id,source.submitted.connection_ref,source.submitted.id) for source in scoped if source.submitted is not None]
    _require(len(keys)==len(set(keys)) and len(jobs)==len(set(jobs)),"duplicate_queue_binding")
    return tuple(project_job(source,now=now) for source in scoped)
