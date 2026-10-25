"""Detached companion packages only: no host installation, control, or removal.

Reuses the selected-backup service's bounded filesystem/verified publication
primitives. Its configuration selection and backup authority are not reused.
Qualification and ownership anchors come from trusted application providers,
never from claims in a package JSON file. Reopening verifies bytes, not runtime
qualification. A future host installer needs its own reviewed authority boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from threading import RLock
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from . import swarm_configuration_backup as safe

PackageError = safe.BackupError
FILES = (
    "CollectiGenReadOnlyInventory.cs", "InventoryContract.cs",
    "CollectiGenReadOnlyInventory.csproj", "CollectiGenReadOnlyInventory.dll",
)
INTENT = "companion-intent.json"
MANIFEST = "companion-manifest.json"
MAX_TOTAL = 32 * 1024 * 1024
MAX_ENTRIES = 32


@dataclass(frozen=True)
class QualificationEvidence:
    """A trusted verifier's observation; never constructed from package metadata."""
    artifact_hashes: tuple[tuple[str, str], ...]
    host_assembly_sha256: str
    evidence_sha256: str
    level: str = "isolated-dispatch"
    qualified_artifact_hashes: tuple[tuple[str, str], ...] = ()
    reviewed_support_hashes: tuple[tuple[str, str], ...] = ()
    normal_loader_qualified: bool = False
    install_supported: bool = False


class QualificationVerifier(Protocol):
    def verify(self, files: tuple[safe.BackupSource, ...]) -> QualificationEvidence | None: ...


@dataclass(frozen=True)
class PackagePreview:
    preview_id: str
    destination: str
    files: tuple[safe.BackupSource, ...]
    enrollment: safe.BackupSource
    qualification: QualificationEvidence
    parent_stat: tuple[int, int, int, int]
    ancestors: tuple[tuple[str, int, int], ...]
    source_parents: tuple[tuple[str, int, int], ...]
    fingerprint: str


@dataclass(frozen=True)
class PackageReceipt:
    """Application-retained anchor issued after verified publication, not an import."""
    package_id: str
    directory: str
    directory_identity: tuple[int, int]
    files: tuple[safe.BackupSource, ...]


@dataclass(frozen=True)
class PackageFileReport:
    name: str
    state: str


@dataclass(frozen=True)
class PackageReport:
    directory: str
    package_id: str
    state: str
    files: tuple[PackageFileReport, ...]
    provenance: str = "Package metadata only; runtime qualification is not reattested."


@dataclass(frozen=True)
class RecoveryPreview:
    directory: str
    files: tuple[PackageFileReport, ...]
    recovery_candidates: tuple[str, ...]
    empty_directory_candidate: bool
    execution_supported: bool = False


def _chain(path: Path) -> tuple[tuple[str, int, int], ...]:
    paths = (path, *path.parents)
    if len(paths) > 64:
        raise PackageError("The selected path exceeds the ancestor limit.")
    return tuple((str(p), *safe._directory_identity(p)) for p in paths)


def _check_chain(chain):
    for path, device, inode in chain:
        if safe._directory_identity(Path(path)) != (device, inode):
            raise PackageError("A selected path parent changed; review the detached package again.")


def _stamp_matches(source: safe.BackupSource):
    info = safe._regular(Path(source.path))
    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino) != (
            source.size, source.mtime_ns, source.ctime_ns, source.device, source.inode):
        raise PackageError("A reviewed file changed or was replaced.")


def _qualification(value, files):
    if (type(value) is not QualificationEvidence or value.level not in ("offline-host-compile", "isolated-dispatch")
            or value.artifact_hashes != tuple((f.role, f.sha256) for f in files)
            or not safe._hex_digest(value.host_assembly_sha256) or not safe._hex_digest(value.evidence_sha256)
            or value.qualified_artifact_hashes != tuple((f.role, f.sha256) for f in files if f.role != FILES[2])
            or value.reviewed_support_hashes != tuple((f.role, f.sha256) for f in files if f.role == FILES[2])
            or value.normal_loader_qualified is not False or value.install_supported is not False):
        raise PackageError("Exact artifact qualification is unavailable; package JSON cannot provide it.")
    return value


