"""Portable synthetic checks for canonical-only source staging; no product execution."""
import builtins,hashlib,json
from pathlib import Path
import pytest
from tools import prepare_manager_production_stage as candidate

H=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
SHARED_STAGE_SOURCES={'core/foundation_dependencies.py','core/shared_tag_snapshot.py',
    'core/effective_tag_snapshot_reader.py','core/application_profile.py','core/tag_catalog_connection.py'}
READER_STAGE_SOURCES={'core/encyclopedia_connection.py','core/encyclopedia_installation.py',
    'core/component_release_transport.py','suite_core/manager/encyclopedia_connection_ui.py',
    'suite_core/manager/encyclopedia_acquisition_ui.py',
    'suite_core/manager/compiled_encyclopedia_config.py',
    'suite_core/manager/encyclopedia_acquisition_binding.py'}

@pytest.fixture
def public_inputs(tmp_path):
    root=tmp_path/'accepted-export';root.mkdir()
    names=['core/manager_launch_owner.py','suite_core/manager/bootstrap_entry.py',
        'suite_core/manager/entry_binding.py','suite_core/manager/frozen_entry.py',
        'suite_core/manager/launcher_main.py']
    for name in names:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'# Public synthetic source, never executed.\n')
    # A declared but unselected source must not be opportunistically included.
    omitted=root/'core/unselected.py';omitted.write_bytes(b'# Outside accepted frozen subset.\n')
    manifest=root/'suite_core/manager/MODULE_MANIFEST.json'
    manifest.write_text(json.dumps({'software_version':'0.3.1-alpha.1',
        'runtime_files':names+['core/unselected.py']}),encoding='utf-8')
    inventory=root/'public-source.json'
    inventory.write_text(json.dumps({'schema_version':1,'manifest_excludes_self':True,
        'files':{name:{'size_bytes':(root/name).stat().st_size,'sha256':H(root/name)}
            for name in names+['core/unselected.py','suite_core/manager/MODULE_MANIFEST.json']}}),encoding='utf-8')
    subset=tmp_path/'frozen-subset.json'
    subset.write_text(json.dumps({'schema':1,'source_rows':[
        {'path':name,'bytes':(root/name).stat().st_size,'sha256':H(root/name)} for name in names]}),encoding='utf-8')
    return dict(source_root=root,output=tmp_path/'new-parent/public-stage',
        public_inventory_sha256=H(inventory),frozen_sources=subset,
        frozen_sources_sha256=H(subset),manifest_sha256=H(manifest))


def test_public_subset_stages_without_historical_baseline_or_full_runtime(public_inputs):
    root=public_inputs['source_root']
    # Unselected changed bytes are not read or copied: pins cover the explicit subset.
    (root/'core/unselected.py').write_bytes(b'UNSELECTED DRIFT')
    stage=candidate.prepare_public_stage(**public_inputs)
    config=json.loads((stage/'STAGE.json').read_bytes())
    names={row['path'] for row in config['source_rows']}
    assert 'core/unselected.py' not in names and len(names)==6
    assert not (stage/'source/core/unselected.py').exists()
    assert config['software_version']=='0.3.1-alpha.1'
    assert config['public_inventory_sha256']==public_inputs['public_inventory_sha256']
    assert config['frozen_sources_sha256']==public_inputs['frozen_sources_sha256']
    assert config['manifest_sha256']==public_inputs['manifest_sha256']
    for row in config['source_rows']:
        path=stage/'source'/row['path']
        assert path.stat().st_size==row['bytes'] and H(path)==row['sha256']
        if row['path']!='public_root_entry.py':
            assert path.read_bytes()==(root/row['path']).read_bytes()
    assert (stage/'source/public_root_entry.py').read_bytes()==candidate.root_entry().encode('utf-8')
    assert config['root_package_pin_bound'] is False
    proof=json.loads((stage.parent/'PRODUCTION_STAGE.json').read_bytes())
    assert proof['freeze_or_product_execution'] is False
    assert proof['historical_stage_required'] is False and proof['external_tools_or_sdk_enrolled'] is False


@pytest.mark.parametrize('drift',['inventory','subset','manifest','last-source','missing-entry',
    'duplicate','case-alias','undeclared','wrong-size','wrong-row-pin','normalization','private-import'])
