"""Synthetic packaging proof only: no freezer, SDK, app or interpreter is launched."""
from copy import deepcopy
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from tools import build_manager_distribution as build
from tools.prepare_manager_production_stage import root_entry, prepare_public_stage

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ['core/manager_launch_owner.py', 'suite_core/manager/bootstrap_entry.py',
            'suite_core/manager/entry_binding.py', 'suite_core/manager/frozen_entry.py',
            'suite_core/manager/launcher_main.py']


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build.json_bytes(value))


def row(name, raw):
    return {'path': name, 'bytes': len(raw), 'sha256': build.digest(raw)}


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source = tmp_path / 'export'
    files = {name: b'# synthetic public entry\n' for name in REQUIRED}
    files['suite_core/manager/MODULE_MANIFEST.json'] = build.json_bytes({
        'software_version': '0.3.1-alpha.1', 'runtime_files': REQUIRED})
    for name, raw in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    inventory = {'schema_version': 1, 'manifest_excludes_self': True,
                 'files': {name: {'size_bytes': len(raw), 'sha256': build.digest(raw)}
                           for name, raw in files.items()}}
    save(source / 'public-source.json', inventory)
    manifest = tmp_path / 'subset.json'
    save(manifest, {'schema': 1, 'source_rows': [row(n, r) for n, r in files.items()]})
    stage = prepare_public_stage(source, tmp_path / 'prepared',
        public_inventory_sha256=build.digest((source / 'public-source.json').read_bytes()),
        frozen_sources=manifest, frozen_sources_sha256=build.digest(manifest.read_bytes()),
        manifest_sha256=build.digest(files['suite_core/manager/MODULE_MANIFEST.json']))
    sdk = tmp_path / 'sdk'
    (sdk / 'velopack').mkdir(parents=True)
    (sdk / 'velopack/velopack.pyd').write_bytes(b'synthetic-sdk-not-executable')
    notices = tmp_path / 'notices'
    (notices / 'licenses').mkdir(parents=True)
    (notices / 'licenses/test.txt').write_bytes(b'synthetic-license')
    profile = deepcopy(build.decode((build.HERE / 'tool-inputs.json').read_bytes()))
    profile['sdk']['files'] = {'velopack/velopack.pyd': build.digest((sdk / 'velopack/velopack.pyd').read_bytes())}
    profile['notices'] = {'test.txt': {'source': 'licenses/test.txt',
                                    'sha256': build.digest((notices / 'licenses/test.txt').read_bytes())}}
    tool_file = tmp_path / 'tools.json'
    save(tool_file, profile)
    interpreter = tmp_path / 'python.exe'
    interpreter.write_bytes(b'synthetic-interpreter-not-executable')
    profile['python']['sha256'] = build.digest(interpreter.read_bytes())
    profile['python']['bytes'] = interpreter.stat().st_size
    save(tool_file, profile)
    calls = []
    monkeypatch.setattr(build, 'inspect_tools', lambda *args: calls.append('tools-inspected-synthetically'))
    monkeypatch.setattr(build, 'isolated_environment', lambda *args: {})
    def prohibited(*args, **kwargs):
        raise AssertionError('No process may launch in synthetic tests')
    monkeypatch.setattr(build.subprocess, 'run', prohibited)
    arguments = dict(source_stage=stage, output=tmp_path / 'build',
        stage_sha256=build.digest((stage / 'STAGE.json').read_bytes()),
        public_inventory=source / 'public-source.json', interpreter=interpreter,
        sdk_root=sdk, notice_root=notices, root_renderer=ROOT / 'tools/prepare_manager_production_stage.py',
        source_manifest=manifest, tool_inputs=tool_file)
    return arguments, calls


