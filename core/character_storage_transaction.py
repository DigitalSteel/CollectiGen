from __future__ import annotations

import json
import os
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4


CharacterPayloads = Mapping[Path, dict[str, Any]]
FileSnapshot = Mapping[Path, bytes | None]
WriteBytes = Callable[[Path, bytes], None]
WriteJson = Callable[[Path, dict[str, Any]], None]
_THREAD_LOCKS = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def snapshot(paths) -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.exists() else None
        for path in paths
    }


@contextmanager
def character_transaction_lock(directory: str | Path):
    """Hold one cross-process, thread-reentrant Character Knowledge lock."""

    data_root = Path(directory).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    key = str(data_root)
    states = getattr(_THREAD_LOCKS, "states", None)
    if states is None:
        states = {}
        _THREAD_LOCKS.states = states
    state = states.get(key)
    if state is not None:
        state[1] += 1
        try:
            yield
        finally:
            state[1] -= 1
        return

    lock_path = data_root / ".character-knowledge.lock"
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            "Character Knowledge is being saved or recovered by another "
            "application task. Retry after that operation finishes."
        ) from exc
    states[key] = [handle, 1]
    try:
        yield
    finally:
        states.pop(key, None)
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def commit_character_payloads(
    data_dir: str | Path,
    payloads: CharacterPayloads,
    *,
    expected_files: FileSnapshot,
    operation: str,
    kind: str,
    record_id: str,
    verify: Callable[[], None],
    stale_message: str,
    backup_kind: str = "character-record-edit",
    write_json: WriteJson = atomic_json,
    write_bytes: WriteBytes = atomic_bytes,
) -> Path:
    with character_transaction_lock(data_dir):
        return _commit_character_payloads_locked(
            data_dir,
            payloads,
            expected_files=expected_files,
            operation=operation,
            kind=kind,
            record_id=record_id,
            verify=verify,
            stale_message=stale_message,
            backup_kind=backup_kind,
            write_json=write_json,
            write_bytes=write_bytes,
        )


def _commit_character_payloads_locked(
    data_dir: str | Path,
    payloads: CharacterPayloads,
    *,
    expected_files: FileSnapshot,
    operation: str,
    kind: str,
    record_id: str,
    verify: Callable[[], None],
    stale_message: str,
    backup_kind: str,
    write_json: WriteJson,
    write_bytes: WriteBytes,
) -> Path:
    """Publish one recoverable Character Knowledge transaction.

    The expected snapshot may include read-only companion authorities, such as
    canonical relationships, that influence the staged payload without being a
    write target. Every mutable target is backed up before the pending manifest
    becomes durable, and any failure restores the complete prior revision.
    """

    if snapshot(expected_files) != dict(expected_files):
        raise RuntimeError(stale_message)
    if not payloads:
        return Path()
    targets = tuple(sorted(payloads, key=lambda value: value.name))
    if any(path not in expected_files for path in targets):
        raise RuntimeError(
            "Character Knowledge transaction is missing a loaded target snapshot."
        )
    data_root = Path(data_dir).resolve()
    allowed_names = {
        "series.json", "characters.json", "outfits.json", "relationships.json",
    }
    if any(path.resolve().parent != data_root or path.name not in allowed_names for path in targets):
        raise RuntimeError("Character Knowledge transaction contains an unsafe target.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if backup_kind not in {"character-record-edit", "character-database-save"}:
        raise RuntimeError("Character Knowledge transaction has an unsupported recovery kind.")
    backup = data_root / "backups" / backup_kind / f"{stamp}_{uuid4().hex[:8]}"
    backup.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    for index, path in enumerate(targets):
        backup_name = f"{index}_{path.name}"
        current = expected_files[path]
        if current is not None:
            saved = backup / backup_name
            saved.write_bytes(current)
            try:
                shutil.copystat(path, saved)
            except OSError:
                pass
        entries.append({
            "target": str(path.resolve()),
            "backup": backup_name,
            "existed": current is not None,
        })
    manifest_path = backup / "recovery.json"
    manifest: dict[str, Any] = {
        "status": "pending",
        "created_at": now(),
        "kind": kind,
        "record_id": record_id,
        "operation": operation,
        "files": entries,
    }
    write_json(manifest_path, manifest)
    if snapshot(expected_files) != dict(expected_files):
        manifest["status"] = "stale"
        manifest["stale_at"] = now()
        write_json(manifest_path, manifest)
        raise RuntimeError(stale_message)
    try:
        for path in targets:
            write_json(path, payloads[path])
        verify()
    except BaseException:
        for entry in entries:
            target = Path(entry["target"])
            saved = backup / entry["backup"]
            if entry["existed"]:
                write_bytes(target, saved.read_bytes())
            elif target.exists():
                target.unlink()
        manifest["status"] = "restored"
        manifest["restored_at"] = now()
        write_json(manifest_path, manifest)
        raise
    manifest["status"] = "complete"
    manifest["completed_at"] = now()
    write_json(manifest_path, manifest)
    return backup


def recover_interrupted_character_transactions(
    directory: str | Path,
    *,
    write_json: WriteJson = atomic_json,
    write_bytes: WriteBytes = atomic_bytes,
) -> list[Path]:
    with character_transaction_lock(directory):
        return _recover_interrupted_character_transactions_locked(
            directory, write_json=write_json, write_bytes=write_bytes,
        )


def _recover_interrupted_character_transactions_locked(
    directory: str | Path,
    *,
    write_json: WriteJson,
    write_bytes: WriteBytes,
) -> list[Path]:
    data_root = Path(directory).resolve()
    restored: list[Path] = []
    manifest_paths = sorted(
        manifest_path
        for backup_kind in (
            "character-record-edit",
            "character-database-save",
            "character-tag-bridge",
        )
        for manifest_path in (data_root / "backups" / backup_kind).glob("*/recovery.json")
    )
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "pending":
            continue
        entries = manifest.get("files")
        if not isinstance(entries, list):
            entries = [manifest.get("file") or {}]
        resolved: list[tuple[dict[str, Any], Path, Path]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    "Character record recovery manifest contains an invalid file entry."
                )
            target = Path(str(entry.get("target") or "")).resolve()
            backup = (manifest_path.parent / str(entry.get("backup") or "")).resolve()
            if target.parent != data_root or target.name not in {
                "series.json", "characters.json", "outfits.json", "relationships.json",
            }:
                raise RuntimeError(
                    "Character record recovery manifest contains an unsafe target."
                )
            if backup.parent != manifest_path.parent.resolve():
                raise RuntimeError(
                    "Character record recovery manifest contains an unsafe backup path."
                )
            resolved.append((entry, target, backup))
        for entry, target, backup in resolved:
            if entry.get("existed"):
                write_bytes(target, backup.read_bytes())
            elif target.exists():
                target.unlink()
        manifest["status"] = "restored"
        manifest["restored_at"] = now()
        write_json(manifest_path, manifest)
        restored.append(manifest_path.parent)
    return restored
