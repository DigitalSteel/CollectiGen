"""Generated preference edits only; no real settings, endpoint or credential."""
from dataclasses import FrozenInstanceError
import inspect
import json
from pathlib import Path
import traceback

import pytest

from core import lmstudio_connection_settings as preferences
from core.lmstudio_connection_settings import (
    KEY, LmStudioConnectionSettings, SettingsError, SettingsRecoveryRequired,
)
from core.settings import AppSettings, FLORENCE2_MODEL_ID

BEFORE = {"ui_theme": "dark", "plain_language_model": FLORENCE2_MODEL_ID,
          "future": {"retained": [1, "synthetic"]}}
FOREIGN = b'{"future":{"newer":true}}\n'


def prepare(tmp_path, payload=BEFORE):
    path = tmp_path / "settings.json"
    path.write_bytes((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    return path, LmStudioConnectionSettings(path)


def no_publication(path):
    assert not (path.parent / "settings-history").exists()
    assert not path.with_suffix(".json.tmp").exists()


def test_inert_constructor_and_explicit_empty_observe(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "settings.json"
    with monkeypatch.context() as patch:
        def forbidden(*args, **kwargs): pytest.fail("construction performed IO")
        patch.setattr(Path, "open", forbidden)
        patch.setattr(Path, "mkdir", forbidden)
        store = LmStudioConnectionSettings(path)
    assert not path.parent.exists()
    empty = store.observe()
    assert empty.digest is None and empty.selection is None and empty._raw is None
    assert not path.parent.exists()


@pytest.mark.parametrize("value", [None, "relative/settings.json", Path("relative"),
                                  Path("C:/synthetic/../settings.json")])
def test_explicit_absolute_nontraversing_path_required(value):
    with pytest.raises(SettingsError):
        LmStudioConnectionSettings(value)


def test_observe_is_read_only_immutable_detached_and_redacted(tmp_path, monkeypatch):
    data = {**BEFORE, KEY: {"schema_version": 1, "endpoint": "http://localhost:1234",
                           "preferred_model_key": "synthetic/private-model"}}
    path, store = prepare(tmp_path, data)
    raw = path.read_bytes()
    with monkeypatch.context() as patch:
        def forbidden(*args, **kwargs): pytest.fail("Observe wrote state")
        patch.setattr(Path, "write_bytes", forbidden)
        patch.setattr(Path, "mkdir", forbidden)
        snap = store.observe()
    assert snap._raw == raw and snap.selection.endpoint == "http://127.0.0.1:1234"
    assert not hasattr(snap, "_payload")
    assert "private-model" not in repr(snap) and "1234" not in repr(snap)
    assert str(path) not in repr(snap) + repr(store)
    with pytest.raises(FrozenInstanceError): snap.digest = "other"
    with pytest.raises(FrozenInstanceError): snap.selection.endpoint = "other"
    data[KEY]["endpoint"] = "http://localhost:1235"
    assert store.observe()._raw == raw
    no_publication(path)


@pytest.mark.parametrize("key", [None, "publisher/exact-key"])
def test_save_reopen_forget_preserves_unknown_fields_and_existing_ai(tmp_path, key):
    original = {**BEFORE, "other_provider": {"api_token": "synthetic-other-value"},
        KEY: {"schema_version": 1, "endpoint": "http://localhost:1235",
              "preferred_model_key": "old", "future": {"keep": [1, 2]}}}
    path, store = prepare(tmp_path, original)
    expected = store.observe()
    saved = store.save_selection("http://localhost:1234", preferred_model_key=key, expected=expected)
    assert saved._raw == path.read_bytes() and saved.digest == store.observe().digest
    assert saved.selection.preferred_model_key is key or saved.selection.preferred_model_key == key
    reopened = LmStudioConnectionSettings(path).observe()
    assert reopened == saved
    value = json.loads(path.read_bytes())
    assert value[KEY] == {**original[KEY], "endpoint": "http://127.0.0.1:1234", "preferred_model_key": key}
    assert {k: v for k, v in value.items() if k != KEY} == {k: v for k, v in original.items() if k != KEY}
    assert AppSettings.load(path).plain_language_model == FLORENCE2_MODEL_ID
    forgotten = store.forget_selection(expected=saved)
    assert forgotten.selection is None and KEY not in json.loads(path.read_bytes())
    assert json.loads(path.read_bytes()) == {k: v for k, v in original.items() if k != KEY}
    assert store.observe() == forgotten


def test_forget_absent_key_does_not_create_or_rewrite_settings(tmp_path):
    missing = tmp_path / "absent.json"
    store = LmStudioConnectionSettings(missing)
    expected = store.observe()
    assert store.forget_selection(expected=expected) == expected
    assert not missing.exists()
    path, existing = prepare(tmp_path)
    raw = path.read_bytes()
    assert existing.forget_selection(expected=existing.observe())._raw == raw
    assert path.read_bytes() == raw
    no_publication(path)


def test_cross_origin_snapshot_rejected_even_for_identical_bytes(tmp_path):
    a, one = prepare(tmp_path)
    b = tmp_path / "second.json"
    b.write_bytes(a.read_bytes())
    two = LmStudioConnectionSettings(b)
    with pytest.raises(SettingsError, match="^settings_snapshot_required$"):
        two.save_selection("http://localhost:1234", expected=one.observe())
    assert not b.with_name(b.name + ".lock").exists()
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("change", ["replace", "delete", "create"])
def test_stale_expected_snapshot_preserves_current_state(tmp_path, change):
    path = tmp_path / "settings.json"
    if change != "create": path.write_bytes(b"{}\n")
    store = LmStudioConnectionSettings(path)
    expected = store.observe()
    if change == "delete": path.unlink()
    else: path.write_bytes(FOREIGN)
    with pytest.raises(SettingsError, match="^settings_changed_reload_setup$"):
        store.save_selection("http://localhost:1234", expected=expected)
    assert not path.exists() if change == "delete" else path.read_bytes() == FOREIGN
    no_publication(path)


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": True},
    {"schema_version": 2}, {"schema_version": 1, "endpoint": "http://remote.test:1234"},
    {"schema_version": 1, "endpoint": "http://localhost"},
    {"schema_version": 1, "endpoint": "http://localhost:1234", "preferred_model_key": ""},
    {"schema_version": 1, "endpoint": "http://localhost:1234", "preferred_model_key": False}])
def test_malformed_or_future_owned_schema_is_held_not_cleared(tmp_path, value):
    path, store = prepare(tmp_path, {KEY: value})
    raw = path.read_bytes()
    with pytest.raises(SettingsError): store.observe()
    assert path.read_bytes() == raw
    no_publication(path)


@pytest.mark.parametrize("field", ["token", "API_TOKEN", "Api_Key", "authorization",
                                   "password", "credentials", "headers"])
def test_known_credential_fields_refused_without_echo(tmp_path, field):
    marker = "synthetic-secret-must-not-echo"
    path, store = prepare(tmp_path, {KEY: {"schema_version": 1, "endpoint": "http://localhost:1234", field: marker}})
    try:
        store.observe()
    except SettingsError as exc:
        assert str(exc) == "credential_field_refused"
        assert marker not in str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    else: pytest.fail("credential field accepted")
    no_publication(path)


@pytest.mark.parametrize("raw", [b"[]", b"", b"not-json", b'{"x":1,"x":2}',
    b'{"x":NaN}', b'{"x":1e999}'])
def test_invalid_or_nonfinite_document_never_repaired(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_bytes(raw)
    with pytest.raises(SettingsError): LmStudioConnectionSettings(path).observe()
    assert path.read_bytes() == raw
    no_publication(path)


@pytest.mark.parametrize("endpoint,key", [("https://localhost:1234", None),
    ("http://localhost:1234?token=x", None), ("http://localhost:1234", "x\n"),
    ("http://localhost:1234", "x" * 1025)])
def test_invalid_save_preflight_does_not_create_history(tmp_path, endpoint, key):
    path, store = prepare(tmp_path)
    expected = store.observe()
    with pytest.raises(SettingsError):
        store.save_selection(endpoint, preferred_model_key=key, expected=expected)
    assert path.read_bytes() == expected._raw
    no_publication(path)


def test_read_and_candidate_size_limits_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")
    store = LmStudioConnectionSettings(path)
    expected = store.observe()
    monkeypatch.setattr(preferences, "MAX_SETTINGS_BYTES", 64)
    with pytest.raises(SettingsError, match="^settings_size_limit$"):
        store.save_selection("http://localhost:1234", expected=expected)
    assert path.read_bytes() == b"{}\n"
    no_publication(path)
    path.write_bytes(b" " * 65)
    with pytest.raises(SettingsError, match="^settings_size_limit$"): store.observe()


def test_change_during_candidate_preparation_is_checked_before_writer(tmp_path, monkeypatch):
    path, store = prepare(tmp_path)
    expected = store.observe()
    encode = preferences._encoded
    def interleave(payload, *, bounded):
        data = encode(payload, bounded=bounded)
        if bounded: path.write_bytes(FOREIGN)
        return data
    monkeypatch.setattr(preferences, "_encoded", interleave)
    with pytest.raises(SettingsError, match="^settings_changed_reload_setup$"):
        store.save_selection("http://localhost:1234", expected=expected)
    assert path.read_bytes() == FOREIGN
    no_publication(path)


def test_late_canonical_writer_hold_preserves_newer_state_and_typed_recovery(tmp_path, monkeypatch):
    path, store = prepare(tmp_path)
    expected = store.observe()
    write = Path.write_bytes
    def interleave(target, data):
        result = write(target, data)
        if target == path.with_suffix(".json.tmp"): write(path, FOREIGN)
        return result
    monkeypatch.setattr(Path, "write_bytes", interleave)
    with pytest.raises(SettingsRecoveryRequired, match="^settings_recovery_required$"):
        store.save_selection("http://localhost:1234", expected=expected)
    assert path.read_bytes() == FOREIGN
    assert path.with_suffix(".json.tmp").is_file()
    history = list((tmp_path / "settings-history").glob("*.json"))
    assert len(history) == 1 and history[0].read_bytes() == expected._raw


def test_canonical_readback_mismatch_restores_before_reporting_failure(tmp_path, monkeypatch):
    path, store = prepare(tmp_path)
    expected = store.observe()
    read = Path.read_text
    def mismatch(target, *args, **kwargs):
        return "{}" if target == path else read(target, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", mismatch)
    with pytest.raises(SettingsError, match="^settings_publication_failed$"):
        store.save_selection("http://localhost:1234", expected=expected)
    assert path.read_bytes() == expected._raw


def test_no_network_process_secret_lookup_or_credential_argument(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess
    import urllib.request
    from core import lmstudio_transport
    path, store = prepare(tmp_path)
    def forbidden(*args, **kwargs): pytest.fail("preference service escaped its authority")
    for obj, attr in [(socket, "socket"), (socket, "create_connection"),
                      (subprocess, "Popen"), (urllib.request, "urlopen"),
                      (os, "getenv"), (lmstudio_transport, "discover")]:
        monkeypatch.setattr(obj, attr, forbidden)
    saved = store.save_selection("http://localhost:1234", expected=store.observe())
    assert store.forget_selection(expected=saved).selection is None
    assert "token" not in inspect.signature(store.save_selection).parameters
    assert not any(key.casefold() in preferences._CREDENTIAL_FIELDS for key in json.loads(saved._raw)[KEY])


def test_filesystem_failure_details_are_not_exposed(tmp_path, monkeypatch):
    path, store = prepare(tmp_path)
    opened = Path.open
    marker = "synthetic-private-path-and-secret"
    def refused(target, mode="r", *args, **kwargs):
        if target == path and mode == "rb": raise PermissionError(marker)
        return opened(target, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", refused)
    with pytest.raises(SettingsError) as raised:
        store.observe()
    assert str(raised.value) == "settings_invalid_or_unavailable"
    assert marker not in "".join(traceback.format_exception(raised.value))
    no_publication(path)


def test_no_fallible_read_after_successful_canonical_publication(tmp_path, monkeypatch):
    path, store = prepare(tmp_path)
    expected = store.observe()
    write, opened = AppSettings._save_locked, Path.open
    completed = []
    def publish(*args, **kwargs):
        result = write(*args, **kwargs)
        completed.append(True)
        return result
    def refused(target, mode="r", *args, **kwargs):
        if target == path and completed and mode == "rb":
            pytest.fail("service read after canonical transaction reported success")
        return opened(target, mode, *args, **kwargs)
    monkeypatch.setattr(AppSettings, "_save_locked", publish)
    monkeypatch.setattr(Path, "open", refused)
    saved = store.save_selection("http://localhost:1234", expected=expected)
    assert completed == [True] and saved.selection.endpoint == "http://127.0.0.1:1234"
    assert json.loads(saved._raw)[KEY]["preferred_model_key"] is None
