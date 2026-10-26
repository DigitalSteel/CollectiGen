"""Bounded in-memory parser-ready handshake; no filesystem or process access."""
from __future__ import annotations

import json
import mmap
import time

MAX_MESSAGE = 1024
SLOT = 4 + MAX_MESSAGE
MAPPING_SIZE = SLOT * 2


class Barrier:
    """Events publish complete shared-memory messages across process boundaries."""
    def __init__(self, name, *, create=False):
        import ctypes as C
        from ctypes import wintypes as W
        import re
        if not re.fullmatch(r"Local\\CollectiGen\.Native\.[0-9a-f]{32}", name):
            raise ValueError("native_mapping_identity")
        self.api = C.WinDLL("kernel32", use_last_error=True)
        for key, args, result in (
            ("CreateEventW", [W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            ("OpenEventW", [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            ("SetEvent", [W.HANDLE], W.BOOL),
            ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            ("CloseHandle", [W.HANDLE], W.BOOL),
        ):
            function = getattr(self.api, key)
            function.argtypes, function.restype = args, result
        self.events, self.mapping = [], None
        try:
            for suffix in (".ready", ".ack"):
                C.set_last_error(0)
                handle = self.api.CreateEventW(None, True, False, name + suffix) if create else self.api.OpenEventW(0x100002, False, name + suffix)
                error = C.get_last_error()
                if not handle:
                    raise OSError("Native barrier event unavailable.")
                self.events.append(handle)
                if create and error == 183:
                    raise ValueError("native_barrier_occupied")
            self.mapping = mmap.mmap(-1, MAPPING_SIZE, tagname=name, access=mmap.ACCESS_WRITE)
            if create and self.mapping[:] != b"\0" * MAPPING_SIZE:
                raise ValueError("native_mapping_occupied")
        except BaseException:
            self.close()
            raise

    def read(self, slot):
        state = self.api.WaitForSingleObject(self.events[slot], 0)
        if state == 258:
            return None
        if state != 0:
            raise OSError("Native barrier state unavailable.")
        value = read_message(self.mapping, slot)
        if value is None:
            raise ValueError("native_message_missing")
        return value

    def write(self, slot, value):
        if self.read(slot) is not None:
            raise ValueError("native_message_repeated")
        write_message(self.mapping, slot, value)
        if not self.api.SetEvent(self.events[slot]):
            raise OSError("Native barrier publication failed.")

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        for handle in self.events:
            self.api.CloseHandle(handle)
        self.events.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def read_message(mapping, slot):
    offset = slot * SLOT
    size = int.from_bytes(mapping[offset:offset + 4], "little")
    if size == 0:
        return None
    if size > MAX_MESSAGE:
        raise ValueError("native_message_size")
    raw = mapping[offset + 4:offset + 4 + size]
    value = json.loads(raw)
    if type(value) is not dict or canonical(value) != raw:
        raise ValueError("native_message_invalid")
    return value


def write_message(mapping, slot, value):
    offset = slot * SLOT
    raw = canonical(value)
    if not 0 < len(raw) <= MAX_MESSAGE or mapping[offset:offset + 4] != b"\0" * 4:
        raise ValueError("native_message_occupied")
    mapping[offset + 4:offset + 4 + len(raw)] = raw
    mapping[offset:offset + 4] = len(raw).to_bytes(4, "little")


def wait_for_observation(name, ready, *, timeout=30):
    import hashlib
    import re
    if name != "Local\\CollectiGen.Native." + ready["nonce"] or not re.fullmatch(r"[0-9a-f]{32}", ready["nonce"]):
        raise ValueError("native_mapping_identity")
    with Barrier(name) as barrier:
        if barrier.read(1) is not None:
            raise ValueError("native_ack_preexists")
        barrier.write(0, ready)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ack = barrier.read(1)
            if ack is not None:
                expected = {"schema_version": 1, "nonce": ready["nonce"],
                    "request_sha256": ready["request_sha256"],
                    "ready_sha256": hashlib.sha256(canonical(ready)).hexdigest()}
                if set(ack) != set(expected) | {"process_fingerprint", "observation_sha256"} or any(
                    type(ack[key]) is not type(value) or ack[key] != value for key, value in expected.items()
                ) or any(type(ack[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", ack[key]) for key in ("process_fingerprint", "observation_sha256")):
                    raise ValueError("native_ack_invalid")
                return ack["observation_sha256"]
            time.sleep(.01)
    raise ValueError("native_ack_timeout")
