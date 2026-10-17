"""Synthetic build preparation only. No freezer, executable or signer runs."""
import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from urllib.request import Request

import pytest

from tools import build_manager_distribution as stock
from tools.prepare_encyclopedia_qualification_stage import prepare_stage, REQUIRED, BUILD_INPUTS, PURPOSE, validate_toolchain
from tools.encyclopedia_build.profile_renderer import render_fixture_profile, render_entry, validate_profile
from tools.encyclopedia_build import recipe_support
from tools.encyclopedia_build.profile_renderer import (
    NORMAL_PURPOSE, validate_normal_profile, render_normal_profile, render_normal_entry)


def raw(value): return json.dumps(value, sort_keys=True).encode()
def row(path, data): return {'path': path, 'bytes': len(data), 'sha256': sha256(data).hexdigest()}


def normal_profile():
    import base64
    trust = raw({'schema_version': 1, 'keys': [{'key_id': 'synthetic-public-only',
        'algorithm': 'rsa-pkcs1v15-sha256', 'modulus': base64.urlsafe_b64encode(b'\xff' * 256).decode().rstrip('='),
        'exponent': 65537, 'status': 'active'}]})
    return dict(schema=1, purpose=NORMAL_PURPOSE, layout_id='portable-executable-relative-v1',
        trust_hex=trust.hex(), trust_sha256=sha256(trust).hexdigest(), channel='beta',
        host_version='0.2.0-alpha.1', transport_id='github-release-assets-https',
        asset_redirect_origins=['https://release-assets.githubusercontent.com'])


def normal_namespace(root, monkeypatch):
    namespace = {}
    exec(compile(render_normal_profile(normal_profile()), '<synthetic-normal-profile>', 'exec'), namespace)
    root.mkdir(); (root / 'TagEncyclopedia.exe').write_bytes(b'INERT ROOT; NEVER EXECUTE')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'TagEncyclopedia.exe'))
    monkeypatch.setattr(sys, 'argv', [str(root / 'TagEncyclopedia.exe')])
    return namespace


@pytest.fixture(autouse=True)
def no_process_or_network(monkeypatch):
    import subprocess, urllib.request, socket
    def deny(*args, **kwargs): raise AssertionError('No build/native/network/signing action authorized')
    monkeypatch.setattr(subprocess, 'run', deny)
    monkeypatch.setattr(subprocess, 'Popen', deny)
    monkeypatch.setattr(urllib.request, 'urlopen', deny)
    monkeypatch.setattr(socket, 'socket', deny)


def profile(tmp_path):
    # Public schema-only placeholder, explicitly NOT a verified fixture signature.
    trust = raw({'schema_version': 1, 'keys': [{'key_id': 'public-placeholder-not-admitted',
        'algorithm': 'rsa-pkcs1v15-sha256', 'modulus': 'PUBLIC SCHEMA PLACEHOLDER',
        'exponent': 65537, 'status': 'active'}]})
    return {'schema': 1, 'purpose': 'ENCYCLOPEDIA_ISOLATED_QUALIFICATION_ONLY',
        'fixture_root': str(tmp_path / 'disposable-fixture'), 'data_relative': 'Data',
        'settings_relative': 'Data/manager.json', 'cache_relative': 'Data/Encyclopedia/UpdateCache/attempt',
        'trust_hex': trust.hex(), 'trust_sha256': sha256(trust).hexdigest(),
        'responses_sha256': 'a' * 64, 'channel': 'beta', 'host_version': '0.2.0-alpha.1'}


def public_inputs(tmp_path, *, normal=False):
    source = tmp_path / 'export'; source.mkdir()
    module = json.loads(Path('suite_core/encyclopedia/MODULE_MANIFEST.json').read_text(encoding='utf-8-sig'))
    sources = {name: b'# INERT SOURCE; NEVER EXECUTE\n' for name in REQUIRED}
    if normal:
        sources.update({name: b'# INERT NORMAL DEPENDENCY\n' for name in
            ('core/component_release_transport.py', 'suite_core/installation.py')})
    sources['core/manager_launch_owner.py'] = b'from release_contract import sha256_file\n'  # admitted importer
    for name in ('core/__init__.py', 'suite_core/__init__.py', 'suite_core/manager/__init__.py',
                 'suite_core/encyclopedia/__init__.py', 'suite_core/discovery/__init__.py'):
        sources[name] = b'# INERT PACKAGE\n'
    for name in ('core/encyclopedia_update_binding.py', 'suite_core/discovery/component_release_signature.py'):
        sources[name] = Path(name).read_bytes()  # pinned validator bytes, never execute exported copies
    module['runtime_files'] = sorted(name for name in sources if name.endswith('.py')) + ['release_contract.py']
    sources['suite_core/encyclopedia/MODULE_MANIFEST.json'] = raw(module)
    repo = Path.cwd()
    tools = set(BUILD_INPUTS.values()) | {'tools/encyclopedia_build/profile_renderer.py',
        'tools/prepare_encyclopedia_qualification_stage.py'}
    for name in tools: sources[name] = (repo / name).read_bytes()
    sources['runtime_contract.py'] = b'# PUBLIC BUT NOT RUNTIME DECLARED\n'
    for name, data in sources.items():
        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    inventory = raw({'schema_version': 1, 'manifest_excludes_self': True,
        'files': {name: {'size_bytes': len(data), 'sha256': sha256(data).hexdigest()} for name, data in sources.items()}})
    (source / 'public-source.json').write_bytes(inventory)
    subset = raw({'schema': 1, 'source_rows': [row(name, sources[name]) for name in sorted(sources)
        if name not in tools and name != 'runtime_contract.py']})
    subset_path = tmp_path / 'subset.json'; subset_path.write_bytes(subset)
    from tools.prepare_encyclopedia_qualification_stage import NORMAL_PURPOSE
    toolchain = raw({'schema': 1, 'purpose': NORMAL_PURPOSE if normal else PURPOSE,
        'platform': {'os': 'windows', 'architecture': 'x86_64', 'python_abi': 'cp313'},
        'python': {'version': '3.13.15', 'bytes': 16, 'sha256': '0' * 64},
        'distributions': {'pyinstaller': '6.22.3', 'PySide6-Essentials': '6.11.2', 'shiboken6': '6.11.2'},
        'build_inputs': [row(name, sources[name]) for name in sorted(tools)]})
    tool_path = tmp_path / 'tools.json'; tool_path.write_bytes(toolchain)
    args = dict(public_inventory_sha256=sha256(inventory).hexdigest(), frozen_sources=subset_path,
        frozen_sources_sha256=sha256(subset).hexdigest(),
        module_sha256=sha256(sources['suite_core/encyclopedia/MODULE_MANIFEST.json']).hexdigest(),
        toolchain=tool_path, toolchain_sha256=sha256(toolchain).hexdigest())
    return source, args


