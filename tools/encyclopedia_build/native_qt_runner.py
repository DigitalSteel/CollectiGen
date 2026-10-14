"""Narrow fixed-fixture runner for approved instrumented A907 -> B908 episode.

Preflight is read-only. Run is explicit, no shell/outer Job/PID reopening/kill.
An uncertain live run stays retained in this runner until original process exit.
"""
from pathlib import Path,PurePosixPath
from hashlib import sha256
from time import monotonic,sleep
import argparse,json,os,subprocess,sys

ROOT=Path('D:/CollectiGen-Dev')
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from core.manager_launch_owner import LaunchHold
BASE=ROOT/'.tmp/standalone-encyclopedia-update-20261012'
FIXTURE=BASE/'packet-65-qt-source64/fixture-root'
EVIDENCE=BASE/'native-episode70'
HANDOFF=BASE/'packet-68-qt-final64/FINAL_HANDOFF69.json'
HANDOFF_SHA='709ef209906962d4083305904e19cfddff95c8fdfcc89b02a413c03ebf239053'
CASE_SELECTION=(67,68,'64')
VERSION_A='0.2.0-alpha.907';VERSION_B='0.2.0-alpha.908'
SENTINEL=b'{"synthetic_user_correction":"retain","future_schema":{"lineage":[1,2,3]}}\n'
SETTINGS_MARKER={'qualification_episode':'source64-A907-B908','future_qualification_field':{'preserve':True}}
CACHE_RELATIVE='Data/Encyclopedia/UpdateCache/qualification'
_RETAINED_RUNS=[]
# Historic roots predate the controlled diagnostic protocol and remain unqualified.
# Future enrollment requires independently reviewed immutable entry/image/profile/readback pins.
_PREOWNER_FAILURE_CASES={}
SOURCE113_RECEIPT=BASE/'packet-119-unsigned-readers-source113/SIGNED_RECEIPT125.json'
SOURCE113_RECEIPT_SHA='6413e849b3d831dae675ff53f5f3406701ba2cddf6adc734f1d6122748d0a2e7'

def configure_source113_fresh():
    global FIXTURE,EVIDENCE,HANDOFF,HANDOFF_SHA,CASE_SELECTION,VERSION_A,VERSION_B,SETTINGS_MARKER
    FIXTURE=BASE/'packet-118-source113-freshpath/fixture-root'
    EVIDENCE=BASE/'native-episode132-source113fresh'
    HANDOFF=BASE/'ROOT_BUILD_HANDOFF129.json'
    HANDOFF_SHA='6db5f7af3eee011f9e054e43118c15d0dfe86b2318aa6470d9dee50ce0f493c1'
    CASE_SELECTION=(126,127,'113fresh')
    VERSION_A='0.2.0-alpha.909';VERSION_B='0.2.0-alpha.910'
    SETTINGS_MARKER={'qualification_episode':'source113fresh-A909-B910','future_qualification_field':{'preserve':True}}

class RunnerHold(ValueError):pass
class FailedPreOwnerStartup(RunnerHold):pass

KNOWN_PREOWNER_STDERR=('Traceback (most recent call last):\n'
    '  File "encyclopedia_root_entry.py", line 10, in <module>\n'
    '  File "encyclopedia_fixture_profile.py", line 81, in make_profile\n'
    '  File "core\\component_acquisition.py", line 166, in __init__\n'
    '  File "suite_core\\discovery\\component_staging.py", line 35, in require\n'
    'suite_core.discovery.component_staging.StagingHold: empty_owned_cache_required\n')

def require(value,reason):
    if not value:raise RunnerHold(reason)

def pin(raw):return sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,indent=2,sort_keys=True)+'\n').encode('utf-8')

def checked(path,expected=None,size=None):
    from suite_core.discovery.component_staging import guard
    path=Path(path).absolute();guard(path)
    raw=path.read_bytes()
    require(size is None or len(raw)==size,'artifact_size_changed')
    require(expected is None or pin(raw)==expected,'artifact_pin_changed')
    return raw

def fresh(path,raw):
    from tools.build_manager_distribution import write_new,unredirected
    unredirected(path);write_new(path,raw)

def safe_relative(name):
    p=PurePosixPath(name)
    require(type(name) is str and not p.is_absolute() and '..' not in p.parts
        and ':' not in name and '\\' not in name,'safe_relative_artifact_required')
    require(p.parts and p.parts[0] not in ('Data','Apps','State'),'bootstrap_must_exclude_mutable_zones')
    return name

