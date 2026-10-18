"""Pure source-owned joins; no catalog store, file reads, estimator or admission."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import re

from ..backend_contracts import ApplicationId, BackendId, ConnectionId, ResourceGroupId, StableId
from ..discovery.swarm_readonly import DiscoveryReport, DiscoveryState, ModelObservation
from .status_view import ConfiguredConnection, Freshness, _freshness
MODULE_ID = "collectigen.suite-catalog-footprint-join"
MODULE_VERSION = "0.1.0-alpha.1"
CATALOG_JOIN_VERSION = 1

MAX_MODELS = 128
MAX_CATALOG = 256
MAX_EVIDENCE = 256
MAX_BYTES = 2**63 - 1
MAX_VALIDITY = timedelta(days=1)


def _require(value, code):
    if not value: raise ValueError(code)


def _text(value):
    return (type(value) is str and 0 < len(value) <= 512
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _observation(at, validity):
    _require(isinstance(at,datetime) and at.tzinfo is not None and at.utcoffset() is not None, "aware_timestamp_required")
    _require(type(validity) is timedelta and timedelta(0) < validity <= MAX_VALIDITY, "invalid_catalog_validity")


@dataclass(frozen=True, slots=True)
class CatalogRef:
    owner: ApplicationId
    source_ref: StableId
    model_id: StableId

    def __post_init__(self):
        _require(type(self.owner) is ApplicationId and type(self.source_ref) is StableId
                 and type(self.model_id) is StableId, "invalid_catalog_reference")

    def as_ui(self):
        return {"owner":self.owner.value,"source_ref":self.source_ref.value,"model_id":self.model_id.value}


@dataclass(frozen=True, slots=True)
class CatalogBinding:
    owner: ApplicationId
    backend_id: BackendId
    connection_ref: ConnectionId
    offered_name: str
    catalog: CatalogRef
    expected_revision: str | None

    def __post_init__(self):
        _require(type(self.owner) is ApplicationId and type(self.backend_id) is BackendId
                 and type(self.connection_ref) is ConnectionId and type(self.catalog) is CatalogRef,
                 "invalid_catalog_binding")
        _require(_text(self.offered_name) and (self.expected_revision is None or _text(self.expected_revision)),
                 "invalid_binding_label_or_revision")
        _require(self.catalog.owner == self.owner, "foreign_catalog_binding")


class CatalogCategory(str, Enum):
    MAIN_MODEL = "main_model"
    ADAPTER = "adapter"
    AUXILIARY = "auxiliary"


@dataclass(frozen=True, slots=True)
class CatalogSummary:
    catalog: CatalogRef
    display_name: str
    family: str | None
    revision: str | None
    observed_at: datetime
    valid_for: timedelta
    category: CatalogCategory | None = None

    def __post_init__(self):
        _require(type(self.catalog) is CatalogRef and _text(self.display_name)
                 and (self.family is None or _text(self.family)) and (self.revision is None or _text(self.revision)),
                 "invalid_catalog_summary")
        _observation(self.observed_at,self.valid_for)
        _require(self.category is None or type(self.category) is CatalogCategory, "invalid_catalog_category")


@dataclass(frozen=True, slots=True)
class WorkloadScope:
    reference: StableId
    fingerprint: str

    def __post_init__(self):
        _require(type(self.reference) is StableId and type(self.fingerprint) is str
                 and re.fullmatch(r"[0-9a-f]{64}",self.fingerprint) is not None, "invalid_workload_scope")


class FootprintKind(str, Enum):
    DISK = "disk"
    ESTIMATE = "estimate"
    HISTORICAL_MEASUREMENT = "historical_measurement"


class MemoryComponent(str, Enum):
    ALLOCATED = "allocated"
    RESIDENT = "resident"
    PEAK_ALLOCATED = "peak_allocated"


@dataclass(frozen=True, slots=True)
class FootprintEvidence:
    evidence_ref: StableId
    source_ref: StableId
    catalog: CatalogRef
    revision: str | None
    kind: FootprintKind
    value_bytes: int
    basis: str
    observed_at: datetime
    valid_for: timedelta
    resource_group: ResourceGroupId | None = None
    workload: WorkloadScope | None = None
    component: MemoryComponent | None = None

    def __post_init__(self):
        _require(type(self.evidence_ref) is StableId and type(self.source_ref) is StableId
                 and type(self.catalog) is CatalogRef and type(self.kind) is FootprintKind, "invalid_footprint_identity")
        _require(type(self.value_bytes) is int and 0 <= self.value_bytes <= MAX_BYTES, "invalid_footprint_bytes")
        _require(_text(self.basis) and (self.revision is None or _text(self.revision)), "invalid_footprint_basis_or_revision")
        _observation(self.observed_at,self.valid_for)
        if self.kind is FootprintKind.DISK:
            _require(self.resource_group is None and self.workload is None and self.component is None,
                     "disk_is_not_gpu_evidence")
        else:
            _require(type(self.resource_group) is ResourceGroupId and type(self.workload) is WorkloadScope,
                     "gpu_scope_required")
            _require((self.kind is FootprintKind.ESTIMATE and self.component is None)
                     or (self.kind is FootprintKind.HISTORICAL_MEASUREMENT and type(self.component) is MemoryComponent),
                     "invalid_memory_component")


@dataclass(frozen=True, slots=True)
class FootprintView:
    evidence: FootprintEvidence
    freshness: Freshness

    def as_ui(self):
        item=self.evidence
        return {"bytes":None if self.freshness is Freshness.FUTURE else item.value_bytes,"unit":"bytes",
            "kind":item.kind.value,"component":None if item.component is None else item.component.value,
            "source_ref":item.source_ref.value,"evidence_ref":item.evidence_ref.value,"basis":item.basis,
            "observed_at":item.observed_at.isoformat(),"valid_for_seconds":item.valid_for.total_seconds(),
            "freshness":self.freshness.value,"model_revision":item.revision,
            "resource_group":None if item.resource_group is None else item.resource_group.value,
            "workload_ref":None if item.workload is None else item.workload.reference.value,
            "workload_fingerprint":None if item.workload is None else item.workload.fingerprint,
            "historical_sample":item.kind is FootprintKind.HISTORICAL_MEASUREMENT,
            "current_usage":False,"admission_evidence":False}


class JoinState(str, Enum):
    UNMAPPED = "unmapped"
    SOURCE_MISSING = "source_missing"
    REVISION_MISMATCH = "revision_mismatch"
    FUTURE_SOURCE = "future_source"
    SOURCE_CATEGORY_MISMATCH = "source_category_mismatch"
    REFERENCE_BOUND = "reference_bound"


@dataclass(frozen=True, slots=True)
class ModelInspectionRow:
    owner: ApplicationId
    backend_id: BackendId
    connection_ref: ConnectionId
    offered_name: str
    offered_revision: str | None
    catalog: CatalogRef | None
    display_name: str | None
    family: str | None
    catalog_revision: str | None
    catalog_category: CatalogCategory | None
    join_state: JoinState
    inventory_freshness: Freshness
    catalog_freshness: Freshness
    disk: FootprintView | None
    estimate: FootprintView | None
    historical_measurement: FootprintView | None
    footprint_hold: str | None

    def as_ui(self):
        return {"interface_version":CATALOG_JOIN_VERSION,"owner":self.owner.value,"backend_id":self.backend_id.value,
            "connection_ref":self.connection_ref.value,"offered_name":self.offered_name,
            "offered_revision":self.offered_revision,"catalog_reference":None if self.catalog is None else self.catalog.as_ui(),
            "display_name":self.display_name,"family":self.family,"catalog_revision":self.catalog_revision,
            "catalog_category":None if self.catalog_category is None else self.catalog_category.value,
            "join_state":self.join_state.value,"inventory_freshness":self.inventory_freshness.value,
            "catalog_freshness":self.catalog_freshness.value,"model_count_category":"main_models",
            "disk":None if self.disk is None else self.disk.as_ui(),
            "estimate":None if self.estimate is None else self.estimate.as_ui(),
            "historical_measurement":None if self.historical_measurement is None else self.historical_measurement.as_ui(),
            "footprint_hold":self.footprint_hold,"current_usage_bytes":None,
            "provider_identity_verified":False,"routing_authorized":False,"model_profile":None}


def join_model_rows(connection, inventory, bindings, catalog_summaries, footprint_evidence=(),
                    *, now, workload=None, resource_group=None):
    """Exact in-memory references only. Output is inspection, never execution proof."""
    _require(type(connection) is ConfiguredConnection and type(inventory) is DiscoveryReport, "join_inputs_required")
    _observation(now,timedelta(seconds=1));_observation(inventory.observed_at,inventory.valid_for)
    _require(inventory.valid_for <= timedelta(seconds=30), "invalid_inventory_validity")
    _require((inventory.owner,inventory.connection_ref)==(connection.owner,connection.connection_ref), "inventory_binding_mismatch")
    _require(type(inventory.state) is DiscoveryState and inventory.state is not DiscoveryState.HELD, "unusable_inventory")
    _require(inventory.observed_at <= now, "future_inventory")
    _require(type(inventory.models) is tuple and len(inventory.models)<=MAX_MODELS
             and all(type(item) is ModelObservation and _text(item.reported_name)
                     and (item.revision is None or _text(item.revision)) for item in inventory.models), "invalid_offered_models")
    _require(len({item.reported_name for item in inventory.models})==len(inventory.models), "duplicate_offered_model")
    for values,kind,maximum in ((bindings,CatalogBinding,MAX_MODELS),(catalog_summaries,CatalogSummary,MAX_CATALOG),
                                (footprint_evidence,FootprintEvidence,MAX_EVIDENCE)):
        _require(type(values) is tuple and len(values)<=maximum and all(type(item) is kind for item in values), "join_collection_bound")
    _require(workload is None or type(workload) is WorkloadScope, "invalid_selected_workload")
    _require(resource_group is None or type(resource_group) is ResourceGroupId, "invalid_selected_resource")
    physical=[(group.host_id,group.device_reference) for group in connection.resource_groups if group.identity_verified]
    _require(len(set(physical))==len(physical), "ambiguous_resource_identity")
    exact=(connection.owner,connection.backend_id,connection.connection_ref)
    _require(all((item.owner,item.backend_id,item.connection_ref)==exact for item in bindings), "foreign_binding")
    _require(all(item.catalog.owner==connection.owner for item in catalog_summaries+footprint_evidence), "foreign_source_record")
    by_name={item.offered_name:item for item in bindings}
    sources={item.catalog:item for item in catalog_summaries}
    _require(len(by_name)==len(bindings), "ambiguous_binding")
    _require(len(sources)==len(catalog_summaries), "ambiguous_catalog")
    _require(len({(item.source_ref,item.evidence_ref) for item in footprint_evidence})==len(footprint_evidence), "duplicate_footprint_evidence")
    verified = any(group.id==resource_group and group.identity_verified for group in connection.resource_groups)
    inventory_freshness=_freshness(inventory.observed_at,inventory.valid_for,now)
    rows=[]
    for offered in inventory.models:
        binding=by_name.get(offered.reported_name)
        source=None if binding is None else sources.get(binding.catalog)
        state=JoinState.UNMAPPED if binding is None else JoinState.SOURCE_MISSING if source is None else JoinState.REFERENCE_BOUND
        source_freshness=Freshness.MISSING if source is None else _freshness(source.observed_at,source.valid_for,now)
        if source is not None:
            if source_freshness is Freshness.FUTURE: state=JoinState.FUTURE_SOURCE
            elif source.revision!=binding.expected_revision or (offered.revision is not None and offered.revision!=source.revision):
                state=JoinState.REVISION_MISMATCH
            elif source.category not in (None,CatalogCategory.MAIN_MODEL): state=JoinState.SOURCE_CATEGORY_MISMATCH
        metrics={kind:None for kind in FootprintKind};hold=None
        if state is JoinState.REFERENCE_BOUND:
            evidence=[item for item in footprint_evidence if item.catalog==source.catalog]
            disk=[item for item in evidence if item.kind is FootprintKind.DISK and item.revision==source.revision]
            _require(len(disk)<=1,"ambiguous_disk_evidence")
            if disk: metrics[FootprintKind.DISK]=FootprintView(disk[0],_freshness(disk[0].observed_at,disk[0].valid_for,now))
            if source.category is None: hold="source_category_unknown"
            elif source.revision is None: hold="model_revision_unknown"
            elif workload is None: hold="workload_unknown"
            elif resource_group is None: hold="resource_not_selected"
            elif not any(group.id==resource_group for group in connection.resource_groups): hold="resource_binding_mismatch"
            elif not verified: hold="resource_identity_unknown"
            else:
                for kind in (FootprintKind.ESTIMATE,FootprintKind.HISTORICAL_MEASUREMENT):
                    matches=[item for item in evidence if item.kind is kind and item.revision==source.revision
                             and item.resource_group==resource_group and item.workload==workload]
                    _require(len(matches)<=1,"ambiguous_gpu_evidence")
                    if matches: metrics[kind]=FootprintView(matches[0],_freshness(matches[0].observed_at,matches[0].valid_for,now))
                if all(metrics[kind] is None for kind in (FootprintKind.ESTIMATE,FootprintKind.HISTORICAL_MEASUREMENT)):
                    hold="no_matching_gpu_evidence"
        usable=state is JoinState.REFERENCE_BOUND
        rows.append(ModelInspectionRow(connection.owner,connection.backend_id,connection.connection_ref,
            offered.reported_name,offered.revision,None if binding is None else binding.catalog,
            source.display_name if usable else None,source.family if usable else None,source.revision if usable else None,
            source.category if usable else None,
            state,inventory_freshness,source_freshness,metrics[FootprintKind.DISK],metrics[FootprintKind.ESTIMATE],
            metrics[FootprintKind.HISTORICAL_MEASUREMENT],hold))
    return tuple(rows)
