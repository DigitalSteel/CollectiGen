"""Native synthetic recipients only; reuse production source/empty catalog setup."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from core.encyclopedia_catalog import EncyclopediaCatalog
from core import tag_catalog_connection as connection
from suite_core.catalog_identity_candidate import encode_identity
from test_tag_catalog_connection import selected, connect
from test_tag_database_pack_producer import reviewed_local_input


def authority(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}


def test_own_database_without_source_has_no_authority_or_publication_writes(selected):
    settings, source, root = selected
    connect(selected)
    before = authority(root)
    settings_before = settings.read_bytes()
    source.rename(source.with_name(source.name + "-unavailable"))
    service = EncyclopediaCatalog(settings)
    result = service.open_catalog(root)
    assert result.root == root and result.publication.catalog_id == result.identity.publication_catalog_id
    assert authority(root) == before and settings.read_bytes() == settings_before
    assert service.open_catalog(root).publication == result.publication
    assert authority(root) == before


def test_unknown_and_legacy_database_retains_original_view(selected, tmp_path):
    settings, _source, root = selected
    connect(selected)
    service = EncyclopediaCatalog(settings)
    first = service.open_catalog(root)
    legacy = tmp_path / "legacy"; legacy.mkdir()
    data = legacy / "data/user"; data.mkdir(parents=True)
    (data / "tags.json").write_text('{"unknown_future":[1]}')
    before = authority(legacy)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        service.open_catalog(legacy)
    assert service.view is first and authority(legacy) == before


def test_identity_replacement_holds_previous_view(selected):
    settings, _source, root = selected
    connect(selected)
    service = EncyclopediaCatalog(settings)
    first = service.open_catalog(root)
    path = root / "data/user/catalog_identity.json"
    path.write_bytes(encode_identity(replace(first.identity, incarnation_id=str(uuid4()))))
    before = authority(root)
    with pytest.raises((ValueError, RuntimeError)):
        service.open_catalog(root)
    assert service.view is first and authority(root) == before


def test_optional_pack_uses_reviewed_native_import_and_preserves_user_records(selected):
    settings, source, root = selected
    settings.write_text('{"future_setting":{"keep":[1,2]}}')
    service = EncyclopediaCatalog(settings)
    review = service.review_source(source, root, create_new=True)
    assert not root.exists()
    result = service.confirm_source(review, confirmed_token=review.token)
    assert result.choice.native_adapter.root == root
    initial = (root / "data/user/tags.json").read_bytes()
    preview = service.preview_import()
    assert preview.can_apply, preview.holds
    applied = service.apply(preview, confirmed_token=preview.token)
    assert applied.publication is not None and not applied.publication_error
    assert service.open_catalog(root).publication.snapshot.tags
    assert (root / "data/user/tags.json").read_bytes() == initial
    assert json.loads(settings.read_bytes())["future_setting"] == {"keep": [1, 2]}
    with pytest.raises((ValueError, RuntimeError)):
        service.apply(preview, confirmed_token=preview.token)


def test_committed_import_failed_publication_reload_never_reimports(selected, monkeypatch):
    settings, source, root = selected
    service = EncyclopediaCatalog(settings)
    review = service.review_source(source, root, create_new=True)
    service.confirm_source(review, confirmed_token=review.token)
    transactions = service.choice.transactions
    original = transactions.reload
    monkeypatch.setattr(transactions, "reload", lambda **kwargs: (_ for _ in ()).throw(OSError("publication blocked")))
    preview = service.preview_import()
    result = service.apply(preview, confirmed_token=preview.token)
    assert result.publication is None and "publication blocked" in result.publication_error
    before = authority(root)
    monkeypatch.setattr(transactions, "reload", original)
    monkeypatch.setattr(service.choice.native_adapter, "apply", lambda *args: pytest.fail("Reload cannot reimport"))
    published = service.reload()
    assert published.snapshot.tags
    after = authority(root)
    changed = {p for p in before if before[p] != after[p]}
    assert changed <= {root.relative_to(root) / "tag_catalog_snapshot.json"}


def test_source_review_drift_holds_before_catalog_creation(selected):
    settings, source, root = selected
    service = EncyclopediaCatalog(settings)
    review = service.review_source(source, root, create_new=True)
    (source / "TagDatabase.zip").write_bytes(b"unexpected source replacement")
    with pytest.raises((ValueError, RuntimeError)):
        service.confirm_source(review, confirmed_token=review.token)
    assert not root.exists() and not settings.exists()


def test_own_database_retains_native_user_notes_and_unknown_fields(selected):
    from core.tag_database import TagDatabase
    settings, source, root = selected
    service = EncyclopediaCatalog(settings)
    review = service.review_source(source, root, create_new=True)
    service.confirm_source(review, confirmed_token=review.token)
    preview = service.preview_import()
    service.apply(preview, confirmed_token=preview.token)
    view = service.open_catalog(root)
    adapter = service.choice.native_adapter
    with adapter.guard():
        db = TagDatabase(root / 'data/user/tags.json',
            rule_pair_admission=connection.native.RulePairAdmission(root / 'data/user'))
        record = db.add_known_tag('Personal synthetic tag', category=view.publication.snapshot.categories[0].id)
        record.notes = 'Retained local note'
        record.extras['future_field'] = {'retain': [1, 2]}
        db.save()
    before = authority(root)
    source.rename(source.with_name(source.name + '-offline'))
    reopened = EncyclopediaCatalog(settings).open_catalog(root)
    assert any(tag.display == 'Personal synthetic tag' for tag in reopened.publication.snapshot.tags)
    assert authority(root) == before
    user = json.loads((root / 'data/user/tags.json').read_bytes())
    saved = next(t for t in user['tags'] if t['display'] == 'Personal synthetic tag')
    assert saved['notes'] == 'Retained local note' and saved['future_field'] == {'retain': [1, 2]}


def test_physical_recipient_replacement_holds_even_with_identical_identity_bytes(selected):
    import shutil
    settings, _source, root = selected
    connect(selected)
    service = EncyclopediaCatalog(settings)
    first = service.open_catalog(root)
    user = root / 'data/user'
    old = root / 'data/original-user'
    user.rename(old)
    shutil.copytree(old, user)
    before = authority(root)
    with pytest.raises((ValueError, RuntimeError), match='folder changed'):
        service.open_catalog(root)
    assert service.view is first and authority(root) == before


def test_default_import_over_populated_user_records_preserves_notes_unknown_fields(selected):
    from core.tag_database import TagDatabase
    from core.category_registry import CategoryRegistry
    settings, source, root = selected
    service = EncyclopediaCatalog(settings)
    review = service.review_source(source, root, create_new=True)
    service.confirm_source(review, confirmed_token=review.token)
    with service.choice.native_adapter.guard():
        categories = CategoryRegistry(root / 'data/defaults/categories.json', root / 'data/user/categories.json')
        category = categories.add('Personal category', description='Retained description')
        category.extras['future_category'] = {'keep': True}
        categories.save_user()
        db = TagDatabase(root / 'data/user/tags.json',
            rule_pair_admission=connection.native.RulePairAdmission(root / 'data/user'))
        record = db.add_known_tag('Personal tag before defaults', category=category.id)
        record.notes = 'Original local note'
        record.extras['future_user'] = {'keep': [1, 2]}
        db.save()
    before = {name: (root / 'data/user' / name).read_bytes() for name in ('tags.json', 'categories.json')}
    service.reload()  # Explicitly refresh the selected review after fixture edits.
    preview = service.preview_import()
    assert preview.can_apply, preview.holds
    result = service.apply(preview, confirmed_token=preview.token)
    assert not result.publication_error
    assert any(t.display == record.display for t in result.publication.snapshot.tags)
    assert all((root / 'data/user' / name).read_bytes() == raw for name, raw in before.items())
    assert (root / 'data/defaults/foundation_tags.json').exists()
