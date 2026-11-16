"""Deterministic pending-config source/build seals; no freezer or native trial."""
import ast
import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from core.component_acquisition import ExactComponentOffer, ExactOfferAsset
from core.workflow_candidate_admission import CandidateWorkflowAdmission
from suite_core.installation import ComponentId
from tools.prepare_manager_production_stage import render_workflow_candidate_configs, root_entry
from tools.build_manager_distribution import (candidate_render_inputs, seal_candidate_inputs,
    canonical_renderer, canonical_candidate_renderer, json_bytes, verify_working, BuildHold)

ROOT = Path(__file__).resolve().parents[1]


def renderer_inputs():
    raw = (ROOT / 'tools/prepare_manager_production_stage.py').read_bytes()
    function, = [node for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef)
                 and node.name == 'render_workflow_candidate_configs']
    return {'renderer_raw': raw, 'renderer_ast_sha256': sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()}


@pytest.fixture
def inputs(tmp_path):
    modulus = base64.urlsafe_b64encode(b'\x80' + b'\0' * 255).decode().rstrip('=')
    trust = json_bytes({'schema_version': 1, 'keys': [{'key_id': 'synthetic-public-only',
        'algorithm': 'rsa-pkcs1v15-sha256', 'modulus': modulus, 'exponent': 65537}]})
    document = {'schema': 1, 'purpose': 'workflow-candidate-evaluation', 'candidate_id': 'a' * 32,
        'status': 'pending', 'suite_root': str(tmp_path / 'manager-build/Suite'),
        'data_root': str(tmp_path / 'manager-build/Suite/Data'),
        'components': {'collectigen.generate': '0.1.0-alpha.1', 'collectigen.train': '0.1.0-alpha.1'},
        'manager_version': '0.8.0-alpha.1', 'bootstrap_version': '0.1.1',
        'workspace_contract_version': 1, 'trust_sha256': sha256(trust).hexdigest()}
    raw = json_bytes(document)
    admission = CandidateWorkflowAdmission(raw, sha256(raw).hexdigest(), trust, sha256(trust).hexdigest())
    names = ('suite_core/workflow_modules/compiled_admission.py',
             'suite_core/workflow_modules/compiled_candidate.py',
             'suite_core/manager/compiled_workflow_config.py',
             'suite_core/manager/compiled_update_config.py')
    sources = {name: (ROOT / name).read_bytes() for name in names}
    return admission, sources


def offer(admission, component):
    version = admission.version_for(component)
    tag = component + '/v' + version
    def asset(name, index):
        return ExactOfferAsset(name, 100, str(index) * 64,
            'https://github.com/DigitalSteel/CollectiGen/releases/download/' + tag + '/' + name, index)
    return ExactComponentOffer(ComponentId(component), version, 'beta', 'windows', 'x86_64',
        'synthetic-public-only', admission.trust_sha256, 1, 2, tag,
        asset('release.json', 1), asset('release.sig.json', 2), asset('package.zip', 3))


def test_modules_render_before_offers_without_changing_admitted_sources(inputs):
    admission, sources = inputs
    before = dict(sources)
    for component in ('generate', 'train'):
        rendered = render_workflow_candidate_configs(admission, component=component, source_bytes=sources)
        assert rendered == render_workflow_candidate_configs(admission, component=component, source_bytes=sources)
        assert set(rendered) == {'suite_core/workflow_modules/compiled_candidate.py'}
        scope = {'__name__': 'synthetic_candidate_config'}
        exec(next(iter(rendered.values())), scope)
        profile = scope['PROFILE'].validate('collectigen.' + component)
        assert profile.admission == admission and profile.software_version == '0.1.0-alpha.1'
        assert sources == before
    with pytest.raises(ValueError, match='precedes_exact_offers'):
        render_workflow_candidate_configs(admission, component='generate', source_bytes=sources, offers={})


def test_manager_render_binds_exact_offers_map_and_empty_production(inputs):
    admission, sources = inputs
    offers = {component: offer(admission, component) for component in admission.document()['components']}
    kwargs = dict(component='manager', source_bytes=sources, mapping_sha256='b' * 64, offers=offers)
    rendered = render_workflow_candidate_configs(admission, **kwargs)
    assert rendered == render_workflow_candidate_configs(admission, **{**kwargs, 'offers': dict(reversed(list(offers.items())))})
    scope = {'__name__': 'synthetic_manager_candidate_config'}
    exec(next(iter(rendered.values())), scope)
    assert dict(scope['PROFILES']) == {}
    for component, profile in scope['CANDIDATE_PROFILES'].items():
        assert profile.admission == admission and profile.expected_offer == offers[component]
        assert profile.mapping_sha256 == 'b' * 64
    for updates in ({'mapping_sha256': None}, {'offers': {}},
                    {'offers': {key: {} for key in offers}},
                    {'offers': {**offers, 'collectigen.unknown': offers['collectigen.generate']}}):
        with pytest.raises(ValueError): render_workflow_candidate_configs(admission, **{**kwargs, **updates})
    wrong = dict(offers)
    wrong['collectigen.generate'] = replace(wrong['collectigen.generate'], trust_sha256='c' * 64)
    with pytest.raises(ValueError): render_workflow_candidate_configs(admission, **{**kwargs, 'offers': wrong})


