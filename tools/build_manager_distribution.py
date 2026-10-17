"""Build-only Manager producer from a pinned public source stage; never launches apps."""
from __future__ import annotations

import argparse
import ast
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
             'sdk_binary_origin.py', 'windows_build_isolation.py'}
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


def validate_sources(stage, stage_sha256, manifest_raw, inventory_raw):
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
    require(required_entries <= expected, 'canonical_manager_entries_required')
    require(all(n.startswith(('core/', 'suite_core/', 'qt_app/', 'licenses/', 'tools/'))
                or n in {'LICENSE', 'LICENSES.md', 'THIRD_PARTY_NOTICES.md', 'version.py',
                         'runtime_contract.py', 'release_contract.py'} for n in expected), 'nonpublic_source_scope')
    require(digest(inventory_raw) == receipt.get('public_inventory_sha256'), 'public_inventory_changed')
    inventory = decode(inventory_raw)
    require(inventory.get('schema_version') == 1 and inventory.get('manifest_excludes_self') is True
            and type(inventory.get('files')) is dict, 'public_inventory_required')
    all_rows = rows(receipt.get('source_rows'), adapter=True)
    require('public_root_entry.py' in all_rows, 'generated_unbound_root_required')
    generated = {'public_root_entry.py': all_rows.pop('public_root_entry.py')}
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
    module = decode(sources.get('suite_core/manager/MODULE_MANIFEST.json', b'{}'))
    require(module.get('software_version') == version, 'manager_version_changed')
    require(set(name for name in expected if name.endswith('.py')) <= set(module.get('runtime_files', [])),
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


def canonical_renderer(raw, expected_ast):
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
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<canonical-root-entry>', 'exec'), namespace)
    return namespace['root_entry']


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


def freeze(output, interpreter, environment, name):
    command = [str(interpreter), '-I', '-B', '-m', 'PyInstaller', '--noconfirm',
               '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'),
               str(output / 'stage' / (name + '.spec'))]
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
    allowed = {r['path'][:-3].replace('/', '.').removesuffix('.__init__') for r in config['source_rows'] if r['path'].endswith('.py')}
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
    for path in folder.rglob('*'):
        unredirected(path)
        relative = path.relative_to(folder)
        require(not any(p.casefold() in {'data', 'models', 'logs', 'backups', 'catalogs'}
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


def build_distribution(source_stage, output, *, stage_sha256, public_inventory, interpreter, sdk_root,
                       notice_root, root_renderer, source_manifest=HERE / 'source-manifest.json',
                       tool_inputs=HERE / 'tool-inputs.json'):
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
    render = canonical_renderer(renderer_raw, profile['root_entry_ast_sha256'])
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
    write_new(output / 'SEALED_INPUTS.json', json_bytes({'stage_sha256': stage_sha256,
        'source_manifest_sha256': digest(manifest_raw), 'tool_inputs_sha256': digest(profile_raw),
        'root_renderer_sha256': digest(renderer_raw), 'public_inventory_sha256': receipt['public_inventory_sha256']}))
    def set_root(text):
        raw = text.encode()
        path = stage / 'source/public_root_entry.py'
        path.write_bytes(raw)
        config['source_rows'] = [r for r in config['source_rows'] if r['path'] != 'public_root_entry.py']
        config['source_rows'].append({'path': 'public_root_entry.py', 'bytes': len(raw), 'sha256': digest(raw)})
        (stage / 'STAGE.json').write_bytes(json_bytes(config))
        require(path.read_bytes() == raw, 'root_entry_readback_failed')
    set_root(unbound)
    try:
        verify_working(stage, config, metadata)
        require(len(pinned(interpreter, profile['python']['sha256'])) == profile['python']['bytes'],
                'interpreter_size_changed')
        freeze(output, interpreter, environment, 'CollectiGenManager')
        verify_working(stage, config, metadata)
        child = verify_freeze(output, 'CollectiGenManager', '_internal', config)
        child_config = decode(json_bytes(config))  # Retain its unbound wrapper expectation.
        child_inventory = tree_inventory(child.parent)
        validate_sources(source_stage, stage_sha256, manifest_raw, inventory_raw)
        package = {'schema': 1, 'manager_version': receipt['software_version'], 'manager_sha256': digest(read(child)),
                   'protocol': 1, 'minimum_bootstrap': '0.1.0',
                   'maximum_bootstrap_exclusive': '0.2.0',
                   'manager_relative': 'Apps/Manager/current/CollectiGenManager.exe'}
        package_raw = json_bytes(package)
        write_new(output / 'MANAGER_PACKAGE.json', package_raw)
        package_pin = digest(package_raw)
        set_root(render(package_pin))
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
        shutil.copytree(root_exe.parent, suite)
        shutil.copytree(child.parent, suite / 'Apps/Manager/current')
        require(tree_inventory(suite / 'Apps/Manager/current') == child_inventory,
                'child_distribution_copy_changed')
        write_new(suite / 'MANAGER_PACKAGE.json', package_raw)
        require(not (suite / 'Data').exists(), 'mutable_data_in_distribution')
        files = []
        for path in sorted(suite.rglob('*')):
            unredirected(path)
            if path.is_file():
                raw = read(path)
                source = root_exe.parent / path.relative_to(suite)
                if path.is_relative_to(suite / 'Apps/Manager/current'):
                    source = child.parent / path.relative_to(suite / 'Apps/Manager/current')
                if path.name != 'MANAGER_PACKAGE.json':
                    require(raw == read(source), 'distribution_copy_changed')
                files.append({'path': path.relative_to(suite).as_posix(), 'bytes': len(raw), 'sha256': digest(raw)})
        result = {'status': 'BUILD_ONLY_NOT_EXECUTED', 'software_version': receipt['software_version'],
                  'manager_package_sha256': package_pin, 'files': files, 'apps_launched': False,
                  'signed': False, 'native_qualified': False}
        write_new(output / 'BUILD_RESULT.json', json_bytes(result))
        return suite
    except Exception as error:
        write_new(output / 'BUILD_FAILURE.json', json_bytes({'status': 'BUILD_HELD', 'reason': str(error),
                                                          'error_type': type(error).__name__,
                                                          'apps_launched': False, 'signed': False}))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-stage', 'output', 'public-inventory', 'interpreter', 'sdk-root', 'notice-root', 'root-renderer'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--stage-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        print(build_distribution(**vars(args)))
    except (BuildHold, OSError, subprocess.SubprocessError) as error:
        parser.exit(2, 'Manager build held: ' + str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