def test_renderer_emits_fixed_roles_and_keeps_custody_child_cache_inert(tmp_path, monkeypatch):
    value = profile(tmp_path)
    data = render_fixture_profile(value)
    tree = ast.parse(data)
    assert any(isinstance(n, ast.ClassDef) and n.name == 'SealedEncyclopediaFixtureOpener' for n in tree.body)
    namespace = {}; exec(compile(tree, '<synthetic-profile>', 'exec'), namespace)
    def no_root_allocation(): raise AssertionError('reader/custody cannot allocate a cache leaf')
    namespace['uuid4'] = no_root_allocation
    root = Path(value['fixture_root']); root.mkdir(); (root / 'TagEncyclopedia.exe').write_bytes(b'INERT IMAGE')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'Apps/collectigen.tag-encyclopedia/0.2.0-alpha.1/TagEncyclopedia/TagEncyclopedia.exe'))
    admitted = namespace['make_profile']('reader')
    assert admitted.acquisition is None and not (root / 'Data').exists()
    monkeypatch.setattr(sys, 'executable', str(root / 'TagEncyclopedia.exe'))
    assert namespace['make_profile']('custody').acquisition is None and not (root / 'Data').exists()
    for role in ('reader', 'root'):
        ast.parse(render_entry(role, bound=True))
    assert b'not_admitted' in render_entry('root', bound=False)
    assert Path('suite_core/encyclopedia/bootstrap_entry.py').read_text().find('PROFILE = None') >= 0


def _rendered_root_profile(tmp_path, monkeypatch):
    value = profile(tmp_path)
    namespace = {}; exec(compile(render_fixture_profile(value), '<synthetic-profile>', 'exec'), namespace)
    root = Path(value['fixture_root']); root.mkdir()
    (root / 'TagEncyclopedia.exe').write_bytes(b'INERT root image')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'TagEncyclopedia.exe'))
    return namespace, root, root / value['cache_relative']


def test_root_restart_preserves_prior_cache_and_uses_stock_empty_leaf(tmp_path, monkeypatch):
    from core.component_acquisition import ComponentAcquisition
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    parent.mkdir(parents=True)
    first = namespace['make_profile']('root').acquisition
    retained = first.root / 'attempt-synthetic' / 'A909.zip'
    retained.parent.mkdir(); retained.write_bytes(b'INERT retained acquired artifact')
    first._acquired['synthetic-manifest'] = retained
    before = {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
              for path in parent.rglob('*') if path.is_file()}
    setup_image = root / 'TagEncyclopedia.exe'
    final_image = tmp_path / 'final' / 'TagEncyclopedia.exe'
    final_image.parent.mkdir(); final_image.write_bytes(b'INERT replacement root image')
    # Only the cache-factory restart contract is public here. The private
    # native-runner mechanics tests retain guarded-exchange coverage. This
    # inert image replacement is fixture setup, not native exchange evidence.
    setup_image.write_bytes(final_image.read_bytes())
    assert {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in parent.rglob("*") if path.is_file()} == before
    second = namespace['make_profile']('root').acquisition
    assert type(first) is type(second) is ComponentAcquisition
    assert first.root.parent == second.root.parent == parent and first.root != second.root
    assert first.root.name.startswith('root-start-') and second.root.name.startswith('root-start-')
    assert all(sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())
    assert first._acquired == {'synthetic-manifest': retained} and retained.read_bytes() == b'INERT retained acquired artifact'
    assert second._acquired == {} and list(second.root.iterdir()) == [second._marker]
    assert json.loads(second._marker.read_bytes()) == dict(purpose='COMPONENT_ACQUISITION_CACHE', owner='encyclopedia-sealed-qualification')
    with pytest.raises(ValueError, match='empty_owned_cache_required'):
        ComponentAcquisition(cache_root=second.root, cache_owner='encyclopedia-sealed-qualification',
            opener=namespace['SealedEncyclopediaFixtureOpener'](), host_version='0.2.0-alpha.1')


@pytest.mark.parametrize('parent_kind', ['absent', 'file'])
def test_root_cache_parent_must_already_be_an_admitted_directory(tmp_path, monkeypatch, parent_kind):
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    if parent_kind == 'file':
        parent.parent.mkdir(parents=True); parent.write_bytes(b'PRESERVE foreign file')
    with pytest.raises(ValueError, match='qualification_cache_parent_required'):
        namespace['make_profile']('root')
    if parent_kind == 'file': assert parent.read_bytes() == b'PRESERVE foreign file'
    else: assert not (root / 'Data').exists()


def test_root_start_preserves_legacy_flat_acquisition_marker_and_artifact(tmp_path, monkeypatch):
    from core.component_acquisition import ComponentAcquisition
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    parent.mkdir(parents=True)
    prior = ComponentAcquisition(cache_root=parent, cache_owner='encyclopedia-sealed-qualification',
        opener=namespace['SealedEncyclopediaFixtureOpener'](), host_version='0.2.0-alpha.1')
    retained = parent / 'attempt-synthetic' / 'A909.zip'
    retained.parent.mkdir(); retained.write_bytes(b'INERT legacy acquired artifact')
    prior._acquired['synthetic-manifest'] = retained
    before = {path.relative_to(parent): path.read_bytes() for path in parent.rglob('*') if path.is_file()}
    next_instance = namespace['make_profile']('root').acquisition
    assert next_instance.root.parent == parent and next_instance._marker != prior._marker
    assert all((parent / name).read_bytes() == data for name, data in before.items())
    assert prior._acquired == {'synthetic-manifest': retained} and next_instance._acquired == {}
    prior._check(None)  # Its retained marker still passes the unchanged stock guard.


def test_root_cache_leaf_collision_refuses_adoption_without_retry_or_cleanup(tmp_path, monkeypatch):
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    parent.mkdir(parents=True)
    leaf = parent / ('root-start-' + '1' * 32); leaf.mkdir()
    retained = leaf / 'foreign.bin'; retained.write_bytes(b'PRESERVE existing bytes')
    calls = []
    def fixed_uuid(): calls.append(True); return SimpleNamespace(hex='1' * 32)
    namespace['uuid4'] = fixed_uuid
    with pytest.raises(FileExistsError): namespace['make_profile']('root')
    assert calls == [True] and list(parent.iterdir()) == [leaf] and retained.read_bytes() == b'PRESERVE existing bytes'


