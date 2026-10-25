"""Bounded, read-only source observations for a selected SwarmUI checkout.

These observations describe local Git candidates, not activated extensions,
running code, or local modifications. No Git command or working-tree walk runs.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Any


_REVISION = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_REF_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\Z")
_MAX_CANDIDATES = 100
_MAX_SCAN = 1000
_REPARSE = 0x400


def _ordinary_directory(path: Path) -> bool:
    """Reject redirects anywhere from the selected path back to its anchor."""
    try:
        for part in (*reversed(path.parents), path):
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                return False
        return True
    except OSError:
        return False


def _read_ordinary_file(path: Path, limit: int) -> bytes | None:
    if not _ordinary_directory(path.parent):
        return None
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, "st_file_attributes", 0) & _REPARSE
                or before.st_size > limit):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_dev != before.st_dev or opened.st_ino != before.st_ino
                    or getattr(opened, "st_file_attributes", 0) & _REPARSE):
                return None
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            current = path.lstat()
            identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(opened, key) != getattr(after, key) or getattr(before, key) != getattr(current, key)
                   for key in identity):
                return None
            return data if len(data) <= limit else None
        finally:
            os.close(descriptor)
    except OSError:
        return None


def _line(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        value = data.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        return ""
    return value if value and "\r" not in value and "\n" not in value else ""


def _valid_ref(value: str) -> bool:
    parts = value.split("/")
    return (len(parts) >= 3 and parts[0] == "refs"
            and len(value) <= 512 and all(
                part not in {".", ".."} and not part.endswith((".", ".lock"))
                and _REF_PART.fullmatch(part) for part in parts
            ))


def read_git_revision(root: Path) -> str:
    """Read only a checkout's own ordinary HEAD, loose ref or packed ref."""
    root = Path(root).absolute()
    git_dir = root / ".git"
    if not _ordinary_directory(git_dir):
        return ""
    head = _line(_read_ordinary_file(git_dir / "HEAD", 512))
    if _REVISION.fullmatch(head):
        return head.lower()
    if not head.startswith("ref: "):
        return ""
    ref = head[5:]
    if not _valid_ref(ref):
        return ""
    loose = git_dir.joinpath(*ref.split("/"))
    # A packed ref need not have any loose parent directories. Inspect each
    # existing component without following redirects, and stop at absence.
    parent = git_dir
    missing_parent = False
    for part in ref.split("/")[:-1]:
        parent /= part
        try:
            info = parent.lstat()
        except FileNotFoundError:
            missing_parent = True
            break
        except OSError:
            return ""
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
            return ""
    try:
        if not missing_parent:
            loose.lstat()
        has_loose = not missing_parent
    except FileNotFoundError:
        has_loose = False
    except OSError:
        return ""
    if has_loose:
        value = _line(_read_ordinary_file(loose, 256))
        return value.lower() if _REVISION.fullmatch(value) else ""
    packed = _read_ordinary_file(git_dir / "packed-refs", 1_048_576)
    if packed is None:
        return ""
    try:
        rows = packed.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return ""
    found = ""
    for row in rows:
        if not row or row.startswith("#"):
            continue
        if row.startswith("^"):
            if not _REVISION.fullmatch(row[1:]):
                return ""
            continue
        candidate, separator, name = row.partition(" ")
        if not separator or not _REVISION.fullmatch(candidate) or not _valid_ref(name):
            return ""
        if name == ref:
            if found:
                return ""
            found = candidate.lower()
    return found


def _candidate_name(name: str) -> bool:
    return (0 < len(name) <= 256 and name not in {".", ".."}
            and name.isprintable() and not any(char in "/\\:" for char in name))


