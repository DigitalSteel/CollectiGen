"""Synthetic selected catalogs; no global profiles or production database reads."""
import copy
import json
from uuid import uuid4

import pytest

from core import knowledge_pack
from core.encyclopedia_editing import EncyclopediaEditingHold, EncyclopediaEditingSession
from core.tag_record_save import TagDetailsSaveRequest, TagRecordSaveRequest
from suite_core.catalog_identity_candidate import CatalogIdentity, encode_identity
from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter


def editing_catalog(tmp_path):
    """One explicitly synthetic native catalog, all bytes inside tmp_path."""
    root = tmp_path / "synthetic-catalog"
    defaults, user = root / "data/defaults", root / "data/user"
    defaults.mkdir(parents=True)
    user.mkdir()
    identity = CatalogIdentity(str(uuid4()), str(uuid4()))
    (user / "catalog_identity.json").write_bytes(encode_identity(identity))
    documents = {
        defaults / "categories.json": {"schema_version": 6, "categories": [
            {"id": "animals", "name": "Animals", "description": "Synthetic",
             "ordering": {"group": "subject_traits", "priority": 500, "reviewed": True}},
            {"id": "scene", "name": "Scene",
             "ordering": {"group": "scene", "priority": 200, "reviewed": True}},
        ]},
        user / "categories.json": {"schema_version": 6, "categories": [], "future": {"keep": 1}},
        user / "tags.json": {"schema_version": 9, "tags": [
            {"id": "fox", "display": "fox", "category": "animals", "notes": "Synthetic note",
             "future_record": {"keep": [1, 2]}},
        ], "merge_rules": [], "future_root": {"keep": True}},
        defaults / "patterns.json": {"schema_version": 1, "patterns": []},
        user / "patterns.json": {"schema_version": 1, "patterns": [], "future_patterns": True},
        defaults / "modifiers.json": {"groups": {}},
        defaults / "concepts.json": {"concepts": []},
        defaults / "relationships.json": {"schema_version": 1, "relationships": []},
        user / "relationships.json": {"schema_version": 1, "relationships": []},
    }
    for path, value in documents.items():
        path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    return NativeTagCatalogAdapter(root, native=knowledge_pack,
        catalog_id=identity.publication_catalog_id, source_reference="Synthetic editor fixture")


@pytest.fixture
def editor(tmp_path):
    return EncyclopediaEditingSession.open(editing_catalog(tmp_path))


@pytest.mark.parametrize("namespace", ["category-workspace", "tag-order", "tag-autosave"])
def test_pending_recovery_holds_open_and_direct_category_writer(tmp_path, namespace):
    adapter = editing_catalog(tmp_path)
    session = EncyclopediaEditingSession.open(adapter)
    session.create_category("Synthetic pending draft")
    receipt = adapter.root / "data/user/backups" / namespace / "synthetic" / "recovery.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"pending","files":[]}', encoding="utf-8")
    before = session.categories.user_path.read_bytes()
    with pytest.raises(EncyclopediaEditingHold, match="recovery"):
        session.category_session.save()
    with pytest.raises(EncyclopediaEditingHold, match="recovery"):
        EncyclopediaEditingSession.open(adapter)
    assert receipt.read_text(encoding="utf-8") == '{"status":"pending","files":[]}'
    assert session.categories.user_path.read_bytes() == before


@pytest.mark.parametrize("hold", ["recovery", "identity", "closed", "reload"])
def test_original_native_writer_runs_only_after_selected_admission(editor, hold):
    ran = []
    if hold == "recovery":
        receipt = editor.root / "data/user/backups/category-workspace/synthetic/recovery.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"status":"pending"}',encoding="utf-8")
    elif hold == "identity":
        path = editor.root / "data/user/catalog_identity.json"
        path.write_bytes(encode_identity(CatalogIdentity(str(uuid4()),str(uuid4()))))
    elif hold == "closed":
        editor._closed = True
    else:
        editor.reload_required = True
    with pytest.raises((RuntimeError, ValueError)):
        editor.run_native_operation(lambda: ran.append(True),kind="modifier_policy")
    assert not ran


