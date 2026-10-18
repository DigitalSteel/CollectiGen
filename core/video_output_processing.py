"""Faithful, bounded and isolated resize of one already verified silent MP4."""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import stat
import subprocess
import sys
from threading import Thread
from time import monotonic
from typing import Callable
from uuid import uuid4

from .training_source_io import _assert_unredirected
from .video_decoder import (
    MAX_FRAME_BYTES, MAX_VIDEO_BYTES, VideoVerification,
    _contained_regular, _hash_descriptor, verify_video_output,
)
from .video_generation import MAX_PIXELS, MAX_SIDE, VideoGenerationRequest, VideoSourceAsset
from .video_temporal import DerivedClipMapping, FrameCorrespondence


class VideoOutputProcessingError(ValueError):
    """A faithful derivative could not be proven or safely published."""


@dataclass(frozen=True)
class VideoResizeResult:
    output_path: Path
    receipt_path: Path
    verification: VideoVerification
    source_sha256: str
    output_sha256: str
    recipe: dict[str, object]
    lineage: DerivedClipMapping


RECEIPT_SCHEMA = "collectigen.video-faithful-resize.v1"
MAX_RECEIPT_BYTES = 128 * 1024
FREE_SPACE_RESERVE_BYTES = 512 * 1024 * 1024


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _recipe(source_request: VideoGenerationRequest, source_asset: VideoSourceAsset,
            width: int, height: int) -> dict[str, object]:
    scale = min(Fraction(width, source_request.width),
                Fraction(height, source_request.height))
    fitted_width = max(1, int(source_request.width * scale))
    fitted_height = max(1, int(source_request.height * scale))
    return {
        "operation": "faithful_resize", "aspect_policy": "fit_center_black_pad",
        "interpolation": "Pillow LANCZOS", "width": width, "height": height,
        "fitted_width": fitted_width, "fitted_height": fitted_height,
        "pad_left": (width - fitted_width) // 2,
        "pad_top": (height - fitted_height) // 2, "pad_color": "black",
        "codec": "libx264", "pixel_format": "yuv420p", "crf": 18,
        "preset": "ultrafast", "audio": "none", "timeline": "preserve_each_observed_pts",
        "source_sha256": source_asset.sha256,
        "source_request_fingerprint": hashlib.sha256(_canonical(source_request.to_dict())).hexdigest(),
    }


def _remaining(deadline: float) -> float:
    value = deadline - monotonic()
    if value <= 0:
        raise VideoOutputProcessingError("Video resize exceeded its time limit.")
    return value


def _hash_source(path: Path, asset: VideoSourceAsset, cancel, deadline: float) -> None:
    remaining = _remaining(deadline)
    source, descriptor, opened = _contained_regular(path, path.parent, MAX_VIDEO_BYTES)
    try:
        if opened.st_size != asset.byte_length or _hash_descriptor(
            descriptor, cancel, monotonic(), remaining,
        ) != asset.sha256:
            raise VideoOutputProcessingError("The reviewed video source changed.")
        _assert_unredirected(source)
        if os.fstat(descriptor).st_ino != source.stat(follow_symlinks=False).st_ino:
            raise VideoOutputProcessingError("The reviewed video source changed.")
    finally:
        os.close(descriptor)


