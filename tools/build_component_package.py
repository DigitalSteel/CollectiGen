"""Package explicit frozen Windows Suite staging bytes; no freezing or signing."""
from __future__ import annotations

import argparse
import importlib.util
import shutil
from hashlib import sha256
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from suite_core.installation import ComponentId, _relative
from suite_core.discovery.component_release_signature import _json, _version, MAX_METADATA_BYTES, inventory_json
from suite_core.discovery.component_staging import INVENTORY, MAX_ARCHIVE, MAX_UNPACKED, guard, canonical, require, read_metadata_bytes
from suite_core.discovery.offline_component_producer import produce_inventory
from suite_core.discovery.component_payload_policy import select_payload_policy, payload_relative

COMPONENTS = {'collectigen.suite-manager': 'manager', 'collectigen.tag-encyclopedia': 'encyclopedia',
              'collectigen.media-organizer': 'organizer'}
MUTABLE = {'data', 'models', 'catalogs', 'saves', 'logs', 'backups', '.git', '.venv', '__pycache__'}
ORGANIZER_PTH = b'python313.zip\n.\nLib\\site-packages\n..\\..\nimport site\n'


def _mutable_parts(name,runtime_kind):
    policy=select_payload_policy(ComponentId('collectigen.media-organizer'),'windows','x86_64',runtime_kind)
    parts=payload_relative(name,payload_policy=policy)
    # Exempt only the measured vendor directory position, never its suffix.
    vendors=('Runtime/python/Lib/site-packages/pip/_internal/models',
             'Runtime/python/Lib/site-packages/PySide6/qml/QtQml/Models',
             'Runtime/exiftool/exiftool_files/lib/Data',
             'Runtime/exiftool/exiftool_files/lib/auto/Data')
    exempt=set()
    if runtime_kind=='interpreter-portable':
        for prefix in vendors:
            if name==prefix or name.startswith(prefix+'/'):
                exempt.add(len(prefix.split('/'))-1)
    return {part.casefold() for index,part in enumerate(parts)
            if index not in exempt and part.casefold() in MUTABLE}


def _file_sha(path):
    digest=sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def _software_files(root,policy):
    """Use original path guard before traversal; finite package preparation only."""
    guard(root)
    require(root.is_dir(),'assembly_input_directory')
    rows=[]
    count=0
    def visit(directory):
        nonlocal count
        for path in sorted(directory.iterdir()):
            count+=1
            require(count<=policy.max_assembly_tree_entries,'assembly_tree_bounds')
            guard(path)
            if path.is_dir(): visit(path)
            else:
                require(path.is_file(),'assembly_regular_input')
                rows.append(path)
    visit(root)
    return rows


def _generated_cache_source(path):
    if path.suffix.casefold() not in {'.pyc','.pyo'}: return None
    try: source=Path(importlib.util.source_from_cache(str(path)))
    except ValueError: source=path.with_suffix('.py')
    guard(source)
    return source if source.is_file() else None


SHIPPING_REQUIRED_ASSETS = frozenset({
    'Start_MediaOrganizer.bat', 'tools/Test-WindowsPrerequisites.ps1',
    'README.md', 'TESTING.md', 'RELEASE_NOTES.md', 'docs/USER_GUIDE.md',
    'WINDOWS_PREREQUISITES.md', 'SUPPORT.md', 'PRIVACY.md', 'SECURITY.md',
    'LICENSE', 'LICENSES.md', 'THIRD_PARTY_NOTICES.md',
    'suite_core/media_organizer/MODULE_MANIFEST.json',
})


