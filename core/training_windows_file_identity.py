"""Full Windows file identities across the app and Python 3.11 trainer.

The app's identities are unchanged. Older Windows Python needs the native API
because its stat result exposes only the legacy volume/file identifier widths.
No identity bits or file-change guards are discarded.
"""
from __future__ import annotations

from functools import lru_cache
import os
import stat
from pathlib import Path
import sys

from .training_source_io import _assert_unredirected, _identity

NEEDS_NATIVE_IDENTITY = os.name == 'nt' and sys.version_info < (3, 12)


@lru_cache(maxsize=1)
def _api():
    import ctypes
    from ctypes import wintypes
    class FileIdInfo(ctypes.Structure):
        _fields_ = [('volume', ctypes.c_ulonglong), ('file', ctypes.c_ubyte * 16)]
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    create = library.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    query = library.GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    close = library.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    return ctypes, FileIdInfo, create, query, close


@lru_cache(maxsize=1)
def _file_attributes():
    import ctypes
    from ctypes import wintypes
    function = ctypes.WinDLL('kernel32', use_last_error=True).GetFileAttributesW
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = wintypes.DWORD
    return function


@lru_cache(maxsize=4096)
def _parent_names(parent):
    # Only lexical names are cached; every guard queries current attributes.
    names = []
    while True:
        names.append(parent)
        ancestor = os.path.dirname(parent)
        if ancestor == parent: return tuple(reversed(names))
        parent = ancestor


def assert_existing_unredirected(path):
    """Check an existing path from its volume inward without statting ancestors."""
    path = Path(path).absolute()
    for component in (*_parent_names(str(path.parent)), str(path)):
        if os.name == 'nt':
            attributes = _file_attributes()(component)
            rejected = attributes == 0xFFFFFFFF or attributes & (0x400 | 0x1000 | 0x40000 | 0x400000)
        else:
            rejected = stat.S_ISLNK(os.stat(component, follow_symlinks=False).st_mode)
        if rejected:
            raise RuntimeError('training_windows_file_identity_path_missing_or_redirected')


def _from_handle(handle, state):
    ctypes, FileIdInfo, _, query, _ = _api()
    info = FileIdInfo()
    if not query(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    volume, file_id = int(info.volume), int.from_bytes(bytes(info.file), 'little')
    # Match the supplied stat observation without ever projecting native IDs
    # down into a persisted identity. Ambiguous old packed file IDs stay held.
    observed = _identity(state)
    if observed[0] not in {volume, volume & 0xffffffff} or observed[1] != file_id:
        raise RuntimeError('training_windows_file_identity_unavailable_or_changed')
    return (volume, file_id, *observed[2:])


def path_identity(path):
    path = Path(path)
    _assert_unredirected(path)
    before = path.stat(follow_symlinks=False)
    return checked_path_identity(path, before)


def checked_path_identity(path, before):
    """Convert a caller's just-checked lstat without repeating its path walk.

    The caller must already reject redirects and validate the selected file type.
    Older Windows interpreters still query the complete native ID and bracket
    that handle observation with the existing final path/stat checks.
    """
    path = Path(path)
    # The leaf can change after the caller's ancestor walk. Reject a newly
    # observed redirect before either the modern or native identity branch.
    if stat.S_ISLNK(before.st_mode) or int(getattr(before, 'st_file_attributes', 0)) & 0x400:
        raise RuntimeError('training_windows_file_identity_redirected')
    if not NEEDS_NATIVE_IDENTITY:
        return _identity(before)
    ctypes, _, create, _, close = _api()
    # Attribute-only, open the reparse point itself, allow ordinary readers and
    # writers. Caller guards still reject redirects; never traverse a link here.
    handle = create(str(path.absolute()), 0x80, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    failed = True
    try:
        identity = _from_handle(handle, before)
        after = path.stat(follow_symlinks=False)
        if _identity(after) != _identity(before):
            raise RuntimeError('training_windows_file_identity_changed')
        assert_existing_unredirected(path)
        failed = False
        return identity
    finally:
        if not close(handle) and not failed:
            raise ctypes.WinError(ctypes.get_last_error())


def descriptor_identity(descriptor):
    before = os.fstat(descriptor)
    if not NEEDS_NATIVE_IDENTITY:
        return _identity(before)
    import msvcrt
    identity = _from_handle(msvcrt.get_osfhandle(descriptor), before)
    if _identity(os.fstat(descriptor)) != _identity(before):
        raise RuntimeError('training_windows_file_identity_changed')
    return identity
