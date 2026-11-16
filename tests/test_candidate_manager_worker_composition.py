"""Synthetic build composition only; no interpreter, freezer, signing or apps."""
import ast
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.workflow_candidate_admission import CandidateWorkflowAdmission
from tools import build_manager_distribution as build
from tools.prepare_manager_production_stage import prepare_public_stage, render_workflow_candidate_configs, root_entry
from test_manager_distribution_builder import inputs, fake_freeze, row, save
from test_workflow_candidate_renderer import inputs as candidate_inputs, offer

ROOT = Path(__file__).resolve().parents[1]


def pins():
    raw = (ROOT / 'tools/prepare_manager_production_stage.py').read_bytes()
    names = {'root_entry': 'root_entry_ast_sha256',
        'render_workflow_candidate_configs': 'workflow_candidate_renderer_ast_sha256',
        'render_compiled_update_config': 'compiled_update_renderer_ast_sha256',
        'render_qualification_update_config': 'qualification_update_renderer_ast_sha256',
        'render_organizer_qualification_configs': 'organizer_qualification_renderer_ast_sha256'}
    return raw, {key: sha256(ast.dump(next(n for n in ast.parse(raw).body
        if isinstance(n, ast.FunctionDef) and n.name == name), include_attributes=False).encode()).hexdigest()
        for name,key in names.items()}


def stock_input(admission):
    # Explicit synthetic evidence for contract tests, never actual qualification.
    report = build.json_bytes({'status':'stock_cli_native_qualified', 'stock_version':'1.2.161',
        'updater_sha256':'c'*64, 'platform':{'os':'windows','architecture':'x86_64'},
        'hook_mode':'All', 'elevation_mode':'none', 'exact_full_package_apply':True,
        'prerequisites_verified':True, 'evidence_refs':[{'synthetic':True}]})
    return {'schema':1, 'trust_hex':admission.trust_bytes.hex(), 'trust_sha256':admission.trust_sha256,
        'channel':'beta', 'transport_id':'sealed-local-qualification', 'initial_prior':None,
        'updater_sha256':'c'*64, 'stock_review_hex':report.hex(),
        'stock_review_sha256':build.digest(report), 'enable_training_baseline':True}


def test_candidate_cannot_build_a_direct_root_without_current_worker_baseline(inputs, candidate_inputs):
    arguments, calls = inputs
    with pytest.raises(build.BuildHold, match='workflow_candidate_requires_deferred_signed_baseline'):
        build.build_distribution(**arguments, candidate_admission=candidate_inputs[0])
    assert calls == [] and not arguments['output'].exists()


def test_child_stock_profile_same_trust_map_and_no_prior(candidate_inputs):
    admission,sources = candidate_inputs
    # Match the actual closed Manager stage: module entry policies belong only
    # to the independently frozen Generate/Train applications.
    sources = {name: raw for name, raw in sources.items() if name.startswith('suite_core/manager/')}
    value = stock_input(admission); raw,profile = pins()
    profile['candidate_stock_review_sha256'] = value['stock_review_sha256']
    renderer = build.canonical_candidate_renderer(raw, profile['workflow_candidate_renderer_ast_sha256'], helper_pins=profile)
    kwargs = dict(component='manager', source_bytes=sources, mapping_sha256='b'*64,
        offers={c:offer(admission,c) for c in admission.document()['components']}, candidate_update_profile=value)
    generated = renderer(admission, **kwargs)
    assert generated == render_workflow_candidate_configs(admission, **kwargs)
    scope = {}; exec(generated['suite_core/manager/compiled_update_config.py'], scope)
    selected = scope['PROFILE'].check()
    assert selected.initial_prior is None and selected.trust_bytes == admission.trust_bytes
    assert selected.opener.mapping_sha256 == 'b'*64 and selected.transport_id == 'sealed-local-qualification'
    assert selected.enable_training_baseline is False  # Root alone supplies signed baseline.
    for changes in ({'trust_sha256':'0'*64}, {'trust_hex':b'{}'.hex()}, {'channel':'stable'},
                    {'transport_id':'github-release-assets-https'}, {'initial_prior':{}},
                    {'enable_training_baseline':False}):
        with pytest.raises(ValueError): renderer(admission, **{**kwargs,'candidate_update_profile':{**value,**changes}})