def _admit_organizer_shipping(descriptor, asset_inputs, *, software_version, policy):
    """Bind reviewed shipping inputs; this grants no installation/native authority."""
    require(type(descriptor) is dict and set(descriptor) == {
        'schema_version', 'component_id', 'software_version', 'runtime_kind',
        'runtime_profile', 'entrypoint', 'assets'}, 'shipping_descriptor_schema')
    require(type(descriptor['schema_version']) is int and descriptor['schema_version'] == 1,
            'shipping_descriptor_schema')
    require(descriptor['component_id'] == 'collectigen.media-organizer'
            and descriptor['software_version'] == software_version
            and descriptor['runtime_kind'] == policy.runtime_kind == 'interpreter-portable'
            and descriptor['runtime_profile'] == 'organizer-image'
            and descriptor['entrypoint'] == 'Start_MediaOrganizer.bat', 'shipping_identity')
    rows=descriptor['assets']
    require(type(rows) is list and len(rows) <= policy.max_inventory_members,
            'shipping_asset_rows')
    approved={};seen=set()
    for row in rows:
        require(type(row) is dict and set(row) == {'source','destination','size_bytes','sha256'},
                'shipping_asset_row')
        name=row['destination']
        payload_relative(name,payload_policy=policy)
        require(name.casefold() not in seen, 'shipping_duplicate_destination')
        seen.add(name.casefold())
        require(type(row['source']) is str and type(row['size_bytes']) is int
                and row['size_bytes'] >= 0 and type(row['sha256']) is str
                and len(row['sha256']) == 64
                and all(c in '0123456789abcdef' for c in row['sha256']), 'shipping_asset_pin')
        source=Path(row['source']);guard(source)
        require(source.is_file() and source.stat().st_size == row['size_bytes']
                and _file_sha(source) == row['sha256'], 'shipping_asset_changed')
        approved[name]=(source.resolve(),row['sha256'])
    require(SHIPPING_REQUIRED_ASSETS <= set(approved), 'shipping_required_inputs')
    actual={}
    for source,name,digest in asset_inputs:
        require(name not in actual, 'shipping_duplicate_tuple')
        actual[name]=(Path(source).resolve(),digest)
    require(actual == approved, 'shipping_tuple_closure')
    module=_json(approved['suite_core/media_organizer/MODULE_MANIFEST.json'][0].read_bytes(),
                 'shipping module manifest')
    require(module.get('module_id') == descriptor['component_id']
            and module.get('software_version') == software_version
            and type(module.get('interface_version')) is int and module['interface_version'] == 1
            and module.get('entrypoint') == descriptor['entrypoint']
            and module.get('runtime_kind') == descriptor['runtime_kind']
            and module.get('runtime_profile') == descriptor['runtime_profile'],
            'shipping_module_binding')
    return sha256(canonical(descriptor)).hexdigest()


