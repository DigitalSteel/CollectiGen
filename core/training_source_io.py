from __future__ import annotations

import hashlib
import os
import stat as stat_module
from pathlib import Path


def _redirected(path: Path) -> bool:
    try:
        state=path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat_module.S_ISLNK(state.st_mode) or bool(
        int(getattr(state,"st_file_attributes",0))&0x400
    )


def _assert_unredirected(path: Path) -> None:
    current=path.absolute()
    while True:
        # One non-following snapshot supplies existence, link type and Windows
        # reparse attributes. Repeat the full walk at each read boundary; never
        # cache an ancestor decision across source reads or publication phases.
        if _redirected(current):
            raise RuntimeError(
                f"{path.name} cannot be read through a redirected source path."
            )
        if current.parent==current:
            return
        current=current.parent


def _identity(state) -> tuple[int,int,int,int,int]:
    return (
        int(getattr(state,"st_dev",0)),int(getattr(state,"st_ino",0)),
        int(getattr(state,"st_size",0)),int(getattr(state,"st_mtime_ns",0)),
        int(getattr(state,"st_file_attributes",0)),
    )


def read_stable_training_source(path: str|Path, *, max_bytes: int|None=None) -> tuple[bytes,dict[str,object]]:
    """Read a stable regular source, optionally bounded even if it grows.

    A bounded read consumes at most max_bytes plus one overflow-detection byte.
    The default preserves existing callers' unrestricted read behavior.
    """
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError("The source byte limit must be a positive integer.")
    source=Path(path).absolute()
    _assert_unredirected(source)
    before_path=source.stat(follow_symlinks=False)
    if not stat_module.S_ISREG(before_path.st_mode):
        raise RuntimeError(f"{source.name} is not a regular source image file.")
    if max_bytes is not None and before_path.st_size > max_bytes:
        raise ValueError("The source exceeds the requested byte limit.")
    flags=os.O_RDONLY
    flags|=int(getattr(os,"O_BINARY",0))
    flags|=int(getattr(os,"O_NOFOLLOW",0))
    descriptor=os.open(source,flags)
    try:
        opened=os.fstat(descriptor)
        current_path=source.stat(follow_symlinks=False)
        _assert_unredirected(source)
        if _identity(opened)!=_identity(current_path):
            raise RuntimeError(
                f"{source.name} changed before its source bytes could be read."
            )
        if max_bytes is not None and opened.st_size > max_bytes:
            raise ValueError("The source exceeds the requested byte limit.")
        blocks=[]
        total=0
        while True:
            size=1024*1024 if max_bytes is None else min(1024*1024,max_bytes-total+1)
            block=os.read(descriptor,size)
            if not block:
                break
            total+=len(block)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("The source exceeds the requested byte limit.")
            blocks.append(block)
        after_open=os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path=source.stat(follow_symlinks=False)
    _assert_unredirected(source)
    raw=b"".join(blocks)
    if (
        _identity(opened)!=_identity(after_open)
        or _identity(after_open)!=_identity(after_path)
        or len(raw)!=after_path.st_size
    ):
        raise RuntimeError(f"{source.name} changed while its source bytes were read.")
    return raw,{
        "size":after_path.st_size,"mtime_ns":after_path.st_mtime_ns,
        "sha256":hashlib.sha256(raw).hexdigest(),
    }
