"""Runner mechanics with synthetic bytes/process doubles; no native launch."""
from pathlib import Path
from hashlib import sha256
import json
import pytest
from tools.encyclopedia_build import native_qt_runner as runner


@pytest.fixture
def source113(monkeypatch):
    for name in ('FIXTURE','EVIDENCE','HANDOFF','HANDOFF_SHA','CASE_SELECTION','VERSION_A','VERSION_B','SETTINGS_MARKER'):
        monkeypatch.setattr(runner,name,getattr(runner,name))
    runner.configure_source113_fresh()


def synthetic_receipt125():
    rows=[]
    for label,version in (('A','0.2.0-alpha.909'),('B','0.2.0-alpha.910')):
        home=runner.SOURCE113_RECEIPT.parent/label/'unsigned-package'
        row={name:'a'*64 for name in ('frozen_descriptor_sha256','image_sha256','module_sha256',
            'portable_manifest_sha256','profile_sha256','qt_stage_sha256','source_inventory_sha256')}
        row.update(reader=label,version=version,stock_signature_and_archive_publicly_verified=True,
            release_manifest_path=str(home/'release-manifest.input.json'),release_manifest_sha256='b'*64,
            release_manifest_size_bytes=705,package_path=str(home/('TagEncyclopedia-'+version+'-windows-x64-qualification.zip')),
            package_sha256='c'*64,package_size_bytes=100,signature_output=str(home/'collectigen-release.json.sig'),
            signature_sha256='d'*64,signature_size_bytes=763)
        rows.append(row)
    return dict(readers=rows,public_trust_sha256='e'*64)


def test_source113_closed_configuration(source113):
    assert runner.CASE_SELECTION==(126,127,'113fresh')
    assert (runner.VERSION_A,runner.VERSION_B)==('0.2.0-alpha.909','0.2.0-alpha.910')
    assert runner.FIXTURE==runner.BASE/'packet-118-source113-freshpath/fixture-root'
    assert runner.EVIDENCE==runner.BASE/'native-episode132-source113fresh'


def test_receipt125_is_hash_checked_before_parse(source113,monkeypatch):
    calls=[]
    def read(path,expected=None,size=None):
        calls.append((path,expected))
        raise runner.RunnerHold('artifact_pin_changed')
    monkeypatch.setattr(runner,'checked',read)
    with pytest.raises(runner.RunnerHold,match='artifact_pin_changed'):runner.signed_receipt()
    assert calls==[(runner.SOURCE113_RECEIPT,runner.SOURCE113_RECEIPT_SHA)]


@pytest.mark.parametrize('mutation',('version','duplicate','extra','path','unverified'))
def test_receipt125_rejects_wrong_closed_reader(source113,monkeypatch,mutation):
    receipt=synthetic_receipt125();row=receipt['readers'][0]
    if mutation=='version':row['version']='0.2.0-alpha.907'
    elif mutation=='duplicate':receipt['readers'][1]['reader']='A'
    elif mutation=='extra':row['unexpected']=True
    elif mutation=='path':row['signature_output']=str(runner.BASE/'other.sig')
    else:row['stock_signature_and_archive_publicly_verified']=False
    monkeypatch.setattr(runner,'checked',lambda *a,**kw:runner.encoded(receipt))
    with pytest.raises(runner.RunnerHold):runner.signed_receipt()


def test_receipt125_flat_adapter_preserves_all_asset_pins(source113,monkeypatch):
    receipt=synthetic_receipt125()
    monkeypatch.setattr(runner,'checked',lambda *a,**kw:runner.encoded(receipt))
    adapted=runner.signed_receipt()
    for original,row in zip(receipt['readers'],adapted['readers']):
        assert row['manifest']==dict(path=original['release_manifest_path'],sha256=original['release_manifest_sha256'],bytes=705)
        assert row['archive']==dict(path=original['package_path'],sha256=original['package_sha256'],bytes=100)
        assert row['signature_sha256']==original['signature_sha256'] and row['signature_bytes']==763


@pytest.mark.parametrize('number,kind,revision',((126,'final','113fresh'),(127,'setup','113fresh'),(126,'setup','113'),(128,'final','113fresh')))
def test_source113_rejects_unreviewed_artifact_combinations(number,kind,revision):
    with pytest.raises(runner.RunnerHold,match='reviewed_artifact_case_required'):
        runner.artifact_case(number,kind,revision)


