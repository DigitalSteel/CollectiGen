"""Inert signed-state doubles test settings handoff, never native compatibility."""
from hashlib import sha256
import json
import pytest

from core.encyclopedia_connection import ReaderConnectionStore, manager_reader_settings_path
from core.encyclopedia_legacy_handoff import (prepare_legacy_reader_handoff,
    commit_legacy_selection_authority, cancel_legacy_selection_authority)
from tests.test_standalone_encyclopedia_update import standalone
from tests.test_standalone_encyclopedia_update import add_schema_package


@pytest.fixture
def handoff(standalone, monkeypatch):
    f, boot, _ = standalone
    # Existing inert fixture identity only; production constants stay closed.
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.LEGACY_VERSION', boot.binding.initial.selected.version)
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.LEGACY_PROGRAM_SHA256',
                        sha256(boot.binding.initial.selected.program.read_bytes()).hexdigest())
    def prepare():
        return prepare_legacy_reader_handoff(f.admission, manager_settings_path=f.settings,
            expected_selection=boot.binding.initial.selected,
            bootstrap_sha256=boot.binding.bootstrap_sha256,
            trust_bytes=f.trust, trust_sha256=sha256(f.trust).hexdigest())
    return f, boot, prepare


def test_only_selection_is_moved_and_canonical_authority_survives_restart(handoff):
    f, boot, prepare = handoff
    target = f.data / 'Encyclopedia/settings.json'
    target.write_text('{"legacy_future":{"keep":[1,2]}}', encoding='utf-8')
    original = f.settings.read_bytes()
    journal = (f.root / 'State/collectigen.tag-encyclopedia/transaction.json').read_bytes()
    assert manager_reader_settings_path(f.root, f.settings) == f.settings
    result = prepare()
    assert result.revalidate().selected == boot.binding.initial.selected
    assert f.settings.read_bytes() == original
    assert json.loads(target.read_bytes())['legacy_future'] == {'keep':[1,2]}
    assert (f.root / 'State/collectigen.tag-encyclopedia/transaction.json').read_bytes() == journal
    assert manager_reader_settings_path(f.root, f.settings) == f.settings  # preparation is not commitment
    authority = commit_legacy_selection_authority(f.admission,result,f.settings)
    assert authority is not None
    assert manager_reader_settings_path(f.root, f.settings) == target
    assert prepare().binding.current() == result.binding.current()


@pytest.mark.parametrize('changed', ['source', 'target', 'program', 'bootstrap', 'journal'])
def test_prepared_plan_rejects_drift_without_granting_health(handoff, changed):
    f, boot, prepare = handoff
    plan = prepare()
    paths = {'source':f.settings, 'target':plan.binding.settings_path,
             'program':plan.binding.initial.selected.program,
             'bootstrap':plan.binding.bootstrap_program,
             'journal':f.root/'State/collectigen.tag-encyclopedia/transaction.json'}
    path = paths[changed]
    path.write_bytes(path.read_bytes() + b' ' if changed in ('source','target') else b'changed')
    with pytest.raises(Exception): plan.revalidate()


def test_conflicting_canonical_connection_is_preserved(handoff):
    f, boot, prepare = handoff
    target = f.data/'Encyclopedia/settings.json'
    payload = json.loads(f.settings.read_bytes())
    payload['manager_encyclopedia_connection']['version'] = '9.0.0'
    target.write_text(json.dumps(payload), encoding='utf-8')
    original = target.read_bytes()
    with pytest.raises(ValueError, match='selection_conflict'): prepare()
    assert target.read_bytes() == original


def test_wrong_published_program_is_rejected_before_settings_write(handoff, monkeypatch):
    f, boot, prepare = handoff
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.LEGACY_PROGRAM_SHA256', '0'*64)
    target = f.data/'Encyclopedia/settings.json'
    assert not target.exists()
    with pytest.raises(ValueError, match='exact_published_legacy'): prepare()
    assert not target.exists()


def test_publication_failure_does_not_issue_a_plan(handoff, monkeypatch):
    f, boot, prepare = handoff
    def fail(*args, **kwargs): raise OSError('injected settings publication failure')
    monkeypatch.setattr(ReaderConnectionStore, 'save', fail)
    with pytest.raises(OSError, match='publication failure'): prepare()
    assert not (f.data/'Encyclopedia/settings.json').exists()


