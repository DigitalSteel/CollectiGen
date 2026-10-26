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
    if admitted['recipe']['manager_version']=='0.6.4-alpha.1':
        assert recipe.layout_result(result) and recipe.layout_result(record)


@pytest.mark.parametrize('missing',['layout_facts','layout_captures','actions_in_view','keyboard_disclosure','draft_approval_preserved'])
def test_layout_oracle_requires_each_visible_observation(missing):
    states=[state+'-'+theme for state in ['entry','decision','saved'] for theme in ['dark','light']]
    record={'scenario':'preserve','layout_captures':dict.fromkeys(states,{}),
        'layout_facts':{state:{'keyboard_disclosure':True,'draft_approval_preserved':True,
            'actions_in_view':dict.fromkeys(['preview','confirm','apply','receipts','recover','observe','open_encyclopedia'],True)} for state in states}}
    assert recipe.layout_result(record)
    if missing.startswith('layout_'):del record[missing]
    else:del record['layout_facts'][states[0]][missing]
    assert not recipe.layout_result(record)


def test_context_recipe_requires_explicit_baseline_pins(prepared):
    repo,plan,value=prepared;value['scenario']='context';write_json(plan,value)
    with pytest.raises(ValueError,match='Exact finite'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


@pytest.mark.parametrize('violation',['native_change','overlap'])
@pytest.mark.parametrize('scenario',['context',*sorted(recipe.FEEDBACK_SCENARIOS),*sorted(recipe.KEYBOARD_SCENARIOS)])
def test_context_baseline_cannot_admit_native_drift_or_nested_output(prepared,violation,scenario):
    import shutil
    repo,plan,value=prepared;code=Path(value['source_root']);baseline=plan.parent/'baseline'
    shutil.copytree(code,baseline)
    value.update(scenario=scenario,saved_source_root=str(baseline),saved_source_inventory_sha256=recipe.digest(baseline/'public-source.json'),
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


def _observer_tick():
    """Execute the actual trusted callback branch without launching Qt/native work."""
    import ast
    import traceback
    from types import SimpleNamespace
    module=ast.parse(Path(recipe.__file__).read_text(encoding='utf-8'))
    node=next(n for n in ast.walk(module) if isinstance(n,ast.FunctionDef) and n.name=='tick')
    captures=[];retains=[];closes=[];stops=[];releases=[]
    namespace={'time':SimpleNamespace(monotonic=lambda:121),'started':0,'scenario':'context',
        'stage':['context_wait'],'record':{'errors':[]},
        'controller':SimpleNamespace(pending=True,close_held=True),
        'host':SimpleNamespace(busy=True,executor=SimpleNamespace(close_held=True)),
        'open_release':SimpleNamespace(set=lambda:releases.append(True)),
        'window':SimpleNamespace(close=lambda:closes.append(True) or True),
        'timer':SimpleNamespace(stop=lambda:stops.append(True)),
        'capture':captures.append,'retain':lambda:retains.append(True),'traceback':traceback}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(recipe.__file__),'exec'),namespace)
    return namespace,captures,retains,closes,stops,releases


def test_retained_observer_failure_does_not_repeat_deadline_or_cancel_owned_work():
    ns,captures,retains,closes,stops,releases=_observer_tick()
    ns['tick']()
    assert ns['stage']==['failure_close'] and len(ns['record']['errors'])==1
    assert captures==['failure'] and retains==[True] and releases==[True]
    assert not closes and not stops
    for _ in range(5):ns['tick']()
    assert len(ns['record']['errors'])==1 and captures==['failure'] and retains==[True]
    assert not closes and releases==[True]


@pytest.mark.parametrize('held',['pending','controller','host','executor'])
def test_failure_cleanup_waits_for_each_original_owner(held):
    ns,captures,retains,closes,stops,_=_observer_tick();ns['stage'][0]='failure_close'
    ns['controller'].pending=held=='pending';ns['controller'].close_held=held=='controller'
    ns['host'].busy=held=='host';ns['host'].executor.close_held=held=='executor'
    ns['tick']();ns['tick']()
    assert not captures and not retains and not closes and not stops and ns['record']['errors']==[]


