"""Canonical writer composition over explicit disposable settings only."""
import json
from pathlib import Path

import pytest

from core.settings import AppSettings, SettingsRecoveryRequiredError
from core.lmstudio_connection_settings import KEY, LmStudioConnectionSettings


def target_file(tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b'{"ui_theme":"dark"}\n')
    return target, LmStudioConnectionSettings(target)


def save_preference(store, key="synthetic-a"):
    return store.save_selection("http://127.0.0.1:1234",
        preferred_model_key=key, expected=store.observe())


@pytest.mark.parametrize("mode", ["fresh", "create", "forget", "change"])
def test_ordinary_save_preserves_latest_separate_selection(tmp_path, mode):
    target, store = target_file(tmp_path)
    if mode == "create":
        editor = AppSettings.load(target)
    save_preference(store)
    if mode != "create":
        editor = AppSettings.load(target)
    if mode == "forget":
        store.forget_selection(expected=store.observe())
    elif mode == "change":
        save_preference(store, "synthetic-b")
    current = json.loads(target.read_bytes())
    editor.ui_theme = "light"
    editor.save(target)
    after = json.loads(target.read_bytes())
    assert after.get(KEY) == current.get(KEY)
    assert (KEY in after) == (KEY in current)
    assert AppSettings.load(target).ui_theme == "light"
    observed = store.observe()
    if mode == "forget":
        assert observed.selection is None
    else:
        assert observed.selection.preferred_model_key == (
            "synthetic-b" if mode == "change" else "synthetic-a")
        store.forget_selection(expected=observed)
        assert store.observe().selection is None


@pytest.mark.parametrize("current", [
    {"schema_version": 99, "future": [1, {"retain": True}]},
    {"schema_version": 1, "endpoint": "http://127.0.0.1:1234",
     "preferred_model_key": None, "future": {"retain": [2, "synthetic"]}},
    None, ["future-shape"], "future-string",
])
def test_ordinary_save_carries_unknown_extension_without_interpreting(tmp_path, current):
    target, store = target_file(tmp_path)
    save_preference(store)
    stale = AppSettings.load(target)
    payload = json.loads(target.read_bytes())
    payload[KEY] = current
    target.write_bytes((json.dumps(payload) + "\n").encode())
    stale.ui_theme = "light"
    stale.save(target)
    after = json.loads(target.read_bytes())
    assert KEY in after and after[KEY] == current
    assert after["ui_theme"] == "light"


def test_absent_extension_stays_absent(tmp_path):
    target, _ = target_file(tmp_path)
    editor = AppSettings.load(target)
    editor.ui_theme = "light"
    editor.save(target)
    assert KEY not in json.loads(target.read_bytes())


def test_explicit_recovery_replacement_retains_original_authority(tmp_path):
    target, store = target_file(tmp_path)
    editor = AppSettings.load(target)
    save_preference(store)
    editor.ui_theme = "light"
    editor.save(target, _allow_recovery_replacement=True)
    assert KEY not in json.loads(target.read_bytes())
    assert AppSettings.load(target).ui_theme == "light"


def test_ordinary_save_still_refuses_late_target_change(tmp_path, monkeypatch):
    target, store = target_file(tmp_path)
    editor = AppSettings.load(target)
    save_preference(store)
    before = target.read_bytes()
    foreign = b'{"ui_theme":"dark","synthetic_foreign":true}\n'
    write = Path.write_bytes
    def change(path, data):
        result = write(path, data)
        if path == target.with_suffix(".json.tmp"):
            write(target, foreign)
        return result
    monkeypatch.setattr(Path, "write_bytes", change)
    editor.ui_theme = "light"
    with pytest.raises(SettingsRecoveryRequiredError):
        editor.save(target)
    assert target.read_bytes() == foreign
    assert target.with_suffix(".json.tmp").exists()
    assert before in [p.read_bytes() for p in (tmp_path / "settings-history").glob("*.json")]


def test_ordinary_save_readback_failure_restores_current_preference(tmp_path, monkeypatch):
    target, store = target_file(tmp_path)
    editor = AppSettings.load(target)
    save_preference(store)
    before = target.read_bytes()
    read = Path.read_text
    def mismatch(path, *args, **kwargs):
        return "{}" if path == target else read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", mismatch)
    editor.ui_theme = "light"
    with pytest.raises(OSError, match="previous settings file was restored"):
        editor.save(target)
    assert target.read_bytes() == before