def test_root_cache_guard_refusal_after_creation_retains_empty_leaf_without_acquisition(tmp_path, monkeypatch):
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    parent.mkdir(parents=True)
    namespace['uuid4'] = lambda: SimpleNamespace(hex='2' * 32)
    leaf = parent / ('root-start-' + '2' * 32)
    original = namespace['guard']; visits = []
    def guarded(path):
        visits.append(path)
        if path == leaf and visits.count(leaf) == 2: raise ValueError('synthetic_post_creation_guard_hold')
        return original(path)
    namespace['guard'] = guarded
    def no_acquisition(**kwargs): raise AssertionError('guard refusal must precede stock acquisition')
    namespace['ComponentAcquisition'] = no_acquisition
    with pytest.raises(ValueError, match='synthetic_post_creation_guard_hold'): namespace['make_profile']('root')
    assert visits == [parent, leaf, leaf] and leaf.is_dir() and not list(leaf.iterdir())


def test_root_cache_creation_failure_does_not_retry_or_touch_prior_leaf(tmp_path, monkeypatch):
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    parent.mkdir(parents=True)
    retained = parent / 'root-start-prior'; retained.mkdir(); (retained / 'evidence.bin').write_bytes(b'PRESERVE')
    leaf = parent / ('root-start-' + '3' * 32)
    namespace['uuid4'] = lambda: SimpleNamespace(hex='3' * 32)
    original = Path.mkdir; calls = []
    def blocked(path, *args, **kwargs):
        if path == leaf: calls.append(kwargs); raise PermissionError('synthetic creation refusal')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', blocked)
    with pytest.raises(PermissionError, match='synthetic creation refusal'): namespace['make_profile']('root')
    assert calls == [dict(exist_ok=False)] and list(parent.iterdir()) == [retained]
    assert (retained / 'evidence.bin').read_bytes() == b'PRESERVE'


def test_generated_root_entry_allocates_stock_leaf_before_lifecycle_dispatch(tmp_path, monkeypatch):
    from types import ModuleType
    import suite_core.encyclopedia as package
    namespace, root, parent = _rendered_root_profile(tmp_path, monkeypatch)
    parent.mkdir(parents=True)
    generated = ModuleType('encyclopedia_fixture_profile'); generated.__dict__.update(namespace)
    bootstrap = ModuleType('suite_core.encyclopedia.bootstrap_entry'); bootstrap.PROFILE = None
    application = ModuleType('suite_core.encyclopedia.__main__'); observed = []
    def lifecycle():
        acquisition = bootstrap.PROFILE.acquisition
        assert acquisition.root.parent == parent and acquisition._marker.is_file()
        observed.append(acquisition.root)
        return 17  # Component dispatch sentinel, never native/normal-settlement evidence.
    application.run = lifecycle
    monkeypatch.setitem(sys.modules, 'encyclopedia_fixture_profile', generated)
    monkeypatch.setitem(sys.modules, 'suite_core.encyclopedia.bootstrap_entry', bootstrap)
    monkeypatch.setitem(sys.modules, 'suite_core.encyclopedia.__main__', application)
    monkeypatch.setattr(package, 'bootstrap_entry', bootstrap, raising=False)
    monkeypatch.setattr(sys, 'argv', [str(root / 'TagEncyclopedia.exe')])
    with pytest.raises(SystemExit) as ended:
        exec(compile(render_entry('root', bound=True), '<generated-root-entry>', 'exec'), {'__name__': '__main__'})
    assert ended.value.code == 17 and len(observed) == 1


@pytest.mark.parametrize('change', ['trust_pin', 'data', 'purpose', 'private_key', 'root_relative'])
def test_renderer_refuses_unsealed_or_private_profile_fields(tmp_path, change):
    value = profile(tmp_path)
    if change == 'trust_pin': value['trust_sha256'] = '0' * 64
    elif change == 'data': value['cache_relative'] = '../user-library'
    elif change == 'purpose': value['purpose'] = 'PRODUCTION'
    elif change == 'root_relative': value['fixture_root'] = 'relative/root'
    else:
        trust = json.loads(bytes.fromhex(value['trust_hex']))
        trust['keys'][0]['private_exponent'] = 'DO NOT ADMIT'
        value['trust_hex'] = raw(trust).hex(); value['trust_sha256'] = sha256(raw(trust)).hexdigest()
    with pytest.raises((stock.BuildHold, ValueError)):
        render_fixture_profile(value)


def test_prepare_public_stage_unbound_and_bound_are_build_only(tmp_path):
    source, args = public_inputs(tmp_path)
    unbound = prepare_stage(source, tmp_path / 'unbound', **args)
    config = json.loads((unbound / 'STAGE.json').read_bytes())
    assert config['status'] == 'SOURCE_STAGE_ONLY_NOT_FROZEN' and not config['profile_bound']
    assert not config['signed_windows_fixtures_verified'] and not config['production_activation']
    assert not (unbound / 'source/encyclopedia_fixture_profile.py').exists()
    value = profile(tmp_path); path = tmp_path / 'profile.json'; path.write_bytes(raw(value))
    bound = prepare_stage(source, tmp_path / 'bound', **args,
        fixture_profile=path, fixture_profile_sha256=sha256(path.read_bytes()).hexdigest())
    config = json.loads((bound / 'STAGE.json').read_bytes())
    assert config['profile_bound'] and len(config['generated_rows']) == 3
    assert config['build_or_app_execution'] is False and not Path(value['fixture_root']).exists()
    for name in BUILD_INPUTS: assert (bound / name).is_file()


@pytest.mark.parametrize('changed', ['source', 'tool', 'extra_subset', 'existing_fixture'])
def test_prepare_holds_drift_or_nonpublic_inputs_before_output(tmp_path, changed):
    source, args = public_inputs(tmp_path)
    if changed == 'source': (source / 'core/manager_launch_owner.py').write_bytes(b'CHANGED')
    elif changed == 'tool': (source / 'tools/encyclopedia_build/recipe_support.py').write_bytes(b'CHANGED')
    elif changed == 'extra_subset':
        subset = json.loads(Path(args['frozen_sources']).read_bytes())
        subset['source_rows'].append(row('private_scope.py', b'NOT PUBLIC'))
        Path(args['frozen_sources']).write_bytes(raw(subset)); args['frozen_sources_sha256'] = sha256(raw(subset)).hexdigest()
    else:
        value = profile(tmp_path); Path(value['fixture_root']).mkdir()
        path = tmp_path / 'profile.json'; path.write_bytes(raw(value))
        args.update(fixture_profile=path, fixture_profile_sha256=sha256(raw(value)).hexdigest())
    output = tmp_path / 'held-output'
    with pytest.raises((stock.BuildHold, ValueError)):
        prepare_stage(source, output, **args)
    assert not output.exists()


