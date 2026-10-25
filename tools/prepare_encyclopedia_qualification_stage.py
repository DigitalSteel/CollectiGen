"""Prepare pinned Encyclopedia fixture or normal source stages; never freeze/run/sign."""
from pathlib import Path
import argparse
import ast
from hashlib import sha256
import json
import re

from tools import build_manager_distribution as stock
from tools.encyclopedia_build.profile_renderer import render_entry, render_fixture_profile, validate_profile
from tools.encyclopedia_build.profile_renderer import render_normal_entry, render_normal_profile, validate_normal_profile

MODULE = 'suite_core/encyclopedia/MODULE_MANIFEST.json'
PURPOSE = 'ENCYCLOPEDIA_QUALIFICATION_BUILD_ONLY'
NORMAL_PURPOSE = 'ENCYCLOPEDIA_NORMAL_PORTABLE_BUILD_ONLY'
REQUIRED = frozenset({MODULE, 'core/manager_launch_owner.py', 'core/component_acquisition.py',
    'core/encyclopedia_entry_adapter.py', 'core/encyclopedia_enrollment.py', 'core/encyclopedia_installation.py',
    'core/encyclopedia_update_binding.py', 'suite_core/manager/bootstrap_entry.py',
    'suite_core/encyclopedia/__main__.py', 'suite_core/encyclopedia/bootstrap_entry.py',
    'suite_core/encyclopedia/entry_binding.py', 'suite_core/encyclopedia/control_entry.py',
    'suite_core/encyclopedia/custody_entry.py',
    'suite_core/discovery/component_release_signature.py'})
BUILD_INPUTS = {
    'recipe_support.py': 'tools/encyclopedia_build/recipe_support.py',
    'TagEncyclopediaReader.spec': 'tools/encyclopedia_build/TagEncyclopediaReader.spec',
    'TagEncyclopediaRoot.spec': 'tools/encyclopedia_build/TagEncyclopediaRoot.spec',
    'build_validators.py': 'tools/build_manager_distribution.py',
    'windows_build_isolation.py': 'tools/manager_build/windows_build_isolation.py'}
EXCLUDED_PARTS = {'local only', 'training', 'output', 'models', 'logs', 'backups', '.venv', '.aws'}


def _admit_executing_validators(source_root, toolchain, toolchain_pin, inventory_pin):
    """Bootstrap admission uses stdlib only, before calling any project validator."""
    def read_checked(path, pin):
        path = Path(path).absolute()
        for item in (path, *path.parents):
            if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
                raise ValueError('executing_validator_redirect_refused')
        raw = path.read_bytes()
        if type(pin) is not str or sha256(raw).hexdigest() != pin:
            raise ValueError('executing_validator_input_pin_changed')
        return raw
    inventory = json.loads(read_checked(source_root / 'public-source.json', inventory_pin))
    tools = json.loads(read_checked(toolchain, toolchain_pin))
    from core import encyclopedia_update_binding as binding
    from suite_core.discovery import component_release_signature as signature
    from tools.encyclopedia_build import profile_renderer as renderer
    from tools.manager_build import windows_build_isolation as isolation
    executed = {
        'tools/build_manager_distribution.py': (stock, stock.check_row),
        'tools/manager_build/windows_build_isolation.py': (isolation, isolation.local_graph_dependencies),
        'tools/prepare_encyclopedia_qualification_stage.py': (None, prepare_stage),
        'tools/encyclopedia_build/profile_renderer.py': (renderer, renderer.validate_profile),
        'core/encyclopedia_update_binding.py': (binding, binding._data_contract),
        'suite_core/discovery/component_release_signature.py': (signature, signature._version)}
    tool_rows = {row['path']: row for row in tools['build_inputs']}
    for name, (module, function) in executed.items():
        public = inventory['files'][name]
        if name.startswith('tools/'):
            declared = tool_rows[name]
            if public != {'size_bytes': declared['bytes'], 'sha256': declared['sha256']}:
                raise ValueError('executing_validator_public_pin_mismatch:' + name)
        origin = Path(__file__ if module is None else module.__file__).absolute()
        if Path(function.__code__.co_filename).absolute() != origin:
            raise ValueError('executing_validator_function_origin_mismatch:' + name)
        loaded = read_checked(origin, public['sha256'])
        accepted = read_checked(source_root / name, public['sha256'])
        if len(loaded) != public['size_bytes'] or loaded != accepted:
            raise ValueError('executing_validator_bytes_mismatch:' + name)