@pytest.mark.parametrize('accepted',[True,False])
def test_failure_cleanup_requests_guarded_close_once_after_drain(accepted):
    ns,captures,retains,closes,stops,_=_observer_tick();ns['tick']()
    ns['controller'].pending=ns['controller'].close_held=ns['host'].busy=ns['host'].executor.close_held=False
    ns['window'].close=lambda:closes.append(True) or accepted
    ns['tick']();ns['tick']()
    assert closes==[True] and stops==[True] and captures==['failure'] and len(ns['record']['errors'])==1
    assert len(retains)==2 and ns['record']['failure_close_attempts']==1
    assert ns['record']['failure_cleanup_status']==('closed_after_drain' if accepted else 'held_for_supervision')


def test_close_guard_exception_is_checkpointed_without_error_capture_loop():
    ns,captures,retains,closes,stops,_=_observer_tick();ns['tick']()
    ns['controller'].pending=ns['controller'].close_held=ns['host'].busy=ns['host'].executor.close_held=False
    def uncertain_close():closes.append(True);raise RuntimeError('Synthetic Close hold')
    ns['window'].close=uncertain_close;ns['tick']();ns['tick']()
    assert closes==[True] and captures==['failure'] and len(ns['record']['errors'])==1
    assert ns['record']['failure_cleanup_status']=='held_for_supervision' and len(retains)==2


@pytest.mark.parametrize('invalid',['passed','worker_exit_code','original_process_retired','saved_catalog_unchanged','failure_close_attempts','errors'])
def test_expected_failure_cleanup_never_becomes_generic_success(invalid):
    record={'schema_version':1,'scenario':'observer_failure','passed':False,'errors':[recipe.EXPECTED_OBSERVER_FAILURE],
        'blocked_events':[],'exit_code':0,'worker_exit_code':1,'ordinary_branch':True,'original_process_retired':True,
        'close_accepted':True,'failure_close_attempts':1,'failure_cleanup_status':'closed_after_drain',
        'host_busy':False,'controller_close_held':False,'executor_close_held':False,'saved_catalog_unchanged':True,
        'saved_settings_unchanged':True,'input_bytes_preserved':True,'unknown_settings_preserved':True,
        'application_origins':{'suite_core.manager.__main__':'suite_core/manager/__main__.py'},'captures':{'entry':{},'failure':{}}}
    assert recipe.expected_failure_cleanup_result(record) and not recipe.accepted_result(record)
    del record[invalid]
    assert not recipe.expected_failure_cleanup_result(record)