@pytest.mark.parametrize('mutation',('none','handoff','versions','map','stage','stage_pin','receipt'))
def test_source113_binds_signed_assets_and_hashed_stage(source113,monkeypatch,tmp_path,mutation):
    monkeypatch.setattr(runner,'checked',lambda *a,**kw:runner.encoded(synthetic_receipt125()))
    receipt=runner.signed_receipt()
    record=dict(case_selection_proposal=[126,127,'113fresh'],fixture_root=str(runner.FIXTURE),
        signed_receipt_path=str(runner.SOURCE113_RECEIPT),signed_receipt_sha256=runner.SOURCE113_RECEIPT_SHA)
    stage=runner.encoded(dict(qualification_qt=dict(scenario='baseline-ordinary-close-retry',
        a_version=runner.VERSION_A,b_version=runner.VERSION_B)))
    cases=[]
    for count,versions in ((6,[runner.VERSION_A]),(9,[runner.VERSION_A,runner.VERSION_B])):
        rows={}
        for row in receipt['readers'][:len(versions)]:
            prefix='_qualification/packages/'+row['reader']+'/'
            for asset,name in ((row['manifest'],'collectigen-release.json'),
                (row['archive'],Path(row['archive']['path']).name),
                (dict(sha256=row['signature_sha256'],bytes=row['signature_bytes']),'collectigen-release.json.sig')):
                rows[prefix+name]=dict(sha256=asset['sha256'],size=asset['bytes'])
        cases.append(dict(home=tmp_path,rows=rows,inputs=dict(reader_versions=versions,response_count=count,
            signed_receipt_sha256=runner.SOURCE113_RECEIPT_SHA,stage_sha256=runner.pin(stage))))
    if mutation=='handoff':record['fixture_root']=str(tmp_path/'wrong')
    elif mutation=='versions':cases[0]['inputs']['reader_versions']=[runner.VERSION_B]
    elif mutation=='map':next(iter(cases[0]['rows'].values()))['sha256']='0'*64
    elif mutation=='stage':stage=runner.encoded(dict(qualification_qt=dict(scenario='baseline',a_version=runner.VERSION_A,b_version=runner.VERSION_B)));cases[0]['inputs']['stage_sha256']=runner.pin(stage)
    elif mutation=='stage_pin':stage+=b' '
    elif mutation=='receipt':cases[0]['inputs']['signed_receipt_sha256']='0'*64
    def checked_stage(path,expected=None,size=None):
        runner.require(runner.pin(stage)==expected,'artifact_pin_changed')
        return stage
    monkeypatch.setattr(runner,'checked',checked_stage)
    if mutation=='none':runner.bind_source113(record,*cases,receipt)
    else:
        with pytest.raises(runner.RunnerHold):runner.bind_source113(record,*cases,receipt)


def test_preflight_does_not_require_fixture_absence():
    # Settlement reuses preflight after materialization. Absence belongs to materialize.
    import ast,inspect
    tree=ast.parse(inspect.getsource(runner.preflight))
    assert not any(isinstance(node,ast.Attribute) and node.attr=='exists' for node in ast.walk(tree))


def test_watchdog_retains_original_process_until_exit_without_force(monkeypatch):
    class Original:
        def __init__(self):self.polls=0
        def poll(self):
            self.polls+=1
            return 0 if self.polls>=4 else None
    process=Original();holds=[];clock=iter([0,2,3,4])
    monkeypatch.setattr(runner,'monotonic',lambda:next(clock))
    monkeypatch.setattr(runner,'sleep',lambda _:None)
    with pytest.raises(runner.RunnerHold,match='watchdog_hold_no_next_episode'):
        runner.watch_original(process,timeout=1,on_hold=holds.append)
    assert holds==['root_watchdog_expired_original_handle_retained'] and process.polls==4
    assert not hasattr(process,'kill') and not hasattr(process,'terminate')