def validate_toolchain(value, *, purpose=PURPOSE):
    stock.require(purpose in (PURPOSE, NORMAL_PURPOSE), 'exact_encyclopedia_build_purpose_required')
    stock.require(type(value) is dict and set(value) == {'schema', 'purpose', 'platform', 'python', 'distributions', 'build_inputs'}
        and type(value['schema']) is int and value['schema'] == 1 and value['purpose'] == purpose,
        'exact_encyclopedia_toolchain_required')
    platform = value['platform']
    stock.require(type(platform) is dict and set(platform) == {'os', 'architecture', 'python_abi'}
        and platform['os'] == 'windows' and platform['architecture'] == 'x86_64'
        and platform['python_abi'] in {'cp313', 'cp314'}, 'explicit_windows_x64_toolchain_required')
    python = value['python']
    stock.require(type(python) is dict and set(python) == {'version', 'bytes', 'sha256'}
        and type(python['version']) is str and re.fullmatch(r'3\.(13|14)\.[0-9]+', python['version'])
        and platform['python_abi'] == 'cp3' + python['version'].split('.')[1]
        and type(python['bytes']) is int and 0 < python['bytes'] <= stock.MAX_FILE
        and type(python['sha256']) is str and re.fullmatch('[0-9a-f]{64}', python['sha256']),
        'exact_interpreter_input_required')
    wheels = value['distributions']
    stock.require(type(wheels) is dict and {'pyinstaller', 'PySide6-Essentials', 'shiboken6'} <= set(wheels)
        and all(type(k) is str and type(v) is str and 0 < len(v) <= 64 for k, v in wheels.items()),
        'exact_wheel_versions_required')
    rows = stock.rows(value['build_inputs'])
    stock.require(set(rows) == set(BUILD_INPUTS.values()) | {'tools/encyclopedia_build/profile_renderer.py', 'tools/prepare_encyclopedia_qualification_stage.py'},
        'exact_build_input_membership_required')
    return rows