def _launch_worker():
    script = Path(__file__).with_name("video_output_processing_worker.py")
    return subprocess.Popen([sys.executable, "-I", "-u", str(script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _encode_child(spec: dict, *, should_cancel, deadline: float) -> dict:
    try:
        child = _launch_worker()
    except OSError as exc:
        raise VideoOutputProcessingError("The isolated video encoder could not start.") from exc
    records: Queue = Queue(maxsize=2)
    reader = writer = None
    try:
        assert child.stdin is not None and child.stdout is not None
        payload = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(payload) > 8 * 1024 * 1024:
            raise VideoOutputProcessingError("Video resize request exceeds its bound.")

        def write_request():
            try:
                child.stdin.write(payload)
                child.stdin.close()
            except (OSError, ValueError) as exc:
                records.put(("error", str(exc)[:1000]))

        def read_response():
            try:
                line = child.stdout.readline(16 * 1024 + 1)
                if not line.endswith(b"\n") or len(line) > 16 * 1024:
                    raise VideoOutputProcessingError("The encoder returned no bounded result.")
                records.put(("result", json.loads(line)))
            except BaseException as exc:
                records.put(("error", str(exc)[:1000]))

        writer = Thread(target=write_request, daemon=True)
        reader = Thread(target=read_response, daemon=True)
        writer.start(); reader.start()
        while True:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Video resize cancelled safely.")
            if monotonic() >= deadline:
                raise VideoOutputProcessingError("Video resize exceeded its time limit.")
            try:
                kind, value = records.get(timeout=.05)
            except Empty:
                continue
            if kind == "error" or not isinstance(value, dict) or value.get("status") != "ok":
                message = value.get("error", "") if isinstance(value, dict) else value
                raise VideoOutputProcessingError("Isolated video encoding failed: " + str(message)[:1000])
            return value
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=1)
        if child.stdout is not None:
            child.stdout.close()
        if reader is not None:
            reader.join(timeout=.5)
        if writer is not None:
            writer.join(timeout=.5)


def _lineage_result(target: Path, receipt: Path, source: VideoSourceAsset,
                    verification: VideoVerification, recipe: dict) -> VideoResizeResult:
    before, after = source.timing, verification.asset.timing
    if (before.frame_count != after.frame_count or before.duration != after.duration
            or tuple(before.seconds_at(pts) for pts in before.frame_pts)
            != tuple(after.seconds_at(pts) for pts in after.frame_pts)):
        raise VideoOutputProcessingError("The resized clip changed its observed presentation timeline.")
    transform = hashlib.sha256(_canonical(recipe)).hexdigest()
    lineage = DerivedClipMapping(source, verification.asset,
        before.start_pts, before.end_pts, after.start_pts, after.end_pts,
        str(verification.decoder_evidence.get("decoder", "PyAV")), transform,
        tuple(FrameCorrespondence(out_pts, (src_pts,)) for src_pts, out_pts in
              zip(before.frame_pts, after.frame_pts)), {"recipe": recipe})
    return VideoResizeResult(target, receipt, verification, source.sha256,
        verification.asset.sha256, recipe, lineage)


def _receipt_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.collectigen-resize.json")


def _receipt_payload(target: Path, stage: Path, stage_id: int,
                     source: VideoSourceAsset, verification: VideoVerification,
                     recipe: dict) -> dict:
    payload = {"schema_id": RECEIPT_SCHEMA, "destination_name": target.name,
        "stage_name": stage.name, "stage_id": stage_id,
        "source_sha256": source.sha256, "source_byte_length": source.byte_length,
        "source_stream_id": source.stream_id, "source_timing": source.timing.to_dict(),
        "source_request_fingerprint": recipe["source_request_fingerprint"],
        "recipe": recipe, "recipe_fingerprint": hashlib.sha256(_canonical(recipe)).hexdigest(),
        "output_sha256": verification.asset.sha256,
        "output_byte_length": verification.asset.byte_length,
        "output_stream_id": verification.asset.stream_id,
        "output_timing": verification.asset.timing.to_dict()}
    payload["receipt_fingerprint"] = hashlib.sha256(_canonical(payload)).hexdigest()
    return payload


def _write_receipt(path: Path, payload: dict) -> int:
    raw = _canonical(payload)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise VideoOutputProcessingError("The resize receipt exceeds its bound.")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags, 0o600)
    identity = os.fstat(descriptor).st_ino
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw); file.flush(); os.fsync(file.fileno())
    except BaseException:
        if os.path.lexists(path) and path.stat(follow_symlinks=False).st_ino == identity:
            path.unlink()
        raise
    return identity