def fake_freeze(monkeypatch, calls, *, failure=False, drift=False, missing=False):
    def freeze(output, interpreter, environment, name):
        calls.append(name)
        if failure:
            raise build.BuildHold('child_freeze_failed')
        config = build.decode((output / 'stage/STAGE.json').read_bytes())
        contents = '_internal' if name == 'CollectiGenManager' else '_bootstrap'
        folder = output / 'dist' / name
        folder.mkdir(parents=True)
        raw = bytearray(256)
        raw[:2] = b'MZ'
        struct.pack_into('<I', raw, 60, 128)
        raw[128:132] = b'PE\0\0'
        struct.pack_into('<H', raw, 132, 0x8664)
        struct.pack_into('<H', raw, 152, 0x20b)
        (folder / (name + '.exe')).write_bytes(raw)
        for r in config['source_rows']:
            target = folder / contents / r['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((output / 'stage/source' / r['path']).read_bytes())
        for r in config['sdk_rows']:
            if not r['path'].endswith('.py'):
                target = folder / contents / r['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((output / 'stage/sdk' / r['path']).read_bytes())
        for r in config['notice_rows']:
            target = folder / contents / 'licenses' / Path(r['path']).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((output / 'stage' / r['path']).read_bytes())
        save(output / (name + '_ANALYSIS_CLOSURE.json'), {
            'status': 'ANALYSIS_MODULE_BOUND_PASSED', 'source_rows': len(config['source_rows']),
            'missing_local_modules': ['core.synthetic-missing'] if missing else [],
            'actual_pure_modules': sorted({r['path'][:-3].replace('/', '.').removesuffix('.__init__')
                for r in config['source_rows'] if r['path'].endswith('.py')} |
                {'.'.join(r['path'][:-3].replace('/', '.').removesuffix('.__init__').split('.')[:i])
                 for r in config['source_rows'] if r['path'].endswith('.py')
                 for i in range(1, len(r['path'][:-3].replace('/', '.').removesuffix('.__init__').split('.')))})})
        if drift:
            (output / 'stage/sdk/velopack/velopack.pyd').write_bytes(b'changed-sdk')
    monkeypatch.setattr(build, 'freeze', freeze)


def test_two_pass_pin_propagation_layout_and_no_application_launch(inputs, monkeypatch):
    args, calls = inputs
    fake_freeze(monkeypatch, calls)
    suite = build.build_distribution(**args)
    assert calls == ['tools-inspected-synthetically', 'CollectiGenManager', 'CollectiGen']
    package = (suite / 'MANAGER_PACKAGE.json').read_bytes()
    pin = build.digest(package)
    assert (args['output'] / 'stage/source/public_root_entry.py').read_text() == root_entry(pin)
    assert (args['source_stage'] / 'source/public_root_entry.py').read_text() == root_entry()
    assert build.decode(package)['manager_sha256'] == build.digest((suite / 'Apps/Manager/current/CollectiGenManager.exe').read_bytes())
    result = build.decode((args['output'] / 'BUILD_RESULT.json').read_bytes())
    assert result['status'] == 'BUILD_ONLY_NOT_EXECUTED'
    assert result['apps_launched'] is False and result['signed'] is False and result['native_qualified'] is False
    assert not (suite / 'Data').exists()


@pytest.mark.parametrize('missing', ['suite_core', 'suite_core.manager', 'suite_core.manager.frozen_entry'])
def test_declared_frozen_module_or_parent_missing_refuses_assembly(inputs, monkeypatch, missing):
    args, calls = inputs
    fake_freeze(monkeypatch, calls)
    original = build.freeze
    def freeze(output, interpreter, environment, name):
        original(output, interpreter, environment, name)
        report_path = output / (name + '_ANALYSIS_CLOSURE.json')
        report = build.decode(report_path.read_bytes())
        report['actual_pure_modules'].remove(missing)
        save(report_path, report)
    monkeypatch.setattr(build, 'freeze', freeze)
    with pytest.raises(build.BuildHold, match='declared_frozen_module_missing'):
        build.build_distribution(**args)
    assert 'CollectiGen' not in calls
    assert not (args['output'] / 'Suite').exists()


@pytest.mark.parametrize('quoted', [False, True])
@pytest.mark.parametrize('missing', ['qt_app.components', 'qt_app', 'suite_core.manager'])
def test_stock_missing_toc_normalizes_local_modules_and_packages(quoted, missing):
    from tools.manager_build.windows_build_isolation import local_graph_dependencies
    class MissingModule: pass
    class ExcludedModule: pass
    wrap = lambda name: "'" + name + "'" if quoted else name
    class Graph:
        def make_missing_toc(self):
            return [(wrap(missing), None, 'missing'),
                (wrap('qt_app.main_window'), None, 'missing'), ('external.optional', None, 'missing')]
        def find_node(self, name):
            return ExcludedModule() if name == wrap('qt_app.main_window') else MissingModule()
    absent, excluded = local_graph_dependencies(Graph(), {'qt_app', 'core', 'suite_core'})
    assert absent == [missing]
    assert excluded == ['qt_app.main_window']


@pytest.mark.parametrize('recipe', ['CollectiGen.spec', 'CollectiGenManager.spec'])
def test_both_recipes_use_the_same_normalized_stock_graph_helper(recipe):
    source = (build.HERE / recipe).read_text(encoding='utf-8')
    assert 'local_graph_dependencies(a.graph, local_top_level)' in source
    assert 'node.identifier.partition' not in source


def test_shared_card_import_does_not_load_ai_inference():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-B', '-c',
        "import sys; import qt_app.module_card; assert 'core.ai_interrogation' not in sys.modules"],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_duration_helpers_request_existing_lazy_estimator_only_when_called(monkeypatch):
    import qt_app.components as components
    calls = []
    estimate = SimpleNamespace(benchmarked_models=('synthetic',), warm_seconds=65,
        first_run_seconds=80, missing_models=(), stale_models=())
    def request(settings, count, models):
        calls.append((settings, count, models))
        return estimate
    monkeypatch.setattr(components, 'core', SimpleNamespace(estimate_ai_duration=request))
    assert not calls
    assert 'Estimated warm processing time' in components.ai_estimate_text('synthetic', 3, ('model',))
    assert 'AI estimate:' in components.ai_estimate_short_text('synthetic', 3, ('model',))
    assert calls == [('synthetic', 3, ('model',))] * 2


@pytest.mark.parametrize('case', ['source_drift', 'undeclared', 'nonpublic', 'sdk_drift',
                                  'unsafe_notice', 'fresh_output', 'root_drift', 'tool_template_drift'])
def test_preflight_refusals_leave_no_build_output(inputs, case):
    args, calls = inputs
    if case == 'source_drift':
        (args['source_stage'] / 'source' / REQUIRED[0]).write_bytes(b'changed')
    elif case == 'undeclared':
        (args['source_stage'] / 'source/unlisted.py').write_bytes(b'extra')
    elif case == 'nonpublic':
        inv = build.decode(args['public_inventory'].read_bytes())
        inv['files'].pop(REQUIRED[0])
        save(args['public_inventory'], inv)
        receipt = build.decode((args['source_stage'] / 'STAGE.json').read_bytes())
        receipt['public_inventory_sha256'] = build.digest(args['public_inventory'].read_bytes())
        save(args['source_stage'] / 'STAGE.json', receipt)
        args['stage_sha256'] = build.digest((args['source_stage'] / 'STAGE.json').read_bytes())
    elif case == 'sdk_drift':
        (args['sdk_root'] / 'velopack/velopack.pyd').write_bytes(b'changed')
    elif case in {'unsafe_notice', 'tool_template_drift'}:
        profile = build.decode(args['tool_inputs'].read_bytes())
        if case == 'unsafe_notice':
            profile['notices']['test.txt']['source'] = '../private-input'
        else:
            profile['templates']['CollectiGen.spec'] = '0' * 64
        save(args['tool_inputs'], profile)
    elif case == 'fresh_output':
        args['output'].mkdir()
    else:
        (args['source_stage'] / 'source/public_root_entry.py').write_bytes(b'changed')
    with pytest.raises(build.BuildHold):
        build.build_distribution(**args)
    assert not calls
    if case != 'fresh_output':
        assert not args['output'].exists()


@pytest.mark.parametrize('mode', ['child_failure', 'sealed_sdk_drift', 'missing_module'])
def test_failed_child_never_freezes_root_or_claims_completion(inputs, monkeypatch, mode):
    args, calls = inputs
    fake_freeze(monkeypatch, calls, failure=mode == 'child_failure',
                drift=mode == 'sealed_sdk_drift', missing=mode == 'missing_module')
    with pytest.raises(build.BuildHold):
        build.build_distribution(**args)
    assert 'CollectiGen' not in calls
    assert not (args['output'] / 'BUILD_RESULT.json').exists()
    assert (args['output'] / 'stage/source/public_root_entry.py').read_text() == root_entry()
    assert build.decode((args['output'] / 'BUILD_FAILURE.json').read_bytes())['apps_launched'] is False


@pytest.mark.parametrize('path', ['../escape.py', 'core/../escape.py', '/absolute',
                                 'core/private_probe.py', 'core\\alias.py', 'C:/private/value'])
def test_unsafe_paths_refuse_before_any_file_access(path):
    with pytest.raises(build.BuildHold):
        build.safe_path(path)


def test_renderer_does_not_execute_module_level_payload(inputs, tmp_path):
    args, _calls = inputs
    raw = args['root_renderer'].read_bytes() + b"\nraise AssertionError('must never run')\n"
    profile = build.decode(args['tool_inputs'].read_bytes())
    render = build.canonical_renderer(raw, profile['root_entry_ast_sha256'])
    assert render() == root_entry()


def test_root_failure_retains_child_pin_without_completion(inputs, monkeypatch):
    args, calls = inputs
    fake_freeze(monkeypatch, calls)
    child_freeze = build.freeze
    def fail_root(output, interpreter, environment, name):
        if name == 'CollectiGen':
            calls.append(name)
            raise build.BuildHold('root_freeze_failed')
        child_freeze(output, interpreter, environment, name)
    monkeypatch.setattr(build, 'freeze', fail_root)
    with pytest.raises(build.BuildHold, match='root_freeze_failed'):
        build.build_distribution(**args)
    package_pin = build.digest((args['output'] / 'MANAGER_PACKAGE.json').read_bytes())
    assert (args['output'] / 'stage/source/public_root_entry.py').read_text() == root_entry(package_pin)
    assert not (args['output'] / 'BUILD_RESULT.json').exists()
    assert not (args['output'] / 'Suite').exists()
    assert build.decode((args['output'] / 'BUILD_FAILURE.json').read_bytes())['reason'] == 'root_freeze_failed'


def test_input_stage_drift_after_child_refuses_root(inputs, monkeypatch):
    args, calls = inputs
    fake_freeze(monkeypatch, calls)
    child_freeze = build.freeze
    def drift_source(*parameters):
        child_freeze(*parameters)
        (args['source_stage'] / 'source' / REQUIRED[0]).write_bytes(b'changed-after-child')
    monkeypatch.setattr(build, 'freeze', drift_source)
    with pytest.raises(build.BuildHold, match='input_digest_changed'):
        build.build_distribution(**args)
    assert 'CollectiGen' not in calls
    assert not (args['output'] / 'BUILD_RESULT.json').exists()


@pytest.mark.parametrize('drift', [None, 'platform', 'version', 'distributions', 'interpreter',
    'empty_machine_verified', 'wrong_native_architecture', 'missing_native_architecture',
    'noninteger_native_architecture', 'conflicting_machine'])
def test_tool_inspection_is_isolated_and_refuses_drift(tmp_path, monkeypatch, drift):
    interpreter = tmp_path / 'python.exe'
    raw = b'not-an-executable'
    interpreter.write_bytes(raw)
    profile = {'platform': {'os': 'windows', 'architecture': 'x86_64', 'python_abi': 'cp314'},
               'python': {'sha256': build.digest(raw), 'bytes': len(raw), 'version': '3.14.7'},
               'distributions': {'pyinstaller': '6.22.3'}}
    observed = {'platform': 'win32', 'bits': 64, 'machine': 'AMD64', 'version': '3.14.7',
                'native_processor_architecture': 9,
                'distributions': dict(profile['distributions'])}
    commands = []
    if drift == 'interpreter':
        interpreter.write_bytes(b'changed')
    elif drift == 'platform':
        observed['platform'] = 'linux'
    elif drift == 'version':
        observed['version'] = '3.13.0'
    elif drift == 'distributions':
        observed['distributions']['pyinstaller'] = '6.22.0'
    elif drift == 'empty_machine_verified':
        observed['machine'] = ''
    elif drift == 'wrong_native_architecture':
        observed['machine'] = ''
        observed['native_processor_architecture'] = 12
    elif drift == 'missing_native_architecture':
        observed.pop('native_processor_architecture')
    elif drift == 'noninteger_native_architecture':
        observed['native_processor_architecture'] = 9.0
    elif drift == 'conflicting_machine':
        observed['machine'] = 'ARM64'
    def inspect(command, **kwargs):
        commands.append(command)
        assert command[:4] == [str(interpreter), '-I', '-B', '-c']
        assert kwargs == {'env': environment, 'capture_output': True, 'check': True, 'text': True}
        return SimpleNamespace(stdout=json.dumps(observed))
    monkeypatch.setattr(build.subprocess, 'run', inspect)
    environment = {'PATH': 'synthetic-approved-path'}
    if drift and drift != 'empty_machine_verified':
        with pytest.raises(build.BuildHold):
            build.inspect_tools(interpreter, profile, environment)
    else:
        build.inspect_tools(interpreter, profile, environment)
    assert len(commands) == (0 if drift == 'interpreter' else 1)


@pytest.mark.parametrize('name', ['CollectiGenManager', 'CollectiGen'])
def test_freezer_command_requires_isolated_imports(tmp_path, monkeypatch, name):
    (tmp_path / 'stage').mkdir()
    interpreter = tmp_path / 'python.exe'
    environment = {'PATH': 'approved-only'}
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        # A non-isolated invocation can admit user-site or PYTHONPATH packages.
        assert command[:5] == [str(interpreter), '-I', '-B', '-m', 'PyInstaller']
        assert kwargs['env'] is environment
        assert kwargs['cwd'] == tmp_path / 'stage'
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(build.subprocess, 'run', run)
    build.freeze(tmp_path, interpreter, environment, name)
    assert len(commands) == 1
    recorded = build.decode((tmp_path / (name + '_BUILD_COMMAND.json')).read_bytes())
    assert recorded['command'] == commands[0] and recorded['app_launch'] is False


def test_import_environment_excludes_user_injection(tmp_path, monkeypatch):
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'windows'))
    monkeypatch.setenv('PYTHONPATH', 'uninspected-user-modules')
    monkeypatch.setenv('PYTHONHOME', 'uninspected-user-runtime')
    monkeypatch.setenv('PATH', 'uninspected-user-binaries')
    environment = build.isolated_environment(tmp_path, tmp_path / 'native/python.exe')
    assert 'PYTHONPATH' not in environment and 'PYTHONHOME' not in environment
    assert 'uninspected-user-binaries' not in environment['PATH']


