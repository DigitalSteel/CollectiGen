"""Bounded pure Swarm translation. No transport, scheduler, files or activation.

Policy is supplied by the embedding trusted host, never by a wire payload.
Inputs are private bytes; coordination receives only core metadata/references.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
import math
from urllib.parse import urljoin, urlsplit

from ..backend_contracts import (
    ApplicationId, ApprovedRoute, BackendId, BackendInstance, ConnectionId,
    FrozenSettings, JobId, JobState, JobStatus, ModelProfile, PrivatePayloadRef,
    ResourceGroup, ResultReceipt, StableId, TaskRequest, TechnicalFailure, backend_matches_request,
)

SETTINGS_BYTES = 16 * 1024
PAYLOAD_BYTES = 1024 * 1024
RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 16
MAX_COLLECTION = 256
MAX_CAPABILITIES = 64
MAX_PROFILES = 128
MAX_RESOURCES = 16
MAX_ROUTES = 16
MAX_ARTIFACTS = 64
MAX_JOBS = 64
MAX_RESULTS = 64
RETAINED_BYTES = 32 * 1024 * 1024
_IMAGE_FIELDS = frozenset({"images", "prompt", "negativeprompt", "model", "width", "height",
                           "steps", "cfgscale", "seed", "sampler", "scheduler", "session_id"})
_REQUIRED_FIELDS = _IMAGE_FIELDS - {"session_id"}


class BoundaryError(ValueError):
    """Stable content-free boundary failure; no private values in messages."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise BoundaryError(code)


def _time(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None, "invalid_timestamp")


def _text(value):
    if type(value) is not str or not value or len(value) > 512:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _pairs(pairs):
    _require(len(pairs) <= MAX_COLLECTION, "collection_limit")
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _check_json_tree(value):
    # Iterative: never recurse into unchecked user collections.
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if type(item) in (dict, list):
            _require(depth <= MAX_DEPTH, "depth_limit")
            _require(len(item) <= MAX_COLLECTION, "collection_limit")
            values = item.values() if type(item) is dict else item
            if type(item) is dict:
                for key in item:
                    try: key.encode("utf-8")
                    except UnicodeError: raise BoundaryError("invalid_json_unicode") from None
            pending.extend((child, depth + 1) for child in values)
        elif type(item) is str:
            try: item.encode("utf-8")
            except UnicodeError: raise BoundaryError("invalid_json_unicode") from None
        elif type(item) is float:
            _require(math.isfinite(item), "nonfinite_json")
        else:
            _require(item is None or type(item) in (str, int, bool), "invalid_json_value")


def _object(raw, max_bytes):
    _require(type(raw) is bytes, "private_bytes_required")
    _require(len(raw) <= max_bytes, "byte_limit")
    # Bound nesting before json.loads can recurse. Braces inside quoted strings
    # are data; JSON syntax and duplicate/finite checks follow the lexical guard.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            _require(depth <= MAX_DEPTH, "depth_limit")
        elif byte in (93, 125):
            depth -= 1
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(BoundaryError("nonfinite_json")))
    except BoundaryError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise BoundaryError("malformed_json") from None
    _require(type(value) is dict, "json_object_required")
    _check_json_tree(value)
    return value


def bounded_settings(raw: bytes) -> FrozenSettings:
    """Use before constructing core settings from untrusted JSON."""
    return FrozenSettings.from_mapping(_object(raw, SETTINGS_BYTES))


def _settings(value):
    _require(type(value) is FrozenSettings, "settings_required")
    try:
        raw = value.canonical_json.encode("utf-8")
    except UnicodeError:
        raise BoundaryError("invalid_json_unicode") from None
    return _object(raw, SETTINGS_BYTES)


