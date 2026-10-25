"""Explicit derivatives of one already verified silent clip.

Extraction and trimming preserve the original and publish only new, bounded
outputs. A trim is a new clip, never a continuation or an inferred last frame.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
from fractions import Fraction
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import stat
from time import monotonic
from typing import Callable
from uuid import uuid4
import re

from PIL import Image

from .training_source_io import _assert_unredirected
from .video_decoder import MAX_VIDEO_BYTES, VideoVerification, iter_video_preview_frames, verify_video_output
from .video_generation import DerivedFrameReference, VideoGenerationRequest, VideoSourceAsset
from .video_output_processing import (
    FREE_SPACE_RESERVE_BYTES, VideoOutputProcessingError, _canonical,
    _encode_child, _hash_source, _remaining, _write_receipt,
)
from .video_temporal import DerivedClipMapping, FrameCorrespondence
from .video_asset import VideoAssetRecord


FINISHING_SCHEMA = "collectigen.video-finishing.v1"
MAX_FRAME_PNG_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class ExtractedFrame:
    output_path: Path
    receipt_path: Path
    source: VideoSourceAsset
    reference: DerivedFrameReference


@dataclass(frozen=True)
class TrimmedVideo:
    output_path: Path
    receipt_path: Path
    verification: VideoVerification
    lineage: DerivedClipMapping


@dataclass(frozen=True)
class PreparedFrameIngest:
    """Exact unreviewed image family prepared for normal Ingest admission."""

    output_path: Path
    sidecar_path: Path
    image_sha256: str
    sidecar_sha256: str
    reference: DerivedFrameReference


def _request_for_asset(request: VideoGenerationRequest, asset: VideoSourceAsset) -> VideoGenerationRequest:
    if not isinstance(request, VideoGenerationRequest) or not request.schema_supported:
        raise VideoOutputProcessingError("An exact supported source request is required.")
    evidence = asset.probe_provenance
    if (not isinstance(evidence, dict) or type(evidence.get("width")) is not int
            or type(evidence.get("height")) is not int):
        raise VideoOutputProcessingError("The source lacks verified frame geometry.")
    return replace(request, width=evidence["width"], height=evidence["height"],
                   frame_count=asset.timing.frame_count)


def _destination(destination, allowed_root, suffix: str) -> tuple[Path, Path]:
    target = Path(destination).absolute()
    root = Path(allowed_root).absolute()
    _assert_unredirected(root); _assert_unredirected(target.parent)
    if (not root.is_dir() or not target.parent.is_dir()
            or not target.resolve(strict=False).is_relative_to(root.resolve(strict=True))
            or target.suffix.casefold() != suffix or target.name.startswith(".")
            or os.path.lexists(target)):
        raise VideoOutputProcessingError("Choose a new output name inside the allowed finishing folder.")
    receipt = target.with_name(f".{target.name}.collectigen-finishing.json")
    if os.path.lexists(receipt):
        raise VideoOutputProcessingError("This finishing output already has a receipt; choose a new name.")
    return target, receipt


def _source_check(path: Path, asset: VideoSourceAsset, request: VideoGenerationRequest,
                  cancel, deadline: float) -> None:
    _hash_source(path, asset, cancel, deadline)
    observed = verify_video_output(path, _request_for_asset(request, asset),
        allowed_root=path.parent, should_cancel=cancel, max_seconds=_remaining(deadline))
    if (observed.asset.sha256, observed.asset.byte_length, observed.asset.stream_id,
            observed.asset.timing) != (asset.sha256, asset.byte_length, asset.stream_id, asset.timing):
        raise VideoOutputProcessingError("The reviewed video source or timeline changed.")


def _publish(stage: Path, target: Path, receipt: Path, payload: dict, stage_id: int) -> None:
    payload["receipt_fingerprint"] = hashlib.sha256(_canonical(payload)).hexdigest()
    receipt_id = _write_receipt(receipt, payload)
    try:
        if (os.path.lexists(target) or stage.stat(follow_symlinks=False).st_ino != stage_id):
            raise VideoOutputProcessingError("The finishing destination or stage changed before publication.")
        os.link(stage, target)
    except BaseException:
        if (os.path.lexists(receipt)
                and receipt.stat(follow_symlinks=False).st_ino == receipt_id):
            receipt.unlink()
        raise


def _retire_stage(stage: Path, stage_id: int) -> None:
    try:
        if (os.path.lexists(stage)
                and stat.S_ISREG(stage.stat(follow_symlinks=False).st_mode)
                and stage.stat(follow_symlinks=False).st_ino == stage_id):
            stage.unlink()
    except OSError:
        pass


def extract_verified_frame(
    source_path: str | Path, source_asset: VideoSourceAsset,
    source_request: VideoGenerationRequest, frame_index: int,
    destination: str | Path, *, allowed_root: str | Path,
    should_cancel: Callable[[], bool] | None = None, timeout_seconds: float = 120,
) -> ExtractedFrame:
    """Full-check and extract one observed frame to a new PNG with exact lineage."""
    if (type(frame_index) is not int or not 0 <= frame_index < source_asset.timing.frame_count
            or not 0 < timeout_seconds <= 600):
        raise VideoOutputProcessingError("Choose one observed frame and a bounded deadline.")
    target, receipt = _destination(destination, allowed_root, ".png")
    source = Path(source_path).absolute()
    deadline = monotonic() + timeout_seconds
    _source_check(source, source_asset, source_request, should_cancel, deadline)
    pts = source_asset.timing.frame_pts[frame_index]
    frames = iter_video_preview_frames(source, source_asset, allowed_root=source.parent,
        start_pts=pts, should_cancel=should_cancel, max_seconds=_remaining(deadline))
    try:
        frame = next(frames)
    finally:
        frames.close()
    if frame.pts != pts or len(frame.rgb) != frame.width * frame.height * 3:
        raise VideoOutputProcessingError("The selected observed frame changed during extraction.")
    image = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)
    stream = BytesIO(); image.save(stream, format="PNG", optimize=False)
    raw = stream.getvalue()
    if len(raw) > MAX_FRAME_PNG_BYTES:
        raise VideoOutputProcessingError("Extracted frame exceeds the PNG byte bound.")
    _hash_source(source, source_asset, should_cancel, deadline)
    if should_cancel is not None and should_cancel():
        raise InterruptedError("Video extraction cancelled safely.")
    recipe = {"operation": "extract_rgb_frame", "frame_index": frame_index,
        "source_pts": pts, "decoder": "PyAV", "format": "PNG RGB8"}
    transform = hashlib.sha256(_canonical(recipe)).hexdigest()
    digest = hashlib.sha256(raw).hexdigest()
    reference = DerivedFrameReference(source_asset.asset_id, source_asset.sha256,
        source_asset.stream_id, pts, frame_index, "PyAV", transform, digest,
        {"source_timing": source_asset.timing.to_dict(), "recipe": recipe})
    stage = target.with_name(f".{target.name}.collectigen-{uuid4().hex}.staging.png")
    descriptor = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY | int(getattr(os, "O_BINARY", 0)), 0o600)
    stage_id = os.fstat(descriptor).st_ino
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw); file.flush(); os.fsync(file.fileno())
        if stage.read_bytes() != raw:
            raise VideoOutputProcessingError("The staged extracted frame changed.")
        _hash_source(source, source_asset, should_cancel, deadline)
        _publish(stage, target, receipt, {"schema_id": FINISHING_SCHEMA,
            "operation": "extract_frame", "destination_name": target.name,
            "source_asset": source_asset.to_dict(), "frame": reference.to_dict(),
            "output_sha256": digest, "output_byte_length": len(raw)}, stage_id)
        return ExtractedFrame(target, receipt, source_asset, reference)
    finally:
        _retire_stage(stage, stage_id)


def trim_verified_video(
    source_path: str | Path, source_asset: VideoSourceAsset,
    source_request: VideoGenerationRequest, first_frame: int, stop_frame: int,
    destination: str | Path, *, allowed_root: str | Path,
    should_cancel: Callable[[], bool] | None = None, timeout_seconds: float = 180,
) -> TrimmedVideo:
    """Transcode a reviewed half-open frame interval to a new silent MP4.

    Current output verification qualifies constant observed cadence only. VFR
    sources remain available for frame extraction but cannot be silently trimmed.
    """
    timing = source_asset.timing
    if (type(first_frame) is not int or type(stop_frame) is not int
            or not 0 <= first_frame < stop_frame <= timing.frame_count
            or stop_frame - first_frame == timing.frame_count
            or not 0 < timeout_seconds <= 600):
        raise VideoOutputProcessingError("Choose a shorter observed frame interval and bounded deadline.")
    if timing.end_pts is None or len(timing.frame_pts) != timing.frame_count:
        raise VideoOutputProcessingError("Trim needs a complete observed frame timeline.")
    step = Fraction(1, 1) / source_request.fps.fraction / timing.time_base.fraction
    points = timing.frame_pts
    if (step.denominator != 1 or any(right - left != step.numerator
            for left, right in zip(points, points[1:]))
            or timing.end_pts - points[-1] != step.numerator):
        raise VideoOutputProcessingError("This clip has variable observed frame timing; frame extraction is available, but exact trim is not qualified.")
    target, receipt = _destination(destination, allowed_root, ".mp4")
    source = Path(source_path).absolute()
    deadline = monotonic() + timeout_seconds
    _source_check(source, source_asset, source_request, should_cancel, deadline)
    output_count = stop_frame - first_frame
    output_request = replace(_request_for_asset(source_request, source_asset), frame_count=output_count)
    limit = min(MAX_VIDEO_BYTES, source_request.expected_output_bytes or MAX_VIDEO_BYTES,
                source_request.storage_budget_bytes or MAX_VIDEO_BYTES)
    if shutil.disk_usage(target.parent).free < limit + FREE_SPACE_RESERVE_BYTES:
        raise VideoOutputProcessingError("Safe trim needs one bounded output stage plus a 512 MiB reserve.")
    stage = target.with_name(f".{target.name}.collectigen-{uuid4().hex}.staging.mp4")
    descriptor = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY | int(getattr(os, "O_BINARY", 0)), 0o600)
    stage_id = os.fstat(descriptor).st_ino
    os.close(descriptor)
    try:
        result = _encode_child({"operation": "trim", "source": str(source), "stage": str(stage),
            "source_asset": source_asset.to_dict(), "width": output_request.width,
            "height": output_request.height, "first_frame": first_frame,
            "stop_frame": stop_frame, "stage_id": stage_id,
            "source_max_bytes": MAX_VIDEO_BYTES, "output_max_bytes": limit,
            "timeout_seconds": _remaining(deadline)}, should_cancel=should_cancel, deadline=deadline)
        if (result.get("source_sha256") != source_asset.sha256
                or result.get("selected_frames") != output_count
                or stage.stat(follow_symlinks=False).st_ino != stage_id):
            raise VideoOutputProcessingError("The isolated trim changed exact source or stage evidence.")
        verified = verify_video_output(stage, output_request, allowed_root=target.parent,
            should_cancel=should_cancel, max_seconds=_remaining(deadline), max_bytes=limit)
        output = verified.asset
        selected = points[first_frame:stop_frame]
        if tuple(timing.seconds_at(p) - timing.seconds_at(selected[0]) for p in selected) != tuple(
                output.timing.seconds_at(p) for p in output.timing.frame_pts):
            raise VideoOutputProcessingError("The trimmed clip changed the selected frame timeline.")
        recipe = {"operation": "trim", "first_frame": first_frame, "stop_frame": stop_frame,
            "source_pts": list(selected), "codec": "libx264", "pixel_format": "yuv420p",
            "audio": "none", "frame_mapping": "one_to_one_reencoded"}
        transform = hashlib.sha256(_canonical(recipe)).hexdigest()
        source_end = points[stop_frame] if stop_frame < len(points) else timing.end_pts
        lineage = DerivedClipMapping(source_asset, output, selected[0], source_end,
            output.timing.start_pts, output.timing.end_pts, "PyAV", transform,
            tuple(FrameCorrespondence(out_pts, (src_pts,)) for src_pts, out_pts in
                  zip(selected, output.timing.frame_pts)), {"recipe": recipe})
        _hash_source(source, source_asset, should_cancel, deadline)
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Video trim cancelled safely.")
        _publish(stage, target, receipt, {"schema_id": FINISHING_SCHEMA,
            "operation": "trim", "destination_name": target.name,
            "source_asset": source_asset.to_dict(), "output_asset": output.to_dict(),
            "lineage": lineage.to_dict(), "output_sha256": output.sha256,
            "output_byte_length": output.byte_length}, stage_id)
        return TrimmedVideo(target, receipt, verified, lineage)
    finally:
        _retire_stage(stage, stage_id)


def verified_trimmed_output(
    trimmed: TrimmedVideo, original_descriptor: dict,
    source_request: VideoGenerationRequest,
    *, should_cancel: Callable[[], bool] | None = None,
) -> dict:
    """Recheck a finished result and project its exact lineage for Ingest.

    The caller must first obtain ``original_descriptor`` from the source job's
    current verified-output callback; a stale cached descriptor is insufficient.
    """
    source = trimmed.lineage.source_asset
    output = trimmed.lineage.output_asset
    if (not isinstance(original_descriptor, dict)
            or original_descriptor.get("sha256") != source.sha256
            or original_descriptor.get("source_asset") != source.to_dict()):
        raise VideoOutputProcessingError("The original verified video differs from the trim source.")
    source_path = Path(original_descriptor.get("path", "")).absolute()
    if not source_path.is_file():
        raise VideoOutputProcessingError("The original verified video is unavailable.")
    deadline = monotonic() + 120
    _source_check(source_path, source, source_request, should_cancel, deadline)
    target, receipt = trimmed.output_path.absolute(), trimmed.receipt_path.absolute()
    if receipt != target.with_name(f".{target.name}.collectigen-finishing.json"):
        raise VideoOutputProcessingError("The exact trim receipt path changed.")
    _assert_unredirected(target); _assert_unredirected(receipt)
    before = receipt.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024:
        raise VideoOutputProcessingError("The exact trim receipt is unavailable or oversized.")
    raw = receipt.read_bytes()
    after = receipt.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise VideoOutputProcessingError("The exact trim receipt changed during review.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise VideoOutputProcessingError("The exact trim receipt cannot be decoded.") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise VideoOutputProcessingError("The exact trim receipt changed encoding.")
    fingerprint = value.pop("receipt_fingerprint", None)
    if fingerprint != hashlib.sha256(_canonical(value)).hexdigest():
        raise VideoOutputProcessingError("The exact trim receipt fingerprint changed.")
    if (set(value) != {"schema_id", "operation", "destination_name", "source_asset",
            "output_asset", "lineage", "output_sha256", "output_byte_length"}
            or value["schema_id"] != FINISHING_SCHEMA or value["operation"] != "trim"
            or value["destination_name"] != target.name
            or value["source_asset"] != source.to_dict()
            or value["output_asset"] != output.to_dict()
            or value["lineage"] != trimmed.lineage.to_dict()
            or value["output_sha256"] != output.sha256
            or value["output_byte_length"] != output.byte_length):
        raise VideoOutputProcessingError("The trim receipt differs from exact reviewed work.")
    recipe = trimmed.lineage.extra_fields.get("recipe", {})
    output_request = replace(_request_for_asset(source_request, source),
                             frame_count=output.timing.frame_count)
    observed = verify_video_output(target, output_request, allowed_root=target.parent,
        max_seconds=_remaining(deadline), should_cancel=should_cancel)
    if observed.asset.to_dict() != output.to_dict():
        raise VideoOutputProcessingError("The finished clip differs from its exact receipt.")
    _hash_source(source_path, source, should_cancel, deadline)
    generation = deepcopy(original_descriptor["generation"])
    previous_native = generation.pop("native_output", None)
    if previous_native is not None:
        generation["prior_native_output"] = previous_native
    generation["native_output"] = {"path": str(source_path),
        "sha256": source.sha256, "byte_length": source.byte_length,
        "source_asset": source.to_dict(), "recipe": recipe,
        "timeline": trimmed.lineage.to_dict()}
    generation["finishing"] = {"schema_id": FINISHING_SCHEMA,
        "operation": "trim", "original_output_receipt": deepcopy(original_descriptor.get("output_receipt")),
        "source_sha256": source.sha256, "trim_receipt_sha256": hashlib.sha256(raw).hexdigest()}
    descriptor = {"media_type": "video", "path": str(target),
        "sha256": output.sha256, "byte_length": output.byte_length,
        "source_asset": output.to_dict(), "decoder_evidence": observed.decoder_evidence,
        "review_status": "needs_review", "generation": generation,
        "output_receipt": {"path": str(receipt), "sha256": hashlib.sha256(raw).hexdigest()}}
    VideoAssetRecord.from_descriptor(descriptor)
    return descriptor


def verify_extracted_frame(
    extracted: ExtractedFrame, original_descriptor: dict,
    source_request: VideoGenerationRequest,
    *, should_cancel: Callable[[], bool] | None = None,
) -> DerivedFrameReference:
    """Recheck the exact PNG, receipt, and original clip before reuse."""
    source = extracted.source
    if (not isinstance(original_descriptor, dict)
            or original_descriptor.get("sha256") != source.sha256
            or original_descriptor.get("source_asset") != source.to_dict()):
        raise VideoOutputProcessingError("The original verified video differs from the extracted frame.")
    source_path = Path(original_descriptor.get("path", "")).absolute()
    deadline = monotonic() + 120
    _source_check(source_path, source, source_request, should_cancel, deadline)
    target, receipt = extracted.output_path.absolute(), extracted.receipt_path.absolute()
    if receipt != target.with_name(f".{target.name}.collectigen-finishing.json"):
        raise VideoOutputProcessingError("The exact frame receipt path changed.")
    _assert_unredirected(target); _assert_unredirected(receipt)
    before = receipt.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024:
        raise VideoOutputProcessingError("The exact frame receipt is unavailable or oversized.")
    raw = receipt.read_bytes()
    after = receipt.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise VideoOutputProcessingError("The exact frame receipt changed during review.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise VideoOutputProcessingError("The exact frame receipt cannot be decoded.") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise VideoOutputProcessingError("The exact frame receipt changed encoding.")
    fingerprint = value.pop("receipt_fingerprint", None)
    if (fingerprint != hashlib.sha256(_canonical(value)).hexdigest()
            or set(value) != {"schema_id", "operation", "destination_name",
                "source_asset", "frame", "output_sha256", "output_byte_length"}
            or value["schema_id"] != FINISHING_SCHEMA or value["operation"] != "extract_frame"
            or value["destination_name"] != target.name
            or value["source_asset"] != source.to_dict()
            or value["frame"] != extracted.reference.to_dict()
            or value["output_sha256"] != extracted.reference.output_sha256):
        raise VideoOutputProcessingError("The frame receipt differs from exact reviewed work.")
    if type(value["output_byte_length"]) is not int or not 0 < value["output_byte_length"] <= MAX_FRAME_PNG_BYTES:
        raise VideoOutputProcessingError("The extracted frame byte bound changed.")
    before_frame = target.stat(follow_symlinks=False)
    if not stat.S_ISREG(before_frame.st_mode) or before_frame.st_size != value["output_byte_length"]:
        raise VideoOutputProcessingError("The extracted frame changed after publication.")
    raw_frame = target.read_bytes()
    after_frame = target.stat(follow_symlinks=False)
    if ((before_frame.st_dev, before_frame.st_ino, before_frame.st_size, before_frame.st_mtime_ns)
            != (after_frame.st_dev, after_frame.st_ino, after_frame.st_size, after_frame.st_mtime_ns)
            or hashlib.sha256(raw_frame).hexdigest() != extracted.reference.output_sha256):
        raise VideoOutputProcessingError("The extracted frame changed after publication.")
    _hash_source(source_path, source, should_cancel, deadline)
    return extracted.reference


def prepare_frame_ingest(
    extracted: ExtractedFrame, original_descriptor: dict,
    source_request: VideoGenerationRequest,
) -> PreparedFrameIngest:
    """No-clobber Image Knowledge sidecar for a selected observed frame."""
    reference = verify_extracted_frame(extracted, original_descriptor, source_request)
    from .annotation import ImageAnnotation
    from .image_record import _atomic_create_text
    target = extracted.output_path.absolute()
    sidecar = target.with_suffix(".tags.json")
    _assert_unredirected(sidecar)
    annotation = ImageAnnotation(image=target.name, review_status="needs_review")
    annotation.source = {"kind": "verified_video_frame", "source_asset_id": reference.source_asset_id,
                         "source_sha256": reference.source_sha256}
    annotation.extra_fields["derived_frame"] = reference.to_dict()
    output_receipt = original_descriptor.get("output_receipt", {})
    if isinstance(output_receipt, dict) and isinstance(output_receipt.get("sha256"), str):
        annotation.source["video_output_receipt_sha256"] = output_receipt["sha256"]
    encoded = json.dumps(annotation.to_dict(), ensure_ascii=False,
                         sort_keys=True, indent=2, allow_nan=False) + "\n"
    raw = encoded.encode("utf-8")
    if len(raw) > 128 * 1024:
        raise VideoOutputProcessingError("Frame lineage exceeds the bounded image sidecar limit.")
    if sidecar.exists():
        existing = sidecar.stat(follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode) or existing.st_size > 128 * 1024:
            raise VideoOutputProcessingError("Existing frame Image Knowledge is unavailable or oversized.")
        with sidecar.open("rb") as stream:
            prior = stream.read(128 * 1024 + 1)
        current = sidecar.stat(follow_symlinks=False)
        if (len(prior) > 128 * 1024 or
                (existing.st_dev, existing.st_ino, existing.st_size, existing.st_mtime_ns) !=
                (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)):
            raise VideoOutputProcessingError("Existing frame Image Knowledge changed while being read.")
        if prior != raw:
            raise VideoOutputProcessingError("The extracted frame already has different Image Knowledge; review it before Ingest.")
    else:
        try:
            _atomic_create_text(sidecar, encoded)
        except FileExistsError as exc:
            raise VideoOutputProcessingError("The extracted frame gained Image Knowledge during preparation.") from exc
    prepared = PreparedFrameIngest(target, sidecar, reference.output_sha256,
                                  hashlib.sha256(raw).hexdigest(), reference)
    verify_prepared_frame_ingest(prepared, extracted, original_descriptor, source_request)
    return prepared


def verify_prepared_frame_ingest(
    prepared: PreparedFrameIngest, extracted: ExtractedFrame,
    original_descriptor: dict, source_request: VideoGenerationRequest,
) -> PreparedFrameIngest:
    """Recheck source clip, frame receipt, PNG and sidecar before normal Ingest."""
    if (not isinstance(prepared, PreparedFrameIngest)
            or prepared.output_path != extracted.output_path.absolute()
            or prepared.sidecar_path != prepared.output_path.with_suffix(".tags.json")
            or prepared.reference != extracted.reference):
        raise VideoOutputProcessingError("The frame Ingest family differs from exact reviewed work.")
    reference = verify_extracted_frame(extracted, original_descriptor, source_request)
    if prepared.image_sha256 != reference.output_sha256:
        raise VideoOutputProcessingError("The frame Ingest PNG identity changed.")
    _assert_unredirected(prepared.sidecar_path)
    before = prepared.sidecar_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024:
        raise VideoOutputProcessingError("The frame Ingest Image Knowledge is unavailable or oversized.")
    raw = prepared.sidecar_path.read_bytes()
    after = prepared.sidecar_path.stat(follow_symlinks=False)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or hashlib.sha256(raw).hexdigest() != prepared.sidecar_sha256):
        raise VideoOutputProcessingError("The frame Ingest Image Knowledge changed.")
    from .annotation import ImageAnnotation
    try:
        value = ImageAnnotation.from_dict(json.loads(raw), prepared.output_path.name)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise VideoOutputProcessingError("The frame Ingest Image Knowledge is invalid.") from exc
    if (value.review_status != "needs_review"
            or value.extra_fields.get("derived_frame") != reference.to_dict()):
        raise VideoOutputProcessingError("The frame Ingest provenance changed.")
    return prepared


def discover_finished_video(
    finishing_root: str | Path, source_asset: VideoSourceAsset,
    source_request: VideoGenerationRequest, original_descriptor: dict, *,
    should_cancel: Callable[[], bool] | None = None,
    max_entries: int = 128, max_results: int = 12,
) -> dict:
    """Find only this clip's published, fully reverified finishing receipts.

    The bounded directory walk is not an authority: each exact receipt, output,
    and original source is checked by the same handoff verifiers used live.
    Broken or foreign entries remain untouched and are reported as unavailable.
    """
    if (not isinstance(source_asset, VideoSourceAsset)
            or not isinstance(source_request, VideoGenerationRequest)
            or type(max_entries) is not int or not 1 <= max_entries <= 256
            or type(max_results) is not int or not 1 <= max_results <= 32):
        raise VideoOutputProcessingError("Choose a bounded exact video finishing source.")
    root = Path(finishing_root).absolute()
    _assert_unredirected(root)
    if not root.exists():
        return {"frames": (), "trims": (), "unavailable": 0, "truncated": False}
    if not root.is_dir():
        raise VideoOutputProcessingError("The saved finishing folder is unavailable.")
    if (not isinstance(original_descriptor, dict)
            or original_descriptor.get("sha256") != source_asset.sha256
            or original_descriptor.get("source_asset") != source_asset.to_dict()):
        raise VideoOutputProcessingError("The saved results lack the exact verified source.")
    prefix = re.escape(source_asset.sha256[:12])
    names = re.compile(rf"(?:frame-{prefix}-[0-9]+-[0-9a-f]{{12}}\.png|"
                       rf"trim-{prefix}-[0-9]+-[0-9]+-[0-9a-f]{{12}}\.mp4)")
    frames: list[ExtractedFrame] = []
    trims: list[TrimmedVideo] = []
    unavailable = 0
    truncated = False
    for index, path in enumerate(root.iterdir()):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Saved finishing discovery cancelled.")
        if index >= max_entries:
            truncated = True; break
        if names.fullmatch(path.name) is None:
            continue
        if len(frames) + len(trims) >= max_results:
            truncated = True; break
        receipt = path.with_name(f".{path.name}.collectigen-finishing.json")
        try:
            _assert_unredirected(path); _assert_unredirected(receipt)
            info = receipt.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
                raise VideoOutputProcessingError("Saved finishing receipt is unavailable.")
            value = json.loads(receipt.read_bytes())
            if (not isinstance(value, dict) or value.get("schema_id") != FINISHING_SCHEMA
                    or value.get("destination_name") != path.name
                    or value.get("source_asset") != source_asset.to_dict()):
                raise VideoOutputProcessingError("Saved finishing source differs.")
            if path.suffix == ".png" and value.get("operation") == "extract_frame":
                reference = DerivedFrameReference.from_dict(value["frame"])
                result = ExtractedFrame(path, receipt, source_asset, reference)
                verify_extracted_frame(result, original_descriptor, source_request,
                                       should_cancel=should_cancel)
                frames.append(result)
            elif path.suffix == ".mp4" and value.get("operation") == "trim":
                lineage = DerivedClipMapping.from_dict(value["lineage"])
                result = TrimmedVideo(path, receipt, VideoVerification(lineage.output_asset, {}), lineage)
                verified_trimmed_output(result, original_descriptor, source_request,
                                        should_cancel=should_cancel)
                trims.append(result)
            else:
                raise VideoOutputProcessingError("Saved finishing operation differs.")
        except InterruptedError:
            raise
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            unavailable += 1
    frames.sort(key=lambda item: (item.reference.ordinal, item.output_path.name))
    trims.sort(key=lambda item: (item.lineage.source_start_pts, item.output_path.name))
    return {"frames": tuple(frames), "trims": tuple(trims),
            "unavailable": unavailable, "truncated": truncated}