@pytest.mark.parametrize('component,name,target,replacement', [
    ('generate', 'suite_core/workflow_modules/compiled_admission.py', 'PROFILE = None', 'PROFILE = object()'),
    ('generate', 'suite_core/workflow_modules/compiled_candidate.py', 'PROFILE = None', 'PROFILE = object()'),
    ('manager', 'suite_core/manager/compiled_workflow_config.py', 'PROFILES = MappingProxyType({})', 'PROFILES = MappingProxyType({"foreign": 1})'),
    ('manager', 'suite_core/manager/compiled_update_config.py', 'PROFILE = None', 'PROFILE = object()'),
])
def test_mixed_or_already_configured_inputs_refused(inputs, component, name, target, replacement):
    admission, sources = inputs
    sources[name] = sources[name].replace(target.encode(), replacement.encode())
    with pytest.raises(ValueError):
        render_workflow_candidate_configs(admission, component=component, source_bytes=sources,
            **({'mapping_sha256': 'b' * 64, 'offers': {c: offer(admission, c)
                for c in admission.document()['components']}} if component == 'manager' else {}))


def test_canonical_root_ast_pins_candidate_guard_and_keeps_launcher(inputs):
    admission, _sources = inputs
    raw = (ROOT / 'tools/prepare_manager_production_stage.py').read_bytes()
    function, = [n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name == 'root_entry']
    pin = sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()
    renderer = canonical_renderer(raw, pin)
    assert renderer() == root_entry()
    generated = renderer('f' * 64, candidate_admission=admission)
    assert generated == root_entry('f' * 64, candidate_admission=admission)
    assert generated.index('_WORKFLOW_CANDIDATE.check_marker') < generated.index('from suite_core.manager.launcher_main import main')
    assert 'expected_package_sha256=EXPECTED_MANAGER_PACKAGE_SHA256' in generated
    assert 'compiled_update_profile=PROFILE' not in generated
    with pytest.raises(ValueError): root_entry(candidate_admission=admission)
    with pytest.raises(ValueError): root_entry('f' * 64, candidate_admission=admission,
        compiled_update_profile=True, compiled_profile_input={})
    with pytest.raises(BuildHold): canonical_renderer(raw.replace(b'candidate_admission.validate()', b'candidate_admission.document()'), pin)


def test_build_seals_original_and_generated_identity_and_rechecks_drift(inputs, tmp_path):
    admission, sources = inputs
    output = tmp_path / 'module-build'
    generated = candidate_render_inputs(admission, component='generate', sources=sources,
        software_version='0.1.0-alpha.1', output=output, **renderer_inputs())
    assert not output.exists() and not admission.suite_root.exists()
    stage = output / 'stage'; config = {'source_rows': [], 'sdk_rows': [], 'notice_rows': []}
    for name, raw in sources.items():
        path = stage / 'source' / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        config['source_rows'].append({'path': name, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    seal = seal_candidate_inputs(output, stage, config, sources, generated, admission)
    (stage / 'STAGE.json').write_bytes(json_bytes(config))
    verify_working(stage, config, {})
    name, raw = next(iter(generated.items()))
    assert (output / 'candidate-inputs/original-source' / name).read_bytes() == sources[name]
    assert (output / 'candidate-inputs/generated-source' / name).read_bytes() == raw
    assert seal['generated_sources'][0]['original_sha256'] == sha256(sources[name]).hexdigest()
    (output / 'candidate-inputs/original-source' / name).write_bytes(b'changed')
    with pytest.raises(BuildHold): verify_working(stage, config, {})


def test_candidate_renderer_is_separately_pinned_and_never_executes_module_top_level(inputs):
    admission, sources = inputs
    pins = renderer_inputs()
    raw = pins['renderer_raw']
    renderer = canonical_candidate_renderer(raw + b'\nraise AssertionError("untrusted top level")\n',
                                             pins['renderer_ast_sha256'])
    assert renderer(admission, component='generate', source_bytes=sources) == render_workflow_candidate_configs(
        admission, component='generate', source_bytes=sources)
    with pytest.raises(BuildHold):
        canonical_candidate_renderer(raw.replace(b'typed_pending_candidate_render_required', b'changed_pending_candidate_render_required'),
                                     pins['renderer_ast_sha256'])


def test_producer_refuses_existing_suite_wrong_version_and_wrong_output_before_writes(inputs, tmp_path):
    admission, sources = inputs
    for component, version, output in (('generate', '0.2.0', tmp_path / 'unused'),
                                       ('manager', admission.manager_version, tmp_path / 'wrong')):
        with pytest.raises(ValueError): candidate_render_inputs(admission, component=component, sources=sources,
            software_version=version, output=output)
        assert not output.exists()
    admission.suite_root.mkdir(parents=True)
    retained = admission.suite_root / 'do-not-touch.txt'; retained.write_bytes(b'retained')
    with pytest.raises(ValueError): candidate_render_inputs(admission, component='generate', sources=sources,
        software_version='0.1.0-alpha.1', output=tmp_path / 'unused')
    assert retained.read_bytes() == b'retained'
