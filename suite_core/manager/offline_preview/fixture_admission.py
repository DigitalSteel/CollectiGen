"""Test-trust-only byte admission. No production trust, install or filesystem API."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from suite_core.manager.offline_preview.release_contract import SemVer
from suite_core.installation import ComponentId

MAX_METADATA_BYTES = 65536
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
TEST_TRUST = 'TEST_ONLY_NEVER_PRODUCTION'

class Hold(ValueError):
    pass

def require(value, reason):
    if not value: raise Hold(reason)

def digest(raw): return sha256(raw).hexdigest()

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def bounded_json(raw):
    require(type(raw) is bytes and len(raw)<=MAX_METADATA_BYTES,'metadata_size')
    quoted=escaped=False;depth=0
    for c in raw:
        if quoted:
            if escaped:escaped=False
            elif c==92:escaped=True
            elif c==34:quoted=False
        elif c==34:quoted=True
        elif c in (91,123):
            depth+=1;require(depth<=16,'metadata_depth')
        elif c in (93,125):depth-=1;require(depth>=0,'metadata_depth')
    require(depth==0 and not quoted,'metadata_syntax')
    def pairs(rows):
        output={}
        for key,value in rows:
            require(key not in output,'duplicate_key');output[key]=value
        return output
    def constant(_):raise Hold('nonfinite_metadata')
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as e:raise Hold('metadata_syntax') from e

def exact_version(value):
    require(type(value) is str and value==value.strip() and len(value)<=96,'exact_version')
    SemVer.parse(value)

@dataclass(frozen=True)
class FixtureAdmission:
    component_id: ComponentId
    software_version: str
    manifest_sha256: str
    artifact_sha256: str
    envelope_sha256: str
    catalog_epoch: int
    dependencies: tuple
    reader_interfaces: tuple
    trust_scope: str = TEST_TRUST

    @property
    def production_install_authorized(self):return False

def admit_fixture(envelope_bytes, artifact_bytes, signature_bytes, *, verifier,
                  expected_component, expected_version, expected_epoch):
    """Injected verifier is a TEST fixture seam, never publisher authentication."""
    require(type(expected_component) is ComponentId,'expected_component')
    exact_version(expected_version)
    require(type(expected_epoch) is int and expected_epoch>0,'expected_epoch')
    require(type(artifact_bytes) is bytes and len(artifact_bytes)<=MAX_ARTIFACT_BYTES,'artifact_size')
    require(type(signature_bytes) is bytes and len(signature_bytes)<=1024,'signature_size')
    require(callable(verifier),'test_verifier_required')
    e=bounded_json(envelope_bytes)
    keys={'schema','trust_scope','component_id','software_version','platform','channel','epoch',
          'artifact_sha256','artifact_size','manifest_sha256','dependencies','reader_interfaces'}
    require(type(e) is dict and set(e)==keys,'envelope_schema')
    require(type(e['schema']) is int and e['schema']==1,'envelope_schema')
    require(e['trust_scope']==TEST_TRUST and e['channel']=='offline-test','test_trust_only')
    require(e['platform']=={'os':'windows','architecture':'x86_64'},'platform')
    require(type(e['epoch']) is int and e['epoch']==expected_epoch,'catalog_epoch')
    require(e['component_id']==expected_component.value,'component_identity')
    exact_version(e['software_version']);require(e['software_version']==expected_version,'version_identity')
    require(type(e['artifact_size']) is int and e['artifact_size']==len(artifact_bytes),'artifact_size')
    require(all(type(e[k]) is str and re.fullmatch('[0-9a-f]{64}',e[k]) for k in ('artifact_sha256','manifest_sha256')),'exact_hash')
    require(digest(artifact_bytes)==e['artifact_sha256'],'artifact_tamper')
    require(verifier(envelope_bytes,signature_bytes) is True,'test_signature_rejected')
    # Real developer ZIPs and executable package formats can never enter simulation.
    package=bounded_json(artifact_bytes)
    require(type(package) is dict and set(package)=={'kind','component_id','software_version','manifest'},'fixture_package')
    require(package['kind']=='INERT_OFFLINE_FIXTURE' and package['component_id']==e['component_id']
            and package['software_version']==e['software_version'],'fixture_package')
    require(type(package['manifest']) is dict and digest(canonical(package['manifest']))==e['manifest_sha256'],'manifest_tamper')
    require(type(e['dependencies']) is list and len(e['dependencies'])<=16,'dependencies')
    deps=[]
    for dep in e['dependencies']:
        require(type(dep) is dict and set(dep)=={'component_id','software_version'},'dependencies')
        component=ComponentId(dep['component_id']);exact_version(dep['software_version'])
        deps.append((component,dep['software_version']))
    require(len({c for c,v in deps})==len(deps),'duplicate_dependency')
    interfaces=e['reader_interfaces']
    require(type(interfaces) is list and 1<=len(interfaces)<=16 and all(type(i) is int and 1<=i<=65535 for i in interfaces)
            and interfaces==sorted(set(interfaces)),'reader_interfaces')
    require(package['manifest']=={'dependencies':e['dependencies'],'reader_interfaces':interfaces},'manifest_binding')
    return FixtureAdmission(expected_component,expected_version,e['manifest_sha256'],e['artifact_sha256'],
                            digest(envelope_bytes),e['epoch'],tuple(deps),tuple(interfaces))