def test_public_subset_preflights_all_participants_before_output(public_inputs,drift):
    kw=public_inputs;root=kw['source_root'];subset=kw['frozen_sources']
    inventory=root/'public-source.json';manifest=root/'suite_core/manager/MODULE_MANIFEST.json'
    value=json.loads(subset.read_bytes());row=value['source_rows'][-1]
    if drift=='inventory':inventory.write_bytes(inventory.read_bytes()+b' ')
    elif drift=='subset':subset.write_bytes(subset.read_bytes()+b' ')
    elif drift=='manifest':manifest.write_bytes(manifest.read_bytes()+b' ')
    elif drift=='last-source':(root/row['path']).write_bytes(b'changed last participant')
    elif drift=='missing-entry':value['source_rows'].pop()
    elif drift=='duplicate':value['source_rows'].append(dict(row))
    elif drift=='case-alias':value['source_rows'].append(dict(row,path=row['path'].upper()))
    elif drift=='undeclared':
        doc=json.loads(manifest.read_bytes());doc['runtime_files'].remove(row['path'])
        manifest.write_text(json.dumps(doc),encoding='utf-8');kw['manifest_sha256']=H(manifest)
        inv=json.loads(inventory.read_bytes());inv['files']['suite_core/manager/MODULE_MANIFEST.json']={
            'size_bytes':manifest.stat().st_size,'sha256':H(manifest)}
        inventory.write_text(json.dumps(inv),encoding='utf-8');kw['public_inventory_sha256']=H(inventory)
    elif drift=='wrong-size':row['bytes']+=1
    elif drift=='wrong-row-pin':row['sha256']='0'*64
    elif drift=='normalization':row['path']=row['path'].replace('suite_core/','suite_core//')
    elif drift=='private-import':
        path=root/row['path'];path.write_bytes(b'import PySide6.QtTest\n')
        row.update(bytes=path.stat().st_size,sha256=H(path))
        inv=json.loads(inventory.read_bytes());inv['files'][row['path']]={
            'size_bytes':row['bytes'],'sha256':row['sha256']}
        inventory.write_text(json.dumps(inv),encoding='utf-8');kw['public_inventory_sha256']=H(inventory)
    if drift not in {'inventory','subset','manifest','last-source','undeclared'}:
        subset.write_text(json.dumps(value),encoding='utf-8');kw['frozen_sources_sha256']=H(subset)
    with pytest.raises(ValueError):candidate.prepare_public_stage(**kw)
    assert not kw['output'].parent.exists()


def test_public_subset_refuses_output_inside_accepted_source(public_inputs):
    public_inputs['output']=public_inputs['source_root']/'new-stage'
    with pytest.raises(ValueError,match='new_unredirected_public_stage_required'):
        candidate.prepare_public_stage(**public_inputs)
    assert not public_inputs['output'].exists()


def test_public_subset_copy_checks_source_and_readback(public_inputs,monkeypatch):
    original=candidate.shutil.copyfile
    def corrupt(origin,target):
        original(origin,target);target.write_bytes(b'corrupted copied bytes')
    monkeypatch.setattr(candidate.shutil,'copyfile',corrupt)
    with pytest.raises(ValueError,match='public_source_changed_during_copy'):
        candidate.prepare_public_stage(**public_inputs)
    assert not (public_inputs['output']/'stage/STAGE.json').exists()


@pytest.mark.parametrize('drift',['early-source','inventory','subset','manifest'])
def test_public_subset_rechecks_snapshot_after_last_copy(public_inputs,monkeypatch,drift):
    original=candidate.shutil.copyfile
    root=public_inputs['source_root']
    first=root/'core/manager_launch_owner.py'
    count=[0]
    def change_late(origin,target):
        original(origin,target);count[0]+=1
        if count[0]==5:
            path={'early-source':first,'inventory':root/'public-source.json',
                'subset':public_inputs['frozen_sources'],
                'manifest':root/'suite_core/manager/MODULE_MANIFEST.json'}[drift]
            path.write_bytes(path.read_bytes()+b' ')
    monkeypatch.setattr(candidate.shutil,'copyfile',change_late)
    with pytest.raises(ValueError):candidate.prepare_public_stage(**public_inputs)
    assert not (public_inputs['output']/'stage/STAGE.json').exists()