def test_fixture_opener_uses_exact_tag_urls_and_local_pins_only(tmp_path):
    value = profile(tmp_path); root = Path(value['fixture_root']); (root / '_bootstrap').mkdir(parents=True)
    payload = root / '_qualification/payload.bin'; payload.parent.mkdir(); payload.write_bytes(b'INERT ASSET')
    from suite_core.discovery.github_component_releases import REPOSITORY
    url = f'https://github.com/{REPOSITORY}/releases/download/collectigen.tag-encyclopedia%2Fv0.2.0-alpha.1/fixture.zip'
    descriptor = raw({'schema': 1, 'responses': {url: {'path': '_qualification/payload.bin',
        'size': 11, 'sha256': sha256(payload.read_bytes()).hexdigest()}}})
    (root / '_bootstrap/qualification-responses.json').write_bytes(descriptor)
    value['responses_sha256'] = sha256(descriptor).hexdigest()
    namespace = {}; exec(compile(render_fixture_profile(value), '<synthetic-profile>', 'exec'), namespace)
    opener = namespace['SealedEncyclopediaFixtureOpener']()
    with opener(Request(url)) as response: assert response.read() == b'INERT ASSET' and response.getcode() == 200
    with opener(Request(url, headers={'Range': 'bytes=6-'})) as response: assert response.read() == b'ASSET' and response.getcode() == 206
    with pytest.raises(ValueError, match='unknown_encyclopedia_fixture_url'):
        opener(Request(url.replace('tag-encyclopedia', 'suite-manager')))
    payload.write_bytes(b'CHANGED ASSET')
    with pytest.raises(ValueError): opener(Request(url))


class Graph:
    def __init__(self, missing=()): self.missing = missing
    def make_missing_toc(self): return [(name, None, 'MISSING') for name in self.missing]
    def find_node(self, name): return object()


@pytest.mark.parametrize('role', ['reader', 'root'])
@pytest.mark.parametrize('normal', [False, True])
def test_onedir_recipes_use_declared_closure_and_stock_guard(tmp_path, monkeypatch, role, normal):
    if normal:
        stage, _args, _profile = prepare_normal_stage(tmp_path)
        monkeypatch.setattr(recipe_support, 'verify_compiled_sources', lambda *args, **kwargs: {'synthetic_double': True})
    else:
        source, args = public_inputs(tmp_path)
        stage = prepare_stage(source, tmp_path / 'build-input', **args)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    calls = []
    monkeypatch.setattr(recipe_support, 'admit_current_toolchain', lambda *a: calls.append('TOOLCHAIN_DOUBLE_ONLY'))
    # The actual binary-origin guard still runs on an empty synthetic TOC.
    def analysis(scripts, **kwargs):
        assert kwargs['binaries'] == [] and kwargs['runtime_hooks'] == []
        assert 'velopack' not in kwargs['hiddenimports']
        assert 'PySide6.QtTest' in kwargs['excludes'] and 'PySide6.QtTest' not in kwargs['hiddenimports']
        local = {r['path'][:-3].replace('/', '.').removesuffix('.__init__'): str(stage / 'source' / r['path'])
            for r in config['source_rows'] + config['generated_rows'] if r['path'].endswith('.py')}
        return SimpleNamespace(pure=[(name, local.get(name), 'PYMODULE') for name in kwargs['hiddenimports']],
            binaries=[], datas=kwargs['datas'], scripts=scripts, graph=Graph())
    def exe(*args, **kwargs):
        calls.append(('exe', kwargs)); return object()
    def collect(*args, **kwargs): calls.append(('collect', kwargs)); return object()
    namespace = {'SPECPATH': str(stage), 'DISTPATH': str(tmp_path / 'inert-no-build'),
        'Analysis': analysis, 'PYZ': lambda pure: object(), 'EXE': exe, 'COLLECT': collect}
    monkeypatch.syspath_prepend(str(stage))
    monkeypatch.setitem(sys.modules, 'recipe_support', recipe_support)  # identical admitted source, toolchain observation double
    recipe = Path('tools/encyclopedia_build/TagEncyclopedia' + role.title() + '.spec').read_text()
    # Execute the same recipe with only stock freezer constructors substituted.
    exec(compile(recipe, '<synthetic-spec>', 'exec'), namespace)
    assert calls[0] == 'TOOLCHAIN_DOUBLE_ONLY'
    assert calls[1][1]['name'] == 'TagEncyclopedia' and calls[1][1]['contents_directory'] == '_internal'
    assert calls[2][1]['name'] == 'TagEncyclopedia' + role.title()
    report = json.loads((stage.parent / ('TagEncyclopedia' + role.title() + '_ANALYSIS_CLOSURE.json')).read_bytes())
    assert report['status'] == 'ANALYSIS_MODULE_BOUND_PASSED' and not report['product_apps_started']


def test_recipe_missing_local_module_remains_hold(tmp_path, monkeypatch):
    source, args = public_inputs(tmp_path); stage = prepare_stage(source, tmp_path / 'closure-held', **args)
    monkeypatch.setattr(recipe_support, 'admit_current_toolchain', lambda *a: None)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    local = {r['path'][:-3].replace('/', '.').removesuffix('.__init__'): str(stage / 'source' / r['path'])
        for r in config['source_rows'] + config['generated_rows'] if r['path'].endswith('.py')}
    def analysis(scripts, **kwargs):
        return SimpleNamespace(pure=[(name, local.get(name), 'PYMODULE') for name in kwargs['hiddenimports']],
            binaries=[], datas=[], scripts=scripts, graph=Graph(["'core.missing_fixture_dependency'"]))
    namespace = {'SPECPATH': str(stage), 'Analysis': analysis, 'PYZ': lambda _: pytest.fail('must not reach PYZ')}
    with pytest.raises(stock.BuildHold, match='analysis_closure_insufficient'):
        recipe_support.build_recipe('root', namespace)


def test_toolchain_abi_and_build_membership_are_exact(tmp_path):
    source, args = public_inputs(tmp_path)
    value = json.loads(Path(args['toolchain']).read_bytes())
    value['platform']['python_abi'] = 'cp314'
    with pytest.raises(stock.BuildHold, match='interpreter_input'):
        validate_toolchain(value)


def test_prepared_stage_pin_and_membership_refuse_unreviewed_changes(tmp_path):
    from tools.prepare_encyclopedia_qualification_stage import verify_prepared_stage
    source, args = public_inputs(tmp_path)
    stage = prepare_stage(source, tmp_path / 'preflight', **args)
    pin = sha256((stage / 'STAGE.json').read_bytes()).hexdigest()
    assert verify_prepared_stage(stage, pin)['purpose'] == PURPOSE
    (stage / 'unreviewed.py').write_bytes(b'EXTRA')
    with pytest.raises(stock.BuildHold, match='undeclared_or_missing'):
        verify_prepared_stage(stage, pin)