def test_root_stock_dependencies_are_pinned_and_module_top_level_not_executed(candidate_inputs):
    admission,_sources = candidate_inputs; raw,profile = pins()
    renderer = build.canonical_renderer(raw+b'\nraise AssertionError("module top level")\n',
        profile['root_entry_ast_sha256'], helper_pins=profile)
    assert renderer() == root_entry()
    for name in ('render_compiled_update_config','render_qualification_update_config'):
        altered = raw.replace(('def '+name+'(').encode(), ('def changed_'+name+'(').encode())
        with pytest.raises(build.BuildHold): build.canonical_renderer(altered, profile['root_entry_ast_sha256'], helper_pins=profile)
    value = stock_input(admission)
    with pytest.raises(ValueError, match='current_signed_baseline'):
        renderer('f'*64, candidate_admission=admission, compiled_update_profile=True,
            compiled_profile_input=value, qualification_test_build=True, qualification_mapping_sha256='b'*64)
    value['initial_prior'] = {'version':admission.manager_version, 'channel':'beta',
        'platform':{'os':'windows','architecture':'x86_64'}, 'files':{name:{'path':'Recovery/'+name,'size':1,'sha256':'d'*64}
        for name in ('archive','manifest','signature','trust','proof','inventory','executable')}}
    rendered = renderer('f'*64, candidate_admission=admission, compiled_update_profile=True,
        compiled_profile_input=value, qualification_test_build=True, qualification_mapping_sha256='b'*64)
    assert 'enable_training_baseline=True' in rendered and '_WORKFLOW_CANDIDATE.check_marker' in rendered
    assert 'ComponentReleaseTransport(' not in rendered and 'SealedLocalQualificationOpener' in rendered
    wrong = deepcopy(value); wrong['initial_prior']['version']='0.7.0-alpha.1'
    with pytest.raises(ValueError): renderer('f'*64,candidate_admission=admission, compiled_update_profile=True,
        compiled_profile_input=wrong, qualification_test_build=True,qualification_mapping_sha256='b'*64)


@pytest.fixture
def child_build(inputs, candidate_inputs, tmp_path, monkeypatch):
    args,calls = inputs; example,sources = candidate_inputs
    source = args['public_inventory'].parent
    for name,data in sources.items():
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    module = source/'suite_core/manager/MODULE_MANIFEST.json'
    runtime = build.decode(module.read_bytes()); runtime['runtime_files'] += list(sources)
    module.write_bytes(build.json_bytes(runtime))
    files={path.relative_to(source).as_posix():path.read_bytes() for path in source.rglob('*') if path.is_file()
           and path.name!='public-source.json'}
    save(args['public_inventory'],{'schema_version':1,'manifest_excludes_self':True,
        'files':{name:{'size_bytes':len(data),'sha256':build.digest(data)} for name,data in files.items()}})
    save(args['source_manifest'],{'schema':1,'source_rows':[row(name,data) for name,data in files.items()]})
    stage=prepare_public_stage(source,tmp_path/'candidate-stage',
        public_inventory_sha256=build.digest(args['public_inventory'].read_bytes()),
        frozen_sources=args['source_manifest'],frozen_sources_sha256=build.digest(args['source_manifest'].read_bytes()),
        manifest_sha256=build.digest(module.read_bytes()))
    args.update(source_stage=stage,stage_sha256=build.digest((stage/'STAGE.json').read_bytes()))
    document=example.document();document.update(manager_version=runtime['software_version'],
        suite_root=str(args['output']/'Suite'),data_root=str(args['output']/'Suite/Data'))
    raw=build.json_bytes(document)
    admission=CandidateWorkflowAdmission(raw,build.digest(raw),example.trust_bytes,example.trust_sha256)
    value=stock_input(admission);renderer,pinned=pins()
    profile=build.decode(args['tool_inputs'].read_bytes());profile.update(pinned,candidate_stock_review_sha256=value['stock_review_sha256'])
    save(args['tool_inputs'],profile)
    worker=tmp_path/'synthetic-worker';worker.mkdir();(worker/'python.exe').write_bytes(b'inert worker fixture')
    args.update(candidate_admission=admission,candidate_mapping_sha256='b'*64,
        candidate_offers={c:offer(admission,c) for c in document['components']},candidate_child_only=True,
        candidate_update_profile=value,training_worker_root=worker,
        training_worker_inventory_sha256=build.digest(build.json_bytes(build.tree_inventory(worker))))
    fake_freeze(monkeypatch,calls)
    return args,calls