def artifact_case(number,kind,source_revision='64'):
    require((number,kind,source_revision) in ((67,'setup','64'),(68,'final','64'),
        (84,'setup','81'),(85,'final','81'),(96,'setup','95'),(97,'final','95'),
        (126,'setup','113fresh'),(127,'final','113fresh')),'reviewed_artifact_case_required')
    home=BASE/('packet-'+str(number)+'-qt-'+kind+source_revision)
    payload=home/'root-dist/TagEncyclopediaRoot'
    inventory=json.loads(checked(home/('ROOT_PAYLOAD_INVENTORY'+str(number)+'.json')))
    inputs=json.loads(checked(home/('ROOT_INPUTS'+str(number)+'.json')))
    rows={safe_relative(r['path']):dict(path=payload/r['path'],size=r['bytes'],sha256=r['sha256'])
        for r in inventory['files']}
    layout=home/'sealed-response-layout'
    map_name='_bootstrap/qualification-responses.json'
    mapping=checked(layout/map_name,inputs['response_map_sha256'])
    document=json.loads(mapping)
    require(set(document)=={'schema','responses'} and document['schema']==1,'closed_response_map_required')
    require(len(document['responses'])==inputs['response_count'],'exact_response_count_required')
    for spec in document['responses'].values():
        name=safe_relative(spec['path'])
        require(name not in rows,'response_payload_collision')
        rows[name]=dict(path=layout/name,size=spec['size'],sha256=spec['sha256'])
    rows[map_name]=dict(path=layout/map_name,size=len(mapping),sha256=pin(mapping))
    for row in rows.values():checked(row['path'],row['sha256'],row['size'])
    return dict(home=home,rows=rows,inputs=inputs)

def signed_receipt():
    if CASE_SELECTION!=(126,127,'113fresh'):
        return json.loads(checked(ROOT/'.tmp/encyclopedia-root-replacement-20261012/signing-readiness66/SIGNED_RECEIPT67.json'))
    # Receipt125 is separately pinned: it is deliberately absent from handoff129's rows.
    receipt=json.loads(checked(SOURCE113_RECEIPT,SOURCE113_RECEIPT_SHA))
    readers=receipt['readers']
    fields={'frozen_descriptor_sha256','image_sha256','module_sha256','package_path','package_sha256',
        'package_size_bytes','portable_manifest_sha256','profile_sha256','qt_stage_sha256','reader',
        'release_manifest_path','release_manifest_sha256','release_manifest_size_bytes','signature_output',
        'signature_sha256','signature_size_bytes','source_inventory_sha256',
        'stock_signature_and_archive_publicly_verified','version'}
    require(len(readers)==2 and [r.get('reader') for r in readers]==['A','B'],'exact_signed_readers_required')
    adapted=[]
    for row,label,version in zip(readers,('A','B'),('0.2.0-alpha.909','0.2.0-alpha.910')):
        require(set(row)==fields and row['version']==version
            and row['stock_signature_and_archive_publicly_verified'] is True,'closed_signed_reader_required')
        home=SOURCE113_RECEIPT.parent/label/'unsigned-package'
        require(Path(row['release_manifest_path'])==home/'release-manifest.input.json'
            and Path(row['signature_output'])==home/'collectigen-release.json.sig'
            and Path(row['package_path'])==home/('TagEncyclopedia-'+version+'-windows-x64-qualification.zip'),
            'fixed_signed_reader_paths_required')
        adapted.append(dict(reader=label,version=version,
            manifest=dict(path=row['release_manifest_path'],sha256=row['release_manifest_sha256'],bytes=row['release_manifest_size_bytes']),
            archive=dict(path=row['package_path'],sha256=row['package_sha256'],bytes=row['package_size_bytes']),
            signature_output=row['signature_output'],signature_sha256=row['signature_sha256'],signature_bytes=row['signature_size_bytes']))
    return dict(receipt,readers=adapted)