def test_missing_character_authority_never_initializes_during_unreviewed_setup(editor):
    assert editor.category_session.characters is editor.characters
    paths = (editor.characters.series_path,editor.characters.characters_path,editor.characters.outfits_path)
    assert not any(p.exists() for p in paths)
    with pytest.raises(EncyclopediaEditingHold,match="initialize"):
        editor.run_native_operation(lambda: pytest.fail("Unreviewed setup entered its writer"),kind="character_setup")
    with pytest.raises(EncyclopediaEditingHold,match="initialize"):
        editor.category_session.prepare_tag_retirement(("fox",),reason="Synthetic review")
    with pytest.raises(EncyclopediaEditingHold,match="initialize"):
        editor.category_session.preview_tag_removal(("fox",))
    with pytest.raises(EncyclopediaEditingHold,match="initialize"):
        editor.category_session.remove_tags(("fox",))
    assert not any(p.exists() for p in paths)


@pytest.mark.parametrize("hold", ["closed", "reload", "defaults", "busy"])
def test_borrowed_category_writer_enforces_full_outer_gate(editor, hold):
    editor.create_category("Synthetic category")
    before = editor.categories.user_path.read_bytes()
    if hold == "closed":
        editor._closed = True
    elif hold == "reload":
        editor.reload_required = True
    elif hold == "busy":
        editor.busy = True
    else:
        (editor.root / "data/defaults/modifiers.json").write_text('{"groups":{},"changed":true}', encoding="utf-8")
    with pytest.raises(EncyclopediaEditingHold):
        editor.category_session.save()
    assert editor.categories.user_path.read_bytes() == before


def tag_request(editor, record, *, created=False, mode="record", changes=None):
    return TagDetailsSaveRequest(TagRecordSaveRequest(
        editor.tags.path, editor.tags._loaded_bytes,
        json.dumps(editor.tags._record_payload(record), ensure_ascii=False), created,
        editor.tags.rule_pair_admission,
    ), editor.authorities, mode, json.dumps(changes or {}), prepare_palette=False)


def test_native_open_is_read_only_and_snapshot_refused(tmp_path):
    adapter = editing_catalog(tmp_path)
    before = {p: p.read_bytes() for p in adapter.root.rglob("*.json")}
    session = EncyclopediaEditingSession.open(adapter)
    assert {p: p.read_bytes() for p in adapter.root.rglob("*.json")} == before
    assert not (adapter.root / "tag_catalog_snapshot.json").exists()
    assert not list(adapter.root.glob("backups/*"))
    from suite_core.tests.test_encyclopedia import fixture
    with pytest.raises(EncyclopediaEditingHold, match="read-only"):
        EncyclopediaEditingSession.open(fixture())
    assert session.close()


def test_tag_create_edit_save_and_reopen_preserve_unknowns(editor):
    detached = copy.deepcopy(editor.tags)
    record = detached.add_known_tag("synthetic wolf", category="animals")
    editor.save_tag_request(tag_request(editor, record, created=True))
    reopened = EncyclopediaEditingSession.open(editor.adapter)
    saved = copy.deepcopy(reopened.tags.tags[record.id])
    saved.notes = "Changed synthetic note"
    saved.category = "scene"
    reopened.save_tag_request(tag_request(reopened, saved))
    final = EncyclopediaEditingSession.open(editor.adapter)
    assert final.tags.tags[record.id].notes == "Changed synthetic note"
    assert final.tags.tags[record.id].category == "scene"
    raw = json.loads(final.tags.path.read_bytes())
    assert raw["future_root"] == {"keep": True}
    assert next(r for r in raw["tags"] if r["id"] == "fox")["future_record"] == {"keep": [1, 2]}
    assert list(final.tags.path.parent.glob("backups/tag-save/*")) or list(final.tags.path.parent.glob("backups/*"))