def test_deferred_builder_freezes_child_only_and_checkpoint_detects_profile_child_drift(child_build, monkeypatch):
    args,calls=child_build
    result=build.build_distribution(**args)
    assert result['status']=='CHILD_ONLY_AWAITING_SIGNED_BASELINE'
    assert calls==['tools-inspected-synthetically','CollectiGenManager']
    output=args['output'];assert not (output/'Suite').exists()
    assert not (output/'dist/CollectiGen').exists()
    checkpoint,*_=build.load_candidate_child_checkpoint(output,result['checkpoint_sha256'])
    assert checkpoint['package_sha256']==result['manager_package_sha256']
    child=output/checkpoint['child_relative'];before=child.read_bytes()
    child.write_bytes(before+b'changed')
    with pytest.raises(ValueError):build.load_candidate_child_checkpoint(output,result['checkpoint_sha256'])
    child.write_bytes(before)
    profile=output/'candidate-inputs/child-update-profile.json';before=profile.read_bytes();profile.write_bytes(before+b' ')
    with pytest.raises(ValueError):build.load_candidate_child_checkpoint(output,result['checkpoint_sha256'])


def test_deferred_worker_and_genuine_report_pin_are_required_before_freeze(child_build):
    args,calls=child_build
    for changes in ({'training_worker_root':None,'training_worker_inventory_sha256':None},
                    {'candidate_update_profile':None}, {'candidate_admission':None}):
        with pytest.raises(ValueError):build.build_distribution(**{**args,**changes})
    value={**args['candidate_update_profile'],'stock_review_sha256':'0'*64}
    with pytest.raises(ValueError):build.build_distribution(**{**args,'candidate_update_profile':value})
    assert calls==[] and not args['output'].exists()


def test_finalizer_rejects_changed_profile_and_requires_actual_signed_baseline(child_build, monkeypatch):
    args,calls=child_build;result=build.build_distribution(**args)
    wrong={**args['candidate_update_profile'],'updater_sha256':'e'*64}
    with pytest.raises(ValueError,match='root_child_profile_changed'):
        build.finalize_candidate_distribution(args['output'],child_checkpoint_sha256=result['checkpoint_sha256'],
            candidate_update_profile=wrong,recovery_root=args['output'].parent/'absent')
    with pytest.raises(ValueError,match='current_signed_candidate_baseline'):
        build.finalize_candidate_distribution(args['output'],child_checkpoint_sha256=result['checkpoint_sha256'],
            candidate_update_profile=args['candidate_update_profile'],recovery_root=args['output'].parent/'absent')
    assert 'CollectiGen' not in calls and not (args['output']/'Suite').exists()