def bind_source113(record,setup,final,receipt):
    require(record['case_selection_proposal']==[126,127,'113fresh']
        and Path(record['fixture_root'])==FIXTURE
        and Path(record['signed_receipt_path'])==SOURCE113_RECEIPT
        and record['signed_receipt_sha256']==SOURCE113_RECEIPT_SHA,'source113_handoff_binding_required')
    for case,versions,count in ((setup,[VERSION_A],6),(final,[VERSION_A,VERSION_B],9)):
        inputs=case['inputs']
        require(inputs['reader_versions']==versions and inputs['response_count']==count
            and inputs['signed_receipt_sha256']==SOURCE113_RECEIPT_SHA,'source113_response_binding_required')
        stage=json.loads(checked(case['home']/'qt-stage/STAGE.json',inputs['stage_sha256']))
        qt=stage['qualification_qt']
        require(qt['scenario']=='baseline-ordinary-close-retry' and qt['a_version']==VERSION_A
            and qt['b_version']==VERSION_B,'source113_stage_binding_required')
        for row in receipt['readers'][:len(versions)]:
            assets=((row['manifest'],'collectigen-release.json'),(row['archive'],Path(row['archive']['path']).name),
                (dict(sha256=row['signature_sha256'],bytes=row['signature_bytes']),'collectigen-release.json.sig'))
            for asset,name in assets:
                mapped=case['rows']['_qualification/packages/'+row['reader']+'/'+name]
                require(mapped['sha256']==asset['sha256'] and mapped['size']==asset['bytes'],
                    'signed_response_asset_binding_required')

def preflight():
    record=json.loads(checked(HANDOFF,HANDOFF_SHA))
    for row in record['rows']:checked(row['path'],row['sha256'],row['bytes'])
    require(CASE_SELECTION in ((67,68,'64'),(84,85,'81'),(96,97,'95'),(126,127,'113fresh')),'reviewed_case_selection_required')
    setup_number,final_number,revision=CASE_SELECTION
    setup=artifact_case(setup_number,'setup',revision);final=artifact_case(final_number,'final',revision)
    require(set(setup['rows'])<=set(final['rows']),'unexpected_obsolete_bootstrap_file')
    profile=json.loads(checked(setup['home']/('ROOT_PROFILE'+str(setup_number)+'.json')))
    final_profile=json.loads(checked(final['home']/('ROOT_PROFILE'+str(final_number)+'.json')))
    require(Path(profile['fixture_root'])==FIXTURE and Path(final_profile['fixture_root'])==FIXTURE,'fixed_fixture_required')
    require(profile['cache_relative']==final_profile['cache_relative']==CACHE_RELATIVE,'fixed_owned_cache_required')
    require([k for k in profile if profile[k]!=final_profile[k]]==['responses_sha256'],'map_only_profile_difference_required')
    from suite_core.discovery.component_release_signature import verify_component_release,verify_component_artifact
    from suite_core.installation import ComponentId
    receipt=signed_receipt();trust=bytes.fromhex(profile['trust_hex'])
    if CASE_SELECTION==(126,127,'113fresh'):bind_source113(record,setup,final,receipt)
    require(pin(trust)==profile['trust_sha256']==receipt['public_trust_sha256'],'public_trust_changed')
    for r in receipt['readers']:
        message=checked(r['manifest']['path'],r['manifest']['sha256'],r['manifest']['bytes'])
        signature=checked(r['signature_output'],r['signature_sha256'],r['signature_bytes'])
        release=verify_component_release(message,signature,trust,expected_component=ComponentId('collectigen.tag-encyclopedia'),
            expected_version=r['version'],expected_channel='beta',expected_os='windows',expected_architecture='x86_64')
        if CASE_SELECTION==(126,127,'113fresh'):checked(r['archive']['path'],r['archive']['sha256'],r['archive']['bytes'])
        verify_component_artifact(Path(r['archive']['path']),release)
    return dict(setup=setup,final=final,profile=profile,trust=trust)

def materialize(case):
    from tools.build_manager_distribution import unredirected
    unredirected(FIXTURE);require(not FIXTURE.exists(),'fresh_absent_fixture_required')
    for name,row in case['rows'].items():fresh(FIXTURE/name,checked(row['path'],row['sha256'],row['size']))
    fresh(FIXTURE/'Data/qualification-sentinel.json',SENTINEL)
    fresh(FIXTURE/'Data/Encyclopedia/settings.json',encoded(SETTINGS_MARKER))
    cache=FIXTURE/CACHE_RELATIVE
    unredirected(cache);cache.mkdir(parents=True,exist_ok=False)
    require(cache.is_dir() and not any(cache.iterdir()),'empty_owned_fixture_cache_required')
    verify_materialized(case)