def capture_source_inventory(root: Path) -> dict[str, Any]:
    """Observe at most 100 immediate folders, scanning at most 1000 entries."""
    root = Path(root).absolute()
    revision = read_git_revision(root)
    directory = root / "src" / "Extensions"
    inventory: dict[str, Any] = {
        "schema_version": 1, "status": "unavailable", "root_revision": revision,
        "extensions": [],
    }
    if not _ordinary_directory(directory):
        return inventory
    paths: list[Path] = []
    status = "complete"
    try:
        for scanned, entry in enumerate(directory.iterdir(), 1):
            if scanned > _MAX_SCAN:
                status = "partial"
                break
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                status = "partial"
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            if not _candidate_name(entry.name):
                status = "partial"
                continue
            paths.append(entry)
    except OSError:
        status = "partial"
    if len(paths) > _MAX_CANDIDATES:
        status = "partial"
    candidates = []
    for entry in sorted(paths, key=lambda path: path.name)[:_MAX_CANDIDATES]:
        candidate_revision = read_git_revision(entry)
        candidates.append({
            "name": entry.name, "revision": candidate_revision,
            "status": "available" if candidate_revision else "unavailable",
        })
    inventory["status"] = status
    inventory["extensions"] = candidates
    return inventory


def _validated(inventory: Any) -> dict[str, Any] | None:
    if (not isinstance(inventory, dict) or type(inventory.get("schema_version")) is not int
            or inventory["schema_version"] != 1):
        return None
    if not isinstance(inventory.get("status"), str) or inventory["status"] not in {"complete", "partial", "unavailable"}:
        return None
    root = inventory.get("root_revision")
    rows = inventory.get("extensions")
    if not isinstance(root, str) or (root and not _REVISION.fullmatch(root)) or not isinstance(rows, list) or len(rows) > _MAX_CANDIDATES:
        return None
    if inventory["status"] == "unavailable" and rows:
        return None
    seen: set[str] = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("status"), str)
                or row["status"] not in {"available", "unavailable"}):
            return None
        name, revision = row.get("name"), row.get("revision")
        if (not isinstance(name, str) or not _candidate_name(name) or name in seen
                or not isinstance(revision, str) or (revision and not _REVISION.fullmatch(revision))
                or (row["status"] == "available") != bool(revision)):
            return None
        seen.add(name)
    return inventory


def format_source_inventory(inventory: Any) -> str:
    """Render only validated, bounded source observations; never paths or content."""
    value = _validated(inventory)
    if value is None:
        return "Source candidate baseline: unavailable (not saved or invalid)."
    lines = [
        f"Source candidate scan: {value['status']}.",
        f"Root Git revision: {value['root_revision'] or 'unavailable'}.",
        "Extension candidates (immediate folders, up to 100; activation and local changes unverified):",
    ]
    lines.extend(
        f"- {row['name']}: {row['revision'] or 'revision unavailable'}"
        for row in value["extensions"]
    )
    if not value["extensions"]:
        lines.append("- none observed" if value["status"] == "complete" else "- unavailable")
    return "\n".join(lines)


def compare_source_inventory(saved: Any, current: Any) -> str:
    """Compare observations, suppressing absence claims from incomplete scans."""
    before, after = _validated(saved), _validated(current)
    if before is None:
        return "Saved source candidate baseline: unavailable (inspect and use the existing installation to establish one)."
    if after is None:
        return "Current source candidate inventory: unavailable."
    lines = [
        f"Saved source candidate scan: {before['status']}.",
        f"Current source candidate scan: {after['status']}.",
    ]
    if before["root_revision"] and after["root_revision"]:
        state = "unchanged" if before["root_revision"] == after["root_revision"] else "revision changed"
        lines.append(f"Root Git revision: {state} ({before['root_revision']} -> {after['root_revision']}).")
    else:
        lines.append("Root Git revision comparison: unavailable.")
    if before["status"] != "complete" or after["status"] != "complete":
        lines.append("Extension candidate comparison: unavailable (a scan was incomplete; absence is unverified).")
        return "\n".join(lines)
    old = {row["name"]: row for row in before["extensions"]}
    new = {row["name"]: row for row in after["extensions"]}
    lines.append("Extension candidate comparison (activation and local changes unverified):")
    for name in sorted(old.keys() | new.keys()):
        if name not in old:
            state = "added"
        elif name not in new:
            state = "removed"
        elif old[name]["status"] != "available" or new[name]["status"] != "available":
            state = "revision unavailable"
        else:
            state = "unchanged" if old[name]["revision"] == new[name]["revision"] else "revision changed"
        if state == "revision changed":
            state += f" ({old[name]['revision']} -> {new[name]['revision']})"
        lines.append(f"- {name}: {state}.")
    if not old and not new:
        lines.append("- none observed.")
    return "\n".join(lines)
