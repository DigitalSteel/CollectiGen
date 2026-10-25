"""Small, app-owned preferences for the explicit local video runtime setup."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .external_write_transaction import atomic_verified_bytes
from .training_source_io import _assert_unredirected, _identity


SCHEMA_ID = "collectigen.video-runtime-setup.v1"
MAX_BYTES = 16 * 1024
FIELDS = {"source_root", "python_executable", "python_runtime_root", "workspace_root", "port", "free_gib", "transformer_role"}


def _path(storage_root: str | Path) -> Path:
    root = Path(storage_root).absolute()
    _assert_unredirected(root)
    return root / "local-video-runtime.json"


def _validate(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("Video setup has missing or unsupported fields.")
    for key in FIELDS - {"port", "free_gib", "transformer_role"}:
        if not isinstance(value[key], str) or len(value[key]) > 2048 or "\x00" in value[key]:
            raise ValueError(f"Video setup {key} is invalid.")
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("Video setup port is invalid.")
    if type(value["free_gib"]) is not int or not 1 <= value["free_gib"] <= 1024:
        raise ValueError("Video setup free-memory threshold is invalid.")
    if value["transformer_role"] not in {"unet", "checkpoint"}:
        raise ValueError("Video setup transformer role is invalid.")
    return dict(value)


def _bounded_raw(target: Path) -> bytes | None:
    _assert_unredirected(target)
    try:
        before = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Saved video setup is not a regular file.")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if opened.st_size > MAX_BYTES:
            raise ValueError("Saved video setup exceeds the supported size.")
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            block = os.read(descriptor, MAX_BYTES + 1 - len(raw))
            if not block:
                break
            raw.extend(block)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _assert_unredirected(target)
    after_path = target.stat(follow_symlinks=False)
    if len(raw) > MAX_BYTES:
        raise ValueError("Saved video setup exceeds the supported size.")
    if (_identity(before) != _identity(opened) or _identity(opened) != _identity(after_open)
            or _identity(after_open) != _identity(after_path) or len(raw) != after_path.st_size):
        raise RuntimeError("Saved video setup changed while it was read.")
    return bytes(raw)


def load_video_runtime_preferences(storage_root: str | Path) -> tuple[dict | None, str | None]:
    """Load only this exact owned file; unknown schemas remain untouched."""
    raw = _bounded_raw(_path(storage_root))
    if raw is None:
        return None, None
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {"schema_id", "settings"} or payload["schema_id"] != SCHEMA_ID:
        raise ValueError("Saved video setup has an unsupported schema.")
    return _validate(payload["settings"]), sha256(raw).hexdigest()


def save_video_runtime_preferences(storage_root: str | Path, settings: dict,
                                   *, expected_sha256: str | None) -> str:
    """Save a reviewed setup with stale protection and a verified recovery copy."""
    target = _path(storage_root)
    settings = _validate(settings)
    raw = (json.dumps({"schema_id": SCHEMA_ID, "settings": settings}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Saved video setup exceeds the supported size.")
    current, digest = load_video_runtime_preferences(storage_root)
    if digest != expected_sha256:
        raise RuntimeError("Saved video setup changed elsewhere. Reload it before saving.")
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_unredirected(target)
    if current is not None:
        existing = _bounded_raw(target)
        if existing is None or sha256(existing).hexdigest() != digest:
            raise RuntimeError("Saved video setup changed before its recovery copy.")
        backup = target.with_suffix(".previous.json")
        _assert_unredirected(backup)
        atomic_verified_bytes(backup, existing,
                              label="Video setup recovery copy")
        _assert_unredirected(target)
        atomic_verified_bytes(target, raw, label="Video setup")
    else:
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            _assert_unredirected(target)
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target); temporary.unlink()
            verified = _bounded_raw(target)
            if verified != raw:
                raise RuntimeError("Saved video setup failed read-back verification.")
        finally:
            if temporary.exists():
                temporary.unlink()
    return sha256(raw).hexdigest()