def assemble_organizer_interpreter(runtime_root,source_inputs_root,output_root,*,
        software_version,runtime_kind,expected_runtime_manifest_sha256,
        expected_source_receipt_sha256,asset_inputs=(),shipping_descriptor=None):
    """Prepare fresh immutable bytes; never install, repair, launch, sign or publish.

    Assets are explicit (source path, package-relative path, expected SHA256)
    tuples supplied by the coordinator's approved resource owner.
    """
    policy=select_payload_policy(ComponentId('collectigen.media-organizer'),'windows','x86_64',runtime_kind)
    require(policy.runtime_kind=='interpreter-portable','explicit_interpreter_assembly')
    _version(software_version)
    asset_inputs=tuple(asset_inputs)
    shipping_pin=None
    if shipping_descriptor is not None:
        shipping_pin=_admit_organizer_shipping(shipping_descriptor,asset_inputs,
                    software_version=software_version,policy=policy)
    runtime,prepared,output=map(Path,(runtime_root,source_inputs_root,output_root))
    for path in (runtime,prepared,output): guard(path)
    runtime,prepared,output=map(lambda p:p.resolve(),(runtime,prepared,output))
    require(not output.exists() and output.parent.is_dir(),'fresh_assembly_destination')
    require(output.name==software_version,'versioned_assembly_destination')
    require(not any(output.is_relative_to(p) or p.is_relative_to(output) for p in (runtime,prepared)),
            'separate_assembly_destination')
    stock=runtime/'python/collectigen-runtime.json'
    receipt_path=prepared/'INPUTS.json'
    for path in (stock,receipt_path): guard(path)
    require(_file_sha(stock)==expected_runtime_manifest_sha256,'runtime_input_changed')
    require(_file_sha(receipt_path)==expected_source_receipt_sha256,'source_receipt_changed')
    base=_json(stock.read_bytes(),'builder runtime')
    receipt=inventory_json(receipt_path.read_bytes(),max_bytes=policy.max_inventory_json_bytes)
    require(base.get('schema_version')==2 and base.get('profile')=='organizer-image'
            and base.get('python_version')=='3.13.15' and base.get('architecture')=='amd64',
            'stock_image_runtime_required')
    require(receipt.get('runtime_scope')=='public','public_source_preparation_required')
    source_rows=receipt.get('source_rows')
    require(type(source_rows) is list and bool(source_rows),'source_rows')
    copies={}
    seen=set()
    directories=set()
    excluded=[]
    def admit(source,name,expected=None):
        parts=payload_relative(name,payload_policy=policy)
        require(not any(part.casefold() in {'incomplete.json','runtime_manifest.json','bootstrap_inventory.json'} for part in parts),
                'reserved_assembly_destination')
        require(len(name)<=policy.max_relative_path_characters,'assembly_path_bounds')
        blocked=_mutable_parts(name,runtime_kind)
        orphan_cache=(blocked=={'__pycache__'} and name.startswith('Runtime/python/')
                      and source.suffix.casefold() in {'.pyc','.pyo'}
                      and _generated_cache_source(source) is None)
        require(not blocked or orphan_cache,'mutable_assembly_path')
        folded=name.casefold()
        require(folded not in seen,'duplicate_assembly_path')
        parents={'/'.join(parts[:index]).casefold() for index in range(1,len(parts))}
        require(folded not in directories and not parents.intersection(seen),
                'assembly_file_directory_conflict')
        guard(source)
        require(source.is_file(),'assembly_regular_file')
        digest=_file_sha(source)
        require(expected is None or digest==expected,'assembly_source_changed')
        seen.add(folded);directories.update(parents)
        copies[name]=(source,source.stat().st_size,digest)
    for folder in ('python','exiftool'):
        for source in _software_files(runtime/folder,policy):
            counterpart=_generated_cache_source(source)
            if counterpart is not None:
                excluded.append({'path':source.relative_to(runtime).as_posix(),
                                 'source':counterpart.relative_to(runtime).as_posix(),
                                 'sha256':_file_sha(source),'size_bytes':source.stat().st_size})
                continue
            admit(source,'Runtime/'+source.relative_to(runtime).as_posix())
    declared=set()
    for row in source_rows:
        require(type(row) is dict and type(row.get('path')) is str,'source_row')
        require(type(row.get('sha256')) is str and len(row['sha256'])==64
                and all(c in '0123456789abcdef' for c in row['sha256']),'source_row_pin')
        name=row['path'];declared.add(name)
        require(name.endswith('.py'),'source_python_only')
        admit(prepared/'source'/name,name,row.get('sha256'))
    actual={p.relative_to(prepared/'source').as_posix() for p in _software_files(prepared/'source',policy)}
    require(actual==declared,'prepared_source_closure')
    for source,name,expected in asset_inputs:
        source=Path(source)
        # Known retained raw seed inputs are never package assets, regardless of label.
        source_parts=tuple(p.casefold() for p in source.parts)
        require('public-seed-inputs' not in source_parts
                and not any(source_parts[i:i+2]==('data','defaults') for i in range(len(source_parts)-1)),
                'raw_seed_assets_held')
        require(type(expected) is str and len(expected)==64,'explicit_asset_pin')
        admit(source,name,expected)
    required={'Runtime/python/python.exe','Runtime/python/python313._pth',
              'Runtime/exiftool/exiftool.exe','suite_core/media_organizer/__main__.py',
              'suite_core/media_organizer/MODULE_MANIFEST.json','tools/runtime-pins.json',
              'tools/optional-ai-profiles.json','requirements-organizer-image-runtime.lock',
              'LICENSE','LICENSES.md','THIRD_PARTY_NOTICES.md'}
    require(required<=set(copies),'assembly_required_inputs')
    module=_json(copies['suite_core/media_organizer/MODULE_MANIFEST.json'][0].read_bytes(),'module manifest')
    require(module.get('module_id')=='collectigen.media-organizer'
            and module.get('software_version')==software_version,'assembly_module_identity')
    # Confirm the builder's declared native critical bytes before relocation.
    for name,digest in base.get('critical_files',{}).items():
        require(copies.get('Runtime/'+name,(None,None,None))[2]==digest,'builder_critical_pin')
    overrides={'Runtime/python/python313._pth':ORGANIZER_PTH}
    total=sum(len(overrides[name]) if name in overrides else row[1] for name,row in copies.items())
    require(len(copies)+1<=policy.max_inventory_members and total<=policy.max_unpacked_bytes,'assembly_payload_bounds')
    # Pin every input again before creating output; source and builder trees stay untouched.
    require(all(_file_sha(row[0])==row[2] for row in copies.values()),'assembly_input_changed')
    output.mkdir()
    incomplete=output/'INCOMPLETE.json'
    incomplete.write_bytes(canonical({'production_install_authorized':False}))
    rows=[]
    try:
        for name,(source,size,digest) in sorted(copies.items()):
            target=output.joinpath(*payload_relative(name,payload_policy=policy));target.parent.mkdir(parents=True,exist_ok=True)
            if name in overrides: target.write_bytes(overrides[name])
            else: shutil.copyfile(source,target)
            expected=sha256(overrides[name]).hexdigest() if name in overrides else digest
            require(_file_sha(target)==expected,'assembly_copy_readback')
            rows.append({'path':name,'size_bytes':target.stat().st_size,'sha256':expected})
        manifest=canonical({'schema_version':1,'component':'organizer','feature_version':software_version,
            'os':'windows','architecture':'x86_64','runtime_kind':runtime_kind,
            'capability_profile':'organizer-image','inventory_excludes_itself':'RUNTIME_MANIFEST.json',
            'cache_projection':{'builder_manifest_sha256':expected_runtime_manifest_sha256,
                                'excluded_generated_bytecode':excluded},'files':rows})
        require(len(manifest)<=policy.max_inventory_json_bytes and total+len(manifest)<=policy.max_unpacked_bytes,
                'assembly_manifest_bounds')
        (output/'RUNTIME_MANIFEST.json').write_bytes(manifest)
        incomplete.unlink()
        tree=list(output.rglob('*'))
        require(len(tree)<=policy.max_assembly_tree_entries,'assembly_final_tree_bounds')
        return {'schema_version':1,'component_id':'collectigen.media-organizer','software_version':software_version,
            'runtime_kind':runtime_kind,'file_count':len(rows)+1,'payload_bytes':total+len(manifest),
            'runtime_manifest_bytes':len(manifest),'runtime_manifest_sha256':sha256(manifest).hexdigest(),
            'source_receipt_sha256':expected_source_receipt_sha256,'builder_manifest_sha256':expected_runtime_manifest_sha256,
            'excluded_cache_files':len(excluded),'excluded_cache_bytes':sum(r['size_bytes'] for r in excluded),
            'final_pth_sha256':sha256(ORGANIZER_PTH).hexdigest(),'tree_entries':len(tree),
            'assembly_complete':True,'shipping_inputs_admitted':shipping_pin is not None,
            'shipping_descriptor_sha256':shipping_pin,'Manager_admitted':False,'native_health_verified':False,
            'same_version_installed_repair_supported':False}
    except Exception:
        incomplete.write_bytes(canonical({'state':'incomplete','production_install_authorized':False}))
        raise