def test_tag_invalid_duplicate_stale_and_cross_catalog_requests_refuse(editor, tmp_path):
    saved = copy.deepcopy(editor.tags.tags["fox"])
    before = editor.tags.path.read_bytes()
    saved.display = ""
    with pytest.raises(ValueError, match="name"):
        editor.save_tag_request(tag_request(editor, saved))
    saved.display = "fox"
    saved.category = "absent"
    with pytest.raises(ValueError, match="category"):
        editor.save_tag_request(tag_request(editor, saved))
    saved.id = "another"
    saved.category = "animals"
    with pytest.raises(ValueError, match="another stable"):
        editor.save_tag_request(tag_request(editor, saved, created=True))
    other = EncyclopediaEditingSession.open(editing_catalog(tmp_path / "other"))
    with pytest.raises(EncyclopediaEditingHold, match="different or stale"):
        editor.save_tag_request(tag_request(other, other.tags.tags["fox"]))
    assert editor.tags.path.read_bytes() == before
    request = tag_request(editor, editor.tags.tags["fox"])
    editor.tags.path.write_bytes(before + b" ")
    with pytest.raises(EncyclopediaEditingHold, match="changed after opening"):
        editor.save_tag_request(request)
    assert editor.tags.path.read_bytes() == before + b" "


def test_canonical_rename_requires_original_alias_review_and_roundtrips(editor):
    record = copy.deepcopy(editor.tags.tags["fox"])
    record.display = "red fox"
    with pytest.raises(EncyclopediaEditingHold, match="Other Names review"):
        editor.save_tag_request(tag_request(editor, record))
    editor.save_tag_request(tag_request(editor, record, mode="aliases", changes={"aliases": ["fox"]}))
    final = EncyclopediaEditingSession.open(editor.adapter)
    assert final.tags.tags["fox"].display == "red fox"
    assert final.services["tag_alias_service"].resolve("fox").tag_id == "fox"


def test_category_create_edit_reparent_saved_reopen_and_paths(editor):
    parent = editor.create_category("Synthetic Parent", parent_id="animals")
    child = editor.create_category("Synthetic Child", parent_id=parent.id)
    editor.save_categories()
    editor.update_category(parent.id, name="Renamed Parent", parent_id="scene", description="Saved description")
    editor.save_categories()
    final = EncyclopediaEditingSession.open(editor.adapter)
    assert final.categories.get(parent.id).name == "Renamed Parent"
    assert final.categories.get(child.id).parent_id == parent.id
    assert ["scene", parent.id, child.id] == final.category_session.path_ids(child.id)
    assert "Renamed Parent" in final.category_session.path_label(child.id)
    assert final.categories.effective_ordering(child.id)[0] == "scene"
    assert json.loads(final.categories.user_path.read_bytes())["future"] == {"keep": 1}


def test_category_validation_cancel_dirty_close_and_stale(editor):
    before = editor.categories.user_path.read_bytes()
    with pytest.raises(ValueError, match="name"):
        editor.create_category(" ")
    parent = editor.create_category("Parent", parent_id="animals")
    child = editor.create_category("Child", parent_id=parent.id)
    with pytest.raises(ValueError, match="already exists"):
        editor.create_category(" parent ", parent_id="animals")
    with pytest.raises(ValueError, match="descendants"):
        editor.update_category(parent.id, name="Parent", parent_id=child.id)
    assert editor.has_changes and not editor.close()
    with pytest.raises(EncyclopediaEditingHold, match="Discard"):
        editor.reload()
    with pytest.raises(EncyclopediaEditingHold, match="Category changes"):
        editor.save_tag_request(tag_request(editor, editor.tags.tags["fox"]))
    editor.discard()
    assert not editor.has_changes and editor.categories.user_path.read_bytes() == before
    editor.create_category("Draft")
    editor.categories.user_path.write_bytes(before + b" ")
    with pytest.raises(EncyclopediaEditingHold, match="changed after opening"):
        editor.save_categories()
    assert editor.categories.user_path.read_bytes() == before + b" "
    editor.reload(discard=True)
    assert not editor.has_changes and editor.close()


def test_failed_save_holds_until_explicit_reload_without_recovery(editor, monkeypatch):
    before = editor.tags.path.read_bytes()
    import core.encyclopedia_editing as module
    monkeypatch.setattr(module, "save_tag_details", lambda _request: (_ for _ in ()).throw(OSError("synthetic refusal")))
    with pytest.raises(OSError, match="synthetic refusal"):
        editor.save_tag_request(tag_request(editor, editor.tags.tags["fox"]))
    assert editor.reload_required and not editor.busy
    assert editor.tags.path.read_bytes() == before
    with pytest.raises(EncyclopediaEditingHold, match="recovery holds"):
        editor.create_category("Blocked")
    editor.reload()
    assert not editor.reload_required