def verify_materialized(case):
    for name,row in case['rows'].items():checked(FIXTURE/name,row['sha256'],row['size'])

def protected_snapshot():
    from suite_core.discovery.component_staging import guard
    result={}
    for zone in ('Data','Apps','State'):
        base=FIXTURE/zone;guard(base)
        if base.exists():
            for path in sorted(base.rglob('*')):
                guard(path)
                if path.is_file():result[path.relative_to(FIXTURE).as_posix()]=pin(path.read_bytes())
    return result

def ui_records(after):
    home=FIXTURE/'Data/QualificationQt';rows=[]
    if home.exists():
        for path in sorted(home.glob('*/events.jsonl')):
            if str(path) in after:continue
            raw=checked(path);require(len(raw)<=1024*1024,'bounded_driver_log_required')
            events=[json.loads(line) for line in raw.decode('utf-8').splitlines()]
            rows.append(dict(path=str(path),sha256=pin(raw),events=events))
    return rows

def settlement_gate(receipt,witness,*,version):
    from suite_core.encyclopedia.custody_entry import validate_custody_record
    require(receipt.get('phase')=='standalone_closed','original_terminal_receipt_required')
    deployment=receipt.get('deployment',{})
    require(deployment.get('root')==str(FIXTURE) and deployment.get('data')==str(FIXTURE/'Data'),
        'original_terminal_deployment_required')
    custody=receipt.get('standalone_custody');validate_custody_record(custody,receipt)
    require(custody['phase']=='drained' and custody['context']['bootstrap']==witness,'original_custody_exit_and_ack_required')
    attempt=receipt.get('standalone_encyclopedia',{}).get('attempt',{})
    require(attempt.get('phase')=='drained' and attempt.get('outcome')=='normal_close','original_reader_normal_drain_required')
    require(attempt.get('selected',{}).get('version')==version,'terminal_reader_version_changed')
    require(not receipt.get('children') and not receipt.get('component_stage') and not receipt.get('standalone_update'),
        'original_scopes_still_retained')


def validate_episode_ui_logs(logs,version,*,ordinary_retry_allowed=False):
    require(all(log.get('events') for log in logs),'complete_episode_ui_logs_required')
    expected={'enrollment-open','reader-close'} if version==VERSION_A else {'reader-update','reader-close'}
    scenarios=[log['events'][0]['scenario'] for log in logs]
    recovered='root-ordinary-close-retry' in scenarios
    require(not recovered or ordinary_retry_allowed,'root_retry_scenario_not_admitted')
    if recovered:expected.add('root-ordinary-close-retry')
    require(set(scenarios)==expected and len(scenarios)==len(expected),'exact_episode_ui_logs_required')
    for log in logs:require(log['events'][-1].get('status')=='completed-ui-sequence','ui_driver_held_or_incomplete')
    if recovered:
        retry=next(log['events'] for log in logs if log['events'][0]['scenario']=='root-ordinary-close-retry')
        intents=[e for e in retry if e.get('event')=='intent']
        steps=[e.get('step') for e in intents]
        close_intents=[e for e in intents if e.get('step') in ('close-root-1','close-root-2')]
        elapsed=retry[-1].get('elapsed_ms')
        require(steps in (['check-status-1','close-root-1'],
            ['check-status-1','close-root-1','check-status-2','close-root-2'])
            and all(e.get('action')=='click' for e in intents)
            and type(elapsed) is int and 0<=elapsed<=20000,'bounded_ordinary_close_retry_ui_required')
        require(any(e.get('event')=='observed' and e.get('step')==close_intents[-1]['step'] for e in retry),
            'ordinary_close_hidden_observation_required')
    if version==VERSION_B:
        update=next(log['events'] for log in logs if log['events'][0]['scenario']=='reader-update')
        require(len([e for e in update if e['event']=='modal-confirmed'])==2
            and any(e['event']=='observed' and e.get('step')=='cancel-update' for e in update),
            'actual_review_cancel_reconfirm_ui_evidence_required')
    return recovered

