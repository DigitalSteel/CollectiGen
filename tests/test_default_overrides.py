from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.default_overrides import (
    DefaultOverrideItem,
    DefaultOverrideService,
    DefaultOverrideSnapshot,
    recover_interrupted_default_override_restores,
)
from core.default_sync import sync_promoted_defaults
from core.pattern_registry import PatternRegistry
from core.automation_rule_conversion import RulePairAdmission, RuleConversionAdmissionError


def _pending_conversion(user):
    pending = RulePairAdmission(user.absolute()).pending_root
    pending.mkdir(parents=True)
    (pending / "opaque-pending").write_bytes(b"Do not interpret this receipt")


def test_pending_conversion_blocks_default_refresh_before_sync(tmp_path, monkeypatch):
    root = _root(tmp_path)
    user = root / "data" / "user"
    before = {path: path.read_bytes() for path in user.glob("*.json")}
    _pending_conversion(user)
    monkeypatch.setattr("core.default_overrides.sync_promoted_defaults", lambda *_args:
                        pytest.fail("Pending conversion must refuse before default synchronization"))
    with pytest.raises(RuleConversionAdmissionError, match="recovery is pending"):
        DefaultOverrideService(root).scan()
    assert {path: path.read_bytes() for path in user.glob("*.json")} == before
    assert not (user / "backups").exists()


def test_pending_conversion_blocks_reviewed_restore_before_backup(tmp_path):
    root = _root(tmp_path)
    user = root / "data" / "user"
    service = DefaultOverrideService(root)
    service.scan()
    tags = json.loads((user / "tags.json").read_bytes())
    tags["tags"][0]["notes"] = "Personal note"
    _write(user / "tags.json", tags)
    snapshot = service.scan()
    choice = next(item for item in snapshot.items if item.field == "notes")
    before = {path: path.read_bytes() for path in user.glob("*.json")}
    _pending_conversion(user)
    with pytest.raises(RuleConversionAdmissionError):
        service.restore([choice.key], authority_fingerprint=snapshot.authority_fingerprint)
    assert {path: path.read_bytes() for path in user.glob("*.json")} == before
    assert not (user / "backups" / "default-overrides").exists()


def test_default_restore_recovery_reuses_startup_admission(tmp_path):
    root = _root(tmp_path)
    user = root / "data" / "user"
    with RulePairAdmission(user.absolute()).admit():
        assert recover_interrupted_default_override_restores(user) == []
        assert isinstance(DefaultOverrideService(root).scan(), DefaultOverrideSnapshot)


def test_direct_restore_recovery_refuses_pending_conversion_without_receipt_changes(tmp_path):
    root = _root(tmp_path)
    user = root / "data" / "user"
    target = user / "patterns.json"
    target.write_bytes(b'{"patterns": [{"id": "changed"}]}')
    backup = user / "backups" / "default-overrides" / "pending"
    backup.mkdir(parents=True)
    original = b'{"patterns": []}'
    (backup / "0_patterns.json").write_bytes(original)
    _write(backup / "recovery.json", {"schema_version": 1, "status": "pending", "files": [
        {"target": str(target.resolve()), "backup": "0_patterns.json", "existed": True}]})
    before = target.read_bytes(), (backup / "recovery.json").read_bytes()
    _pending_conversion(user)
    with pytest.raises(RuleConversionAdmissionError):
        recover_interrupted_default_override_restores(user)
    assert (target.read_bytes(), (backup / "recovery.json").read_bytes()) == before
    assert (backup / "0_patterns.json").read_bytes() == original


