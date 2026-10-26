"""Shared standard-library diagnostic I/O, available before application imports."""
from __future__ import annotations

import hashlib
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Iterable
import zipfile


class SafeRotatingFileHandler(RotatingFileHandler):
    """Keep diagnostics I/O failures out of application control flow."""

    failed = False

    def handleError(self, record):
        self.failed = True


def rotating_handler(path: Path, *, max_bytes: int, backups: int):
    """Return a fail-safe handler, or None when the destination is unavailable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return SafeRotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8'
        )
    except (OSError, ValueError):
        return None


def write_verified_zip(output: Path, files: Iterable[tuple[str, bytes]]) -> dict[str, str]:
    """Write a deterministic-path ZIP and verify every member after replacement.

    Moved from core.support_bundle without changing its existing API or behavior.
    """
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    normalized: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for raw_name, raw_payload in files:
        name = str(raw_name).replace('\\', '/').strip('/')
        parts = Path(name).parts
        if (not name or name in seen or Path(name).is_absolute() or '..' in parts
                or any(part in {'', '.'} for part in parts)):
            raise ValueError(f'Unsafe or duplicate ZIP member: {raw_name}')
        payload = bytes(raw_payload)
        normalized.append((name, payload))
        seen.add(name)
    temporary = destination.with_name(f'.{destination.name}.{os.getpid()}.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(normalized):
                archive.writestr(name, payload)
        with zipfile.ZipFile(temporary, 'r') as archive:
            names = archive.namelist()
            if sorted(names) != sorted(seen):
                raise OSError('Diagnostic ZIP read-back did not match the requested files.')
            for name, payload in normalized:
                if archive.read(name) != payload:
                    raise OSError(f'Diagnostic ZIP read-back failed for {name}.')
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(normalized)}
