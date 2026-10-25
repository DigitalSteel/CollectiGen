"""Reusable standard-library directory admission; no profile or writes on import."""
from pathlib import Path
import os
import stat


def unredirected_directory(path, *, must_exist=False):
    # Do not resolve first: that would hide junction/symlink ancestor evidence.
    raw = Path(path).expanduser()
    absolute = Path(os.path.abspath(raw))
    # Inspect original components too, before normalizing '..'.
    cursor = Path(raw.anchor) if raw.is_absolute() else Path.cwd()
    parts = raw.parts[1:] if raw.is_absolute() else raw.parts
    for part in parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Test path contains symlink/reparse point")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Test path contains non-directory")
    # Also inspect the cwd/anchor and every normalized ancestor.
    for target in (*reversed(absolute.parents), absolute):
        try:
            info = target.lstat()
        except FileNotFoundError:
            if target == absolute and must_exist:
                raise RuntimeError("Owned test directory does not exist")
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Test path contains symlink/reparse point")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Test path contains non-directory")
    resolved = absolute.resolve(strict=must_exist)
    if resolved != absolute:
        raise RuntimeError("Test path resolves elsewhere")
    return absolute