def test_fresh_scope_keeps_historical_store_until_explicit_legacy_commit(tmp_path):
    root = tmp_path/'root'; canonical = root/'Data/Encyclopedia/settings.json'
    historical = root/'Data/Manager/settings.ini'
    assert manager_reader_settings_path(root, historical) == historical.with_suffix('.json')
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b'not JSON')
    assert manager_reader_settings_path(root, historical) == historical.with_suffix('.json')


def test_cancel_restores_prior_authority_and_keeps_both_documents(handoff):
    f,boot,prepare=handoff
    plan=prepare()
    authority=commit_legacy_selection_authority(f.admission,plan,f.settings)
    canonical=plan.binding.settings_path.read_bytes()
    original=f.settings.read_bytes()
    cancel_legacy_selection_authority(f.admission,authority)
    assert manager_reader_settings_path(f.root,f.settings)==f.settings
    assert f.settings.read_bytes()==original and plan.binding.settings_path.read_bytes()==canonical


def test_cancel_cannot_remove_another_authority(handoff):
    f,boot,prepare=handoff
    authority=commit_legacy_selection_authority(f.admission,prepare(),f.settings)
    path=f.root/'State/collectigen.tag-encyclopedia/legacy-reader-settings.json'
    value=json.loads(path.read_bytes());value['schema']=2
    path.write_text(json.dumps(value),encoding='utf-8')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='cancel_changed'):
        cancel_legacy_selection_authority(f.admission,authority)
    assert path.read_bytes()==before


def test_changed_original_connection_does_not_silently_override_shared_choice(handoff):
    f,boot,prepare=handoff
    commit_legacy_selection_authority(f.admission,prepare(),f.settings)
    payload=json.loads(f.settings.read_bytes())
    payload['manager_encyclopedia_connection']['version']='9.0.0'
    f.settings.write_text(json.dumps(payload),encoding='utf-8')
    with pytest.raises(ValueError,match='original Manager Reader selection changed'):
        manager_reader_settings_path(f.root,f.settings)


def test_shared_selection_and_catalog_choice_survive_update_and_rollback(handoff,monkeypatch):
    f,boot,prepare=handoff
    plan=prepare();commit_legacy_selection_authority(f.admission,plan,f.settings)
    canonical=plan.binding.settings_path
    payload=json.loads(canonical.read_bytes());payload['synthetic_catalog_choice']={'keep':'same-catalog'}
    canonical.write_text(json.dumps(payload),encoding='utf-8')
    original=f.settings.read_bytes()
    f.service.store=ReaderConnectionStore(canonical)
    add_schema_package(f,'0.1.0-alpha.9')
    updated=f.install().installed
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.HOSTED_UPGRADE_VERSION',updated.selected.version)
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.HOSTED_UPGRADE_PROGRAM_SHA256',sha256(updated.selected.program.read_bytes()).hexdigest())
    def shared_plan():
        return prepare_legacy_reader_handoff(f.admission,manager_settings_path=canonical,
            expected_selection=f.service.observe().selected,bootstrap_sha256=boot.binding.bootstrap_sha256,
            trust_bytes=f.trust,trust_sha256=sha256(f.trust).hexdigest(),admitted_manager_settings_path=f.settings)
    assert shared_plan().binding.current().selected==updated.selected
    assert manager_reader_settings_path(f.root,f.settings)==canonical
    f.service.recover(confirmed=True)
    assert shared_plan().binding.current().selected==boot.binding.initial.selected
    assert json.loads(canonical.read_bytes())['synthetic_catalog_choice']=={'keep':'same-catalog'}
    assert f.settings.read_bytes()==original


def test_new_reader_without_committed_legacy_origin_cannot_enter_shared_route(handoff,monkeypatch):
    f,boot,prepare=handoff
    add_schema_package(f,'0.1.0-alpha.9');updated=f.install().installed
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.HOSTED_UPGRADE_VERSION',updated.selected.version)
    monkeypatch.setattr('core.encyclopedia_legacy_handoff.HOSTED_UPGRADE_PROGRAM_SHA256',sha256(updated.selected.program.read_bytes()).hexdigest())
    with pytest.raises(ValueError,match='exact_published_legacy'):
        prepare_legacy_reader_handoff(f.admission,manager_settings_path=f.settings,
            expected_selection=updated.selected,bootstrap_sha256=boot.binding.bootstrap_sha256,
            trust_bytes=f.trust,trust_sha256=sha256(f.trust).hexdigest(),admitted_manager_settings_path=f.settings)