def test_exchange_includes_all_response_assets_and_preserves_protected_data(tmp_path,monkeypatch):
    fixture=tmp_path/'fixed-fixture';monkeypatch.setattr(runner,'FIXTURE',fixture)
    setup_root=tmp_path/'setup';final_root=tmp_path/'final'
    setup_bytes={'TagEncyclopedia.exe':b'INERT setup root',
        '_bootstrap/qualification-responses.json':b'old map',
        '_qualification/releases-page1.json':b'A listing',
        '_qualification/packages/A/A.zip':b'unchanged signed fixture bytes'}
    final_bytes={**setup_bytes,'TagEncyclopedia.exe':b'INERT final root',
        '_bootstrap/qualification-responses.json':b'new map',
        '_qualification/releases-page1.json':b'A and B listing',
        '_qualification/packages/B/B.zip':b'INERT B ZIP',
        '_qualification/packages/B/collectigen-release.json':b'INERT manifest',
        '_qualification/packages/B/collectigen-release.json.sig':b'INERT public signature'}
    def case(home,values):
        rows={}
        for name,raw in values.items():
            p=home/name;runner.fresh(p,raw)
            rows[name]=dict(path=p,size=len(raw),sha256=sha256(raw).hexdigest())
        return dict(rows=rows,inputs=dict(response_count=9))
    setup=case(setup_root,setup_bytes);final=case(final_root,final_bytes)
    runner.materialize(setup)
    runner.fresh(fixture/'Apps/fixture-package/retained.bin',b'preserve installed A')
    runner.fresh(fixture/'State/fixture-journal.json',b'preserve journal')
    before=runner.protected_snapshot();backup=tmp_path/'outside-backup'
    settlement=dict(selected_version=runner.VERSION_A,root_exit_code=0,receipt_sha256='a'*64)
    runner.exchange(setup,final,settlement,backup)
    assert runner.protected_snapshot()==before
    assert all((fixture/n).read_bytes()==raw for n,raw in final_bytes.items())
    intent=json.loads((backup/'EXCHANGE_INTENT.json').read_bytes())
    assert len(intent['added'])==3
    assert set(intent['changed'])=={'TagEncyclopedia.exe','_bootstrap/qualification-responses.json','_qualification/releases-page1.json'}
    assert all((backup/'preimages'/name).read_bytes()==setup_bytes[name] for name in intent['changed'])
    assert json.loads((backup/'EXCHANGE_COMPLETE.json').read_bytes())['final_responses_verified']==9


def test_active_original_owner_prevents_any_exchange_write(tmp_path,monkeypatch):
    fixture=tmp_path/'fixture';monkeypatch.setattr(runner,'FIXTURE',fixture)
    runner.fresh(fixture/'Data/Manager/LaunchOwner/owner.json',b'original owner retained')
    backup=tmp_path/'backup'
    with pytest.raises(runner.RunnerHold,match='exchange_owner_still_active'):
        runner.exchange({'rows':{}},{'rows':{}},
            dict(selected_version=runner.VERSION_A,root_exit_code=0),backup)
    assert not backup.exists()


def test_nonterminal_record_cannot_substitute_for_owner_settlement():
    with pytest.raises(runner.RunnerHold,match='original_terminal_receipt_required'):
        runner.settlement_gate(dict(phase='standalone_encyclopedia'),{},version=runner.VERSION_A)


def test_fresh_materialization_creates_exact_empty_owned_cache(tmp_path,monkeypatch):
    fixture=tmp_path/'fixture';monkeypatch.setattr(runner,'FIXTURE',fixture)
    runner.materialize(dict(rows={}))
    cache=fixture/runner.CACHE_RELATIVE
    assert cache.is_dir() and list(cache.iterdir())==[]
    assert (fixture/'Data/qualification-sentinel.json').read_bytes()==runner.SENTINEL
    assert json.loads((fixture/'Data/Encyclopedia/settings.json').read_bytes())==runner.SETTINGS_MARKER
    with pytest.raises(runner.RunnerHold,match='fresh_absent_fixture_required'):
        runner.materialize(dict(rows={}))


def test_legacy_known_trace_cannot_authorize_force_stop_or_failed_receipt(tmp_path,monkeypatch):
    class Original:
        _handle=987
        def poll(self):return None
        def terminate(self):pytest.fail('Text cannot authorize force stop')
        def wait(self,timeout):pytest.fail('No original exit proven')
    process=Original();output=tmp_path/'logs';output.mkdir()
    monkeypatch.setattr(runner,'FIXTURE',tmp_path/'fixture')
    monkeypatch.setattr(runner,'verify_materialized',lambda _:None)
    witness=dict(pid=123,creation_filetime=456,executable_sha256='a'*64)
    runner.fresh(output/'stderr.log',runner.KNOWN_PREOWNER_STDERR.encode())
    assert runner.resolve_known_preowner_failure(process,witness,{},output) is False
    assert not (output/'FAILED_PREOWNER_STARTUP_RESOLVED.json').exists()
    assert not (output/'SETTLED.json').exists()


