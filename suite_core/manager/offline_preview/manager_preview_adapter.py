"""Read-only Manager text projection of the accepted pure fixture planner."""
from dataclasses import dataclass
import re
from suite_core.manager.offline_preview.fixture_admission import FixtureAdmission,exact_version
from suite_core.manager.offline_preview.transition_planner import SimulatedWorld,FakeIdleReceipt,plan_transition
from suite_core.installation import ComponentId,InstalledComponent,LocalInventory

UNAVAILABLE='Offline fixture preview unavailable: invalid supplied preview. Actions remain disabled. Production authority: false.'
MAX_ROWS=128
MAX_ROW_CHARS=4096
MAX_TEXT_CHARS=65536

REASONS={
    'bound_publication_not_readable': 'Keep the current reader. The selected rollback cannot read the bound publication interface; choose a compatible reader without rewriting catalog data.',
    'prior_reader_cannot_preserve_bound_publication': 'The supplied current reader cannot read the bound publication. Correct that binding before planning recovery; keep catalog bytes unchanged.',
    'windows_directory_ownership_conflict': 'Choose a separate, unoccupied version directory. Another retained installation owns an equal or overlapping Windows path.',
    'immutable_version_directory_required': 'Choose a distinct version directory. Case-only names and nested paths would overlap the current code.',
    'busy_unknown_or_stale_idle': 'Wait for consumers to become idle, then refresh the supplied registry and idle receipt before previewing again.',
    'stale_world_or_payload': 'Refresh the supplied registry, directory and payload pins, then review a new preview.',
    'stale_catalog': 'Refresh the supplied fixture catalog epoch before reviewing this transition.',
    'dependency_binding': 'Supply the exact required runtime/dependency instance and version, then preview again. Nothing will be acquired automatically.',
    'dependency_cycle': 'Resolve the circular dependency bindings before previewing a change.',
    'surviving_consumer_requires_review': 'Review compatibility for the listed surviving consumers before changing their required installation.',
    'unbound_installation_dependency': 'Supply the missing dependency instance in the registry, then preview again.',
    'target_binding': 'Refresh the selected component/version and manifest pin; they do not match admitted fixture metadata.',
    'reader_maximum_binding': 'Correct the target reader interface declaration to match the admitted fixture metadata.',
    'version_direction': 'Choose a newer version for Update or an older compatible version for Rollback.',
    'already_installed': 'This installation instance already exists. Review an update or a separate instance instead.',
    'prior_instance_binding': 'Select the exact current installation instance before planning update or rollback.',
}

@dataclass(frozen=True)
class PreviewViewModel:
    component_id: ComponentId
    operation: str
    target_version: str
    state: str
    compatibility: str
    blockers: tuple
    requirements: tuple
    affected_dependencies: tuple
    data_binding: tuple
    code_change: tuple
    world_token: str
    restart_required: bool | None

    @property
    def production_install_authorized(self):return False

    def as_text(self):
        if not valid_preview(self):return UNAVAILABLE
        lines=['Offline fixture '+self.operation.title()+' preview',
               f'Component: {self.component_id.value} | target: {self.target_version}',
               f'Status: {self.state}. Compatibility: {self.compatibility}.',
               'Synthetic supplied bindings only; no current installed/runtime/data authority.',
               'Install / Update / Rollback actions remain disabled. Production authority: false.',
               '', 'Blockers / next action']
        lines.extend(self.blockers or ('No fixture-planning blocker. Review only; production installation is unavailable.',))
        for heading,rows in (('Code reference change',self.code_change),('Requirements',self.requirements),
                            ('Affected dependency instances / consumers',self.affected_dependencies),
                            ('Data binding — hashes only, unchanged',self.data_binding)):
            lines.extend(('',heading));lines.extend(rows or ('None supplied; completeness unknown.',))
        lines.extend(('',f'Supplied world token: {self.world_token}',
                      'Manager restart required (simulated only): '+('Unknown (not established)' if self.restart_required is None else str(self.restart_required))))
        return '\n'.join(lines)

def valid_component_id(value):
    if type(value) is not ComponentId:return False
    try:
        return type(value.value) is str and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}',value.value))
    except AttributeError:return False

