"""Pure TEST simulation planning and immutable journal recovery; no live adapters."""
from dataclasses import dataclass,fields,is_dataclass,replace
from enum import Enum
from suite_core.manager.offline_preview.fixture_admission import FixtureAdmission,Hold,TEST_TRUST,canonical,digest,require
from suite_core.manager.offline_preview.release_contract import SemVer
from suite_core.installation import InstalledComponent,InstallationRegistry,LocalInventory,_relative

def windows_directory_identity(relative_directory):
    """Ordinary Windows lexical identity for the existing portable ASCII contract.

    Reuse strict path admission: canonical forward slashes, no dot/parent parts,
    drive/UNC, backslashes, trailing dots/spaces, reserved devices or short-name
    spellings. Case-fold admitted ASCII components without rewriting DTO paths.
    This is not physical filesystem/reparse/hardlink or mapped-drive attestation.
    """
    return tuple(part.casefold() for part in _relative(relative_directory))

def directories_overlap(left,right):
    return left[:len(right)]==right or right[:len(left)]==left

def require_disjoint_local_ownership(entries):
    owned=[]
    for row in entries:
        if type(row.location) is LocalInventory:
            identity=windows_directory_identity(row.location.relative_directory)
            require(not any(directories_overlap(identity,prior) for prior in owned),'windows_directory_ownership_conflict')
            owned.append(identity)

def mapping(value):
    if is_dataclass(value):return {f.name:mapping(getattr(value,f.name)) for f in fields(value)}
    if isinstance(value,Enum):return value.value
    if type(value) is frozenset:return sorted((mapping(v) for v in value),key=lambda v:canonical(v))
    if type(value) is tuple:return [mapping(v) for v in value]
    return value

@dataclass(frozen=True)
class SimulatedWorld:
    registry: InstallationRegistry
    registry_revision: int
    payload_pins: tuple
    data_sentinels: tuple
    publication_interface: int

    def __post_init__(self):
        require(type(self.registry) is InstallationRegistry,'registry_type')
        require(type(self.registry_revision) is int and self.registry_revision>=0,'registry_revision')
        require(type(self.publication_interface) is int and self.publication_interface in (1,2),'publication_interface')
        for rows in (self.payload_pins,self.data_sentinels):
            require(type(rows) is tuple and len(rows)<=64 and all(type(r) is tuple and len(r)==2
                    and type(r[0]) is str and type(r[1]) is str and len(r[1])==64
                    and all(c in '0123456789abcdef' for c in r[1]) for r in rows),'sentinel_or_pin_shape')
            require(len({r[0] for r in rows})==len(rows),'duplicate_pin')
        require({r[0] for r in self.payload_pins}=={r.installation_id.value for r in self.registry.entries},'payload_pin_closure')

    @property
    def token(self):return digest(canonical(mapping(self)))

@dataclass(frozen=True)
class FakeIdleReceipt:
    world_token: str
    epoch: int
    state: str

@dataclass(frozen=True)
class TransitionPlan:
    before: SimulatedWorld
    after: SimulatedWorld
    operation: str
    target: InstalledComponent
    admitted: FixtureAdmission
    restart_required: bool

    @property
    def token(self):return digest(canonical(mapping(self)))

    @property
    def production_install_authorized(self):return False

