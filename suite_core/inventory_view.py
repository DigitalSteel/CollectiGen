"""Bounded detached inventory composition. No collection, persistence or authority."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json

from .backend_contracts import ApplicationId, HostId, ResourceGroup, ResourceGroupId, ResourceTelemetry, StableId
from .installation import InstalledComponent, InstallationId, Resolution, ResolveState
from .discovery.swarm_readonly import DiscoveryReport, ModelObservation
from .registry.catalog_join import join_model_rows
from .registry.status_view import ConfiguredConnection, MetadataSnapshot, PinnedAssignment, ResourceSnapshot, ResourceStatus, SnapshotStore, StatusRow
from .inventory_version import INVENTORY_VIEW_VERSION, MODULE_ID, MODULE_VERSION

MAX_REPORT_BYTES = 1024 * 1024

def _require(condition, code):
    if not condition: raise ValueError(code)

def _time(value):
    _require(type(value) is datetime and value.utcoffset() is not None, "aware_time_required")

def _observed(at, ttl):
    _time(at)
    _require(type(ttl) is timedelta and timedelta(0) < ttl <= timedelta(seconds=30), "observation_ttl_bound")

def _freshness(at, ttl, now):
    if at > now: return "future"
    return "fresh" if now - at <= ttl else "stale"

def _collection(values, kind, maximum):
    _require(type(values) is tuple and len(values) <= maximum and all(type(v) is kind for v in values), "inventory_input_bound")

class DetectionState(str, Enum):
    OBSERVED = "observed"
    MISSING = "missing"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"

@dataclass(frozen=True, slots=True)
class HardwareObservation:
    """Caller-owned safe observation; labels are never physical identity evidence."""
    owner: ApplicationId
    host_id: HostId
    observation_ref: StableId
    source_ref: StableId
    observed_at: datetime
    valid_for: timedelta
    state: DetectionState
    kind: str
    label: str
    total_memory_bytes: int | None = None
    resource_group: ResourceGroupId | None = None

    def __post_init__(self):
        _require(type(self.owner) is ApplicationId and type(self.host_id) is HostId
                 and type(self.observation_ref) is StableId and type(self.source_ref) is StableId, "hardware_identity_required")
        _observed(self.observed_at, self.valid_for)
        _require(type(self.state) is DetectionState and type(self.kind) is str and self.kind in {"cpu", "gpu", "ram", "unknown"}, "hardware_state_required")
        _require(type(self.label) is str and 1 <= len(self.label) <= 256 and not any(ord(c) < 32 for c in self.label), "hardware_label_bound")
        _require(self.total_memory_bytes is None or type(self.total_memory_bytes) is int and 0 <= self.total_memory_bytes <= 2**63-1, "hardware_memory_bound")
        _require(self.state is DetectionState.OBSERVED or self.total_memory_bytes is None, "unobserved_hardware_memory")
        _require(self.resource_group is None or type(self.resource_group) is ResourceGroupId, "hardware_group_required")

@dataclass(frozen=True, slots=True)
class ComponentObservation:
    """Bound one explicit configured entry to its existing locator result."""
    entry: InstalledComponent
    resolution: Resolution
    source_ref: StableId
    observed_at: datetime
    valid_for: timedelta

    def __post_init__(self):
        _require(type(self.entry) is InstalledComponent and type(self.resolution) is Resolution
                 and type(self.source_ref) is StableId, "component_observation_required")
        _observed(self.observed_at, self.valid_for)
        _require(type(self.resolution.consumer) is ApplicationId and type(self.resolution.state) is ResolveState
                 and type(self.resolution.interface_version) is int and self.resolution.interface_version == 1, "locator_result_required")
        _require(self.resolution.installation_id is None or self.resolution.installation_id == self.entry.installation_id, "locator_installation_mismatch")
        _require(self.resolution.state is not ResolveState.LOCATED_LOCAL or self.resolution.installation_id == self.entry.installation_id, "located_identity_required")

@dataclass(frozen=True, slots=True)
class ModelBatch:
    """Owning join inputs retained for fresh projection; no duplicate catalog/store."""
    connection: ConfiguredConnection
    inventory: DiscoveryReport
    bindings: tuple = ()
    catalogs: tuple = ()
    footprint_evidence: tuple = ()
    workload: object = None
    resource_group: ResourceGroupId | None = None

    def __post_init__(self):
        _require(type(self.connection) is ConfiguredConnection and type(self.inventory) is DiscoveryReport, "model_inputs_required")
        _require(all(type(v) is tuple for v in (self.bindings, self.catalogs, self.footprint_evidence)), "model_collections_required")

@dataclass(frozen=True, slots=True)
class InventoryReport:
    canonical_json: str

    def to_mapping(self):
        """Return a detached copy; mutating it cannot change this report or sources."""
        return json.loads(self.canonical_json)

    def to_bytes(self):
        return (self.canonical_json + "\n").encode("utf-8")

def compose_inventory(owner, status_rows=(), components=(), model_batches=(), hardware=(), *, now):
    """Reproject supplied snapshots through existing owners; never invoke producers."""
    _require(type(owner) is ApplicationId, "inventory_owner_required"); _time(now)
    _collection(status_rows, StatusRow, 64); _collection(components, ComponentObservation, 64)
    _collection(model_batches, ModelBatch, 64); _collection(hardware, HardwareObservation, 128)
    _require(all(type(row.configured) is ConfiguredConnection for row in status_rows), "configured_connection_required")
    _require(all(row.configured.owner == owner for row in status_rows), "foreign_status_owner")
    for row in status_rows:
        _collection(row.assignments, PinnedAssignment, 128)
    # This disposable existing SnapshotStore evaluates freshness/dedup. It is not
    # retained, changed upstream, a new store implementation, or a producer call.
    pins = tuple(pin for row in status_rows for pin in row.assignments)
    store = SnapshotStore(tuple(row.configured for row in status_rows), pins)
    configured = {row.configured.connection_ref: row.configured for row in status_rows}
    groups = {g.id: g for row in status_rows for g in row.configured.resource_groups}
    snapshots = {}
    for row in status_rows:
        _require(type(row.resources) is tuple and all(type(r) is ResourceStatus for r in row.resources), "resource_rows_required")
        _require(all(type(r.group) is ResourceGroup for r in row.resources), "resource_group_required")
        expected = {g.id: g for g in row.configured.resource_groups}
        _require(len(row.resources) == len(expected) and {r.group.id for r in row.resources} == set(expected), "resource_row_binding_mismatch")
        for resource in row.resources:
            _require(resource.group == expected[resource.group.id], "resource_identity_mismatch")
            snapshot = resource.snapshot
            if snapshot is not None:
                _require(type(snapshot) is ResourceSnapshot, "resource_snapshot_required")
                _require(type(snapshot.telemetry) is ResourceTelemetry, "resource_telemetry_required")
                _require(snapshot.telemetry.group_id == resource.group.id, "telemetry_binding_mismatch")
                # Equal instants can use different UTC offsets. Canonicalize the
                # shared representation before equality and representative choice.
                snapshot = replace(snapshot, telemetry=replace(snapshot.telemetry,
                    observed_at=snapshot.telemetry.observed_at.astimezone(timezone.utc)))
                previous = snapshots.get(resource.group.id)
                _require(previous is None or previous == snapshot, "conflicting_resource_snapshots")
                snapshots[resource.group.id] = snapshot
        summary, latest = row.discovery, row.latest_observation
        _require(summary is None or type(summary) is MetadataSnapshot, "summary_required")
        _require(latest is None or type(latest) is MetadataSnapshot, "latest_observation_required")
        for value in (summary, latest):
            if value is not None:
                _require((value.owner, value.connection_ref) == (owner, row.configured.connection_ref), "metadata_binding_mismatch")
        _require(summary is None or latest is not None and latest.observed_at >= summary.observed_at, "metadata_order_mismatch")
        for value in (summary, latest):
            if value is not None:
                # Allows a clock-rollback display. Future facts stay FUTURE in
                # rows(owner, now); ingestion is only of already supplied DTOs.
                store.observe_metadata(value, max(now, value.observed_at))
    for value in snapshots.values(): store.observe_resource(value, max(now, value.telemetry.observed_at))
    rows = store.rows(owner, now)
    by_connection = {row.configured.connection_ref: row for row in rows}
    hosts = [host.as_ui() for host in store.hosts(owner, now)]
    for host in hosts:
        host["resources"].sort(key=lambda r: r["group_id"])
        for connection in host["connections"]:
            connection["resource_group_refs"].sort()
            connection["configured_wire_channels"].sort()
            connection["pinned_assignments"].sort(key=lambda p: p["feature"])
        host["qualified_capability"] = "unknown"

    component_rows = []
    _require(len({c.entry.installation_id for c in components}) == len(components), "duplicate_component_observation")
    for item in sorted(components, key=lambda c: c.entry.installation_id.value):
        entry, located = item.entry, item.resolution
        _require(located.consumer == owner, "foreign_locator_owner")
        fresh = _freshness(item.observed_at, item.valid_for, now)
        component_rows.append({"component_id": entry.component_id.value, "installation_id": entry.installation_id.value,
            "configured_software_version": entry.software_version, "kind": entry.kind.value,
            "interfaces": sorted(({"key": i.manifest_key, "version": i.version} for i in entry.interfaces), key=lambda i: i["key"]),
            "dependencies": sorted(i.value for i in entry.dependencies), "configured_capabilities": sorted(c.value for c in entry.capabilities),
            "locator_state": located.state.value, "locator_interface_version": located.interface_version,
            "source_ref": item.source_ref.value, "observed_at": item.observed_at.isoformat(), "valid_for_seconds": item.valid_for.total_seconds(),
            "freshness": fresh, "location_observation_current": fresh == "fresh",
            "package_authenticity": "unknown", "qualified_capability": "unknown", "execution_authorized": False})

    models, holds = [], []
    _require(all(type(b.inventory.models) is tuple and all(type(m) is ModelObservation for m in b.inventory.models)
                 for b in model_batches), "model_inventory_rows_required")
    _require(sum(len(b.inventory.models) for b in model_batches) <= 128, "report_model_bound")
    _require(len({b.connection.connection_ref for b in model_batches}) == len(model_batches), "duplicate_model_batch")
    for batch in sorted(model_batches, key=lambda b: b.connection.connection_ref.value):
        _require(type(batch.connection) is ConfiguredConnection and type(batch.inventory) is DiscoveryReport, "model_inputs_required")
        connection = batch.connection
        _require(connection.owner == owner and configured.get(connection.connection_ref) == connection, "model_connection_mismatch")
        observed = MetadataSnapshot.from_discovery(batch.inventory)
        retained = by_connection[connection.connection_ref].discovery
        _require(retained is not None and (observed.owner, observed.connection_ref, observed.observed_at, observed.valid_for,
                    observed.model_count, observed.backend_count, observed.parameter_count) ==
                 (retained.owner, retained.connection_ref, retained.observed_at, retained.valid_for,
                    retained.model_count, retained.backend_count, retained.parameter_count), "model_inventory_snapshot_mismatch")
        if batch.inventory.observed_at > now:
            holds.append({"connection_ref": connection.connection_ref.value, "reason": "future_model_inventory"})
            continue
        joined = join_model_rows(connection, batch.inventory, batch.bindings, batch.catalogs, batch.footprint_evidence,
                                 now=now, workload=batch.workload, resource_group=batch.resource_group)
        for joined_row in sorted(joined, key=lambda m: m.offered_name):
            value = joined_row.as_ui()
            value.update(qualified_capability="unknown", inventory_observed_at=batch.inventory.observed_at.isoformat(),
                         inventory_valid_for_seconds=batch.inventory.valid_for.total_seconds(),
                         qualification_reason="no_execution_qualification_consumed")
            models.append(value)
    _require(len(models) <= 128, "report_model_bound")

    hardware_rows = []
    _require(len({(h.host_id, h.source_ref, h.observation_ref) for h in hardware}) == len(hardware), "duplicate_hardware_observation")
    for item in sorted(hardware, key=lambda h: (h.host_id.value, h.source_ref.value, h.observation_ref.value)):
        _require(item.owner == owner, "foreign_hardware_owner")
        group = None if item.resource_group is None else groups.get(item.resource_group)
        _require(item.resource_group is None or group is not None and group.host_id == item.host_id, "hardware_group_mismatch")
        fresh = _freshness(item.observed_at, item.valid_for, now)
        hardware_rows.append({"host_id": item.host_id.value, "observation_ref": item.observation_ref.value,
            "source_ref": item.source_ref.value, "observed_at": item.observed_at.isoformat(), "valid_for_seconds": item.valid_for.total_seconds(),
            "state": item.state.value, "kind": item.kind, "label": item.label, "freshness": fresh,
            "detected_current": (item.state is DetectionState.OBSERVED) if fresh == "fresh" and item.state in (DetectionState.OBSERVED, DetectionState.MISSING) else None,
            "retained_total_memory_bytes": item.total_memory_bytes,
            "current_total_memory_bytes": item.total_memory_bytes if fresh == "fresh" and item.state is DetectionState.OBSERVED else None,
            "resource_group_ref": None if group is None else group.id.value,
            "physical_identity_verified": group is not None and group.identity_verified,
            "qualified_capability": "unknown", "free_capacity_known": False})

    payload = {"interface_version": INVENTORY_VIEW_VERSION, "module_id": MODULE_ID, "software_version": MODULE_VERSION,
        "owner": owner.value, "viewed_at": now.astimezone(timezone.utc).isoformat(), "scope": "caller_supplied_local_observations",
        "hosts": hosts, "components": component_rows, "models": models, "model_source_holds": holds, "hardware_observations": hardware_rows,
        "qualified_capability": "unknown", "qualification_reason": "no_execution_qualification_consumed",
        "routing_authorized": False, "execution_authorized": False, "installation_authorized": False, "free_capacity_known": False}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    _require(len(raw.encode("utf-8")) <= MAX_REPORT_BYTES, "report_byte_bound")
    return InventoryReport(raw)

def render_summary(report):
    _require(type(report) is InventoryReport, "inventory_report_required")
    value = report.to_mapping()
    lines = [f"Read-only local inventory: {value['owner']} at {value['viewed_at']}",
             f"Configured hosts: {len(value['hosts'])}; model inspection rows: {len(value['models'])}",
             f"Hardware observations: {len(value['hardware_observations'])}; no inferred physical device count."]
    for host in value["hosts"]:
        for connection in host["connections"]:
            lines.append(f"Connection {connection['connection_ref']}: {connection['state']}; inventory {connection['freshness']}; main model count {connection['main_model_count']!r}; counts current={connection['counts_are_current']}")
    for component in value["components"]:
        lines.append(f"Component {component['component_id']} {component['configured_software_version']}: {component['locator_state']} ({component['freshness']}); package authenticity unknown.")
    lines.append("Qualified execution capability: unknown. No routing, execution, install or free-capacity authority.")
    return "\n".join(lines) + "\n"