def valid_preview(value):
    """Validate fields at selection AND rendering; constructible DTOs are untrusted."""
    if type(value) is not PreviewViewModel:return False
    try:
        if not valid_component_id(value.component_id):return False
        if type(value.operation) is not str or value.operation not in ('install','update','rollback'):return False
        exact_version(value.target_version)
        if type(value.state) is not str or type(value.compatibility) is not str:return False
        if (value.state,value.compatibility) not in (('Plan available','compatible in fixture only'),('Held','not established')):return False
        if type(value.world_token) is not str or not re.fullmatch('[0-9a-f]{64}',value.world_token):return False
        if value.restart_required is not None and type(value.restart_required) is not bool:return False
        if value.component_id.value=='collectigen.suite-manager':
            if value.restart_required is False:return False
            if value.state=='Plan available' and value.restart_required is not True:return False
        elif value.restart_required is True:return False
        total=0
        for rows in (value.blockers,value.requirements,value.affected_dependencies,value.data_binding,value.code_change):
            if type(rows) is not tuple or len(rows)>MAX_ROWS:return False
            for row in rows:
                if type(row) is not str or not 1<=len(row)<=MAX_ROW_CHARS or any(ord(c)<32 for c in row):return False
                total+=len(row)
        if total>MAX_TEXT_CHARS-2048:return False
        if (value.state=='Held')!=bool(value.blockers):return False
        return True
    except (AttributeError,ValueError,TypeError):return False

def build_preview(world,admitted,target,*,operation,expected_world_token,expected_epoch,idle):
    """Consume existing DTOs; call planner once; return text facts, never a journal."""
    if type(world) is not SimulatedWorld or type(admitted) is not FixtureAdmission or type(target) is not InstalledComponent:
        raise ValueError('exact_fixture_dtos_required')
    if type(operation) is not str or operation not in ('install','update','rollback'):
        raise ValueError('preview_operation')
    entries={r.installation_id:r for r in world.registry.entries}
    prior=entries.get(target.installation_id)
    requirements=tuple(f'{c.value} {v} (fixture-declared exact requirement)' for c,v in admitted.dependencies)
    affected=[]
    for key in target.dependencies:
        row=entries.get(key)
        affected.append(f'Required instance: {key.value} — '+(f'{row.component_id.value} {row.software_version}' if row else 'missing'))
    for row in world.registry.entries:
        if row.installation_id!=target.installation_id and target.installation_id in row.dependencies:
            affected.append(f'Surviving consumer: {row.installation_id.value} — compatibility review required')
    def reference(row):
        if row is None:return 'not configured in supplied fixture'
        if type(row.location) is LocalInventory:return row.location.relative_directory
        return 'remote descriptor; local code path not supplied'
    code=(f'Instance: {target.installation_id.value}',f'Current: {reference(prior)}',f'Target: {reference(target)}',
          'Physical path/alias ownership remains unverified.')
    bindings=(f'Bound publication interface: {world.publication_interface} (caller-supplied fixture fact)',
              'Catalog/settings/saves/model data are not opened or changed.')+tuple(f'{name}: SHA256 {pin}' for name,pin in world.data_sentinels)
    try:
        planned=plan_transition(world,admitted,target,operation=operation,expected_world_token=expected_world_token,
                                expected_epoch=expected_epoch,idle=idle)
    except ValueError as failure:
        reason=str(failure)
        actionable=REASONS.get(reason,'Refresh the exact supplied fixture bindings and review again. This preview is held; no action occurred.')
        return PreviewViewModel(target.component_id,operation,target.software_version,'Held','not established',
            (actionable,),requirements,tuple(affected),bindings,code,world.token,target.component_id.value=='collectigen.suite-manager')
    return PreviewViewModel(target.component_id,operation,target.software_version,'Plan available','compatible in fixture only',
        (),requirements,tuple(affected),bindings,code,world.token,planned.restart_required)

def preview_text_for_component(previews,component_id):
    """Exact read-only selection for Manager's existing plain-text Manage dialog."""
    if not valid_component_id(component_id) or type(previews) is not tuple or len(previews)>64 or any(not valid_preview(v) for v in previews):
        return UNAVAILABLE
    matches=[v for v in previews if v.component_id==component_id]
    if not matches:return 'Offline fixture preview not supplied for this component. Actions remain disabled.'
    if len(matches)!=1:return 'Offline fixture preview held: multiple transitions supplied for this component. Select one exact transition; actions remain disabled.'
    return matches[0].as_text()