@pytest.mark.parametrize("fail_sync", [False, True])
def test_restore_holds_one_exclusion_through_sync_verification_rollback_and_receipt(tmp_path, monkeypatch, fail_sync):
    import core.default_overrides as overrides
    root = _root(tmp_path)
    user = root / "data" / "user"
    service = DefaultOverrideService(root)
    service.scan()
    tags = json.loads((user / "tags.json").read_bytes())
    tags["tags"][0]["notes"] = "Personal note"
    _write(user / "tags.json", tags)
    snapshot = service.scan()
    choice = next(item for item in snapshot.items if item.field == "notes")
    before = {path: path.read_bytes() for path in user.glob("*.json")}
    real_admit = RulePairAdmission.admit
    real_sync, real_json, real_bytes = overrides.sync_promoted_defaults, overrides._atomic_json, overrides._atomic_bytes
    depth, saw_sync, saw_rollback = [0], [False], [False]

    @contextmanager
    def track_admission(bound):
        with real_admit(bound):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    def sync_under_exclusion(*args):
        assert depth[0] == 1
        saw_sync[0] = True
        if fail_sync:
            raise OSError("synthetic sync failure")
        return real_sync(*args)

    def write_json_under_exclusion(path, value):
        assert depth[0] == 1
        return real_json(path, value)

    def restore_bytes_under_exclusion(path, value):
        assert depth[0] == 1
        saw_rollback[0] = True
        return real_bytes(path, value)

    monkeypatch.setattr(RulePairAdmission, "admit", track_admission)
    monkeypatch.setattr(overrides, "sync_promoted_defaults", sync_under_exclusion)
    monkeypatch.setattr(overrides, "_atomic_json", write_json_under_exclusion)
    monkeypatch.setattr(overrides, "_atomic_bytes", restore_bytes_under_exclusion)
    if fail_sync:
        with pytest.raises(OSError, match="synthetic sync failure"):
            service.restore([choice.key], authority_fingerprint=snapshot.authority_fingerprint)
        assert {path: path.read_bytes() for path in user.glob("*.json")} == before
        assert saw_rollback[0]
    else:
        service.restore([choice.key], authority_fingerprint=snapshot.authority_fingerprint)
        assert json.loads((user / "tags.json").read_bytes())["tags"][0]["notes"] == "Shipped note"
    assert depth[0] == 0 and saw_sync[0]
    manifest = next((user / "backups" / "default-overrides").glob("*/recovery.json"))
    assert json.loads(manifest.read_bytes())["status"] == ("restored" if fail_sync else "complete")


def test_defaults_restore_ui_admission_refusal_does_not_claim_rollback(tmp_path, monkeypatch, qt_test_scope):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    import qt_app.knowledge_page as page_module
    root = _root(tmp_path)
    user = root / "data" / "user"
    service = DefaultOverrideService(root)
    service.scan()
    tags = json.loads((user / "tags.json").read_bytes())
    tags["tags"][0]["notes"] = "Personal note"
    _write(user / "tags.json", tags)
    snapshot = service.scan()
    key = next(item.key for item in snapshot.items if item.field == "notes")
    before = (user / "tags.json").read_bytes()
    monkeypatch.setattr(page_module, "APP_ROOT", root)
    messages = []
    monkeypatch.setattr(page_module.QMessageBox, "critical", lambda _parent, title, text: messages.append((title, text)))

    class ReviewedDialog:
        restore_complete_records = False
        def __init__(self, *_args):
            pass
        def exec(self):
            return QDialog.Accepted
        def selected_keys(self):
            return (key,)

    def confirm_then_hold(*_args):
        _pending_conversion(user)
        return page_module.QMessageBox.Yes

    monkeypatch.setattr(page_module, "DefaultOverridesDialog", ReviewedDialog)
    monkeypatch.setattr(page_module.QMessageBox, "question", confirm_then_hold)
    page = SimpleNamespace(_flush_tag_save=lambda: None, _tag_save_pending=False,
                           _category_staging_active=lambda: False,
                           editor_context=SimpleNamespace(app_root=root))
    page_module.KnowledgePage.show_default_overrides(page)
    assert len(messages) == 1
    assert "No restore changes or backups were made" in messages[0][1]
    assert "recovered after" not in messages[0][1]
    assert (user / "tags.json").read_bytes() == before
    assert not (user / "backups" / "default-overrides").exists()


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    defaults.mkdir(parents=True)
    user.mkdir(parents=True)
    _write(defaults / "foundation_tags.json", {
        "schema_version": 1,
        "tags": [{
            "id": "blue", "display": "blue", "category": "colors",
            "aliases": ["azure"], "notes": "Shipped note",
        }],
        "merge_rules": [],
    })
    _write(defaults / "community_tags.json", {"schema_version": 1, "tags": []})
    _write(defaults / "promoted_tags.json", {
        "schema_version": 1, "tags": [], "merge_rules": [],
        "removed_tag_ids": [], "merged_tag_ids": {},
    })
    _write(defaults / "categories.json", {
        "schema_version": 5,
        "categories": [{
            "id": "colors", "name": "Colors", "description": "Shipped description",
            "color": "#112233", "color_policy": "own",
        }],
    })
    _write(defaults / "patterns.json", {
        "schema_version": 1,
        "patterns": [{
            "id": "color_word", "name": "Color word", "regex": "blue",
            "category": "colors", "enabled": True,
        }],
    })
    for filename, key in (
        ("series.json", "series"),
        ("characters.json", "characters"),
        ("outfits.json", "outfits"),
    ):
        _write(defaults / filename, {"schema_version": 1, key: []})
    return root