def test_known_trace_with_ownership_evidence_cannot_stop_original(tmp_path,monkeypatch):
    class Original:
        def terminate(self):pytest.fail('ownership must prevent termination')
    monkeypatch.setattr(runner,'FIXTURE',tmp_path/'fixture')
    monkeypatch.setattr(runner,'verify_materialized',lambda _:None)
    output=tmp_path/'logs';output.mkdir()
    runner.fresh(output/'stderr.log',runner.KNOWN_PREOWNER_STDERR.encode())
    runner.fresh(runner.FIXTURE/'Data/Manager/LaunchOwner/owner.json',b'owner')
    assert runner.resolve_known_preowner_failure(Original(),{}, {},output) is False
    assert not (output/'FAILED_PREOWNER_STARTUP_RESOLVED.json').exists()


def test_repeated_stock_custody_refusals_keep_original_runner_until_settlement(tmp_path,monkeypatch):
    from core import manager_launch_owner as native
    from core.manager_launch_owner import LaunchHold
    class Original:
        pid=123
        _handle=987
        def poll(self):return 0
    process=Original();output=tmp_path/'evidence';output.mkdir()
    monkeypatch.setattr(runner,'FIXTURE',tmp_path/'fixture')
    monkeypatch.setattr(runner,'verify_materialized',lambda _:None)
    monkeypatch.setattr(runner.subprocess,'Popen',lambda *a,**kw:process)
    monkeypatch.setattr(native,'_normal_native_api',lambda:object())
    monkeypatch.setattr(native,'_native_witness',lambda api,handle:
        dict(pid=123,creation_filetime=456,executable_sha256='a'*64))
    monkeypatch.setattr(runner,'watch_original',lambda *a,**kw:0)
    sleeps=[];monkeypatch.setattr(runner,'sleep',sleeps.append)
    calls=[]
    def inspect(original,*a,**kw):
        calls.append(original)
        assert original is process and runner._RETAINED_RUNS[-1][0] is process
        assert not runner._RETAINED_RUNS[-1][1].closed and not runner._RETAINED_RUNS[-1][2].closed
        if len(calls)<5:raise LaunchHold('original_custody_drain_ack_required')
        return dict(selected_version=runner.VERSION_A,root_exit_code=0)
    monkeypatch.setattr(runner,'inspect_settlement',inspect)
    with pytest.raises(LaunchHold,match='original_custody_drain_ack_required'):
        runner.launch_episode(dict(rows={'TagEncyclopedia.exe':{'sha256':'a'*64}}),runner.VERSION_A,output)
    assert len(calls)==5 and sleeps==[.5,.5,.5]
    assert (output/'HOLD.json').exists() and (output/'SETTLED_AFTER_HOLD.json').exists()
    assert not (output/'SETTLED.json').exists()
    # Test-only disposal after proving original retention and no successful advance.
    retained=runner._RETAINED_RUNS.pop();retained[1].close();retained[2].close()


def completed_ui(scenario):
    return dict(events=[dict(event='started',scenario=scenario),
        dict(event='finished',status='completed-ui-sequence',native_health_claimed=False)])


def ordinary_retry_ui(*,attempts=1,status='completed-ui-sequence',elapsed=100):
    log=completed_ui('root-ordinary-close-retry');events=log['events'][:1]
    for attempt in range(1,attempts+1):
        events.extend([dict(event='intent',step=f'check-status-{attempt}',action='click'),
            dict(event='intent',step=f'close-root-{attempt}',action='click'),
            dict(event='observed',step=f'close-root-{attempt}')])
    events.append(dict(event='finished',status=status,elapsed_ms=elapsed,native_health_claimed=False))
    return dict(events=events)


@pytest.mark.parametrize('attempts',[1,2])
def test_completed_ordinary_retry_is_optional_only_in_admitted_root_scenario(attempts):
    logs=[completed_ui('enrollment-open'),completed_ui('reader-close'),ordinary_retry_ui(attempts=attempts)]
    with pytest.raises(runner.RunnerHold,match='root_retry_scenario_not_admitted'):
        runner.validate_episode_ui_logs(logs,runner.VERSION_A)
    assert runner.validate_episode_ui_logs(logs,runner.VERSION_A,ordinary_retry_allowed=True)
    assert not runner.validate_episode_ui_logs(logs[:2],runner.VERSION_A,ordinary_retry_allowed=True)


