"""Owned local decoder child. No Qt, user profile, network, or model runtime."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import struct
import sys


# -I omits the repository from sys.path. Admit only this known code parent.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong),
        ("flags", W.DWORD), ("min_working", C.c_size_t),
        ("max_working", C.c_size_t), ("active_limit", W.DWORD),
        ("affinity", C.c_size_t), ("priority", W.DWORD),
        ("scheduling", W.DWORD)]


class _ExtendedLimits(C.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", C.c_ulonglong * 6),
        ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
        ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


def _bound_memory(maximum: int):
    """Bound native decoder, frames and Python together before loading PyAV."""
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
        return None
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [W.LPVOID, W.LPCWSTR]
    kernel.CreateJobObjectW.restype = W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, W.LPVOID, W.DWORD]
    kernel.SetInformationJobObject.restype = W.BOOL
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.AssignProcessToJobObject.restype = W.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise C.WinError(C.get_last_error())
    limits = _ExtendedLimits()
    limits.basic.flags = 0x100 | 0x2000  # process memory; kill on job close
    limits.process_memory = maximum
    if not kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)):
        raise C.WinError(C.get_last_error())
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise C.WinError(C.get_last_error())
    return job  # Keep handle live until process exit.


def _send(message: dict, rgb: bytes = b"") -> None:
    header = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("!I", len(header)))
    sys.stdout.buffer.write(header)
    if rgb:
        sys.stdout.buffer.write(rgb)
    sys.stdout.buffer.flush()


def _runtime_file_bytes(path, *, maximum=None, check=None):
    """Yield bounded read blocks; parent admission remains cancellable in preflight."""
    total = 0
    with path.open("rb") as stream:
        while True:
            if check is not None:
                check()
            block = stream.read(1024 * 1024)
            if check is not None:
                check()
            if not block:
                return
            total += len(block)
            if maximum is not None and total > maximum:
                raise ValueError("Video inventory exceeds its bound.")
            yield block


def validate_runtime_payload(payload, *, check=None):
    """Recheck explicit admitted software pins, without settings or service imports."""
    import hashlib
    from core.training_source_io import _assert_unredirected
    if check is not None:
        check()
    fields = {"profile_id", "reviewed_profile_sha256", "component_site", "destination_root",
              "inventory_sha256", "interpreter_path", "interpreter_sha256", "python_version", "version"}
    if type(payload) is not dict or set(payload) != fields:
        raise ValueError("Exact Video runtime binding required.")
    if any(type(value) is not str or not value or len(value) > 4096 for value in payload.values()):
        raise ValueError("Bounded Video runtime binding required.")
    for key in ("reviewed_profile_sha256", "inventory_sha256", "interpreter_sha256"):
        value = payload[key]
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Exact Video runtime fingerprint required.")
    site, root, interpreter = (Path(payload[key]) for key in
                               ("component_site", "destination_root", "interpreter_path"))
    for path in (site, root, interpreter):
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Absolute Video runtime paths required.")
        _assert_unredirected(path)
    if site.name != "python" or site.parent != root or not site.is_dir():
        raise ValueError("Video component site differs from its admitted store.")
    if (interpreter.resolve(strict=True) != Path(sys.executable).resolve(strict=True)
            or payload["python_version"] != sys.version.split()[0]):
        raise ValueError("Video interpreter changed before decoder launch.")
    digest = hashlib.sha256()
    for block in _runtime_file_bytes(interpreter, check=check):
        digest.update(block)
    if digest.hexdigest() != payload["interpreter_sha256"]:
        raise ValueError("Video interpreter changed before decoder launch.")
    inventory = root / "installed-components.json"
    _assert_unredirected(inventory)
    if not inventory.is_file() or inventory.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Video inventory is missing or exceeds its bound.")
    raw = b"".join(_runtime_file_bytes(inventory, maximum=16 * 1024 * 1024, check=check))
    if hashlib.sha256(raw).hexdigest() != payload["inventory_sha256"]:
        raise ValueError("Video component inventory changed before decoder launch.")
    value = json.loads(raw)
    if (value.get("schema_version") != 2 or value.get("storage_layout") != "durable-optional-python-v1"
            or value.get("python_version") != payload["python_version"]
            or payload["profile_id"] != "organizer-video-windows" or payload["version"] != "18.1.0"
            or payload["profile_id"] not in value.get("profile_ids", [])):
        raise ValueError("Video component profile binding differs.")
    profiles = [row for row in value.get("profiles", [])
                if type(row) is dict and row.get("id") == payload["profile_id"]]
    if len(profiles) != 1 or profiles[0].get("sha256") != payload["reviewed_profile_sha256"]:
        raise ValueError("Video reviewed profile changed before decoder launch.")
    return site


def _component_runtime():
    if not sys.argv[1:]:
        return None
    if len(sys.argv) != 3 or sys.argv[1] != "--runtime-binding" or len(sys.argv[2]) > 16384:
        raise ValueError("Exact admitted decoder arguments required.")
    payload = json.loads(sys.argv[2])
    site = validate_runtime_payload(payload)
    sys.path.insert(0, str(site))
    return site, payload


def main() -> int:
    try:
        raw = sys.stdin.buffer.readline(16 * 1024 * 1024 + 1)
        if not raw.endswith(b"\n") or len(raw) > 16 * 1024 * 1024:
            raise ValueError("Decoder request exceeds its bound.")
        request = json.loads(raw)
        # Keep aggregate PyAV allocations and RGB copies below this owned cap.
        memory_job = _bound_memory(768 * 1024 * 1024)
        component = _component_runtime()
        from core.video_decoder import iter_video_preview_frames, verify_video_output
        from core.video_generation import VideoGenerationRequest, VideoSourceAsset
        import av
        if component is not None:
            from importlib.machinery import EXTENSION_SUFFIXES
            from av import _core
            site, binding = component
            if (av.__version__ != binding["version"] or not av.library_versions
                    or any(not Path(module.__file__).resolve(strict=True).is_relative_to(site)
                           for module in (av, _core))
                    or not any(str(_core.__file__).endswith(suffix) for suffix in EXTENSION_SUFFIXES)):
                raise ValueError("Video runtime origin/version differs from admitted component.")
        common = dict(allowed_root=request["allowed_root"], av_module=av,
            max_bytes=request["max_bytes"], max_frame_bytes=request["max_frame_bytes"],
            max_seconds=request["max_seconds"])
        if request["operation"] == "verify":
            expected = VideoGenerationRequest.from_dict(request["expected_request"])
            result = verify_video_output(request["path"], expected,
                privacy_disposition=request["privacy_disposition"], **common)
            if component is not None:
                result.decoder_evidence["component_inventory_sha256"] = binding["inventory_sha256"]
            _send({"kind": "result", "asset": result.asset.to_dict(),
                "decoder_evidence": result.decoder_evidence})
        elif request["operation"] == "preview":
            asset = VideoSourceAsset.from_dict(request["asset"])
            for frame in iter_video_preview_frames(request["path"], asset,
                    start_pts=request["start_pts"], **common):
                _send({"kind": "frame", "pts": frame.pts, "width": frame.width,
                    "height": frame.height, "rgb_length": len(frame.rgb)}, frame.rgb)
        else:
            raise ValueError("Unknown decoder operation.")
        _send({"kind": "end"})
        del memory_job
        return 0
    except BaseException as exc:
        try:
            _send({"kind": "error", "message": str(exc)[:2000]})
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