def inspect_settlement(process,witness,*,version,receipt_before,logs_before):
    require(process.poll()==0,'original_root_exit_zero_required')
    directory=FIXTURE/'Data/Manager/LaunchOwner'
    require(not (directory/'owner.json').exists(),'original_active_marker_retained')
    paths=[p for p in directory.glob('closed-*.json') if str(p) not in receipt_before]
    require(len(paths)==1,'one_fresh_original_terminal_receipt_required')
    raw=checked(paths[0]);require(len(raw)<=1024*1024,'bounded_terminal_receipt_required')
    receipt=json.loads(raw);require(paths[0].name=='closed-'+receipt['owner']+'.json','terminal_owner_name_changed')
    settlement_gate(receipt,witness,version=version)
    base=FIXTURE/'State/collectigen.tag-encyclopedia'
    require(not (base/'pending-transaction.json').exists() and not (base/'connection-pending.json').exists(),
        'pending_original_transaction_required_recovery')
    from core.encyclopedia_update_binding import admit_standalone_binding
    facts=preflight();profile=facts['profile']
    binding=admit_standalone_binding(root=FIXTURE,data_root=FIXTURE/'Data',
        settings_path=FIXTURE/'Data/Encyclopedia/settings.json',bootstrap_sha256=witness['executable_sha256'],
        trust_bytes=bytes.fromhex(profile['trust_hex']),trust_sha256=profile['trust_sha256'])
    require(binding.initial.phase=='healthy' and binding.initial.selected.version==version,'signed_healthy_saved_reader_required')
    checked(FIXTURE/'Data/qualification-sentinel.json',pin(SENTINEL))
    settings=json.loads(checked(FIXTURE/'Data/Encyclopedia/settings.json'))
    require(all(settings.get(k)==v for k,v in SETTINGS_MARKER.items()),'synthetic_unknown_settings_changed')
    logs=ui_records(logs_before)
    case=facts['setup'] if version==VERSION_A else facts['final']
    stage=json.loads(checked(case['home']/'qt-stage/STAGE.json',case['inputs']['stage_sha256']))
    recovered=validate_episode_ui_logs(logs,version,ordinary_retry_allowed=
        stage['qualification_qt']['scenario']=='baseline-ordinary-close-retry')
    return dict(receipt_path=str(paths[0]),receipt_sha256=pin(raw),witness=witness,
        selected_version=version,signed_healthy=True,ui_logs=logs,root_exit_code=0,
        recovered_ordinary_close=recovered)

def exchange(setup,final,settlement,backup):
    require(settlement.get('selected_version')==VERSION_A and settlement.get('root_exit_code')==0,
        'setup_settlement_before_exchange_required')
    require(not (FIXTURE/'Data/Manager/LaunchOwner/owner.json').exists(),'exchange_owner_still_active')
    verify_materialized(setup)
    before=protected_snapshot();added=[];changed=[]
    for name,row in final['rows'].items():
        old=setup['rows'].get(name)
        if old is not None:
            checked(FIXTURE/name,old['sha256'],old['size'])
            if old['sha256']==row['sha256']:continue
            fresh(backup/'preimages'/name,checked(FIXTURE/name,old['sha256'],old['size']))
            changed.append(name)
        else:require(not (FIXTURE/name).exists(),'new_response_destination_already_exists');added.append(name)
    # Entire response set included. No protected-zone path passes safe_relative.
    fresh(backup/'EXCHANGE_INTENT.json',encoded(dict(changed=changed,added=added,protected_before=before,
        settlement_receipt=settlement['receipt_sha256'])))
    for name in changed+added:
        row=final['rows'][name];destination=FIXTURE/name
        raw=checked(row['path'],row['sha256'],row['size'])
        if name in changed:
            old=setup['rows'][name];checked(destination,old['sha256'],old['size'])
            temp=destination.with_name(destination.name+'.qualification-exchange-new')
            fresh(temp,raw);os.replace(temp,destination)
        else:fresh(destination,raw)
        checked(destination,row['sha256'],row['size'])
    verify_materialized(final)
    require(protected_snapshot()==before,'protected_data_apps_state_changed_during_exchange')
    fresh(backup/'EXCHANGE_COMPLETE.json',encoded(dict(changed=changed,added=added,protected_unchanged=True,
        final_responses_verified=final['inputs']['response_count'])))

def resolve_known_preowner_failure(process,witness,case,output):
    """Legacy stderr/absence has no pinned pre-owner entry proof; keep it held."""
    return False  # No force stop, fabricated receipt or retrospective enrollment.