@pytest.mark.parametrize('retry',[ordinary_retry_ui(status='held'),ordinary_retry_ui(attempts=3),
    ordinary_retry_ui(elapsed=20001),dict(events=[])])
def test_held_partial_extra_or_late_retry_is_not_ui_success(retry):
    logs=[completed_ui('enrollment-open'),completed_ui('reader-close'),retry]
    with pytest.raises(runner.RunnerHold):runner.validate_episode_ui_logs(logs,runner.VERSION_A,ordinary_retry_allowed=True)


def test_completed_retry_logs_cannot_waive_actual_root_exit_or_native_terminal_record():
    class NotExitedNormally:
        def poll(self):return 1
    with pytest.raises(runner.RunnerHold,match='original_root_exit_zero_required'):
        runner.inspect_settlement(NotExitedNormally(),{},version=runner.VERSION_A,receipt_before=set(),logs_before=set())
    with pytest.raises(runner.RunnerHold,match='original_terminal_receipt_required'):
        runner.settlement_gate(dict(phase='standalone_encyclopedia'),{},version=runner.VERSION_A)


def test_scenario_only_stage_mutation_cannot_enable_root_retry(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from core import encyclopedia_update_binding as binding
    fixture=tmp_path/'fixture';monkeypatch.setattr(runner,'FIXTURE',fixture)
    runner.fresh(fixture/'Data/Manager/LaunchOwner/closed-fixture-owner.json',runner.encoded(dict(owner='fixture-owner')))
    runner.fresh(fixture/'Data/qualification-sentinel.json',runner.SENTINEL)
    runner.fresh(fixture/'Data/Encyclopedia/settings.json',runner.encoded(runner.SETTINGS_MARKER))
    # Isolate stage admission after previously tested native gates; no native proof claim.
    monkeypatch.setattr(runner,'settlement_gate',lambda *a,**kw:None)
    monkeypatch.setattr(binding,'admit_standalone_binding',lambda **kw:SimpleNamespace(
        initial=SimpleNamespace(phase='healthy',selected=SimpleNamespace(version=runner.VERSION_A))))
    monkeypatch.setattr(runner,'ui_records',lambda _: [completed_ui('enrollment-open'),completed_ui('reader-close')])
    stage=tmp_path/'qt-stage/STAGE.json'
    original=runner.encoded(dict(qualification_qt=dict(scenario='baseline')))
    runner.fresh(stage,original)
    case=dict(home=tmp_path,inputs=dict(stage_sha256=runner.pin(original)))
    monkeypatch.setattr(runner,'preflight',lambda:dict(setup=case,final=case,
        profile=dict(trust_hex='00',trust_sha256='a'*64)))
    process=SimpleNamespace(poll=lambda:0);witness=dict(executable_sha256='b'*64)
    args=dict(version=runner.VERSION_A,receipt_before=set(),logs_before=set())
    assert runner.inspect_settlement(process,witness,**args)['recovered_ordinary_close'] is False
    mutated=runner.encoded(dict(qualification_qt=dict(scenario='baseline-ordinary-close-retry')))
    stage.write_bytes(mutated)  # Synthetic test mutation; no real stage is modified.
    with pytest.raises(runner.RunnerHold,match='artifact_pin_changed'):
        runner.inspect_settlement(process,witness,**args)


@pytest.fixture
def controlled_failure(tmp_path,monkeypatch):
    """Explicit fake compiled readback; mechanics evidence, never native proof."""
    from core import manager_launch_owner as native
    from tools.encyclopedia_build.profile_renderer import render_entry
    from suite_core.encyclopedia.root_recovery_ui import preowner_failure_diagnostic
    fixture=tmp_path/'fixture';monkeypatch.setattr(runner,'FIXTURE',fixture)
    home=tmp_path/'case';image=b'INERT original root';profile=b'INERT pinned profile'
    entry=render_entry('root',bound=True);handler=b'INERT compiled handler double'
    readback=runner.encoded(dict(status='PINNED_COMPILED_SOURCE_READBACK_PASSED',
        actual_carchive_startup_verified=True,startup_source='encyclopedia_root_entry.py',
        modules=['suite_core.encyclopedia.__main__','suite_core.encyclopedia.root_recovery_ui']))
    sources={'ROOT_PROFILE1.json':profile,'qt-stage/source/encyclopedia_root_entry.py':entry,
        'qt-stage/source/suite_core/encyclopedia/root_recovery_ui.py':handler,
        'ROOT_COMPILED_READBACK1.json':readback,'payload/TagEncyclopedia.exe':image}
    for name,raw in sources.items():runner.fresh(home/name,raw)
    case=dict(home=home,rows={'TagEncyclopedia.exe':dict(path=home/'payload/TagEncyclopedia.exe',
        size=len(image),sha256=runner.pin(image))},inputs=dict(profile_sha256=runner.pin(profile)))
    admission=dict(number=1,image_sha256=runner.pin(image),profile_sha256=runner.pin(profile),
        entry_sha256=runner.pin(entry),handler_sha256=runner.pin(handler),readback_sha256=runner.pin(readback))
    monkeypatch.setattr(runner,'_PREOWNER_FAILURE_CASES',{str(home.absolute()):admission})
    runner.materialize(case)
    # Completed prior setup state is legitimate and must remain byte-exact.
    runner.fresh(fixture/'Apps/A/retained.bin',b'INERT installed A')
    runner.fresh(fixture/'State/A/selection.json',b'INERT selection')
    runner.fresh(fixture/'Data/Manager/LaunchOwner/closed-A.json',b'INERT prior terminal receipt')
    class Handle:
        closed=False
        def __int__(self):return 987
        def Close(self):self.closed=True
    class Original:
        pid=123
        _handle=Handle()
        exit_code=2
        def poll(self):return self.exit_code
        def terminate(self):pytest.fail('No force cleanup')
        def kill(self):pytest.fail('No force cleanup')
    process=Original();witness=dict(pid=123,creation_filetime=456,executable_sha256=runner.pin(image))
    monkeypatch.setattr(native,'_normal_native_api',lambda:object())
    monkeypatch.setattr(native,'_native_witness',lambda api,handle:dict(witness) if handle==987 else None)
    output=tmp_path/'logs';output.mkdir()
    runner.fresh(output/'stderr.log',(preowner_failure_diagnostic(ValueError('empty_owned_cache_required'))+'\n').encode())
    checkpoint=dict(process=process,handle=987,witness=dict(witness),admission=dict(admission),state=runner.preowner_state())
    return process,witness,case,output,checkpoint


def test_controlled_failed_startup_records_nonzero_exit_and_preserves_prior_state(controlled_failure):
    process,witness,case,output,checkpoint=controlled_failure
    with pytest.raises(runner.FailedPreOwnerStartup,match='original_exit_verified'):
        runner.resolve_controlled_preowner_failure(*controlled_failure)
    receipt=json.loads((output/'FAILED_PREOWNER_STARTUP.json').read_bytes())
    assert receipt['actual_exit_code']==2 and receipt['witness']==witness
    assert receipt['normal_settlement'] is receipt['native_qualified'] is receipt['advance_allowed'] is False
    assert receipt['forced_stop'] is False
    assert runner.preowner_state()==checkpoint['state']
    assert not (output/'SETTLED.json').exists() and not (output/'SETTLED_AFTER_HOLD.json').exists()


def test_controlled_failure_allows_only_one_new_retained_cache_leaf(controlled_failure):
    leaf=runner.FIXTURE/runner.CACHE_RELATIVE/('root-start-'+'a'*32)
    leaf.mkdir()
    marker=json.dumps(dict(purpose='COMPONENT_ACQUISITION_CACHE',owner='encyclopedia-sealed-qualification'),sort_keys=True).encode()
    runner.fresh(leaf/'.component-acquisition-owner.json',marker)
    with pytest.raises(runner.FailedPreOwnerStartup):runner.resolve_controlled_preowner_failure(*controlled_failure)
    assert (leaf/'.component-acquisition-owner.json').read_bytes()==marker
    receipt=json.loads((controlled_failure[3]/'FAILED_PREOWNER_STARTUP.json').read_bytes())
    assert receipt['retained_cache_leaves']==[runner.CACHE_RELATIVE+'/root-start-'+'a'*32]


def test_pinned_profile_failure_can_redact_its_cause_without_losing_failed_classification(controlled_failure):
    from suite_core.encyclopedia.root_recovery_ui import preowner_failure_diagnostic
    output=controlled_failure[3]
    (output/'stderr.log').write_text(preowner_failure_diagnostic(PermissionError('SYNTHETIC_SECRET'))+'\n')
    with pytest.raises(runner.FailedPreOwnerStartup):runner.resolve_controlled_preowner_failure(*controlled_failure)
    assert 'SYNTHETIC_SECRET' not in (output/'stderr.log').read_text()
    receipt=json.loads((output/'FAILED_PREOWNER_STARTUP.json').read_bytes())
    assert receipt['normal_settlement'] is False and receipt['actual_exit_code']==2


@pytest.mark.parametrize('mutation',('live','zero','process','handle','birth','image','profile','entry','handler',
    'unadmitted','diagnostic','errors','prior_file','new_owner','new_receipt','new_child_state','cache_artifact','two_leaves','reused_receipt'))
def test_uncertain_preowner_attempt_stays_unsettled(controlled_failure,monkeypatch,mutation):
    process,witness,case,output,checkpoint=controlled_failure
    if mutation in ('live','zero'):
        process.exit_code=None if mutation=='live' else 0
        assert runner.resolve_controlled_preowner_failure(*controlled_failure) is False
        assert not (output/'FAILED_PREOWNER_STARTUP.json').exists();return
    if mutation=='process':checkpoint['process']=object()
    elif mutation=='handle':checkpoint['handle']=988
    elif mutation=='birth':checkpoint['witness']['creation_filetime']=999
    elif mutation=='image':case['rows']['TagEncyclopedia.exe']['sha256']='0'*64
    elif mutation=='profile':(case['home']/'ROOT_PROFILE1.json').write_bytes(b'changed')
    elif mutation=='entry':(case['home']/'qt-stage/source/encyclopedia_root_entry.py').write_bytes(b'changed')
    elif mutation=='handler':(case['home']/'qt-stage/source/suite_core/encyclopedia/root_recovery_ui.py').write_bytes(b'changed')
    elif mutation=='unadmitted':monkeypatch.setattr(runner,'_PREOWNER_FAILURE_CASES',{})
    elif mutation=='diagnostic':(output/'stderr.log').write_bytes(runner.KNOWN_PREOWNER_STDERR.encode())
    elif mutation=='errors':
        record=json.loads((output/'stderr.log').read_bytes());record['errors']=[dict(unexpected=True)]
        (output/'stderr.log').write_bytes(runner.encoded(record))
    elif mutation=='prior_file':(runner.FIXTURE/'Apps/A/retained.bin').write_bytes(b'changed')
    elif mutation=='new_owner':runner.fresh(runner.FIXTURE/'Data/Manager/LaunchOwner/owner.json',b'new owner')
    elif mutation=='new_receipt':runner.fresh(runner.FIXTURE/'Data/Manager/LaunchOwner/closed-B.json',b'new receipt')
    elif mutation=='new_child_state':(runner.FIXTURE/'State/new-child').mkdir()
    elif mutation=='cache_artifact':runner.fresh(runner.FIXTURE/runner.CACHE_RELATIVE/'unexpected.zip',b'INERT')
    elif mutation=='two_leaves':
        for letter in ('a','b'):(runner.FIXTURE/runner.CACHE_RELATIVE/('root-start-'+letter*32)).mkdir()
    else:runner.fresh(output/'FAILED_PREOWNER_STARTUP.json',b'prior failed receipt')
    with pytest.raises(Exception):runner.resolve_controlled_preowner_failure(*controlled_failure)
    assert not process._handle.closed
    if mutation!='reused_receipt':assert not (output/'FAILED_PREOWNER_STARTUP.json').exists()


def test_failed_receipt_write_refusal_retains_original_without_cleanup(controlled_failure,monkeypatch):
    def refused(*a,**kw):raise OSError('synthetic receipt write refusal')
    monkeypatch.setattr(runner,'fresh',refused)
    with pytest.raises(OSError,match='write refusal'):runner.resolve_controlled_preowner_failure(*controlled_failure)
    assert not controlled_failure[0]._handle.closed
    assert runner.preowner_state()==controlled_failure[4]['state']


def test_launch_controlled_failure_retires_only_its_original_handles(controlled_failure,monkeypatch):
    process,witness,case,output,checkpoint=controlled_failure
    # launch_episode owns stderr creation; simulate only the diagnostic write at Popen.
    diagnostic=(output/'stderr.log').read_bytes();(output/'stderr.log').unlink()
    def spawn(*a,**kw):kw['stderr'].write(diagnostic);kw['stderr'].flush();return process
    monkeypatch.setattr(runner.subprocess,'Popen',spawn)
    retained_before=list(runner._RETAINED_RUNS)
    with pytest.raises(runner.FailedPreOwnerStartup):runner.launch_episode(case,runner.VERSION_A,output)
    assert runner._RETAINED_RUNS==retained_before and process._handle.closed
    assert (output/'FAILED_PREOWNER_STARTUP.json').exists() and not (output/'SETTLED.json').exists()


def test_failed_setup_never_advances_exchange_or_second_launch(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'EVIDENCE',tmp_path/'evidence')
    monkeypatch.setattr(runner,'preflight',lambda:dict(setup={'phase':'setup'},final={'phase':'final'}))
    monkeypatch.setattr(runner,'materialize',lambda _:None)
    calls=[]
    def failed(case,*a):calls.append(case);raise runner.FailedPreOwnerStartup('failed')
    monkeypatch.setattr(runner,'launch_episode',failed)
    monkeypatch.setattr(runner,'exchange',lambda *a:pytest.fail('No exchange after failure'))
    with pytest.raises(runner.FailedPreOwnerStartup):runner.run()
    assert calls==[{'phase':'setup'}]
    assert not (runner.EVIDENCE/'final').exists() and not (runner.EVIDENCE/'COMPLETE.json').exists()


@pytest.mark.parametrize('trigger',('watchdog','temporary_settlement'))
def test_enrolled_zero_exit_after_hold_reaches_strict_normal_settlement(controlled_failure,monkeypatch,trigger):
    from core.manager_launch_owner import LaunchHold
    process,witness,case,output,checkpoint=controlled_failure
    process.exit_code=0
    (output/'stderr.log').unlink()
    monkeypatch.setattr(runner.subprocess,'Popen',lambda *a,**kw:process)
    inspected=[]
    def inspect(original,*a,**kw):
        assert original is process
        inspected.append(original)
        if trigger=='temporary_settlement' and len(inspected)==1:raise LaunchHold('temporary_settlement_hold')
        return dict(selected_version=runner.VERSION_A,root_exit_code=0)
    monkeypatch.setattr(runner,'inspect_settlement',inspect)
    monkeypatch.setattr(runner,'sleep',lambda _:None)
    if trigger=='watchdog':
        def watchdog(*a,**kw):raise runner.RunnerHold('watchdog_hold_no_next_episode')
        monkeypatch.setattr(runner,'watch_original',watchdog)
    retained_before=len(runner._RETAINED_RUNS)
    with pytest.raises((runner.RunnerHold,LaunchHold)):
        runner.launch_episode(case,runner.VERSION_A,output)
    assert len(inspected)==(1 if trigger=='watchdog' else 2)
    assert (output/'SETTLED_AFTER_HOLD.json').exists() and (output/'HOLD.json').exists()
    assert not (output/'SETTLED.json').exists() and not (output/'FAILED_PREOWNER_STARTUP.json').exists()
    assert (output/'stderr.log').read_bytes()==b''  # Normal settlement required no failure diagnostic.
    assert len(runner._RETAINED_RUNS)==retained_before+1
    retained=runner._RETAINED_RUNS.pop();retained[1].close();retained[2].close()


@pytest.mark.parametrize('zone',('Data','Apps','State'))
@pytest.mark.parametrize('change',('delete','create','file'))
def test_protected_base_change_cannot_settle_failed_startup(controlled_failure,tmp_path,monkeypatch,zone,change):
    process,witness,case,output,checkpoint=controlled_failure
    root=tmp_path/'empty-zones';root.mkdir()
    runner.fresh(root/'TagEncyclopedia.exe',runner.checked(case['rows']['TagEncyclopedia.exe']['path']))
    monkeypatch.setattr(runner,'FIXTURE',root)
    base=root/zone
    if change!='create':base.mkdir()
    checkpoint['state']=runner.preowner_state()
    if change=='create':base.mkdir()
    else:
        base.rmdir()
        if change=='file':base.write_bytes(b'INERT substituted file')
    with pytest.raises(runner.RunnerHold,match='protected_base'):
        runner.resolve_controlled_preowner_failure(*controlled_failure)
    assert not (output/'FAILED_PREOWNER_STARTUP.json').exists() and not process._handle.closed
