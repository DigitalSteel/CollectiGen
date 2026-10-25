"""Original admitted empty-default-layer and retained-local import contracts."""
from dataclasses import replace
import time

import pytest
from core.encyclopedia_catalog import EncyclopediaCatalog
from core import tag_catalog_connection as connection
from core.category_registry import CategoryRegistry
from core.tag_database import TagDatabase, TagRecord
from suite_core.manager.tag_database_controller import empty_default_layer, _observe, TagDatabaseController
from suite_core.manager.resource_executor import ResourceExecutorOwner
from test_tag_catalog_connection import selected, connect
from test_tag_database_pack_producer import reviewed_local_input


def populate(choice):
    root = choice.native_adapter.root
    with choice.native_adapter.guard():
        categories = CategoryRegistry(root / 'data/defaults/categories.json', root / 'data/user/categories.json')
        cat = categories.add('Personal category', description='Retained description')
        cat.extras['future_category'] = {'keep': True}; categories.save_user()
        db = TagDatabase(root / 'data/user/tags.json', rule_pair_admission=connection.native.RulePairAdmission(root / 'data/user'))
        tag = db.add_known_tag('Personal tag', category=cat.id)
        tag.notes = 'Retained note'; tag.extras['future_user'] = {'nested': [1, 2]}; db.save()
    choice.transactions.reload()
    return cat


def test_cold_reopen_has_empty_default_baseline_and_retains_live_personal_records(selected):
    settings, _source, root = selected
    choice = connect(selected); populate(choice)
    service = EncyclopediaCatalog(settings)
    opened = service.reopen_source().choice
    assert not opened.installed and opened.observation_status == 'current'
    assert not opened.base_release.revision.snapshot.tags
    assert opened.native_adapter.capture(1).snapshot.tags
    review = service.preview_import()
    assert review.database.mode == 'merged' and review.database.installed_version is None
    assert review.can_apply, review.holds
    before = {p: p.read_bytes() for p in (root / 'data/user').glob('*.json')}
    applied = service.apply(review, confirmed_token=review.token)
    assert applied.publication and any(t.display == 'Personal tag' for t in applied.publication.snapshot.tags)
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('member', ['opaque.txt', 'nested', 'future.json', 'tracking'])
def test_any_untracked_default_member_or_tracking_holds(selected, member):
    settings, _source, root = selected
    choice = connect(selected)
    if member == 'tracking':
        (root / 'data/user/default_knowledge_state.json').write_text('{"schema_version":999,"unknown":true}')
    elif member == 'nested':
        (root / 'data/defaults/nested').mkdir()
    else:
        (root / 'data/defaults' / member).write_text('{}')
    assert not empty_default_layer(choice.native_adapter)
    service = EncyclopediaCatalog(settings); service.choice = choice
    with pytest.raises((ValueError, RuntimeError, OSError)):
        service.preview_import()
    assert not tuple((root / 'backups/knowledge-pack-import').glob('*/recovery.json'))


def test_membership_drift_during_guarded_empty_observation_holds(selected, monkeypatch):
    _settings, _source, root = selected
    choice = connect(selected); original = choice.native_adapter.default_content
    def drift(*args):
        result = original(*args)
        (root / 'data/defaults/late-opaque.txt').write_text('unexpected')
        return result
    monkeypatch.setattr(choice.native_adapter, 'default_content', drift)
    with pytest.raises(RuntimeError, match='membership changed'):
        empty_default_layer(choice.native_adapter)


def test_overlapping_untracked_personal_identity_stays_protected_hold(selected):
    settings, _source, root = selected
    choice = connect(selected); cat = populate(choice)
    incoming = choice.available_release.revision.snapshot.tags[0]
    with choice.native_adapter.guard():
        db = TagDatabase(root / 'data/user/tags.json', rule_pair_admission=connection.native.RulePairAdmission(root / 'data/user'))
        db.tags[incoming.id] = TagRecord(incoming.id, 'Personal override', cat.id, notes='Keep this')
        db.save()
    choice.transactions.reload()
    before = (root / 'data/user/tags.json').read_bytes()
    service = EncyclopediaCatalog(settings); service.reopen_source()
    review = service.preview_import()
    assert not review.can_apply and any('collision' in str(h) for h in review.holds)
    with pytest.raises((ValueError, RuntimeError)):
        service.apply(review, confirmed_token=review.token)
    assert (root / 'data/user/tags.json').read_bytes() == before


def test_foundation_effective_mismatch_still_holds(selected, monkeypatch):
    settings, _source, _root = selected
    choice = connect(selected); service = EncyclopediaCatalog(settings); service.choice = choice
    original = choice.transactions.host.plan_default_release
    # Mutate only an injected proposal DTO, never source bytes or native engine.
    object.__setattr__(choice.transactions.host, 'plan_default_release',
        lambda *args: replace(original(*args), foundation_effective_tags=()))
    review = service.preview_import()
    assert not review.can_apply and any('differs' in str(h) for h in review.holds)


def test_arbitrary_default_entry_after_preview_holds_before_apply(selected):
    settings, _source, root = selected
    choice = connect(selected); populate(choice)
    service = EncyclopediaCatalog(settings); service.reopen_source()
    review = service.preview_import()
    assert review.can_apply
    before = {p: p.read_bytes() for p in (root / 'data/user').glob('*.json')}
    (root / 'data/defaults/untracked-directory').mkdir()
    with pytest.raises(RuntimeError, match='membership or tracking changed'):
        service.apply(review, confirmed_token=review.token)
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not tuple((root / 'backups/knowledge-pack-import').glob('*/recovery.json'))


def poll(controller, executor):
    end = time.monotonic() + 30
    while (controller.pending or executor.close_held) and time.monotonic() < end:
        controller.poll(); time.sleep(.005)
    controller.poll()
    assert not controller.pending and not executor.close_held


def test_original_controller_cold_review_confirm_apply_and_reload_personal_catalog(selected):
    settings, _source, root = selected
    choice = connect(selected); populate(choice)
    executor = ResourceExecutorOwner()
    controller = TagDatabaseController(executor=executor, settings_path=settings, apply_authorized=True)
    assert controller.connect('connection_open'); poll(controller, executor)
    assert not controller.error and not controller.choice.installed
    assert controller.request('preview'); poll(controller, executor)
    assert controller.review.can_apply and controller.review.database.mode == 'merged'
    before = (root / 'data/user/tags.json').read_bytes()
    assert controller.request('apply', confirmed=True); poll(controller, executor)
    assert not controller.error and controller.result.publication
    assert (root / 'data/user/tags.json').read_bytes() == before
    assert controller.request('reload'); poll(controller, executor)
    assert not controller.error and (root / 'data/user/tags.json').read_bytes() == before
    controller.begin_close(); executor.begin_close()