def admitted_preowner_entry(case):
    import re
    home=case.get('home')
    if home is None:return None
    admitted=_PREOWNER_FAILURE_CASES.get(str(Path(home).absolute()))
    if admitted is None:return None
    require(set(admitted)=={'number','image_sha256','profile_sha256','entry_sha256','handler_sha256','readback_sha256'},
        'closed_preowner_entry_admission_required')
    require(type(admitted['number']) is int and admitted['number']>0
        and all(type(admitted[name]) is str and re.fullmatch(r'[0-9a-f]{64}',admitted[name])
            for name in admitted if name!='number'),'exact_preowner_entry_pins_required')
    require(case['rows']['TagEncyclopedia.exe']['sha256']==admitted['image_sha256']
        and case['inputs']['profile_sha256']==admitted['profile_sha256'],'preowner_case_identity_changed')
    checked(Path(home)/('ROOT_PROFILE'+str(admitted['number'])+'.json'),admitted['profile_sha256'])
    entry=checked(Path(home)/'qt-stage/source/encyclopedia_root_entry.py',admitted['entry_sha256'])
    checked(Path(home)/'qt-stage/source/suite_core/encyclopedia/root_recovery_ui.py',admitted['handler_sha256'])
    from tools.encyclopedia_build.profile_renderer import render_entry
    prefix=render_entry('root',bound=True).split(b"if __name__ == '__main__':",1)[0]
    require(entry.startswith(prefix),'pinned_preowner_entry_boundary_required')
    readback=json.loads(checked(Path(home)/('ROOT_COMPILED_READBACK'+str(admitted['number'])+'.json'),admitted['readback_sha256']))
    require(readback['status']=='PINNED_COMPILED_SOURCE_READBACK_PASSED'
        and readback['actual_carchive_startup_verified'] is True
        and readback['startup_source']=='encyclopedia_root_entry.py'
        and {'suite_core.encyclopedia.__main__','suite_core.encyclopedia.root_recovery_ui'}<=set(readback['modules']),
        'compiled_preowner_entry_proof_required')
    return dict(admitted)


def preowner_state():
    from suite_core.discovery.component_staging import guard
    require(not (FIXTURE/'Data/Manager/LaunchOwner/owner.json').exists(),'preowner_baseline_active_owner')
    directories=[];bases={}
    for zone in ('Data','Apps','State'):
        base=FIXTURE/zone;guard(base)
        bases[zone]='directory' if base.is_dir() else 'absent'
        if base.exists():
            require(base.is_dir(),'preowner_protected_base_directory_required')
            for path in base.rglob('*'):
                guard(path)
                if path.is_dir():directories.append(path.relative_to(FIXTURE).as_posix())
    return dict(files=protected_snapshot(),directories=sorted(directories),bases=bases)