@pytest.mark.parametrize('kind', ['workflow', 'organizer'])
def test_signed_baseline_uses_stock_checkpoint_admission_and_exact_generated_mapping(candidate_inputs, tmp_path, monkeypatch, kind):
    import zipfile
    import suite_core.discovery.manager_runtime_detached_admission as detached
    admission,_sources=candidate_inputs
    if kind == 'organizer':
        admission=build._ManagerBaselineIdentity(admission.suite_root, admission.manager_version,
            admission.trust_bytes, admission.trust_sha256)
    child=tmp_path/'frozen-child/CollectiGenManager.exe';child.parent.mkdir();child.write_bytes(b'inert frozen child')
    child_tree=build.tree_inventory(child.parent)
    generated={detached.PACKAGE_STUB:b'inert stub','Squirrel.exe':b'inert vendor updater','sq.version':b'inert stock version'}
    archive=tmp_path/'generated.nupkg'
    with zipfile.ZipFile(archive,'w') as package:
        package.writestr('lib/app/CollectiGenManager.exe',child.read_bytes())
        for name,data in generated.items():package.writestr('lib/app/'+name,data)
    contents={'archive':archive.read_bytes(),'manifest':b'synthetic signed manifest',
        'signature':b'synthetic signature','trust':admission.trust_bytes,'proof':b'synthetic detached proof',
        'inventory':b'synthetic detached proof','executable':child.read_bytes()}
    recovery=tmp_path/'public-baseline';files={}
    for name,data in contents.items():
        relative='Recovery/Manager/candidate/'+(archive.name if name=='archive' else name)
        path=recovery/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        files[name]={'path':relative,'size':len(data),'sha256':build.digest(data)}
    value=stock_input(admission);value['updater_sha256']=build.digest(generated['Squirrel.exe'])
    value['initial_prior']={'version':admission.manager_version,'channel':'beta',
        'platform':{'os':'windows','architecture':'x86_64'},'files':files}
    admitted=[]
    def stock_admission(path, **kwargs):
        admitted.append((path,kwargs))
        assert kwargs['checkpoint']['child_tree']==child_tree
        assert kwargs['checkpoint']['child_executable_sha256']==build.digest(child.read_bytes())
        assert kwargs['manifest_bytes']==contents['manifest'] and kwargs['signature_bytes']==contents['signature']
    monkeypatch.setattr(detached,'admit_checkpointed_baseline',stock_admission)
    monkeypatch.setattr(detached,'manager_inventory_json',lambda _raw:{'files':[
        {'path':name,'size_bytes':len(data),'sha256':build.digest(data)} for name,data in generated.items()]})
    retained,mapped,evidence=build.verify_candidate_signed_baseline(admission,value,recovery,child,child_tree)
    assert len(admitted)==1 and admitted[0][0]==recovery/files['archive']['path']
    assert mapped=={'Apps/Manager/Update.exe':generated['Squirrel.exe'],
        'Apps/Manager/current/sq.version':generated['sq.version'],
        'Apps/Manager/current/'+detached.PACKAGE_STUB:generated[detached.PACKAGE_STUB]}
    assert evidence['archive_relative']==files['archive']['path'] and len(retained)==7
    assert build.tree_inventory(child.parent)==child_tree and not admission.suite_root.exists()
    wrong=deepcopy(value);wrong['initial_prior']['files']['inventory']=deepcopy(files['proof'])
    with pytest.raises(ValueError,match='distinct_candidate_recovery_files'):
        build.verify_candidate_signed_baseline(admission,wrong,recovery,child,child_tree)
    assert len(admitted)==1


def organizer_sources():
    return {name:(ROOT/name).read_bytes() for name in (
        'suite_core/manager/compiled_organizer_config.py','suite_core/manager/compiled_update_config.py')}


def organizer_stock_input():
    from suite_core.manager.compiled_organizer_config import PROFILE
    return stock_input(SimpleNamespace(trust_bytes=PROFILE.trust_bytes, trust_sha256=PROFILE.trust_sha256))


