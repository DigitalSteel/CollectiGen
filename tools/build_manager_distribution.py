"""Build-only Manager producer from a pinned public source stage; never launches apps."""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys

TEMPLATES = {'CollectiGenManager.spec', 'CollectiGen.spec',
             'sdk_binary_origin.py', 'windows_build_isolation.py', 'hooks/hook-core.py'}
HERE = Path(__file__).resolve().parent / 'manager_build'
MAX_FILE = 256 * 1024 * 1024


class BuildHold(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise BuildHold(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(value):
    require(type(value) is str and value and '\\' not in value and ':' not in value,
            'exact_relative_path_required')
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(p not in {'.', '..', ''} for p in path.parts)
            and path.as_posix() == value, 'exact_relative_path_required')
    require(not any(p.casefold().startswith(('private_', '.')) for p in path.parts),
            'private_source_refused')
    return value


def unredirected(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        require(not part.is_symlink() and not part.is_junction(), 'redirected_input_or_output')
    return path


def read(path):
    path = unredirected(path)
    require(path.is_file() and path.stat().st_size <= MAX_FILE, 'bounded_input_required')
    raw = path.read_bytes()
    require(len(raw) <= MAX_FILE, 'bounded_input_required')
    return raw


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_metadata_key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BuildHold('invalid_metadata') from error


def pinned(path, expected):
    require(type(expected) is str and re.fullmatch('[0-9a-f]{64}', expected), 'exact_digest_required')
    raw = read(path)
    require(digest(raw) == expected, 'input_digest_changed')
    return raw


def rows(values, *, adapter=False):
    require(type(values) is list, 'exact_rows_required')
    result = {}
    folded = set()
    for row in values:
        require(type(row) is dict and set(row) in (
            {'path', 'bytes', 'sha256'}, {'path', 'bytes', 'sha256', 'origin'} if adapter else set()),
            'exact_row_schema_required')
        name = safe_path(row['path'])
        require(name.casefold() not in folded, 'duplicate_source_identity')
        require(type(row['bytes']) is int and 0 <= row['bytes'] <= MAX_FILE, 'bounded_row_size_required')
        require(type(row['sha256']) is str and re.fullmatch('[0-9a-f]{64}', row['sha256']), 'exact_digest_required')
        result[name] = row
        folded.add(name.casefold())
    return result


def check_row(path, row):
    raw = pinned(path, row['sha256'])
    require(len(raw) == row['bytes'], 'input_size_changed')
    return raw


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def write_new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(raw)
    require(path.read_bytes() == raw, 'output_readback_failed')


def validate_sources(stage, stage_sha256, manifest_raw, inventory_raw, *, component='manager'):
    require(component in {'manager', 'generate', 'train'}, 'finite_frozen_component_required')
    receipt_raw = pinned(stage / 'STAGE.json', stage_sha256)
    receipt = decode(receipt_raw)
    require(type(receipt) is dict and receipt.get('schema') == 1, 'source_stage_schema_required')
    require(receipt.get('frozen_sources_sha256') == digest(manifest_raw), 'frozen_subset_changed')
    manifest = decode(manifest_raw)
    require(set(manifest) == {'schema', 'source_rows'} and manifest['schema'] == 1,
            'source_manifest_schema_required')
    expected_rows = rows(manifest['source_rows'])
    require(expected_rows, 'explicit_frozen_subset_required')
    expected = set(expected_rows)
    required_entries = {'core/manager_launch_owner.py', 'suite_core/manager/bootstrap_entry.py',
                        'suite_core/manager/entry_binding.py', 'suite_core/manager/frozen_entry.py',
                        'suite_core/manager/launcher_main.py', 'suite_core/manager/MODULE_MANIFEST.json'}
    if component != 'manager':
        require(receipt.get('component') == component, 'same_frozen_component_required')
        require(not {'suite_core/manager/window.py', 'suite_core/manager/__main__.py',
                     'suite_core/manager/launcher_main.py', 'suite_core/manager/frozen_entry.py'} & expected,
                'workflow_management_entry_forbidden')
        required_entries = {'suite_core/' + component + '/__main__.py',
                            'suite_core/' + component + '/MODULE_MANIFEST.json',
                            'suite_core/workflow_modules/entry.py', 'suite_core/workflow_modules/session.py'}
    require(required_entries <= expected, 'canonical_manager_entries_required')
    require(all(n.startswith(('core/', 'suite_core/', 'qt_app/', 'licenses/', 'tools/'))
                or n in {'LICENSE', 'LICENSES.md', 'THIRD_PARTY_NOTICES.md', 'version.py',
                         'runtime_contract.py', 'release_contract.py',
                         'resources/learning/bundled_topics.json',
                         'data/model_workflow_baseline.json'} for n in expected), 'nonpublic_source_scope')
    require(digest(inventory_raw) == receipt.get('public_inventory_sha256'), 'public_inventory_changed')
    inventory = decode(inventory_raw)
    require(inventory.get('schema_version') == 1 and inventory.get('manifest_excludes_self') is True
            and type(inventory.get('files')) is dict, 'public_inventory_required')
    all_rows = rows(receipt.get('source_rows'), adapter=True)
    if component == 'manager':
        require('public_root_entry.py' in all_rows, 'generated_unbound_root_required')
        generated = {'public_root_entry.py': all_rows.pop('public_root_entry.py')}
    else:
        require('public_root_entry.py' not in all_rows, 'workflow_root_launcher_forbidden')
        generated = {}
    source_rows = all_rows
    require(set(source_rows) == expected, 'undeclared_or_missing_source')
    sources = {}
    for name, row in source_rows.items():
        public = inventory['files'].get(name)
        require(all(row[k] == expected_rows[name][k] for k in ('bytes', 'sha256')), 'frozen_source_changed')
        require(type(public) is dict and public.get('sha256') == row['sha256']
                and public.get('size_bytes') == row['bytes'], 'nonpublic_or_changed_source')
        sources[name] = check_row(stage / 'source' / name, row)
    version = receipt.get('software_version')
    require(type(version) is str and re.fullmatch(r'0\.\d+\.\d+(?:-[A-Za-z0-9.]+)?', version),
            'existing_manager_version_required')
    module = decode(sources.get('suite_core/' + component + '/MODULE_MANIFEST.json', b'{}'))
    if component != 'manager':
        require(module.get('schema_version') == 1 and module.get('module_id') == 'collectigen.' + component,
                'same_workflow_runtime_manifest_required')
    require(module.get('software_version') == version, 'manager_version_changed')
    require({name for name in expected if name.endswith('.py') or name.startswith(('tools/', 'resources/'))}
            <= set(module.get('runtime_files', [])),
            'undeclared_runtime_source')
    for name, row in generated.items():
        check_row(stage / 'source' / name, row)
    require(receipt.get('root_package_pin_bound') is False, 'bound_source_stage_refused')
    expected_members = {'STAGE.json'} | {'source/' + n for n in source_rows} | {'source/' + n for n in generated}
    actual = set()
    for path in stage.rglob('*'):
        unredirected(path)
        if path.is_file():
            actual.add(path.relative_to(stage).as_posix())
    require(actual == expected_members, 'undeclared_stage_member')
    return receipt, manifest, sources


def canonical_update_renderers(raw, pins):
    """Admit both stock renderer dependencies without executing module top-level."""
    names = {'render_compiled_update_config': 'compiled_update_renderer_ast_sha256',
             'render_qualification_update_config': 'qualification_update_renderer_ast_sha256'}
    functions = []
    for name, key in names.items():
        found = [node for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef) and node.name == name]
        require(len(found) == 1 and digest(ast.dump(found[0], include_attributes=False).encode()) == pins.get(key),
                'canonical_stock_update_renderer_changed:' + name)
        functions.extend(found)
    namespace = {'re': re, 'hashlib': __import__('hashlib'), 'Path': Path}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<canonical-stock-update-renderers>', 'exec'), namespace)
    return namespace


def canonical_renderer(raw, expected_ast, *, helper_pins=None):
    try:
        tree = ast.parse(raw)
    except SyntaxError as error:
        raise BuildHold('invalid_root_renderer') from error
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'root_entry']
    require(len(functions) == 1 and digest(ast.dump(functions[0], include_attributes=False).encode()) == expected_ast,
            'canonical_root_renderer_changed')
    # Execute only the independently AST-pinned canonical function, not supplied
    # module-level statements or imports. The default call has no profile activation.
    namespace = {'re': re}
    if helper_pins is not None:
        namespace.update(canonical_update_renderers(raw, helper_pins))
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<canonical-root-entry>', 'exec'), namespace)
    return namespace['root_entry']