@pytest.fixture
def inputs(tmp_path):
    workspace=tmp_path/'workspace';baseline=tmp_path/'stock'
    required=['core/manager_launch_owner.py','suite_core/manager/bootstrap_entry.py',
        'suite_core/manager/entry_binding.py','suite_core/manager/frozen_entry.py','suite_core/manager/launcher_main.py',
        'core/default_catalog_release.py','suite_core/manager/update_review_ui.py',
        'core/component_release_identity.py','suite_core/manager/RELEASE_PRESENTATION.json',
        'suite_core/encyclopedia/RELEASE_PRESENTATION.json','suite_core/media_organizer/RELEASE_PRESENTATION.json',
        'core/manager_stock_hook_pipe.py','core/manager_stock_hook_admission.py','core/manager_stock_hook_provider.py',
        'core/manager_native_member.py','core/manager_successor_admission.py','core/manager_update_host.py',
        'suite_core/manager/compiled_update_config.py',
        'suite_core/manager/tag_database_controller.py','suite_core/manager/tag_database_ui.py',
        'suite_core/manager/tag_entry_recovery.py',
        'suite_core/manager/model_chat_binding.py','suite_core/manager/model_chat_controller.py','suite_core/manager/model_chat_ui.py',
        'core/manager_llamacpp_provider.py','core/manager_direct_admission.py','suite_core/manager/direct_model_ui.py',
        'core/model_resource_registry.py','core/training_source_io.py',
        'suite_core/encyclopedia/browser.py','suite_core/encyclopedia/hosted_examples.py',
        'suite_core/knowledge/tag_transactions.py','suite_core/knowledge/native_tag_catalog_host.py',
        'suite_core/media_organizer/__init__.py','suite_core/media_organizer/version.py',
        *sorted(SHARED_STAGE_SOURCES | READER_STAGE_SOURCES)]
    for name in required:
        p=workspace/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('OWNED_HELPER_PRODUCTION_QUALIFIED = False\n' if name.endswith('launcher_main.py') else '# Synthetic source; no execution.\n')
    public=workspace/'tools/public-source-manifest.json';public.parent.mkdir();public.write_text(json.dumps({'files':required}))
    manifest=workspace/'suite_core/manager/MODULE_MANIFEST.json';manifest.write_text(json.dumps({'runtime_files':required}))
    metadata={'notices/NOTICE.txt':b'Synthetic notice\n','sdk_binary_origin.py':b'# Synthetic SDK metadata only\n',
        'windows_build_isolation.py':b'# Synthetic build metadata only\n',
        'CollectiGenManager.spec':b"a = Analysis([str(source/'public_child_entry.py')])\nhidden={'PySide6.QtTest','velopack'}\nexcludes=['qt_app.main_window']\n# existing-system-prerequisite\n",
        'CollectiGen.spec':b"a = Analysis([str(source/'public_root_entry.py')])\nhidden={'PySide6.QtTest','velopack'}\nexcludes=['qt_app.main_window']\n# existing-system-prerequisite\n"}
    for name,raw in metadata.items():
        p=baseline/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    sdk=baseline/'sdk/velopack/synthetic.py';sdk.parent.mkdir(parents=True);sdk.write_bytes(b'# Synthetic SDK, never imported\n')
    config={'schema':1,'software_version':'synthetic','source_rows':[{'path':n} for n in required if n not in SHARED_STAGE_SOURCES | READER_STAGE_SOURCES],
        'notice_rows':[{'path':'notices/NOTICE.txt'}],'sdk_rows':[{'path':'velopack/synthetic.py','sha256':H(sdk)}]}
    stage=baseline/'STAGE.json';stage.write_text(json.dumps(config))
    pins=tmp_path/'approved-pins.json';pins.write_text(json.dumps({'schema':1,'stage_sha256':H(stage),
        'files':{n:H(baseline/n) for n in metadata}}))
    return {'workspace':workspace,'baseline':baseline,'output':workspace/'.tmp/new-parent/source-stage',
        'allowlist_sha256':H(public),'stage_sha256':H(stage),'manifest_sha256':H(manifest),
        'metadata_pins':pins,'metadata_pins_sha256':H(pins)}

def test_fresh_workspace_creates_output_parents_after_admission(inputs):
    assert not (inputs['workspace']/'.tmp').exists()
    stage=candidate.prepare(**inputs)
    config=json.loads((stage/'STAGE.json').read_bytes())
    assert not any(r['path'].startswith('private_') for r in config['source_rows'])
    assert 'core/default_catalog_release.py' in {r['path'] for r in config['source_rows']}
    assert 'suite_core/manager/update_review_ui.py' in {r['path'] for r in config['source_rows']}
    assert "source/'suite_core/manager/frozen_entry.py'" in (stage/'CollectiGenManager.spec').read_text()
    assert 'PySide6.QtTest' not in (stage/'CollectiGen.spec').read_text()
    assert (stage/'source/public_root_entry.py').read_text()==candidate.root_entry()
    proof=json.loads((stage.parent/'PRODUCTION_STAGE.json').read_bytes())
    assert proof['approved_metadata_pins_sha256']==inputs['metadata_pins_sha256']
    assert proof['freeze_or_product_execution'] is False and proof['root_package_pin_bound'] is False