def test_organizer_renderer_preserves_production_profile_and_pins_both_stock_helpers():
    from tools.prepare_manager_production_stage import render_organizer_qualification_configs
    from suite_core.manager.compiled_organizer_config import PROFILE
    from suite_core.manager.organizer_acquisition_binding import CompiledOrganizerQualificationProfile
    sources=organizer_sources(); value=organizer_stock_input(); raw,profile=pins()
    render=build.canonical_organizer_qualification_renderer(raw+b'\nraise AssertionError("No module top level")\n',profile)
    generated=render(source_bytes=sources,mapping_sha256='b'*64,update_profile=value)
    assert generated==render_organizer_qualification_configs(source_bytes=sources,mapping_sha256='b'*64,update_profile=value)
    assert set(generated)==set(sources)
    scope={};exec(generated['suite_core/manager/compiled_organizer_config.py'],scope)
    result=scope['PROFILE']
    assert type(result) is CompiledOrganizerQualificationProfile
    assert result.production==PROFILE and result.mapping_sha256=='b'*64
    scope={};exec(generated['suite_core/manager/compiled_update_config.py'],scope)
    assert scope['PROFILE'].check().opener.mapping_sha256=='b'*64
    assert scope['PROFILE'].initial_prior is None
    assert 'CandidateWorkflowAdmission' not in b''.join(generated.values()).decode()
    for function in ('render_organizer_qualification_configs','render_compiled_update_config','render_qualification_update_config'):
        altered=raw.replace(('def '+function+'(').encode(),('def changed_'+function+'(').encode())
        with pytest.raises(build.BuildHold):build.canonical_organizer_qualification_renderer(altered,profile)
    for changes in ({'trust_sha256':'0'*64},{'trust_hex':b'{}'.hex()},{'channel':'stable'},
                    {'initial_prior':{}},{'transport_id':'github-release-assets-https'},
                    {'enable_training_baseline':False},{'stock_review_sha256':'0'*64}):
        with pytest.raises(ValueError):render(source_bytes=sources,mapping_sha256='b'*64,update_profile={**value,**changes})
    for pin in (None,'','0'*63,'not-a-pin'):
        with pytest.raises(ValueError):render(source_bytes=sources,mapping_sha256=pin,update_profile=value)
    mixed=dict(sources);mixed['suite_core/manager/compiled_update_config.py']=b'PROFILE = object()\n'
    with pytest.raises(ValueError,match='unmixed_organizer_update_default'):
        render(source_bytes=mixed,mapping_sha256='b'*64,update_profile=value)


@pytest.fixture
def organizer_child_build(inputs, tmp_path, monkeypatch):
    args,calls=inputs; source=args['public_inventory'].parent
    sources=organizer_sources()
    for name,raw in sources.items():
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    module=source/'suite_core/manager/MODULE_MANIFEST.json'
    runtime=build.decode(module.read_bytes());runtime['runtime_files']+=list(sources)
    save(module,runtime)
    files={path.relative_to(source).as_posix():path.read_bytes() for path in source.rglob('*')
           if path.is_file() and path.name!='public-source.json'}
    save(args['public_inventory'],{'schema_version':1,'manifest_excludes_self':True,
        'files':{name:{'size_bytes':len(raw),'sha256':build.digest(raw)} for name,raw in files.items()}})
    save(args['source_manifest'],{'schema':1,'source_rows':[row(name,raw) for name,raw in files.items()]})
    stage=prepare_public_stage(source,tmp_path/'organizer-stage',
        public_inventory_sha256=build.digest(args['public_inventory'].read_bytes()),
        frozen_sources=args['source_manifest'],frozen_sources_sha256=build.digest(args['source_manifest'].read_bytes()),
        manifest_sha256=build.digest(module.read_bytes()))
    value=organizer_stock_input();_raw,pinned=pins()
    profile=build.decode(args['tool_inputs'].read_bytes());profile.update(pinned,candidate_stock_review_sha256=value['stock_review_sha256'])
    save(args['tool_inputs'],profile)
    worker=tmp_path/'synthetic-organizer-worker';worker.mkdir();(worker/'python.exe').write_bytes(b'inert worker fixture')
    args.update(source_stage=stage,stage_sha256=build.digest((stage/'STAGE.json').read_bytes()),
        training_worker_root=worker,training_worker_inventory_sha256=build.digest(build.json_bytes(build.tree_inventory(worker))),
        organizer_qualification_mapping_sha256='b'*64,organizer_qualification_update_profile=value)
    fake_freeze(monkeypatch,calls)
    return args,calls


def test_organizer_child_checkpoint_does_not_enroll_workflow_candidates(organizer_child_build):
    args,calls=organizer_child_build;result=build.build_distribution(**args)
    assert result['status']=='CHILD_ONLY_AWAITING_SIGNED_BASELINE'
    assert calls==['tools-inspected-synthetically','CollectiGenManager']
    output=args['output'];sealed=build.decode((output/'SEALED_INPUTS.json').read_bytes())
    assert 'organizer_qualification' in sealed and 'workflow_candidate' not in sealed
    assert not (output/'candidate-inputs/document.json').exists() and not (output/'Suite').exists()
    checkpoint,*_=build.load_candidate_child_checkpoint(output,result['checkpoint_sha256'])
    assert checkpoint['mapping_sha256']=='b'*64
    descriptor=build.decode((output/'candidate-inputs/organizer-qualification.json').read_bytes())
    assert descriptor['kind']=='organizer-only' and descriptor['manager_version']==checkpoint['receipt']['software_version']
    child=output/checkpoint['child_relative'];original=child.read_bytes();child.write_bytes(original+b'drift')
    with pytest.raises(ValueError):build.load_candidate_child_checkpoint(output,result['checkpoint_sha256'])