def _qualification_payload(value):
    data = asdict(value)
    for key in ("artifact_hashes", "qualified_artifact_hashes", "reviewed_support_hashes"):
        data[key] = [list(row) for row in data[key]]
    return data


def _entries(folder: Path):
    names = []
    with os.scandir(folder) as iterator:
        for item in iterator:
            names.append(item.name)
            if len(names) > MAX_ENTRIES:
                raise PackageError("The detached package exceeds the entry limit; preserve it for review.")
    if len({name.casefold() for name in names}) != len(names):
        raise PackageError("Case-colliding package entries are not supported.")
    return tuple(sorted(names))


def _rows(files):
    return [{"name": f.role, "size": f.size, "sha256": f.sha256} for f in files]


def _read_marker(path):
    try:
        return safe._read_recovery_json(path)
    except FileNotFoundError as exc:
        raise PackageError("A required package or enrollment marker is missing.") from exc
    except RecursionError as exc:
        raise PackageError("A package or enrollment marker is nested beyond the supported limit.") from exc


def _manifest(folder: Path):
    data = _read_marker(folder / MANIFEST)
    keys = {"schema", "package_id", "scope", "files", "enrollment_sha256", "qualification", "intent_sha256"}
    if (type(data) is not dict or set(data) != keys or type(data["schema"]) is not int or data["schema"] != 1
            or data["scope"] != "detached-companion-only" or not safe._hex_digest(data["enrollment_sha256"])
            or not safe._hex_digest(data["intent_sha256"])):
        raise PackageError("Unsupported detached-package manifest.")
    safe._id(data["package_id"])
    rows = data["files"]
    if type(rows) is not list or len(rows) != len(FILES):
        raise PackageError("The package must describe exactly four fixed companion files.")
    for name, row in zip(FILES, rows):
        if (type(row) is not dict or set(row) != {"name", "size", "sha256"} or row["name"] != name
                or type(row["size"]) is not int or not 0 < row["size"] <= 16 * 1024 * 1024
                or not safe._hex_digest(row["sha256"])):
            raise PackageError("Unsafe package file identity.")
    if sum(row["size"] for row in rows) > MAX_TOTAL:
        raise PackageError("The detached package exceeds its total byte budget.")
    q = data["qualification"]
    original_keys = {"artifact_hashes", "host_assembly_sha256", "evidence_sha256", "level"}
    scope_keys = {"qualified_artifact_hashes", "reviewed_support_hashes", "normal_loader_qualified", "install_supported"}
    if (type(q) is not dict or set(q) not in (original_keys, original_keys | scope_keys)
            or q["level"] not in ("offline-host-compile", "isolated-dispatch")
            or not safe._hex_digest(q["host_assembly_sha256"]) or not safe._hex_digest(q["evidence_sha256"])
            or q["artifact_hashes"] != [[row["name"], row["sha256"]] for row in rows]):
        raise PackageError("Unsupported claimed provenance; no qualification can be inferred.")
    if scope_keys <= set(q) and (
            q["qualified_artifact_hashes"] != [row for row in q["artifact_hashes"] if row[0] != FILES[2]]
            or q["reviewed_support_hashes"] != [q["artifact_hashes"][2]]
            or q["normal_loader_qualified"] is not False or q["install_supported"] is not False):
        raise PackageError("The package claims unsupported loader or installation qualification.")
    intent = _read_marker(folder / INTENT)
    if intent != {key: value for key, value in data.items() if key != "intent_sha256"}:
        raise PackageError("Package intent and completion disagree.")
    if safe._file_digest(folder / INTENT)[1] != data["intent_sha256"]:
        raise PackageError("The package intent changed.")
    return data