@pytest.mark.parametrize('name',['notices/NOTICE.txt','sdk_binary_origin.py','windows_build_isolation.py','CollectiGenManager.spec','CollectiGen.spec'])
def test_changed_metadata_rejected_against_approved_pins_before_any_output(inputs,name):
    p=inputs['baseline']/name;p.write_bytes(p.read_bytes()+b'# Changed after approval\n')
    with pytest.raises(ValueError,match='approved_stock_metadata_changed'):candidate.prepare(**inputs)
    assert not (inputs['workspace']/'.tmp').exists()

def test_changed_sdk_rejected_before_output(inputs):
    (inputs['baseline']/'sdk/velopack/synthetic.py').write_bytes(b'changed')
    with pytest.raises(ValueError,match='retained_stock_sdk_changed'):candidate.prepare(**inputs)
    assert not (inputs['workspace']/'.tmp').exists()

def test_changed_approved_pin_record_is_not_self_admitted(inputs):
    p=inputs['metadata_pins'];value=json.loads(p.read_bytes());value['files']['notices/NOTICE.txt']='0'*64;p.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='approved_metadata_pins_changed'):candidate.prepare(**inputs)
    assert not (inputs['workspace']/'.tmp').exists()

@pytest.mark.parametrize('name',['core/knowledge_promotion.py','qt_app/knowledge_promotion_dialog.py'])
def test_nonpublic_component_declarations_block_export_staging(inputs,name):
    p=inputs['workspace']/'suite_core/manager/MODULE_MANIFEST.json';value=json.loads(p.read_bytes());value['runtime_files'].append(name);p.write_text(json.dumps(value));inputs['manifest_sha256']=H(p)
    with pytest.raises(ValueError,match='runtime_manifest_has_nonpublic_sources'):candidate.prepare(**inputs)
    assert not (inputs['workspace']/'.tmp').exists()

def test_unbound_root_holds_before_any_product_import():
    calls=[]
    def denied(*args,**kwargs):calls.append(args);raise AssertionError('Product import forbidden')
    scope={'__name__':'__main__','__builtins__':dict(vars(builtins),__import__=denied)}
    with pytest.raises(RuntimeError,match='manager_package_pin_not_finalized'):
        exec(compile(candidate.root_entry(),'<production-root>','exec'),scope)
    assert calls==[]


def test_compiled_profile_root_is_explicit_and_public_data_cannot_inject_code(inputs):
    prior=dict(version='0.2.1-alpha.9',channel='beta',platform=dict(os='windows',architecture='x86_64'),
        files={name:dict(path='Recovery/retained-A/'+name,size=1,sha256='a'*64)
            for name in ('archive','manifest','signature','trust','proof','inventory','executable')})
    public=b'public fixture bytes';review=b'qualified fixture report, never executed'
    value=dict(schema=1,trust_hex=public.hex(),trust_sha256=hashlib.sha256(public).hexdigest(),
        channel='beta',transport_id='github-release-assets-https',initial_prior=prior,
        updater_sha256='b'*64,stock_review_hex=review.hex(),stock_review_sha256=hashlib.sha256(review).hexdigest())
    rendered=candidate.render_compiled_update_config(value)
    import ast
    ast.parse(rendered)
    assert 'QualifiedStockReview' in rendered and 'urlopen' in rendered
    assert 'initial_prior=None' in rendered
    root=candidate.root_entry('a'*64,compiled_update_profile=True,compiled_profile_input=value)
    assert 'compiled_update_profile=PROFILE' in root and 'Recovery/retained-A/' in root
    with pytest.raises(ValueError,match='exact_compiled_root_profile_input_required'):
        candidate.root_entry('a'*64,compiled_update_profile=True)
    child_only=dict(value,initial_prior=None)
    assert 'initial_prior=None' in candidate.render_compiled_update_config(child_only)
    with pytest.raises(ValueError,match='reviewed_stock_update_profile_required'):
        candidate.root_entry('a'*64,compiled_update_profile=True,compiled_profile_input=child_only)
    local=dict(value,transport_id='sealed-local-qualification')
    with pytest.raises(ValueError,match='reviewed_stock_update_profile_required'):
        candidate.render_compiled_update_config(local)
    rendered_local=candidate.render_qualification_update_config(dict(local,initial_prior=None))
    ast.parse(rendered_local)
    assert 'urlopen' not in rendered_local and 'SealedLocalQualificationOpener(None)' in rendered_local
    root_local=candidate.root_entry('a'*64,compiled_update_profile=True,compiled_profile_input=local,
        qualification_test_build=True,qualification_mapping_sha256='c'*64)
    assert 'urlopen' not in root_local and "SealedLocalQualificationOpener('"+'c'*64+"')" in root_local
    with pytest.raises(ValueError,match='sealed_test_root_profile_required'):
        candidate.root_entry('a'*64,compiled_update_profile=True,compiled_profile_input=local,qualification_test_build=True)
    value['trust_sha256']='0'*64
    with pytest.raises(ValueError,match='compiled_public_input_pin_changed'):
        candidate.render_compiled_update_config(value)

