"""Private retained model-file choices, never saved inspection authority.

Opening a draft reads only the explicitly selected JSON file. Model bytes are
read solely by explicit revalidation; registration stays with ModelWorkspace.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .model_resource_registry import (
    ModelResourceError, RESOURCE_ROLES, _bounded_read, _bytes, _cancel, _json,
    inspect_model_resource,
)
from .model_workspace import IndexDeclaration, IndexPreview, _path, _pinned_parent
from .external_write_transaction import atomic_verified_bytes

MAX_FILES = 20_000
MAX_HELD = 100_000
MAX_DRAFT_BYTES = 16 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ScanReviewCandidate:
    path: Path
    resource_id: str
    size: int


@dataclass(frozen=True)
class ScanReviewDraft:
    root: Path
    registry_token: str
    candidates: tuple[ScanReviewCandidate, ...]
    declarations: tuple[IndexDeclaration, ...]
    selected_paths: tuple[Path, ...]
    held: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ScanReviewValidation:
    preview: IndexPreview
    declarations: tuple[IndexDeclaration, ...]
    selected_paths: tuple[Path, ...]
    completed_paths: tuple[Path, ...]
    held: tuple[tuple[str, str], ...]
    registry_changed: bool
    retained_draft: ScanReviewDraft


def _text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ModelResourceError(f"The saved review has an invalid {label}.")
    return value


def _lexical_path(value):
    """Validate text only; never resolve, stat or follow saved paths."""
    value = _text(value, 4096, "path")
    path = Path(value)
    if (value.startswith(("\\\\?\\", "\\\\.\\")) or not path.is_absolute() or ".." in path.parts
            or any(":" in part or part.endswith((".", " ")) for part in path.parts[1:])):
        raise ModelResourceError("Saved model paths must be absolute and cannot escape their folder.")
    return path


def _key(path):
    return str(path).casefold()


def _inside(path, root):
    if path == root or root not in path.parents:
        raise ModelResourceError("A saved model path escapes its reviewed folder.")


def _sequence(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ModelResourceError("The saved file review exceeds its supported row limit.")
    return value


def _decode(value):
    fields = {"schema_version", "root", "registry_token", "candidates", "declarations", "selected_paths", "held"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ModelResourceError("Unsupported file-review schema; preserve the original draft.")
    root = _lexical_path(value["root"])
    token = value["registry_token"]
    if not isinstance(token, str) or not _HASH.fullmatch(token):
        raise ModelResourceError("The saved registry revision is invalid.")
    candidates, paths = [], {}
    for row in _sequence(value["candidates"], MAX_FILES):
        if not isinstance(row, dict) or set(row) != {"path", "resource_id", "size"}:
            raise ModelResourceError("Invalid saved candidate fields.")
        path = _lexical_path(row["path"]); _inside(path, root)
        if (path.suffix.casefold() != ".safetensors" or _key(path) in paths
                or not isinstance(row["resource_id"], str) or not _RESOURCE.fullmatch(row["resource_id"])
                or type(row["size"]) is not int or not 0 < row["size"] <= 2**63 - 1):
            raise ModelResourceError("Invalid or duplicate saved model candidate.")
        paths[_key(path)] = path
        candidates.append(ScanReviewCandidate(path, row["resource_id"], row["size"]))
    declarations, assigned = [], set()
    for row in _sequence(value["declarations"], MAX_FILES):
        if not isinstance(row, dict) or set(row) != {"path", "role", "family"}:
            raise ModelResourceError("Invalid saved declaration fields.")
        path = _lexical_path(row["path"])
        key = _key(path)
        if key not in paths or key in assigned or row["role"] not in RESOURCE_ROLES:
            raise ModelResourceError("A saved declaration repeats or references an unknown file or role.")
        family = _text(row["family"], 160, "model family")
        assigned.add(key); declarations.append(IndexDeclaration(paths[key], row["role"], family))
    selected, checked = [], set()
    for raw in _sequence(value["selected_paths"], MAX_FILES):
        key = _key(_lexical_path(raw))
        if key not in paths or key in checked:
            raise ModelResourceError("The saved selection repeats or references an unknown file.")
        checked.add(key); selected.append(paths[key])
    held, held_paths = [], set()
    for row in _sequence(value["held"], MAX_HELD):
        if not isinstance(row, list) or len(row) != 2:
            raise ModelResourceError("Invalid saved held-file row.")
        name = _text(row[0], 4096, "held path")
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts or any(":" in part for part in relative.parts):
            raise ModelResourceError("A saved held path escapes its reviewed folder.")
        if _key(relative) in held_paths or _key(root / relative) in paths:
            raise ModelResourceError("Duplicate or conflicting saved held-file row.")
        held_paths.add(_key(relative))
        held.append((name, _text(row[1], 4096, "held reason")))
    return ScanReviewDraft(root, token, tuple(candidates), tuple(declarations), tuple(selected), tuple(held))


def _encode(draft):
    if not isinstance(draft, ScanReviewDraft):
        raise ModelResourceError("Choose a retained file review.")
    try:
        value = {"schema_version": 1, "root": str(draft.root), "registry_token": draft.registry_token,
            "candidates": [{"path": str(row.path), "resource_id": row.resource_id, "size": row.size} for row in draft.candidates],
            "declarations": [{"path": str(row.path), "role": row.role, "family": row.family} for row in draft.declarations],
            "selected_paths": [str(path) for path in draft.selected_paths], "held": [list(row) for row in draft.held]}
        _decode(value)
        raw = _bytes(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ModelResourceError("Invalid retained file review.") from exc
    if len(raw) > MAX_DRAFT_BYTES:
        raise ModelResourceError("The saved file review exceeds its supported size limit.")
    return raw


def _read_draft_bytes(path):
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ModelResourceError("A linked draft or non-regular file cannot be used for a file review.")
    if int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
        raise ModelResourceError("The selected review must be available locally; implicit cloud hydration is disabled.")
    return _bounded_read(path, MAX_DRAFT_BYTES)


@contextmanager
def _writer(path, protected_roots):
    """Use the registry writer's OS-lock convention for this exact draft."""
    lock = _path(path.with_name(path.name + ".lock"), protected_roots)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        info = os.fstat(handle.fileno())
        named = lock.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise ModelResourceError("The file-review lock is not a stable unlinked regular file.")
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
            raise ModelResourceError("Another save of this file review is active.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publish_new(path, raw, should_cancel=None):
    temporary = path.with_name(f".collectigen-file-review-{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        if _bounded_read(temporary, MAX_DRAFT_BYTES) != raw:
            raise ModelResourceError("Saved review staging verification failed.")
        _cancel(should_cancel)
        if os.name == "nt":
            os.rename(temporary, path)  # No-clobber on the qualified platform.
        else:
            os.link(temporary, path)
            temporary.unlink()
        if _bounded_read(path, MAX_DRAFT_BYTES) != raw:
            raise ModelResourceError("Saved review read-back failed; retain the draft for inspection.")
    finally:
        if temporary.exists():
            temporary.unlink()


class ModelScanReviewService:
    def __init__(self, workspace):
        self.workspace = workspace

    def capture(self, preview, *, declarations=(), selected_paths=()):
        if not isinstance(preview, IndexPreview):
            raise ModelResourceError("Review model files before saving their choices.")
        draft = ScanReviewDraft(preview.root, preview.registry_token,
            tuple(ScanReviewCandidate(row.path, row.resource_id, row.size) for row in preview.inspections),
            tuple(declarations), tuple(selected_paths), tuple(preview.held))
        return _decode(_json(_encode(draft)))

    def read(self, path):
        path = _path(path, self.workspace.protected_roots)
        raw = _read_draft_bytes(path)
        if raw is None:
            raise ModelResourceError("The selected file-review draft is missing.")
        return _decode(_json(raw)), hashlib.sha256(raw).hexdigest()

    def save(self, path, draft, *, expected_digest=None, should_cancel=None):
        """No-clobber first save; exact-read-token updates retain a full backup."""
        raw = _encode(draft)
        digest = hashlib.sha256(raw).hexdigest()
        path = _path(path, self.workspace.protected_roots)
        if path.suffix.casefold() != ".json":
            raise ModelResourceError("Choose a new .json file for this private review.")
        _cancel(should_cancel)
        with _pinned_parent(path.parent, self.workspace.protected_roots), _writer(path, self.workspace.protected_roots):
            existing = _read_draft_bytes(path)
            if existing is not None:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ModelResourceError("A linked draft cannot be a save destination.")
                if expected_digest != hashlib.sha256(existing).hexdigest():
                    raise ModelResourceError("This draft already exists or changed. Reopen it before saving, or choose a new file.")
                if existing == raw:
                    return digest
                _cancel(should_cancel)
                backup = _path(path.with_name(path.stem + ".backup-" + uuid4().hex + ".json"), self.workspace.protected_roots)
                _publish_new(backup, existing, should_cancel)
                _cancel(should_cancel)
                if _read_draft_bytes(path) != existing:
                    raise ModelResourceError("The file review changed while saving. Reopen before retrying; its backup is retained.")
                try:
                    atomic_verified_bytes(path, raw, label="Model file review")
                except Exception as exc:
                    raise ModelResourceError(f"Save could not be verified. Reopen the draft or its retained backup: {backup}") from exc
                return digest
            if expected_digest is not None:
                raise ModelResourceError("The previously saved draft disappeared. Choose a new file.")
            _publish_new(path, raw, should_cancel)
        return digest

    def revalidate(self, draft, *, should_cancel=None, progress=None, reconcile_registered=True):
        draft = _decode(_json(_encode(draft)))
        _cancel(should_cancel)
        # Guard the complete explicit selection before inspecting any model.
        _path(draft.root, self.workspace.protected_roots)
        for row in draft.candidates:
            _path(row.path, self.workspace.protected_roots)
        snapshot, token = self.workspace.registry.snapshot()
        assignments = {_key(row.path): row for row in draft.declarations}
        inspections, remaining, completed = [], set(), []
        held_rows = {_key(Path(name)): (name, reason) for name, reason in draft.held}
        for index, row in enumerate(draft.candidates):
            _cancel(should_cancel)
            phase = f"Verifying saved model: {row.path.name}"
            if progress:
                progress(index, len(draft.candidates), phase)
            def file_progress(done, size, label):
                if progress:
                    progress(index, len(draft.candidates),
                        f"{phase} · {label}: {done / 2**20:.1f} / {size / 2**20:.1f} MiB")
            try:
                inspection = inspect_model_resource(_path(row.path, self.workspace.protected_roots),
                    should_cancel=should_cancel, progress=file_progress)
                if inspection.resource_id != row.resource_id or inspection.size != row.size:
                    raise ModelResourceError("Model bytes differ from the saved review. Scan this file again before choosing it.")
            except InterruptedError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                name = str(row.path.relative_to(draft.root))
                held_rows[_key(Path(name))] = (name, str(exc))
                continue
            declaration = assignments.get(_key(row.path))
            record = snapshot.get("resources", {}).get(row.resource_id, {})
            claim = ({"role": declaration.role, "family": declaration.family.strip(),
                "authority": "user_declared", "compatibility": "unverified"} if declaration else None)
            exact = any(isinstance(location, dict) and _key(location.get("path", "")) == _key(row.path)
                and tuple(location.get("file_state", ())) == inspection.file_state for location in record.get("locations", ()))
            if (reconcile_registered and record.get("identity") == inspection.stored_identity() and exact
                    and claim is not None and claim in record.get("declarations", ())):
                completed.append(row.path)
            else:
                inspections.append(inspection); remaining.add(_key(row.path))
        _cancel(should_cancel)
        if self.workspace.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed during review verification. Retry the saved review.")
        if progress:
            progress(len(draft.candidates), len(draft.candidates), "Saved review verified; review remaining files before registration")
        held = tuple(held_rows.values())
        finished = {_key(path) for path in completed}
        retained = replace(draft, registry_token=token,
            candidates=tuple(row for row in draft.candidates if _key(row.path) not in finished),
            declarations=tuple(row for row in draft.declarations if _key(row.path) not in finished),
            selected_paths=tuple(path for path in draft.selected_paths if _key(path) not in finished))
        return ScanReviewValidation(IndexPreview(draft.root, tuple(inspections), tuple(held), token),
            tuple(row for row in draft.declarations if _key(row.path) in remaining),
            tuple(path for path in draft.selected_paths if _key(path) in remaining), tuple(completed),
            tuple(held), token != draft.registry_token, retained)
