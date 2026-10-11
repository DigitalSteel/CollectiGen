"""Explicit standalone profile binding before canonical storage imports.

The original application never binds this override. Bundled public defaults
initialize only a missing defaults directory; existing profile authority stays
with canonical settings, Knowledge synchronization and recovery services.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

_selected_root = None


def empty_profile_stamp(root):
    """Observe an explicitly selected new/empty root without creating it."""
    from .training_source_io import _assert_unredirected
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Select an absolute catalog folder.")
    from .paths import PROGRAM_ROOT
    program = Path(PROGRAM_ROOT)
    nested = program in root.parents
    source_test_zone = (not getattr(sys, "frozen", False) and nested
                        and root.relative_to(program).parts[0] in {".tmp", "logs"})
    if root == program or root in program.parents or (nested and not source_test_zone):
        raise ValueError("The catalog profile must be separate from application files.")
    _assert_unredirected(root)
    if not root.parent.is_dir():
        raise ValueError("Create the catalog's parent folder before connecting.")
    parent = root.parent.stat()
    if root.exists():
        if not root.is_dir() or next(root.iterdir(), None) is not None:
            raise ValueError("Creation requires a new or empty catalog folder.")
        current = root.stat()
        return (parent.st_dev, parent.st_ino, current.st_dev, current.st_ino)
    return (parent.st_dev, parent.st_ino, None, None)


def publish_empty_profile(root, staged_root, *, expected):
    """Publish a complete canonical stage without replacing existing authority.

    A failed publication retains the sibling stage for explicit recovery. This
    does not change the process-global application profile.
    """
    root, stage = Path(root), Path(staged_root)
    from .training_source_io import _assert_unredirected
    _assert_unredirected(stage)
    if stage.parent != root.parent or not (stage / "data").is_dir():
        raise ValueError("Use a complete sibling profile stage.")
    if empty_profile_stamp(root) != expected:
        raise RuntimeError("Catalog folder changed after review; stage retained.")
    if expected[2] is None:
        os.rename(stage, root)
    else:
        os.rename(stage / "data", root / "data")
        stage.rmdir()
    if {p.name for p in root.iterdir()} != {"data"}:
        raise RuntimeError("Catalog folder changed during creation; retain its data and review.")
    return root


def selected_profile_root():
    return _selected_root


def default_organizer_profile():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local/share")
    return Path(base) / "CollectiGen" / "MediaOrganizer"


def bind_profile_root(root, *, program_root):
    global _selected_root
    if "core.paths" in sys.modules or _selected_root is not None:
        raise RuntimeError("Choose the profile before storage services are loaded.")
    from tools.test_profile_isolation import unredirected_directory
    root = unredirected_directory(root)
    program = unredirected_directory(program_root, must_exist=True)
    nested = program in root.parents
    source_test_zone = (not getattr(sys, "frozen", False) and nested
                        and root.relative_to(program).parts[0] in {".tmp", "logs"})
    if root == program or root in program.parents or (nested and not source_test_zone):
        raise ValueError("The profile must be separate from application files.")
    data = unredirected_directory(root / "data")
    defaults = unredirected_directory(data / "defaults")
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(exist_ok=True)
    if not defaults.exists():
        manifest_path = program / "DEFAULT_ASSETS.json"
        if not manifest_path.is_file():
            raise RuntimeError("An existing CollectiGen profile with defaults is required by this source entry.")
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("schema_version") != 1 or not manifest.get("files"):
            raise ValueError("The bundled defaults inventory is invalid.")
        stage = Path(tempfile.mkdtemp(prefix=".defaults-stage-", dir=data))
        names = set()
        for row in manifest["files"]:
            name = row["name"]
            if Path(name).name != name or not name.endswith(".json") or "/" in name or "\\" in name:
                raise ValueError("The bundled defaults inventory contains an invalid path.")
            if name.casefold() in names:
                raise ValueError("The bundled defaults inventory contains duplicate names.")
            names.add(name.casefold())
            source = program / "assets/defaults" / name
            unredirected_directory(source.parent, must_exist=True)
            if source.is_symlink() or getattr(source.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("Bundled defaults cannot be redirected.")
            raw = source.read_bytes()
            if len(raw) != row["size"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("Bundled defaults changed; the incomplete stage is retained.")
            target = stage / name
            target.write_bytes(raw)
            if target.read_bytes() != raw:
                raise OSError("Bundled defaults read-back failed; stage retained.")
        # Windows rename refuses an existing destination, retaining any racing
        # profile owner and the complete staged bytes for explicit recovery.
        os.rename(stage, defaults)
    _selected_root = root
    return root
