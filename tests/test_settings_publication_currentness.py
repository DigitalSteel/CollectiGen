"""Actual public settings writer over explicit disposable synthetic targets."""
from pathlib import Path
import json

import pytest

from core.settings import AppSettings, SettingsRecoveryRequiredError, _settings_writer
from core import training_source_io

BEFORE = b'{"synthetic_preference":"before","future":{"preserve":true}}\n'
FOREIGN = b'{"synthetic_preference":"newer","future":{"preserve":true}}\n'
OUR_PAYLOAD = {"synthetic_preference": "reviewed", "future": {"preserve": True}}


def publish(target, payload=OUR_PAYLOAD):
    settings = AppSettings._from_payload(json.loads(BEFORE))
    with _settings_writer(target):
        settings._save_locked(target, _patch_payload=payload)


def target_file(tmp_path, exists=True):
    target = tmp_path / "settings.json"
    if exists:
        target.write_bytes(BEFORE)
    return target


def assert_evidence(target, *, existed=True):
    history = list((target.parent / "settings-history").glob("*.json"))
    assert len(history) == (1 if existed else 0)
    if existed:
        assert history[0].read_bytes() == BEFORE
    prepared = target.with_suffix(".json.tmp")
    assert prepared.is_file() and json.loads(prepared.read_bytes()) == OUR_PAYLOAD


@pytest.mark.parametrize("existed", [False, True])
def test_successful_create_and_replace_keep_canonical_readback_history(tmp_path, existed):
    target = target_file(tmp_path, existed)
    publish(target)
    assert json.loads(target.read_bytes()) == OUR_PAYLOAD
    assert not target.with_suffix(".json.tmp").exists()
    snapshots = list((tmp_path / "settings-history").glob("*.json"))
    assert len(snapshots) == int(existed)
    if existed:
        assert snapshots[0].read_bytes() == BEFORE


@pytest.mark.parametrize("change", ["replace", "delete", "create"])
def test_late_target_change_is_not_published_or_rolled_back(tmp_path, monkeypatch, change):
    target = target_file(tmp_path, change != "create")
    write = Path.write_bytes
    injected = []
    def prepare(path, data):
        result = write(path, data)
        if path == target.with_suffix(".json.tmp"):
            if change == "delete":
                target.unlink()
            else:
                write(target, FOREIGN)
            injected.append(True)
        return result
    monkeypatch.setattr(Path, "write_bytes", prepare)
    with pytest.raises(SettingsRecoveryRequiredError, match="Settings changed"):
        publish(target)
    assert injected == [True]
    if change == "delete":
        assert not target.exists()
    else:
        assert target.read_bytes() == FOREIGN
    assert_evidence(target, existed=change != "create")
    assert not target.with_suffix(".json.restore.tmp").exists()


def test_late_unreadable_target_is_held_without_post_publication_recovery_read(tmp_path, monkeypatch):
    target = target_file(tmp_path)
    write, opened = Path.write_bytes, Path.open
    prepared = []
    failed_reads = []
    def prepare(path, data):
        result = write(path, data)
        if path == target.with_suffix(".json.tmp"):
            prepared.append(True)
        return result
    def observe(path, mode="r", *args, **kwargs):
        if path == target and prepared and mode == "rb":
            failed_reads.append(True)
            raise PermissionError("synthetic access refusal")
        return opened(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "write_bytes", prepare)
    monkeypatch.setattr(Path, "open", observe)
    with pytest.raises(SettingsRecoveryRequiredError, match="could not be verified"):
        publish(target)
    assert failed_reads == [True]  # No recovery handler follows an unverified path.
    monkeypatch.setattr(Path, "open", opened)
    assert target.read_bytes() == BEFORE
    assert_evidence(target)


def test_large_late_target_is_read_only_to_preimage_length_plus_one(tmp_path, monkeypatch):
    target = target_file(tmp_path)
    write, opened = Path.write_bytes, Path.open
    prepared, limits = [], []
    def prepare(path, data):
        result = write(path, data)
        if path == target.with_suffix(".json.tmp"):
            write(target, b"x" * (1024 * 1024))
            prepared.append(True)
        return result
    class Bounded:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def read(self, amount):
            limits.append(amount)
            assert amount == len(BEFORE) + 1
            return self.stream.read(amount)
    def observe(path, mode="r", *args, **kwargs):
        stream = opened(path, mode, *args, **kwargs)
        return Bounded(stream) if prepared and path == target and mode == "rb" else stream
    monkeypatch.setattr(Path, "write_bytes", prepare)
    monkeypatch.setattr(Path, "open", observe)
    with pytest.raises(SettingsRecoveryRequiredError, match="Settings changed"):
        publish(target)
    assert limits == [len(BEFORE) + 1]
    assert target.stat().st_size == 1024 * 1024
    assert_evidence(target)