def test_pending_rule_conversion_holds_before_editor_load(editor):
    pending = editor.tags.rule_pair_admission.pending_root
    pending.mkdir()
    (pending / "synthetic-pending").write_text("opaque synthetic hold")
    before = editor.tags.path.read_bytes()
    with pytest.raises(RuntimeError, match="pending"):
        EncyclopediaEditingSession.open(editor.adapter)
    assert editor.tags.path.read_bytes() == before


def test_native_tag_readback_failure_restores_bytes_and_retains_receipt(editor, monkeypatch):
    from core.tag_database import TagDatabase
    before = editor.tags.path.read_bytes()
    real_save = TagDatabase.save
    def corrupt_after_save(database):
        real_save(database)
        payload = json.loads(database.path.read_bytes())
        payload["tags"][0]["notes"] = "Synthetic corrupt readback"
        database.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(TagDatabase, "save", corrupt_after_save)
    record = copy.deepcopy(editor.tags.tags["fox"])
    record.notes = "Requested synthetic edit"
    with pytest.raises(RuntimeError, match="read-back verification"):
        editor.save_tag_request(tag_request(editor, record))
    assert editor.tags.path.read_bytes() == before
    manifests = list((editor.tags.path.parent / "backups/tag-autosave").glob("*/recovery.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_bytes())["status"] == "restored"
    assert editor.reload_required
    editor.reload()
    assert editor.tags.tags["fox"].notes == "Synthetic note"


def test_category_protected_role_cannot_reparent(editor):
    record = editor.create_category("Synthetic Protected", stable_id="synthetic-protected",
                                    extras={"semantic_role": "character_identity", "protected": True})
    with pytest.raises(ValueError, match="protected system role"):
        editor.update_category(record.id, name="Harmless name", parent_id="scene")


def test_catalog_identity_replacement_holds_existing_session(editor):
    path = editor.root / "data/user/catalog_identity.json"
    path.write_bytes(encode_identity(CatalogIdentity(str(uuid4()), str(uuid4()))))
    before = editor.tags.path.read_bytes()
    with pytest.raises(RuntimeError, match="identity differs|incarnation changed"):
        editor.create_category("Blocked after identity replacement")
    assert editor.tags.path.read_bytes() == before


def test_default_layer_name_owner_cannot_be_duplicated_in_user_rows(editor):
    path = editor.root / "data/defaults/foundation_tags.json"
    path.write_text(json.dumps({"schema_version": 11, "tags": [
        {"id": "shipped-wolf", "display": "Synthetic default wolf", "category": "animals"},
    ]}), encoding="utf-8")
    fresh = EncyclopediaEditingSession.open(editor.adapter)
    detached = copy.deepcopy(fresh.tags)
    record = detached.add_known_tag("Synthetic default wolf", category="animals")
    before = fresh.tags.path.read_bytes()
    with pytest.raises(ValueError, match="another stable"):
        fresh.save_tag_request(tag_request(fresh, record, created=True))
    assert fresh.tags.path.read_bytes() == before


def test_missing_lifecycle_envelope_holds_without_native_writes(editor):
    path = editor.tags.path
    payload = json.loads(path.read_bytes())
    payload["schema_version"] = editor.tags.ENROLLED_SCHEMA_VERSION
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    held = EncyclopediaEditingSession.open(editor.adapter)
    assert held.tags.lifecycle_write_hold
    with pytest.raises(EncyclopediaEditingHold, match="lifecycle authority"):
        held.create_category("Cannot write through damaged lifecycle")
    assert path.read_bytes() == before


def test_borrowed_category_session_save_retains_selected_identity_pin(editor):
    # The stock UI calls its session writer directly, without the service facade.
    editor.create_category("Held borrowed draft")
    path = editor.root / "data/user/catalog_identity.json"
    path.write_bytes(encode_identity(CatalogIdentity(str(uuid4()), str(uuid4()))))
    before = editor.categories.user_path.read_bytes()
    with pytest.raises(RuntimeError, match="identity differs|incarnation changed"):
        editor.category_session.save()
    assert editor.categories.user_path.read_bytes() == before
