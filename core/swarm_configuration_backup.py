"""Opaque, selected-file backups for an enrolled SwarmUI installation.

The active Settings.fds and Backends.fds locations may be overridden by
SwarmUI command-line flags. Callers must choose both exact files; this module
never infers them from default paths, parses their contents, or restores them.
Archives may contain credentials and remain beside enrollment; recovery copies
use an explicitly reviewed new local folder. An explicit recovery copy is a
new offline directory of those opaque bytes;
it never replaces active settings, changes enrollment, or launches SwarmUI.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Callable
from uuid import uuid4

from .external_write_transaction import file_fingerprint, write_verified_json


_SCHEMA = 1
_ROLES = ("settings", "backends")
_NAMES = {"settings": "settings.fds", "backends": "backends.fds"}
_CHUNK = 1024 * 1024
_MAX_FILE = 16 * 1024 * 1024
_MAX_TOTAL = 32 * 1024 * 1024
_MAX_ARCHIVES = 256
_MAX_LIST = 512
_RECOVERY_SCHEMA = 1
_RECOVERY_INTENT = "recovery-intent.json"
_RECOVERY_MANIFEST = "recovery-manifest.json"
_COMPANION_RECEIPTS = "companion-receipts.json"
_MAX_COMPANION_RECEIPTS = 16
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_BINDING_KEYS = (
    "endpoint", "server_id", "install_root", "model_root",
    "ownership", "adopted_at", "source_revision",
)


class BackupError(RuntimeError):
    """A selected backup is unsafe, stale, incomplete, or unavailable."""


@dataclass(frozen=True)
class BackupSource:
    role: str
    path: str
    size: int
    sha256: str
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class BackupPreview:
    files: tuple[BackupSource, BackupSource]
    total_bytes: int
    binding: dict[str, str]
    record_sha256: str
    preview_id: str
    fingerprint: str


@dataclass(frozen=True)
class BackupSummary:
    backup_id: str
    status: str
    created_at: str
    total_bytes: int
    files: tuple[BackupSource, ...]
    message: str


@dataclass(frozen=True)
class BackupFileReport:
    role: str
    path: str
    status: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupReport:
    backup_id: str
    status: str
    files: tuple[BackupFileReport, ...]
    message: str


@dataclass(frozen=True)
class RecoveryCopyFile:
    role: str
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RecoveryCopyPreview:
    backup_id: str
    destination: str
    total_bytes: int
    files: tuple[RecoveryCopyFile, RecoveryCopyFile]
    preview_id: str
    fingerprint: str
    manifest_sha256: str
    intent_sha256: str
    parent_stat: tuple[int, int, int, int]


@dataclass(frozen=True)
class RecoveryCopyFileReport:
    role: str
    path: str
    status: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RecoveryCopyReport:
    backup_id: str
    destination: str
    status: str
    files: tuple[RecoveryCopyFileReport, ...]
    message: str


Progress = Callable[[str, int, int], None]
Cancel = Callable[[], bool]


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _preview_payload(value: BackupPreview) -> dict:
    return {
        "files": [source.__dict__ for source in value.files],
        "total_bytes": value.total_bytes, "binding": value.binding,
        "record_sha256": value.record_sha256,
        "preview_id": value.preview_id,
    }


def _recovery_preview_payload(value: RecoveryCopyPreview) -> dict:
    return {
        **{key: data for key, data in value.__dict__.items() if key not in {"fingerprint", "files"}},
        "files": [item.__dict__ for item in value.files],
    }


def _parent_stat(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise BackupError("The recovery destination parent is unavailable.") from exc
    if not stat.S_ISDIR(info.st_mode) or _redirected(path):
        raise BackupError("Choose an existing, ordinary local destination parent.")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _under(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _destination_path(value: str | Path) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise BackupError("Choose an absolute local recovery destination.") from exc
    raw = str(path)
    if (
        not path.is_absolute() or len(raw) > 4096 or ".." in path.parts
        or raw.startswith(("\\\\", "//"))
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
        or not path.name or _redirected(path)
    ):
        raise BackupError("Choose a new absolute local recovery destination.")
    if os.name == "nt" and _drive_type(path.anchor) not in {2, 3, 6}:
        raise BackupError("Choose a recovery destination on a local writable drive.")
    # Windows rejects or aliases these spellings; never let a reviewed path
    # silently become a different filesystem name (including ADS and devices).
    for component in path.parts[1:]:
        if (
            component != component.rstrip(" .")
            or any(char in component for char in '<>:"|?*')
            or component.split(".", 1)[0].upper() in {
                "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
        ):
            raise BackupError("The recovery destination has an unsupported component.")
    return path


def _drive_type(anchor: str) -> int:
    """Check only the chosen Windows volume; never enumerate machine drives."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(anchor))
    except (AttributeError, OSError, ValueError) as exc:
        raise BackupError("Cannot establish whether the recovery destination is local.") from exc


def _check_cancel(should_cancel: Cancel | None) -> None:
    if should_cancel is not None:
        try:
            cancelled = should_cancel()
        except InterruptedError:
            raise
        except Exception as exc:
            raise BackupError("Backup cancellation state is unavailable.") from exc
        if cancelled:
            raise InterruptedError("Configuration operation cancelled; any created output remains incomplete for review.")


def _progress(callback: Progress | None, role: str, done: int, total: int) -> None:
    if callback is not None:
        try:
            callback(role, done, total)
        except InterruptedError:
            raise
        except Exception as exc:
            raise BackupError("Backup progress reporting failed.") from exc