def resolve_controlled_preowner_failure(process,witness,case,output,checkpoint):
    """Only exited original process plus pinned pre-dispatch proof can settle FAILED."""
    if process.poll() in (None,0):return False  # Zero exit follows strict normal settlement, even after a hold.
    require(checkpoint['process'] is process and int(process._handle)==checkpoint['handle']
        and witness==checkpoint['witness'] and process.pid==witness['pid'],'original_failed_startup_handle_required')
    from core.manager_launch_owner import _normal_native_api,_native_witness
    require(_native_witness(_normal_native_api(),int(process._handle))==witness,'original_failed_startup_witness_changed')
    admitted=admitted_preowner_entry(case)
    require(admitted is not None and admitted==checkpoint['admission'],'pinned_preowner_entry_required')
    verify_materialized(case)
    error=output/'stderr.log'
    require(error.is_file() and error.stat().st_size<=4096,'bounded_preowner_diagnostic_required')
    raw=checked(error)
    from suite_core.discovery.component_release_signature import _json
    document=_json(raw,'preowner failure diagnostic')
    require(type(document) is dict and set(document)=={'schema','event','phase','status','code','errors'}
        and type(document['schema']) is int and document['schema']==1
        and document['event']=='encyclopedia_preowner_startup_failed'
        and document['phase']=='profile_initialization' and document['status']=='failed'
        and document['code'] in ('empty_owned_cache_required','qualification_cache_parent_required','not_recorded'),
        'known_controlled_preowner_failure_required')
    require(type(document['errors']) is list and len(document['errors'])<=4,'bounded_preowner_errors_required')
    import re
    for item in document['errors']:
        require(type(item) is dict and set(item)=={'type','frames'}
            and type(item['type']) is str and (item['type']=='<unknown>' or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}',item['type']))
            and type(item['frames']) is list and len(item['frames'])<=4,'closed_preowner_errors_required')
        for frame in item['frames']:
            require(type(frame) is dict and set(frame)=={'module','function','line'}
                and type(frame['line']) is int and frame['line']>0
                and type(frame['module']) is str and len(frame['module'])<=128
                and (frame['module']=='<external>' or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]*',frame['module']))
                and type(frame['function']) is str and (frame['function']=='<external>'
                    or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}',frame['function'])),
                'closed_preowner_frame_required')
    current=preowner_state();before=checkpoint['state']
    require(current['bases']==before['bases'],'preowner_failure_protected_base_changed')
    added=set(current['directories'])-set(before['directories'])
    removed=set(before['directories'])-set(current['directories'])
    allowed={name for name in added if re.fullmatch(re.escape(CACHE_RELATIVE)+r'/root-start-[0-9a-f]{32}',name)}
    require(not removed and added==allowed and len(allowed)<=1,'preowner_failure_new_resources')
    files=dict(current['files'])
    for leaf in allowed:
        marker=leaf+'/.component-acquisition-owner.json'
        if marker in files:
            expected=json.dumps(dict(purpose='COMPONENT_ACQUISITION_CACHE',owner='encyclopedia-sealed-qualification'),sort_keys=True).encode()
            require(checked(FIXTURE/marker)==expected,'failed_cache_leaf_owner_changed')
            del files[marker]
    require(files==before['files'],'preowner_failure_prior_state_changed')
    code=process.poll();require(type(code) is int and code!=0,'original_failed_startup_nonzero_exit_required')
    receipt=dict(status='FAILED_PREOWNER_STARTUP',witness=witness,case=str(case['home']),
        admission=admitted,prelaunch_state_sha256=pin(encoded(before)),diagnostic_sha256=pin(raw),
        retained_cache_leaves=sorted(allowed),actual_exit_code=code,forced_stop=False,
        normal_settlement=False,native_qualified=False,advance_allowed=False)
    destination=output/'FAILED_PREOWNER_STARTUP.json';payload=encoded(receipt)
    fresh(destination,payload);checked(destination,pin(payload),len(payload))
    raise FailedPreOwnerStartup('controlled_preowner_startup_failed_original_exit_verified')

def watch_original(process,*,timeout,on_hold,on_check=None):
    deadline=monotonic()+timeout;held=False
    while process.poll() is None:
        if on_check is not None:on_check()
        if not held and monotonic()>deadline:
            held=True;on_hold('root_watchdog_expired_original_handle_retained')
        try:sleep(.1)
        except KeyboardInterrupt:
            if not held:
                held=True;on_hold('runner_interrupted_original_handle_retained')
    require(not held,'watchdog_hold_no_next_episode')
    return process.poll()

