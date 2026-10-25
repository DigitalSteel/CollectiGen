"""Admission/oracle checks; original Windows entry is explicit and opt-in."""
import json
import os
from pathlib import Path
import sys

import pytest

from tools.acceptance import manager_catalog_recipe as recipe


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def prepared(tmp_path):
    repo = tmp_path
    zone = repo / '.tmp/recipe'
    code, inputs = zone / 'source', zone / 'generated'
    files = {
        'suite_core/manager/MODULE_MANIFEST.json': json.dumps({'module_id':'collectigen.suite-manager','software_version':'0.6.1-alpha.1'}),
        'suite_core/manager/version.py': 'VERSION = MODULE_VERSION = "0.6.1-alpha.1"\n',
        'suite_core/manager/__main__.py': '# Inert admission fixture; never executed.\n',
        'suite_core/manager/tag_database_controller.py': '# Inert admission fixture.\n',
        'core/knowledge_pack.py': '# Inert admission fixture.\n',
    }
    for name, text in files.items():
        path = code / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding='utf-8')
    inventory = {'schema_version':1,'export_scope':'component-source','component_ids':['collectigen.suite-manager'],
        'manifest_excludes_self':True,'files':{name:{'sha256':recipe.digest(code/name),'size_bytes':(code/name).stat().st_size} for name in files}}
    write_json(code/'public-source.json', inventory)
    data = inputs/'dist/data';data.mkdir(parents=True);(data/'TagDatabase.zip').write_bytes(b'inert synthetic admission fixture')
    receipt = inputs/'ADMISSION.json'
    write_json(receipt, {'schema_version':1,'synthetic':True,'source':str(data),'initial_files':[
        {'path':'dist/data/TagDatabase.zip','sha256':recipe.digest(data/'TagDatabase.zip')}]})
    value = {'schema_version':1,'scenario':'stale','source_root':str(code),
        'source_inventory_sha256':recipe.digest(code/'public-source.json'),'input_receipt':str(receipt),
        'input_receipt_sha256':recipe.digest(receipt),'fixture_root':str(zone/'fresh'),
        'manager_version':'0.6.1-alpha.1','interpreter_sha256':recipe.digest(Path(sys.executable))}
    plan = zone/'recipe.json';write_json(plan,value)
    return repo, plan, value


def test_admission_is_read_only_and_imports_no_application(prepared):
    repo, plan, value = prepared
    before = set(sys.modules)
    result = recipe.admit(repo, plan, recipe.digest(plan))
    assert result['fixture'] == Path(value['fixture_root']) and not result['fixture'].exists()
    added = set(sys.modules)-before
    assert not any(n == 'core.app_state' or n.startswith('suite_core.manager') for n in added)


@pytest.mark.parametrize('field,change', [
    ('schema_version',2), ('scenario','arbitrary-command'),
    ('source_inventory_sha256','0'*64), ('input_receipt_sha256','0'*64),
    ('interpreter_sha256','0'*64), ('manager_version','9.0.0'),
])
def test_invalid_recipe_refuses_before_output(prepared,field,change):
    repo, plan, value = prepared;value[field]=change;write_json(plan,value)
    with pytest.raises(ValueError):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


@pytest.mark.parametrize('target', ['occupied','source','input','protected','escape'])
def test_output_zone_is_fresh_disjoint_and_unredirected(prepared,target):
    repo,plan,value=prepared
    if target=='occupied':Path(value['fixture_root']).mkdir()
    if target=='source':value['fixture_root']=value['source_root']+'/nested'
    if target=='input':value['fixture_root']=str(Path(value['input_receipt']).parent/'nested')
    if target=='protected':value['fixture_root']=str(repo/'.tmp/Local Only/never-read')
    if target=='escape':value['fixture_root']=str(repo/'.tmp/../outside')
    write_json(plan,value)
    with pytest.raises(ValueError):recipe.admit(repo,plan,recipe.digest(plan))