def test_real_toolchain_guard_refuses_synthetic_interpreter_pin(tmp_path):
    source, args = public_inputs(tmp_path)
    stage = prepare_stage(source, tmp_path / 'toolchain-held', **args)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    with pytest.raises(stock.BuildHold, match='input_digest_changed'):
        recipe_support.admit_current_toolchain(stage, config)


@pytest.mark.parametrize('failure', ['omitted_dependency', 'runtime_undeclared_dependency', 'outside_origin', 'origin_bytes'])
def test_recipe_local_dependencies_cannot_escape_subset_or_origin_guards(tmp_path, monkeypatch, failure):
    source, args = public_inputs(tmp_path)
    stage = prepare_stage(source, tmp_path / 'local-held', **args)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    local = {r['path'][:-3].replace('/', '.').removesuffix('.__init__'): str(stage / 'source' / r['path'])
        for r in config['source_rows'] + config['generated_rows'] if r['path'].endswith('.py')}
    monkeypatch.setattr(recipe_support, 'admit_current_toolchain', lambda *a: None)
    def analysis(scripts, **kwargs):
        pure = [(name, local.get(name), 'PYMODULE') for name in kwargs['hiddenimports']]
        if failure == 'outside_origin':
            outside = tmp_path / 'outside.py'; outside.write_bytes((stage / 'source/core/manager_launch_owner.py').read_bytes())
            pure = [(name, str(outside) if name == 'core.manager_launch_owner' else origin, kind)
                    for name, origin, kind in pure]
        elif failure == 'origin_bytes':
            (stage / 'source/core/manager_launch_owner.py').write_bytes(b'CHANGED AFTER ANALYSIS')
        return SimpleNamespace(pure=pure, binaries=[], datas=[], scripts=scripts,
            graph=Graph(["'release_contract'"] if failure == 'omitted_dependency' else
                ["'runtime_contract'"] if failure == 'runtime_undeclared_dependency' else []))
    namespace = {'SPECPATH': str(stage), 'Analysis': analysis,
        'PYZ': lambda _: pytest.fail('held before freeze constructors')}
    match = {'omitted_dependency': 'analysis_closure_insufficient',
        'runtime_undeclared_dependency': 'analysis_closure_insufficient',
        'outside_origin': 'outside_admitted_stage', 'origin_bytes': 'input_size_changed|input_digest_changed'}[failure]
    with pytest.raises(stock.BuildHold, match=match):
        recipe_support.build_recipe('root', namespace)
    if failure == 'runtime_undeclared_dependency':
        report = json.loads((stage.parent / 'TagEncyclopediaRoot_ANALYSIS_CLOSURE.json').read_bytes())
        assert report['missing_local_modules'] == ['runtime_contract']
        assert 'runtime_contract' in config['repository_local_top_levels']


@pytest.mark.parametrize('name', ['tools/build_manager_distribution.py',
    'core/encyclopedia_update_binding.py', 'suite_core/discovery/component_release_signature.py'])
def test_executing_validators_must_match_accepted_bytes_before_stock_validation(tmp_path, monkeypatch, name):
    source, args = public_inputs(tmp_path)
    changed = (source / name).read_bytes() + b'\n# ACCEPTED DIFFERENT VALIDATOR\n'
    (source / name).write_bytes(changed)
    inventory = json.loads((source / 'public-source.json').read_bytes())
    inventory['files'][name] = {'size_bytes': len(changed), 'sha256': sha256(changed).hexdigest()}
    (source / 'public-source.json').write_bytes(raw(inventory))
    args['public_inventory_sha256'] = sha256(raw(inventory)).hexdigest()
    if name.startswith('tools/'):
        toolchain = json.loads(Path(args['toolchain']).read_bytes())
        toolchain['build_inputs'] = [row(name, changed) if r['path'] == name else r for r in toolchain['build_inputs']]
        Path(args['toolchain']).write_bytes(raw(toolchain))
        args['toolchain_sha256'] = sha256(raw(toolchain)).hexdigest()
    monkeypatch.setattr(stock, 'require', lambda *a: pytest.fail('unadmitted stock validator called'))
    output = tmp_path / 'validator-held'
    with pytest.raises(ValueError, match='executing_validator_input_pin_changed'):
        prepare_stage(source, output, **args)
    assert not output.exists()


@pytest.mark.parametrize('registry', [None, ['core', 'core'], ['core', {}]])
def test_recipe_requires_valid_admitted_repository_namespace_registry(tmp_path, monkeypatch, registry):
    source, args = public_inputs(tmp_path)
    stage = prepare_stage(source, tmp_path / 'namespace-held', **args)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    config['repository_local_top_levels'] = registry
    (stage / 'STAGE.json').write_bytes(raw(config))  # inert malformed-input test only
    monkeypatch.setattr(recipe_support, 'admit_current_toolchain', lambda *a: None)
    with pytest.raises(stock.BuildHold, match='accepted_repository_namespace_registry_required'):
        recipe_support.build_recipe('root', {'SPECPATH': str(stage),
            'Analysis': lambda *a, **k: pytest.fail('registry must be admitted before Analysis')})


@pytest.mark.parametrize('failure', [None, 'outside_path', 'wrong_node', 'wrong_origin', 'wrong_filename', 'changed_descendant'])
def test_namespace_packages_require_exact_stage_directory_and_pinned_descendants(tmp_path, failure):
    source = tmp_path / 'source'; directory = source / 'suite_core/examples'; directory.mkdir(parents=True)
    data = b'# INERT NAMESPACE DESCENDANT\n'; child = directory / 'leaf.py'; child.write_bytes(data)
    rows = {'suite_core.examples.leaf': row('suite_core/examples/leaf.py', data)}
    class NamespacePackage:
        filename = '-'
        packagepath = [str(directory)]
    node = NamespacePackage()
    if failure == 'outside_path': node.packagepath = [str(tmp_path / 'outside')]
    elif failure == 'wrong_node': node = object()
    elif failure == 'wrong_filename': node.filename = None
    elif failure == 'changed_descendant': child.write_bytes(b'CHANGED')
    graph = SimpleNamespace(find_node=lambda name: node)
    if failure:
        with pytest.raises(stock.BuildHold):
            recipe_support.admit_local_namespace('suite_core.examples',
                None if failure == 'wrong_origin' else '-', 'PYMODULE', graph, source, rows)
    else:
        recipe_support.admit_local_namespace('suite_core.examples', '-', 'PYMODULE', graph, source, rows)


