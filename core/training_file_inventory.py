"""Bounded identities for explicitly selected trainer software directories.

This reads files, never imports them or discovers additional roots. Callers own
permission for the selected software; it must never point at image libraries,
model stores, credentials or other Local Only data.
"""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import threading

from .training_source_io import _assert_unredirected
from .training_windows_file_identity import assert_existing_unredirected, checked_path_identity, descriptor_identity


MAX_FILES = 200_000
MAX_BYTES = 64 * 1024 ** 3
MAX_MANIFEST_BYTES = 64 * 1024 ** 2
MAX_SECONDS = 180
HASH_WORKERS = 4
SHA = re.compile(r"[0-9a-f]{64}")


@lru_cache(maxsize=1)
def _drive_type():
    # Cache the function binding, never the current volume/readiness result.
    import ctypes
    from ctypes import wintypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = wintypes.UINT
    return function


class FileInventoryError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _check_path(path, directory=False):
    path = Path(path).absolute()
    if os.name == "nt" and _drive_type()(path.anchor) not in {2, 3, 5, 6}:
        raise FileInventoryError("Software inventory requires an available local volume.")
    try:
        assert_existing_unredirected(path)
        value = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        raise FileInventoryError("A selected software path is missing or redirected.") from exc
    if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)) or int(getattr(value, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
        raise FileInventoryError("Software inventory requires fully local regular files and directories.")
    identity = checked_path_identity(path, value)
    # Directory allocation size is not membership and can change from 0 to
    # 4096 merely when Windows enumerates it. Bind its identity/mtime/attributes
    # and the complete child inventory instead; retain exact file byte sizes.
    return (*identity[:2], 0, *identity[3:]) if directory else identity


def _relative(value, *, directory=False):
    if directory and value == ".":
        return
    if type(value) is not str or not value or len(value) > 2000 or "\\" in value or ":" in value or PurePosixPath(value).is_absolute():
        raise FileInventoryError("Invalid software inventory path.")
    if any(part in {"", ".", ".."} or part[-1:] in {".", " "} or any(ord(c) < 32 for c in part) for part in value.split("/")):
        raise FileInventoryError("Invalid software inventory path.")


def _control(cancel, deadline):
    if cancel and cancel():
        raise InterruptedError("Software inventory was cancelled.")
    if time.monotonic() > deadline:
        raise FileInventoryError("Software inventory exceeded its bounded inspection time.")


def inspection_control(should_cancel=None):
    """Share one time budget across selected roots or prelaunch checks."""
    deadline = time.monotonic() + MAX_SECONDS
    def check():
        _control(should_cancel, deadline)
        return False
    return check


def _hash_file(path, expected, cancel, deadline):
    digest = hashlib.sha256()
    _control(cancel, deadline)
    if _check_path(path) != expected:
        raise FileInventoryError("Software changed before hashing.")
    with Path(path).open("rb") as handle:
        if descriptor_identity(handle.fileno()) != expected:
            raise FileInventoryError("Software changed before reading.")
        while True:
            _control(cancel, deadline)
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
        if descriptor_identity(handle.fileno()) != expected:
            raise FileInventoryError("Software changed while reading.")
    if _check_path(path) != expected:
        raise FileInventoryError("Software changed while hashing.")
    return digest.hexdigest()


def _scan(root, cancel, deadline, progress=None):
    """Enumerate only the explicitly selected root, never following links."""
    directories, files, pending, seen, size = [], [], [Path(root)], set(), 0
    last = 0.0
    while pending:
        _control(cancel, deadline)
        directory = pending.pop()
        before = _check_path(directory, directory=True)
        relative = directory.relative_to(root).as_posix()
        directories.append((relative, before))
        with os.scandir(directory) as entries:
            for entry in entries:
                _control(cancel, deadline)
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                _relative(relative)
                folded = relative.casefold()
                if folded in seen or len(seen) >= MAX_FILES:
                    raise FileInventoryError("Software inventory exceeds its entry bound or repeats a path.")
                seen.add(folded)
                if progress and time.monotonic() - last >= .25:
                    progress(len(seen), 0, "Checking selected software directory entries")
                    last = time.monotonic()
                # Check the path before classifying or traversing it. Reparse
                # points/cloud placeholders are never followed or hydrated.
                value = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(value.st_mode):
                    _check_path(path, directory=True)
                    pending.append(path)
                else:
                    identity = _check_path(path)
                    size += identity[2]
                    if size > MAX_BYTES:
                        raise FileInventoryError("Software inventory exceeds its byte bound.")
                    files.append((relative, identity))
        if _check_path(directory, directory=True) != before:
            raise FileInventoryError("Software directory changed during enumeration.")
    return tuple(sorted(directories)), tuple(sorted(files)), size


@dataclass(frozen=True)
class SoftwareInventory:
    root: str
    root_identity: tuple[int, ...]
    directories: tuple[tuple[str, tuple[int, ...]], ...]
    files: tuple[tuple[str, tuple[int, ...], str], ...]

    @property
    def byte_count(self):
        return sum(item[1][2] for item in self.files)

    def payload(self):
        return {"schema_version": 1, "root_identity": self.root_identity,
            "directories": self.directories, "files": self.files}

    @property
    def fingerprint(self):
        return hashlib.sha256(canonical(self.payload())).hexdigest()

    def summary(self):
        return {"schema_version": 1, "inventory_sha256": self.fingerprint,
            "files": len(self.files), "directories": len(self.directories),
            "bytes": self.byte_count, "scope": "explicit_software_directory"}


class SoftwareInventoryService:
    def capture(self, root, *, should_cancel=None, progress=None):
        root = Path(root).absolute()
        before = _check_path(root, directory=True)[:2]
        deadline = time.monotonic() + MAX_SECONDS
        directories, files, total = _scan(root, should_cancel, deadline, progress)
        result, completed, last = [], 0, 0.0
        # Only read-only hashing overlaps. Enumeration, callbacks, manifest
        # writes, process ownership and publication remain on the caller.
        # At most eight pending digests and four 1-MiB read buffers are live.
        aborted = threading.Event()
        pending, remaining = [], iter(files)
        with ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="software-hash") as pool:
            try:
                while True:
                    _control(should_cancel, deadline)
                    while len(pending) < 2 * HASH_WORKERS:
                        item = next(remaining, None)
                        if item is None:
                            break
                        relative, identity = item
                        future = pool.submit(_hash_file, root / relative, identity, aborted.is_set, deadline)
                        pending.append((relative, identity, future))
                    if not pending:
                        break
                    relative, identity, future = pending[0]
                    try:
                        digest = future.result(timeout=.1)
                    except FutureTimeout:
                        continue
                    pending.pop(0)
                    result.append((relative, identity, digest))
                    completed += identity[2]
                    if progress and time.monotonic() - last >= .25:
                        progress(completed, total, "Verifying selected software bytes")
                        last = time.monotonic()
            finally:
                aborted.set()
                for _, _, future in pending:
                    future.cancel()
        if _scan(root, should_cancel, deadline, progress) != (directories, files, total):
            raise FileInventoryError("Software inventory changed during hashing.")
        return SoftwareInventory(str(root), before, directories, tuple(result))

    @staticmethod
    def save(inventory, destination):
        """Create an exact new manifest outside the inventoried software root."""
        path = Path(destination).absolute()
        if path.is_relative_to(Path(inventory.root)):
            raise FileInventoryError("Retain the inventory outside its selected software directory.")
        _check_path(path.parent, directory=True)
        _assert_unredirected(path)
        raw = canonical(inventory.payload())
        if len(raw) > MAX_MANIFEST_BYTES:
            raise FileInventoryError("Software inventory manifest exceeds its size bound.")
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        loaded = SoftwareInventoryService.load(inventory.root, path)
        if loaded != inventory:
            raise FileInventoryError("Software inventory failed exact read-back verification.")

    @staticmethod
    def load(root, manifest):
        path = Path(manifest).absolute()
        before = _check_path(path)
        if before[2] > MAX_MANIFEST_BYTES:
            raise FileInventoryError("Software inventory manifest exceeds its size bound.")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise FileInventoryError("Software inventory contains duplicate fields.")
                result[key] = value
            return result
        with path.open("rb") as handle:
            if descriptor_identity(handle.fileno()) != before:
                raise FileInventoryError("Software inventory changed before reading.")
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
            if descriptor_identity(handle.fileno()) != before:
                raise FileInventoryError("Software inventory changed while reading.")
        if len(raw) != before[2] or _check_path(path) != before:
            raise FileInventoryError("Software inventory changed while reading.")
        try:
            value = json.loads(raw, object_pairs_hook=pairs)
            if type(value) is not dict or set(value) != {"schema_version", "root_identity", "directories", "files"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
                raise FileInventoryError("Unsupported software inventory schema.")
            identity = value["root_identity"]
            if type(identity) is not list or len(identity) != 2 or any(type(item) is not int or item < 0 for item in identity):
                raise FileInventoryError("Invalid software root identity.")
            seen, total = set(), 0
            for field, width in (("directories", 2), ("files", 3)):
                rows = value[field]
                if type(rows) is not list or len(rows) > MAX_FILES:
                    raise FileInventoryError("Invalid software inventory entry count.")
                for row in rows:
                    if type(row) is not list or len(row) != width:
                        raise FileInventoryError("Invalid software inventory entry.")
                    relative, state = row[:2]
                    _relative(relative, directory=field == "directories")
                    if relative.casefold() in seen or type(state) is not list or len(state) != 5 or any(type(item) is not int or item < 0 for item in state):
                        raise FileInventoryError("Invalid software inventory identity.")
                    seen.add(relative.casefold())
                    if field == "files":
                        if type(row[2]) is not str or not SHA.fullmatch(row[2]):
                            raise FileInventoryError("Invalid software file digest.")
                        total += state[2]
            if not value["directories"] or value["directories"][0][0] != "." or value["directories"][0][1][:2] != identity or len(seen) > MAX_FILES + 1 or total > MAX_BYTES:
                raise FileInventoryError("Invalid software inventory root or bounds.")
            inventory = SoftwareInventory(str(Path(root).absolute()), tuple(identity),
                tuple((name, tuple(state)) for name, state in value["directories"]),
                tuple((name, tuple(state), digest) for name, state, digest in value["files"]))
            if canonical(inventory.payload()) != raw:
                raise FileInventoryError("Software inventory is not in its canonical format.")
            return inventory
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise FileInventoryError("Invalid software inventory data.") from exc

    def verify(self, root, manifest, *, should_cancel=None, progress=None):
        expected = self.load(root, manifest)
        actual = self.capture(root, should_cancel=should_cancel, progress=progress)
        if actual != expected:
            raise FileInventoryError("Selected software bytes or directory membership changed.")
        return actual.summary()