class SwarmCompanionInstallation:
    """Prepare detached files; no method publishes into or starts a host.

    ``receipt_lookup`` must be an independently trusted application-owned store
    of issued receipts. Without it, a reopened package grants no ownership claim.
    The default verifier is unavailable and performs no implicit runtime IO.
    """

    def __init__(self, enrollment_path: Path | None = None, *, verifier: QualificationVerifier | None = None,
                 receipt_lookup: Callable[[str], PackageReceipt | None] | None = None):
        self.enrollment_path = enrollment_path
        self.verifier = verifier
        self.receipt_lookup = receipt_lookup
        self._issued: dict[str, PackagePreview] = {}
        self._receipts: dict[str, PackageReceipt] = {}
        self._lock = RLock()
        self._archive = None

    @classmethod
    def for_manager(cls, manager):
        """Canonical production entry: fixed reviewed pins and independent receipts.

        Construction and reopening do not read the candidate, host or enrollment.
        Only a new qualified preview needs the retained developer artifacts.
        """
        from .swarm_companion_qualification import PinnedCompanionQualification
        archive = safe.SwarmConfigurationBackup(manager)
        service = cls(Path(manager.record_path), verifier=PinnedCompanionQualification(),
                      receipt_lookup=archive.companion_receipt)
        service._archive = archive
        return service

    def preview_qualified(self, destination, *, should_cancel=None, progress=None):
        """Prepare the one reviewed candidate without a build, download or host read."""
        from .swarm_companion_qualification import PinnedCompanionQualification, UNAVAILABLE
        if self._archive is None or type(self.verifier) is not PinnedCompanionQualification:
            raise PackageError("Use the canonical runtime manager to prepare a qualified package.")
        self._archive.check_companion_receipt_capacity()
        try:
            return self.preview(self.verifier.artifacts(), destination,
                                should_cancel=should_cancel, progress=progress)
        except PackageError as exc:
            if any(not path.exists() for path in self.verifier.artifacts().values()):
                raise PackageError(UNAVAILABLE) from exc
            raise

    def _enrollment(self):
        if self.enrollment_path is None:
            raise PackageError("An explicit enrollment record is required for a new detached package.")
        path = safe._destination_path(self.enrollment_path)
        data = _read_marker(path)  # bounded, single ordinary file; no config discovery
        before = safe._scan("enrollment", path)
        if type(data) is not dict or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise PackageError("The enrollment record is unavailable.")
        for name in ("endpoint", "server_id", "install_root", "adopted_at"):
            if (type(data.get(name)) is not str or not 0 < len(data[name]) <= 4096
                    or any(ord(c) < 32 for c in data[name])):
                raise PackageError("The enrollment binding is incomplete.")
        roots = []
        for name in ("install_root", "model_root", "output_root"):
            if data.get(name):
                if type(data[name]) is not str or len(data[name]) > 4096 or any(ord(c) < 32 for c in data[name]):
                    raise PackageError("An enrollment protection root is invalid.")
                root = Path(data[name])
                if not root.is_absolute() or ".." in root.parts:
                    raise PackageError("An enrollment protection root is not absolute.")
                roots.append(root)
        if _read_marker(path) != data or safe._scan("enrollment", path) != before:
            raise PackageError("Enrollment changed during review.")
        return before, tuple(roots)

    def _attest(self, files):
        try:
            value = None if self.verifier is None else self.verifier.verify(files)
        except Exception as exc:
            raise PackageError("Artifact qualification is unavailable.") from exc
        return _qualification(value, files)

    def preview(self, artifacts: Mapping[str, str | Path], destination: str | Path, *, should_cancel=None, progress=None):
        with self._lock:
            safe._check_cancel(should_cancel)
            if len(self._issued) >= 16 or len(self._receipts) >= 128:
                raise PackageError("The detached package session limit has been reached.")
            if set(artifacts) != set(FILES):
                raise PackageError("Select exactly the four companion artifacts.")
            enrollment, protected = self._enrollment()
            target = safe._destination_path(destination)
            if self._archive is not None and safe._under(target, self._archive.backup_dir):
                raise PackageError("Choose a detached destination outside the private recovery archive.")
            parent_stat = safe._parent_stat(target.parent)
            ancestors = _chain(target.parent)
            if os.path.lexists(target) or any(safe._under(target, root) for root in protected):
                raise PackageError("Choose a new detached folder outside the enrolled installation/model/output roots.")
            paths = tuple(safe._destination_path(artifacts[name]) for name in FILES)
            if len({str(p).casefold() for p in paths}) != len(FILES):
                raise PackageError("Selected source paths collide.")
            if any(safe._under(target, p.parent) for p in paths):
                raise PackageError("Keep the detached destination outside the selected artifact folders.")
            source_parents = tuple(dict.fromkeys(row for p in (*paths, Path(enrollment.path)) for row in _chain(p.parent)))
            sizes = tuple(safe._regular(path).st_size for path in paths)
            if any(size == 0 for size in sizes) or sum(sizes) > MAX_TOTAL:
                raise PackageError("Selected artifacts exceed the package byte budget or are empty.")
            files = tuple(safe._scan(name, path, should_cancel=should_cancel, progress=progress) for name, path in zip(FILES, paths))
            if any(f.size == 0 for f in files) or sum(f.size for f in files) > MAX_TOTAL:
                raise PackageError("Selected artifacts exceed the package byte budget or are empty.")
            qualification = self._attest(files)
            safe.SwarmConfigurationBackup._recovery_capacity(target.parent, sum(f.size for f in files))
            _check_chain(ancestors); _check_chain(source_parents)
            if self._enrollment()[0] != enrollment or safe._parent_stat(target.parent) != parent_stat:
                raise PackageError("Enrollment or destination changed during preview.")
            values = dict(preview_id=uuid4().hex, destination=str(target), files=files, enrollment=enrollment,
                          qualification=qualification, parent_stat=parent_stat, ancestors=ancestors, source_parents=source_parents)
            result = PackagePreview(**values, fingerprint=safe._hash({k: asdict(v) if hasattr(v, "__dataclass_fields__") else
                [asdict(f) for f in v] if k == "files" else v for k, v in values.items()}))
            self._issued[result.preview_id] = result
            return result

    def materialize(self, preview: PackagePreview, *, should_cancel=None, progress=None):
        with self._lock:
            safe._check_cancel(should_cancel)
            if type(preview) is not PackagePreview or self._issued.pop(preview.preview_id, None) != preview:
                raise PackageError("The issued package preview changed or expired.")
            target = safe._destination_path(preview.destination)
            _check_chain(preview.ancestors); _check_chain(preview.source_parents)
            if os.path.lexists(target) or safe._parent_stat(target.parent) != preview.parent_stat:
                raise PackageError("The detached destination changed since preview.")
            if self._enrollment()[0] != preview.enrollment:
                raise PackageError("Enrollment changed since preview.")
            current = tuple(safe._scan(f.role, Path(f.path), should_cancel=should_cancel) for f in preview.files)
            if current != preview.files or self._attest(current) != preview.qualification:
                raise PackageError("Artifacts or qualification changed since preview.")
            safe.SwarmConfigurationBackup._recovery_capacity(target.parent, sum(f.size for f in current))
            target.mkdir(exist_ok=False)
            identity = safe._directory_identity(target)

            def guard():
                safe._check_cancel(should_cancel)
                _check_chain(preview.ancestors); _check_chain(preview.source_parents)
                safe._check_directory_identity(target, identity)
                for item in (*preview.files, preview.enrollment):
                    _stamp_matches(item)

            package_id = uuid4().hex
            intent = {"schema": 1, "package_id": package_id, "scope": "detached-companion-only",
                      "files": _rows(current), "enrollment_sha256": preview.enrollment.sha256,
                      "qualification": _qualification_payload(preview.qualification)}
            safe._publish_recovery_json(target, INTENT, intent, guard=guard, should_cancel=should_cancel)
            for source in current:
                # This existing primitive is stateless: exclusive staged copy,
                # hash readback, cancellation and guards; no backup-root access.
                safe.SwarmConfigurationBackup._copy(self, source, target / source.role,
                    progress=progress, should_cancel=should_cancel, guard=guard)
            guard()
            refreshed = tuple(safe._scan(f.role, Path(f.path), should_cancel=should_cancel) for f in current)
            if (refreshed != current or self._enrollment()[0] != preview.enrollment
                    or self._attest(refreshed) != preview.qualification):
                raise PackageError("Authority changed during copying; retain the incomplete detached folder.")
            manifest = {**intent, "intent_sha256": safe._file_digest(target / INTENT, should_cancel=should_cancel)[1]}
            safe._publish_recovery_json(target, MANIFEST, manifest, guard=guard, should_cancel=should_cancel)
            guard()
            if self.verify(target, should_cancel=should_cancel).state != "verified-bytes":
                raise PackageError("The detached package failed final readback.")
            receipt = PackageReceipt(package_id, str(target), identity,
                tuple(safe._scan(name, target / name, should_cancel=should_cancel) for name in (*FILES, INTENT, MANIFEST)))
            if (_rows(receipt.files[:4]) != _rows(current)
                    or _manifest(target) != manifest):
                raise PackageError("The detached package changed before its ownership receipt was issued.")
            guard()
            for item in receipt.files:
                _stamp_matches(item)
            if set(_entries(target)) != {*FILES, INTENT, MANIFEST}:
                raise PackageError("Unknown files appeared before the ownership receipt was issued.")
            if self._archive is not None:
                try:
                    self._archive.record_companion_receipt(receipt)
                except (PackageError, OSError, ValueError) as exc:
                    raise PackageError("Package bytes were created, but the independent ownership receipt was not confirmed. Retain the package and review recovery.") from exc
            else:
                self._receipts[package_id] = receipt
            return receipt

    def verify(self, directory: str | Path, *, should_cancel=None):
        folder = safe._destination_path(directory)
        identity = safe._directory_identity(folder)
        safe._check_cancel(should_cancel)
        names = _entries(folder)
        if MANIFEST not in names:
            return PackageReport(str(folder), "", "incomplete", tuple(PackageFileReport(n, "retain-incomplete") for n in names))
        markers = tuple(safe._scan(n, folder / n, should_cancel=should_cancel) for n in (INTENT, MANIFEST))
        data = _manifest(folder)
        reports = []
        snapshots = []
        for row in data["files"]:
            safe._check_cancel(should_cancel)
            try:
                actual = safe._scan(row["name"], folder / row["name"], should_cancel=should_cancel)
                snapshots.append(actual)
                state = "verified" if (actual.size, actual.sha256) == (row["size"], row["sha256"]) else "changed"
            except PackageError:
                state = "missing-or-unsafe"
            reports.append(PackageFileReport(row["name"], state))
        reports.extend(PackageFileReport(n, "retain-unknown") for n in names if n not in (*FILES, INTENT, MANIFEST))
        safe._check_directory_identity(folder, identity)
        for item in (*markers, *snapshots):
            _stamp_matches(item)
        if _entries(folder) != names:
            raise PackageError("The detached package changed during verification.")
        return PackageReport(str(folder), data["package_id"], "verified-bytes" if all(r.state == "verified" for r in reports)
                             else "needs-review", tuple(reports))

    def preview_recovery(self, directory: str | Path, *, should_cancel=None):
        """Preview exact unchanged owned files only. No removal/copy executor exists."""
        report = self.verify(directory, should_cancel=should_cancel)
        folder = Path(report.directory)
        identity = safe._directory_identity(folder)
        names = _entries(folder)
        receipt = self._receipts.get(report.package_id)
        if receipt is None and report.package_id and self.receipt_lookup is not None:
            receipt = self.receipt_lookup(report.package_id)
        trusted = (type(receipt) is PackageReceipt and receipt.package_id == report.package_id
                   and receipt.directory == str(folder) and receipt.directory_identity == safe._directory_identity(folder)
                   and tuple(f.role for f in receipt.files) == (*FILES, INTENT, MANIFEST))
        if trusted:
            for marker in receipt.files[-2:]:
                try:
                    unchanged = safe._scan(marker.role, folder / marker.role, should_cancel=should_cancel) == marker
                except PackageError:
                    unchanged = False
                if not unchanged:
                    trusted = False
        rows = []
        snapshots = []
        expected = {f.role: f for f in receipt.files} if trusted else {}
        for name in names:
            safe._check_cancel(should_cancel)
            state = "retain-untrusted" if not trusted else "retain-unknown"
            if name in expected:
                try:
                    current = safe._scan(name, folder / name, should_cancel=should_cancel)
                    snapshots.append(current)
                    state = "unchanged-owned-preview-only" if current == expected[name] else "retain-changed"
                except PackageError:
                    state = "retain-unsafe"
            rows.append(PackageFileReport(name, state))
        safe._check_directory_identity(folder, identity)
        for item in snapshots:
            _stamp_matches(item)
        if _entries(folder) != names:
            raise PackageError("The detached package changed during recovery preview.")
        candidates = tuple(r.name for r in rows if r.state == "unchanged-owned-preview-only" and r.name in FILES)
        return RecoveryPreview(str(folder), tuple(rows), candidates,
            trusted and len(rows) == 6 and all(r.state == "unchanged-owned-preview-only" for r in rows))