def test_changed_source_extra_file_and_generated_data_hold(prepared):
    repo,plan,value=prepared
    source=Path(value['source_root'])/'core/knowledge_pack.py';before=source.read_bytes()
    source.write_bytes(b'# changed\n')
    with pytest.raises(ValueError,match='source input changed'):recipe.admit(repo,plan,recipe.digest(plan))
    source.write_bytes(before)
    extra=source.parent/'unlisted.py';extra.write_bytes(b'# unlisted\n')
    with pytest.raises(ValueError,match='inventory differs'):recipe.admit(repo,plan,recipe.digest(plan))
    extra.unlink()
    receipt=json.loads(Path(value['input_receipt']).read_bytes())
    (Path(receipt['source'])/'TagDatabase.zip').write_bytes(b'changed')
    with pytest.raises(ValueError,match='Generated source changed'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


def test_refused_recipe_never_launches_or_creates_output(prepared,monkeypatch):
    repo,plan,value=prepared;monkeypatch.setattr(recipe,'REPO',repo)
    def forbidden(*a,**kw):raise AssertionError('Refused admission must not launch')
    monkeypatch.setattr(recipe.subprocess,'Popen',forbidden)
    assert recipe.main(['--recipe',str(plan),'--sha256','0'*64,'--run'])==2
    assert not Path(value['fixture_root']).exists()


def test_protected_recipe_path_is_refused_before_open(prepared,monkeypatch):
    repo,_,_=prepared
    def forbidden(*a,**kw):raise AssertionError('Protected recipe must not be read')
    monkeypatch.setattr(recipe,'_json',forbidden)
    with pytest.raises(ValueError):recipe.admit(repo,repo/'.tmp/Local Only/receipt.json','0'*64)


@pytest.mark.parametrize('missing', ['ordinary_branch','stale_rejected','no_receipt_created',
    'close_accepted','input_bytes_preserved','connection_review_read_only'])
def test_oracle_rejects_exit_only_or_missing_criteria(missing):
    record={'schema_version':1,'scenario':'stale','level':'source_component','platform':'windows',
        'exit_code':0,'ordinary_branch':True,'connection_review_read_only':True,'stale_rejected':True,
        'no_receipt_created':True,'close_accepted':True,'unknown_settings_preserved':True,
        'input_bytes_preserved':True,'host_busy':False,'controller_close_held':False,'executor_close_held':False,
        'errors':[],'blocked_events':[],'events':['visible_apply_once'],
        'captures':dict.fromkeys(['entry','decision','stale-held'],{}),
        'application_origins':{'suite_core.manager.__main__':'suite_core/manager/__main__.py'}}
    assert recipe.accepted_result(record)
    del record[missing]
    assert not recipe.accepted_result(record)


def test_original_manager_stale_recipe():
    """A real original-entry child; absence of explicit admission is not a pass."""
    path=os.environ.get('COLLECTIGEN_MANAGER_CATALOG_RECIPE')
    pin=os.environ.get('COLLECTIGEN_MANAGER_CATALOG_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned generated Windows recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin)
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==0
    result=json.loads((admitted['fixture']/'RESULT.json').read_bytes())
    assert result['passed'] and result['original_process_retired'] and result['worker_exit_code']==0
    assert result['ordinary_branch'] and result['stale_rejected'] and result['no_receipt_created']
    assert result['input_bytes_preserved'] and result['unknown_settings_preserved']
    assert not result['errors'] and not result['blocked_events']
    assert not result['host_busy'] and not result['controller_close_held'] and not result['executor_close_held']


@pytest.fixture
def personal(tmp_path):
    witness={'tag_id':'personal_tag','category_id':'personal_category','files':{}}
    for name,stable_id in [('tags','personal_tag'),('categories','personal_category')]:
        row={'id':stable_id,'recipe_future_record':{'opaque':['preserve',23]}}
        if name=='tags':row.update(category='personal_category',properties={'note':'preserve personal native record'})
        p=tmp_path/('data/user/'+name+'.json')
        write_json(p,{name:[row],'recipe_future_root':{'opaque':[17,{'keep':True}]}})
        witness['files']['data/user/'+name+'.json']={'before':recipe.digest(p)}
    return tmp_path,witness


@pytest.mark.parametrize('loss',['empty','future_root','future_record','property','category','byte_only'])
def test_personal_oracle_detects_lost_or_changed_native_data(personal,loss):
    catalog,witness=personal
    assert recipe.valid_personal_witness(recipe._personal(catalog,witness))
    p=catalog/'data/user/tags.json';value=json.loads(p.read_bytes())
    if loss=='empty':value['tags']=[]
    if loss=='future_root':del value['recipe_future_root']
    if loss=='future_record':del value['tags'][0]['recipe_future_record']
    if loss=='property':value['tags'][0]['properties']['note']='changed'
    if loss=='category':value['tags'][0]['category']='other'
    write_json(p,value)
    if loss=='byte_only':p.write_bytes(p.read_bytes()+b'\n')
    with pytest.raises((AssertionError,KeyError,StopIteration)):recipe._personal(catalog,witness)


@pytest.mark.parametrize('scenario,missing',[
    ('preserve','personal'),('preserve','participants_verified'),('preserve','native_saved'),
    ('reopen','personal'),('reopen','saved_catalog_unchanged'),('reopen','saved_reopened'),
])
def test_preservation_acceptance_requires_real_witness(personal,scenario,missing):
    catalog,witness=personal;recipe._personal(catalog,witness)
    record={'schema_version':1,'scenario':scenario,'level':'source_component','platform':'windows',
        'exit_code':0,'ordinary_branch':True,'connection_review_read_only':True,'close_accepted':True,
        'unknown_settings_preserved':True,'input_bytes_preserved':True,'host_busy':False,
        'controller_close_held':False,'executor_close_held':False,'errors':[],'blocked_events':[],
        'application_origins':{'suite_core.manager.__main__':'suite_core/manager/__main__.py'},
        'personal':witness,'personal_preserved':True,'native_saved':True,'participants_verified':True,
        'saved_catalog_unchanged':True,'saved_reopened':True,
        'events':['visible_reload_seed','visible_apply_once'] if scenario=='preserve' else ['visible_open_saved_once'],
        'captures':dict.fromkeys(['entry','decision','saved'] if scenario=='preserve' else ['entry','reopened'],{})}
    assert recipe.accepted_result(record)
    del record[missing];assert not recipe.accepted_result(record)


def test_reopen_refuses_unqualified_prior_before_output(prepared):
    repo,plan,value=prepared;prior=plan.parent/'prior/RESULT.json'
    write_json(prior,{'passed':True,'exit_code':0})
    value.update(scenario='reopen',saved_result=str(prior),saved_result_sha256=recipe.digest(prior));write_json(plan,value)
    with pytest.raises(ValueError,match='retired preservation'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


def test_saved_read_lock_exception_cannot_authorize_participant_mutation(tmp_path):
    user=tmp_path/'data/user';user.mkdir(parents=True);lock=user/'.knowledge-write.lock'
    lock.write_bytes(b'\0');flags=os.O_RDWR|os.O_APPEND|os.O_CREAT
    assert recipe._existing_saved_lock('os.mkdir',user,(str(user),),tmp_path)
    assert recipe._existing_saved_lock('open',lock,(str(lock),'a+',flags),tmp_path)
    assert not recipe._existing_saved_lock('open',lock,(str(lock),'w',flags|os.O_TRUNC),tmp_path)
    assert not recipe._existing_saved_lock('open',user/'tags.json',('', 'a+',flags),tmp_path)
    assert not recipe._existing_saved_lock('os.remove',lock,(str(lock),),tmp_path)
    assert not recipe._existing_saved_lock('os.mkdir',user/'new',('',),tmp_path)
    lock.unlink()
    assert not recipe._existing_saved_lock('open',lock,(str(lock),'a+',flags),tmp_path)


def test_original_manager_personal_preservation_recipe():
    path=os.environ.get('COLLECTIGEN_MANAGER_PRESERVATION_RECIPE')
    pin=os.environ.get('COLLECTIGEN_MANAGER_PRESERVATION_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned generated Windows preservation recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin)
    assert admitted['recipe']['scenario']=='preserve'
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==0
    result_path=admitted['fixture']/'RESULT.json';result=json.loads(result_path.read_bytes())
    assert result['passed'] and result['original_process_retired'] and result['worker_exit_code']==0
    assert recipe.valid_personal_witness(result['personal'])
    value=dict(admitted['recipe'],scenario='reopen',fixture_root=str(admitted['fixture'].with_name(admitted['fixture'].name+'-reopen')),
        saved_result=str(result_path),saved_result_sha256=recipe.digest(result_path))
    reopen_path=Path(path).with_name(Path(path).stem+'-reopen.json')
    with reopen_path.open('x',encoding='utf-8') as handle:json.dump(value,handle,indent=2)
    reopen_pin=recipe.digest(reopen_path);reopened=recipe.admit(recipe.REPO,reopen_path,reopen_pin)
    assert recipe.main(['--recipe',str(reopen_path),'--sha256',reopen_pin,'--run'])==0
    record=json.loads((reopened['fixture']/'RESULT.json').read_bytes())
    assert record['passed'] and record['original_process_retired'] and record['worker_exit_code']==0
    assert record['saved_catalog_unchanged'] and recipe.valid_personal_witness(record['personal'])


def test_context_recipe_requires_explicit_baseline_pins(prepared):
    repo,plan,value=prepared;value['scenario']='context';write_json(plan,value)
    with pytest.raises(ValueError,match='Exact finite'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


@pytest.mark.parametrize('violation',['native_change','overlap'])
def test_context_baseline_cannot_admit_native_drift_or_nested_output(prepared,violation):
    import shutil
    repo,plan,value=prepared;code=Path(value['source_root']);baseline=plan.parent/'baseline'
    shutil.copytree(code,baseline)
    value.update(scenario='context',saved_source_root=str(baseline),saved_source_inventory_sha256=recipe.digest(baseline/'public-source.json'),
        saved_result=str(plan.parent/'unread/RESULT.json'),saved_result_sha256='0'*64)
    if violation=='overlap':value['fixture_root']=str(baseline/'nested')
    else:
        changed=code/'core/knowledge_pack.py';changed.write_bytes(b'# Changed native writer\n')
        inventory=json.loads((code/'public-source.json').read_bytes())
        inventory['files']['core/knowledge_pack.py'].update(sha256=recipe.digest(changed),size_bytes=changed.stat().st_size)
        write_json(code/'public-source.json',inventory);value['source_inventory_sha256']=recipe.digest(code/'public-source.json')
    write_json(plan,value)
    with pytest.raises(ValueError,match='separate from saved source|cannot bridge native source changes'):
        recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


@pytest.mark.parametrize('missing',['context_matches_accepted','draft_preserved','held_context_preserved',
    'saved_catalog_unchanged','saved_settings_unchanged','delayed_open_reviewed'])
def test_context_oracle_requires_each_frozen_observation(missing):
    record={'schema_version':1,'scenario':'context','level':'source_component','platform':'windows',
        'exit_code':0,'ordinary_branch':True,'connection_review_read_only':True,'close_accepted':True,
        'unknown_settings_preserved':True,'input_bytes_preserved':True,'host_busy':False,
        'controller_close_held':False,'executor_close_held':False,'errors':[],'blocked_events':[],
        'application_origins':{'suite_core.manager.__main__':'suite_core/manager/__main__.py'},
        'events':['visible_open_saved_once','visible_draft_return','visible_failed_open_once'],
        'captures':dict.fromkeys(['entry','opening','context','held'],{})}
    for key in ['context_matches_accepted','draft_preserved','held_context_preserved',
        'saved_catalog_unchanged','saved_settings_unchanged','delayed_open_reviewed']:record[key]=True
    assert recipe.accepted_result(record)
    del record[missing];assert not recipe.accepted_result(record)


def test_original_manager_connection_context_recipe():
    path=os.environ.get('COLLECTIGEN_MANAGER_CONTEXT_RECIPE');pin=os.environ.get('COLLECTIGEN_MANAGER_CONTEXT_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned original-entry context recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin);assert admitted['recipe']['scenario']=='context'
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==0
    result=json.loads((admitted['fixture']/'RESULT.json').read_bytes())
    assert result['passed'] and result['original_process_retired'] and result['worker_exit_code']==0