@pytest.mark.parametrize('failure', [None, 'owner_not_collected', 'symbol_absent', 'unknown_attribute', 'changed_owner'])
def test_facade_warning_requires_exact_export_and_collected_pinned_owner(tmp_path, failure):
    source = tmp_path / 'source'; (source / 'core').mkdir(parents=True)
    facade = b"_EXPORT_GROUPS = (('.events', (('EventBus', 'EventBus'),)),)\n"
    owner = b'class EventBus: pass\n' if failure != 'symbol_absent' else b'def unrelated():\n    EventBus = None\n'
    (source / 'core/__init__.py').write_bytes(facade); (source / 'core/events.py').write_bytes(owner)
    rows = {'core': row('core/__init__.py', facade), 'core.events': row('core/events.py', owner)}
    warning = 'core.UnknownAttribute' if failure == 'unknown_attribute' else 'core.EventBus'
    collected = {'core'} if failure == 'owner_not_collected' else {'core', 'core.events'}
    if failure == 'changed_owner':
        (source / 'core/events.py').write_bytes(b'CHANGED')
        with pytest.raises(stock.BuildHold):
            recipe_support.classify_pinned_facade_attributes([warning], source, rows, collected)
    else:
        missing, admitted = recipe_support.classify_pinned_facade_attributes([warning], source, rows, collected)
        assert missing == ([] if failure is None else [warning])
        assert bool(admitted) == (failure is None)


def test_compiled_only_stage_keeps_source_pins_but_recipe_datas_only_resources(tmp_path, monkeypatch):
    source, args = public_inputs(tmp_path)
    stage = prepare_stage(source, tmp_path / 'compiled-only', **args, include_python_sources=False)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    assert config['include_python_sources'] is False
    assert any(r['path'].endswith('.py') for r in config['source_rows'])
    local = {r['path'][:-3].replace('/', '.').removesuffix('.__init__'): str(stage / 'source' / r['path'])
        for r in config['source_rows'] + config['generated_rows'] if r['path'].endswith('.py')}
    def analysis(scripts, **kwargs):
        assert kwargs['datas'] and all(not path.endswith('.py') for path, dest in kwargs['datas'])
        return SimpleNamespace(pure=[(name, local.get(name), 'PYMODULE') for name in kwargs['hiddenimports']],
            binaries=[], datas=kwargs['datas'], scripts=scripts, graph=Graph())
    monkeypatch.setattr(recipe_support, 'admit_current_toolchain', lambda *a: None)
    checked = []
    monkeypatch.setattr(recipe_support, 'verify_compiled_sources',
        lambda image, source, rows, **kw: checked.append((image, rows, kw)) or {'status': 'INERT_COMPILED_READBACK_DOUBLE'})
    namespace = {'SPECPATH': str(stage), 'DISTPATH': str(tmp_path / 'dist'), 'Analysis': analysis, 'PYZ': lambda *a: object(),
        'EXE': lambda *a, **k: object(), 'COLLECT': lambda *a, **k: object()}
    recipe_support.build_recipe('reader', namespace)
    assert len(checked) == 1 and checked[0][0].name == 'TagEncyclopedia.exe'
    assert checked[0][2]['entry_source'] == 'encyclopedia_reader_entry.py'


def test_compiled_comparison_accepts_only_filename_relocation_and_rejects_nested_change():
    original = compile('def f():\n    return "PINNED"\n', 'source.py', 'exec', dont_inherit=True)
    relocated = compile('def f():\n    return "PINNED"\n', 'bundle/source.py', 'exec', dont_inherit=True)
    changed = compile('def f():\n    return "CHANGED"\n', 'source.py', 'exec', dont_inherit=True)
    assert recipe_support.compiled_code_equal(original, relocated)
    assert not recipe_support.compiled_code_equal(original, changed)


@pytest.mark.parametrize('left,right', [(-0.0, 0.0), ((-0.0,), (0.0,)),
    ((1, (False,)), (1, (0,))), (frozenset({-0.0}), frozenset({0.0})),
    ({'value': [(-0.0,)]}, {'value': [(0.0,)]}), (complex(-0.0, 0.0), complex(0.0, 0.0))])
def test_compiled_constants_reject_bit_or_nested_type_changes(left, right):
    original = compile('value=None', 'source.py', 'exec').replace(co_consts=(left,))
    changed = original.replace(co_consts=(right,))
    assert not recipe_support.compiled_code_equal(original, changed)


def test_compiled_constants_compare_exact_nan_bits_and_unordered_container_values():
    import struct
    a = struct.unpack('>d', bytes.fromhex('7ff8000000000001'))[0]
    b = struct.unpack('>d', bytes.fromhex('7ff8000000000002'))[0]
    assert recipe_support.compiled_constant_equal((a,), (a,))
    assert not recipe_support.compiled_constant_equal((a,), (b,))
    assert recipe_support.compiled_constant_equal({'a': (1, -0.0), 'b': frozenset({2, 3})},
        {'b': frozenset({3, 2}), 'a': (1, -0.0)})


def test_compiled_constants_support_python314_slice_constants_exactly():
    assert recipe_support.compiled_constant_equal(slice(1, None, None), slice(1, None, None))
    assert not recipe_support.compiled_constant_equal(slice(-0.0, None), slice(0.0, None))


@pytest.mark.parametrize('change', [None, 'startup_only_changed', 'startup_missing'])
def test_compiled_readback_checks_actual_carchive_startup_even_when_pyz_matches(tmp_path, monkeypatch, change):
    import marshal, struct
    from types import ModuleType
    source = tmp_path / 'source'; source.mkdir()
    raw = b"raise RuntimeError('EXPECTED_MARKER')\n"
    entry = 'encyclopedia_reader_entry.py'; (source / entry).write_bytes(raw)
    image = tmp_path / 'inert.exe'; pe = bytearray(256); pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 60, 128); pe[128:132] = b'PE\0\0'
    struct.pack_into('<H', pe, 132, 0x8664); struct.pack_into('<H', pe, 152, 0x20b)
    image.write_bytes(pe)  # Inert format fixture, never executed.
    expected = compile(raw, entry, 'exec', dont_inherit=True, optimize=0)
    altered = compile("print('EXPECTED_MARKER')\n", entry, 'exec', dont_inherit=True, optimize=0)
    class Container:
        toc = {} if change == 'startup_missing' else {'encyclopedia_reader_entry': (0, 0, 0, 0, 's')}
        def __init__(self, path): pass
        def open_embedded_archive(self, name): return SimpleNamespace(extract=lambda module: expected)
        def extract(self, name): return marshal.dumps(altered if change == 'startup_only_changed' else expected)
    for name in ('PyInstaller', 'PyInstaller.archive', 'PyInstaller.archive.readers'):
        module = ModuleType(name); module.__path__ = []
        if name.endswith('.readers'): module.CArchiveReader = Container
        monkeypatch.setitem(sys.modules, name, module)
    args = (image, source, [row(entry, raw)])
    if change:
        with pytest.raises(stock.BuildHold, match='carchive_startup'):
            recipe_support.verify_compiled_sources(*args, entry_source=entry)
    else:
        assert recipe_support.verify_compiled_sources(*args)['actual_carchive_startup_verified']