def plan_transition(world,admitted,target,*,operation,expected_world_token,expected_epoch,idle):
    require(type(world) is SimulatedWorld and type(admitted) is FixtureAdmission
            and admitted.trust_scope==TEST_TRUST and type(target) is InstalledComponent,'test_inputs_required')
    require(operation in ('install','update','rollback'),'operation')
    require(expected_world_token==world.token,'stale_world_or_payload')
    require(type(expected_epoch) is int and expected_epoch==admitted.catalog_epoch,'stale_catalog')
    require(type(idle) is FakeIdleReceipt and idle.world_token==world.token and type(idle.epoch) is int
            and idle.epoch==expected_epoch and idle.state=='idle','busy_unknown_or_stale_idle')
    require(type(target.location) is LocalInventory,'local_fixture_only')
    require_disjoint_local_ownership(world.registry.entries)
    target_directory=windows_directory_identity(target.location.relative_directory)
    require(target.component_id==admitted.component_id and target.software_version==admitted.software_version
            and target.location.manifest_sha256==admitted.manifest_sha256,'target_binding')
    maximum=next((i.version for i in target.interfaces if i.manifest_key=='snapshot_interface_version'),None)
    if target.component_id.value=='collectigen.tag-encyclopedia':
        require(maximum==max(admitted.reader_interfaces),'reader_maximum_binding')
        require(world.publication_interface in admitted.reader_interfaces,'bound_publication_not_readable')
    existing=next((r for r in world.registry.entries if r.installation_id==target.installation_id),None)
    if existing is not None and target.component_id.value=='collectigen.tag-encyclopedia':
        prior_maximum=next((i.version for i in existing.interfaces if i.manifest_key=='snapshot_interface_version'),None)
        require(type(prior_maximum) is int and prior_maximum>=world.publication_interface,'prior_reader_cannot_preserve_bound_publication')
    if operation=='install':require(existing is None,'already_installed')
    else:
        require(existing is not None and existing.component_id==target.component_id and existing.kind==target.kind,'prior_instance_binding')
        comparison=SemVer.parse(target.software_version).compare(SemVer.parse(existing.software_version))
        require(comparison>0 if operation=='update' else comparison<0,'version_direction')
        require(type(existing.location) is LocalInventory,'local_prior_fixture_only')
        prior_directory=windows_directory_identity(existing.location.relative_directory)
        require(not directories_overlap(prior_directory,target_directory),'immutable_version_directory_required')
    entries=tuple(r for r in world.registry.entries if r.installation_id!=target.installation_id)+(target,)
    require_disjoint_local_ownership(entries)
    registry=InstallationRegistry(entries)
    by_id={r.installation_id:r for r in registry.entries}
    dependency_rows=tuple((by_id[d].component_id,by_id[d].software_version) for d in target.dependencies)
    require(set(dependency_rows)==set(admitted.dependencies) and len(dependency_rows)==len(admitted.dependencies),'dependency_binding')
    visited=set();active=set()
    def visit(key):
        require(key not in active,'dependency_cycle')
        if key in visited:return
        active.add(key)
        for dep in by_id[key].dependencies:visit(dep)
        active.remove(key);visited.add(key)
    for key in by_id:visit(key)
    # DTOs carry exact dependency instances, not consumer version/interface ranges.
    # Updating a currently depended-on instance is held until that richer contract exists.
    if existing is not None:
        require(not any(target.installation_id in r.dependencies for r in registry.entries if r!=target),'surviving_consumer_requires_review')
    pins=dict(world.payload_pins);pins[target.installation_id.value]=admitted.artifact_sha256
    after=replace(world,registry=registry,registry_revision=world.registry_revision+1,payload_pins=tuple(sorted(pins.items())))
    return TransitionPlan(world,after,operation,target,admitted,target.component_id.value=='collectigen.suite-manager')

@dataclass(frozen=True)
class SimulationJournal:
    plan: TransitionPlan
    phase: str

@dataclass(frozen=True)
class FakeHealthReceipt:
    plan_token: str
    world_token: str
    outcome: str

def stage(plan,current):
    require(type(plan) is TransitionPlan and type(current) is SimulatedWorld and current.token==plan.before.token,'stale_stage')
    return SimulationJournal(plan,'staged')

def commit(journal,current,*,idle,restart_completed=False):
    require(type(journal) is SimulationJournal and journal.phase=='staged','journal_phase')
    require(type(current) is SimulatedWorld and current.token==journal.plan.before.token,'stale_commit')
    require(type(idle) is FakeIdleReceipt and idle.world_token==current.token
            and type(idle.epoch) is int and idle.epoch==journal.plan.admitted.catalog_epoch
            and idle.state=='idle','busy_unknown_or_stale_commit_idle')
    require(type(restart_completed) is bool and (not journal.plan.restart_required or restart_completed),'manager_restart_required')
    return journal.plan.after,replace(journal,phase='awaiting_health')

def confirm_health(journal,current,receipt):
    require(type(journal) is SimulationJournal and journal.phase=='awaiting_health','journal_phase')
    require(type(current) is SimulatedWorld and current.token==journal.plan.after.token,'stale_health_world')
    require(type(receipt) is FakeHealthReceipt and receipt.plan_token==journal.plan.token
            and receipt.world_token==current.token and receipt.outcome=='healthy','fake_health_mismatch')
    return replace(journal,phase='healthy')

def recover(journal,current):
    """Conservative TEST abort: restore exact prior world only for owned states."""
    require(type(journal) is SimulationJournal and journal.phase in ('staged','awaiting_health','rolled_back'),'recovery_phase')
    require(type(current) is SimulatedWorld and current.token in (journal.plan.before.token,journal.plan.after.token),'foreign_recovery_state')
    return journal.plan.before,replace(journal,phase='rolled_back')