def prepare_stage(source_root, output, *, public_inventory_sha256, frozen_sources,
                  frozen_sources_sha256, module_sha256, toolchain, toolchain_sha256,
                  fixture_profile=None, fixture_profile_sha256=None, include_python_sources=True,
                  normal_profile=None, normal_profile_sha256=None):
    source_root, output = Path(source_root).absolute(), Path(output).absolute()
    _admit_executing_validators(source_root, toolchain, toolchain_sha256, public_inventory_sha256)
    normal = normal_profile is not None
    stock.require(normal == (normal_profile_sha256 is not None), 'explicit_normal_profile_pin_required')
    stock.require(not normal or fixture_profile is None and fixture_profile_sha256 is None,
        'mixed_encyclopedia_profiles_refused')
    stock.require(not normal or include_python_sources is False, 'normal_compiled_sources_only_required')
    purpose = NORMAL_PURPOSE if normal else PURPOSE
    stock.require(type(include_python_sources) is bool, 'explicit_python_source_payload_policy_required')
    stock.unredirected(source_root)
    stock.unredirected(output)
    stock.require(source_root.resolve() == source_root and output.resolve() == output
        and source_root.is_dir() and not output.exists()
        and not output.is_relative_to(source_root) and not source_root.is_relative_to(output),
        'fresh_separate_source_stage_required')
    inventory_path = source_root / 'public-source.json'
    inventory_raw = stock.pinned(inventory_path, public_inventory_sha256)
    inventory = stock.decode(inventory_raw)
    stock.require(inventory.get('schema_version') == 1 and inventory.get('manifest_excludes_self') is True
        and type(inventory.get('files')) is dict, 'accepted_public_inventory_required')
    subset_raw = stock.pinned(Path(frozen_sources), frozen_sources_sha256)
    subset = stock.decode(subset_raw)
    stock.require(type(subset) is dict and set(subset) == {'schema', 'source_rows'}
        and type(subset['schema']) is int and subset['schema'] == 1, 'finite_frozen_subset_required')
    rows = stock.rows(subset['source_rows'])
    stock.require(REQUIRED <= set(rows), 'canonical_encyclopedia_entry_membership_required')
    module_raw = stock.pinned(source_root / MODULE, module_sha256)
    module = stock.decode(module_raw)
    stock.require(module.get('module_id') == 'collectigen.tag-encyclopedia'
        and type(module.get('runtime_files')) is list, 'exact_encyclopedia_module_required')
    from core.encyclopedia_update_binding import _data_contract
    from suite_core.discovery.component_release_signature import _version
    _version(module['software_version'])
    # Bind the existing exact code-only declaration, not a fabricated compatibility flag.
    _data_contract(module_raw, module['software_version'])
    declared = set(module['runtime_files'])
    sources = {}
    for name, row in rows.items():
        stock.require(not any(part.casefold() in EXCLUDED_PARTS or part.casefold().startswith('private_')
            for part in Path(name).parts), 'private_or_mutable_source_refused')
        public = inventory['files'].get(name)
        stock.require(public == {'size_bytes': row['bytes'], 'sha256': row['sha256']}
            and (not name.endswith('.py') or name in declared), 'source_not_public_runtime_declared')
        path = source_root / name
        stock.unredirected(path)
        raw = stock.check_row(path, row)
        if name.endswith('.py'):
            tree = ast.parse(raw.decode('utf-8-sig'))
            imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
            imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            stock.require(not any(n.startswith(('private_', 'PySide6.QtTest', 'suite_core.tests')) for n in imports),
                'private_QA_import_refused')
        sources[name] = raw
    stock.require(sources[MODULE] == module_raw, 'module_pin_differs_from_subset')
    tools_raw = stock.pinned(Path(toolchain), toolchain_sha256)
    tools = stock.decode(tools_raw)
    build_rows = validate_toolchain(tools, purpose=purpose)
    build_bytes = {name: stock.check_row(source_root / name, row) for name, row in build_rows.items()}
    from tools.encyclopedia_build import profile_renderer
    stock.check_row(Path(__file__), build_rows['tools/prepare_encyclopedia_qualification_stage.py'])
    stock.check_row(Path(profile_renderer.__file__), build_rows['tools/encyclopedia_build/profile_renderer.py'])
    for name, row in build_rows.items():
        stock.require(inventory['files'].get(name) == {'size_bytes': row['bytes'], 'sha256': row['sha256']},
            'build_tool_not_accepted_public_source')
    bound = fixture_profile is not None
    stock.require(bound == (fixture_profile_sha256 is not None), 'explicit_profile_pin_required')
    generated = {f'encyclopedia_{role}_entry.py': render_entry(role, bound=bound) for role in ('root', 'reader')}
    if bound:
        raw = stock.pinned(Path(fixture_profile), fixture_profile_sha256)
        profile = validate_profile(stock.decode(raw))
        fixture_root = Path(profile['fixture_root'])
        stock.unredirected(fixture_root)
        stock.require(not fixture_root.exists() and not fixture_root.is_relative_to(source_root)
            and not source_root.is_relative_to(fixture_root), 'fresh_disposable_fixture_layout_required')
        generated['encyclopedia_fixture_profile.py'] = render_fixture_profile(profile)
    if normal:
        raw = stock.pinned(Path(normal_profile), normal_profile_sha256)
        profile = validate_normal_profile(stock.decode(raw))
        stock.require(profile['host_version'] == module['software_version'], 'normal_profile_module_version_mismatch')
        stock.require({'core/component_release_transport.py', 'suite_core/installation.py'} <= set(rows),
            'normal_profile_runtime_membership_required')
        generated = {f'encyclopedia_{role}_entry.py': render_normal_entry(role) for role in ('root', 'reader')}
        generated['encyclopedia_compiled_profile.py'] = render_normal_profile(profile)
    # Validate all admitted bytes before creating output, then recheck after copy.
    output.mkdir(parents=True)
    stage = output / 'stage'
    (stage / 'source').mkdir(parents=True)
    for name, raw in sources.items():
        stock.check_row(source_root / name, rows[name])
        stock.write_new(stage / 'source' / name, raw)
    for name, raw in generated.items(): stock.write_new(stage / 'source' / name, raw)
    staged_build = []
    for target, origin in BUILD_INPUTS.items():
        raw = build_bytes[origin]
        stock.write_new(stage / target, raw)
        staged_build.append({'path': target, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    for name, row in rows.items():
        stock.check_row(source_root / name, row)
        stock.check_row(stage / 'source' / name, row)
    for name, row in build_rows.items(): stock.check_row(source_root / name, row)
    stock.pinned(inventory_path, public_inventory_sha256)
    stock.pinned(Path(frozen_sources), frozen_sources_sha256)
    stock.pinned(Path(toolchain), toolchain_sha256)
    if bound: stock.pinned(Path(fixture_profile), fixture_profile_sha256)
    if normal: stock.pinned(Path(normal_profile), normal_profile_sha256)
    config = {'schema': 1, 'purpose': purpose, 'component_id': 'collectigen.tag-encyclopedia',
        'software_version': module['software_version'], 'source_rows': list(rows.values()),
        'repository_local_top_levels': sorted({name.split('/')[0].removesuffix('.py')
            for name in inventory['files'] if name.endswith('.py')}),
        'generated_rows': [{'path': n, 'bytes': len(b), 'sha256': sha256(b).hexdigest()} for n, b in generated.items()],
        'build_rows': staged_build, 'profile_bound': bound, 'public_inventory_sha256': public_inventory_sha256,
        'include_python_sources': include_python_sources,
        'frozen_sources_sha256': frozen_sources_sha256, 'module_sha256': module_sha256,
        'toolchain_sha256': toolchain_sha256, 'fixture_profile_sha256': fixture_profile_sha256,
        'status': 'SOURCE_STAGE_ONLY_NOT_FROZEN', 'signed_windows_fixtures_verified': False,
        'build_or_app_execution': False, 'production_activation': False}
    if normal:
        config.update(profile_bound=True, profile_kind='normal-portable', normal_profile_sha256=normal_profile_sha256)
    stock.write_new(stage / 'STAGE.json', stock.json_bytes(config))
    stock.write_new(stage / 'TOOLCHAIN.json', tools_raw)
    return stage


def verify_prepared_stage(stage, expected_sha256):
    stage = Path(stage).absolute()
    stock.unredirected(stage)
    raw = stock.pinned(stage / 'STAGE.json', expected_sha256)
    config = stock.decode(raw)
    stock.require(config.get('purpose') in (PURPOSE, NORMAL_PURPOSE)
        and config.get('status') == 'SOURCE_STAGE_ONLY_NOT_FROZEN',
        'exact_prepared_encyclopedia_stage_required')
    expected = {'STAGE.json', 'TOOLCHAIN.json'}
    for key, prefix in (('source_rows', 'source/'), ('generated_rows', 'source/'), ('build_rows', '')):
        for name, row in stock.rows(config[key]).items():
            stock.check_row(stage / prefix / name, row)
            expected.add(prefix + name)
    stock.pinned(stage / 'TOOLCHAIN.json', config['toolchain_sha256'])
    actual = set()
    for path in stage.rglob('*'):
        stock.unredirected(path)
        if path.is_file(): actual.add(path.relative_to(stage).as_posix())
    stock.require(actual == expected, 'undeclared_or_missing_prepared_stage_member')
    return config


def main(argv=None):
    import sys
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'verify-stage':
        parser = argparse.ArgumentParser(description='Verify exact stage bytes only; never freeze/run.')
        parser.add_argument('--stage', required=True)
        parser.add_argument('--stage-sha256', required=True)
        parsed = parser.parse_args(arguments[1:])
        return verify_prepared_stage(parsed.stage, parsed.stage_sha256)
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('source-root', 'output', 'public-inventory-sha256', 'frozen-sources', 'frozen-sources-sha256',
                'module-sha256', 'toolchain', 'toolchain-sha256'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--fixture-profile')
    parser.add_argument('--fixture-profile-sha256')
    parser.add_argument('--normal-profile')
    parser.add_argument('--normal-profile-sha256')
    parser.add_argument('--compiled-sources-only', dest='include_python_sources', action='store_false', default=True)
    return prepare_stage(**vars(parser.parse_args(arguments)))


if __name__ == '__main__':
    print(main())