@pytest.mark.parametrize('change', ['extra', 'fixture', 'purpose', 'layout', 'transport', 'origin',
    'schema_bool', 'pin', 'private_trust', 'inactive', 'version', 'opener', 'response_map'])
def test_normal_profile_rejects_unreviewed_or_mixed_inputs(change):
    value = normal_profile()
    if change == 'extra': value['url'] = 'https://unreviewed.invalid'
    elif change == 'fixture': value['fixture_root'] = 'C:/fixture'
    elif change == 'purpose': value['purpose'] = 'ENCYCLOPEDIA_ISOLATED_QUALIFICATION_ONLY'
    elif change == 'layout': value['layout_id'] = 'cwd'
    elif change == 'transport': value['transport_id'] = 'arbitrary-opener'
    elif change == 'origin': value['asset_redirect_origins'].append('https://other.invalid')
    elif change == 'schema_bool': value['schema'] = True
    elif change == 'pin': value['trust_sha256'] = '0' * 64
    elif change == 'version': value['host_version'] = 'not-semver'
    elif change == 'opener': value['opener'] = 'callable'
    elif change == 'response_map': value['responses_sha256'] = 'a' * 64
    else:
        trust = json.loads(bytes.fromhex(value['trust_hex']))
        if change == 'private_trust': trust['keys'][0]['private_exponent'] = 'SECRET NOT REAL'
        else: trust['keys'][0]['status'] = 'retired'
        value['trust_hex'] = raw(trust).hex(); value['trust_sha256'] = sha256(raw(trust)).hexdigest()
    with pytest.raises((stock.BuildHold, ValueError)):
        validate_normal_profile(value)


def test_normal_cold_start_two_roots_spaces_cwd_and_restart_preserve_caches(tmp_path, monkeypatch):
    from core.component_acquisition import ComponentAcquisition
    other = tmp_path / 'unrelated cwd'; other.mkdir()
    profiles = []
    for name in ('portable one', 'portable two'):
        root = tmp_path / name; namespace = normal_namespace(root, monkeypatch)
        monkeypatch.chdir(other); monkeypatch.setenv('COLLECTIGEN_ROOT', str(other))
        first = namespace['make_profile']('root')
        assert type(first.acquisition) is ComponentAcquisition
        assert first.root == root and first.data_root == root / 'Data'
        assert first.settings_path == root / 'Data/Encyclopedia/settings.json'
        assert first.acquisition.transport.opener.policy.asset_redirect_origins == (
            'https://release-assets.githubusercontent.com',)
        retained = first.acquisition.root / 'retained.bin'; retained.write_bytes(b'KEEP')
        second = namespace['make_profile']('root')
        assert second.acquisition.root != first.acquisition.root
        assert retained.read_bytes() == b'KEEP'
        assert second.acquisition.root.parent == root / 'Data/Encyclopedia/UpdateCache'
        assert not (root / 'State').exists() and not first.settings_path.exists()
        profiles.append(first)
    assert profiles[0].root != profiles[1].root and list(other.iterdir()) == []