def _producer_identity_inputs(reference, *, component_id, software_version, os_name,
        architecture, supported_from, published_at, release_notes, module_bytes, runtime):
    """Read only explicitly pinned public preparation inputs; never scan a cohort."""
    from core.component_release_identity import validate_producer_source_identity, require_component_maturity_upgrade, ComponentReleaseIdentity
    from suite_core.discovery.component_release_signature import verify_component_release
    held = {}

    def read(path, limit=MAX_METADATA_BYTES):
        require(type(path) is str and Path(path).is_absolute(), 'identity_absolute_path')
        p = Path(path); guard(p)
        parts = path.replace('\\', '/').split('/')
        require(all(t not in ('.', '..') and (bool(t) or i == 0 and path.startswith('/'))
            for i,t in enumerate(parts)), 'identity_path_alias')
        require(not any(t.casefold() in MUTABLE | {'state'} for t in p.parts), 'identity_mutable_path')
        require(p.is_file() and p.stat().st_size <= limit, 'identity_input_bounds')
        with p.open('rb') as stream: content = stream.read(limit + 1)
        require(len(content) <= limit, 'identity_input_bounds'); guard(p)
        key = str(p.resolve()).casefold()
        require(key not in {str(v[0].resolve()).casefold() for v in held.values()}, 'identity_input_alias')
        held[path] = (p, content)
        return content

    def pinned(row, limit=MAX_METADATA_BYTES):
        require(type(row) is dict and set(row) == {'path', 'sha256'}, 'identity_pin_schema')
        require(type(row['sha256']) is str and len(row['sha256']) == 64
            and all(c in '0123456789abcdef' for c in row['sha256']), 'identity_sha256')
        value = read(row['path'], limit)
        require(sha256(value).hexdigest() == row['sha256'], 'identity_input_pin_changed')
        return value

    descriptor = read(str(reference) if isinstance(reference, Path) else reference)
    value = _json(descriptor, 'producer identity inputs')
    require(set(value) == {'schema_version', 'purpose', 'component_id', 'version', 'maturity',
        'source_version', 'source_module', 'source_inventory', 'release_history', 'release_notes', 'baseline'}
        and type(value['schema_version']) is int and value['schema_version'] == 1
        and value['purpose'] == 'COMPONENT_SCHEMA2_PRODUCER_INPUTS', 'identity_inputs_schema')
    require(value['component_id'] == component_id and value['version'] == software_version, 'producer_identity_binding')
    identity = ComponentReleaseIdentity(value['version'], value['maturity'])
    inventory = inventory_json(pinned(value['source_inventory'], 1024*1024), max_bytes=1024*1024)
    inventory_path = Path(value['source_inventory']['path'])
    require(inventory_path.name == 'public-source.json' and type(inventory.get('schema_version')) is int
        and inventory['schema_version'] == 1 and type(inventory.get('files')) is dict
        and inventory.get('export_scope') == 'component-source'
        and inventory.get('fresh_history_required') is True and inventory.get('manifest_excludes_self') is True,
        'source_inventory_schema')
    cohort = inventory_path.parent.resolve()
    content = {}
    for role in ('source_version', 'source_module', 'release_history', 'release_notes'):
        row = value[role]; data = pinned(row)
        source = Path(row['path']).resolve()
        require(source.is_relative_to(cohort), 'identity_source_escape')
        name = source.relative_to(cohort).as_posix(); entry = inventory['files'].get(name)
        require(type(entry) is dict and set(entry) == {'sha256', 'size_bytes'}
            and type(entry['size_bytes']) is int and entry['size_bytes'] == len(data)
            and entry['sha256'] == sha256(data).hexdigest(), 'identity_source_not_in_inventory')
        content[role] = data
    require(content['source_module'] == module_bytes, 'staged_source_module_drift')
    require(content['release_notes'].decode('utf-8') == release_notes, 'canonical_notes_changed')
    details = validate_producer_source_identity(content['source_version'], _json(module_bytes, 'source module'),
        _json(content['release_history'], 'source history'), runtime, component_id=component_id,
        version=software_version, maturity=identity.maturity, published_at=published_at, notes=content['release_notes'])
    baseline = value['baseline']
    require(type(baseline) is dict and set(baseline) == {'manifest', 'signature', 'trust'}, 'baseline_inputs_schema')
    message, signature, trust = (pinned(baseline[k]) for k in ('manifest', 'signature', 'trust'))
    untrusted = _json(message, 'baseline manifest')
    require(type(untrusted.get('schema_version')) is int and untrusted['schema_version'] in (1, 2), 'baseline_schema')
    verified = verify_component_release(message, signature, trust, expected_component=ComponentId(component_id),
        expected_version=untrusted.get('version'), expected_channel=untrusted.get('channel'), expected_os=os_name,
        expected_architecture=architecture, expected_maturity=untrusted.get('maturity') if untrusted['schema_version']==2 else None)
    require_component_maturity_upgrade(verified.software_version, identity, current_schema=verified.schema_version,
        current_maturity=verified.maturity, supported_from=supported_from)
    details.update(source_inventory_sha256=value['source_inventory']['sha256'],
        baseline_manifest_sha256=verified.manifest_sha256,
        input_sha256=dict(identity_inputs=sha256(descriptor).hexdigest(),
            **{role:value[role]['sha256'] for role in ('source_inventory', 'source_version', 'source_module', 'release_history', 'release_notes')},
            **{'baseline_'+role:baseline[role]['sha256'] for role in ('manifest', 'signature', 'trust')}))

    def recheck():
        for p, expected in held.values():
            guard(p)
            require(p.is_file() and p.stat().st_size == len(expected), 'producer_input_changed')
            with p.open('rb') as stream: observed = stream.read(len(expected)+1)
            require(observed == expected, 'producer_input_changed')
    return details, recheck


