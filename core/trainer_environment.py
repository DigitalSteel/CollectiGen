"""Exact, separate trainer software generations; no acquisition or app activation.

Callers supply a reviewed pinned software manifest and explicitly selected roots.
Nothing searches installations, imports trainer packages, changes sys.path, loads
models, or grants execution compatibility. A health probe is a caller-owned,
bounded operation; it must not install software or load user/model data.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

from .training_file_inventory import (
    FileInventoryError, MAX_BYTES, MAX_FILES, MAX_MANIFEST_BYTES, MAX_SECONDS,
    _check_path, _control, _hash_file, _scan, canonical,
)
from .training_source_io import _assert_unredirected, _identity


SCHEMA_VERSION = 1
CHUNK_BYTES = 1024 * 1024
FREE_SPACE_RESERVE = 64 * 1024 * 1024
MAX_GENERATIONS = 1000
_SHA = re.compile(r"[0-9a-f]{64}")
_GENERATION = re.compile(r"[0-9a-f]{32}")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{x}" for x in range(10)),
             *(f"lpt{x}" for x in range(10))}
_DATA_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth.tar", ".png", ".jpg",
                  ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".parquet"}
_DATA_DIRECTORIES = {"models", "datasets", "training", "output", "images", "captions"}


# Upper bound includes the health worker (up to 600s) and its bounded IO phases.
HEALTH_PHASE_SECONDS = 5 * MAX_SECONDS + 600


def software_phase_seconds(profile):
    """Bound one complete copy/verification by its reviewed software size.

    A fixed three-minute limit rejected the qualified 6.4 GB / 20,837-file
    runtime on a later read. Allow 16 MiB/s plus 20 ms/file, capped at 30
    minutes. This is an inspection limit, not a promised installation speed.
    """
    estimated = 30 + profile.byte_count / (16 * 1024**2) + len(profile.files) * .02
    return min(1800, max(MAX_SECONDS, math.ceil(estimated)))


class TrainerEnvironmentError(RuntimeError):
    pass


class TrainerEnvironmentBusy(TrainerEnvironmentError):
    pass


def _relative(name: str) -> PurePosixPath:
    if type(name) is not str or not name or len(name) > 2000:
        raise TrainerEnvironmentError("Invalid trainer software path.")
    path = PurePosixPath(name)
    parts = name.split("/")
    if (path.is_absolute() or path.as_posix() != name or "\\" in name
            or any(part in {"", ".", ".."} or part.rstrip(" .") != part
                   or part.split(".", 1)[0].casefold() in _RESERVED
                   or any(ord(c) < 32 or c in '<>:"|?*' for c in part) for part in parts)):
        raise TrainerEnvironmentError("Unsafe trainer software path.")
    return path


def _text(value, field):
    if type(value) is not str or not value.strip() or len(value) > 8000:
        raise TrainerEnvironmentError(f"Missing or invalid {field}.")
    return value


def _source(value, field):
    _text(value, field)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise TrainerEnvironmentError(f"Invalid {field} URL.") from exc
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise TrainerEnvironmentError(f"{field} requires a public HTTPS source without credentials.")


def _digest(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise TrainerEnvironmentError("A pinned software SHA-256 is missing or invalid.")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise TrainerEnvironmentError("Trainer software data contains duplicate fields.")
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError) as exc:
        raise TrainerEnvironmentError("Invalid trainer software JSON.") from exc


@dataclass(frozen=True)
class TrainerProfile:
    """Canonical, immutable identity of all supplied software and attribution."""

    _canonical: bytes

    @classmethod
    def from_dict(cls, value):
        try:
            raw = canonical(value)
        except (ValueError, TypeError) as exc:
            raise TrainerEnvironmentError("Invalid pinned trainer profile.") from exc
        if len(raw) > MAX_MANIFEST_BYTES:
            raise TrainerEnvironmentError("Trainer profile exceeds its manifest bound.")
        value = _decode(raw)
        if (type(value) is not dict or set(value) != {
                "schema_version", "profile_id", "python", "trainer", "backend", "files"}
                or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION):
            raise TrainerEnvironmentError("Unsupported trainer profile schema.")
        _text(value["profile_id"], "profile identity")
        for key, fields in (("python", {"version", "executable", "source_url", "source_sha256", "license"}),
                            ("trainer", {"name", "revision", "source_url", "source_sha256", "license"}),
                            ("backend", {"name", "version", "platform"})):
            item = value[key]
            if type(item) is not dict or set(item) != fields:
                raise TrainerEnvironmentError(f"Invalid pinned {key} metadata.")
            for field in fields:
                _text(item[field], f"{key} {field}")
            if key != "backend":
                _source(item["source_url"], f"{key} source")
                _digest(item["source_sha256"])
        _relative(value["python"]["executable"])
        if (value["trainer"]["name"] != "sd-scripts"
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["trainer"]["revision"])):
            raise TrainerEnvironmentError("The trainer requires an exact sd-scripts revision.")
        files = value["files"]
        if type(files) is not list or not files or len(files) > MAX_FILES:
            raise TrainerEnvironmentError("Trainer profile requires a bounded software inventory.")
        seen, total = set(), 0
        for item in files:
            if type(item) is not dict or set(item) != {"path", "size_bytes", "sha256", "role", "source_url", "license"}:
                raise TrainerEnvironmentError("Invalid trainer software file record.")
            path = _relative(item["path"])
            folded = item["path"].casefold()
            if (folded in seen or any(parent.as_posix().casefold() in seen for parent in path.parents)
                    or type(item["size_bytes"]) is not int or item["size_bytes"] < 0
                    or type(item["role"]) is not str
                    or item["role"] not in {"python", "trainer", "dependency", "license"}):
                raise TrainerEnvironmentError("Invalid or colliding trainer software inventory.")
            if (path.suffix.casefold() in _DATA_SUFFIXES
                    or path.parts[0].casefold() in _DATA_DIRECTORIES):
                raise TrainerEnvironmentError("Trainer software packs cannot contain models or datasets.")
            _digest(item["sha256"])
            _source(item["source_url"], "software source")
            _text(item["license"], "software license")
            total += item["size_bytes"]
            if total > MAX_BYTES:
                raise TrainerEnvironmentError("Trainer software exceeds its unpacked-size bound.")
            seen.add(folded)
        # Check both directions independently of manifest ordering.
        if any(parent.as_posix().casefold() in seen for item in files
               for parent in PurePosixPath(item["path"]).parents):
            raise TrainerEnvironmentError("Trainer software has a file/directory collision.")
        if not any(item["path"] == value["python"]["executable"] and item["role"] == "python" for item in files):
            raise TrainerEnvironmentError("The standalone Python executable is absent from its software inventory.")
        if not any(item["role"] == "trainer" for item in files):
            raise TrainerEnvironmentError("Trainer source files are absent from the software inventory.")
        value["files"] = sorted(files, key=lambda item: item["path"])
        return cls(canonical(value))

    @property
    def sha256(self):
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def profile_id(self):
        return self.payload()["profile_id"]

    @property
    def files(self):
        return tuple(self.payload()["files"])

    @property
    def byte_count(self):
        return sum(item["size_bytes"] for item in self.files)

    def payload(self):
        return _decode(self._canonical)


@dataclass(frozen=True)
class TrainerHealth:
    healthy: bool
    details: str


@dataclass(frozen=True)
class TrainerGeneration:
    generation_id: str
    software_root: Path
    profile_sha256: str
    health: TrainerHealth
    health_checked_at: float
    health_inventory_sha256: str
    execution_ready: bool = False


@dataclass(frozen=True)
class TrainerEnvironmentStatus:
    mode: str
    state: str
    generation: TrainerGeneration | None = None
    details: str = ""
    execution_ready: bool = False


HealthProbe = Callable[[Path, TrainerProfile], TrainerHealth]


def _read_json(path, *, limit=MAX_MANIFEST_BYTES):
    before = _check_path(path)
    if before[2] > limit:
        raise TrainerEnvironmentError("Trainer environment record exceeds its size bound.")
    with Path(path).open("rb") as handle:
        if _identity(os.fstat(handle.fileno())) != before:
            raise TrainerEnvironmentError("Trainer record changed before reading.")
        raw = handle.read(limit + 1)
        if _identity(os.fstat(handle.fileno())) != before:
            raise TrainerEnvironmentError("Trainer record changed while reading.")
    if len(raw) != before[2] or _check_path(path) != before:
        raise TrainerEnvironmentError("Trainer record changed while reading.")
    result = _decode(raw)
    if canonical(result) != raw:
        raise TrainerEnvironmentError("Trainer record is not canonical.")
    return result


def _write_json(path, value):
    raw = canonical(value)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise TrainerEnvironmentError("Trainer record exceeds its size bound.")
    _assert_unredirected(path)
    _check_path(path.parent, directory=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if _read_json(path) != value:
        raise TrainerEnvironmentError("Trainer record failed read-back verification.")


def _verify_software(root, profile, cancel, deadline):
    """Reject extra files before hashing; only the selected software root is read."""
    root = Path(root).absolute()
    directories, actual, total = _scan(root, cancel, deadline)
    expected = {item["path"]: item for item in profile.files}
    if {name for name, _ in actual} != set(expected) or total != profile.byte_count:
        raise TrainerEnvironmentError("Trainer software membership does not match the pinned profile.")
    for name, identity in actual:
        record = expected[name]
        if identity[2] != record["size_bytes"] or _hash_file(root / name, identity, cancel, deadline) != record["sha256"]:
            raise TrainerEnvironmentError(f"Trainer software verification failed: {name}.")
    if _scan(root, cancel, deadline) != (directories, actual, total):
        raise TrainerEnvironmentError("Trainer software changed during verification.")
    return hashlib.sha256(canonical({"directories": directories, "files": actual,
                                    "profile_sha256": profile.sha256})).hexdigest()


def _copy_file(source, destination, record, cancel, deadline):
    before = _check_path(source)
    if before[2] != record["size_bytes"]:
        raise TrainerEnvironmentError("Selected trainer software has an unexpected size.")
    _assert_unredirected(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_path(destination.parent, directory=True)
    digest, copied = hashlib.sha256(), 0
    with source.open("rb") as handle, destination.open("xb") as output:
        if _identity(os.fstat(handle.fileno())) != before:
            raise TrainerEnvironmentError("Selected trainer software changed before copying.")
        while True:
            _control(cancel, deadline)
            block = handle.read(min(CHUNK_BYTES, record["size_bytes"] - copied + 1))
            if not block:
                break
            copied += len(block)
            if copied > record["size_bytes"]:
                raise TrainerEnvironmentError("Selected trainer software exceeded its pinned byte count.")
            output.write(block)
            digest.update(block)
        output.flush()
        os.fsync(output.fileno())
        if _identity(os.fstat(handle.fileno())) != before:
            raise TrainerEnvironmentError("Selected trainer software changed while copying.")
    if _check_path(source) != before or copied != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
        raise TrainerEnvironmentError("Selected trainer software failed copy verification.")


class TrainerEnvironmentService:
    def __init__(self, root, profile: TrainerProfile, *, health_probe: HealthProbe | None = None,
                 retain_stage=None):
        if retain_stage is not None and not callable(retain_stage):
            raise ValueError("The trainer stage recovery recorder must be callable.")
        self.retain_stage = retain_stage
        self.root = Path(root).absolute()
        # An external environment may live beside the app, never in its Python.
        for runtime in (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()):
            if self.root.resolve().is_relative_to(runtime):
                raise TrainerEnvironmentError("Trainer software must remain outside the app Python runtime.")
        self.profile = TrainerProfile.from_dict(profile.payload())
        self.health_probe = health_probe

    @contextmanager
    def _locked(self):
        _assert_unredirected(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _check_path(self.root, directory=True)
        path = self.root / ".operation.lock"
        _assert_unredirected(path)
        with path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if _identity(os.fstat(handle.fileno())) != _check_path(path):
                raise TrainerEnvironmentError("Trainer operation lock changed before acquisition.")
            locked = False
            try:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError as exc:
                    raise TrainerEnvironmentBusy("Another trainer environment operation owns this location.") from exc
                yield
            finally:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _health(self, root, cancel, deadline):
        _control(cancel, deadline)
        if self.health_probe is None:
            raise TrainerEnvironmentError("A fresh trainer health probe is required; installation is unverified.")
        # Native health has its own bounded preparation, process verification,
        # worker and read-back phases; it cannot share one file-copy deadline.
        health_deadline = time.monotonic() + HEALTH_PHASE_SECONDS
        health = self.health_probe(root, self.profile)
        _control(cancel, health_deadline)
        if (not isinstance(health, TrainerHealth) or type(health.healthy) is not bool
                or type(health.details) is not str or len(health.details) > 8000):
            raise TrainerEnvironmentError("The trainer health probe returned invalid evidence.")
        if not health.healthy:
            raise TrainerEnvironmentError(f"Trainer health verification failed: {health.details}")
        return health

    def _generation_path(self, generation_id):
        if type(generation_id) is not str or not _GENERATION.fullmatch(generation_id):
            raise TrainerEnvironmentError("Invalid trainer generation identity.")
        return self.root / "generations" / generation_id

    def _load_generation(self, generation_id):
        root = self._generation_path(generation_id)
        _check_path(root, directory=True)
        receipt = _read_json(root / "receipt.json")
        if (type(receipt) is not dict or set(receipt) != {
                "schema_version", "generation_id", "profile", "profile_sha256", "health", "health_checked_at", "health_inventory_sha256"}
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != SCHEMA_VERSION
                or receipt["generation_id"] != generation_id
                or receipt["profile_sha256"] != self.profile.sha256
                or TrainerProfile.from_dict(receipt["profile"]).sha256 != self.profile.sha256
                or type(receipt["health"]) is not dict or set(receipt["health"]) != {"healthy", "details"}
                or receipt["health"].get("healthy") is not True
                or type(receipt["health"].get("details")) is not str
                or len(receipt["health"]["details"]) > 8000
                or type(receipt["health_checked_at"]) not in {int, float}
                or not 0 < receipt["health_checked_at"] <= time.time() + 60):
            raise TrainerEnvironmentError("Trainer generation does not match its completed pinned profile.")
        _digest(receipt["health_inventory_sha256"])
        return TrainerGeneration(generation_id, root / "software", self.profile.sha256,
                                 TrainerHealth(**receipt["health"]), receipt["health_checked_at"],
                                 receipt["health_inventory_sha256"])

    def verify_generation(self, generation_id, *, cancel=None):
        """Check immutable receipt and current software bytes; no process runs."""
        result = self._load_generation(generation_id)
        current = _verify_software(result.software_root, self.profile, cancel, time.monotonic() + software_phase_seconds(self.profile))
        if current != result.health_inventory_sha256:
            raise TrainerEnvironmentError("Trainer software identity changed since its health verification.")
        return result

    def status(self, *, cancel=None):
        """Read explicit managed storage only. Health is dated evidence, not launch authority."""
        try:
            pointer = self.root / "active.json"
            _assert_unredirected(pointer)
            if not pointer.exists():
                if self.root.exists():
                    _check_path(self.root, directory=True)
                    return TrainerEnvironmentStatus("managed", "not_selected", details="No trainer generation is active; retained generations and interrupted stages are preserved.")
                return TrainerEnvironmentStatus("manual", "not_installed", details="Manual export or an explicitly selected existing installation remains available.")
            active = _read_json(pointer)
            if (type(active) is not dict or set(active) != {"schema_version", "generation_id", "profile_sha256"}
                    or type(active["schema_version"]) is not int or active["schema_version"] != SCHEMA_VERSION
                    or active["profile_sha256"] != self.profile.sha256):
                raise TrainerEnvironmentError("Active trainer profile does not match the selected profile.")
            generation = self.verify_generation(active["generation_id"], cancel=cancel)
            return TrainerEnvironmentStatus("managed", "installed", generation,
                                            "Software verified; recorded health is dated and execution compatibility is separate.")
        except (TrainerEnvironmentError, FileInventoryError, OSError, ValueError) as exc:
            if isinstance(exc, InterruptedError):
                raise
            return TrainerEnvironmentStatus("managed", "unverified", details=str(exc))

    def inspect_existing(self, root, *, cancel=None):
        """Inspect only this user-selected software directory, without adopting it."""
        root = Path(root).absolute()
        deadline = time.monotonic() + software_phase_seconds(self.profile)
        try:
            before = _verify_software(root, self.profile, cancel, deadline)
            health = self._health(root, cancel, deadline)
            deadline = time.monotonic() + software_phase_seconds(self.profile)
            if _verify_software(root, self.profile, cancel, deadline) != before:
                raise TrainerEnvironmentError("Trainer software identity changed during its health probe.")
            generation = TrainerGeneration("", root, self.profile.sha256, health, time.time(), before)
            return TrainerEnvironmentStatus("existing", "healthy", generation,
                                            "Existing software passed health; recipe/model/device compatibility remains separate.")
        except (TrainerEnvironmentError, FileInventoryError, OSError, ValueError) as exc:
            if isinstance(exc, InterruptedError):
                raise
            return TrainerEnvironmentStatus("existing", "unverified", details=str(exc))

    def _stage(self, populate, *, cancel, activate):
        # Called under the OS operation lock, including offline import.
        deadline = time.monotonic() + software_phase_seconds(self.profile)
        _control(cancel, deadline)
        if shutil.disk_usage(self.root).free < self.profile.byte_count + FREE_SPACE_RESERVE:
            raise TrainerEnvironmentError("Not enough free space for a new trainer generation; the existing generation is preserved.")
        for name in ("staging", "generations"):
            path = self.root / name
            _assert_unredirected(path)
            path.mkdir(exist_ok=True)
            _check_path(path, directory=True)
        generation_id = uuid.uuid4().hex
        stage = self.root / "staging" / generation_id
        stage.mkdir()
        software = stage / "software"
        software.mkdir()
        _write_json(stage / "operation.json", {"schema_version": SCHEMA_VERSION,
                    "generation_id": generation_id, "profile_sha256": self.profile.sha256})
        # The queue retains this exact stage before copying software or native
        # health. Failed persistence leaves an empty, named stage for recovery.
        if self.retain_stage is not None:
            self.retain_stage(stage, self.profile.sha256)
        _control(cancel, deadline)
        # Failed/cancelled stages are retained for inspection/recovery; never
        # remove earlier generations or overwrite an active installation.
        # Copy/extraction and exact membership verification are distinct bounded
        # IO phases. A completed phase must pass its own deadline before renewal.
        populated_deadline = populate(software, cancel, deadline)
        _control(cancel, populated_deadline if populated_deadline is not None else deadline)
        deadline = time.monotonic() + software_phase_seconds(self.profile)
        before = _verify_software(software, self.profile, cancel, deadline)
        health = self._health(software, cancel, deadline)
        deadline = time.monotonic() + software_phase_seconds(self.profile)
        if _verify_software(software, self.profile, cancel, deadline) != before:
            raise TrainerEnvironmentError("Trainer software identity changed during its health probe.")
        _control(cancel, deadline)
        _write_json(stage / "receipt.json", {"schema_version": SCHEMA_VERSION,
            "generation_id": generation_id, "profile": self.profile.payload(),
            "profile_sha256": self.profile.sha256,
            "health": {"healthy": health.healthy, "details": health.details},
            "health_checked_at": time.time(), "health_inventory_sha256": before})
        _control(cancel, deadline)
        destination = self._generation_path(generation_id)
        _assert_unredirected(destination)
        stage.rename(destination)
        result = self.verify_generation(generation_id, cancel=cancel)
        if activate:
            result = self._activate(generation_id, cancel=cancel)
        return result

    def install(self, source_root, *, cancel=None, activate=False):
        """Copy an explicit complete software tree; never download or execute an installer."""
        source_root = Path(source_root).absolute()
        if source_root.is_relative_to(self.root) or self.root.is_relative_to(source_root):
            raise TrainerEnvironmentError("Select a software source outside managed trainer storage.")
        def populate(destination, should_cancel, deadline):
            _verify_software(source_root, self.profile, should_cancel, deadline)
            _control(should_cancel, deadline)
            copy_deadline = time.monotonic() + software_phase_seconds(self.profile)
            for record in self.profile.files:
                _copy_file(source_root / record["path"], destination / record["path"],
                           record, should_cancel, copy_deadline)
            _control(should_cancel, copy_deadline)
            return copy_deadline
        with self._locked():
            return self._stage(populate, cancel=cancel, activate=activate)

    def repair(self, source_root, *, cancel=None, activate=False):
        """Create a verified replacement generation; explicit activation is separate."""
        return self.install(source_root, cancel=cancel, activate=activate)

    def _activate(self, generation_id, *, cancel):
        deadline = time.monotonic() + software_phase_seconds(self.profile)
        result = self.verify_generation(generation_id, cancel=cancel)
        health = self._health(result.software_root, cancel, deadline)
        deadline = time.monotonic() + software_phase_seconds(self.profile)
        if _verify_software(result.software_root, self.profile, cancel, deadline) != result.health_inventory_sha256:
            raise TrainerEnvironmentError("Trainer software identity changed during its health probe.")
        pointer = self.root / "active.json"
        _assert_unredirected(pointer)
        before = _check_path(pointer) if pointer.exists() else None
        previous = _read_json(pointer) if before is not None else None
        value = {"schema_version": SCHEMA_VERSION, "generation_id": generation_id,
                 "profile_sha256": self.profile.sha256}
        temporary = self.root / f".active-{uuid.uuid4().hex}.json"
        _write_json(temporary, value)
        _control(cancel, deadline)
        if (_check_path(pointer) if pointer.exists() else None) != before:
            raise TrainerEnvironmentError("Active trainer changed during activation.")
        published_identity = _check_path(temporary)
        os.replace(temporary, pointer)
        try:
            if _read_json(pointer) != value:
                raise TrainerEnvironmentError("Active trainer pointer failed read-back verification.")
        except Exception:
            # Preserve the prior pointer in a new exclusive record before its
            # restoration. A first activation failure stays visibly unverified.
            if _check_path(pointer) != published_identity:
                raise TrainerEnvironmentError("Active trainer changed during publication; explicit recovery is required.")
            if previous is not None:
                recovery = self.root / f".recovery-{uuid.uuid4().hex}.json"
                _write_json(recovery, previous)
                os.replace(recovery, pointer)
                if _read_json(pointer) != previous:
                    raise TrainerEnvironmentError("Active trainer restoration requires recovery.")
            else:
                pointer.rename(self.root / f".failed-active-{uuid.uuid4().hex}.json")
            raise
        return TrainerGeneration(result.generation_id, result.software_root,
                                 result.profile_sha256, health, time.time(), result.health_inventory_sha256)

    def activate(self, generation_id, *, cancel=None):
        with self._locked():
            return self._activate(generation_id, cancel=cancel)

    def rollback(self, generation_id, *, cancel=None):
        """Explicitly reactivate a retained generation after current bytes and health pass."""
        return self.activate(generation_id, cancel=cancel)

    def recover_interrupted(self):
        """Return retained abandoned staging IDs, never auto-activate or delete them.

        The OS lock is released on process exit, so no PID guessing or stale-lock
        deletion is needed. Recovery through repair/import creates a new generation.
        """
        with self._locked():
            staging = self.root / "staging"
            _assert_unredirected(staging)
            if not staging.exists():
                return ()
            _check_path(staging, directory=True)
            result = []
            with os.scandir(staging) as entries:
                for entry in entries:
                    if len(result) >= MAX_GENERATIONS or not _GENERATION.fullmatch(entry.name):
                        raise TrainerEnvironmentError("Interrupted staging contains an unexpected entry or exceeds its bound.")
                    _check_path(Path(entry.path), directory=True)
                    result.append(entry.name)
            return tuple(sorted(result))