def _read_receipt(path: Path, target: Path, source: VideoSourceAsset,
                  recipe: dict) -> tuple[dict, os.stat_result]:
    _assert_unredirected(path)
    state = path.stat(follow_symlinks=False)
    if not path.is_file() or state.st_size > MAX_RECEIPT_BYTES:
        raise VideoOutputProcessingError("The exact resize receipt is unavailable.")
    raw = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if ((state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise VideoOutputProcessingError("The resize receipt changed during review.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise VideoOutputProcessingError("The resize receipt cannot be decoded.") from exc
    if not isinstance(payload, dict) or raw != _canonical(payload):
        raise VideoOutputProcessingError("The resize receipt encoding changed.")
    fingerprint = payload.pop("receipt_fingerprint", None)
    if fingerprint != hashlib.sha256(_canonical(payload)).hexdigest():
        raise VideoOutputProcessingError("The resize receipt fingerprint changed.")
    payload["receipt_fingerprint"] = fingerprint
    expected = {"schema_id", "destination_name", "stage_name", "stage_id",
        "source_sha256", "source_byte_length", "source_stream_id", "source_timing",
        "source_request_fingerprint", "recipe", "recipe_fingerprint", "output_sha256",
        "output_byte_length", "output_stream_id", "output_timing", "receipt_fingerprint"}
    stage_pattern = rf"\.{re.escape(target.name)}\.collectigen-[a-f0-9]{{32}}\.staging\.mp4"
    if (set(payload) != expected or payload["schema_id"] != RECEIPT_SCHEMA
            or payload["destination_name"] != target.name
            or not isinstance(payload["stage_name"], str)
            or re.fullmatch(stage_pattern, payload["stage_name"]) is None
            or type(payload["stage_id"]) is not int or payload["stage_id"] <= 0
            or payload["source_sha256"] != source.sha256
            or payload["source_byte_length"] != source.byte_length
            or payload["source_stream_id"] != source.stream_id
            or payload["source_timing"] != source.timing.to_dict()
            or payload["source_request_fingerprint"] != recipe["source_request_fingerprint"]
            or payload["recipe"] != recipe
            or payload["recipe_fingerprint"] != hashlib.sha256(_canonical(recipe)).hexdigest()
            or not isinstance(payload["output_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", payload["output_sha256"]) is None
            or type(payload["output_byte_length"]) is not int
            or not 0 < payload["output_byte_length"] <= MAX_VIDEO_BYTES):
        raise VideoOutputProcessingError("The resize receipt belongs to different exact work.")
    return payload, state


def _matches_receipt(verification: VideoVerification, receipt: dict) -> None:
    asset = verification.asset
    if (asset.sha256 != receipt["output_sha256"]
            or asset.byte_length != receipt["output_byte_length"]
            or asset.stream_id != receipt["output_stream_id"]
            or asset.timing.to_dict() != receipt["output_timing"]):
        raise VideoOutputProcessingError("The retained resized output differs from its receipt.")


def _after_publish(_target: Path) -> None:
    """Fault-injection seam after atomic publication, before caller bookkeeping."""


def resize_verified_video(
    source_path: str | Path, source_asset: VideoSourceAsset,
    source_request: VideoGenerationRequest, destination: str | Path, *,
    allowed_root: str | Path, width: int, height: int,
    should_cancel: Callable[[], bool] | None = None,
    timeout_seconds: float = 120,
) -> VideoResizeResult:
    """Fit without cropping, black-pad and publish one separate verified derivative.

    No source is modified. The child owns only an unpredictable sibling stage;
    the parent re-decodes it and uses no-clobber hard-link publication.
    """
    if (not isinstance(source_asset, VideoSourceAsset)
            or not isinstance(source_request, VideoGenerationRequest)
            or not source_request.schema_supported):
        raise VideoOutputProcessingError("Exact reviewed source and supported request are required.")
    if (type(width) is not int or type(height) is not int or width < 64 or height < 64
            or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS
            or width * height * 4 > MAX_FRAME_BYTES or width % 2 or height % 2):
        raise VideoOutputProcessingError("Resize dimensions must be bounded, even H.264 dimensions.")
    if not 0 < timeout_seconds <= 600:
        raise VideoOutputProcessingError("Choose a bounded video resize deadline.")
    deadline = monotonic() + timeout_seconds
    source = Path(source_path).absolute()
    target = Path(destination).absolute()
    boundary = Path(allowed_root).absolute()
    _assert_unredirected(source); _assert_unredirected(target.parent); _assert_unredirected(boundary)
    if (not boundary.is_dir() or not target.parent.is_dir()
            or not target.resolve(strict=False).is_relative_to(boundary.resolve(strict=True))
            or target.suffix.casefold() != ".mp4"
            or source.resolve(strict=True) == target.resolve(strict=False)):
        raise VideoOutputProcessingError("Choose a new MP4 destination inside the allowed output root.")
    receipt = _receipt_path(target)
    if os.path.lexists(target) and not os.path.lexists(receipt):
        raise VideoOutputProcessingError("Choose a new MP4 destination inside the allowed output root.")
    if should_cancel is not None and should_cancel():
        raise InterruptedError("Video resize cancelled safely.")
    _hash_source(source, source_asset, should_cancel, deadline)
    observed = verify_video_output(source, source_request, allowed_root=source.parent,
        should_cancel=should_cancel, max_seconds=_remaining(deadline))
    if (observed.asset.sha256 != source_asset.sha256
            or observed.asset.byte_length != source_asset.byte_length
            or observed.asset.stream_id != source_asset.stream_id
            or observed.asset.timing != source_asset.timing):
        raise VideoOutputProcessingError("The reviewed source identity or timeline changed.")
    recipe = _recipe(source_request, source_asset, width, height)
    output_request = replace(source_request, width=width, height=height)
    output_limit = min(MAX_VIDEO_BYTES,
        source_request.expected_output_bytes or MAX_VIDEO_BYTES,
        source_request.storage_budget_bytes or MAX_VIDEO_BYTES)
    if os.path.lexists(receipt):
        payload, receipt_state = _read_receipt(receipt, target, source_asset, recipe)
        old_stage = target.with_name(payload["stage_name"])
        candidate = target if os.path.lexists(target) else old_stage
        if os.path.lexists(candidate):
            if candidate == old_stage:
                _assert_unredirected(old_stage)
                if old_stage.stat(follow_symlinks=False).st_ino != payload["stage_id"]:
                    raise VideoOutputProcessingError("The retained resize stage changed identity.")
            verified = verify_video_output(candidate, output_request, allowed_root=boundary,
                should_cancel=should_cancel, max_seconds=_remaining(deadline),
                max_bytes=output_limit)
            _matches_receipt(verified, payload)
            result = _lineage_result(target, receipt, source_asset, verified, recipe)
            _hash_source(source, source_asset, should_cancel, deadline)
            if candidate == old_stage:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Video resize cancelled safely.")
                _remaining(deadline)
                os.link(old_stage, target)
                _after_publish(target)
            _hash_source(target, verified.asset, should_cancel, deadline)
            if (os.path.lexists(old_stage)
                    and old_stage.stat(follow_symlinks=False).st_ino == payload["stage_id"]
                    and stat.S_ISREG(old_stage.stat(follow_symlinks=False).st_mode)):
                old_stage.unlink()
            return result
        # An owned prepublication receipt with no output or stage proves no
        # derivative was published. Retire only this exact receipt and retry.
        current_receipt = receipt.stat(follow_symlinks=False)
        if (current_receipt.st_dev, current_receipt.st_ino, current_receipt.st_size,
                current_receipt.st_mtime_ns) != (
                receipt_state.st_dev, receipt_state.st_ino, receipt_state.st_size,
                receipt_state.st_mtime_ns):
            raise VideoOutputProcessingError("The resize receipt changed before retry.")
        receipt.unlink()
    if os.path.lexists(target):
        raise VideoOutputProcessingError("The resize destination was occupied by another result.")
    required_free = output_limit + FREE_SPACE_RESERVE_BYTES
    if shutil.disk_usage(target.parent).free < required_free:
        raise VideoOutputProcessingError(
            "Safe video resize needs room for one bounded output stage plus a 512 MiB reserve.")
    stage = target.with_name(f".{target.name}.collectigen-{uuid4().hex}.staging.mp4")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(stage, flags, 0o600)
    stage_id = os.fstat(descriptor).st_ino
    os.close(descriptor)
    receipt_id = None
    try:
        result = _encode_child({"source": str(source), "stage": str(stage),
            "source_asset": source_asset.to_dict(), "width": width, "height": height,
            "stage_id": stage_id, "source_max_bytes": MAX_VIDEO_BYTES,
            "output_max_bytes": output_limit,
            "timeout_seconds": _remaining(deadline)},
            should_cancel=should_cancel, deadline=deadline)
        if result.get("source_sha256") != source_asset.sha256:
            raise VideoOutputProcessingError("The encoder did not retain the exact source identity.")
        if (result.get("fitted_width") != recipe["fitted_width"]
                or result.get("fitted_height") != recipe["fitted_height"]):
            raise VideoOutputProcessingError("The encoder changed the reviewed fit geometry.")
        if stage.stat(follow_symlinks=False).st_ino != stage_id:
            raise VideoOutputProcessingError("The isolated output stage changed identity.")
        verified = verify_video_output(stage, output_request, allowed_root=boundary,
            should_cancel=should_cancel, max_seconds=_remaining(deadline),
            max_bytes=output_limit)
        outcome = _lineage_result(target, receipt, source_asset, verified, recipe)
        _hash_source(source, source_asset, should_cancel, deadline)
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Video resize cancelled safely.")
        _remaining(deadline)
        if os.path.lexists(target):
            raise VideoOutputProcessingError("The resize destination appeared before publication.")
        payload = _receipt_payload(target, stage, stage_id, source_asset, verified, recipe)
        receipt_id = _write_receipt(receipt, payload)
        os.link(stage, target)
        _after_publish(target)
        _hash_source(target, verified.asset, should_cancel, deadline)
        return outcome
    finally:
        if (receipt_id is not None and not os.path.lexists(target)
                and os.path.lexists(receipt)
                and receipt.stat(follow_symlinks=False).st_ino == receipt_id):
            receipt.unlink()
        try:
            if (os.path.lexists(stage) and stat.S_ISREG(stage.stat(follow_symlinks=False).st_mode)
                    and stage.stat(follow_symlinks=False).st_ino == stage_id):
                stage.unlink()
        except OSError:
            pass