def launch_episode(case,version,output):
    verify_materialized(case)
    failure_admission=admitted_preowner_entry(case)
    failure_state=preowner_state() if failure_admission is not None else None
    receipt_before={str(p) for p in (FIXTURE/'Data/Manager/LaunchOwner').glob('closed-*.json')}
    logs_before={str(p) for p in (FIXTURE/'Data/QualificationQt').glob('*/events.jsonl')}
    from tools.manager_build.windows_build_isolation import build_environment
    environment=build_environment(output/'environment')
    # Stock isolated environment omits inherited QT_QPA_PLATFORM/Python injections.
    stdout=(output/'stdout.log').open('xb');stderr=(output/'stderr.log').open('xb')
    process=subprocess.Popen([str(FIXTURE/'TagEncyclopedia.exe')],cwd=FIXTURE,env=environment,
        stdout=stdout,stderr=stderr,shell=False)
    _RETAINED_RUNS.append((process,stdout,stderr))
    witness=None
    checkpoint=None
    def check_failure():
        if failure_admission is not None:
            require(checkpoint is not None,'original_failed_startup_checkpoint_required')
            return resolve_controlled_preowner_failure(process,witness,case,output,checkpoint)
        return resolve_known_preowner_failure(process,witness,case,output)
    def retire_failed_original():
        stdout.close();stderr.close()
        if failure_admission is not None:
            require(process.poll() is not None,'failed_original_still_live')
            process._handle.Close()
            for index,retained in enumerate(_RETAINED_RUNS):
                if retained[0] is process:
                    del _RETAINED_RUNS[index];break
    def hold(reason):
        if not (output/'HOLD.json').exists():
            fresh(output/'HOLD.json',encoded(dict(reason=reason,pid=process.pid,
                original_creator_handle_retained=True,no_force_cleanup=True)))
    try:
        from core.manager_launch_owner import _normal_native_api,_native_witness
        witness=_native_witness(_normal_native_api(),int(process._handle))
        expected=case['rows']['TagEncyclopedia.exe']['sha256']
        require(witness['pid']==process.pid and witness['executable_sha256']==expected,'original_creator_witness_changed')
        if failure_admission is not None:
            checkpoint=dict(process=process,handle=int(process._handle),witness=dict(witness),
                admission=failure_admission,state=failure_state)
        fresh(output/'LAUNCH.json',encoded(dict(witness=witness,expected_image=expected,outer_job=False)))
        require(watch_original(process,timeout=240,on_hold=hold,
            on_check=check_failure)==0,'root_abnormal_exit_no_next_episode')
        result=inspect_settlement(process,witness,version=version,receipt_before=receipt_before,logs_before=logs_before)
        fresh(output/'SETTLED.json',encoded(result))
    except FailedPreOwnerStartup:
        retire_failed_original()
        raise  # Failed test terminal outcome; never inspect/fabricate a normal receipt.
    except BaseException:
        if witness is not None:
            try:check_failure()
            except FailedPreOwnerStartup:
                retire_failed_original();raise
            except (RunnerHold,LaunchHold,OSError,ValueError,subprocess.TimeoutExpired):pass
        try:hold('runner_uncertain_original_handle_retained')
        except OSError:pass  # Failed logging cannot authorize detaching a live run.
        # Keep the creator handle even after root exit while descendant/receipt
        # settlement is uncertain. Never advance after a hold, even if later settled.
        while True:
            try:
                if witness is not None and failure_admission is not None:
                    check_failure()
                if witness is not None and process.poll()==0:
                    later=inspect_settlement(process,witness,version=version,
                        receipt_before=receipt_before,logs_before=logs_before)
                    fresh(output/'SETTLED_AFTER_HOLD.json',encoded(later))
                    break
                sleep(.5)
            except FailedPreOwnerStartup:
                retire_failed_original();raise
            except KeyboardInterrupt:pass
            except (RunnerHold,LaunchHold,ValueError,OSError):sleep(.5)
        raise
    stdout.close();stderr.close()
    return result

def run():
    facts=preflight();require(not EVIDENCE.exists(),'fresh_runner_evidence_required')
    EVIDENCE.mkdir();fresh(EVIDENCE/'PREFLIGHT.json',encoded(dict(handoff_sha256=HANDOFF_SHA,fixture=str(FIXTURE))))
    materialize(facts['setup'])
    setup_out=EVIDENCE/'setup';setup_out.mkdir()
    settled=launch_episode(facts['setup'],VERSION_A,setup_out)
    exchange(facts['setup'],facts['final'],settled,EVIDENCE/'bootstrap-exchange')
    final_out=EVIDENCE/'final';final_out.mkdir()
    launch_episode(facts['final'],VERSION_B,final_out)
    scope=('synthetic instrumented native A909-B910 source113fresh episode'
        if CASE_SELECTION==(126,127,'113fresh') else 'synthetic instrumented native A907-B908 episode')
    fresh(EVIDENCE/'COMPLETE.json',encoded(dict(scope=scope,
        native_episode_passed=True,clean_machine_qualified=False,production_activated=False)))

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('preflight','run'))
    parser.add_argument('--case',choices=('113fresh',))
    args=parser.parse_args()
    if args.case=='113fresh':configure_source113_fresh()
    if args.mode=='preflight':
        facts=preflight()
        print(json.dumps(dict(status='READ_ONLY_PREFLIGHT_PASSED',setup_rows=len(facts['setup']['rows']),
            final_rows=len(facts['final']['rows']),fixture_exists=FIXTURE.exists(),native_launched=False),indent=2))
    else:run()

if __name__=='__main__':main()
