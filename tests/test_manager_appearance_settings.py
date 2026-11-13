"""Synthetic local Manager settings; canonical transaction and authority boundaries."""
import json
from pathlib import Path
import pytest
from core.manager_appearance_settings import ManagerAppearanceSettings, AppearanceSettingsError


def write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_store_is_read_only_until_save_and_preserves_only_selected_values(tmp_path):
    path = tmp_path / "settings.json"; store = ManagerAppearanceSettings(path)
    original = store.observe()
    assert original.theme == "system" and original.show_tooltips and not path.exists()
    saved = store.save({"ui_theme": "midnight"}, expected=original)
    assert store.observe() == saved
    assert json.loads(path.read_bytes()) == {"ui_theme": "midnight"}


@pytest.mark.parametrize("theme", ["system", "dark", "light", "midnight"])
def test_persisted_appearance_preserves_unknowns_and_exact_history(tmp_path, theme):
    path = tmp_path / "settings.json"
    payload = {"ui_theme": "system", "future": {"text": "unchanged"}, "ollama_metadata_connection": {"future": 42}}
    write(path, payload); before = path.read_bytes()
    store = ManagerAppearanceSettings(path)
    saved = store.save({"ui_theme": theme, "color_blind_mode": True, "show_tooltips": False}, expected=store.observe())
    assert (saved.theme, saved.color_blind_mode, saved.show_tooltips) == (theme, True, False)
    assert store.observe() == saved
    actual = json.loads(path.read_bytes())
    assert actual["future"] == payload["future"] and actual["ollama_metadata_connection"] == payload["ollama_metadata_connection"]
    assert next((tmp_path / "settings-history").glob("*.json")).read_bytes() == before


def test_stale_editor_never_overwrites_another_change(tmp_path):
    path = tmp_path / "settings.json"; store = ManagerAppearanceSettings(path)
    before = store.observe(); write(path, {"future": 2}); raw = path.read_bytes()
    with pytest.raises(AppearanceSettingsError, match="settings_changed"):
        store.save({"ui_theme": "dark"}, expected=before)
    assert path.read_bytes() == raw and not (tmp_path / "settings-history").exists()


@pytest.mark.parametrize("raw", [b"broken", b'{"ui_theme":"unknown"}', b'{"show_tooltips":"false"}', b'{"ui_theme":"dark","ui_theme":"light"}'])
def test_invalid_settings_remain_intact(tmp_path, raw):
    path = tmp_path / "settings.json"; path.write_bytes(raw)
    with pytest.raises(AppearanceSettingsError): ManagerAppearanceSettings(path).observe()
    assert path.read_bytes() == raw


def test_failed_readback_restores_exact_prior_bytes(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"; write(path, {"future": [1, 2]}); raw = path.read_bytes()
    store = ManagerAppearanceSettings(path); before = store.observe()
    read_text = Path.read_text
    def corrupt(self, *args, **kwargs):
        return "{}" if self == path else read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", corrupt)
    with pytest.raises(AppearanceSettingsError, match="appearance_save_failed"):
        store.save({"ui_theme": "dark"}, expected=before)
    assert path.read_bytes() == raw
    assert next((tmp_path / "settings-history").glob("*.json")).read_bytes() == raw


def test_managed_groups_keep_original_authority_but_unmanaged_choice_can_save(tmp_path):
    path = tmp_path / "settings.json"
    managed = {"schema": 1, "binding": "a"*64, "suite_id": "synthetic", "revision": 1,
        "suite_fingerprint": "b"*64, "detached": False, "receipts": {},
        "groups": {"appearance.theme": {"mode": "inherited", "value_fingerprint": "c"*64}}}
    write(path, {"ui_theme": "light", "suite_managed_settings": managed})
    store = ManagerAppearanceSettings(path); before = store.observe(); raw = path.read_bytes()
    assert before.managed_fields == frozenset({"ui_theme"})
    with pytest.raises(AppearanceSettingsError, match="managed_appearance"):
        store.save({"ui_theme": "dark"}, expected=before)
    assert path.read_bytes() == raw
    saved = store.save({"show_tooltips": False}, expected=before)
    assert saved.theme == "light" and json.loads(path.read_bytes())["suite_managed_settings"] == managed


def test_no_default_profile_or_ini_reinterpretation():
    for path in ("settings.json", "C:/synthetic/settings.ini"):
        with pytest.raises(ValueError): ManagerAppearanceSettings(path)
