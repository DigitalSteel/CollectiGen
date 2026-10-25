"""Single-owner in-memory projections, not routing, credentials or free capacity."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from ..backend_contracts import (
    ApplicationId, BackendId, CapabilityId, ConnectionId, FailureKind, FeatureId, HostId,
    ModelProfileId, ResourceGroup, ResourceTelemetry, StableId,
)
from ..discovery.swarm_readonly import DiscoveryReport, DiscoveryState
from .version import STATUS_VIEW_VERSION

MAX_CONNECTIONS = 64
MAX_RESOURCE_GROUPS = 128
MAX_CAPABILITIES = 64
MAX_ASSIGNMENTS = 128
MAX_VALIDITY = timedelta(seconds=30)


def _require(value, code):
    if not value: raise ValueError(code)


def _time(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             "aware_timestamp_required")


def _validity(value):
    _require(type(value) is timedelta and timedelta(0) < value <= MAX_VALIDITY, "invalid_validity")


class Freshness(str, Enum):
    MISSING = "missing"
    FRESH = "fresh"
    STALE = "stale"
    FUTURE = "future"


class ViewState(str, Enum):
    UNKNOWN = "unknown"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


def _freshness(observed_at, valid_for, now):
    if observed_at is None: return Freshness.MISSING
    if now < observed_at: return Freshness.FUTURE
    return Freshness.FRESH if now - observed_at <= valid_for else Freshness.STALE


@dataclass(frozen=True, slots=True)
class ConfiguredConnection:
    owner: ApplicationId
    backend_id: BackendId
    connection_ref: ConnectionId
    host_id: HostId
    resource_groups: tuple[ResourceGroup, ...] = ()
    capabilities: frozenset[CapabilityId] = frozenset()
    wire_channels: tuple[StableId, ...] = ()

    def __post_init__(self):
        for value, kind in ((self.owner, ApplicationId), (self.backend_id, BackendId),
                            (self.connection_ref, ConnectionId), (self.host_id, HostId)):
            _require(type(value) is kind, "invalid_configured_identity")
        _require(type(self.resource_groups) is tuple and len(self.resource_groups) <= 16
                 and all(type(item) is ResourceGroup and item.host_id == self.host_id for item in self.resource_groups),
                 "invalid_configured_resources")
        _require(len({item.id for item in self.resource_groups}) == len(self.resource_groups), "duplicate_resource_group")
        _require(type(self.wire_channels) is tuple and len(self.wire_channels) <= 16
                 and all(type(item) is StableId for item in self.wire_channels)
                 and len(set(self.wire_channels)) == len(self.wire_channels), "invalid_configured_channels")
        _require(type(self.capabilities) is frozenset and len(self.capabilities) <= MAX_CAPABILITIES
                 and all(type(item) is CapabilityId for item in self.capabilities), "invalid_configured_capabilities")


@dataclass(frozen=True, slots=True)
class PinnedAssignment:
    """Display preference only; not approval, profile loading or route admission."""
    owner: ApplicationId
    feature: FeatureId
    backend_id: BackendId
    connection_ref: ConnectionId
    model_profile: ModelProfileId | None = None

    def __post_init__(self):
        for value, kind in ((self.owner, ApplicationId), (self.feature, FeatureId),
                            (self.backend_id, BackendId), (self.connection_ref, ConnectionId)):
            _require(type(value) is kind, "invalid_assignment_identity")
        _require(self.model_profile is None or type(self.model_profile) is ModelProfileId, "invalid_assignment_profile")


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    owner: ApplicationId
    connection_ref: ConnectionId
    state: DiscoveryState
    observed_at: datetime
    valid_for: timedelta
    model_count: int | None
    backend_count: int | None
    parameter_count: int | None
    source_ref: StableId | None = None
    failure_kind: FailureKind | None = None

    def __post_init__(self):
        _require(type(self.owner) is ApplicationId and type(self.connection_ref) is ConnectionId
                 and type(self.state) is DiscoveryState, "invalid_metadata_identity")
        _time(self.observed_at); _validity(self.valid_for)
        _require(self.source_ref is None or type(self.source_ref) is StableId, "invalid_metadata_source")
        _require(self.failure_kind is None or type(self.failure_kind) is FailureKind, "invalid_failure_kind")
        _require(self.state is not DiscoveryState.READY or self.failure_kind is None, "contradictory_ready_failure")
        for count, maximum in ((self.model_count, 128), (self.backend_count, 64), (self.parameter_count, 4096)):
            _require(count is None or type(count) is int and 0 <= count <= maximum, "metadata_count_bound")
        if self.state in (DiscoveryState.UNKNOWN, DiscoveryState.HELD):
            _require(all(count is None for count in (self.model_count,self.backend_count,self.parameter_count)),
                     "unknown_metadata_counts")

    @classmethod
    def from_discovery(cls, report, source_ref=None):
        _require(type(report) is DiscoveryReport, "discovery_report_required")
        _require(type(report.owner) is ApplicationId and type(report.connection_ref) is ConnectionId
                 and type(report.state) is DiscoveryState, "invalid_discovery_identity")
        _time(report.observed_at); _validity(report.valid_for)
        _require(type(report.models) is tuple and len(report.models) <= 128
                 and type(report.backends) is tuple and len(report.backends) <= 64
                 and type(report.fields) is frozenset and len(report.fields) <= 4096, "discovery_summary_bounds")
        failure = None
        if report.state is DiscoveryState.HELD:
            failure = {"transport_unavailable": FailureKind.TRANSPORT, "io_timeout": FailureKind.TIMEOUT,
                       "total_timeout": FailureKind.TIMEOUT, "http_unavailable": FailureKind.UNAVAILABLE}.get(
                       report.failure_code, FailureKind.INVALID_OUTPUT)
        completed = report.state in (DiscoveryState.READY, DiscoveryState.UNAVAILABLE, DiscoveryState.UNSUPPORTED)
        return cls(report.owner, report.connection_ref, report.state, report.observed_at, report.valid_for,
                   len(report.models) if completed else None, len(report.backends) if completed else None,
                   len(report.fields) if completed else None, source_ref, failure)


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Host assertion of complete group telemetry, never session-only status."""
    telemetry: ResourceTelemetry
    source_ref: StableId

    def __post_init__(self):
        _require(type(self.telemetry) is ResourceTelemetry and type(self.source_ref) is StableId,
                 "invalid_resource_snapshot")
        _validity(self.telemetry.valid_for)