@pytest.mark.parametrize("refused", ["target", "prepared"])
def test_late_path_guard_refusal_never_follows_or_replaces_target(tmp_path, monkeypatch, refused):
    target = target_file(tmp_path)
    write, opened, guard = Path.write_bytes, Path.open, training_source_io._assert_unredirected
    prepared, late_reads = [], []
    def prepare(path, data):
        result = write(path, data)
        if path == target.with_suffix(".json.tmp"):
            prepared.append(True)
        return result
    def require_original(path):
        chosen = target if refused == "target" else target.with_suffix(".json.tmp")
        if prepared and Path(path) == chosen:
            raise ValueError("synthetic redirected path;not an actual alias")
        return guard(path)
    def observe(path, mode="r", *args, **kwargs):
        if prepared and path == target and mode == "rb":
            late_reads.append(True)
        return opened(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "write_bytes", prepare)
    monkeypatch.setattr(Path, "open", observe)
    monkeypatch.setattr(training_source_io, "_assert_unredirected", require_original)
    with pytest.raises(SettingsRecoveryRequiredError, match="could not be verified"):
        publish(target)
    assert late_reads == []
    monkeypatch.setattr(Path, "open", opened)
    assert target.read_bytes() == BEFORE
    # Evidence assertion reads the original snapshot/prepared file only.
    assert_evidence(target)


def test_temp_write_failure_preserves_original(tmp_path, monkeypatch):
    target = target_file(tmp_path)
    write = Path.write_bytes
    def fail(path, data):
        if path == target.with_suffix(".json.tmp"):
            raise OSError("synthetic temp failure")
        return write(path, data)
    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(OSError, match="previous settings state remains unchanged"):
        publish(target)
    assert target.read_bytes() == BEFORE


@pytest.mark.parametrize("existed", [False, True])
def test_original_readback_mismatch_still_restores_exact_preimage(tmp_path, monkeypatch, existed):
    target = target_file(tmp_path, existed)
    read = Path.read_text
    def mismatch(path, *args, **kwargs):
        if path == target:
            return "{}"
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", mismatch)
    with pytest.raises(OSError, match="previous settings file was restored"):
        publish(target)
    if existed:
        assert target.read_bytes() == BEFORE
    else:
        assert not target.exists()


def test_foreign_change_after_publication_is_not_blindly_restored(tmp_path, monkeypatch):
    target = target_file(tmp_path)
    read = Path.read_text
    def foreign_readback(path, *args, **kwargs):
        if path == target:
            target.write_bytes(FOREIGN)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", foreign_readback)
    with pytest.raises(SettingsRecoveryRequiredError, match="changed target"):
        publish(target)
    assert target.read_bytes() == FOREIGN


def test_restore_failure_does_not_claim_successful_rollback(tmp_path, monkeypatch):
    target = target_file(tmp_path)
    replace, read = Path.replace, Path.read_text
    def mismatch(path, *args, **kwargs):
        return "{}" if path == target else read(path, *args, **kwargs)
    def fail_restore(path, destination):
        if path == target.with_suffix(".json.restore.tmp"):
            raise OSError("synthetic restore refusal")
        return replace(path, destination)
    monkeypatch.setattr(Path, "read_text", mismatch)
    monkeypatch.setattr(Path, "replace", fail_restore)
    with pytest.raises(OSError, match="could not be restored"):
        publish(target)
    assert json.loads(target.read_bytes()) == OUR_PAYLOAD


def test_ordinary_settings_save_and_backend_extension_roundtrip(tmp_path):
    from core.backend_connection_settings import BackendConnectionSettings
    target = target_file(tmp_path)
    settings = AppSettings._from_payload(json.loads(BEFORE))
    settings.ui_theme = "dark"
    settings.save(target)
    assert AppSettings.load(target).ui_theme == "dark"
    assert json.loads(target.read_bytes())["future"] == {"preserve": True}
    store = BackendConnectionSettings(target)
    before = store.observe()
    saved = store.save_connection("http://localhost:11434", endpoint_network_allowed=False, expected=before)
    assert saved.digest == store.observe().digest
    assert store.observe().selection.endpoint == "http://127.0.0.1:11434"
    assert json.loads(target.read_bytes())["future"] == {"preserve": True}
    with pytest.raises(ValueError, match="settings_changed_reload_setup"):
        store.save_connection("http://localhost:11435", endpoint_network_allowed=False, expected=before)
