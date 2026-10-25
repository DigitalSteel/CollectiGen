"""Private append-only import coordination records, not owner recovery authority."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .image_record import _atomic_create_text
from .model_resource_registry import ModelResourceError, _bounded_read, _json
from .model_workspace import _path, _pinned_parent

MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 1024
MAX_APPROVAL_ENTRIES = 512
MAX_RECENT_APPROVALS = 50
EVENTS = frozenset({"intent", "started", "outcome", "reconciled", "revision"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STEP = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")


def encoded(value):
    try:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ModelResourceError("Unsupported import receipt data.") from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise ModelResourceError("Import receipt exceeds its bounded size.")
    return raw


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


@contextmanager
def _lock(path, protected_roots):
    path = _path(path, protected_roots)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        info, named = os.fstat(handle.fileno()), path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise ModelResourceError("Import lock is not a stable private regular file.")
        if info.st_size == 0:
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
            raise ModelResourceError("Another operation owns this import plan. Retry after it finishes.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ImportReceiptStore:
    """Only named, validated ledger files are read; owner locators stay inert."""
    def __init__(self, directory, *, protected_roots=()):
        self.directory = Path(directory)
        self.protected_roots = tuple(protected_roots)

    def _directory(self, identity):
        if not isinstance(identity, str) or not _HASH.fullmatch(identity):
            raise ModelResourceError("Choose an exact import approval identity.")
        return _path(self.directory / identity, self.protected_roots)

    def recent_approvals(self, *, limit=MAX_RECENT_APPROVALS):
        """List only bounded canonical approval directories; never follow entries."""
        if type(limit) is not int or not 1 <= limit <= MAX_RECENT_APPROVALS:
            raise ModelResourceError("Choose a bounded saved-import count.")
        root = _path(self.directory, self.protected_roots)
        try:
            info = root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return ()
        if not stat.S_ISDIR(info.st_mode):
            raise ModelResourceError("Saved-import storage is not a local directory.")
        entries = []
        with os.scandir(root) as listing:
            for index, entry in enumerate(listing):
                if index >= MAX_APPROVAL_ENTRIES:
                    raise ModelResourceError(
                        "Saved-import history exceeds the bounded list. Browse an exact plan.json instead."
                    )
                if not _HASH.fullmatch(entry.name):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    raise ModelResourceError("A saved-import entry is not an independent directory.")
                plan = root / entry.name / "plan.json"
                try:
                    plan_info = plan.stat(follow_symlinks=False)
                except FileNotFoundError:
                    entries.append((0, entry.name))
                    continue
                if not stat.S_ISREG(plan_info.st_mode) or plan_info.st_nlink != 1:
                    raise ModelResourceError("A saved-import plan is not an independent regular file.")
                entries.append((plan_info.st_mtime_ns, entry.name))
        entries.sort(reverse=True)
        return tuple((identity, timestamp) for timestamp, identity in entries[:limit])

    def _read(self, path):
        path = _path(path, self.protected_roots)
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            raise ModelResourceError("An import receipt is missing; preserve remaining evidence.") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ModelResourceError("Import receipts must be independent regular files.")
        if int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
            raise ModelResourceError("Import receipts must be local; automatic hydration is disabled.")
        raw = _bounded_read(path, MAX_RECORD_BYTES)
        if raw is None:
            raise ModelResourceError("An import receipt disappeared.")
        return _json(raw)

    def _publish(self, path, value):
        raw = encoded(value)
        _path(path, self.protected_roots)
        _atomic_create_text(path, raw.decode("utf-8"))
        if self._read(path) != value:
            raise ModelResourceError("Import receipt read-back failed; preserve the recorded operation.")

    def create(self, approved_payload):
        if not isinstance(approved_payload, dict):
            raise ModelResourceError("An approved import must be a bounded metadata record.")
        identity = digest(approved_payload)
        directory = self._directory(identity)
        pending, ancestor = [], directory
        while not ancestor.exists():
            pending.append(ancestor); ancestor = ancestor.parent
        _path(ancestor, self.protected_roots)
        with _pinned_parent(ancestor, self.protected_roots):
            for parent in reversed(pending):
                _path(parent, self.protected_roots)
                parent.mkdir(exist_ok=True)
            with _pinned_parent(directory, self.protected_roots), _lock(directory / "ledger.lock", self.protected_roots):
                path = directory / "plan.json"
                record = {"schema_version": 1, "approval_id": identity, "approved": approved_payload}
                if path.exists():
                    if self._read(path) != record:
                        raise ModelResourceError("Existing import approval differs; its records were preserved.")
                else:
                    self._publish(path, record)
        return identity

    def read(self, identity):
        value = self._read(self._directory(identity) / "plan.json")
        if (not isinstance(value, dict) or set(value) != {"schema_version", "approval_id", "approved"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["approval_id"] != identity or not isinstance(value["approved"], dict)
                or digest(value["approved"]) != identity):
            raise ModelResourceError("Import approval is changed or unsupported; review a new plan.")
        return value["approved"]

    def events(self, identity):
        self.read(identity)
        directory = self._directory(identity)
        names = []
        with os.scandir(directory) as entries:
            for index, item in enumerate(entries):
                if index >= MAX_EVENTS + 16:
                    raise ModelResourceError("Import receipt directory exceeds its bounded history.")
                if re.fullmatch(r"[0-9]{6}\.json", item.name):
                    names.append(item.name)
                elif item.name in {"plan.json", "ledger.lock", "execution.lock"}:
                    continue
                elif re.fullmatch(r"\.(?:plan|[0-9]{6})\.json\.[0-9a-f]{32}\.tmp", item.name):
                    continue  # Interrupted unpublished metadata; never execution evidence.
                else:
                    raise ModelResourceError("Unexpected import receipt contents; preserve them for review.")
        if len(names) > MAX_EVENTS or sorted(names) != [f"{index:06d}.json" for index in range(1, len(names) + 1)]:
            raise ModelResourceError("Import receipt sequence is missing or exceeds its bound.")
        previous, results = identity, []
        for index, name in enumerate(sorted(names), 1):
            value = self._read(directory / name)
            if (not isinstance(value, dict) or set(value) != {"schema_version", "sequence", "previous", "event", "step_id", "payload"}
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or type(value["sequence"]) is not int or value["sequence"] != index
                    or value["previous"] != previous or not isinstance(value["event"], str)
                    or value["event"] not in EVENTS
                    or not isinstance(value["step_id"], str) or not _STEP.fullmatch(value["step_id"])
                    or not isinstance(value["payload"], dict)):
                raise ModelResourceError("Import receipt chain is changed or unsupported.")
            previous = digest(value); results.append(value)
        return tuple(results)

    def append(self, identity, event, step_id, payload, *, expected_head=None):
        if not isinstance(event, str) or event not in EVENTS or not isinstance(step_id, str) or not _STEP.fullmatch(step_id) or not isinstance(payload, dict):
            raise ModelResourceError("Unsupported import event.")
        directory = self._directory(identity)
        with _pinned_parent(directory, self.protected_roots), _lock(directory / "ledger.lock", self.protected_roots):
            events = self.events(identity)
            head = digest(events[-1]) if events else identity
            if expected_head is not None and expected_head != head:
                raise ModelResourceError("The import history changed after review; review the remaining work again.")
            if len(events) >= MAX_EVENTS:
                raise ModelResourceError("Import receipt history is full; preserve it for review.")
            value = {"schema_version": 1, "sequence": len(events) + 1,
                "previous": head,
                "event": event, "step_id": step_id, "payload": payload}
            self._publish(directory / f"{len(events) + 1:06d}.json", value)
        return value

    @contextmanager
    def execution_lock(self, identity):
        self.read(identity)
        directory = self._directory(identity)
        with _pinned_parent(directory, self.protected_roots), _lock(directory / "execution.lock", self.protected_roots):
            yield