def test_default_sync_tracks_fields_and_refreshes_only_unoverridden_values(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    sync_promoted_defaults(defaults, user)

    tags = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    tags["tags"][0]["category"] = "personal_colors"
    tags["tags"][0]["aliases"] = []
    _write(user / "tags.json", tags)
    categories = {
        "schema_version": 5,
        "categories": [{
            "id": "colors", "name": "My Colors", "description": "Shipped description",
            "color": "#112233", "color_policy": "own",
        }],
        "disabled_default_ids": [],
    }
    _write(user / "categories.json", categories)
    sync_promoted_defaults(defaults, user)

    foundation = json.loads((defaults / "foundation_tags.json").read_text(encoding="utf-8"))
    foundation["tags"][0]["aliases"] = ["azure", "cerulean"]
    foundation["tags"][0]["notes"] = "Updated shipped note"
    foundation["tags"][0]["future_shipped_field"] = {"available": True}
    _write(defaults / "foundation_tags.json", foundation)
    category_defaults = json.loads((defaults / "categories.json").read_text(encoding="utf-8"))
    category_defaults["categories"][0]["description"] = "Updated description"
    category_defaults["categories"][0]["color"] = "#445566"
    _write(defaults / "categories.json", category_defaults)

    sync_promoted_defaults(defaults, user)

    tag = json.loads((user / "tags.json").read_text(encoding="utf-8"))["tags"][0]
    assert tag["category"] == "personal_colors"
    assert tag.get("aliases", []) == []
    assert tag["notes"] == "Updated shipped note"
    assert tag["future_shipped_field"] == {"available": True}
    category = json.loads((user / "categories.json").read_text(encoding="utf-8"))["categories"][0]
    assert category["name"] == "My Colors"
    assert category["description"] == "Updated description"
    assert category["color"] == "#445566"
    state = json.loads((user / "default_knowledge_state.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 6
    assert "default_hash" not in state["records"]["tags"]["blue"]
    assert "default_field_hashes" not in state["records"]["tags"]["blue"]
    assert (user / "default_knowledge_state.json").read_text(
        encoding="utf-8"
    ).count("\n") == 1
    assert state["records"]["tags"]["blue"]["override_fields"] == ["aliases", "category"]
    assert state["records"]["categories"]["colors"]["override_fields"] == ["name"]


def test_form_portrayal_category_update_preserves_personal_override(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    names = ("personification", "humanization", "animalization", "furrification")
    foundation = {
        "schema_version": 1,
        "tags": [
            {"id": name, "display": name, "category": "theme", "notes": "Known original form required."}
            for name in names
        ],
        "merge_rules": [],
    }
    _write(defaults / "foundation_tags.json", foundation)
    sync_promoted_defaults(defaults, user)
    personal = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    next(tag for tag in personal["tags"] if tag["id"] == "humanization")["category"] = "my_portrayals"
    _write(user / "tags.json", personal)
    for tag in foundation["tags"]:
        tag["category"] = "appearance_form"
    _write(defaults / "foundation_tags.json", foundation)
    sync_promoted_defaults(defaults, user)
    updated = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    assert {tag["id"]: tag["category"] for tag in updated["tags"]} == {
        name: "my_portrayals" if name == "humanization" else "appearance_form"
        for name in names
    }
    assert all(tag["notes"] == "Known original form required." for tag in updated["tags"])


def test_default_sync_upgrades_internal_ledger_when_inputs_are_unchanged(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    sync_promoted_defaults(defaults, user)
    state_path = user / "default_knowledge_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["schema_version"] = 5
    record = state["records"]["tags"]["blue"]
    record["default_hash"] = "retired"
    record["default_field_hashes"] = {"display": "retired"}
    _write(state_path, state)

    sync_promoted_defaults(defaults, user)

    upgraded = json.loads(state_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 6
    assert "default_hash" not in upgraded["records"]["tags"]["blue"]
    assert "default_field_hashes" not in upgraded["records"]["tags"]["blue"]
    assert state_path.read_text(encoding="utf-8").count("\n") == 1


def test_default_sync_accepts_legacy_tag_without_aliases(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    _write(user / "tags.json", {
        "schema_version": 1,
        "tags": [{
            "id": "blue", "display": "blue", "category": "colors",
            "notes": "Shipped note",
        }],
        "merge_rules": [],
    })

    sync_promoted_defaults(defaults, user)

    tag = json.loads((user / "tags.json").read_text(encoding="utf-8"))["tags"][0]
    assert tag["aliases"] == ["azure"]
    state = json.loads(
        (user / "default_knowledge_state.json").read_text(encoding="utf-8")
    )
    assert "aliases" not in state["records"]["tags"]["blue"]["override_fields"]


def test_default_sync_rehomes_shipped_tags_from_tombstoned_categories(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    categories = json.loads((defaults / "categories.json").read_text(encoding="utf-8"))
    categories["categories"].extend((
        {"id": "uncategorized", "name": "Uncategorized"},
        {"id": "pose", "name": "Pose"},
        {"id": "on_lap", "name": "Lap Support", "parent_id": "pose"},
        {"id": "scene", "name": "Scene"},
        {"id": "time", "name": "Time", "parent_id": "scene"},
    ))
    _write(defaults / "categories.json", categories)
    promoted = json.loads((defaults / "promoted_tags.json").read_text(encoding="utf-8"))
    promoted["tags"] = [
        {"id": "hand_on_lap", "display": "hand on lap", "category": "on_lap"},
        {"id": "crescent_moon", "display": "crescent moon", "category": "time"},
    ]
    _write(defaults / "promoted_tags.json", promoted)
    sync_promoted_defaults(defaults, user)

    category_user = {
        "schema_version": 5,
        "categories": [{"id": "personal_pose", "name": "Personal Pose"}],
        "disabled_default_ids": ["on_lap", "time"],
    }
    _write(user / "categories.json", category_user)
    existing_tags = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    next(
        item for item in existing_tags["tags"] if item["id"] == "hand_on_lap"
    )["category"] = "personal_pose"
    _write(user / "tags.json", existing_tags)
    promoted["tags"].append(
        {"id": "book_on_lap", "display": "book on lap", "category": "on_lap"}
    )
    _write(defaults / "promoted_tags.json", promoted)

    sync_promoted_defaults(defaults, user)

    tags = {
        item["id"]: item
        for item in json.loads((user / "tags.json").read_text(encoding="utf-8"))["tags"]
    }
    assert tags["hand_on_lap"]["category"] == "personal_pose"
    assert tags["book_on_lap"]["category"] == "pose"
    assert tags["crescent_moon"]["category"] == "scene"
    assert json.loads((user / "categories.json").read_text(encoding="utf-8"))[
        "disabled_default_ids"
    ] == ["on_lap", "time"]
    state = json.loads((user / "default_knowledge_state.json").read_text(encoding="utf-8"))
    assert "category" in state["records"]["tags"]["hand_on_lap"]["override_fields"]
    assert all(
        "category" not in state["records"]["tags"][tag_id]["override_fields"]
        for tag_id in ("book_on_lap", "crescent_moon")
    )


def test_default_override_review_restores_fields_and_complete_records(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    sync_promoted_defaults(defaults, user)
    tags = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    tags["tags"][0]["category"] = "personal_colors"
    tags["tags"][0]["notes"] = "Personal note"
    _write(user / "tags.json", tags)
    sync_promoted_defaults(defaults, user)

    service = DefaultOverrideService(root)
    snapshot = service.scan()
    category_item = next(item for item in snapshot.items if item.field == "category")
    result = service.restore(
        (category_item.key,), authority_fingerprint=snapshot.authority_fingerprint
    )
    assert result.restored_fields == 1
    restored = json.loads((user / "tags.json").read_text(encoding="utf-8"))["tags"][0]
    assert restored["category"] == "colors"
    assert restored["notes"] == "Personal note"

    refreshed = service.scan()
    note_item = next(item for item in refreshed.items if item.field == "notes")
    result = service.restore(
        (note_item.key,), authority_fingerprint=refreshed.authority_fingerprint,
        complete_records=True,
    )
    assert result.restored_records == 1
    restored = json.loads((user / "tags.json").read_text(encoding="utf-8"))["tags"][0]
    assert restored["notes"] == "Shipped note"
    assert "" not in json.loads((user / "tags.json").read_text(encoding="utf-8"))
    assert not service.scan().items


def test_default_review_classifies_differences_and_humanizes_structured_links(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    _write(defaults / "characters.json", {
        "schema_version": 1,
        "characters": [{"id": "hero", "name": "Hero"}],
    })
    sync_promoted_defaults(defaults, user)
    characters = json.loads((user / "characters.json").read_text(encoding="utf-8"))
    characters["characters"][0]["knowledge_tag"] = {
        "tag_id": "blue",
        "status": "reviewed",
        "basis": "preferred_name",
    }
    _write(user / "characters.json", characters)

    snapshot = DefaultOverrideService(root).scan()
    item = next(
        value for value in snapshot.items
        if value.kind == "character" and value.field == "knowledge_tag"
    )

    assert item.kind_label == "Character"
    assert item.difference_kind == "current_only"
    assert item.difference_label == "Current value only"
    assert item.current_value == "Reviewed representative Tag: blue (preferred name match)"
    assert not item.current_value.startswith("{")
    assert '"basis": "preferred_name"' in item.current_detail
    assert item.default_detail == "Not set"
    assert not item.can_reset_setting
    assert item not in snapshot.reset_items
    assert item in snapshot.audit_items


def test_removed_tags_stay_owned_by_removed_vocabulary_recovery(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    sync_promoted_defaults(defaults, user)
    tags = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    tags["removed_tag_ids"] = ["blue"]
    tags["tags"] = []
    _write(user / "tags.json", tags)

    snapshot = DefaultOverrideService(root).scan()

    assert not any(item.kind == "tag" and item.disabled for item in snapshot.items)


def test_default_override_restore_rolls_back_after_sync_failure(monkeypatch, tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    sync_promoted_defaults(defaults, user)
    tags = json.loads((user / "tags.json").read_text(encoding="utf-8"))
    tags["tags"][0]["notes"] = "Personal note"
    _write(user / "tags.json", tags)
    service = DefaultOverrideService(root)
    snapshot = service.scan()
    before = (user / "tags.json").read_bytes()
    item = next(value for value in snapshot.items if value.field == "notes")

    monkeypatch.setattr(
        "core.default_overrides.sync_promoted_defaults",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("forced sync failure")),
    )
    with pytest.raises(OSError, match="forced sync failure"):
        service.restore((item.key,), authority_fingerprint=snapshot.authority_fingerprint)

    assert (user / "tags.json").read_bytes() == before
    manifests = list((user / "backups" / "default-overrides").glob("*/recovery.json"))
    assert manifests
    assert json.loads(manifests[-1].read_text(encoding="utf-8"))["status"] == "restored"


def test_pending_default_override_restore_recovers_only_user_knowledge_files(tmp_path):
    root = _root(tmp_path)
    user = root / "data" / "user"
    target = user / "patterns.json"
    original = b'{"schema_version":1,"patterns":[]}\n'
    changed = b'{"schema_version":1,"patterns":[{"id":"changed"}]}\n'
    target.write_bytes(changed)
    backup = user / "backups" / "default-overrides" / "pending"
    backup.mkdir(parents=True)
    (backup / "0_patterns.json").write_bytes(original)
    _write(backup / "recovery.json", {
        "schema_version": 1, "status": "pending",
        "files": [{
            "target": str(target.resolve()), "backup": "0_patterns.json", "existed": True,
        }],
    })

    assert recover_interrupted_default_override_restores(user) == [backup]
    assert target.read_bytes() == original
    assert json.loads((backup / "recovery.json").read_text(encoding="utf-8"))["status"] == "restored"


def test_pattern_registry_preserves_disabled_shipped_pattern_decisions(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    _write(user / "patterns.json", {
        "schema_version": 1, "patterns": [],
        "disabled_default_ids": ["color_word"],
    })

    registry = PatternRegistry(defaults / "patterns.json", user / "patterns.json")
    assert "color_word" not in registry.patterns
    registry.save_user()
    assert json.loads((user / "patterns.json").read_text(encoding="utf-8"))[
        "disabled_default_ids"
    ] == ["color_word"]


def test_default_review_restores_a_disabled_pattern_and_rejects_stale_defaults(tmp_path):
    root = _root(tmp_path)
    defaults = root / "data" / "defaults"
    user = root / "data" / "user"
    _write(user / "patterns.json", {
        "schema_version": 1, "patterns": [],
        "disabled_default_ids": ["color_word"],
    })
    service = DefaultOverrideService(root)
    snapshot = service.scan()
    disabled = next(item for item in snapshot.items if item.kind == "pattern")

    changed = json.loads((defaults / "patterns.json").read_text(encoding="utf-8"))
    changed["patterns"][0]["examples"] = ["blue"]
    _write(defaults / "patterns.json", changed)
    with pytest.raises(RuntimeError, match="changed after this default review opened"):
        service.restore(
            (disabled.key,), authority_fingerprint=snapshot.authority_fingerprint
        )

    current = service.scan()
    disabled = next(item for item in current.items if item.kind == "pattern")
    result = service.restore(
        (disabled.key,), authority_fingerprint=current.authority_fingerprint
    )
    assert result.restored_fields == 1
    assert json.loads((user / "patterns.json").read_text(encoding="utf-8"))[
        "disabled_default_ids"
    ] == []
    assert "color_word" in PatternRegistry(
        defaults / "patterns.json", user / "patterns.json"
    ).patterns


def test_default_override_dialog_is_bounded_and_requires_selection(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtWidgets import QApplication
    from qt_app.default_overrides_dialog import DefaultOverridesDialog

    snapshot = DefaultOverrideSnapshot(
        tuple(
            DefaultOverrideItem(
                f"tag\x1ftag_{index}\x1fnotes",
                "tag",
                "Tag",
                f"tag_{index}",
                f"Tag {index}",
                "notes",
                "Notes",
                "My note",
                "Shipped note",
            )
            for index in range(201)
        ) + (
            DefaultOverrideItem(
                "tag\x1ftag_0\x1fknowledge_tag",
                "tag",
                "Tag",
                "tag_0",
                "Audit only",
                "knowledge_tag",
                "Representative Tag",
                "Local value",
                "Not set",
                difference_kind="current_only",
                difference_label="Current value only",
            ),
        ),
        "authority",
    )
    app = QApplication.instance() or QApplication([])
    dialog = DefaultOverridesDialog(snapshot)
    dialog.show()
    app.processEvents()

    assert dialog.model.loaded_count == 200
    assert dialog.model.total_count == 201
    assert dialog.load_more.isVisible()
    assert dialog.model.headerData(3, Qt.Horizontal) == "Current Value"
    assert dialog.model.headerData(4, Qt.Horizontal) == "Shipped Value"
    assert dialog.close_button.text() == "Close"
    assert dialog.restore_fields_button.text() == "Reset Selected Settings"
    assert dialog.restore_records_button.text() == "Reset Entire Selected Records"
    assert dialog.advanced_audit.text().startswith("Include 1 current-only")
    assert not dialog.restore_fields_button.isEnabled()
    assert not dialog.restore_records_button.isEnabled()
    selection = dialog.table.selectionModel()
    selection.select(
        dialog.model.index(0, 0),
        QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
    )
    app.processEvents()
    assert dialog.restore_fields_button.isEnabled()
    assert dialog.restore_records_button.isEnabled()
    assert dialog.selected_keys() == ("tag\x1ftag_0\x1fnotes",)
    assert "Tag 0 - Notes" in dialog.selection_summary.text()
    assert "including 1 current-only" in dialog.selection_effect.text()
    assert not dialog.table.horizontalScrollBar().isVisible()

    dialog.search.setText("Tag 200")
    app.processEvents()
    assert dialog.model.total_count == 1
    assert "1 matching reset candidate" in dialog.result_status.text()

    dialog.search.clear()
    dialog.advanced_audit.setChecked(True)
    app.processEvents()
    assert dialog.model.total_count == 202
    dialog.search.setText("Audit only")
    app.processEvents()
    audit_index = dialog.model.index(0, 0)
    assert dialog.model.data(dialog.model.index(0, 4)) == "No shipped value"
    selection.select(
        audit_index,
        QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
    )
    app.processEvents()
    assert not dialog.restore_fields_button.isEnabled()
    assert not dialog.restore_records_button.isEnabled()
    assert "audit only" in dialog.selection_effect.text().casefold()
    dialog.close()
