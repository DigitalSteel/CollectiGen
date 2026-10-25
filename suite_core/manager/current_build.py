"""Bounded, explicit build-resource input; presentation facts only, no probing."""
from dataclasses import dataclass
from pathlib import Path
import json
import re
from core.component_release_identity import display_component_version

RESOURCE_NAME='CURRENT_BUILD.json'
MAX_BYTES=16*1024
COMPONENT='suite-manager'

@dataclass(frozen=True)
class BuildStatus:
    component_id: str
    software_version: str
    kind: str='unknown'
    packaging_version: str | None=None
    target_os: str | None=None
    architecture: str | None=None
    python: str | None=None
    qt: str | None=None
    unsigned: bool | None=None
    clean_machine_qualified: bool | None=None
    source_plan_sha256: str | None=None
    runtime_inventory_sha256: str | None=None

    def lines(self):
        if self.kind=='unknown':
            return ('Developer build; distribution type unknown.',
                'Runtime inclusion not established. Consult the package documentation.')
        title=('Portable build with Python and Qt included.' if self.kind=='portable-runtime'
            else 'Source developer package.')
        runtime=(f'Python {self.python} and Qt {self.qt} included.' if self.kind=='portable-runtime'
            else 'External Python/Qt runtime required.')
        result=[title,runtime,f'Package {self.packaging_version}; Manager {display_component_version("suite-manager", self.software_version)}.']
        if self.target_os is not None:result.append(f'Declared build target: {self.target_os}/{self.architecture}.')
        if self.unsigned is True:result.append('Unsigned developer build.')
        elif self.unsigned is False:result.append('Signing declared by build metadata; publisher authentication not verified.')
        result.append('Build metadata reports a clean-machine check; compatibility has not been independently checked here.'
            if self.clean_machine_qualified else 'Compatibility on a clean computer is not established.')
        return tuple(result)

def unknown(version):return BuildStatus(COMPONENT,version)

def _string(value,pattern):
    return type(value) is str and re.fullmatch(pattern,value) is not None

def parse_metadata(value,version):
    """Only the producer's exact schema can establish a declared bundle kind."""
    if type(value) is not dict:return unknown(version)
    allowed={'schema_version','component_id','software_version','packaging_version','distribution_kind',
        'runtime_bundled','runtime_versions','unsigned','clean_machine_qualified','target','provenance'}
    if set(value)-allowed:return unknown(version)
    if type(value.get('schema_version')) is not int or value['schema_version']!=1:return unknown(version)
    if value.get('component_id')!=COMPONENT or value.get('software_version')!=version:return unknown(version)
    if not _string(value.get('packaging_version'),r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.]+)?'):return unknown(version)
    kind=value.get('distribution_kind'); bundled=value.get('runtime_bundled')
    if type(bundled) is not bool or kind not in ('source-developer','portable-runtime'):return unknown(version)
    if bundled!=(kind=='portable-runtime'):return unknown(version)
    if any(type(value.get(k)) is not bool for k in ('unsigned','clean_machine_qualified')):return unknown(version)
    target=value.get('target'); os_name=arch=None
    if target is not None:
        if type(target) is not dict or set(target)!={'os','architecture'}:return unknown(version)
        os_name=target['os']; arch=target['architecture']
        if not _string(os_name,r'[a-z][a-z0-9_-]{0,23}') or not _string(arch,r'[a-zA-Z0-9_-]{1,24}'):return unknown(version)
    provenance=value.get('provenance')
    if type(provenance) is not dict or set(provenance)-{'source_plan_sha256','runtime_inventory_sha256'}:return unknown(version)
    source_pin=provenance.get('source_plan_sha256'); runtime_pin=provenance.get('runtime_inventory_sha256')
    if not _string(source_pin,r'[a-f0-9]{64}'):return unknown(version)
    versions=value.get('runtime_versions'); python=qt=None
    if bundled:
        if not _string(runtime_pin,r'[a-f0-9]{64}') or type(versions) is not dict or set(versions)!={'python','qt'}:return unknown(version)
        python=versions['python'];qt=versions['qt']
        if not all(_string(v,r'[0-9]+\.[0-9]+\.[0-9]+') for v in (python,qt)):return unknown(version)
    elif versions is not None or runtime_pin is not None:return unknown(version)
    return BuildStatus(COMPONENT,version,kind,value['packaging_version'],os_name,arch,python,qt,
        value['unsigned'],value['clean_machine_qualified'],source_pin,runtime_pin)

def _read(path,limit):
    with path.open('rb') as stream:return stream.read(limit+1)

def _object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate build metadata key')
        value[key]=item
    return value

def load_current_build(resource_root,version,*,reader=None):
    """Read one declared resource once; no cwd, sibling or installed-path fallback."""
    path=Path(resource_root)/RESOURCE_NAME
    try:
        raw=(reader or _read)(path,MAX_BYTES)
        if type(raw) is not bytes or len(raw)>MAX_BYTES:return unknown(version)
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_object)
        return parse_metadata(value,version)
    except (OSError,ValueError,TypeError,RecursionError):return unknown(version)

def project_status(status,version):
    """Typed DTO injection is validated again; arbitrary construction grants no claim."""
    if type(status) is not BuildStatus:return unknown(version)
    if status.kind=='unknown':return unknown(version)
    value=dict(schema_version=1,component_id=status.component_id,software_version=status.software_version,
        packaging_version=status.packaging_version,distribution_kind=status.kind,
        runtime_bundled=status.kind=='portable-runtime',unsigned=status.unsigned,
        clean_machine_qualified=status.clean_machine_qualified,
        provenance=dict(source_plan_sha256=status.source_plan_sha256))
    if status.target_os is not None:value['target']=dict(os=status.target_os,architecture=status.architecture)
    if status.kind=='portable-runtime':
        value['runtime_versions']=dict(python=status.python,qt=status.qt)
        value['provenance']['runtime_inventory_sha256']=status.runtime_inventory_sha256
    elif any(item is not None for item in (status.python,status.qt,status.runtime_inventory_sha256)):
        return unknown(version)
    return parse_metadata(value,version)
