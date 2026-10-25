"""Durable cache-use authority; selection recency is never execution evidence."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import os
import stat
from uuid import uuid4

from .model_resource_registry import ModelResourceError, _bytes, _identity, inspect_model_resource
from .model_workspace import _path, _pinned_model_file, _pinned_parent


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def cache_state(value, *, create=False):
    state = value.get("model_cache")
    if state is None:
        state = {"schema_version": 1, "entries": {}, "operations": {}, "leases": {}, "root_generations": {}}
        if create:
            value["model_cache"] = state
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state.get("schema_version") != 1:
        raise ModelResourceError("Cache state is invalid or from a newer version. Preserve it for review.")
    for name in ("entries", "operations", "leases"):
        table = state.get(name)
        if not isinstance(table, dict) or len(table) > 10000 or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in table.items()):
            raise ModelResourceError("Cache history is invalid or exceeds its supported bound.")
    for lease in state["leases"].values():
        if lease.get("kind") == "execution_preparation":
            # Reserve future entry IDs as well as already available copies.
            # Unknown/uncertain consumers remain protected until their owner
            # closes the entire preparation, never merely on process exit.
            for name in ("entry_ids", "reserved_entry_ids"):
                ids = lease.get(name)
                if (not isinstance(ids, list) or len(ids) > 64
                        or any(not isinstance(key, str) for key in ids) or len(set(ids)) != len(ids)
                        or lease.get("state") not in {"active", "released"}):
                    raise ModelResourceError("Execution preparation protection is invalid; preserve cached files.")
    for operation in state["operations"].values():
        if "reserved_entry_ids" in operation:
            ids = operation["reserved_entry_ids"]
            if (operation.get("kind") != "prepare" or not isinstance(ids, list) or len(ids) > 64
                    or any(not isinstance(key, str) for key in ids) or len(set(ids)) != len(ids)):
                raise ModelResourceError("Incoming preparation reservations are invalid; preserve cached files.")
    return state


@contextmanager
def cache_lock(registry, *, protected_roots=()):
    """All cache writers take this lock BEFORE the existing registry writer."""
    # This operation touches two exact child files, not the contents of their
    # parent. Protected sibling folders may legitimately share that parent.
    registry_path = _path(registry.path, protected_roots)
    parent = registry_path.parent
    _path(parent / "model-cache.lock", protected_roots)
    parent.mkdir(parents=True, exist_ok=True)
    # Both exact children passed protected-boundary guards before mkdir. Pin
    # ancestor identities without reclassifying a protected sibling as a read.
    with _pinned_parent(parent, ()):
        _path(registry_path, protected_roots)
        with _cache_lock_file(parent, protected_roots):
            yield


@contextmanager
def _cache_lock_file(parent, protected_roots):
    path = _path(parent / "model-cache.lock", protected_roots)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _identity(before) != _identity(path.stat(follow_symlinks=False)):
            raise ModelResourceError("Cache lock is not a stable private file.")
        if before.st_size == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ModelResourceError("Another cache operation is active; retry after it finishes.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def pinned_entries(state):
    result = set()
    for lease in state["leases"].values():
        if lease.get("state") not in ("released",):
            entries = lease.get("entry_ids")
            if not isinstance(entries, list) or any(not isinstance(key, str) for key in entries):
                raise ModelResourceError("A cache use record is uncertain; preserve cached files for review.")
            result.update(entries)
            if lease.get("kind") == "execution_preparation":
                result.update(lease["reserved_entry_ids"])
    for operation in state["operations"].values():
        if operation.get("kind") == "prepare" and operation.get("phase") != "complete":
            result.update(operation.get("reserved_entry_ids", ()))
    return result


def add_manual_pin(state, entry_ids, operation_id):
    if len(state["leases"]) >= 10000:
        raise ModelResourceError("Cache pin history is full. Review retained history before preparing more models.")
    pin = uuid4().hex
    state["leases"][pin] = {"kind": "manual", "state": "active", "entry_ids": list(entry_ids),
        "operation_id": operation_id, "created_at": now(),
        "notice": "Keep on fast storage until explicitly released after external use finishes."}
    return pin


@dataclass(frozen=True)
class ManualPinReleasePreview:
    preview_id: str
    registry_token: str
    pin_id: str
    entry_ids: tuple[str, ...]
    message: str = "Release this manual pin only after external use finishes. Releasing does not delete files."


@dataclass(frozen=True)
class ModelUseLease:
    lease_id: str
    consumer_job_id: str
    entry_ids: tuple[str, ...]
    state: str


class ModelUseLeaseService:
    def __init__(self, registry, *, protected_roots=(), managed_roots=(), use_verifier=None, terminal_verifier=None):
        self.registry, self.protected_roots = registry, tuple(protected_roots)
        self.managed_roots = tuple(managed_roots)
        self.use_verifier, self.terminal_verifier = use_verifier, terminal_verifier
        self._issued = {}

    def preview_release(self, pin_id):
        value, token = self.registry.snapshot()
        pin = cache_state(value)["leases"].get(pin_id)
        if not pin or pin.get("kind") != "manual" or pin.get("state") != "active":
            raise ModelResourceError("Choose an active manual pin; execution leases use their job's recovery.")
        result = ManualPinReleasePreview(uuid4().hex, token, pin_id, tuple(pin["entry_ids"]))
        if len(self._issued) >= 32:
            self._issued.pop(next(iter(self._issued)))
        self._issued[result.preview_id] = digest(asdict(result))
        return result

    def release(self, preview, *, confirmed=False):
        if not confirmed:
            raise ModelResourceError("Confirm external use is finished before releasing its manual pin.")
        if not isinstance(preview, ManualPinReleasePreview) or self._issued.get(preview.preview_id) != digest(asdict(preview)):
            raise ModelResourceError("Review an unchanged manual-pin release in this session.")
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            value, token = self.registry.snapshot()
            pin = cache_state(value)["leases"].get(preview.pin_id)
            if token != preview.registry_token or not pin or pin.get("kind") != "manual" or pin.get("state") != "active" or tuple(pin["entry_ids"]) != preview.entry_ids:
                raise ModelResourceError("The pin or registry changed. Review release again.")
            pin.update(state="released", released_at=now(), release_authority="explicit_manual_confirmation")
            self.registry._save(value, token)
            del self._issued[preview.preview_id]
            return preview.pin_id

    def acquire_use(self, operation_id, *, consumer_job_id):
        """Future execution owners must freshly attest their exact job binding."""
        if not isinstance(consumer_job_id, str) or not consumer_job_id.strip() or len(consumer_job_id) > 256 or self.use_verifier is None:
            raise ModelResourceError("Execution use requires an exact consumer job and its qualified admission verifier.")
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer(), ExitStack() as pins:
            value, token = self.registry.snapshot()
            state = cache_state(value)
            from .model_storage_policy import validate_policy_roots
            from .model_cache import ModelCacheService
            policy = validate_policy_roots(value.get("workspace", {}), protected_roots=self.protected_roots, managed_roots=self.managed_roots)
            if policy.mode != "tiered":
                raise ModelResourceError("Tiered execution leases require the current Fast storage policy.")
            verifier = ModelCacheService(self.registry, protected_roots=self.protected_roots, managed_roots=self.managed_roots)
            operation = state["operations"].get(operation_id)
            if not operation or operation.get("phase") != "complete" or operation.get("qualification") != "adapter_qualified":
                raise ModelResourceError("This preparation does not prove the adapter's complete required model set.")
            ids = tuple(operation["entry_ids"])
            bindings = []
            for key in ids:
                entry = verifier._verify_entry(value, key)
                if not entry or entry.get("state") != "available":
                    raise ModelResourceError("A prepared cache member is unavailable. Prepare the complete set again.")
                path = _path(entry["path"], self.protected_roots)
                from pathlib import Path
                if Path(policy.fast_root).absolute() not in path.parents:
                    raise ModelResourceError("This prepared copy belongs to an older Fast folder. Prepare against the current hot-only layout.")
                pins.enter_context(_pinned_model_file(path, self.protected_roots))
                inspection = inspect_model_resource(path)
                if inspection.resource_id != entry["resource_id"] or list(inspection.file_state) != entry["file_state"]:
                    raise ModelResourceError("A prepared cache file changed. Prepare the set again.")
                locations = value["resources"].get(entry["resource_id"], {}).get("locations", [])
                if not any(row.get("cache_entry_id") == key and row.get("path") == str(path) and row.get("file_state") == entry["file_state"] for row in locations):
                    raise ModelResourceError("A prepared model registry location changed.")
                bindings.append((entry["resource_id"], str(path)))
            if self.use_verifier(consumer_job_id, operation_id, tuple(bindings)) is not True:
                raise ModelResourceError("The consumer did not verify this exact prepared resource set.")
            if len(state["leases"]) >= 10000:
                raise ModelResourceError("Cache-use history is full; preserve its retained records.")
            lease_id = uuid4().hex
            state["leases"][lease_id] = {"kind": "execution", "state": "active", "entry_ids": list(ids),
                "consumer_job_id": consumer_job_id, "operation_id": operation_id, "created_at": now()}
            self.registry._save(value, token)
            return ModelUseLease(lease_id, consumer_job_id, ids, "active")

    def reconcile_use(self, lease_id):
        """Unknown outcome remains pinned, independent of clocks/process liveness."""
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            value, token = self.registry.snapshot()
            lease = cache_state(value)["leases"].get(lease_id)
            if not lease or lease.get("kind") != "execution":
                raise ModelResourceError("Choose a recorded execution lease.")
            if lease["state"] == "released":
                return ModelUseLease(lease_id, lease["consumer_job_id"], tuple(lease["entry_ids"]), "released")
            evidence = self.terminal_verifier(lease["consumer_job_id"], lease_id) if self.terminal_verifier else None
            if evidence in ("proven_unsubmitted", "server_stopped", "process_stopped"):
                lease.update(state="released", terminal_evidence=evidence, released_at=now())
            else:
                lease.update(state="uncertain", terminal_evidence="unavailable")
            self.registry._save(value, token)
            return ModelUseLease(lease_id, lease["consumer_job_id"], tuple(lease["entry_ids"]), lease["state"])