@dataclass(frozen=True, slots=True)
class ResourceStatus:
    group: ResourceGroup
    freshness: Freshness
    observed_at: datetime | None
    busy: bool | None
    snapshot: ResourceSnapshot | None

    def as_ui(self):
        qualified = self.group.identity_verified and self.freshness is Freshness.FRESH and self.snapshot is not None
        telemetry = None if self.snapshot is None else self.snapshot.telemetry
        return {"group_id": self.group.id.value, "host_id": self.group.host_id.value,
            "identity_verified": self.group.identity_verified, "freshness": self.freshness.value,
            "observed_at": None if telemetry is None else telemetry.observed_at.isoformat(),
            "source_ref": None if self.snapshot is None else self.snapshot.source_ref.value,
            "busy": self.busy, **{name: getattr(telemetry,name) if qualified else None for name in
                ("active_jobs","queued_jobs","free_vram_bytes","total_vram_bytes","free_ram_bytes","total_ram_bytes")}}


@dataclass(frozen=True, slots=True)
class StatusRow:
    configured: ConfiguredConnection
    discovery: MetadataSnapshot | None
    latest_observation: MetadataSnapshot | None
    attempt_freshness: Freshness
    age_seconds: float | None
    freshness: Freshness
    state: ViewState
    resources: tuple[ResourceStatus, ...]
    assignments: tuple[PinnedAssignment, ...]

    def as_ui(self):
        summary, latest = self.discovery, self.latest_observation
        return {"interface_version": STATUS_VIEW_VERSION,
            "owner": self.configured.owner.value, "backend_id": self.configured.backend_id.value,
            "connection_ref": self.configured.connection_ref.value, "host_id": self.configured.host_id.value,
            "configured_capability_count": len(self.configured.capabilities),
            "configured_wire_channels": [item.value for item in self.configured.wire_channels],
            "configured_capabilities": sorted(item.value for item in self.configured.capabilities),
            "main_model_count": None if summary is None else summary.model_count,
            "model_count_category": "main_models",
            "observation_scope": "configured_connection_main_inventory",
            "reported_backend_count": None if summary is None else summary.backend_count,
            "parameter_count": None if summary is None else summary.parameter_count,
            "metadata_state": None if latest is None else latest.state.value,
            "failure_kind": None if latest is None or latest.failure_kind is None else latest.failure_kind.value,
            "last_attempt_at": None if latest is None else latest.observed_at.isoformat(),
            "attempt_freshness": self.attempt_freshness.value,
            "source_ref": None if summary is None or summary.source_ref is None else summary.source_ref.value,
            "valid_for_seconds": None if summary is None else summary.valid_for.total_seconds(),
            "age_seconds": self.age_seconds,
            "observed_at": None if summary is None else summary.observed_at.isoformat(),
            "freshness": self.freshness.value,
            "counts_are_current": self.freshness is Freshness.FRESH and summary == latest and summary.model_count is not None,
            "state": self.state.value, "free_capacity_known": False, "routing_authorized": False,
            "resources": [row.as_ui() for row in self.resources],
            "pinned_assignments": [{"feature": pin.feature.value,
                "model_profile": None if pin.model_profile is None else pin.model_profile.value}
                for pin in self.assignments]}