def test_inspection_and_both_freezes_share_environment(inputs, monkeypatch):
    args, calls = inputs
    environment = {'PATH': 'same-isolated-environment'}
    monkeypatch.setattr(build, 'isolated_environment', lambda *unused: environment)
    seen = []
    def inspect(interpreter, profile, passed):
        assert passed is environment
        seen.append('inspection')
    monkeypatch.setattr(build, 'inspect_tools', inspect)
    fake_freeze(monkeypatch, calls)
    substitute = build.freeze
    def freeze(output, interpreter, passed, name):
        assert passed is environment
        seen.append(name)
        substitute(output, interpreter, passed, name)
    monkeypatch.setattr(build, 'freeze', freeze)
    build.build_distribution(**args)
    assert seen == ['inspection', 'CollectiGenManager', 'CollectiGen']


@pytest.mark.parametrize('change', ['support', 'sdk', 'notice', 'omitted', 'added', 'wrapper'])
def test_child_tree_drift_after_root_refuses_assembly(inputs, monkeypatch, change):
    args, calls = inputs
    fake_freeze(monkeypatch, calls)
    substitute = build.freeze
    def freeze(output, interpreter, environment, name):
        substitute(output, interpreter, environment, name)
        child = output / 'dist/CollectiGenManager/_internal'
        support = child / 'support.dll'
        if name == 'CollectiGenManager':
            support.write_bytes(b'original-support')
        elif change == 'support':
            support.write_bytes(b'changed-support')
        elif change == 'sdk':
            (child / 'velopack/velopack.pyd').write_bytes(b'changed-sdk')
        elif change == 'notice':
            (child / 'licenses/test.txt').write_bytes(b'changed-notice')
        elif change == 'omitted':
            support.unlink()
        elif change == 'added':
            (child / 'extra.dll').write_bytes(b'added-support')
        else:
            # Child must retain its original unbound wrapper, even after root binding.
            (child / 'public_root_entry.py').write_bytes(
                (output / 'stage/source/public_root_entry.py').read_bytes())
    monkeypatch.setattr(build, 'freeze', freeze)
    with pytest.raises(build.BuildHold, match='child_tree_changed_after_root_freeze'):
        build.build_distribution(**args)
    assert calls == ['tools-inspected-synthetically', 'CollectiGenManager', 'CollectiGen']
    assert not (args['output'] / 'Suite').exists()
    assert not (args['output'] / 'BUILD_RESULT.json').exists()
    assert build.decode((args['output'] / 'BUILD_FAILURE.json').read_bytes())['status'] == 'BUILD_HELD'