def test_organizer_build_rejects_mixed_or_incomplete_inputs_before_freeze(organizer_child_build, candidate_inputs):
    args,calls=organizer_child_build
    for changes in ({'candidate_admission':candidate_inputs[0]}, {'candidate_child_only':True},
                    {'organizer_qualification_mapping_sha256':None}, {'organizer_qualification_update_profile':None},
                    {'training_worker_root':None,'training_worker_inventory_sha256':None}):
        with pytest.raises(ValueError):build.build_distribution(**{**args,**changes})
    bad={**args['organizer_qualification_update_profile'],'stock_review_sha256':'0'*64}
    with pytest.raises(ValueError,match='pinned_genuine_organizer_stock_review'):
        build.build_distribution(**{**args,'organizer_qualification_update_profile':bad})
    assert calls==[] and not args['output'].exists()


def test_organizer_finalizer_requires_profile_agreement_and_actual_signed_child(organizer_child_build):
    args,calls=organizer_child_build;result=build.build_distribution(**args)
    value=args['organizer_qualification_update_profile'];wrong={**value,'updater_sha256':'e'*64}
    for profile,reason in [(wrong,'root_child_profile_changed'),(value,'current_signed_candidate_baseline')]:
        with pytest.raises(ValueError,match=reason):
            build.finalize_candidate_distribution(args['output'],child_checkpoint_sha256=result['checkpoint_sha256'],
                candidate_update_profile=profile,recovery_root=args['output'].parent/'absent')
    assert 'CollectiGen' not in calls and not (args['output']/'Suite').exists()


def test_organizer_continuation_preserves_original_signed_baseline_identity(organizer_child_build, monkeypatch):
    args,calls=organizer_child_build;result=build.build_distribution(**args)
    value=deepcopy(args['organizer_qualification_update_profile'])
    value['initial_prior']={'version':'0.3.1-alpha.1','channel':'beta',
        'platform':{'os':'windows','architecture':'x86_64'},'files':{name:{'path':'Recovery/'+name,'size':1,'sha256':'d'*64}
        for name in ('archive','manifest','signature','trust','proof','inventory','executable')}}
    observed=[]
    def verify(identity,profile,recovery,child,inventory):
        assert type(identity) is build._ManagerBaselineIdentity
        assert identity.suite_root==args['output']/'Suite' and identity.manager_version=='0.3.1-alpha.1'
        assert profile==value and build.tree_inventory(child.parent)==inventory
        observed.append('signed-baseline-verifier')
        return {},{},None  # Synthetic composition only; real verifier is covered separately.
    def finish(**kwargs):
        assert observed==['signed-baseline-verifier'] and kwargs['candidate_admission'] is None
        assert type(kwargs['baseline_identity']) is build._ManagerBaselineIdentity
        assert kwargs['candidate_update_profile']==value and kwargs['qualification_mapping_sha256']=='b'*64
        assert 'CandidateWorkflowAdmission' not in kwargs['render'](kwargs['package_pin'],compiled_update_profile=True,
            compiled_profile_input=value,qualification_test_build=True,qualification_mapping_sha256='b'*64)
        return 'source-composition-only'
    monkeypatch.setattr(build,'verify_candidate_signed_baseline',verify)
    monkeypatch.setattr(build,'_finish_manager_distribution',finish)
    assert build.finalize_candidate_distribution(args['output'],child_checkpoint_sha256=result['checkpoint_sha256'],
        candidate_update_profile=value,recovery_root=args['output'].parent/'synthetic-baseline')=='source-composition-only'