@dataclass(frozen=True, slots=True)
class HostStatus:
    host_id: HostId
    connections: tuple[StatusRow, ...]
    resources: tuple[ResourceStatus, ...]

    def as_ui(self):
        connections = []
        for row in self.connections:
            value = row.as_ui()
            value["resource_group_refs"] = [resource.group.id.value for resource in row.resources]
            del value["resources"]  # Host resource facts occur once; channel rows reference them.
            connections.append(value)
        return {"host_id": self.host_id.value, "resources": [row.as_ui() for row in self.resources],
                "connections": connections, "routing_authorized": False}


class SnapshotStore:
    """Bounded single-owner CPU store. Callers serialize access and supply clocks."""
    def __init__(self, configured, assignments=()):
        _require(type(configured) is tuple and len(configured) <= MAX_CONNECTIONS
                 and all(type(item) is ConfiguredConnection for item in configured), "invalid_registry_configuration")
        self._configured = {(item.owner, item.connection_ref): item for item in configured}
        _require(len(self._configured) == len(configured), "duplicate_configured_connection")
        _require(len({(item.owner, item.backend_id) for item in configured}) == len(configured), "duplicate_configured_backend")
        self._groups = {}
        physical = {}
        for item in configured:
            for group in item.resource_groups:
                previous = self._groups.get(group.id)
                _require(previous is None or previous == group, "conflicting_resource_identity")
                self._groups[group.id] = group
                if group.identity_verified:
                    key = (group.host_id, group.device_reference)
                    _require(key not in physical or physical[key] == group.id, "aliased_physical_resource")
                    physical[key] = group.id
        _require(len(self._groups) <= MAX_RESOURCE_GROUPS, "resource_group_bound")
        _require(type(assignments) is tuple and len(assignments) <= MAX_ASSIGNMENTS
                 and all(type(item) is PinnedAssignment for item in assignments), "invalid_assignments")
        _require(len({(item.owner, item.feature) for item in assignments}) == len(assignments), "duplicate_feature_assignment")
        for pin in assignments:
            target = self._configured.get((pin.owner, pin.connection_ref))
            _require(target is not None and target.backend_id == pin.backend_id, "assignment_binding_mismatch")
        self._assignments = assignments
        self._discovery, self._inventory, self._resources = {}, {}, {}

    @staticmethod
    def _put(mapping, key, value, observed_at, now):
        _time(now)
        _require(observed_at <= now, "future_snapshot")
        previous = mapping.get(key)
        old_time = None if previous is None else (previous.observed_at if type(previous) is MetadataSnapshot
                                                  else previous.telemetry.observed_at)
        if old_time is not None and observed_at < old_time: return False
        _require(old_time != observed_at or previous == value, "conflicting_same_time_snapshot")
        mapping[key] = value
        return True

    def observe_metadata(self, summary, now):
        _require(type(summary) is MetadataSnapshot, "metadata_snapshot_required")
        key = (summary.owner, summary.connection_ref)
        _require(key in self._configured, "unconfigured_discovery_connection")
        accepted = self._put(self._discovery, key, summary, summary.observed_at, now)
        if accepted and summary.model_count is not None: self._inventory[key] = summary
        return accepted

    def observe_discovery(self, report, now, source_ref=None):
        return self.observe_metadata(MetadataSnapshot.from_discovery(report,source_ref), now)

    def observe_resource(self, snapshot, now):
        _require(type(snapshot) is ResourceSnapshot, "resource_snapshot_required")
        key = snapshot.telemetry.group_id
        _require(key in self._groups, "unconfigured_resource_group")
        return self._put(self._resources, key, snapshot, snapshot.telemetry.observed_at, now)

    def rows(self, owner, now):
        _require(type(owner) is ApplicationId, "owner_required"); _time(now)
        rows = []
        for key, configured in sorted(self._configured.items(), key=lambda item: item[0][1].value):
            if configured.owner != owner: continue
            latest = self._discovery.get(key)
            summary = self._inventory.get(key)
            attempt_freshness = _freshness(None if latest is None else latest.observed_at,
                                          None if latest is None else latest.valid_for, now)
            freshness = _freshness(None if summary is None else summary.observed_at,
                                   None if summary is None else summary.valid_for, now)
            resources = []
            for group in configured.resource_groups:
                snapshot = self._resources.get(group.id)
                telemetry = None if snapshot is None else snapshot.telemetry
                fresh = _freshness(None if telemetry is None else telemetry.observed_at,
                                   None if telemetry is None else telemetry.valid_for, now)
                busy = None
                if group.identity_verified and fresh is Freshness.FRESH:
                    if (telemetry.active_jobs or 0) > 0 or (telemetry.queued_jobs or 0) > 0: busy = True
                    elif telemetry.active_jobs == 0 and telemetry.queued_jobs == 0: busy = False
                resources.append(ResourceStatus(group, fresh, None if telemetry is None else telemetry.observed_at, busy, snapshot))
            state = ViewState.UNKNOWN
            if any(row.busy is True for row in resources): state = ViewState.BUSY
            if attempt_freshness is Freshness.FRESH and (latest.state is DiscoveryState.UNAVAILABLE or
                    latest.failure_kind in (FailureKind.OFFLINE,FailureKind.UNAVAILABLE,FailureKind.TRANSPORT)):
                state = ViewState.UNAVAILABLE  # Resource busy facts remain separately visible.
            age = None if summary is None or now < summary.observed_at else (now-summary.observed_at).total_seconds()
            rows.append(StatusRow(configured, summary, latest, attempt_freshness, age, freshness, state, tuple(resources),
                tuple(pin for pin in self._assignments if (pin.owner, pin.connection_ref) == key)))
        return tuple(rows)

    def hosts(self, owner, now):
        grouped = {}
        for row in self.rows(owner,now): grouped.setdefault(row.configured.host_id,[]).append(row)
        return tuple(HostStatus(host,tuple(rows),tuple({resource.group.id:resource for row in rows
            for resource in row.resources}.values())) for host,rows in sorted(grouped.items(),key=lambda item:item[0].value))