def canonical_candidate_renderer(raw, expected_ast, *, helper_pins=None):
    """Extract only the separately pinned candidate renderer and its imports."""
    try:
        functions = [node for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef)
                     and node.name == 'render_workflow_candidate_configs']
    except SyntaxError as error:
        raise BuildHold('invalid_candidate_renderer') from error
    require(len(functions) == 1 and digest(ast.dump(functions[0], include_attributes=False).encode()) == expected_ast,
            'canonical_candidate_renderer_changed')
    namespace = {'ast': ast, 're': re}
    if helper_pins is not None:
        namespace.update(canonical_update_renderers(raw, helper_pins))
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<canonical-candidate-config>', 'exec'), namespace)
    return namespace['render_workflow_candidate_configs']


def canonical_organizer_qualification_renderer(raw, pins):
    """Admit only the explicit Organizer renderer and its pinned stock helpers."""
    try:
        functions = [node for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef)
                     and node.name == 'render_organizer_qualification_configs']
    except SyntaxError as error:
        raise BuildHold('invalid_organizer_qualification_renderer') from error
    require(len(functions) == 1 and digest(ast.dump(functions[0], include_attributes=False).encode())
        == pins.get('organizer_qualification_renderer_ast_sha256'),
        'canonical_organizer_qualification_renderer_changed')
    namespace = {'ast': ast, 're': re}
    namespace.update(canonical_update_renderers(raw, pins))
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<canonical-organizer-qualification>', 'exec'), namespace)
    return namespace['render_organizer_qualification_configs']


def inspect_tools(interpreter, profile, environment):
    require(profile['platform'] == {'os': 'windows', 'architecture': 'x86_64', 'python_abi': 'cp314'},
            'windows_x64_abi_required')
    raw = pinned(interpreter, profile['python']['sha256'])
    require(len(raw) == profile['python']['bytes'], 'interpreter_size_changed')
    # WMI-backed platform.machine() can be empty in a normal sandbox after the
    # environment is sanitized. Require actual native Windows architecture;
    # never infer it from an inherited PROCESSOR_ARCHITECTURE value.
    native_probe = ('import ctypes as c; from ctypes import wintypes as w\n'
        'class NativeInfo(c.Structure):\n'
        ' _fields_=[("architecture",w.WORD),("reserved",w.WORD),("page",w.DWORD),'
        '("minimum",c.c_void_p),("maximum",c.c_void_p),("mask",c.c_size_t),'
        '("processors",w.DWORD),("type",w.DWORD),("granularity",w.DWORD),'
        '("level",w.WORD),("revision",w.WORD)]\n'
        'api=c.WinDLL("kernel32",use_last_error=True).GetNativeSystemInfo\n'
        'api.argtypes=[c.POINTER(NativeInfo)]; api.restype=None\n'
        'native_info=NativeInfo(); api(c.byref(native_info))\n')
    code = native_probe + ('import json,sys,struct,platform,importlib.metadata as m; print(json.dumps({'
            '"platform":sys.platform,"bits":struct.calcsize("P")*8,"machine":platform.machine(),'
            '"native_processor_architecture":native_info.architecture,'
            '"version":platform.python_version(),"distributions":{n:m.version(n) for n in '
            + repr(list(profile['distributions'])) + '}}))')
    result = subprocess.run([str(interpreter), '-I', '-B', '-c', code], env=environment,
                            capture_output=True, check=True, text=True)
    observed = decode(result.stdout.encode())
    require(observed['platform'] == 'win32' and observed['bits'] == 64
            and type(observed.get('native_processor_architecture')) is int
            and observed['native_processor_architecture'] == 9
            and type(observed.get('machine')) is str
            and observed['machine'].casefold() in {'', 'amd64', 'x86_64'}
            and observed['version'] == profile['python']['version']
            and observed['distributions'] == profile['distributions'], 'toolchain_drift')


def isolated_environment(output, interpreter):
    system = Path(os.environ['SystemRoot']).resolve()
    environment = {key: os.environ[key] for key in
                   ('SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA')
                   if key in os.environ}
    environment['PATH'] = os.pathsep.join(map(str, (system / 'System32', system, interpreter.parent)))
    environment['PYINSTALLER_CONFIG_DIR'] = str(output / 'isolated-pyinstaller-cache')
    return environment