def build_package(payload_root, output_root, *, runtime_manifest, module_manifest,
                  component_id, software_version, os_name, architecture, artifact_name,
                  supported_from, published_at, release_notes, require_update_data_contract=False,
                  runtime_kind="frozen-portable", identity_inputs=None):
    """Fail closed on mismatched inventory/identity; output is unsigned and inactive."""
    require(component_id in COMPONENTS, 'suite_component_required')
    policy=select_payload_policy(ComponentId(component_id),os_name,architecture,runtime_kind)
    require(type(require_update_data_contract) is bool, 'explicit_update_contract_requirement')
    require(not require_update_data_contract or component_id == 'collectigen.tag-encyclopedia',
            'encyclopedia_update_contract_required')
    require(os_name == 'windows' and architecture == 'x86_64', 'builder_windows_x64_only')
    version, minimum = _version(software_version), _version(supported_from)
    require(minimum.compare(version) <= 0, 'supported_from_newer_than_target')
    require(type(published_at) is str and 0 < len(published_at) <= 64
            and all(32 <= ord(c) < 127 for c in published_at), 'publication_date')
    require(type(release_notes) is str and bool(release_notes.strip()) and len(release_notes) <= 100000,
            'release_notes')
    require(type(artifact_name) is str and len(artifact_name) <= 255
            and len(_relative(artifact_name)) == 1 and artifact_name.endswith('.zip'), 'artifact_basename')
    _relative(runtime_manifest)
    _relative(module_manifest)
    source, output = Path(payload_root), Path(output_root)
    guard(source)
    guard(output)
    source, output = source.resolve(), output.resolve()
    guard(source)
    guard(output)
    require(source.is_dir() and not output.exists(), 'explicit_payload_and_fresh_output_required')
    require(not output.is_relative_to(source) and not source.is_relative_to(output), 'separate_output_required')
    members = []
    count = total = 0
    for path in source.rglob('*'):
        count += 1
        require(count <= (policy.max_assembly_tree_entries or 1024), 'payload_entry_bounds')
        guard(path)
        name = path.relative_to(source).as_posix()
        parts = payload_relative(name,payload_policy=policy)
        blocked=_mutable_parts(name,runtime_kind)
        orphan_cache=(runtime_kind=='interpreter-portable' and blocked=={'__pycache__'}
            and name.startswith('Runtime/python/') and (path.is_dir()
                or (path.suffix.casefold() in {'.pyc','.pyo'} and _generated_cache_source(path) is None)))
        require(not blocked or orphan_cache, 'mutable_or_generated_payload')
        require(not any(p.casefold() == INVENTORY.casefold() for p in parts), 'inventory_already_present')
        if path.is_dir():
            continue
        require(path.is_file(), 'regular_payload_only')
        total += path.stat().st_size
        require(total <= policy.max_unpacked_bytes and len(members) < policy.max_inventory_members, 'payload_bounds')
        members.append((name, path.read_bytes()))
    members = tuple(sorted(members))
    by_name = dict(members)
    require(len(by_name) == len(members) and runtime_manifest in by_name and module_manifest in by_name,
            'required_manifests')
    runtime = (_json(by_name[runtime_manifest], 'runtime manifest') if runtime_kind=='frozen-portable'
               else inventory_json(by_name[runtime_manifest],max_bytes=policy.max_inventory_json_bytes))
    module = _json(by_name[module_manifest], 'module manifest')
    if runtime_kind=='interpreter-portable':
        require(runtime.get('runtime_kind')==runtime_kind and runtime.get('capability_profile')=='organizer-image',
                'interpreter_runtime_declaration')
        require('Runtime/python/python.exe' in by_name and 'suite_core/media_organizer/__main__.py' in by_name
                and by_name.get('Runtime/python/python313._pth')==ORGANIZER_PTH, 'interpreter_layout')
    require(type(runtime.get('schema_version')) is int and runtime['schema_version'] == 1
            and runtime.get('component') == COMPONENTS[component_id]
            and runtime.get('feature_version') == software_version, 'runtime_identity')
    require(runtime.get('os') == os_name and runtime.get('architecture') == architecture, 'runtime_platform')
    require(type(module.get('schema_version')) is int and module['schema_version'] == 1
            and module.get('module_id') == component_id and module.get('software_version') == software_version,
            'module_identity')
    require(runtime.get('inventory_excludes_itself') == 'RUNTIME_MANIFEST.json'
            and Path(runtime_manifest).name == 'RUNTIME_MANIFEST.json', 'runtime_self_exclusion')
    update_contract_validated = False
    if component_id == 'collectigen.tag-encyclopedia' and (
            require_update_data_contract or 'update_data_contract' in module):
        # Reuse the original updater's exact schema; never synthesize compatibility.
        # Its fixed installed/target module path must be inventoried verbatim.
        require(module_manifest == 'TagEncyclopedia/MODULE_MANIFEST.json',
                'signed_component_schema_manifest_path_required')
        from core.encyclopedia_update_binding import _data_contract
        compatibility = _data_contract(by_name[module_manifest], software_version)
        snapshots = compatibility.snapshot_interfaces
        declared = module.get('supported_snapshot_interface_versions')
        maximum = module.get('snapshot_interface_version')
        require(type(declared) is list and all(type(v) is int for v in declared)
                and tuple(sorted(declared)) == snapshots and type(maximum) is int
                and maximum == max(snapshots), 'update_snapshot_declaration_mismatch')
        update_contract_validated = True  # schema only, never installed-data health
    identity = recheck_identity = None
    if identity_inputs is not None:
        identity, recheck_identity = _producer_identity_inputs(identity_inputs, component_id=component_id,
            software_version=software_version, os_name=os_name, architecture=architecture,
            supported_from=supported_from, published_at=published_at, release_notes=release_notes,
            module_bytes=by_name[module_manifest], runtime=runtime)
    rows = runtime.get('files')
    require(type(rows) is list and 0 < len(rows) <= policy.max_inventory_members, 'runtime_file_rows')
    base = Path(runtime_manifest).parent.as_posix()
    expected = {}
    for row in rows:
        require(type(row) is dict and set(row) == {'path', 'size_bytes', 'sha256'}, 'runtime_row_schema')
        payload_relative(row['path'],payload_policy=policy)
        name = row['path'] if base == '.' else base + '/' + row['path']
        require(name not in expected, 'duplicate_runtime_row')
        expected[name] = row
    require(set(by_name) == set(expected) | {runtime_manifest}, 'runtime_inventory_closure')
    for name, row in expected.items():
        require(type(row['size_bytes']) is int and row['size_bytes'] == len(by_name[name])
                and row['sha256'] == sha256(by_name[name]).hexdigest(), 'runtime_file_pin')
    inventory = produce_inventory(members, component_id=ComponentId(component_id),
        software_version=software_version, os_name=os_name, architecture=architecture, runtime_kind=runtime_kind)
    require(total + len(inventory) <= policy.max_unpacked_bytes, 'final_payload_bounds')
    # Stale guard before creating any output. ZIP reads the admitted byte snapshot.
    require(all((source / n).is_file() and (source / n).read_bytes() == v for n, v in members), 'payload_changed')
    require({p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()} == set(by_name),
            'payload_closure_changed')
    if recheck_identity is not None: recheck_identity()
    output.mkdir()  # no force/overwrite, no deletion or alteration of input
    incomplete = output / 'INCOMPLETE.json'
    incomplete.write_bytes(canonical({'state': 'incomplete', 'production_install_authorized': False}))
    archive = output / artifact_name
    with archive.open('xb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, content in sorted((*members, (INVENTORY, inventory))):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content, compresslevel=9)
    require(archive.stat().st_size <= policy.max_archive_bytes, 'archive_size')
    with zipfile.ZipFile(archive) as z:
        require(z.namelist() == sorted((*by_name, INVENTORY)), 'output_closure')
        require(all(z.read(n) == v for n, v in (*members, (INVENTORY, inventory))), 'output_readback')
    envelope_value = {'schema_version': 1, 'product': 'collectigen', 'component_id': component_id,
        'version': software_version, 'channel': 'beta' if version.prerelease else 'stable',
        'published_at': published_at, 'supported_from': supported_from, 'release_notes': release_notes,
        'platform': {'os': os_name, 'architecture': architecture},
        'artifacts': [{'name': artifact_name, 'kind': 'component-package', 'size_bytes': archive.stat().st_size,
            'sha256': sha256(archive.read_bytes()).hexdigest(), 'portable_manifest_sha256': sha256(inventory).hexdigest()}]}
    if identity is not None:
        recheck_identity()
        envelope_value.update(schema_version=2, maturity=identity['maturity'],
            channel='stable' if identity['maturity']=='stable' else 'beta')
    envelope = canonical(envelope_value)
    require(len(envelope) <= MAX_METADATA_BYTES, 'envelope_size')
    (output / 'release-manifest.input.json').write_bytes(envelope)
    (output / INVENTORY).write_bytes(inventory)
    receipt = {'schema_version': 1, 'component_id': component_id, 'software_version': software_version,
        'payload_files': len(members), 'archive_sha256': sha256(archive.read_bytes()).hexdigest(),
        'manifest_sha256': sha256(envelope).hexdigest(), 'inventory_sha256': sha256(inventory).hexdigest(),
        'production_install_authorized': False, 'native_health_verified': False, 'signed': False,
        'update_data_contract_schema_validated': update_contract_validated}
    if identity is not None: receipt.update(release_schema_version=2, release_identity=identity)
    (output / 'packaging-receipt.json').write_bytes(canonical(receipt))
    if recheck_identity is not None: recheck_identity()
    incomplete.unlink()  # only the exact marker just created in this fresh output
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('payload-root', 'output-root', 'runtime-manifest', 'module-manifest', 'component-id',
                 'software-version', 'os-name', 'architecture', 'artifact-name', 'supported-from',
                 'published-at', 'release-notes-file'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--runtime-kind',choices=('frozen-portable','interpreter-portable'),default='frozen-portable')
    parser.add_argument('--require-update-data-contract', action='store_true',
        help='Require the existing Encyclopedia module compatibility schema; does not attest data compatibility.')
    parser.add_argument('--identity-inputs', help='Explicit pinned schema2 producer identity inputs; no legacy fallback.')
    values = vars(parser.parse_args())
    notes = Path(values.pop('release_notes_file'))
    values['release_notes'] = read_metadata_bytes(notes).decode('utf-8') if values['identity_inputs'] is not None else notes.read_text(encoding='utf-8')
    print(canonical(build_package(**values)).decode())


if __name__ == '__main__':
    main()
