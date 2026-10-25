"""Explicit selected-file offline video dependency packs.

This copies caller-selected local bytes only. Verified files and a declared
runtime closure are not proof that the runtime executes independently offline,
nor do they clear native-code attribution or distribution obligations.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, Callable, Mapping
from uuid import uuid4


SCHEMA_ID = "collectigen.video-dependency-pack.v1"
MANIFEST_NAME = "video-dependency-manifest.json"
# The reviewed embedded Comfy Python runtime contains over 43,000 ordinary
# files. Its selected code, dependencies, and stock route assets can exceed
# 20,000 files; retain a finite bound below the selector's 80,000-entry cap.
MAX_FILES = 60_000
MAX_TOTAL_BYTES = 256 * 1024**3
MAX_MANIFEST_BYTES = 64 * 1024**2
FREE_SPACE_RESERVE = 64 * 1024**2
CHUNK_BYTES = 1024**2
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}
Cancel = Callable[[], bool]


class VideoPackError(ValueError):
    """The exact selected pack cannot be verified or safely published."""


def _json(value: Any) -> Any:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise VideoPackError("Pack metadata must contain finite JSON values.") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise VideoPackError("Pack manifest exceeds its metadata bound.")
    return deepcopy(value)


def _canonical(value: Any) -> bytes:
    _json(value)
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise VideoPackError(f"{label} needs a full lowercase SHA-256 hash.")
    return value


def _text(value: str, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise VideoPackError(f"{label} needs bounded nonempty text.")
    return value


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise VideoPackError("Pack path must be a relative POSIX path.")
    path = PurePosixPath(value)
    if (str(path) != value or any(
        part in {"", ".", ".."} or part.endswith((".", " "))
        or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICES
        or any(ord(char) < 32 or char in '<>"|?*' for char in part)
        for part in path.parts
    )
            or ":" in value or value.casefold() == MANIFEST_NAME.casefold()
            or len(value.encode("utf-8")) > 1024):
        raise VideoPackError("Pack path is unsafe, ambiguous, or reserved.")
    return value


def _redirected(path: Path) -> bool:
    for member in (path, *path.parents):
        try:
            info = member.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _signature(path: Path) -> tuple[int, int, int, int]:
    if _redirected(path):
        raise VideoPackError("A selected pack path is redirected.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise VideoPackError("Every selected pack source must be a regular file.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _stream_hash(path: Path, cancel: Cancel, *, expected_signature=None,
                 writer=None) -> tuple[str, int]:
    before = _signature(path)
    if expected_signature is not None and before != expected_signature:
        raise VideoPackError(f"Selected source changed before reading: {path.name}.")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
            raise VideoPackError(f"Selected source changed while opening: {path.name}.")
        while chunk := source.read(CHUNK_BYTES):
            if cancel():
                raise InterruptedError("Video pack operation cancelled before publication.")
            total += len(chunk)
            if total > MAX_TOTAL_BYTES:
                raise VideoPackError("Selected pack bytes exceed the total bound.")
            digest.update(chunk)
            if writer is not None:
                writer.write(chunk)
    if cancel():
        raise InterruptedError("Video pack operation cancelled before publication.")
    if _signature(path) != before:
        raise VideoPackError(f"Selected source changed while reading: {path.name}.")
    return digest.hexdigest(), total


@dataclass(frozen=True)
class PackFile:
    role: str
    source_path: str | Path
    relative_path: str
    sha256: str
    byte_length: int
    license_id: str
    source_record: Mapping[str, Any]
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in {"interpreter", "dependency_manifest"} and not (
            self.role.startswith(("model:", "source:", "dependency:", "license:"))
            and _ROLE.fullmatch(self.role.split(":", 1)[1])
        ):
            raise VideoPackError("Pack role must identify a model, source, interpreter, dependency manifest, dependency, or license.")
        path = Path(self.source_path)
        if not path.is_absolute():
            raise VideoPackError("Each selected source path must be absolute.")
        _relative(self.relative_path)
        _hash(self.sha256, "Selected file")
        if type(self.byte_length) is not int or not 0 <= self.byte_length <= MAX_TOTAL_BYTES:
            raise VideoPackError("Selected file needs a bounded nonnegative byte length.")
        _text(self.license_id, "Selected-file license ID", 256)
        if not isinstance(self.source_record, Mapping) or not self.source_record:
            raise VideoPackError("Selected file needs an explicit source record.")
        _json(dict(self.source_record))
        _json(dict(self.extra_fields))

    @property
    def path(self) -> Path:
        return Path(self.source_path)

    def manifest_dict(self) -> dict[str, Any]:
        result = _json(dict(self.extra_fields))
        result.update(role=self.role, path=self.relative_path, sha256=self.sha256,
                      byte_length=self.byte_length, license_id=self.license_id,
                      source_record=_json(dict(self.source_record)))
        return result


@dataclass(frozen=True)
class PackSelection:
    files: tuple[PackFile, ...]
    model_roles: tuple[str, ...]
    source_revision: str
    interpreter_relative_path: str
    dependency_manifest_relative_path: str
    compatibility_pins: Mapping[str, str]
    runtime_closure_supplied: bool = False
    attribution_gates: tuple[str, ...] = ()
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple) or not 4 <= len(self.files) <= MAX_FILES or any(
            not isinstance(item, PackFile) for item in self.files
        ):
            raise VideoPackError("Pack needs bounded, typed selected model/source/interpreter/dependency files.")
        paths = [item.relative_path.casefold() for item in self.files]
        path_set = set(paths)
        roles = [item.role for item in self.files]
        if len(path_set) != len(paths) or len(set(roles)) != len(roles):
            raise VideoPackError("Pack paths and roles must be unique even across case variants.")
        for file_path in paths:
            segments = file_path.split("/")
            if any("/".join(segments[:index]) in path_set for index in range(1, len(segments))):
                raise VideoPackError("A pack file cannot also be another file's parent directory.")
        if sum(item.byte_length for item in self.files) > MAX_TOTAL_BYTES:
            raise VideoPackError("Selected pack exceeds its total byte bound.")
        if (not self.model_roles or len(set(self.model_roles)) != len(self.model_roles)
                or any(not _ROLE.fullmatch(role) for role in self.model_roles)
                or {f"model:{role}" for role in self.model_roles} != {
                    role for role in roles if role.startswith("model:")
                }):
            raise VideoPackError("Selected model roles must match exact model files.")
        if (roles.count("interpreter") != 1 or roles.count("dependency_manifest") != 1
                or not any(role.startswith("source:") for role in roles)):
            raise VideoPackError("Pack needs one interpreter, one dependency manifest and explicit source files.")
        by_role = {item.role: item for item in self.files}
        if (by_role["interpreter"].relative_path != self.interpreter_relative_path
                or by_role["dependency_manifest"].relative_path != self.dependency_manifest_relative_path):
            raise VideoPackError("Interpreter or dependency-manifest path differs from selected file.")
        _text(self.source_revision, "Source revision", 256)
        if not isinstance(self.compatibility_pins, Mapping) or not self.compatibility_pins or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in self.compatibility_pins.items()
        ):
            raise VideoPackError("Pack needs explicit compatibility pins.")
        if type(self.runtime_closure_supplied) is not bool:
            raise VideoPackError("Runtime closure declaration must be boolean.")
        if any(not isinstance(gate, str) or not gate.strip() for gate in self.attribution_gates):
            raise VideoPackError("Attribution gates must be explicit text.")
        _json(dict(self.compatibility_pins))
        _json(list(self.attribution_gates))
        _json(dict(self.extra_fields))

    def manifest_dict(self) -> dict[str, Any]:
        result = _json(dict(self.extra_fields))
        result.update(schema_id=SCHEMA_ID, schema_version=1,
                      files=[item.manifest_dict() for item in self.files],
                      model_roles=list(self.model_roles), source_revision=self.source_revision,
                      interpreter_relative_path=self.interpreter_relative_path,
                      dependency_manifest_relative_path=self.dependency_manifest_relative_path,
                      compatibility_pins=_json(dict(self.compatibility_pins)),
                      runtime_closure_supplied=self.runtime_closure_supplied,
                      attribution_gates=list(self.attribution_gates),
                      independently_offline_qualified=False)
        return result

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.manifest_dict())).hexdigest()


@dataclass(frozen=True)
class PackPlan:
    selection: PackSelection
    destination: Path
    source_signatures: tuple[tuple[int, int, int, int], ...]
    manifest_sha256: str
    required_bytes: int
    free_bytes: int
    # Caller must obtain exact destination confirmation and apply its external-write
    # policy before publish; selected resource authority alone permits no sharing.
    destination_confirmation_required: bool = True


@dataclass(frozen=True)
class PackVerification:
    destination: Path
    manifest_sha256: str
    file_count: int
    byte_length: int
    selected_files_verified: bool
    declared_runtime_closure_supplied: bool
    independently_offline_qualified: bool
    unresolved_gates: tuple[str, ...]
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class PackResult:
    status: str
    destination: Path
    message: str
    manifest_sha256: str = ""


class VideoDependencyPackService:
    """Verify explicitly selected local files, then publish one no-clobber pack."""

    @staticmethod
    def _paths(destination: str | Path) -> tuple[Path, Path]:
        target = Path(destination)
        if not target.is_absolute() or ".." in target.parts or str(target).startswith("\\\\"):
            raise VideoPackError("Pack destination must be an explicit absolute local path.")
        _relative(target.name)
        parent = target.parent
        if (target.name in {"", ".", ".."} or target.name.endswith((".", " "))
                or target.name.split(".", 1)[0].casefold() in _WINDOWS_DEVICES
                or not parent.is_dir() or _redirected(parent) or _redirected(target)):
            raise VideoPackError("Choose an existing, unredirected local pack destination parent.")
        return target, parent / f".{target.name}.collectigen-video-pack-journal.json"

    def preview(self, selection: PackSelection, destination: str | Path, *,
                should_cancel: Cancel | None = None,
                disk_free: Callable[[Path], int] | None = None) -> PackPlan:
        if not isinstance(selection, PackSelection):
            raise VideoPackError("Pack needs a typed exact file selection.")
        cancel = should_cancel or (lambda: False)
        target, journal = self._paths(destination)
        if os.path.lexists(target) or os.path.lexists(journal):
            raise FileExistsError("Pack destination or its recovery journal already exists.")
        signatures = []
        for item in selection.files:
            signature = _signature(item.path)
            digest, size = _stream_hash(item.path, cancel, expected_signature=signature)
            if digest != item.sha256 or size != item.byte_length:
                raise VideoPackError(f"Selected file changed or differs from its full hash: {item.relative_path}.")
            signatures.append(signature)
        manifest = _canonical(selection.manifest_dict())
        needed = sum(item.byte_length for item in selection.files) + len(manifest) + FREE_SPACE_RESERVE
        free = (disk_free or (lambda parent: shutil.disk_usage(parent).free))(target.parent)
        if type(free) is not int or free < needed:
            raise VideoPackError("The selected local destination lacks pack staging space plus reserve.")
        return PackPlan(selection, target, tuple(signatures), hashlib.sha256(manifest).hexdigest(), needed, free)

    @staticmethod
    def _journal_path(target: Path) -> Path:
        return target.parent / f".{target.name}.collectigen-video-pack-journal.json"

    @staticmethod
    def _write_new(path: Path, content: bytes) -> None:
        if _redirected(path):
            raise VideoPackError("Pack output path is redirected.")
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _publish_stage(stage: Path, target: Path) -> None:
        if os.path.lexists(target) or _redirected(stage) or _redirected(target):
            raise FileExistsError("Pack destination appeared before publication.")
        if os.name != "nt":
            raise VideoPackError("Atomic no-replace directory publication is currently supported on Windows only.")
        os.rename(stage, target)  # Windows directory rename refuses an existing destination.

    def publish(self, plan: PackPlan, *, destination_confirmed: bool = False,
                should_cancel: Cancel | None = None) -> PackResult:
        if not isinstance(plan, PackPlan):
            raise VideoPackError("Publish needs a reviewed pack plan.")
        if destination_confirmed is not True:
            raise VideoPackError("Exact destination confirmation and external-write policy are required before copying.")
        cancel = should_cancel or (lambda: False)
        if cancel():
            raise InterruptedError("Video pack cancelled before any output was created.")
        target, journal = self._paths(plan.destination)
        if target != plan.destination or os.path.lexists(target) or os.path.lexists(journal):
            raise FileExistsError("Pack destination or journal changed after preview.")
        if plan.selection.fingerprint != plan.manifest_sha256:
            raise VideoPackError("Pack selection changed after preview.")
        if len(plan.source_signatures) != len(plan.selection.files):
            raise VideoPackError("Pack plan no longer describes every selected source.")
        for item, signature in zip(plan.selection.files, plan.source_signatures):
            if _signature(item.path) != signature:
                raise VideoPackError("A selected source changed after preview.")
        if shutil.disk_usage(target.parent).free < plan.required_bytes:
            raise VideoPackError("Pack destination no longer has the previewed free-space reserve.")
        stage = target.parent / f".{target.name}.collectigen-stage-{uuid4().hex}"
        journal_payload = {"schema_id": SCHEMA_ID, "schema_version": 1,
                           "destination": target.name, "stage": stage.name,
                           "manifest_sha256": plan.manifest_sha256,
                           "files": [item.relative_path for item in plan.selection.files]}
        self._write_new(journal, _canonical(journal_payload))
        try:
            stage.mkdir()
            for item, signature in zip(plan.selection.files, plan.source_signatures):
                output = stage.joinpath(*PurePosixPath(item.relative_path).parts)
                if _redirected(output):
                    raise VideoPackError("Pack staging path is redirected.")
                output.parent.mkdir(parents=True, exist_ok=True)
                if _redirected(output):
                    raise VideoPackError("Pack staging path changed to a redirect.")
                with output.open("xb") as writer:
                    digest, size = _stream_hash(item.path, cancel,
                                                expected_signature=signature, writer=writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                if digest != item.sha256 or size != item.byte_length:
                    raise VideoPackError(f"Copied source differs from selected identity: {item.relative_path}.")
            self._write_new(stage / MANIFEST_NAME, _canonical(plan.selection.manifest_dict()))
            self.verify(stage, expected_fingerprint=plan.manifest_sha256, should_cancel=cancel)
            if cancel():
                raise InterruptedError("Pack cancelled before atomic publication.")
            self._publish_stage(stage, target)
            verified = self.verify(target, expected_fingerprint=plan.manifest_sha256)
            journal.unlink()
            return PackResult("published", target, "Selected pack files verified and published locally.",
                              verified.manifest_sha256)
        except Exception as exc:
            return PackResult("recovery_required", target,
                              f"Pack publication stopped; inspect or recover the owned staging journal: {exc}",
                              plan.manifest_sha256)

    def verify(self, destination: str | Path, *, expected_fingerprint: str | None = None,
               should_cancel: Cancel | None = None) -> PackVerification:
        cancel = should_cancel or (lambda: False)
        target = Path(destination).expanduser().absolute()
        if _redirected(target) or not target.is_dir():
            raise VideoPackError("Pack folder is missing or redirected.")
        manifest_path = target / MANIFEST_NAME
        before = _signature(manifest_path)
        if before[2] > MAX_MANIFEST_BYTES:
            raise VideoPackError("Pack manifest exceeds its bound.")
        with manifest_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
                raise VideoPackError("Pack manifest changed while opening.")
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if _signature(manifest_path) != before or len(raw) > MAX_MANIFEST_BYTES:
            raise VideoPackError("Pack manifest changed while reading.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise VideoPackError("Pack manifest is not valid UTF-8 JSON.") from exc
        if (not isinstance(payload, dict) or payload.get("schema_id") != SCHEMA_ID
                or payload.get("schema_version") != 1 or payload.get("independently_offline_qualified") is not False
                or _canonical(payload) != raw):
            raise VideoPackError("Unknown or noncanonical pack manifest; refuse interpretation.")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_fingerprint is not None and digest != _hash(expected_fingerprint, "Pack manifest"):
            raise VideoPackError("Pack manifest differs from the selected identity.")
        rows = payload.get("files")
        if not isinstance(rows, list) or not 4 <= len(rows) <= MAX_FILES:
            raise VideoPackError("Pack manifest has an invalid file count.")
        expected: dict[str, Mapping[str, Any]] = {}
        folded: set[str] = set()
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                raise VideoPackError("Pack file entry must be an object.")
            name = _relative(row.get("path"))
            if name.casefold() in folded:
                raise VideoPackError("Pack manifest contains duplicate paths.")
            folded.add(name.casefold())
            _hash(row.get("sha256"), "Pack file")
            size = row.get("byte_length")
            if type(size) is not int or not 0 <= size <= MAX_TOTAL_BYTES:
                raise VideoPackError("Pack file has an invalid byte length.")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise VideoPackError("Pack exceeds its total byte bound.")
            expected[name] = row
        # Check all known manifest claims using the same constructor as preview.
        try:
            files = tuple(PackFile(row["role"], target.joinpath(*PurePosixPath(row["path"]).parts),
                                   row["path"], row["sha256"], row["byte_length"],
                                   row["license_id"], row["source_record"],
                                   {key: value for key, value in row.items() if key not in {
                                       "role", "path", "sha256", "byte_length", "license_id", "source_record",
                                   }}) for row in rows)
            selection = PackSelection(
                files, tuple(payload["model_roles"]), payload["source_revision"],
                payload["interpreter_relative_path"], payload["dependency_manifest_relative_path"],
                payload["compatibility_pins"], payload["runtime_closure_supplied"],
                tuple(payload["attribution_gates"]),
                {key: value for key, value in payload.items() if key not in {
                    "schema_id", "schema_version", "files", "model_roles", "source_revision",
                    "interpreter_relative_path", "dependency_manifest_relative_path", "compatibility_pins",
                    "runtime_closure_supplied", "attribution_gates", "independently_offline_qualified",
                }},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VideoPackError("Pack manifest has missing or invalid selected-file fields.") from exc
        if selection.manifest_dict() != payload:
            raise VideoPackError("Pack manifest has conflicting known fields.")
        found: set[str] = set()
        expected_directories = {
            "/".join(PurePosixPath(name).parts[:index])
            for name in expected for index in range(1, len(PurePosixPath(name).parts))
        }
        for current, dirs, names in os.walk(target, followlinks=False):
            base = Path(current)
            if _redirected(base):
                raise VideoPackError("Pack contains a redirected directory.")
            for dirname in dirs:
                folder = base / dirname
                if (_redirected(folder) or folder.relative_to(target).as_posix() not in expected_directories):
                    raise VideoPackError("Pack contains a redirected or unlisted directory.")
            for name in names:
                path = base / name
                if _redirected(path):
                    raise VideoPackError("Pack contains a redirected file.")
                relative = path.relative_to(target).as_posix()
                if relative != MANIFEST_NAME:
                    if relative not in expected:
                        raise VideoPackError("Pack contains an unlisted file.")
                    found.add(relative)
        if found != set(expected):
            raise VideoPackError("Pack is missing selected files.")
        for name, row in expected.items():
            path = target.joinpath(*PurePosixPath(name).parts)
            digest, size = _stream_hash(path, cancel)
            if digest != row["sha256"] or size != row["byte_length"]:
                raise VideoPackError(f"Pack file no longer matches its selected identity: {name}.")
        gates = list(payload["attribution_gates"])
        gates.extend(("distribution_license_review_not_qualified",
                      "runtime_dependency_closure_not_independently_verified",
                      "independent_offline_execution_not_qualified"))
        if not payload["runtime_closure_supplied"]:
            gates.append("runtime_dependency_closure_not_supplied")
        return PackVerification(target, hashlib.sha256(raw).hexdigest(),
                                len(rows), total, True, payload["runtime_closure_supplied"], False,
                                tuple(dict.fromkeys(gates)), _json(payload))

    def recover(self, destination: str | Path, *, selection: PackSelection,
                action: str = "finish", destination_confirmed: bool = False,
                should_cancel: Cancel | None = None) -> PackResult:
        if action not in {"finish", "discard"} or not isinstance(selection, PackSelection):
            raise VideoPackError("Recovery needs the exact selection and finish or discard action.")
        if destination_confirmed is not True:
            raise VideoPackError("Exact destination confirmation and external-write policy are required before recovery mutation.")
        target, journal = self._paths(destination)
        if not journal.is_file() or _redirected(journal) or _signature(journal)[2] > MAX_MANIFEST_BYTES:
            raise VideoPackError("No safe bounded video-pack recovery journal exists.")
        try:
            before = _signature(journal)
            with journal.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
                    raise VideoPackError("Recovery journal changed while opening.")
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES or _signature(journal) != before:
                raise VideoPackError("Recovery journal changed while reading.")
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise VideoPackError("Recovery journal is not valid UTF-8 JSON.") from exc
        stage_name = record.get("stage") if isinstance(record, dict) else None
        if (not isinstance(stage_name, str) or not re.fullmatch(
            re.escape(f".{target.name}.collectigen-stage-") + r"[0-9a-f]{32}", stage_name
        ) or record.get("schema_id") != SCHEMA_ID or record.get("schema_version") != 1
                or record.get("destination") != target.name
                or record.get("manifest_sha256") != selection.fingerprint
                or record.get("files") != [item.relative_path for item in selection.files]):
            raise VideoPackError("Recovery journal does not match this exact selected pack.")
        stage = target.parent / stage_name
        if os.path.lexists(target):
            if not os.path.lexists(stage):
                result = self.verify(target, expected_fingerprint=selection.fingerprint,
                                     should_cancel=should_cancel)
                journal.unlink()
                return PackResult("already_published", target, "Verified published pack and cleared its journal.",
                                  result.manifest_sha256)
            raise VideoPackError("Both staging and destination exist; recovery needs manual review.")
        if action == "discard":
            if os.path.lexists(stage):
                self._discard_stage(stage, {item.relative_path for item in selection.files})
            journal.unlink()
            return PackResult("discarded", target, "Only owned staging was discarded; sources remain unchanged.")
        if not stage.is_dir() or _redirected(stage):
            return PackResult("recovery_required", target, "Staging is incomplete; discard this journal before retrying.")
        try:
            result = self.verify(stage, expected_fingerprint=selection.fingerprint,
                                 should_cancel=should_cancel)
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Pack recovery cancelled before publication.")
            self._publish_stage(stage, target)
            self.verify(target, expected_fingerprint=selection.fingerprint)
            journal.unlink()
            return PackResult("published", target, "Verified staged pack published locally.",
                              result.manifest_sha256)
        except (VideoPackError, OSError, InterruptedError) as exc:
            return PackResult("recovery_required", target, f"Pack staging still needs recovery: {exc}")

    @staticmethod
    def _discard_stage(stage: Path, expected: set[str]) -> None:
        if _redirected(stage) or not stage.is_dir():
            raise VideoPackError("Recovery stage is redirected or not a directory.")
        allowed = expected | {MANIFEST_NAME}
        allowed_directories = {
            "/".join(PurePosixPath(name).parts[:index])
            for name in expected for index in range(1, len(PurePosixPath(name).parts))
        }
        directories: list[Path] = []
        files: list[Path] = []
        for current, dirs, names in os.walk(stage, followlinks=False):
            base = Path(current)
            directories.append(base)
            for name in dirs:
                folder = base / name
                if (_redirected(folder) or folder.relative_to(stage).as_posix() not in allowed_directories):
                    raise VideoPackError("Recovery stage contains an unowned directory; refuse deletion.")
            for name in names:
                path = base / name
                relative = path.relative_to(stage).as_posix()
                if relative not in allowed or _redirected(path) or not stat.S_ISREG(path.lstat().st_mode):
                    raise VideoPackError("Recovery stage contains an unowned entry; refuse deletion.")
                files.append(path)
        for path in files:
            path.unlink()
        for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
            path.rmdir()
