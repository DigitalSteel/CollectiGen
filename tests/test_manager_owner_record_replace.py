"""Original owner atomic replacement tolerates only bounded Windows denial."""
from copy import deepcopy
from types import SimpleNamespace
import errno
from pathlib import Path

import pytest
from core import manager_launch_owner as module


@pytest.fixture
def owner(tmp_path):
    value = module.DeploymentOwner(tmp_path)
    value.acquire()
    original_fd = value.fd
    try:
        yield value
    finally:
        # A lost-lease test changes only the synthetic Python field.
        value.fd = original_fd
        value.abandon()


def clock(monkeypatch, on_sleep=None, advance=None):
    now = [0.0]
    def sleep(delay):
        now[0] += delay if advance is None else advance
        if on_sleep is not None:
            on_sleep()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


def denied(code=5):
    error = PermissionError(errno.EACCES, "synthetic Windows replacement denial")
    error.winerror = code
    return error


def changed(owner):
    result = deepcopy(owner.record)
    result["helper_generation"] += 1
    return result


@pytest.mark.parametrize("code", [5, 32])
def test_transient_denial_retries_same_candidate_before_commit(owner, monkeypatch, code):
    prior, candidate = owner.raw, changed(owner)
    original = module.os.replace
    calls = []
    clock(monkeypatch)
    def replace(source, target):
        calls.append((Path(source), Path(source).read_bytes()))
        assert owner.marker.read_bytes() == prior
        assert owner.raw == prior and owner.record != candidate
        if len(calls) <= 2:
            raise denied(code)
        original(source, target)
    monkeypatch.setattr(module.os, "replace", replace)
    owner._persist(candidate)
    assert len(calls) == 3 and len({row[0] for row in calls}) == 1
    assert len({row[1] for row in calls}) == 1
    assert owner.record == candidate and owner.marker.read_bytes() == owner.raw
    assert owner.persistence_fault is None


def test_permanent_denial_keeps_original_authority_and_latches(owner, monkeypatch):
    prior, record, lease = owner.raw, deepcopy(owner.record), owner.fd
    calls = []
    clock(monkeypatch)
    def replace(source, target):
        calls.append(Path(source))
        raise denied()
    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(PermissionError):
        owner._persist(changed(owner))
    assert len(calls) == 9 and len(set(calls)) == 1
    assert owner.raw == prior and owner.record == record and owner.fd == lease
    assert owner.marker.read_bytes() == prior and owner.persistence_fault == "PermissionError"
    with pytest.raises(module.LaunchHold, match="persistence_fault"):
        owner._persist(changed(owner))
    assert len(calls) == 9


@pytest.mark.parametrize("change,reason", [
    ("marker", "owner_marker_changed"),
    ("candidate", "owner_candidate_changed"),
    ("lease", "owner_lease_changed"),
    ("raw", "owner_marker_changed"),
])
def test_backoff_staleness_never_replaces(owner, monkeypatch, change, reason):
    prior, record = owner.raw, deepcopy(owner.record)
    calls = []
    def mutate():
        if change == "marker":
            owner.marker.write_bytes(b"external marker change")
        elif change == "candidate":
            calls[0].write_bytes(b"external candidate change")
        elif change == "lease":
            owner.fd = None
        else:
            owner.raw = b"changed in-memory marker"
    clock(monkeypatch, on_sleep=mutate)
    def replace(source, target):
        calls.append(Path(source))
        raise denied()
    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(module.LaunchHold, match=reason):
        owner._persist(changed(owner))
    assert len(calls) == 1 and owner.record == record
    assert owner.raw == (b"changed in-memory marker" if change == "raw" else prior)
    assert owner.persistence_fault == "LaunchHold"


def test_other_permission_error_is_not_retried(owner, monkeypatch):
    calls = []
    clock(monkeypatch)
    def replace(source, target):
        calls.append(source)
        raise PermissionError(errno.EACCES, "not a Windows sharing/access code")
    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(PermissionError):
        owner._persist(changed(owner))
    assert len(calls) == 1 and owner.persistence_fault == "PermissionError"


def test_initial_marker_creation_is_not_retried(tmp_path, monkeypatch):
    value = module.DeploymentOwner(tmp_path)
    calls = []
    clock(monkeypatch)
    def replace(source, target):
        calls.append(source)
        raise denied()
    monkeypatch.setattr(module.os, "replace", replace)
    try:
        with pytest.raises(PermissionError):
            value.acquire()
        assert len(calls) == 1 and value.raw is None
    finally:
        value.abandon()


def test_deadline_ends_retry_before_another_replace(owner, monkeypatch):
    calls = []
    clock(monkeypatch, advance=.300)
    def replace(source, target):
        calls.append(source)
        raise denied()
    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(PermissionError):
        owner._persist(changed(owner))
    assert len(calls) == 1 and owner.persistence_fault == "PermissionError"


def test_uncertain_successful_replace_readback_is_never_retried(owner, monkeypatch):
    prior, record, candidate = owner.raw, deepcopy(owner.record), changed(owner)
    original_replace, original_read = module.os.replace, Path.read_bytes
    calls = []
    clock(monkeypatch)
    def replace(source, target):
        original_replace(source, target)
        calls.append(source)
    def read(path):
        if path == owner.marker and calls:
            raise OSError("synthetic post-replacement readback uncertainty")
        return original_read(path)
    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(OSError, match="readback uncertainty"):
        owner._persist(candidate)
    assert len(calls) == 1 and owner.raw == prior and owner.record == record
    assert original_read(owner.marker) != prior and owner.persistence_fault == "OSError"


def test_flush_failure_never_enters_replace_retry(owner, monkeypatch):
    calls = []
    clock(monkeypatch)
    monkeypatch.setattr(module.os, "replace", lambda *args: calls.append(args))
    def fail(_fd):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync failure"):
        owner._persist(changed(owner))
    assert not calls and owner.persistence_fault == "OSError"


def test_guard_reads_cannot_extend_retry_deadline(owner, monkeypatch):
    now = clock(monkeypatch)
    original_read = Path.read_bytes
    calls = []
    def replace(source, target):
        calls.append(Path(source))
        raise denied()
    def read(path):
        value = original_read(path)
        if calls and path == calls[0]:
            now[0] += .300
        return value
    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(PermissionError):
        owner._persist(changed(owner))
    assert len(calls) == 1 and owner.persistence_fault == "PermissionError"