def _equal_json(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _urlparts(uri):
    try:
        return urlsplit(uri)
    except (ValueError, UnicodeError):
        raise BoundaryError("invalid_artifact_uri") from None


def _origin(uri):
    _require(_text(uri) and not any(c.isspace() for c in uri) and "\\" not in uri, "invalid_artifact_uri")
    try:
        parts = _urlparts(uri)
        _require(parts.scheme in ("http", "https") and bool(parts.hostname)
                 and parts.username is None and parts.password is None and not parts.fragment,
                 "invalid_artifact_uri")
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        _require(1 <= port <= 65535, "invalid_artifact_uri")
    except (ValueError, UnicodeError):
        raise BoundaryError("invalid_artifact_uri") from None
    return (parts.scheme, parts.hostname.lower(), port)


class SwarmWire(str, Enum):
    SYNC = "swarm.sync.t2i"
    WEBSOCKET = "swarm.websocket.t2i"

    @property
    def route(self):
        return "GenerateText2Image" if self is SwarmWire.SYNC else "GenerateText2ImageWS"


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Immutable host-owned approvals; not a token or payload authorization."""
    owner: ApplicationId
    backend_id: BackendId
    connection_ref: ConnectionId
    approved_routes: tuple[ApprovedRoute, ...]
    profiles: tuple[ModelProfile, ...]
    resources: tuple[ResourceGroup, ...]
    allowed_payload_fields: frozenset[str]
    expected_server_id: str
    artifact_origin: str = field(repr=False)
    allowed_artifact_origins: frozenset[str] = field(repr=False)

    def __post_init__(self):
        for value, kind in ((self.owner, ApplicationId), (self.backend_id, BackendId),
                            (self.connection_ref, ConnectionId)):
            _require(type(value) is kind, "invalid_policy_identity")
        for values, kind, maximum in ((self.approved_routes, ApprovedRoute, MAX_ROUTES),
                (self.profiles, ModelProfile, MAX_PROFILES), (self.resources, ResourceGroup, MAX_RESOURCES)):
            _require(type(values) is tuple and 0 < len(values) <= maximum, "policy_collection_limit")
            _require(all(type(item) is kind for item in values), "invalid_policy_record")
        _require(type(self.allowed_payload_fields) is frozenset and
                 _REQUIRED_FIELDS <= self.allowed_payload_fields and
                 len(self.allowed_payload_fields) <= MAX_COLLECTION and
                 all(_text(key) for key in self.allowed_payload_fields), "invalid_field_policy")
        _require(_text(self.expected_server_id), "invalid_server_policy")
        _require(type(self.allowed_artifact_origins) is frozenset and
                 0 < len(self.allowed_artifact_origins) <= MAX_ARTIFACTS, "invalid_artifact_policy")
        for origin in self.allowed_artifact_origins | {self.artifact_origin}:
            parsed = _urlparts(origin)
            _require(parsed.path in ("", "/") and not parsed.query, "invalid_artifact_policy")
            _origin(origin)
        _require(_origin(self.artifact_origin) in {_origin(item) for item in self.allowed_artifact_origins},
                 "unapproved_artifact_origin")
        physical = {}
        for group in self.resources:
            if group.identity_verified:
                key = (group.host_id, group.device_reference)
                _require(key not in physical or physical[key] == group.id, "conflicting_resource_alias")
                physical[key] = group.id
        _require(len({group.id for group in self.resources}) == len(self.resources), "duplicate_resource_id")
        _require(len({profile.id for profile in self.profiles}) == len(self.profiles), "duplicate_profile_id")
        for profile in self.profiles:
            _require(all(_text(item) for item in (profile.family, profile.model_reference, profile.revision)),
                     "invalid_profile_metadata")
            _settings(profile.runtime_settings)
        for route in self.approved_routes:
            _require((route.owner_application, route.backend_id, route.connection_ref) ==
                     (self.owner, self.backend_id, self.connection_ref), "policy_route_mismatch")
            _require(any(p.fingerprint == route.model_profile_fingerprint for p in self.profiles),
                     "policy_profile_mismatch")


class ProbeState(str, Enum):
    COMPATIBLE = "compatible"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    STALE = "stale"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ProbeReport:
    connection_ref: ConnectionId
    state: ProbeState
    observed_at: datetime
    version: str | None = None
    server_id: str | None = None
    backend_ids: tuple[str, ...] = ()
    resource_identity_verified: bool = False
    # Session status/synthetic backend descriptors do not establish global claims.
    global_activity_known: bool = False


def normalize_probe(raw: bytes, policy: HostPolicy, observed_at: datetime,
                    now: datetime, valid_for: timedelta) -> ProbeReport:
    _require(type(policy) is HostPolicy, "host_policy_required")
    _time(observed_at); _time(now)
    _require(type(valid_for) is timedelta and valid_for.total_seconds() > 0, "invalid_freshness")
    try:
        value = _object(raw, PAYLOAD_BYTES)
    except BoundaryError as error:
        if error.code in ("byte_limit", "depth_limit", "collection_limit"):
            raise
        return ProbeReport(policy.connection_ref, ProbeState.MALFORMED, observed_at)
    if not timedelta(0) <= now - observed_at <= valid_for:
        return ProbeReport(policy.connection_ref, ProbeState.STALE, observed_at)
    if value.get("offline") is True:
        return ProbeReport(policy.connection_ref, ProbeState.OFFLINE, observed_at)
    session, params, backends = value.get("session"), value.get("parameters"), value.get("backends")
    if not all(type(item) is dict for item in (session, params, backends)):
        return ProbeReport(policy.connection_ref, ProbeState.UNKNOWN, observed_at)
    version, server = session.get("version"), session.get("server_id")
    if not _text(version) or not _text(server) or not _text(session.get("session_id")):
        return ProbeReport(policy.connection_ref, ProbeState.MALFORMED, observed_at)
    rows = params.get("list")
    if type(rows) is not list or any(type(row) is not dict or not _text(row.get("id")) for row in rows):
        return ProbeReport(policy.connection_ref, ProbeState.MALFORMED, observed_at)
    identifiers = [row["id"] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        return ProbeReport(policy.connection_ref, ProbeState.MALFORMED, observed_at)
    if server != policy.expected_server_id or not (_REQUIRED_FIELDS - {"images"}) <= set(identifiers):
        return ProbeReport(policy.connection_ref, ProbeState.UNSUPPORTED, observed_at, version, server)
    ready, supported = [], False
    for key, row in backends.items():
        if type(row) is not dict or not _text(key):
            return ProbeReport(policy.connection_ref, ProbeState.MALFORMED, observed_at, version, server)
        if row.get("type") == "comfyui_selfstart":
            supported = True
            if row.get("enabled") is True and row.get("status") in ("running", "idle"):
                ready.append(key)
    state = ProbeState.COMPATIBLE if ready else ProbeState.UNAVAILABLE if supported else ProbeState.UNSUPPORTED
    return ProbeReport(policy.connection_ref, state, observed_at, version, server, tuple(ready),
                       all(group.identity_verified for group in policy.resources))


@dataclass(frozen=True, slots=True)
class WirePlan:
    request: TaskRequest = field(repr=False)
    backend: BackendInstance = field(repr=False)
    policy: HostPolicy = field(repr=False)
    wire: SwarmWire
    admitted_payload: bytes = field(repr=False)
    private_payload: bytes = field(repr=False)
    enrichments: tuple[str, ...] = ()

    @property
    def route(self):
        return self.wire.route


def translate_request(request: TaskRequest, backend: BackendInstance, raw_payload: bytes,
                      policy: HostPolicy, wire: SwarmWire, *, session_envelope: bytes | None = None) -> WirePlan:
    _require(type(policy) is HostPolicy and type(wire) is SwarmWire, "host_policy_required")
    _require(type(request) is TaskRequest and type(backend) is BackendInstance, "core_records_required")
    _require(len(backend.capabilities) <= MAX_CAPABILITIES and len(backend.model_profiles) <= MAX_PROFILES
             and 0 < len(backend.resource_groups) <= MAX_RESOURCES and len(request.approved_routes) <= MAX_ROUTES,
             "declaration_limit")
    for profile in backend.model_profiles:
        _require(all(_text(item) for item in (profile.family, profile.model_reference, profile.revision)),
                 "invalid_profile_metadata")
        _settings(profile.runtime_settings)
    _require(all(len(cap.protocols) <= MAX_COLLECTION for cap in backend.capabilities), "declaration_limit")
    options = _settings(request.options)
    _settings(request.model_profile.runtime_settings)
    _require((request.owner_application, backend.id, backend.connection_ref) ==
             (policy.owner, policy.backend_id, policy.connection_ref), "unapproved_connection")
    _require(request.capability.value == "image" and request.protocol == wire.value
             and backend_matches_request(backend, request), "incompatible_image_request")
    _require(request.model_profile in policy.profiles and any(route in policy.approved_routes
             and route.backend_id == backend.id and route.connection_ref == backend.connection_ref
             for route in request.approved_routes), "unapproved_profile_or_route")
    _require(backend.resource_groups == frozenset(group.id for group in policy.resources)
             and all(group.host_id == backend.host_id for group in policy.resources), "resource_binding_mismatch")
    value = _object(raw_payload, PAYLOAD_BYTES)
    _require(hashlib.sha256(raw_payload).hexdigest() == request.payload.fingerprint, "changed_private_payload")
    _require(_REQUIRED_FIELDS <= value.keys() and value.keys() <= policy.allowed_payload_fields, "unsupported_payload_fields")
    _require(value["model"] == request.model_profile.model_reference, "model_mismatch")
    _require(all(key in value and _equal_json(value[key], option)
                 for key, option in options.items()), "effective_options_mismatch")
    for name in ("width", "height", "steps", "images"):
        _require(type(value[name]) is int and value[name] > 0, "invalid_image_dimensions_or_count")
    _require(value["images"] <= MAX_ARTIFACTS and type(value["seed"]) is int
             and type(value["cfgscale"]) in (int, float) and value["cfgscale"] >= 0, "invalid_image_options")
    _require(type(value["prompt"]) is str and type(value["negativeprompt"]) is str
             and _text(value["sampler"]) and _text(value["scheduler"]), "invalid_image_fields")
    if "session_id" in value:
        _require(_text(value["session_id"]), "invalid_private_session")
    effective, enrichments = raw_payload, ()
    if session_envelope is not None:
        session = _object(session_envelope, SETTINGS_BYTES)
        _require(set(session) == {"session_id"} and _text(session["session_id"]), "invalid_private_session")
        _require("session_id" in policy.allowed_payload_fields, "unsupported_payload_fields")
        if "session_id" in value:
            _require(value["session_id"] == session["session_id"], "private_session_mismatch")
        else:
            value["session_id"] = session["session_id"]
            _require(len(value) <= MAX_COLLECTION, "collection_limit")
            effective = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            _object(effective, PAYLOAD_BYTES)
            enrichments = ("session_id",)
    return WirePlan(request, backend, policy, wire, raw_payload, effective, enrichments)


def _job_identity(status):
    return (status.id, status.task_id, status.owner_application, status.backend_id,
            status.connection_ref, status.request_fingerprint, status.operation_id, status.correlation_id)


def _bound(plan, status):
    request = plan.request
    _require(type(status) is JobStatus and
        (status.task_id, status.owner_application, status.backend_id, status.connection_ref,
         status.request_fingerprint, status.operation_id, status.correlation_id) ==
        (request.id, request.owner_application, plan.backend.id, plan.backend.connection_ref,
         request.fingerprint, request.operation_id, request.correlation_id), "job_binding_mismatch")


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    reference: PrivatePayloadRef
    source_uri: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TranslatedResult:
    receipt: ResultReceipt
    raw_response: bytes = field(repr=False)
    artifacts: tuple[ArtifactEvidence, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class TranslatedFailure:
    receipt: ResultReceipt
    raw_response: bytes | None = field(repr=False)


def translate_result(plan: WirePlan, submitted: JobStatus, final_status: JobStatus,
                     raw_response: bytes) -> TranslatedResult:
    _require(type(plan) is WirePlan, "wire_plan_required")
    _bound(plan, submitted); _bound(plan, final_status)
    _require(_job_identity(submitted) == _job_identity(final_status), "wrong_submitted_job")
    _require(final_status.state == JobState.SUCCEEDED, "completion_not_proven")
    value = _object(raw_response, RESPONSE_BYTES)
    _require(not value.get("error") and not value.get("error_id"), "provider_error")
    images = value.get("images")
    _require(type(images) is list and 0 < len(images) <= MAX_ARTIFACTS, "artifact_limit_or_missing")
    allowed = {_origin(uri) for uri in plan.policy.allowed_artifact_origins}
    artifacts = []
    for index, image in enumerate(images):
        _require(type(image) is str and _text(image) and not any(c.isspace() for c in image)
                 and "\\" not in image and not _urlparts(image).fragment, "invalid_artifact_uri")
        uri = urljoin(plan.policy.artifact_origin.rstrip("/") + "/", image)
        _require(_origin(uri) in allowed, "unapproved_artifact_origin")
        ref = PrivatePayloadRef(plan.request.owner_application, StableId("artifact-" +
            hashlib.sha256((final_status.id.value + ":" + str(index)).encode()).hexdigest()),
            hashlib.sha256(uri.encode("utf-8")).hexdigest(), "application/x-image-reference")
        artifacts.append(ArtifactEvidence(ref, uri))
    model, revision, finish = value.get("model"), value.get("model_revision"), value.get("finish_reason")
    _require(all(item is None or _text(item) for item in (model, revision, finish)), "invalid_execution_evidence")
    effective = None
    if "effective_options" in value:
        _require(type(value["effective_options"]) is dict, "invalid_execution_evidence")
        effective = bounded_settings(json.dumps(value["effective_options"], ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8"))
    raw_ref = PrivatePayloadRef(plan.request.owner_application,
        StableId("raw-" + hashlib.sha256(final_status.id.value.encode()).hexdigest()),
        hashlib.sha256(raw_response).hexdigest(), "application/json")
    try:
        receipt = ResultReceipt(final_status, plan.request.model_profile.fingerprint, raw_ref,
            tuple(artifact.reference for artifact in artifacts), finish, model, revision, effective,
            value.get("input_tokens"), value.get("output_tokens"), value.get("queue_seconds"),
            value.get("load_seconds"), value.get("inference_seconds"))
    except (ValueError, TypeError):
        raise BoundaryError("invalid_execution_evidence") from None
    _require(receipt.matches_request(plan.request), "reported_execution_mismatch")
    return TranslatedResult(receipt, raw_response, tuple(artifacts))


class EvidenceStore:
    """Bounded app-owned in-memory evidence. Not a durable queue or lease manager."""
    def __init__(self, owner: ApplicationId):
        _require(type(owner) is ApplicationId, "owner_required")
        self.owner = owner
        self._jobs = {}
        self._results = {}
        self._raw_bytes = 0

    def register(self, plan: WirePlan, submitted: JobStatus):
        _require(type(plan) is WirePlan and plan.request.owner_application == self.owner, "foreign_evidence")
        _bound(plan, submitted)
        _require(submitted.id not in self._jobs and
                 not any(item[0].request.id == plan.request.id for item in self._jobs.values()),
                 "duplicate_submission_query_existing")
        _require(len(self._jobs) < MAX_JOBS, "job_retention_full")
        self._jobs[submitted.id] = (plan, submitted)

    def record(self, final_status: JobStatus, raw_response: bytes) -> TranslatedResult:
        _require(type(final_status) is JobStatus and final_status.id in self._jobs, "unknown_job_query_existing")
        plan, submitted = self._jobs[final_status.id]
        _require(final_status.id not in self._results, "result_already_retained")
        _require(type(raw_response) is bytes and len(raw_response) <= RESPONSE_BYTES, "byte_limit")
        _require(len(self._results) < MAX_RESULTS and self._raw_bytes + len(raw_response) <= RETAINED_BYTES,
                 "result_retention_full")
        result = translate_result(plan, submitted, final_status, raw_response)
        self._results[final_status.id] = result
        self._raw_bytes += len(raw_response)
        return result

    def record_failure(self, final_status: JobStatus, raw_response: bytes | None,
                       failure: TechnicalFailure) -> TranslatedFailure:
        """Retain exact bounded error bytes, or honest absent response.

        Host supplies transport/failure classification; no parsed wire flag
        establishes terminal state, drain, retries or recovery authority.
        """
        _require(type(final_status) is JobStatus and final_status.id in self._jobs, "unknown_job_query_existing")
        _require(type(failure) is TechnicalFailure and final_status.state in
                 (JobState.FAILED, JobState.TIMED_OUT, JobState.CANCELLED), "technical_failure_required")
        plan, submitted = self._jobs[final_status.id]
        _bound(plan, final_status)
        _require(_job_identity(submitted) == _job_identity(final_status), "wrong_submitted_job")
        _require(final_status.id not in self._results, "result_already_retained")
        _require(raw_response is None or (type(raw_response) is bytes and len(raw_response) <= RESPONSE_BYTES), "byte_limit")
        size = len(raw_response) if raw_response is not None else 0
        _require(len(self._results) < MAX_RESULTS and self._raw_bytes + size <= RETAINED_BYTES,
                 "result_retention_full")
        raw_ref = None if raw_response is None else PrivatePayloadRef(self.owner,
            StableId("raw-" + hashlib.sha256(final_status.id.value.encode()).hexdigest()),
            hashlib.sha256(raw_response).hexdigest(), "application/octet-stream")
        receipt = ResultReceipt(replace(final_status, failure=failure), plan.request.model_profile.fingerprint, raw_ref)
        result = TranslatedFailure(receipt, raw_response)
        self._results[final_status.id] = result
        self._raw_bytes += size
        return result

    def result(self, job_id: JobId) -> TranslatedResult | TranslatedFailure:
        _require(type(job_id) is JobId and job_id in self._results, "result_unavailable_query_existing")
        return self._results[job_id]

    def retire(self, job_id: JobId, *, acknowledged: bool, drained: bool,
               terminal_status: JobStatus | None = None) -> bool:
        # Host supplies explicit acknowledgement/drain evidence; no wire flag can
        # release/retire anything. No lease or provider is touched by this store.
        _require(type(acknowledged) is bool and type(drained) is bool, "host_retirement_evidence_required")
        if not acknowledged or not drained or job_id not in self._jobs:
            return False
        plan, submitted = self._jobs[job_id]
        result = self._results.get(job_id)
        status = terminal_status if terminal_status is not None else result.receipt.status if result else None
        if status is None: return False
        _bound(plan, status)
        _require(_job_identity(submitted) == _job_identity(status), "wrong_submitted_job")
        if not status.terminal: return False
        if result is not None:
            del self._results[job_id]
            self._raw_bytes -= len(result.raw_response) if result.raw_response is not None else 0
        del self._jobs[job_id]
        return True