def freeze(output, interpreter, environment, name, *, recipe=None):
    command = [str(interpreter), '-I', '-B', '-m', 'PyInstaller', '--noconfirm',
               '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'),
               str(output / 'stage' / ((recipe or name) + '.spec'))]
    write_new(output / (name + '_BUILD_COMMAND.json'), json_bytes({'command': command, 'app_launch': False}))
    with (output / (name + '_FREEZER.log')).open('xb') as stream:
        result = subprocess.run(command, cwd=output / 'stage', env=environment,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    require(result.returncode == 0, 'child_freeze_failed' if name == 'CollectiGenManager' else 'root_freeze_failed')


def pe_x64(raw):
    require(len(raw) >= 64 and raw[:2] == b'MZ', 'windows_pe_required')
    offset = struct.unpack_from('<I', raw, 60)[0]
    require(offset + 26 <= len(raw) and raw[offset:offset + 4] == b'PE\0\0'
            and struct.unpack_from('<H', raw, offset + 4)[0] == 0x8664
            and struct.unpack_from('<H', raw, offset + 24)[0] == 0x20b, 'windows_x64_pe_required')


def verify_working(stage, config, metadata):
    for row in config.get('candidate_input_rows', ()):
        check_row(stage.parent / 'candidate-inputs' / row['path'], row)
    for row in config['source_rows']:
        check_row(stage / 'source' / row['path'], row)
    for row in config['sdk_rows']:
        check_row(stage / 'sdk' / row['path'], row)
    for row in config['notice_rows']:
        check_row(stage / row['path'], row)
    for name, raw in metadata.items():
        require(read(stage / name) == raw, 'producer_template_drift')
    require(read(stage / 'STAGE.json') == json_bytes(config), 'working_receipt_drift')


def verify_freeze(output, name, contents, config):
    folder = output / 'dist' / name
    exe = folder / (name + '.exe')
    pe_x64(read(exe))
    report = decode(read(output / (name + '_ANALYSIS_CLOSURE.json')))
    require(report.get('status') == 'ANALYSIS_MODULE_BOUND_PASSED' and not report.get('missing_local_modules')
            and report.get('source_rows') == len(config['source_rows']), 'frozen_closure_not_proven')
    allowed = {r['path'][:-3].replace('/', '.').removesuffix('.__init__') for r in config['source_rows'] if r['path'].endswith('.py') and not r['path'].startswith('tools/trainer-sources/')}
    allowed |= {'.'.join(n.split('.')[:i]) for n in tuple(allowed) for i in range(1, len(n.split('.')))}
    tops = {n.partition('.')[0] for n in allowed}
    actual = set(report.get('actual_pure_modules', []))
    require(allowed <= actual, 'declared_frozen_module_missing')
    require(all(n in allowed for n in report.get('actual_pure_modules', []) if n.partition('.')[0] in tops),
            'undeclared_frozen_module')
    for row in config['source_rows']:
        check_row(folder / contents / row['path'], row)
    for row in config['sdk_rows']:
        if not row['path'].endswith('.py'):
            check_row(folder / contents / row['path'], row)
    for row in config['notice_rows']:
        check_row(folder / contents / 'licenses' / Path(row['path']).name, row)
    model_catalog_declared = any(row['path'] == 'data/model_workflow_baseline.json' for row in config['source_rows'])
    for path in folder.rglob('*'):
        unredirected(path)
        relative = path.relative_to(folder)
        # check_row above has already verified every declared byte. Admit only
        # that immutable file and its parent, never a general mutable data tree.
        approved_model_catalog = model_catalog_declared and relative.as_posix() in {
            contents + '/data', contents + '/data/model_workflow_baseline.json'}
        require(approved_model_catalog or not any(p.casefold() in {'data', 'models', 'logs', 'backups', 'catalogs'}
                        or p.casefold().startswith('private_') for p in relative.parts), 'mutable_or_private_payload')
    return exe


def tree_inventory(folder):
    """Retain every frozen file and directory, including non-source support files."""
    inventory = {}
    for path in sorted(folder.rglob('*')):
        unredirected(path)
        name = path.relative_to(folder).as_posix()
        if path.is_file():
            raw = read(path)
            inventory[name] = {'bytes': len(raw), 'sha256': digest(raw)}
        else:
            require(path.is_dir(), 'unsupported_frozen_member')
            inventory[name] = {'directory': True}
    return inventory


def write_current_build_metadata(folder, *, software_version, source_plan_sha256, tool_profile):
    """Seal package-local display facts after freeze, before final child inventory.

    The runtime digest covers the completed child tree before this resource is
    added, avoiding a self-referential hash. It is provenance, not admission.
    """
    from suite_core.manager.current_build import parse_metadata
    folder = unredirected(folder)
    destination = folder / '_internal/suite_core/manager/CURRENT_BUILD.json'
    require((folder / 'CollectiGenManager.exe').is_file() and destination.parent.is_dir()
            and not destination.exists(), 'fresh_completed_manager_build_required')
    inventory = tree_inventory(folder)
    value = dict(schema_version=1, component_id='suite-manager', software_version=software_version,
        packaging_version=software_version, distribution_kind='portable-runtime', runtime_bundled=True,
        runtime_versions=dict(python=tool_profile.get('python', {}).get('version'),
            qt=tool_profile.get('distributions', {}).get('PySide6-Essentials')),
        unsigned=True, clean_machine_qualified=False,
        target=dict(os='windows', architecture='x86_64'),
        provenance=dict(source_plan_sha256=source_plan_sha256,
            runtime_inventory_sha256=digest(json_bytes(inventory))))
    require(parse_metadata(value, software_version).kind == 'portable-runtime',
            'invalid_current_build_metadata')
    raw = json_bytes(value)
    write_new(destination, raw)
    require(parse_metadata(decode(read(destination)), software_version).kind == 'portable-runtime',
            'current_build_metadata_readback_failed')
    return dict(resource=destination.relative_to(folder).as_posix(), bytes=len(raw), sha256=digest(raw),
        runtime_inventory_scope='completed child tree before CURRENT_BUILD.json is added',
        runtime_inventory=inventory, metadata=value, apps_launched=False,
        native_qualified=False, clean_machine_qualified=False)


def stage_training_worker(worker_root, expected_inventory_sha256, manager_root):
    """Stage a reviewed worker tree before sealing the Manager payload inventory."""
    worker = unredirected(worker_root)
    manager = unredirected(manager_root)
    destination = manager / "training-worker" / "python"
    require(worker.is_dir() and manager.is_dir() and not destination.parent.exists()
            and not worker.is_relative_to(manager) and not manager.is_relative_to(worker),
            "separate_fresh_training_worker_stage_required")
    inventory = tree_inventory(worker)
    require(digest(json_bytes(inventory)) == expected_inventory_sha256
            and "sha256" in inventory.get("python.exe", {}), "training_worker_input_changed")
    for name in inventory:
        require(not any(part.casefold() in {"data", "models", "logs", "backups", "catalogs"}
                        for part in PurePosixPath(safe_path(name)).parts),
                "mutable_or_private_payload")
    directories = {name for name, row in inventory.items() if row == {"directory": True}}
    file_parents = {parent.as_posix()
                    for name, row in inventory.items() if "sha256" in row
                    for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")}
    require(directories == file_parents, "training_worker_empty_directory_not_signed")
    for name, row in inventory.items():
        if row == {"directory": True}:
            (destination / safe_path(name)).mkdir(parents=True, exist_ok=True)
        else:
            relative = safe_path(name)
            raw = pinned(worker / relative, row["sha256"])
            require(len(raw) == row["bytes"], "training_worker_input_size_changed")
            write_new(destination / relative, raw)
    require(tree_inventory(destination) == inventory and tree_inventory(worker) == inventory,
            "training_worker_stage_readback_changed")


def candidate_render_inputs(admission, *, component, sources, software_version, output,
                            mapping_sha256=None, offers=None, renderer_raw=None, renderer_ast_sha256=None,
                            candidate_update_profile=None, helper_pins=None):
    """Validate explicit pending inputs before creating any producer output."""
    if admission is None:
        require(mapping_sha256 is None and offers is None and candidate_update_profile is None,
                'explicit_candidate_admission_required')
        return {}
    from core.workflow_candidate_admission import CandidateWorkflowAdmission
    require(type(admission) is CandidateWorkflowAdmission, 'typed_pending_candidate_required')
    admission.validate()
    require(not admission.suite_root.exists(), 'fresh_isolated_candidate_suite_required')
    expected = admission.manager_version if component == 'manager' else admission.version_for('collectigen.' + component)
    require(software_version == expected, 'candidate_source_version_changed')
    if component == 'manager':
        require(admission.suite_root == output / 'Suite', 'candidate_producer_suite_root_changed')
    require(type(renderer_raw) is bytes and type(renderer_ast_sha256) is str,
            'pinned_candidate_renderer_required')
    if candidate_update_profile is not None:
        require(type(candidate_update_profile) is dict and type(helper_pins) is dict
                and candidate_update_profile.get('stock_review_sha256') == helper_pins.get('candidate_stock_review_sha256'),
                'pinned_genuine_candidate_stock_review_required')
    render = canonical_candidate_renderer(renderer_raw, renderer_ast_sha256, helper_pins=helper_pins)
    return render(admission, component=component, source_bytes=sources,
                  mapping_sha256=mapping_sha256, offers=offers, candidate_update_profile=candidate_update_profile)


def seal_candidate_inputs(output, stage, config, sources, generated, admission, *, mapping_sha256=None,
                          renderer_raw=None, organizer_qualification=None):
    """Keep original admitted bytes separately; seal generated working identities."""
    if admission is None and organizer_qualification is None:
        require(not generated, 'candidate_generated_sources_without_admission')
        return {}
    require(admission is None or organizer_qualification is None, 'unmixed_qualification_kind_required')
    if organizer_qualification is not None:
        identity = organizer_qualification_identity(organizer_qualification, output)
        require(identity.suite_root == output / 'Suite' and mapping_sha256 == organizer_qualification['mapping_sha256'],
                'organizer_qualification_scope_changed')
        inputs = {'organizer-qualification.json': json_bytes(organizer_qualification)}
    else:
        inputs = {'document.json': admission.document_bytes, 'trust.json': admission.trust_bytes}
    if renderer_raw is not None:
        require(type(renderer_raw) is bytes, 'candidate_renderer_bytes_required')
        inputs['renderer.py'] = renderer_raw
    identities = []
    for name, raw in sorted(generated.items()):
        require(name in sources and type(raw) is bytes, 'candidate_finite_source_overlay_required')
        inputs['original-source/' + name] = sources[name]
        inputs['generated-source/' + name] = raw
        path = stage / 'source' / name
        require(read(path) == sources[name], 'candidate_original_working_source_changed')
        path.write_bytes(raw)
        require(read(path) == raw, 'candidate_generated_readback_changed')
        config['source_rows'] = [r for r in config['source_rows'] if r['path'] != name]
        config['source_rows'].append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
        identities.append({'path': name, 'original_sha256': digest(sources[name]),
                           'generated_sha256': digest(raw), 'bytes': len(raw)})
    config['candidate_input_rows'] = []
    for name, raw in sorted(inputs.items()):
        write_new(output / 'candidate-inputs' / name, raw)
        config['candidate_input_rows'].append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
    return {'document_sha256': (digest(inputs['organizer-qualification.json']) if organizer_qualification is not None
                              else admission.document_sha256),
            'trust_sha256': identity.trust_sha256 if organizer_qualification is not None else admission.trust_sha256,
            'mapping_sha256': mapping_sha256, 'generated_sources': identities,
            'input_rows': config['candidate_input_rows'], 'status': 'pending'}


@dataclass(frozen=True)
class _ManagerBaselineIdentity:
    """Producer verification facts only; not runtime or component-stage authority."""
    suite_root: Path
    manager_version: str
    trust_bytes: bytes
    trust_sha256: str


def organizer_qualification_identity(document, output):
    require(type(document) is dict and set(document) == {'schema','kind','suite_root','manager_version',
        'trust_hex','trust_sha256','mapping_sha256'} and type(document['schema']) is int and document['schema'] == 1
        and document['kind'] == 'organizer-only', 'exact_organizer_qualification_document_required')
    require(type(document['suite_root']) is str and Path(document['suite_root']) == output / 'Suite'
        and type(document['manager_version']) is str and document['manager_version']
        and all(type(document[key]) is str and re.fullmatch('[0-9a-f]{64}', document[key])
                for key in ('trust_sha256','mapping_sha256')),
        'exact_organizer_qualification_identity_required')
    require(type(document['trust_hex']) is str and len(document['trust_hex']) <= 2*1024*1024,
            'bounded_organizer_qualification_trust_required')
    trust = bytes.fromhex(document['trust_hex'])
    require(digest(trust) == document['trust_sha256'], 'organizer_qualification_trust_changed')
    from suite_core.discovery.component_release_signature import _trust
    _trust(trust)
    return _ManagerBaselineIdentity(output / 'Suite', document['manager_version'], trust, document['trust_sha256'])


def build_distribution(source_stage, output, *, stage_sha256, public_inventory, interpreter, sdk_root,
                       notice_root, root_renderer, source_manifest=HERE / 'source-manifest.json',
                       tool_inputs=HERE / 'tool-inputs.json', training_worker_root=None,
                       training_worker_inventory_sha256=None, candidate_admission=None,
                       candidate_mapping_sha256=None, candidate_offers=None,
                       candidate_child_only=False, candidate_update_profile=None,
                       organizer_qualification_mapping_sha256=None, organizer_qualification_update_profile=None):
    organizer_only = (organizer_qualification_mapping_sha256 is not None
                      or organizer_qualification_update_profile is not None)
    if organizer_only:
        require(candidate_admission is None and candidate_mapping_sha256 is None and candidate_offers is None
                and candidate_child_only is False and candidate_update_profile is None,
                'unmixed_organizer_qualification_build_required')
        require(type(organizer_qualification_mapping_sha256) is str
                and re.fullmatch('[0-9a-f]{64}', organizer_qualification_mapping_sha256)
                and type(organizer_qualification_update_profile) is dict
                and training_worker_root is not None and training_worker_inventory_sha256 is not None,
                'complete_organizer_qualification_build_required')
    deferred_child = candidate_child_only or organizer_only
    child_update_profile = organizer_qualification_update_profile if organizer_only else candidate_update_profile
    mapping_pin = organizer_qualification_mapping_sha256 if organizer_only else candidate_mapping_sha256
    require(type(candidate_child_only) is bool, 'explicit_candidate_child_only_flag_required')
    require(candidate_admission is None or candidate_child_only,
            'workflow_candidate_requires_deferred_signed_baseline')
    if candidate_child_only:
        require(candidate_admission is not None and type(candidate_update_profile) is dict
                and training_worker_root is not None and training_worker_inventory_sha256 is not None,
                'candidate_stock_profile_and_worker_required')
    else:
        require(candidate_update_profile is None, 'candidate_worker_profile_requires_deferred_root')
    require((training_worker_root is None) == (training_worker_inventory_sha256 is None),
            'complete_training_worker_pins_required')
    if training_worker_root is not None:
        training_worker_root = unredirected(training_worker_root)
        require(training_worker_root.is_dir()
                and digest(json_bytes(tree_inventory(training_worker_root))) == training_worker_inventory_sha256,
                'training_worker_input_changed')
    source_stage, output, interpreter = map(unredirected, (source_stage, output, interpreter))
    require(not output.exists() and not output.is_relative_to(source_stage)
            and not source_stage.is_relative_to(output), 'fresh_separate_output_required')
    manifest_raw, profile_raw = read(source_manifest), read(tool_inputs)
    inventory_raw = read(public_inventory)
    receipt, manifest, sources = validate_sources(source_stage, stage_sha256, manifest_raw, inventory_raw)
    profile = decode(profile_raw)
    require(profile.get('schema') == 1 and set(profile.get('templates', {})) == TEMPLATES,
            'public_tool_profile_required')
    require(profile.get('msvc') == {'architecture': 'x64', 'minimum_version': '14.44.35211.0',
                                  'policy': 'existing-system-prerequisite'}, 'stock_msvc_policy_required')
    renderer_raw = read(root_renderer)
    candidate_sources = candidate_render_inputs(candidate_admission, component='manager', sources=sources,
        software_version=receipt['software_version'], output=output,
        mapping_sha256=candidate_mapping_sha256, offers=candidate_offers,
        renderer_raw=renderer_raw, renderer_ast_sha256=profile.get('workflow_candidate_renderer_ast_sha256'),
        candidate_update_profile=candidate_update_profile, helper_pins=profile if candidate_update_profile is not None else None)
    organizer_document = None
    if organizer_only:
        require(child_update_profile.get('stock_review_sha256') == profile.get('candidate_stock_review_sha256'),
                'pinned_genuine_organizer_stock_review_required')
        generate = canonical_organizer_qualification_renderer(renderer_raw, profile)
        candidate_sources = generate(source_bytes=sources, mapping_sha256=mapping_pin,
                                     update_profile=child_update_profile)
        organizer_document = dict(schema=1,kind='organizer-only',suite_root=str(output / 'Suite'),
            manager_version=receipt['software_version'],trust_hex=child_update_profile['trust_hex'],
            trust_sha256=child_update_profile['trust_sha256'],mapping_sha256=mapping_pin)
        organizer_qualification_identity(organizer_document, output)
    render = canonical_renderer(renderer_raw, profile['root_entry_ast_sha256'],
                                helper_pins=profile if child_update_profile is not None else None)
    unbound = render()
    require('manager_package_pin_not_finalized' in unbound and 'EXPECTED_MANAGER_PACKAGE_SHA256 = None' in unbound,
            'refusing_unbound_root_required')
    require(read(source_stage / 'source/public_root_entry.py') == unbound.encode(), 'unbound_root_changed')
    sdk = {safe_path(name): pinned(Path(sdk_root) / name, pin) for name, pin in profile['sdk']['files'].items()}
    require(all(safe_path(spec['source']).startswith('licenses/') for spec in profile['notices'].values()),
            'public_notice_source_required')
    notices = {safe_path(name): pinned(Path(notice_root) / spec['source'], spec['sha256'])
               for name, spec in profile['notices'].items()}
    metadata = {name: pinned(HERE / name, pin) for name, pin in profile['templates'].items()}
    environment = isolated_environment(output, interpreter)
    inspect_tools(interpreter, profile, environment)
    # Seal all source, tool, SDK and renderer bytes before the first freeze.
    output.mkdir(parents=True)
    stage = output / 'stage'
    config = {'schema': 1, 'software_version': receipt['software_version'], 'source_rows': [],
              'sdk_rows': [], 'notice_rows': [], 'sdk_extension_sha256': profile['sdk']['files']['velopack/velopack.pyd']}
    for name, raw in sources.items():
        write_new(stage / 'source' / name, raw)
        config['source_rows'].append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
    for name, raw in sdk.items():
        write_new(stage / 'sdk' / name, raw)
        config['sdk_rows'].append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
    for name, raw in notices.items():
        write_new(stage / 'notices' / name, raw)
        config['notice_rows'].append({'path': 'notices/' + name, 'bytes': len(raw), 'sha256': digest(raw)})
    for name, raw in metadata.items():
        write_new(stage / name, raw)
    candidate_seal = seal_candidate_inputs(output, stage, config, sources, candidate_sources,
        candidate_admission, mapping_sha256=mapping_pin, renderer_raw=renderer_raw,
        organizer_qualification=organizer_document)
    if child_update_profile is not None:
        raw = json_bytes(child_update_profile)
        write_new(output / 'candidate-inputs/child-update-profile.json', raw)
        config['candidate_input_rows'].append({'path': 'child-update-profile.json', 'bytes': len(raw), 'sha256': digest(raw)})
    write_new(output / 'SEALED_INPUTS.json', json_bytes({'stage_sha256': stage_sha256,
        'source_manifest_sha256': digest(manifest_raw), 'tool_inputs_sha256': digest(profile_raw),
        'root_renderer_sha256': digest(renderer_raw), 'public_inventory_sha256': receipt['public_inventory_sha256'],
        'training_worker_inventory_sha256': training_worker_inventory_sha256,
        **({'workflow_candidate': candidate_seal} if candidate_admission is not None else {}),
        **({'organizer_qualification': candidate_seal} if organizer_only else {})}))
    set_root_entry(stage, config, unbound)
    try:
        verify_working(stage, config, metadata)
        require(len(pinned(interpreter, profile['python']['sha256'])) == profile['python']['bytes'],
                'interpreter_size_changed')
        freeze(output, interpreter, environment, 'CollectiGenManager')
        verify_working(stage, config, metadata)
        child = verify_freeze(output, 'CollectiGenManager', '_internal', config)
        child_config = decode(json_bytes(config))  # Retain its unbound wrapper expectation.
        if training_worker_root is not None:
            stage_training_worker(training_worker_root, training_worker_inventory_sha256, child.parent)
        build_metadata = write_current_build_metadata(child.parent,
            software_version=receipt['software_version'], source_plan_sha256=stage_sha256,
            tool_profile=profile)
        write_new(output / 'CURRENT_BUILD_METADATA_RECEIPT.json', json_bytes(build_metadata))
        child_inventory = tree_inventory(child.parent)
        validate_sources(source_stage, stage_sha256, manifest_raw, inventory_raw)
        package = {'schema': 1, 'manager_version': receipt['software_version'], 'manager_sha256': digest(read(child)),
                   'protocol': 1, 'minimum_bootstrap': '0.1.1',
                   'maximum_bootstrap_exclusive': '0.2.0',
                   'manager_relative': 'Apps/Manager/current/CollectiGenManager.exe'}
        package_raw = json_bytes(package)
        write_new(output / 'MANAGER_PACKAGE.json', package_raw)
        package_pin = digest(package_raw)
        if deferred_child:
            return write_candidate_child_checkpoint(output, stage=stage, config=config,
                child=child, child_inventory=child_inventory, package_raw=package_raw,
                source_stage=source_stage, stage_sha256=stage_sha256, manifest_raw=manifest_raw,
                inventory_raw=inventory_raw, profile_raw=profile_raw, renderer_raw=renderer_raw,
                interpreter=interpreter, receipt=receipt,
                training_worker_inventory_sha256=training_worker_inventory_sha256,
                mapping_sha256=mapping_pin)
        return _finish_manager_distribution(
            output=output,
            stage=stage,
            config=config,
            metadata=metadata,
            interpreter=interpreter,
            environment=environment,
            child=child,
            child_inventory=child_inventory,
            child_config=child_config,
            package=package,
            package_raw=package_raw,
            package_pin=package_pin,
            source_stage=source_stage,
            stage_sha256=stage_sha256,
            manifest_raw=manifest_raw,
            inventory_raw=inventory_raw,
            receipt=receipt,
            profile=profile,
            renderer_raw=renderer_raw,
            render=render,
            unbound=unbound,
            candidate_admission=candidate_admission,
            training_worker_inventory_sha256=training_worker_inventory_sha256)
    except Exception as error:
        write_new(output / 'BUILD_FAILURE.json', json_bytes({'status': 'BUILD_HELD', 'reason': str(error),
                                                          'error_type': type(error).__name__,
                                                          'apps_launched': False, 'signed': False}))
        raise


def write_candidate_child_checkpoint(output, *, stage, config, child, child_inventory, package_raw,
        source_stage, stage_sha256, manifest_raw, inventory_raw, profile_raw, renderer_raw,
        interpreter, receipt, training_worker_inventory_sha256, mapping_sha256):
    """Retain the exact child before its separately signed baseline can exist."""
    inputs = {'source-manifest.json': manifest_raw, 'public-inventory.json': inventory_raw,
              'tool-inputs.json': profile_raw, 'renderer.py': renderer_raw,
              'unbound-root-entry.py': read(stage / 'source/public_root_entry.py')}
    rows = []
    for name, raw in inputs.items():
        write_new(output / 'child-checkpoint-inputs' / name, raw)
        rows.append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
    checkpoint = {'schema': 1, 'status': 'CHILD_ONLY_AWAITING_SIGNED_BASELINE',
        'source_stage': str(source_stage), 'stage_sha256': stage_sha256,
        'interpreter': str(interpreter), 'receipt': receipt, 'config': config,
        'child_relative': child.relative_to(output).as_posix(), 'child_inventory': child_inventory,
        'package_sha256': digest(package_raw), 'sealed_inputs_sha256': digest(read(output / 'SEALED_INPUTS.json')),
        'training_worker_inventory_sha256': training_worker_inventory_sha256,
        'mapping_sha256': mapping_sha256, 'input_rows': rows}
    raw = json_bytes(checkpoint)
    write_new(output / 'CHILD_CHECKPOINT.json', raw)
    return {'status': checkpoint['status'], 'checkpoint': str(output / 'CHILD_CHECKPOINT.json'),
            'checkpoint_sha256': digest(raw), 'manager_package_sha256': digest(package_raw),
            'apps_launched': False, 'signed': False, 'native_qualified': False}


def load_candidate_child_checkpoint(output, checkpoint_sha256):
    """Read back pinned source/tool/profile/child bytes before Root continuation."""
    output = unredirected(output)
    value = decode(pinned(output / 'CHILD_CHECKPOINT.json', checkpoint_sha256))
    require(type(value) is dict and set(value) == {'schema','status','source_stage','stage_sha256',
        'interpreter','receipt','config','child_relative','child_inventory','package_sha256',
        'sealed_inputs_sha256','training_worker_inventory_sha256','mapping_sha256','input_rows'}
        and type(value['schema']) is int and value['schema'] == 1
        and value['status'] == 'CHILD_ONLY_AWAITING_SIGNED_BASELINE', 'exact_child_checkpoint_required')
    pinned(output / 'SEALED_INPUTS.json', value['sealed_inputs_sha256'])
    inputs = {}
    expected = {'source-manifest.json','public-inventory.json','tool-inputs.json','renderer.py','unbound-root-entry.py'}
    checked_rows = rows(value['input_rows'])
    require(set(checked_rows) == expected, 'exact_child_checkpoint_inputs_required')
    for name, row in checked_rows.items():
        inputs[name] = check_row(output / 'child-checkpoint-inputs' / name, row)
    stage = output / 'stage'; config = value['config']; profile = decode(inputs['tool-inputs.json'])
    metadata = {name: pinned(stage / name, pin) for name, pin in profile['templates'].items()}
    verify_working(stage, config, metadata)
    child = output / safe_path(value['child_relative'])
    require(child == output / 'dist/CollectiGenManager/CollectiGenManager.exe', 'exact_checkpoint_child_location_required')
    require(tree_inventory(child.parent) == value['child_inventory'], 'checkpoint_child_tree_changed')
    verify_freeze(output, 'CollectiGenManager', '_internal', config)
    package_raw = pinned(output / 'MANAGER_PACKAGE.json', value['package_sha256'])
    package = decode(package_raw)
    require(package['manager_sha256'] == digest(read(child))
            and package['manager_version'] == value['receipt']['software_version'], 'checkpoint_child_package_changed')
    source_stage = unredirected(value['source_stage'])
    validate_sources(source_stage, value['stage_sha256'], inputs['source-manifest.json'], inputs['public-inventory.json'])
    require(value['training_worker_inventory_sha256'] is not None, 'checkpoint_training_worker_required')
    return value, inputs, profile, metadata, child, package_raw


def verify_candidate_signed_baseline(admission, update_profile, recovery_root, child, child_inventory):
    """Use the stock signed detached verifier on the actual current child."""
    from core.workflow_candidate_admission import CandidateWorkflowAdmission
    from suite_core.discovery.manager_runtime_detached_admission import admit_checkpointed_baseline, manager_inventory_json, PACKAGE_STUB
    from core.manager_successor_admission import installed_manager_payload_path
    import zipfile
    require(type(admission) in (CandidateWorkflowAdmission, _ManagerBaselineIdentity),
            'typed_manager_baseline_identity_required')
    require(type(update_profile) is dict and update_profile.get('trust_hex') == admission.trust_bytes.hex()
        and update_profile.get('trust_sha256') == admission.trust_sha256
        and update_profile.get('channel') == 'beta' and update_profile.get('transport_id') == 'sealed-local-qualification'
        and update_profile.get('enable_training_baseline') is True, 'candidate_baseline_profile_changed')
    prior = update_profile.get('initial_prior')
    require(type(prior) is dict and prior.get('version') == admission.manager_version
            and prior.get('channel') == 'beta' and prior.get('platform') == {'os':'windows','architecture':'x86_64'}
            and type(prior.get('files')) is dict
            and set(prior['files']) == {'archive','manifest','signature','trust','proof','inventory','executable'},
            'current_signed_candidate_baseline_required')
    root = unredirected(recovery_root)
    retained = {}; identities = set()
    for name, spec in prior['files'].items():
        require(type(spec) is dict and set(spec) == {'path','size','sha256'}, 'exact_baseline_file_pin_required')
        relative = safe_path(spec['path'])
        require(relative.startswith('Recovery/') and relative.casefold() not in identities,
                'distinct_candidate_recovery_files_required')
        identities.add(relative.casefold())
        raw = pinned(root / relative, spec['sha256'])
        require(type(spec['size']) is int and len(raw) == spec['size'], 'candidate_baseline_size_changed')
        retained[relative] = raw
    def raw(name): return retained[prior['files'][name]['path']]
    require(raw('trust') == admission.trust_bytes and raw('proof') == raw('inventory')
            and raw('executable') == read(child), 'candidate_baseline_current_bytes_changed')
    archive = root / prior['files']['archive']['path']
    admit_checkpointed_baseline(archive, checkpoint={'child_tree': child_inventory,
        'child_executable_bytes': len(read(child)), 'child_executable_sha256': digest(read(child)),
        'software_version': admission.manager_version}, detached_inventory_bytes=raw('inventory'),
        expected_channel='beta', manifest_bytes=raw('manifest'), signature_bytes=raw('signature'),
        trust_bytes=raw('trust'), expected_trust_sha256=admission.trust_sha256,
        expected_inventory_sha256=digest(raw('inventory')))
    generated = {}; inventory = {row['path']: row for row in manager_inventory_json(raw('inventory'))['files']}
    with zipfile.ZipFile(archive) as package:
        for name in (PACKAGE_STUB, 'Squirrel.exe', 'sq.version'):
            data = package.read('lib/app/' + name)
            row = inventory[name]
            require(len(data) == row['size_bytes'] and digest(data) == row['sha256'], 'candidate_generated_stock_member_changed')
            destination = installed_manager_payload_path(admission.suite_root, name, stock_applied=False)
            generated[destination.relative_to(admission.suite_root).as_posix()] = data
    require(digest(generated['Apps/Manager/Update.exe']) == update_profile['updater_sha256'],
            'candidate_stock_updater_changed')
    evidence = {'archive_relative': prior['files']['archive']['path'], 'manifest_bytes': raw('manifest'),
                'signature_bytes': raw('signature'), 'detached_inventory_bytes': raw('inventory')}
    return retained, generated, evidence


def finalize_candidate_distribution(output, *, child_checkpoint_sha256, candidate_update_profile, recovery_root):
    """Continue the original Root/assembly tail only after actual child signing."""
    from core.workflow_candidate_admission import CandidateWorkflowAdmission
    output = unredirected(output)
    checkpoint, inputs, profile, metadata, child, package_raw = load_candidate_child_checkpoint(output, child_checkpoint_sha256)
    sealed = decode(read(output / 'SEALED_INPUTS.json'))
    require(('workflow_candidate' in sealed) != ('organizer_qualification' in sealed),
            'unmixed_checkpoint_qualification_required')
    admission = None
    baseline_identity = None
    if 'workflow_candidate' in sealed:
        seal = sealed['workflow_candidate']
        admission = CandidateWorkflowAdmission(read(output / 'candidate-inputs/document.json'), seal['document_sha256'],
            read(output / 'candidate-inputs/trust.json'), seal['trust_sha256']).validate()
        identity = admission
    else:
        seal = sealed['organizer_qualification']
        document = decode(pinned(output / 'candidate-inputs/organizer-qualification.json', seal['document_sha256']))
        baseline_identity = organizer_qualification_identity(document, output)
        require(document['mapping_sha256'] == checkpoint['mapping_sha256'] == seal['mapping_sha256']
                and baseline_identity.manager_version == checkpoint['receipt']['software_version']
                and baseline_identity.trust_sha256 == seal['trust_sha256'],
                'organizer_checkpoint_identity_changed')
        identity = baseline_identity
    require(not identity.suite_root.exists() and identity.suite_root == output / 'Suite',
            'fresh_isolated_candidate_suite_required')
    child_profile = decode(read(output / 'candidate-inputs/child-update-profile.json'))
    require(type(candidate_update_profile) is dict
            and {k:v for k,v in candidate_update_profile.items() if k != 'initial_prior'}
                == {k:v for k,v in child_profile.items() if k != 'initial_prior'},
            'candidate_root_child_profile_changed')
    retained, generated, baseline_evidence = verify_candidate_signed_baseline(identity, candidate_update_profile, recovery_root,
                                                                            child, checkpoint['child_inventory'])
    renderer_raw = inputs['renderer.py']
    render = canonical_renderer(renderer_raw, profile['root_entry_ast_sha256'], helper_pins=profile)
    # Validate the complete Root rendering before writing continuation inputs.
    render(digest(package_raw), candidate_admission=admission, compiled_update_profile=True,
        compiled_profile_input=candidate_update_profile, qualification_test_build=True,
        qualification_mapping_sha256=checkpoint['mapping_sha256'])
    child_config = decode(json_bytes(checkpoint['config']))
    config = checkpoint['config']; stage = output / 'stage'
    continuation = {'root-update-profile.json': json_bytes(candidate_update_profile)}
    continuation.update({'retained-baseline/' + name: raw for name,raw in retained.items()})
    continuation.update({'generated-stock/' + name: raw for name,raw in generated.items()})
    for name, raw in sorted(continuation.items()):
        write_new(output / 'candidate-inputs' / name, raw)
        config['candidate_input_rows'].append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
    write_new(output / 'CANDIDATE_CONTINUATION_INPUTS.json', json_bytes({'checkpoint_sha256': child_checkpoint_sha256,
        'mapping_sha256': checkpoint['mapping_sha256'], 'input_rows': config['candidate_input_rows'], 'status':'pending'}))
    (stage / 'STAGE.json').write_bytes(json_bytes(config))
    interpreter = unredirected(checkpoint['interpreter']); environment = isolated_environment(output, interpreter)
    inspect_tools(interpreter, profile, environment)
    return _finish_manager_distribution(output=output, stage=stage, config=config, metadata=metadata,
        interpreter=interpreter, environment=environment, child=child, child_inventory=checkpoint['child_inventory'],
        child_config=child_config, package=decode(package_raw), package_raw=package_raw, package_pin=digest(package_raw),
        source_stage=unredirected(checkpoint['source_stage']), stage_sha256=checkpoint['stage_sha256'],
        manifest_raw=inputs['source-manifest.json'], inventory_raw=inputs['public-inventory.json'], receipt=checkpoint['receipt'],
        profile=profile, renderer_raw=renderer_raw, render=render, unbound=inputs['unbound-root-entry.py'].decode(),
        candidate_admission=admission, training_worker_inventory_sha256=checkpoint['training_worker_inventory_sha256'],
        candidate_update_profile=candidate_update_profile, qualification_mapping_sha256=checkpoint['mapping_sha256'],
        retained_files={**retained, **generated}, baseline_evidence=baseline_evidence,
        baseline_identity=baseline_identity)


def set_root_entry(stage, config, text):
    raw = text.encode()
    path = stage / 'source/public_root_entry.py'
    path.write_bytes(raw)
    config['source_rows'] = [r for r in config['source_rows'] if r['path'] != 'public_root_entry.py']
    config['source_rows'].append({'path': 'public_root_entry.py', 'bytes': len(raw), 'sha256': digest(raw)})
    (stage / 'STAGE.json').write_bytes(json_bytes(config))
    require(read(path) == raw, 'root_entry_readback_failed')


def _finish_manager_distribution(*, output, stage, config, metadata, interpreter, environment, child, child_inventory, child_config, package, package_raw, package_pin, source_stage, stage_sha256, manifest_raw, inventory_raw, receipt, profile, renderer_raw, render, unbound, candidate_admission, training_worker_inventory_sha256,
        candidate_update_profile=None, qualification_mapping_sha256=None, retained_files=None, baseline_evidence=None,
        baseline_identity=None):
    bound_root = (render(package_pin, candidate_admission=candidate_admission,
        compiled_update_profile=True, compiled_profile_input=candidate_update_profile,
        qualification_test_build=True, qualification_mapping_sha256=qualification_mapping_sha256)
        if candidate_update_profile is not None else
        render(package_pin, candidate_admission=candidate_admission)
                  if candidate_admission is not None else render(package_pin))
    if candidate_admission is not None or baseline_identity is not None:
        # Root's exact package pin exists only after the child freeze. Seal
        # this second generated identity before the original Root freeze.
        root_inputs = {'original-unbound-root-entry.py': unbound.encode(),
                       'generated-root-entry.py': bound_root.encode()}
        for name, raw in root_inputs.items():
            write_new(output / 'candidate-inputs' / name, raw)
            config['candidate_input_rows'].append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
        write_new(output / 'CANDIDATE_ROOT_INPUTS.json', json_bytes({
            'document_sha256': (candidate_admission.document_sha256 if candidate_admission is not None else
                digest(read(output / 'candidate-inputs/organizer-qualification.json'))),
            'manager_package_sha256': package_pin, 'root_renderer_sha256': digest(renderer_raw),
            'original_root_sha256': digest(unbound.encode()),
            'generated_root_sha256': digest(bound_root.encode()), 'status': 'pending'}))
    set_root_entry(stage, config, bound_root)
    verify_working(stage, config, metadata)
    require(len(pinned(interpreter, profile['python']['sha256'])) == profile['python']['bytes'],
            'interpreter_size_changed')
    freeze(output, interpreter, environment, 'CollectiGen')
    verify_working(stage, config, metadata)
    root_exe = verify_freeze(output, 'CollectiGen', '_bootstrap', config)
    require(tree_inventory(child.parent) == child_inventory, 'child_tree_changed_after_root_freeze')
    verify_freeze(output, 'CollectiGenManager', '_internal', child_config)
    require(digest(read(child)) == package['manager_sha256'], 'child_changed_after_package_binding')
    validate_sources(source_stage, stage_sha256, manifest_raw, inventory_raw)
    suite = output / 'Suite'
    if candidate_admission is not None:
        require(not suite.exists(), 'fresh_isolated_candidate_suite_required')
    shutil.copytree(root_exe.parent, suite)
    shutil.copytree(child.parent, suite / 'Apps/Manager/current')
    require(tree_inventory(suite / 'Apps/Manager/current') == child_inventory,
            'child_distribution_copy_changed')
    write_new(suite / 'MANAGER_PACKAGE.json', package_raw)
    for relative, raw in sorted((retained_files or {}).items()):
        write_new(suite / safe_path(relative), raw)
    if baseline_evidence is not None:
        identity = candidate_admission if candidate_admission is not None else baseline_identity
        require(identity is not None, 'signed_baseline_identity_required')
        retain_workflow_manager_admission(suite, archive=suite / baseline_evidence['archive_relative'],
            manifest_bytes=baseline_evidence['manifest_bytes'], signature_bytes=baseline_evidence['signature_bytes'],
            trust_bytes=identity.trust_bytes, expected_trust_sha256=identity.trust_sha256,
            detached_inventory_bytes=baseline_evidence['detached_inventory_bytes'], software_version=receipt['software_version'])
        retained_files = dict(retained_files)
        for name, key in (('release.json','manifest_bytes'), ('release.sig.json','signature_bytes'), ('inventory.json','detached_inventory_bytes')):
            retained_files['_bootstrap/workflow-manager-admission/initial/' + name] = baseline_evidence[key]
    if candidate_admission is not None:
        write_new(suite / '_bootstrap/workflow-candidate.json', candidate_admission.document_bytes)
        candidate_admission.check_marker(suite, suite / 'Data', manager_version=receipt['software_version'])
    require(not (suite / 'Data').exists(), 'mutable_data_in_distribution')
    files = []
    for path in sorted(suite.rglob('*')):
        unredirected(path)
        if path.is_file():
            raw = read(path)
            source = root_exe.parent / path.relative_to(suite)
            if path.is_relative_to(suite / 'Apps/Manager/current'):
                source = child.parent / path.relative_to(suite / 'Apps/Manager/current')
            if (path.name != 'MANAGER_PACKAGE.json' and path != suite / '_bootstrap/workflow-candidate.json'
                    and path.relative_to(suite).as_posix() not in (retained_files or {})):
                require(raw == read(source), 'distribution_copy_changed')
            if path.relative_to(suite).as_posix() in (retained_files or {}):
                require(raw == retained_files[path.relative_to(suite).as_posix()], 'retained_baseline_copy_changed')
            files.append({'path': path.relative_to(suite).as_posix(), 'bytes': len(raw), 'sha256': digest(raw)})
    result = {'status': 'BUILD_ONLY_NOT_EXECUTED', 'software_version': receipt['software_version'],
              'manager_package_sha256': package_pin, 'files': files, 'apps_launched': False,
              'signed': False, 'native_qualified': False,
              'training_worker_inventory_sha256': training_worker_inventory_sha256}
    write_new(output / 'BUILD_RESULT.json', json_bytes(result))
    return suite


def retain_workflow_manager_admission(suite_root, *, archive, manifest_bytes, signature_bytes,
        trust_bytes, expected_trust_sha256, detached_inventory_bytes, software_version, channel='beta'):
    """Retain already signed stock Manager evidence for independently built modules.

    Called by the existing release assembly after its signed nupkg exists. No
    key, signing, new trust, updater, qualification or mutable-data access occurs.
    """
    from suite_core.discovery.manager_runtime_detached_admission import admit_installed_detached, PROFILE
    root = unredirected(suite_root)
    require(root.is_dir(), 'completed_suite_distribution_required')
    executable = root / 'Apps/Manager/current/CollectiGenManager.exe'
    program_pin = digest(read(executable))
    plan = admit_installed_detached(archive, installed_executable_sha256=program_pin,
        inventory_profile=PROFILE, detached_inventory_bytes=detached_inventory_bytes,
        manifest_bytes=manifest_bytes, signature_bytes=signature_bytes, trust_bytes=trust_bytes,
        expected_trust_sha256=expected_trust_sha256,
        expected_inventory_sha256=digest(detached_inventory_bytes),
        expected_version=software_version, expected_channel=channel)
    destination = root / '_bootstrap/workflow-manager-admission/initial'
    unredirected(destination)
    require(not destination.exists(), 'fresh_initial_workflow_manager_evidence_required')
    # Reuse the child's signed installed-row verifier before writing anything.
    # Original release assembly still seals the resulting complete ZIP and
    # retains the Recovery baseline independently for worker provisioning.
    from core.manager_successor_admission import installed_manager_payload_path
    from suite_core.discovery.manager_runtime_detached_admission import manager_inventory_json
    for row in manager_inventory_json(detached_inventory_bytes)['files']:
        path = installed_manager_payload_path(root, row['path'], stock_applied=False)
        raw = read(path)
        require(len(raw) == row['size_bytes'] and digest(raw) == row['sha256'],
                'initial_workflow_manager_payload_changed')
    files = {'release.json': manifest_bytes, 'release.sig.json': signature_bytes,
             'inventory.json': detached_inventory_bytes}
    for name, raw in files.items():
        write_new(destination / name, raw)
    require(digest(read(executable)) == program_pin, 'initial_workflow_manager_changed_during_retention')
    return dict(status='SIGNED_SOFTWARE_EVIDENCE_RETAINED_NOT_NATIVE_QUALIFICATION',
        manager_version=software_version, manager_sha256=program_pin,
        signed_archive_sha256=plan.artifact_sha256,
        files={name: dict(bytes=len(raw), sha256=digest(raw)) for name, raw in files.items()},
        apps_launched=False, native_qualified=False, signing_performed=False)


def build_workflow_distribution(source_stage, output, *, component, stage_sha256,
        public_inventory, interpreter, sdk_root, notice_root, source_manifest,
    tool_inputs=HERE / 'tool-inputs.json', candidate_admission=None):
    """Independent frozen module using the existing sealed producer and recipe.

    No Manager binary is copied/relabelled, no Root is built, no program is run,
    and no signing, release or native-health assertion is made.
    """
    require(component in {'generate', 'train'}, 'finite_workflow_component_required')
    name = component.title()
    source_stage, output, interpreter = map(unredirected, (source_stage, output, interpreter))
    require(not output.exists() and not output.is_relative_to(source_stage)
            and not source_stage.is_relative_to(output), 'fresh_separate_output_required')
    manifest_raw, inventory_raw, profile_raw = read(source_manifest), read(public_inventory), read(tool_inputs)
    receipt, _manifest, sources = validate_sources(source_stage, stage_sha256, manifest_raw, inventory_raw,
                                                  component=component)
    profile = decode(profile_raw)
    candidate_renderer_raw = read(Path(__file__).with_name('prepare_manager_production_stage.py')) if candidate_admission is not None else None
    candidate_sources = candidate_render_inputs(candidate_admission, component=component, sources=sources,
        software_version=receipt['software_version'], output=output, renderer_raw=candidate_renderer_raw,
        renderer_ast_sha256=profile.get('workflow_candidate_renderer_ast_sha256'))
    require(profile.get('schema') == 1 and set(profile.get('templates', {})) == TEMPLATES,
            'public_tool_profile_required')
    require(profile.get('msvc') == {'architecture': 'x64', 'minimum_version': '14.44.35211.0',
                                  'policy': 'existing-system-prerequisite'}, 'stock_msvc_policy_required')
    sdk = {safe_path(path): pinned(Path(sdk_root) / path, pin) for path, pin in profile['sdk']['files'].items()}
    require(all(safe_path(row['source']).startswith('licenses/') for row in profile['notices'].values()),
            'public_notice_source_required')
    notices = {safe_path(path): pinned(Path(notice_root) / row['source'], row['sha256'])
               for path, row in profile['notices'].items()}
    metadata = {path: pinned(HERE / path, pin) for path, pin in profile['templates'].items()}
    environment = isolated_environment(output, interpreter)
    inspect_tools(interpreter, profile, environment)
    output.mkdir(parents=True)
    stage = output / 'stage'
    config = dict(schema=1, component=component, software_version=receipt['software_version'],
                  source_rows=[], sdk_rows=[], notice_rows=[],
                  sdk_extension_sha256=profile['sdk']['files']['velopack/velopack.pyd'])
    for path, raw in sources.items():
        write_new(stage / 'source' / path, raw)
        config['source_rows'].append(dict(path=path, bytes=len(raw), sha256=digest(raw)))
    for path, raw in sdk.items():
        write_new(stage / 'sdk' / path, raw)
        config['sdk_rows'].append(dict(path=path, bytes=len(raw), sha256=digest(raw)))
    for path, raw in notices.items():
        write_new(stage / 'notices' / path, raw)
        config['notice_rows'].append(dict(path='notices/' + path, bytes=len(raw), sha256=digest(raw)))
    for path, raw in metadata.items():
        write_new(stage / path, raw)
    candidate_seal = seal_candidate_inputs(output, stage, config, sources, candidate_sources, candidate_admission,
        renderer_raw=candidate_renderer_raw)
    write_new(stage / 'STAGE.json', json_bytes(config))
    write_new(output / 'SEALED_INPUTS.json', json_bytes(dict(component=component,
        stage_sha256=stage_sha256, source_manifest_sha256=digest(manifest_raw),
        tool_inputs_sha256=digest(profile_raw), public_inventory_sha256=digest(inventory_raw),
        **({'workflow_candidate': candidate_seal} if candidate_admission is not None else {}))))
    try:
        verify_working(stage, config, metadata)
        require(len(pinned(interpreter, profile['python']['sha256'])) == profile['python']['bytes'],
                'interpreter_size_changed')
        freeze(output, interpreter, environment, name, recipe='CollectiGenManager')
        verify_working(stage, config, metadata)
        exe = verify_freeze(output, name, '_internal', config)
        validate_sources(source_stage, stage_sha256, manifest_raw, inventory_raw, component=component)
        payload = output / 'Payload'
        shutil.copytree(exe.parent, payload)
        require(tree_inventory(payload) == tree_inventory(exe.parent), 'workflow_distribution_copy_changed')
        module = sources['suite_core/' + component + '/MODULE_MANIFEST.json']
        write_new(payload / 'MODULE_MANIFEST.json', module)
        runtime = dict(schema_version=1, component=component, feature_version=receipt['software_version'],
            os='windows', architecture='x86_64', inventory_excludes_itself='RUNTIME_MANIFEST.json',
            GUI_qualified=False, native_qualified=False, source_stage_sha256=stage_sha256, files=[])
        for path in sorted(payload.rglob('*')):
            unredirected(path)
            if path.is_file():
                raw = read(path)
                runtime['files'].append(dict(path=path.relative_to(payload).as_posix(),
                                             size_bytes=len(raw), sha256=digest(raw)))
        write_new(payload / 'RUNTIME_MANIFEST.json', json_bytes(runtime))
        result = dict(status='BUILD_ONLY_NOT_EXECUTED', component_id='collectigen.' + component,
            software_version=receipt['software_version'], executable=name + '.exe', payload=str(payload),
            source_stage_sha256=stage_sha256, files=tree_inventory(payload), apps_launched=False,
            signed=False, native_qualified=False, clean_machine_qualified=False)
        write_new(output / 'BUILD_RESULT.json', json_bytes(result))
        return result
    except BaseException as error:
        write_new(output / 'BUILD_FAILED.json', json_bytes(dict(component=component,
            error=type(error).__name__ + ': ' + str(error), apps_launched=False, signed=False)))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-stage', 'output', 'public-inventory', 'interpreter', 'sdk-root', 'notice-root', 'root-renderer'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--stage-sha256', required=True)
    parser.add_argument('--training-worker-root', type=Path)
    parser.add_argument('--training-worker-inventory-sha256')
    parser.add_argument('--component', choices=('manager', 'generate', 'train'), default='manager')
    parser.add_argument('--source-manifest', type=Path)
    args = parser.parse_args(argv)
    try:
        inputs = vars(args)
        component = inputs.pop('component')
        subset = inputs.pop('source_manifest')
        if component == 'manager':
            if subset is not None:
                inputs['source_manifest'] = subset
            print(build_distribution(**inputs))
        else:
            require(subset is not None, 'explicit_workflow_frozen_subset_required')
            require(inputs.pop('training_worker_root') is None
                    and inputs.pop('training_worker_inventory_sha256') is None,
                    'workflow_training_worker_closure_not_qualified')
            inputs.pop('root_renderer')
            print(build_workflow_distribution(component=component, source_manifest=subset, **inputs))
    except (BuildHold, OSError, subprocess.SubprocessError) as error:
        parser.exit(2, 'Manager build held: ' + str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
