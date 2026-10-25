"""Baseline guard plus exactly one hash-pinned SDK extension origin."""
from pathlib import Path
from hashlib import sha256
from windows_build_isolation import validate_binary_origins

def validate_with_sdk(binaries, sdk_extension, expected_sha256):
    extension = Path(sdk_extension)
    if (not extension.is_file() or extension.is_symlink() or
            getattr(extension.lstat(), "st_file_attributes", 0) & 0x400 or
            sha256(extension.read_bytes()).hexdigest() != expected_sha256):
        raise ValueError("SDK extension drift")
    remaining = []
    seen = False
    for destination, source, kind in binaries:
        path = Path(source)
        if path.resolve() == extension.resolve():
            if kind not in ("BINARY", "EXTENSION") or destination.replace("\\", "/") != "velopack/velopack.pyd":
                raise ValueError("SDK extension destination/kind")
            seen = True
        else:
            remaining.append((destination, source, kind))
    if not seen:
        raise ValueError("SDK extension missing from collected binaries")
    validate_binary_origins(remaining)
