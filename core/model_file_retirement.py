"""Windows exact-file retirement without a close-then-unlink race.

The DELETE-capable handle excludes writers and other deleters for its lifetime.
Its reader is the same handle, not a path reopened with weaker sharing.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import stat

from .model_resource_registry import ModelResourceError, _cancel
from .model_workspace import _path, _pinned_parent
from .training_source_io import _identity


class PinnedModelRetirement:
    def __init__(self, path, handle, reader, expected, expected_links):
        self.path, self.handle, self.reader, self.expected = path, handle, reader, expected
        self.retired = False
        self.expected_links = expected_links

    def verify(self, digest, size, *, should_cancel=None, progress=None, destination=None):
        """Hash (and optionally stage) bytes from the pinned source descriptor."""
        self.reader.seek(0)
        total, hashed = 0, hashlib.sha256()
        while True:
            _cancel(should_cancel)
            chunk = self.reader.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            hashed.update(chunk)
            if destination is not None:
                destination.write(chunk)
            if progress:
                progress(total, size, "Verifying and staging selected model")
        if total != size or hashed.hexdigest() != digest:
            raise ModelResourceError("The selected source bytes changed; preview again.")
        self.check_current()

    def check_current(self):
        if self.retired or self.reader.closed:
            raise ModelResourceError("The selected source handle is no longer active.")
        from .training_windows_file_identity import _from_handle
        current = self.path.stat(follow_symlinks=False)
        _from_handle(self.handle, current)
        if _identity(current) != self.expected or current.st_nlink != self.expected_links:
            raise ModelResourceError("The selected source identity or links changed; preserve it for review.")

    def retire(self):
        """Mark THIS handle's file for deletion and close it before returning."""
        import ctypes
        from ctypes import wintypes
        self.check_current()
        class Disposition(ctypes.Structure):
            _fields_ = [("DeleteFile", ctypes.c_ubyte)]
        function = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
        function.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        function.restype = wintypes.BOOL
        disposition = Disposition(True)
        if not function(self.handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.reader.close()
        self.retired = True


@contextmanager
def pinned_model_retirement(path, expected, *, protected_roots=(), expected_links=1):
    if os.name != "nt":
        raise ModelResourceError("Model relocation requires the qualified Windows file boundary.")
    import msvcrt
    from .training_windows_file_identity import _api, _from_handle
    path = _path(path, protected_roots)
    with _pinned_parent(path.parent, protected_roots):
        before = path.stat(follow_symlinks=False)
        if expected_links not in (1, 2) or not stat.S_ISREG(before.st_mode) or before.st_nlink != expected_links or _identity(before) != tuple(expected):
            raise ModelResourceError("Source changed or has linked copies; choose an unchanged standalone file.")
        if int(getattr(before, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
            raise ModelResourceError("Make this model available locally before relocation.")
        ctypes, _, create, _, close = _api()
        handle = create(str(path), 0x80000000 | 0x10000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ModelResourceError("The model is busy or cannot be moved. Close its engine and preview again.")
        reader = None
        transferred = False
        try:
            _from_handle(handle, before)
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            transferred = True
            reader = os.fdopen(descriptor, "rb")
            pinned = PinnedModelRetirement(path, handle, reader, tuple(expected), expected_links)
            pinned.check_current()
            yield pinned
        finally:
            if reader is not None:
                reader.close()
            elif not transferred:
                close(handle)