def test_original_observer_failure_cleanup():
    path=os.environ.get('COLLECTIGEN_MANAGER_FAILURE_RECIPE');pin=os.environ.get('COLLECTIGEN_MANAGER_FAILURE_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned original-entry negative cleanup recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin);assert admitted['recipe']['scenario']=='observer_failure'
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==1
    result=json.loads((admitted['fixture']/'RESULT.json').read_bytes())
    assert recipe.expected_failure_cleanup_result(result) and not recipe.accepted_result(result)


def _close_record(scenario):
    admitted=scenario=='close_after_deadline'
    final={'admitted':admitted,'future_done':True,'pending':False,'controller_held':False,'executor_held':False,
        'thread_alive':False,'host_busy':False,'host_jobs':0,
        'policy':{'closed':True,'expired':False,'request_id':3 if admitted else 1,
            'decision':{'state':'closed','allow_close':True}}}
    facts={'before':{'admitted':admitted,'pending':True},
        'after_busy_close':{'cancelled':True,'policy':{'pending':True,'request_id':1}},'final':final}
    events=['visible_open_saved_once','busy_close_once'];captures=['entry','closing']
    if admitted:
        facts.update(deadline={'pending':True,'policy':{'expired':True,'request_id':1}},
            drained={'pending':False,'executor_held':False,'host_busy':False,'policy':{'expired':True}},
            after_cancel={'policy':{'expired':False,'request_id':2}})
        events+=['visible_cancel_close_once','fresh_close_once']
        captures+=['deadline-held','drained-held','cancelled-intent']
    return {'schema_version':1,'scenario':scenario,'level':'source_component','platform':'windows','exit_code':0,
        'worker_exit_code':0,'original_process_retired':True,'ordinary_branch':True,'errors':[],'blocked_events':[],
        'busy_close_accepted':False,'fresh_close_accepted':True,'close_facts':facts,'events':events,
        'captures':dict.fromkeys(captures,{}),'saved_catalog_unchanged':True,'saved_settings_unchanged':True,
        'input_bytes_preserved':True,'unknown_settings_preserved':True,
        'application_origins':{'suite_core.manager.__main__':'suite_core/manager/__main__.py'}}


@pytest.mark.parametrize('scenario',sorted(recipe.CLOSE_SCENARIOS))
@pytest.mark.parametrize('missing',['close_facts','original_process_retired','saved_catalog_unchanged','events'])
def test_close_oracle_requires_facts_saved_authority_and_retirement(scenario,missing):
    record=_close_record(scenario)
    assert recipe.close_recovery_result(record) and recipe.accepted_result(record,require_retirement=True)
    del record[missing];assert not recipe.close_recovery_result(record)


@pytest.mark.parametrize('loss',['policy_ack','thread','admission','deadline','renewed_intent','extra_action'])
def test_close_oracle_rejects_shortcuts_in_deadline_recovery(loss):
    record=_close_record('close_after_deadline');facts=record['close_facts']
    if loss=='policy_ack':facts['final']['policy']['decision']['allow_close']=False
    if loss=='thread':facts['final']['thread_alive']=True
    if loss=='admission':facts['before']['admitted']=False
    if loss=='deadline':facts['deadline']['policy']['expired']=False
    if loss=='renewed_intent':facts['final']['policy']['request_id']=1
    if loss=='extra_action':record['events'].append('fresh_close_once')
    assert not recipe.close_recovery_result(record)


@pytest.mark.parametrize('bad',[None,[],{'final':None},{'final':{'policy':None}}])
def test_close_oracle_refuses_malformed_fact_shapes(bad):
    record=_close_record('close_before_admission');record['close_facts']=bad
    assert not recipe.close_recovery_result(record)


@pytest.mark.parametrize('scenario',sorted(recipe.CLOSE_SCENARIOS))
def test_close_admission_requires_explicit_saved_source_pins(prepared,scenario):
    repo,plan,value=prepared;value['scenario']=scenario;write_json(plan,value)
    with pytest.raises(ValueError,match='Exact finite'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


@pytest.mark.parametrize('scenario',sorted(recipe.CLOSE_SCENARIOS))
def test_original_manager_close_recovery(scenario):
    prefix='COLLECTIGEN_MANAGER_CLOSE_BEFORE' if scenario=='close_before_admission' else 'COLLECTIGEN_MANAGER_CLOSE_DEADLINE'
    path=os.environ.get(prefix+'_RECIPE');pin=os.environ.get(prefix+'_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned original-entry Close recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin);assert admitted['recipe']['scenario']==scenario
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==0
    result=json.loads((admitted['fixture']/'RESULT.json').read_bytes())
    assert result['passed'] and recipe.close_recovery_result(result)


@pytest.mark.parametrize('scenario',sorted(recipe.FEEDBACK_SCENARIOS))
def test_original_manager_saved_open_feedback(scenario):
    prefix='COLLECTIGEN_MANAGER_FEEDBACK_BEFORE' if scenario=='cancel_before_admission' else 'COLLECTIGEN_MANAGER_FEEDBACK_AFTER'
    path=os.environ.get(prefix+'_RECIPE');pin=os.environ.get(prefix+'_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned saved-open feedback recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin);assert admitted['recipe']['scenario']==scenario
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==0
    result=json.loads((admitted['fixture']/'RESULT.json').read_bytes())
    assert result['passed'] and recipe.feedback_result(result)

    if 'draft_layout' in result:
        rows=result['draft_layout'];assert result['draft_disclosure_preserved']
        assert set(rows)=={n+'-'+t for n in ['initial-empty','connected-expanded','connected-collapsed','returned'] for t in ['dark','light']}
        for name,row in rows.items():
            assert row['facts']['scroll']==0 and row['facts']['context_before_draft']
            assert row['facts']['expanded'] is (not name.startswith('connected-collapsed'))
            if not name.startswith('initial-empty'):
                assert row['facts']['draft']==rows['connected-expanded-dark']['facts']['draft']


def test_keyboard_oracle_refuses_shortcuts_or_missing_review():
    import copy
    import ast
    owned={'key_role','key_send','key_reach','key_text','key_capture','key_start','key_read','keyboard_tick'}
    nodes=[n for n in ast.walk(ast.parse(Path(recipe.__file__).read_text(encoding='utf-8'))) if isinstance(n,ast.FunctionDef) and n.name in owned]
    assert {n.name for n in nodes}==owned
    forbidden={'setFocus','setCurrentRow','setCurrentIndex','click','mouseClick','ensureWidgetVisible','setValue'}
    assert not [n.func.attr for fn in nodes for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in forbidden]
    value={'schema_version':1,'scenario':'keyboard_review','level':'source_component','platform':'windows',
        'ordinary_branch':True,'application_origins':{'main':'original'},'unknown_settings_preserved':True,
        'errors':[],'blocked_events':[],'exit_code':0,'worker_exit_code':0,'original_process_retired':True,
        'keyboard_trace':[{'input':'key'}],
        'keyboard_operations':[{'kind':k,'operation_id':i+1,'future_id':i+3,'future_done':True,'admitted':True} for i,k in enumerate(['connection_open','connection_review'])],
        'events':['keyboard_open_saved_once','keyboard_review_once','keyboard_return','normal_close_once'],
        'keyboard_captures':{n+'-'+t:{} for n in ['entry','review','returned'] for t in ['dark','light']},
        'close_facts':{'final':{**{k:False for k in ['pending','controller_held','executor_held','thread_alive','host_busy']},'policy':{'closed':True,'decision':{'allow_close':True}}}}}
    for n in ['saved_catalog_unchanged','saved_settings_unchanged','input_bytes_preserved','keyboard_segment_complete','keyboard_review_verified','keyboard_draft_preserved','keyboard_existing_catalog','close_accepted']:value[n]=True
    assert recipe.keyboard_result(value)
    for input in ['mouse','setFocus','handler','scroll']:
        bad=copy.deepcopy(value);bad['keyboard_trace'][0]['input']=input;assert not recipe.keyboard_result(bad)
    for key in ['keyboard_review_verified','keyboard_segment_complete','saved_catalog_unchanged']:
        bad=copy.deepcopy(value);bad[key]=False;assert not recipe.keyboard_result(bad)
    bad=copy.deepcopy(value);bad['keyboard_operations']=bad['keyboard_operations'][:1];assert not recipe.keyboard_result(bad)
    bad=copy.deepcopy(value);bad['keyboard_trace']=[];assert not recipe.keyboard_result(bad)
    bad=copy.deepcopy(value);bad['keyboard_reading_required']=True;assert not recipe.keyboard_result(bad)


def test_original_manager_keyboard_connection_review():
    prefix='COLLECTIGEN_MANAGER_KEYBOARD'
    path=os.environ.get(prefix+'_RECIPE');pin=os.environ.get(prefix+'_RECIPE_SHA256')
    if not path or not pin:pytest.skip('Explicit pinned keyboard recipe required')
    admitted=recipe.admit(recipe.REPO,path,pin);assert admitted['recipe']['scenario']=='keyboard_review'
    assert recipe.main(['--recipe',path,'--sha256',pin,'--run'])==0
    result=json.loads((admitted['fixture']/'RESULT.json').read_bytes())
    assert result['passed'] and recipe.keyboard_result(result)
    if 'client_size' in admitted['recipe']:
        assert result['requested_client_size']==admitted['recipe']['client_size']
        assert recipe.keyboard_geometry_result(result)
    if admitted['recipe'].get('read_details'):
        assert result['keyboard_reading_required'] is True and recipe.keyboard_reading_result(result)


def test_keyboard_geometry_rejects_missing_clamped_or_inconsistent_measurement():
    import copy
    geometry={'client_size':[1280,720],'dpr':2.0,'pixel_size':[2560,1440],'available_size':[1920,1060]}
    value={'requested_client_size':[1280,720],'keyboard_captures':{n+'-'+t:{'geometry':copy.deepcopy(geometry)} for n in ['entry','review','returned'] for t in ['dark','light']}}
    assert recipe.keyboard_geometry_result(value)
    assert recipe.keyboard_geometry_result({})  # Historical K1 makes no size claim.
    for field,replacement in [('client_size',[1280,780]),('dpr',0),('dpr',True),('pixel_size',[1280,720]),('available_size',[]),('available_size',[1920,-1])]:
        bad=copy.deepcopy(value);bad['keyboard_captures']['returned-light']['geometry'][field]=replacement
        assert not recipe.keyboard_geometry_result(bad)
    bad=copy.deepcopy(value);del bad['keyboard_captures']['entry-dark']['geometry'];assert not recipe.keyboard_geometry_result(bad)
    bad=copy.deepcopy(value);bad['requested_client_size']=[1920,1080];assert not recipe.keyboard_geometry_result(bad)


@pytest.mark.parametrize('scenario,size',[('stale',[1280,720]),('keyboard_review',[1920,1080]),('keyboard_review',[1280.0,720]),('keyboard_review',None),('keyboard_review',{'width':1280,'height':720})])
def test_viewport_admission_refuses_wrong_route_or_nonfinite_size(prepared,scenario,size):
    repo,plan,value=prepared;value.update(scenario=scenario,client_size=size);write_json(plan,value)
    with pytest.raises(ValueError,match='1280x720'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()


def test_keyboard_reading_rejects_missing_focus_text_end_or_preservation():
    import copy
    row={k:True for k in ['source_present','catalog_present','token_preserved','draft_preserved','selection_preserved']}
    row.update(confirmation=False,page_down_count=1,text_sha256='a'*64)
    value={'requested_client_size':[1280,720],'keyboard_reading_required':True,'keyboard_reading':row,
        'keyboard_captures':{n+'-'+t:{'focus':'reader','reader':{'minimum':0,'maximum':40,'scroll':0 if n=='reader-top' else 40,'visible_height':140,'viewport_height':140,'text_sha256':'a'*64}} for n in ['reader-top','reader-bottom'] for t in ['dark','light']}}
    assert recipe.keyboard_reading_result(value)
    for k,v in [('token_preserved',False),('draft_preserved',False),('selection_preserved',False),('confirmation',True),('page_down_count',2)]:
        bad=copy.deepcopy(value);bad['keyboard_reading'][k]=v;assert not recipe.keyboard_reading_result(bad)
    for k,v in [('text_sha256','b'*64),('visible_height',0),('visible_height',71),('viewport_height',190),('scroll',0)]:
        bad=copy.deepcopy(value);bad['keyboard_captures']['reader-bottom-light']['reader'][k]=v;assert not recipe.keyboard_reading_result(bad)
    bad=copy.deepcopy(value);bad['keyboard_captures']['reader-top-dark']['focus']='window';assert not recipe.keyboard_reading_result(bad)
    bad=copy.deepcopy(value);del bad['keyboard_captures']['reader-bottom-dark'];assert not recipe.keyboard_reading_result(bad)


@pytest.mark.parametrize('reading,size',[(False,[1280,720]),('yes',[1280,720]),(1,[1280,720]),(True,None)])
def test_reader_admission_requires_explicit_measured_recipe(prepared,reading,size):
    repo,plan,value=prepared;value.update(scenario='keyboard_review',read_details=reading)
    if size is not None:value['client_size']=size
    write_json(plan,value)
    with pytest.raises(ValueError,match='Reading witness'):recipe.admit(repo,plan,recipe.digest(plan))
    assert not Path(value['fixture_root']).exists()
