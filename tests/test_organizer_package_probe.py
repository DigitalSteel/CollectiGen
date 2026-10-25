"""Regression: an exited Qt app must not mask startup/guard failures."""
import pytest
import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

from tools.acceptance.organizer_package_probe import acceptance_passed, admit_reopen, workspace_path


def completed():
    return dict(exit_code=0, ready=True, ordinary_close=True, errors=[],
                pool_drained=True, pool_active=0, jobs_drained=True,
                jobs_active=0, settings_saved=True, checkout_not_on_path=True,
                source_markers_removed=True, blocked_events=[], unhandled_errors=[])


def test_completed_entry_can_pass():
    assert acceptance_passed(completed())


@pytest.mark.parametrize('failure', [
    {'blocked_events': ['subprocess.Popen']},
    {'unhandled_errors': ['RuntimeError']},
    {'errors': ['startup callback failed']},
    {'held': 'original Close not settled'},
    {'jobs_active': 1},
    {'pool_drained': False},
    {'settings_saved': False},
    {'checkout_not_on_path': False},
    {'source_markers_removed': False},
    {'ordinary_close': False},
    {'exit_code': 2},
])
def test_successful_exit_cannot_mask_incomplete_acceptance(failure):
    record = completed()
    record.update(failure)
    assert not acceptance_passed(record)


def test_missing_receipt_is_not_a_pass():
    assert not acceptance_passed({})


@pytest.fixture
def prior(tmp_path):
    repo=tmp_path/'repo';profile=repo/'.tmp/profile'
    settings=profile/'data/user/settings.json';settings.parent.mkdir(parents=True)
    saved={'onboarding_status':'skipped','onboarding_version':1,'ui_theme':'system',
           'active_library_id':'','library_profiles':[]}
    settings.write_text(json.dumps(saved),encoding='utf-8')
    receipt=repo/'.tmp/prior.json'
    record=completed();record.update(passed=True,profile=str(profile),onboarding_status='skipped',
        settings_sha256=hashlib.sha256(settings.read_bytes()).hexdigest())
    receipt.write_text(json.dumps(record),encoding='utf-8')
    return repo,profile,settings,receipt,hashlib.sha256(receipt.read_bytes()).hexdigest()


def test_pinned_qualified_reopen_is_read_only(prior):
    repo,profile,settings,receipt,digest=prior;before=settings.read_bytes()
    admitted=admit_reopen(repo,profile,receipt,digest)
    assert admitted['settings_pre_sha256']==hashlib.sha256(before).hexdigest()
    assert admitted['settings_fields']['onboarding_status']=='skipped'
    assert settings.read_bytes()==before


def test_changed_receipt_rejects_before_missing_settings_read(prior):
    repo,profile,settings,receipt,digest=prior
    receipt.write_bytes(receipt.read_bytes()+b' ');settings.unlink()
    with pytest.raises(ValueError,match='Prior receipt changed'):
        admit_reopen(repo,profile,receipt,digest)


def test_changed_settings_are_not_replayed_or_rewritten(prior):
    repo,profile,settings,receipt,digest=prior
    settings.write_bytes(settings.read_bytes()+b' ');changed=settings.read_bytes()
    with pytest.raises(ValueError,match='Generated settings changed'):
        admit_reopen(repo,profile,receipt,digest)
    assert settings.read_bytes()==changed


def test_unqualified_receipt_cannot_admit_profile(prior):
    repo,profile,settings,receipt,_=prior
    record=json.loads(receipt.read_bytes());record['blocked_events']=['subprocess.Popen']
    receipt.write_text(json.dumps(record),encoding='utf-8')
    with pytest.raises(ValueError,match='not qualified'):
        admit_reopen(repo,profile,receipt,hashlib.sha256(receipt.read_bytes()).hexdigest())


def test_mismatched_profile_is_not_opened(prior):
    repo,profile,settings,receipt,digest=prior
    with pytest.raises(ValueError,match='Prior profile differs'):
        admit_reopen(repo,repo/'.tmp/other-profile',receipt,digest)


def test_redirected_path_rejects_before_any_receipt_read(prior,monkeypatch):
    repo,profile,settings,receipt,digest=prior
    original=Path.lstat
    def redirected(path,*a,**k):
        return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400) if path==profile else original(path,*a,**k)
    monkeypatch.setattr(Path,'lstat',redirected)
    monkeypatch.setattr(Path,'read_bytes',lambda *a:pytest.fail('Read through redirected path'))
    with pytest.raises(ValueError,match='Redirected'):
        admit_reopen(repo,profile,receipt,digest)


@pytest.mark.parametrize('suffix',['../escape','Local Only/fixture','local_only/fixture'])
def test_unadmitted_workspace_paths_reject_before_inspection(tmp_path,monkeypatch,suffix):
    repo=tmp_path/'repo'
    monkeypatch.setattr(Path,'lstat',lambda *a:pytest.fail('Unadmitted path inspected'))
    with pytest.raises(ValueError,match='outside'):
        workspace_path(repo,repo/'.tmp'/suffix)


@pytest.mark.parametrize('failure',[{'settings_reopened':False},{'saved_fields_preserved':False},
                                   {'onboarding_repeated':True}])
def test_reopen_requires_loaded_and_saved_authority(failure):
    record=completed();record.update(mode='reopen',settings_reopened=True,saved_fields_preserved=True,onboarding_repeated=False)
    assert acceptance_passed(record)
    record.update(failure)
    assert not acceptance_passed(record)


@pytest.mark.parametrize('flag',['--reopen-from','--reopen-receipt-sha256'])
def test_reopen_needs_both_pins_before_output_creation(prior,monkeypatch,flag):
    from tools.acceptance import organizer_package_probe as probe
    repo,profile,settings,receipt,digest=prior;output=repo/'.tmp/output'
    monkeypatch.setattr(probe,'__file__',str(repo/'tools/acceptance/organizer_package_probe.py'))
    monkeypatch.setattr(probe.sys,'argv',['probe','--package',str(repo/'.tmp/package'),
        '--profile',str(profile),'--output',str(output),flag,
        str(receipt) if flag=='--reopen-from' else digest])
    with pytest.raises(SystemExit) as error:probe.main()
    assert error.value.code==2 and not output.exists()


def test_fresh_mode_still_refuses_existing_profile(prior,monkeypatch):
    from tools.acceptance import organizer_package_probe as probe
    repo,profile,settings,receipt,digest=prior;output=repo/'.tmp/output'
    monkeypatch.setattr(probe,'__file__',str(repo/'tools/acceptance/organizer_package_probe.py'))
    monkeypatch.setattr(probe.sys,'argv',['probe','--package',str(repo/'.tmp/package'),
        '--profile',str(profile),'--output',str(output)])
    with pytest.raises(AssertionError):probe.main()
    assert not output.exists()


def test_title_ready_does_not_qualify_missing_fresh_branch():
    record=completed();record.update(mode='fresh',events=[],onboarding_status='not_started',
                                    startup_outcome='awaiting_first_run')
    assert not acceptance_passed(record)


@pytest.mark.parametrize('missing',['event','outcome','saved_status'])
def test_fresh_branch_requires_visible_action_and_saved_result(missing):
    record=completed();record.update(mode='fresh',events=['visible_skip_for_now'],
        onboarding_status='skipped',startup_outcome='setup_skipped',required_component_readiness='not_assessed')
    assert acceptance_passed(record)  # Skip is valid; it does not assert component readiness.
    if missing=='event':record['events']=[]
    elif missing=='outcome':record['startup_outcome']='awaiting_first_run'
    else:record['onboarding_status']='not_started'
    assert not acceptance_passed(record)