@pytest.mark.parametrize('failure', ['collision', 'permission', 'post_guard', 'changed_image'])
def test_normal_cache_creation_refusal_retains_evidence_without_retry(tmp_path, monkeypatch, failure):
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    parent = root / 'Data/Encyclopedia/UpdateCache'; parent.mkdir(parents=True)
    token = '1' * 32; leaf = parent / ('root-start-' + token)
    calls = []
    namespace['uuid4'] = lambda: calls.append('uuid') or SimpleNamespace(hex=token)
    original_mkdir, original_guard = Path.mkdir, namespace['guard']
    if failure == 'collision':
        leaf.mkdir(); (leaf / 'foreign.bin').write_bytes(b'KEEP COLLISION')
    if failure == 'permission':
        def denied(path, *args, **kwargs):
            if path == leaf: raise PermissionError('synthetic permission refusal')
            return original_mkdir(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'mkdir', denied)
    if failure == 'post_guard':
        def held(path):
            if path == leaf and leaf.exists(): raise ValueError('synthetic postcreation refusal')
            return original_guard(path)
        namespace['guard'] = held
    if failure == 'changed_image':
        def changed(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if path == leaf: (root / 'TagEncyclopedia.exe').write_bytes(b'CHANGED IMAGE')
            return result
        monkeypatch.setattr(Path, 'mkdir', changed)
    namespace['ComponentAcquisition'] = lambda **kwargs: pytest.fail('must hold before acquisition')
    with pytest.raises((OSError, ValueError)):
        namespace['make_profile']('root')
    assert calls == ['uuid'] and not (leaf / '.component-acquisition-owner.json').exists()
    if failure == 'collision': assert (leaf / 'foreign.bin').read_bytes() == b'KEEP COLLISION'
    elif failure == 'permission': assert not leaf.exists()
    else: assert leaf.is_dir() and list(leaf.iterdir()) == []


@pytest.mark.parametrize('failure', ['argv', 'filename', 'compiled_input', 'unc', 'mapped_remote', 'immutable'])
def test_normal_invalid_role_layout_or_inputs_hold_before_data_creation(tmp_path, monkeypatch, failure):
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    if failure == 'argv': monkeypatch.setattr(sys, 'argv', [sys.executable, '--unknown'])
    elif failure == 'filename': monkeypatch.setattr(sys, 'executable', str(root / 'Wrong.exe'))
    elif failure == 'compiled_input': namespace['INPUT']['transport_id'] = 'changed'
    elif failure == 'unc': monkeypatch.setattr(sys, 'executable', '//server/share/TagEncyclopedia.exe')
    elif failure == 'mapped_remote': namespace['_local_drive'] = lambda path: (_ for _ in ()).throw(ValueError('remote'))
    else:
        nested = root / 'Apps/collectigen.tag-encyclopedia/v1/TagEncyclopedia/TagEncyclopedia.exe'
        monkeypatch.setattr(sys, 'executable', str(nested))
    with pytest.raises(ValueError): namespace['make_profile']('root')
    assert not (root / 'Data').exists()


@pytest.mark.parametrize('marker', ['active.json', 'transaction.json', 'component-origin.json'])
def test_normal_corrupt_or_relocated_state_holds_before_cache_creation(tmp_path, monkeypatch, marker):
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    state = root / 'State/collectigen.tag-encyclopedia'; state.mkdir(parents=True)
    evidence = state / marker; evidence.write_bytes(b'CORRUPT OR OLD ABSOLUTE ORIGIN; PRESERVE')
    before = evidence.read_bytes()
    with pytest.raises((OSError, ValueError)):
        namespace['make_profile']('root')
    assert evidence.read_bytes() == before and not (root / 'Data').exists()


def prepare_normal_stage(tmp_path):
    source, args = public_inputs(tmp_path, normal=True)
    profile_path = tmp_path / 'normal.json'; profile_path.write_bytes(raw(normal_profile()))
    stage = prepare_stage(source, tmp_path / 'normal-stage', **args, normal_profile=profile_path,
        normal_profile_sha256=sha256(profile_path.read_bytes()).hexdigest(), include_python_sources=False)
    return stage, args, profile_path


def test_normal_stage_is_pinned_compiled_only_and_remains_build_only(tmp_path):
    from tools.prepare_encyclopedia_qualification_stage import verify_prepared_stage, NORMAL_PURPOSE
    stage, _args, _profile = prepare_normal_stage(tmp_path)
    config = verify_prepared_stage(stage, sha256((stage / 'STAGE.json').read_bytes()).hexdigest())
    assert config['purpose'] == NORMAL_PURPOSE and config['profile_kind'] == 'normal-portable'
    assert not config['production_activation'] and not config['build_or_app_execution']
    assert {r['path'] for r in config['generated_rows']} == {
        'encyclopedia_reader_entry.py', 'encyclopedia_root_entry.py', 'encyclopedia_compiled_profile.py'}
    assert not (stage / 'source/encyclopedia_fixture_profile.py').exists()
    assert 'PySide6.QtTest' in recipe_support.FORBIDDEN
    assert b'QtTest' not in render_normal_entry('root')


@pytest.mark.parametrize('failure', ['mixed', 'no_pin', 'source_payload', 'wrong_tool_purpose', 'wrong_host'])
def test_normal_stage_rejects_mixed_unpinned_or_wrong_inputs_before_output(tmp_path, failure):
    source, args = public_inputs(tmp_path, normal=True)
    value = normal_profile()
    if failure == 'wrong_host': value['host_version'] = '0.2.0-alpha.2'
    path = tmp_path / 'normal.json'; path.write_bytes(raw(value))
    args.update(normal_profile=path, normal_profile_sha256=sha256(path.read_bytes()).hexdigest(),
        include_python_sources=False)
    if failure == 'mixed': args.update(fixture_profile=path, fixture_profile_sha256='a' * 64)
    elif failure == 'no_pin': args['normal_profile_sha256'] = None
    elif failure == 'source_payload': args['include_python_sources'] = True
    elif failure == 'wrong_tool_purpose':
        tools = json.loads(Path(args['toolchain']).read_bytes()); tools['purpose'] = PURPOSE
        Path(args['toolchain']).write_bytes(raw(tools)); args['toolchain_sha256'] = sha256(raw(tools)).hexdigest()
    with pytest.raises((stock.BuildHold, ValueError)):
        prepare_stage(source, tmp_path / 'held-normal', **args)
    assert not (tmp_path / 'held-normal').exists()


@pytest.mark.parametrize('failure', ['swapped', 'undeclared', 'profile_kind', 'test_source'])
def test_normal_recipe_rejects_swapped_or_undeclared_inputs_before_analysis(tmp_path, monkeypatch, failure):
    stage, _args, _profile = prepare_normal_stage(tmp_path)
    config = json.loads((stage / 'STAGE.json').read_bytes())
    if failure == 'swapped': (stage / 'source/encyclopedia_compiled_profile.py').write_bytes(b'SWAPPED FIXTURE')
    elif failure == 'undeclared': (stage / 'source/extra.py').write_bytes(b'UNDECLARED')
    elif failure == 'profile_kind': config['profile_kind'] = 'fixture'
    else:
        name = 'PySide6/QtTest.py'; data = b'# REFUSED'
        path = stage / 'source' / name; path.parent.mkdir(parents=True); path.write_bytes(data)
        config['source_rows'].append(row(name, data))
    (stage / 'STAGE.json').write_bytes(raw(config))
    monkeypatch.setattr(recipe_support, 'admit_current_toolchain', lambda *args: None)
    with pytest.raises(stock.BuildHold):
        recipe_support.build_recipe('root', {'SPECPATH': str(stage),
            'Analysis': lambda *args, **kwargs: pytest.fail('refuse before analysis')})


@pytest.mark.parametrize('ancestor', ['Data', 'Data/Encyclopedia', 'Data/Encyclopedia/UpdateCache'])
def test_normal_fixed_cache_ancestors_preserve_foreign_files(tmp_path, monkeypatch, ancestor):
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    path = root / ancestor; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'KEEP FOREIGN FILE')
    with pytest.raises((OSError, ValueError)): namespace['make_profile']('root')
    assert path.read_bytes() == b'KEEP FOREIGN FILE'
    assert not any(p.name.startswith('root-start-') for p in root.rglob('*'))


def test_normal_transport_and_trust_are_exact_before_any_cache_creation(tmp_path, monkeypatch):
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    observed = []
    def held_transport(policy):
        assert policy.asset_redirect_origins == ('https://release-assets.githubusercontent.com',)
        assert policy.max_redirects == 4
        observed.append(policy)
        raise ValueError('synthetic stock transport construction refusal')
    namespace['ComponentReleaseTransport'] = held_transport
    with pytest.raises(ValueError, match='transport construction refusal'):
        namespace['make_profile']('root')
    assert len(observed) == 1 and not (root / 'Data').exists()
    namespace['INPUT']['trust_sha256'] = '0' * 64
    namespace['guard'] = lambda *args: pytest.fail('compiled input refusal precedes filesystem')
    with pytest.raises(ValueError, match='compiled_normal_input_changed'):
        namespace['make_profile']('root')
    assert len(observed) == 1


def test_normal_restart_refuses_a_saved_absolute_origin_at_new_location(tmp_path, monkeypatch):
    from suite_core.discovery.component_staging import component_origin
    from core.encyclopedia_connection import COMPONENT
    old = tmp_path / 'old portable'; old.mkdir()
    moved = tmp_path / 'moved portable'; namespace = normal_namespace(moved, monkeypatch)
    origin = component_origin(old, old / 'Data', COMPONENT)
    path = moved / 'State/collectigen.tag-encyclopedia/component-origin.json'
    path.parent.mkdir(parents=True); path.write_bytes(raw(origin))
    before = path.read_bytes()
    with pytest.raises((OSError, ValueError)):
        namespace['make_profile']('root')
    assert path.read_bytes() == before and not (moved / 'Data').exists()