def test_canonical_manifest_version_supersedes_retained_stage_version(inputs):
    manifest=inputs['workspace']/'suite_core/manager/MODULE_MANIFEST.json'
    value=json.loads(manifest.read_bytes());value['software_version']='0.2.1-alpha.9'
    manifest.write_text(json.dumps(value));inputs['manifest_sha256']=H(manifest)
    stage=candidate.prepare(**inputs)
    assert json.loads((stage/'STAGE.json').read_bytes())['software_version']=='0.2.1-alpha.9'
    assert json.loads((stage.parent/'PRODUCTION_STAGE.json').read_bytes())['software_version']=='0.2.1-alpha.9'


def test_foundation_snapshot_and_profile_dependencies_survive_historical_stage_omissions(inputs):
    workspace=inputs['workspace']
    source={
        'core/foundation_dependencies.py':'from .effective_tag_snapshot_reader import snapshot\n',
        'core/effective_tag_snapshot_reader.py':'snapshot = "synthetic read-only snapshot"\n',
        'core/shared_tag_snapshot.py':'snapshot = "synthetic source capture"\n',
        'core/application_profile.py':'def selected_profile_root():\n    return None\n',
    }
    for name,text in source.items():
        (workspace/name).write_text(text,encoding='utf-8')
    # A full-window decoy is deliberately outside source admission. Staging may
    # not pull it in to satisfy the lazy qt_app export or shared profile binding.
    full_window=workspace/'qt_app/main_window.py';full_window.parent.mkdir()
    full_window.write_text('raise AssertionError("full application not admitted")\n')
    retained=json.loads((inputs['baseline']/'STAGE.json').read_bytes())
    assert SHARED_STAGE_SOURCES.isdisjoint(r['path'] for r in retained['source_rows'])
    stage=candidate.prepare(**inputs)
    rows={r['path']:r for r in json.loads((stage/'STAGE.json').read_bytes())['source_rows']}
    assert SHARED_STAGE_SOURCES<=rows.keys()
    for name in source:
        assert (stage/'source'/name).read_bytes()==(workspace/name).read_bytes()
        assert rows[name]['sha256']==H(workspace/name)
    assert 'qt_app/main_window.py' not in rows
    assert not (stage/'source/qt_app/main_window.py').exists()
    for name in ('CollectiGenManager','CollectiGen'):
        assert "excludes=['qt_app.main_window']" in (stage/(name+'.spec')).read_text()
    assert not any('private_' in name for name in rows)
    proof=json.loads((stage.parent/'PRODUCTION_STAGE.json').read_bytes())
    assert proof['freeze_or_product_execution'] is False


@pytest.mark.parametrize('name',sorted(SHARED_STAGE_SOURCES))
def test_shared_stage_dependency_requires_current_runtime_declaration_before_output(inputs,name):
    path=inputs['workspace']/'suite_core/manager/MODULE_MANIFEST.json'
    value=json.loads(path.read_bytes());value['runtime_files'].remove(name)
    path.write_text(json.dumps(value));inputs['manifest_sha256']=H(path)
    with pytest.raises(ValueError,match='canonical_declared_public_closure_required'):
        candidate.prepare(**inputs)
    assert not (inputs['workspace']/'.tmp').exists()


def test_registered_reader_owners_use_current_sources_missing_from_historical_stage(inputs):
    historical=json.loads((inputs['baseline']/'STAGE.json').read_bytes())
    assert not READER_STAGE_SOURCES & {r['path'] for r in historical['source_rows']}
    expected={}
    for name in READER_STAGE_SOURCES:
        p=inputs['workspace']/name
        p.write_text('# Current registered reader owner: '+name+'\n')
        expected[name]=p.read_bytes()
    stage=candidate.prepare(**inputs)
    rows={r['path']:r for r in json.loads((stage/'STAGE.json').read_bytes())['source_rows']}
    for name,raw in expected.items():
        assert (stage/'source'/name).read_bytes()==raw
        assert rows[name]['sha256']==hashlib.sha256(raw).hexdigest()
    assert (stage/'source/public_root_entry.py').read_text()==candidate.root_entry()
