"""Portable, image-independent companion for a verified video asset.

The companion records temporal evidence and lineage. Reading a listing never
opens or decodes the MP4, and an unfamiliar version stays opaque rather than
being interpreted as an image or executable video contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping
from uuid import uuid4

from .video_generation import (
    SCHEMA_VERSION as VIDEO_SOURCE_VERSION, SOURCE_SCHEMA_ID,
    VideoSourceAsset, validate_interval_scope,
)
from .video_temporal import DERIVATIVE_SCHEMA, DerivedClipMapping


VIDEO_ASSET_SCHEMA = "collectigen.video-asset.v1"
MAX_COMPANION_BYTES = 8 * 1024 * 1024


def _redirected(path: Path) -> bool:
    for member in (path, *path.parents):
        try:
            info = member.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return True
    return False


def companion_path(media_path: str | Path) -> Path:
    media = Path(media_path)
    if media.suffix.casefold() != ".mp4":
        raise ValueError("A video companion requires an MP4 path.")
    return media.with_name(media.name + ".video.json")


def _json_copy(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_COMPANION_BYTES:
        raise ValueError("The video companion exceeds its metadata bound.")
    return deepcopy(value)


def _validate_scopes(payload: Mapping[str, Any], asset: VideoSourceAsset) -> None:
    for field in ("temporal_knowledge", "temporal_captions"):
        entries = payload.get(field)
        if not isinstance(entries, list):
            raise ValueError(f"{field} must be a list of scoped evidence.")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("scope"), dict):
                raise ValueError(f"{field} contains evidence without an explicit scope.")
            scope = entry["scope"]
            kind = scope.get("kind")
            if kind == "whole_clip":
                if set(scope) != {"kind"}:
                    raise ValueError("Whole-clip scope cannot imply a frame range.")
            elif kind == "frame":
                pts = scope.get("pts")
                if type(pts) is not int or pts not in asset.timing.frame_pts:
                    raise ValueError("Frame evidence needs an observed presentation timestamp.")
            elif kind == "interval":
                validate_interval_scope(scope.get("start_pts"), scope.get("end_pts"), asset.timing)
            else:
                raise ValueError("Temporal evidence has an unsupported scope.")


@dataclass(frozen=True)
class VideoAssetRecord:
    payload: Mapping[str, Any]
    supported: bool

    @property
    def asset(self) -> VideoSourceAsset | None:
        return VideoSourceAsset.from_dict(self.payload["source_asset"]) if self.supported else None

    @property
    def temporal_derivations(self) -> tuple[DerivedClipMapping, ...]:
        if not self.supported:
            return ()
        return tuple(DerivedClipMapping.from_dict(item) for item in self.payload.get("temporal_derivations", ()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VideoAssetRecord":
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            raise ValueError("The video companion must be a JSON object.")
        payload = _json_copy(dict(value))
        supported = payload.get("schema_id") == VIDEO_ASSET_SCHEMA and payload.get("schema_version") == 1
        source = payload.get("source_asset")
        if supported and isinstance(source, dict) and (
            source.get("schema_id") != SOURCE_SCHEMA_ID
            or source.get("schema_version") != VIDEO_SOURCE_VERSION
        ):
            supported = False
        derivatives = payload.get("temporal_derivations", [])
        if supported and isinstance(derivatives, list) and any(
            isinstance(item, dict) and (
                item.get("schema_id") != DERIVATIVE_SCHEMA or item.get("schema_version") != 1
            ) for item in derivatives
        ):
            supported = False
        if supported:
            if payload.get("media_type") != "video" or payload.get("review_status") not in {
                "needs_review", "reviewed", "rejected",
            }:
                raise ValueError("The video companion has an unsupported media or review state.")
            asset = VideoSourceAsset.from_dict(payload["source_asset"])
            if payload.get("sha256") != asset.sha256 or payload.get("byte_length") != asset.byte_length:
                raise ValueError("Video companion bytes differ from the typed source asset.")
            if not isinstance(payload.get("generation"), dict) or not isinstance(payload.get("output_receipt"), dict):
                raise ValueError("Video companion needs generation and output-receipt provenance.")
            _validate_scopes(payload, asset)
            if not isinstance(derivatives, list):
                raise ValueError("Temporal derivations must be an ordered list.")
            for item in derivatives:
                mapping = DerivedClipMapping.from_dict(item)
                if mapping.output_asset.to_dict() != asset.to_dict():
                    raise ValueError("Temporal derivation output differs from the companion asset.")
        return cls(payload, supported)

    @classmethod
    def from_descriptor(cls, descriptor: Mapping[str, Any]) -> "VideoAssetRecord":
        if descriptor.get("media_type") != "video" or descriptor.get("review_status") != "needs_review":
            raise ValueError("Only a verified unreviewed video result can enter Ingest.")
        payload = {
            "schema_id": VIDEO_ASSET_SCHEMA,
            "schema_version": 1,
            "media_type": "video",
            "sha256": descriptor["sha256"],
            "byte_length": descriptor["byte_length"],
            "source_asset": deepcopy(descriptor["source_asset"]),
            "generation": deepcopy(descriptor["generation"]),
            "output_receipt": deepcopy(descriptor["output_receipt"]),
            "review_status": "needs_review",
            "temporal_knowledge": [],
            "temporal_captions": [],
        }
        native = payload["generation"].get("native_output")
        if native is not None:
            if not isinstance(native, dict) or not isinstance(native.get("timeline"), dict):
                raise ValueError("A native video derivative needs its exact temporal mapping.")
            mapping = DerivedClipMapping.from_dict(native["timeline"])
            if (mapping.source_asset.to_dict() != native.get("source_asset")
                    or mapping.source_asset.sha256 != native.get("sha256")
                    or mapping.source_asset.byte_length != native.get("byte_length")):
                raise ValueError("Temporal derivation source differs from the retained native asset.")
            # Keep raw generation evidence and expose the same mapping through
            # the canonical temporal contract used by grouping and projections.
            payload["temporal_derivations"] = [mapping.to_dict()]
        record = cls.from_dict(payload)
        if not record.supported:
            raise ValueError("The verified source asset has an unsupported video version.")
        return record

    def to_dict(self) -> dict[str, Any]:
        return _json_copy(dict(self.payload))

    @classmethod
    def read(cls, media_path: str | Path) -> "VideoAssetRecord":
        path = companion_path(media_path)
        if _redirected(path):
            raise ValueError("The video companion or its parent is redirected.")
        try:
            before = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError("The video companion is missing.") from exc
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("The video companion is not a regular file.")
        if before.st_size > MAX_COMPANION_BYTES:
            raise ValueError("The video companion exceeds its metadata bound.")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev, before.st_ino,
            ):
                raise ValueError("The video companion changed while opening.")
            raw = handle.read(MAX_COMPANION_BYTES + 1)
        if len(raw) > MAX_COMPANION_BYTES:
            raise ValueError("The video companion exceeds its metadata bound.")
        return cls.from_dict(json.loads(raw.decode("utf-8")))

    def write_new(self, media_path: str | Path) -> Path:
        if not self.supported:
            raise ValueError("An unknown video companion version cannot be written by this workflow.")
        path = companion_path(media_path)
        if _redirected(path):
            raise ValueError("The video companion path is redirected.")
        if path.exists() or path.is_symlink():
            existing = self.read(media_path)
            if existing.to_dict() != self.to_dict():
                raise FileExistsError("The video companion already contains different evidence.")
            return path
        raw = (json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        temporary = path.with_name(f".{path.name}.collectigen-{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
                temporary.unlink()
            if path.read_bytes() != raw:
                raise OSError("Video companion read-back verification failed.")
        finally:
            temporary.unlink(missing_ok=True)
        return path


@dataclass(frozen=True)
class VideoAssetRow:
    path: Path
    sha256: str
    review_status: str
    supported: bool


@dataclass(frozen=True)
class VideoAssetPage:
    rows: tuple[VideoAssetRow, ...]
    total: int
    offset: int
    next_offset: int | None
    issues: tuple[str, ...] = ()


def list_videos(
    root: str | Path, *, offset: int = 0, limit: int = 100,
    should_cancel=None,
) -> VideoAssetPage:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Video listing needs a nonnegative offset and a limit of 1 to 100.")
    folder = Path(root)
    if not folder.is_dir() or _redirected(folder):
        return VideoAssetPage((), 0, offset, None, ("The video folder is unavailable or redirected.",))
    names = sorted(
        (path for path in folder.iterdir() if path.name.casefold().endswith(".mp4.video.json")),
        key=lambda path: (path.name.casefold(), path.name),
    )
    rows = []
    issues = []
    for path in names[offset:offset + limit]:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Video listing was cancelled.")
        media = path.with_name(path.name[:-len(".video.json")])
        try:
            record = VideoAssetRecord.read(media)
            payload = record.to_dict()
            rows.append(VideoAssetRow(media, str(payload.get("sha256") or ""),
                                      str(payload.get("review_status") or "unknown"), record.supported))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            issues.append(f"{path.name}: {exc}")
    next_offset = offset + limit if offset + limit < len(names) else None
    return VideoAssetPage(tuple(rows), len(names), offset, next_offset, tuple(issues))