def _redirected(path: Path) -> bool:
    """Reject reparse points in every component before opening a selected file."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)
            or getattr(part, "is_junction", lambda: False)()
        ):
            return True
    return False


def _source_path(value: str | Path) -> Path:
    path = Path(value)
    if (
        not path.is_absolute() or ".." in path.parts or len(str(path)) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in str(path))
    ):
        raise BackupError("Choose an absolute file for each configuration role.")
    if _redirected(path):
        raise BackupError("A selected configuration path is redirected; choose a regular local file.")
    return path


def _regular(path: Path):
    if _redirected(path):
        raise BackupError("A selected configuration path is redirected; choose a regular local file.")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise BackupError("A selected configuration file is missing.") from exc
    except OSError as exc:
        raise BackupError("A selected configuration file is unavailable.") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("A selected configuration file is not a single regular file.")
    if info.st_size > _MAX_FILE:
        raise BackupError("A selected configuration file exceeds the 16 MiB limit.")
    return info


def _same_stat(first, second) -> bool:
    return all(getattr(first, key) == getattr(second, key) for key in (
        "st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino", "st_nlink",
    ))


def _same_open_stat(first, second) -> bool:
    # On Windows, Python's fstat() may report ctime as mtime while stat()
    # reports the file creation time. Compare ctime only across path stat calls.
    return all(getattr(first, key) == getattr(second, key) for key in (
        "st_size", "st_mtime_ns", "st_dev", "st_ino", "st_nlink",
    ))


def _scan(
    role: str, path: Path, *, progress: Progress | None = None,
    should_cancel: Cancel | None = None,
) -> BackupSource:
    before = _regular(path)
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_open_stat(before, opened):
                raise BackupError("A selected configuration file changed while opening; review again.")
            while True:
                _check_cancel(should_cancel)
                block = stream.read(_CHUNK)
                if not block:
                    break
                count += len(block)
                if count > _MAX_FILE:
                    raise BackupError("A selected configuration file grew beyond the 16 MiB limit.")
                digest.update(block)
                _progress(progress, role, count, before.st_size)
            if count != before.st_size or not _same_open_stat(before, os.fstat(stream.fileno())):
                raise BackupError("A selected configuration file changed during reading; review again.")
    except BackupError:
        raise
    except InterruptedError:
        raise
    except OSError as exc:
        raise BackupError("A selected configuration file could not be read.") from exc
    if not _same_stat(before, _regular(path)):
        raise BackupError("A selected configuration file changed during reading; review again.")
    return BackupSource(
        role, str(path), count, digest.hexdigest(), before.st_mtime_ns,
        before.st_ctime_ns, before.st_dev, before.st_ino,
    )


def _id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise BackupError("The backup identifier is invalid.")
    return value


class SwarmConfigurationBackup:
    """Back up two explicitly chosen opaque SwarmUI configuration files."""

    def __init__(self, manager):
        self.manager = manager
        record_path = Path(manager.record_path)
        self.backup_dir = record_path.with_name(f".{record_path.stem}.configuration-backups")
        self._issued: dict[str, str] = {}

    def _record(self):
        record, issue = self.manager._load()
        if issue:
            raise BackupError("The saved SwarmUI enrollment cannot be read; review it first.")
        if not record or not record.get("endpoint") or not record.get("install_root") or not record.get("adopted_at"):
            raise BackupError("Enroll a SwarmUI installation before backing up its selected configuration.")
        fingerprint = file_fingerprint(self.manager.record_path)
        if not fingerprint.get("exists"):
            raise BackupError("The SwarmUI enrollment record is unavailable.")
        binding = {key: str(record.get(key) or "") for key in _BINDING_KEYS}
        if any(len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value)
               for value in binding.values()):
            raise BackupError("The SwarmUI enrollment contains unsupported identity values.")
        return binding, str(fingerprint["sha256"])

    def _root(self, *, create: bool = False) -> Path:
        path = self.backup_dir
        if _redirected(path) or (path.exists() and not path.is_dir()):
            raise BackupError("The private configuration-backup location is unsafe.")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def _exclusive(self):
        """Bound concurrent creators; OS releases this lock after a crash."""
        lock = self._root(create=True) / "operation.lock"
        if _redirected(lock) or (lock.exists() and lock.stat().st_nlink != 1):
            raise BackupError("The private configuration-backup lock is unsafe.")
        try:
            with lock.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        raise BackupError("Another configuration backup is active; retry after it finishes.") from exc
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        raise BackupError("Another configuration backup is active; retry after it finishes.") from exc
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except InterruptedError:
            raise
        except OSError as exc:
            raise BackupError("Cannot lock the private configuration-backup archive.") from exc

    def _entries(self):
        root = self._root()
        if not root.exists():
            return ()
        entries = []
        with os.scandir(root) as iterator:
            for item in iterator:
                if item.name == "operation.lock":
                    continue
                entries.append(item)
                if len(entries) > _MAX_LIST:
                    raise BackupError("Too many backup archive entries; inspect the private backup folder.")
        return tuple(entries)

    @staticmethod
    def _parse_companion_receipt(value):
        # Lazy import avoids making ordinary configuration backups depend on
        # companion qualification. This is the canonical private record schema,
        # never an import of a detached package's metadata.
        from .swarm_companion_installation import FILES, INTENT, MANIFEST, PackageReceipt
        if (type(value) is not dict
                or set(value) != {"package_id", "directory", "directory_identity", "files"}):
            raise BackupError("The independent companion receipt is invalid.")
        _id(value["package_id"])
        directory = value["directory"]
        if (type(directory) is not str or not 0 < len(directory) <= 4096
                or not Path(directory).is_absolute() or ".." in Path(directory).parts
                or any(ord(c) < 32 for c in directory)):
            raise BackupError("The independent companion receipt directory is invalid.")
        identity = value["directory_identity"]
        if type(identity) is not list or len(identity) != 2 or any(type(v) is not int or v < 0 for v in identity):
            raise BackupError("The independent companion receipt directory identity is invalid.")
        files = value["files"]
        if type(files) is not list or len(files) != 6:
            raise BackupError("The independent companion receipt file count is invalid.")
        parsed = []
        for name, row in zip((*FILES, INTENT, MANIFEST), files):
            if (type(row) is not dict or set(row) != set(BackupSource.__dataclass_fields__)
                    or row["role"] != name or row["path"] != str(Path(directory) / name)
                    or not _hex_digest(row["sha256"])
                    or any(type(row[k]) is not int or row[k] < 0 for k in
                           ("size", "mtime_ns", "ctime_ns", "device", "inode"))
                    or not 0 < row["size"] <= (_MAX_FILE if name in FILES else 32 * 1024)):
                raise BackupError("The independent companion receipt file identity is invalid.")
            parsed.append(BackupSource(**row))
        if sum(row.size for row in parsed[:4]) > _MAX_TOTAL:
            raise BackupError("The independent companion receipt exceeds its byte budget.")
        return PackageReceipt(value["package_id"], directory, tuple(identity), tuple(parsed))

    def _companion_receipts(self):
        path = self._root() / _COMPANION_RECEIPTS
        if not os.path.lexists(path):
            return {"schema": 1, "kind": "collectigen-companion-ownership", "receipts": []}
        try:
            value = _read_recovery_json(path)
        except (FileNotFoundError, RecursionError) as exc:
            raise BackupError("The private companion ownership record is unavailable; retain all packages.") from exc
        if (type(value) is not dict or set(value) != {"schema", "kind", "receipts"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or value["kind"] != "collectigen-companion-ownership"
                or type(value["receipts"]) is not list or len(value["receipts"]) > _MAX_COMPANION_RECEIPTS):
            raise BackupError("The private companion ownership record is invalid; retain all packages.")
        receipts = [self._parse_companion_receipt(row) for row in value["receipts"]]
        if len({row.package_id for row in receipts}) != len(receipts):
            raise BackupError("The private companion ownership record contains duplicate identities.")
        return value

    def check_companion_receipt_capacity(self):
        """Read only; never creates a record during package preview."""
        value = self._companion_receipts()
        if len(value["receipts"]) >= _MAX_COMPANION_RECEIPTS:
            raise BackupError("The private companion receipt limit was reached; preserve existing records.")

    def companion_receipt(self, package_id):
        _id(package_id)
        for row in self._companion_receipts()["receipts"]:
            if row["package_id"] == package_id:
                return self._parse_companion_receipt(row)
        return None

    def record_companion_receipt(self, receipt):
        """Retain a freshly issued typed receipt under the existing archive lock.

        No path argument or package JSON can supply ownership. Full receipt bytes
        are independently retained here after the detached creation readback.
        A missing/corrupt record cannot be reconstructed by reopening a package.
        """
        from .swarm_companion_installation import PackageReceipt, _manifest, _rows
        if type(receipt) is not PackageReceipt:
            raise BackupError("Only a freshly issued typed companion receipt can be recorded.")
        payload = json.loads(json.dumps(asdict(receipt)))
        if self._parse_companion_receipt(payload) != receipt:
            raise BackupError("The issued companion receipt changed.")
        folder = _destination_path(receipt.directory)
        with self._exclusive():
            root = self._root()
            identity = _directory_identity(root)
            path = root / _COMPANION_RECEIPTS
            previous = self._companion_receipts()
            if len(previous["receipts"]) >= _MAX_COMPANION_RECEIPTS:
                raise BackupError("The private companion receipt limit was reached.")
            if any(row["package_id"] == receipt.package_id for row in previous["receipts"]):
                raise BackupError("A companion receipt already exists; it cannot be overwritten.")
            before = _scan("receipts", path) if os.path.lexists(path) else None
            _check_directory_identity(folder, receipt.directory_identity)
            for source in receipt.files:
                if _scan(source.role, folder / source.role) != source:
                    raise BackupError("Package changed before independent ownership was saved.")
            manifest = _manifest(folder)
            if manifest["package_id"] != receipt.package_id or manifest["files"] != _rows(receipt.files[:4]):
                raise BackupError("Package manifest does not match the issued receipt.")
            value = {**previous, "receipts": [*previous["receipts"], payload]}
            if len(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")) + 1 > 32 * 1024:
                raise BackupError("The private companion receipt byte limit was reached; preserve the package.")

            def guard():
                _check_directory_identity(root, identity)
                if (before is None and os.path.lexists(path)) or (
                        before is not None and _scan("receipts", path) != before):
                    raise BackupError("The private companion ownership record changed during publication.")
                _check_directory_identity(folder, receipt.directory_identity)
                for source in receipt.files:
                    current = _regular(Path(source.path))
                    if (current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_dev, current.st_ino) != (
                            source.size, source.mtime_ns, source.ctime_ns, source.device, source.inode):
                        raise BackupError("Package changed during independent receipt publication.")

            if before is None:
                _publish_recovery_json(root, _COMPANION_RECEIPTS, value, guard=guard)
            else:
                # Existing application-owned metadata only; preserve its full
                # prior content until the verified replacement is ready.
                staged_name = f".companion-receipts.{uuid4().hex}.tmp"
                _publish_recovery_json(root, staged_name, value, guard=guard)
                guard()
                os.replace(root / staged_name, path)
            if self._companion_receipts() != value:
                raise BackupError("The independent companion ownership record failed readback.")

    def _capacity(self, total: int) -> None:
        try:
            free = shutil.disk_usage(self.backup_dir.parent).free
        except OSError as exc:
            raise BackupError("Cannot measure free space for the private backup folder.") from exc
        if free < total + 2 * _CHUNK:
            raise BackupError("Insufficient free space for the selected configuration backup.")

    def preview(
        self, settings_path: str | Path, backends_path: str | Path, *,
        progress: Progress | None = None, should_cancel: Cancel | None = None,
    ) -> BackupPreview:
        binding, record_sha256 = self._record()
        paths = (_source_path(settings_path), _source_path(backends_path))
        if os.path.normcase(str(paths[0])) == os.path.normcase(str(paths[1])):
            raise BackupError("Choose distinct settings and backends files.")
        _check_cancel(should_cancel)
        files = tuple(_scan(role, path, progress=progress, should_cancel=should_cancel) for role, path in zip(_ROLES, paths))
        total = sum(item.size for item in files)
        if total > _MAX_TOTAL:
            raise BackupError("The selected configuration files exceed the 32 MiB total limit.")
        self._capacity(total)
        if self._record() != (binding, record_sha256):
            raise BackupError("The SwarmUI enrollment changed while preparing the backup; review again.")
        value = BackupPreview(files, total, binding, record_sha256, uuid4().hex, "")
        value = BackupPreview(**{**value.__dict__, "fingerprint": _hash(_preview_payload(value))})
        self._issued[value.preview_id] = value.fingerprint
        return value

    def _copy(
        self, source: BackupSource, target: Path, *,
        progress: Progress | None, should_cancel: Cancel | None,
        guard: Callable[[], None] | None = None,
    ) -> None:
        path = _source_path(source.path)
        before = _regular(path)
        expected = (source.size, source.mtime_ns, source.ctime_ns, source.device, source.inode)
        actual = (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_dev, before.st_ino)
        if actual != expected:
            raise BackupError("A selected configuration file changed; review the backup again.")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        count = 0
        try:
            if guard is not None:
                guard()
            with path.open("rb") as input_stream, temporary.open("xb") as output_stream:
                if not _same_open_stat(before, os.fstat(input_stream.fileno())):
                    raise BackupError("A selected configuration file changed while opening.")
                while True:
                    _check_cancel(should_cancel)
                    if guard is not None:
                        guard()
                    block = input_stream.read(_CHUNK)
                    if not block:
                        break
                    count += len(block)
                    if count > _MAX_FILE:
                        raise BackupError("A selected configuration file grew beyond the 16 MiB limit.")
                    output_stream.write(block)
                    digest.update(block)
                    _progress(progress, source.role, count, source.size)
                output_stream.flush()
                os.fsync(output_stream.fileno())
                if not _same_open_stat(before, os.fstat(input_stream.fileno())):
                    raise BackupError("A selected configuration file changed during copying.")
            if count != source.size or digest.hexdigest() != source.sha256:
                raise BackupError("A selected configuration file changed since preview; archive is incomplete.")
            # Verify staged bytes directly; the temporary name intentionally is
            # not .fds and never participates in source-selection rules.
            if _file_digest(temporary, should_cancel=should_cancel) != (count, source.sha256):
                raise BackupError("The staged configuration copy failed verification.")
            if guard is not None:
                guard()
            os.link(temporary, target)
            temporary.unlink()
            if _file_digest(target, should_cancel=should_cancel) != (count, source.sha256):
                raise BackupError("The archived configuration copy failed verification.")
        except BackupError:
            raise
        except InterruptedError:
            raise
        except OSError as exc:
            raise BackupError("The selected configuration copy failed; its archive remains incomplete.") from exc

    def create(
        self, preview: BackupPreview, *, progress: Progress | None = None,
        should_cancel: Cancel | None = None,
    ) -> BackupSummary:
        _check_cancel(should_cancel)
        with self._exclusive():
            return self._create_locked(preview, progress=progress, should_cancel=should_cancel)

    def _create_locked(
        self, preview: BackupPreview, *, progress: Progress | None,
        should_cancel: Cancel | None,
    ) -> BackupSummary:
        if (
            not isinstance(preview, BackupPreview)
            or self._issued.pop(preview.preview_id, None) != preview.fingerprint
            or _hash(_preview_payload(preview)) != preview.fingerprint
            or tuple(item.role for item in preview.files) != _ROLES
            or sum(item.size for item in preview.files) != preview.total_bytes
        ):
            raise BackupError("The selected backup preview changed or expired; review again.")
        _check_cancel(should_cancel)
        if self._record() != (preview.binding, preview.record_sha256):
            raise BackupError("The SwarmUI enrollment changed; review the backup again.")
        for item in preview.files:
            if _scan(item.role, _source_path(item.path), should_cancel=should_cancel) != item:
                raise BackupError("A selected configuration file changed; review the backup again.")
        self._capacity(preview.total_bytes)
        if len(self._entries()) >= _MAX_ARCHIVES:
            raise BackupError("The private configuration-backup archive reached its 256-entry limit.")
        root = self._root(create=True)
        backup_id = uuid4().hex
        folder = root / backup_id
        try:
            folder.mkdir(exist_ok=False)
        except OSError as exc:
            raise BackupError("Cannot reserve a new private configuration-backup folder.") from exc
        intent = {
            "schema_version": _SCHEMA, "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "binding": preview.binding, "record_sha256": preview.record_sha256,
            "files": [item.__dict__ for item in preview.files],
            "total_bytes": preview.total_bytes,
        }
        try:
            staged_intent = folder / f".intent.{uuid4().hex}.tmp"
            write_verified_json(staged_intent, intent, label="Configuration backup intent")
            os.link(staged_intent, folder / "intent.json")
            staged_intent.unlink()
            if json.loads((folder / "intent.json").read_text(encoding="utf-8")) != intent:
                raise BackupError("The configuration backup intent failed verification.")
            for item in preview.files:
                self._copy(item, folder / _NAMES[item.role], progress=progress, should_cancel=should_cancel)
            for item in preview.files:
                if _scan(item.role, _source_path(item.path), should_cancel=should_cancel) != item:
                    raise BackupError("A selected configuration file changed during backup; archive remains incomplete.")
            if self._record() != (preview.binding, preview.record_sha256):
                raise BackupError("The SwarmUI enrollment changed during backup; archive remains incomplete.")
            manifest = {
                **intent, "completed_at": datetime.now(timezone.utc).isoformat(),
                "archive_names": dict(_NAMES),
                "intent_sha256": file_fingerprint(folder / "intent.json")["sha256"],
            }
            staged = folder / f".manifest.{uuid4().hex}.tmp"
            write_verified_json(staged, manifest, label="Configuration backup manifest")
            for item in preview.files:
                if _scan(item.role, _source_path(item.path), should_cancel=should_cancel) != item:
                    raise BackupError("A selected configuration file changed before finalizing; archive remains incomplete.")
            if self._record() != (preview.binding, preview.record_sha256):
                raise BackupError("The SwarmUI enrollment changed before finalizing; archive remains incomplete.")
            _check_cancel(should_cancel)
            os.link(staged, folder / "manifest.json")
            staged.unlink()
            if json.loads((folder / "manifest.json").read_text(encoding="utf-8")) != manifest:
                raise BackupError("The configuration backup manifest failed verification.")
        except BackupError:
            raise
        except InterruptedError:
            raise
        except Exception as exc:
            raise BackupError("The selected configuration backup is incomplete; review its private archive.") from exc
        return self._summary(backup_id)

    def _folder(self, backup_id: str) -> Path:
        folder = self._root() / _id(backup_id)
        if _redirected(folder) or (folder.exists() and not folder.is_dir()):
            raise BackupError("The selected backup archive path is unsafe.")
        return folder

    def safe_backup_path(self, backup_id: str) -> Path:
        """Return only a strictly identified local folder; caller verifies first."""
        folder = self._folder(backup_id)
        if not folder.is_dir():
            raise BackupError("The selected backup folder is missing.")
        return folder

    @staticmethod
    def _parse_manifest(folder: Path, *, marker_reader=None) -> dict | None:
        path = folder / "manifest.json"
        if _redirected(path):
            raise BackupError("The backup manifest path is redirected.")
        if not path.exists():
            return None
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32 * 1024:
                raise BackupError("The backup manifest is unsafe or oversized.")
            manifest = json.loads(path.read_text(encoding="utf-8") if marker_reader is None
                                  else marker_reader("manifest.json").decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise BackupError("The backup manifest cannot be read.") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {
                "schema_version", "backup_id", "created_at", "binding",
                "record_sha256", "files", "total_bytes", "completed_at",
                "archive_names", "intent_sha256",
            }
            or manifest.get("schema_version") != _SCHEMA
            or manifest.get("backup_id") != folder.name
        ):
            raise BackupError("The backup manifest has an unsupported identity or format.")
        binding = manifest.get("binding")
        if (
            manifest.get("archive_names") != _NAMES
            or not isinstance(binding, dict) or set(binding) != set(_BINDING_KEYS)
            or any(not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value)
                   for value in binding.values())
            or any(not isinstance(manifest.get(key), str) or len(manifest[key]) > 80
                   for key in ("created_at", "completed_at"))
            or not _hex_digest(manifest.get("record_sha256"))
            or not _hex_digest(manifest.get("intent_sha256"))
            or type(manifest.get("total_bytes")) is not int
            or not 0 <= manifest["total_bytes"] <= _MAX_TOTAL
        ):
            raise BackupError("The backup manifest has unsafe file roles or binding.")
        try:
            datetime.fromisoformat(manifest["created_at"])
            datetime.fromisoformat(manifest["completed_at"])
        except ValueError as exc:
            raise BackupError("The backup manifest has invalid dates.") from exc
        intent_path = folder / "intent.json"
        if _redirected(intent_path):
            raise BackupError("The backup intent path is redirected.")
        try:
            intent_info = intent_path.stat()
            if not stat.S_ISREG(intent_info.st_mode) or intent_info.st_nlink != 1 or intent_info.st_size > 32 * 1024:
                raise BackupError("The backup intent is unsafe or oversized.")
            raw_intent = intent_path.read_bytes() if marker_reader is None else marker_reader("intent.json")
            if hashlib.sha256(raw_intent).hexdigest() != manifest["intent_sha256"]:
                raise BackupError("The backup intent changed.")
            intent = json.loads(raw_intent.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise BackupError("The backup intent cannot be read.") from exc
        intent_keys = {
            "schema_version", "backup_id", "created_at", "binding",
            "record_sha256", "files", "total_bytes",
        }
        if (
            not isinstance(intent, dict) or set(intent) != intent_keys
            or {key: manifest[key] for key in intent_keys} != intent
        ):
            raise BackupError("The backup manifest differs from its original intent.")
        sources = _sources_from_manifest(manifest)
        if sum(item.size for item in sources) != manifest.get("total_bytes"):
            raise BackupError("The backup manifest size accounting is invalid.")
        return manifest

    def _summary(self, backup_id: str) -> BackupSummary:
        folder = self._folder(backup_id)
        if not folder.exists():
            return BackupSummary(backup_id, "invalid", "", 0, (), "Backup archive is missing.")
        try:
            manifest = self._parse_manifest(folder)
        except BackupError:
            return BackupSummary(backup_id, "invalid", "", 0, (), "Backup manifest is invalid; archived bytes are untrusted.")
        if manifest is None:
            return BackupSummary(backup_id, "incomplete", "", 0, (), "Backup has no final manifest; it was interrupted or failed.")
        files = _sources_from_manifest(manifest)
        return BackupSummary(
            backup_id, "complete", manifest["completed_at"], manifest["total_bytes"],
            files, "Manifest complete; verify archived bytes before use.",
        )

    def list_backups(self) -> tuple[BackupSummary, ...]:
        summaries = []
        for item in self._entries():
            if item.name == _COMPANION_RECEIPTS:
                try:
                    self._companion_receipts()
                except (BackupError, OSError, RecursionError):
                    summaries.append(BackupSummary("", "invalid", "", 0, (), "Companion ownership metadata is invalid; retain its packages."))
                continue
            if item.is_dir(follow_symlinks=False) and len(item.name) == 32 and all(c in "0123456789abcdef" for c in item.name):
                summaries.append(self._summary(item.name))
            else:
                summaries.append(BackupSummary("", "invalid", "", 0, (), "Unrecognized backup archive entry."))
        return tuple(sorted(summaries, key=lambda row: row.backup_id, reverse=True))

    def verify(
        self, backup_id: str, *, progress: Progress | None = None,
        should_cancel: Cancel | None = None,
    ) -> BackupReport:
        folder = self._folder(backup_id)
        summary = self._summary(backup_id)
        if summary.status != "complete":
            return BackupReport(backup_id, summary.status, (), summary.message)
        reports = []
        for item in summary.files:
            _check_cancel(should_cancel)
            target = folder / _NAMES[item.role]
            try:
                size, digest = _file_digest(target, progress=progress, role=item.role, should_cancel=should_cancel)
            except BackupError:
                status = "unavailable" if _redirected(target) or target.exists() else "missing"
                reports.append(BackupFileReport(item.role, item.path, status, item.size, item.sha256))
            else:
                reports.append(BackupFileReport(
                    item.role, item.path,
                    "verified" if (size, digest) == (item.size, item.sha256) else "corrupt",
                    item.size, item.sha256,
                ))
        status = "verified" if all(item.status == "verified" for item in reports) else "invalid"
        return BackupReport(backup_id, status, tuple(reports),
                            "Archived bytes match the selected originals at backup time." if status == "verified"
                            else "One or more archived files could not be verified.")

    def compare(
        self, backup_id: str, *, progress: Progress | None = None,
        should_cancel: Cancel | None = None,
    ) -> BackupReport:
        verified = self.verify(backup_id, progress=progress, should_cancel=should_cancel)
        if verified.status != "verified":
            return verified
        manifest = self._parse_manifest(self._folder(backup_id))
        initial_record = self._record()
        binding, _record_sha = initial_record
        if binding != manifest["binding"]:
            raise BackupError("The enrolled SwarmUI installation differs from this backup; comparison is blocked.")
        reports = []
        for item in _sources_from_manifest(manifest):
            _check_cancel(should_cancel)
            try:
                source = _scan(item.role, _source_path(item.path), progress=progress, should_cancel=should_cancel)
            except BackupError as exc:
                status = "missing" if "missing" in str(exc) else "unavailable"
            else:
                status = "unchanged" if (source.size, source.sha256) == (item.size, item.sha256) else "changed"
            reports.append(BackupFileReport(item.role, item.path, status, item.size, item.sha256))
        if self._record() != initial_record:
            raise BackupError("The SwarmUI enrollment changed during comparison; review again.")
        status = (
            "unavailable" if any(item.status == "unavailable" for item in reports)
            else "changed" if any(item.status != "unchanged" for item in reports)
            else "unchanged"
        )
        return BackupReport(backup_id, status, tuple(reports),
                            "Comparison covers only the two selected source files.")

    def review_recovery_scope(self, declaration, *, backup_id=None, companion_ids=(),
                              progress=None, should_cancel=None):
        """Read selected canonical evidence only; never inspect original targets.

        No selections means no filesystem or enrollment I/O. Caller privacy and
        ownership labels remain claims; this report cannot authorize restoration.
        Observations are temporary and must not be used as transaction tokens.
        """
        from .swarm_recovery_scope import (
            CoverageObservation, FileObservation, RecoveryScopeReview,
            validate_scope, validate_selections,
        )

        scope = validate_scope(declaration)
        selected_receipts = validate_selections(scope, backup_id, companion_ids)
        _check_cancel(should_cancel)
        if backup_id is None and not selected_receipts:
            return RecoveryScopeReview(scope)
        observations = []
        guards = []
        frozen_markers = {}

        def metadata(name, path, *, freeze=False):
            before = _regular(path)
            if before.st_size > 32 * 1024:
                raise BackupError("Selected metadata exceeds its bound.")
            _check_cancel(should_cancel)
            with path.open("rb") as stream:
                if not _same_open_stat(before, os.fstat(stream.fileno())):
                    raise BackupError("Selected metadata changed.")
                raw = stream.read(32 * 1024 + 1)
                if not _same_open_stat(before, os.fstat(stream.fileno())):
                    raise BackupError("Selected metadata changed.")
            if len(raw) != before.st_size or not _same_stat(before, _regular(path)):
                raise BackupError("Selected metadata changed.")
            if freeze:
                frozen_markers[name] = raw
            return BackupSource(name, str(path), len(raw), hashlib.sha256(raw).hexdigest(),
                                before.st_mtime_ns, before.st_ctime_ns, before.st_dev, before.st_ino)

        def marker_reader(name):
            if name not in {"intent.json", "manifest.json"} or name not in frozen_markers:
                raise BackupError("Only frozen canonical archive markers may be parsed.")
            return frozen_markers[name]

        def unchanged(facts, folder, identity):
            _check_directory_identity(folder, identity)
            for fact in facts:
                _check_cancel(should_cancel)
                current = (metadata(fact.role, Path(fact.path))
                           if fact.role in {"intent.json", "manifest.json", _COMPANION_RECEIPTS}
                           else _scan(fact.role, Path(fact.path), should_cancel=should_cancel))
                if current != fact:
                    raise BackupError("Selected evidence changed during review.")
            _check_directory_identity(folder, identity)

        if backup_id is not None:
            kind = "configuration-archive"
            try:
                folder = self._folder(backup_id)
                identity = _directory_identity(folder)
                markers = tuple(metadata(name, folder / name, freeze=True) for name in ("intent.json", "manifest.json"))
                manifest = self._parse_manifest(folder, marker_reader=marker_reader)
                if manifest is None:
                    raise BackupError("Selected archive integrity is unavailable.")
                sources = _sources_from_manifest(manifest)
                # These paths are fixed canonical archive names, never the
                # original locations stored in the manifest or declaration.
                payloads = tuple(_scan(row.role, folder / _NAMES[row.role], progress=progress, should_cancel=should_cancel)
                                 for row in sources)
                if any((fact.size, fact.sha256) != (row.size, row.sha256) for fact, row in zip(payloads, sources)):
                    raise BackupError("Selected archive bytes changed.")
                unchanged(markers, folder, identity)
                observations.append(CoverageObservation(kind, backup_id, "archive-integrity-verified",
                    tuple(FileObservation(row.role, row.size, row.sha256) for row in payloads),
                    tuple(row.sha256 for row in markers), datetime.now(timezone.utc).isoformat()))
                guards.append((len(observations) - 1, (*markers, *payloads), folder, identity))
            except InterruptedError:
                raise
            except (BackupError, OSError, ValueError, TypeError, KeyError, RecursionError):
                # Do not expose archive paths, original locations or exception
                # details that may contain supplied/private values.
                observations.append(CoverageObservation(kind, backup_id, "unavailable"))

        if selected_receipts:
            kind = "companion-receipt"
            try:
                folder = self._root()  # read-only; never creates archive storage
                identity = _directory_identity(folder)
                marker = metadata(_COMPANION_RECEIPTS, folder / _COMPANION_RECEIPTS)
                receipt_rows = []
                for package_id in selected_receipts:
                    _check_cancel(should_cancel)
                    receipt = self.companion_receipt(package_id)
                    if receipt is None:
                        receipt_rows.append(CoverageObservation(kind, package_id, "unavailable"))
                    else:
                        receipt_rows.append(CoverageObservation(kind, package_id, "detached-ownership-recorded",
                            tuple(FileObservation(row.role, row.size, row.sha256) for row in receipt.files[:4]),
                            (marker.sha256,), datetime.now(timezone.utc).isoformat()))
                unchanged((marker,), folder, identity)
                for row in receipt_rows:
                    observations.append(row)
                    guards.append((len(observations) - 1, (marker,), folder, identity))
            except InterruptedError:
                raise
            except (BackupError, OSError, ValueError, TypeError, KeyError, RecursionError):
                observations.extend(CoverageObservation(kind, package_id, "unavailable") for package_id in selected_receipts)

        # The archive could have changed while selected receipt metadata was
        # reviewed. Recheck the exact bounded evidence before returning it.
        for index, facts, folder, identity in guards:
            try:
                unchanged(facts, folder, identity)
            except InterruptedError:
                raise
            except (BackupError, OSError, ValueError):
                row = observations[index]
                observations[index] = CoverageObservation(row.kind, row.reference, "unavailable")
        _check_cancel(should_cancel)
        return RecoveryScopeReview(scope, tuple(observations))

    def _recovery_archive(self, backup_id: str, *, progress=None, should_cancel=None):
        _id(backup_id)
        checked = self.verify(backup_id, progress=progress, should_cancel=should_cancel)
        if checked.status != "verified":
            raise BackupError("Verify the selected backup archive before making a recovery copy.")
        folder = self._folder(backup_id)
        manifest = self._parse_manifest(folder)
        if manifest is None:
            raise BackupError("The selected backup archive is incomplete.")
        manifest_sha256 = _file_digest(folder / "manifest.json", should_cancel=should_cancel)[1]
        return folder, manifest, manifest_sha256

    def _recovery_destination(self, value: str | Path, manifest: dict, *, absent: bool):
        destination = _destination_path(value)
        parent = destination.parent
        parent_stat = _parent_stat(parent)
        protected = [self.backup_dir, *(Path(item.path) for item in _sources_from_manifest(manifest))]
        for key in ("install_root", "model_root"):
            root = Path(manifest["binding"][key])
            if root.is_absolute():
                protected.append(root)
        if any(_under(destination, root) for root in protected):
            raise BackupError("Choose a recovery destination outside the archive, installation, and selected source folders.")
        if absent and os.path.lexists(destination):
            raise BackupError("The recovery destination already exists; choose a new directory.")
        return destination, parent_stat

    @staticmethod
    def _recovery_files(destination: Path, manifest: dict) -> tuple[RecoveryCopyFile, RecoveryCopyFile]:
        return tuple(RecoveryCopyFile(item.role, str(destination / _NAMES[item.role]), item.size, item.sha256)
                     for item in _sources_from_manifest(manifest))

    @staticmethod
    def _recovery_capacity(parent: Path, total: int) -> None:
        try:
            free = shutil.disk_usage(parent).free
        except OSError as exc:
            raise BackupError("Cannot measure recovery destination free space.") from exc
        if free < total + 2 * _CHUNK:
            raise BackupError("Insufficient free space for the recovery copy.")

    def preview_materialization(
        self, backup_id: str, destination: str | Path, *,
        progress: Progress | None = None, should_cancel: Cancel | None = None,
    ) -> RecoveryCopyPreview:
        """Review two new opaque copies; neither runtime nor source files are touched."""
        _check_cancel(should_cancel)
        _folder, manifest, manifest_sha256 = self._recovery_archive(
            backup_id, progress=progress, should_cancel=should_cancel)
        target, parent_stat = self._recovery_destination(destination, manifest, absent=True)
        self._recovery_capacity(target.parent, manifest["total_bytes"])
        _check_cancel(should_cancel)
        value = RecoveryCopyPreview(
            backup_id, str(target), manifest["total_bytes"],
            self._recovery_files(target, manifest), uuid4().hex, "",
            manifest_sha256, manifest["intent_sha256"], parent_stat,
        )
        value = RecoveryCopyPreview(**{**value.__dict__, "fingerprint": _hash(_recovery_preview_payload(value))})
        self._issued[value.preview_id] = value.fingerprint
        return value

    def materialize(
        self, preview: RecoveryCopyPreview, *, progress: Progress | None = None,
        should_cancel: Cancel | None = None,
    ) -> RecoveryCopyReport:
        """Publish a new directory and final manifest without replacing originals."""
        _check_cancel(should_cancel)
        with self._exclusive():
            return self._materialize_locked(preview, progress=progress, should_cancel=should_cancel)

    def _materialize_locked(self, preview: RecoveryCopyPreview, *, progress, should_cancel):
        try:
            issued = isinstance(preview, RecoveryCopyPreview) and self._issued.pop(preview.preview_id, None)
            valid = issued == preview.fingerprint and _hash(_recovery_preview_payload(preview)) == preview.fingerprint
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise BackupError("The recovery-copy preview changed or expired; review again.")
        folder, manifest, manifest_sha256 = self._recovery_archive(
            preview.backup_id, progress=progress, should_cancel=should_cancel)
        destination, parent_stat = self._recovery_destination(preview.destination, manifest, absent=True)
        if (
            preview.manifest_sha256 != manifest_sha256
            or preview.intent_sha256 != manifest["intent_sha256"]
            or preview.total_bytes != manifest["total_bytes"]
            or preview.files != self._recovery_files(destination, manifest)
            or parent_stat != preview.parent_stat
        ):
            raise BackupError("The archive or recovery destination changed; review again.")
        self._recovery_capacity(destination.parent, preview.total_bytes)
        _check_cancel(should_cancel)
        if _parent_stat(destination.parent) != parent_stat:
            raise BackupError("The recovery destination parent changed; review again.")
        try:
            destination.mkdir(exist_ok=False)
        except OSError as exc:
            raise BackupError("Cannot reserve the new recovery destination; no existing files were replaced.") from exc
        _check_parent_identity(destination.parent, parent_stat)
        identity = _directory_identity(destination)
        def guard():
            _check_parent_identity(destination.parent, parent_stat)
            _check_directory_identity(destination, identity)
        intent = {
            "schema_version": _RECOVERY_SCHEMA,
            "backup_id": preview.backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest_sha256": manifest_sha256,
            "total_bytes": preview.total_bytes,
            "files": [_recovery_file_manifest(item) for item in preview.files],
        }
        try:
            _publish_recovery_json(destination, _RECOVERY_INTENT, intent, guard=guard,
                                   should_cancel=should_cancel)
            for item in preview.files:
                guard()
                archive = folder / _NAMES[item.role]
                info = _regular(archive)
                source = BackupSource(item.role, str(archive), item.size, item.sha256,
                                      info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)
                self._copy(source, Path(item.path), progress=progress, should_cancel=should_cancel, guard=guard)
                guard()
            for item in preview.files:
                if _file_digest(Path(item.path), should_cancel=should_cancel) != (item.size, item.sha256):
                    raise BackupError("A recovery copy failed readback; the destination remains incomplete.")
            guard()
            refreshed_folder, refreshed_manifest, refreshed_hash = self._recovery_archive(
                preview.backup_id, should_cancel=should_cancel)
            if refreshed_folder != folder or refreshed_manifest != manifest or refreshed_hash != manifest_sha256:
                raise BackupError("The backup archive changed during copying; the destination remains incomplete.")
            completion = {
                **intent, "completed_at": datetime.now(timezone.utc).isoformat(),
                "intent_sha256": _file_digest(destination / _RECOVERY_INTENT, should_cancel=should_cancel)[1],
            }
            _check_cancel(should_cancel)
            guard()
            _publish_recovery_json(destination, _RECOVERY_MANIFEST, completion, guard=guard,
                                   should_cancel=should_cancel)
        except (BackupError, InterruptedError):
            raise
        except Exception as exc:
            raise BackupError("The recovery copy is incomplete; review its new destination.") from exc
        result = self.verify_materialization(preview.backup_id, destination)
        if result.status != "verified":
            raise BackupError("The recovery copy could not be verified; its destination remains for review.")
        return result

    def verify_materialization(
        self, backup_id: str, destination: str | Path, *,
        progress: Progress | None = None, should_cancel: Cancel | None = None,
    ) -> RecoveryCopyReport:
        """Read the new copy offline; incomplete or changed copies never pass."""
        _id(backup_id)
        folder, manifest, manifest_sha256 = self._recovery_archive(
            backup_id, should_cancel=should_cancel)
        target, parent_stat = self._recovery_destination(destination, manifest, absent=False)
        expected = self._recovery_files(target, manifest)
        if not os.path.lexists(target):
            return RecoveryCopyReport(backup_id, str(target), "missing", (), "Recovery destination is missing.")
        identity = _directory_identity(target)
        try:
            intent = _read_recovery_json(target / _RECOVERY_INTENT)
            completion = _read_recovery_json(target / _RECOVERY_MANIFEST)
        except FileNotFoundError:
            return RecoveryCopyReport(backup_id, str(target), "incomplete", (),
                                      "Recovery copy has no complete intent and final manifest.")
        except BackupError:
            return RecoveryCopyReport(backup_id, str(target), "invalid", (),
                                      "Recovery copy markers are unsafe or changed.")
        try:
            intent_sha256 = _file_digest(target / _RECOVERY_INTENT, should_cancel=should_cancel)[1]
        except BackupError:
            return RecoveryCopyReport(backup_id, str(target), "invalid", (),
                                      "Recovery copy intent changed or is unavailable.")
        expected_files = [_recovery_file_manifest(item) for item in expected]
        intent_keys = {"schema_version", "backup_id", "created_at", "manifest_sha256", "total_bytes", "files"}
        if (
            not isinstance(intent, dict) or set(intent) != intent_keys
            or intent.get("schema_version") != _RECOVERY_SCHEMA
            or intent.get("backup_id") != backup_id
            or intent.get("manifest_sha256") != manifest_sha256
            or intent.get("total_bytes") != manifest["total_bytes"]
            or intent.get("files") != expected_files
            or not isinstance(completion, dict)
            or set(completion) != intent_keys | {"completed_at", "intent_sha256"}
            or {key: completion[key] for key in intent_keys} != intent
            or completion.get("intent_sha256") != intent_sha256
            or not _valid_date(intent.get("created_at"))
            or not _valid_date(completion.get("completed_at"))
        ):
            return RecoveryCopyReport(backup_id, str(target), "invalid", (),
                                      "Recovery copy manifest does not match the verified archive and original intent.")
        reports = []
        for item in expected:
            _check_cancel(should_cancel)
            path = Path(item.path)
            try:
                result = _file_digest(path, progress=progress, role=item.role, should_cancel=should_cancel)
            except BackupError:
                status = "missing" if not os.path.lexists(path) else "unavailable"
            else:
                status = "verified" if result == (item.size, item.sha256) else "changed"
            reports.append(RecoveryCopyFileReport(item.role, item.path, status, item.size, item.sha256))
        try:
            _check_parent_identity(target.parent, parent_stat)
            _check_directory_identity(target, identity)
            refreshed_folder, refreshed_manifest, refreshed_hash = self._recovery_archive(
                backup_id, should_cancel=should_cancel)
            if refreshed_folder != folder or refreshed_manifest != manifest or refreshed_hash != manifest_sha256:
                raise BackupError("The backup archive changed during recovery-copy verification.")
        except BackupError:
            return RecoveryCopyReport(backup_id, str(target), "invalid", tuple(reports),
                                      "The archive or recovery destination changed during verification.")
        status = "verified" if all(item.status == "verified" for item in reports) else "invalid"
        return RecoveryCopyReport(backup_id, str(target), status, tuple(reports),
                                  "Exact opaque recovery copies verified." if status == "verified"
                                  else "One or more recovery copies changed or are unavailable.")


def _sources_from_manifest(manifest: dict) -> tuple[BackupSource, BackupSource]:
    rows = manifest.get("files")
    if not isinstance(rows, list) or len(rows) != 2:
        raise BackupError("The backup manifest does not contain exactly two selected files.")
    sources = []
    expected_keys = set(BackupSource.__dataclass_fields__)
    for role, row in zip(_ROLES, rows):
        if not isinstance(row, dict) or set(row) != expected_keys or row.get("role") != role:
            raise BackupError("The backup manifest has invalid file roles.")
        source = BackupSource(**row)
        if (
            not isinstance(source.path, str) or len(source.path) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in source.path)
            or not Path(source.path).is_absolute() or ".." in Path(source.path).parts
            or type(source.size) is not int or not 0 <= source.size <= _MAX_FILE
            or not _hex_digest(source.sha256)
            or any(type(getattr(source, key)) is not int or getattr(source, key) < 0
                   for key in ("mtime_ns", "ctime_ns", "device", "inode"))
        ):
            raise BackupError("The backup manifest has invalid selected-file metadata.")
        sources.append(source)
    if os.path.normcase(sources[0].path) == os.path.normcase(sources[1].path):
        raise BackupError("The backup manifest repeats a selected path.")
    return tuple(sources)


def _hex_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _file_digest(
    path: Path, *, progress: Progress | None = None, role: str = "archive",
    should_cancel: Cancel | None = None,
) -> tuple[int, str]:
    info = _regular(path)
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as stream:
            if not _same_open_stat(info, os.fstat(stream.fileno())):
                raise BackupError("An archived configuration file changed while opening.")
            while True:
                _check_cancel(should_cancel)
                block = stream.read(_CHUNK)
                if not block:
                    break
                count += len(block)
                if count > _MAX_FILE:
                    raise BackupError("An archived configuration file exceeds the 16 MiB limit.")
                digest.update(block)
                _progress(progress, role, count, info.st_size)
            if count != info.st_size or not _same_open_stat(info, os.fstat(stream.fileno())):
                raise BackupError("An archived configuration file changed during verification.")
    except BackupError:
        raise
    except InterruptedError:
        raise
    except OSError as exc:
        raise BackupError("An archived configuration file cannot be read.") from exc
    if not _same_stat(info, _regular(path)):
        raise BackupError("An archived configuration file changed during verification.")
    return count, digest.hexdigest()


def _recovery_file_manifest(item: RecoveryCopyFile) -> dict:
    return {"role": item.role, "name": _NAMES[item.role], "size": item.size, "sha256": item.sha256}


def _valid_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 80:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _directory_identity(path: Path) -> tuple[int, int]:
    if _redirected(path):
        raise BackupError("The recovery destination is redirected; its copy cannot be trusted.")
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("The recovery destination is unavailable.") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise BackupError("The recovery destination is not an ordinary directory.")
    return info.st_dev, info.st_ino


def _check_directory_identity(path: Path, expected: tuple[int, int]) -> None:
    if _directory_identity(path) != expected:
        raise BackupError("The recovery destination changed during copying; it remains incomplete.")


def _check_parent_identity(path: Path, expected: tuple[int, int, int, int]) -> None:
    # Creating the new leaf changes the parent's timestamps. Its device and
    # inode must remain those reviewed before reservation.
    if _parent_stat(path)[:2] != expected[:2]:
        raise BackupError("The recovery destination parent changed; the copy remains incomplete.")


def _read_recovery_json(path: Path) -> object:
    if _redirected(path):
        raise BackupError("A recovery marker path is redirected.")
    try:
        before = path.stat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise BackupError("A recovery marker is unavailable.") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 32 * 1024:
        raise BackupError("A recovery marker is unsafe or oversized.")
    try:
        with path.open("rb") as stream:
            if not _same_open_stat(before, os.fstat(stream.fileno())):
                raise BackupError("A recovery marker changed while opening.")
            raw = stream.read(32 * 1024 + 1)
            if len(raw) != before.st_size or not _same_open_stat(before, os.fstat(stream.fileno())):
                raise BackupError("A recovery marker changed while reading.")
        if not _same_stat(before, path.stat()):
            raise BackupError("A recovery marker changed while reading.")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, OSError) as exc:
        raise BackupError("A recovery marker cannot be read.") from exc


def _publish_recovery_json(folder: Path, name: str, value: dict, *, guard=None,
                           should_cancel: Cancel | None = None) -> None:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > 32 * 1024:
        raise BackupError("A recovery marker exceeds its bounded size.")
    temporary = folder / f".{name}.{uuid4().hex}.tmp"
    final = folder / name
    try:
        if guard is not None:
            guard()
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_recovery_json(temporary) != value:
            raise BackupError("A staged recovery marker failed verification.")
        _check_cancel(should_cancel)
        if guard is not None:
            guard()
        os.link(temporary, final)
        temporary.unlink()
        if _read_recovery_json(final) != value:
            raise BackupError("A recovery marker failed readback.")
    except BackupError:
        raise
    except InterruptedError:
        raise
    except OSError as exc:
        raise BackupError("The recovery copy is incomplete; its marker could not be published.") from exc
