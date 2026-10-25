"""Pure, explicit video derivative and training-split evidence.

No media is opened. A mapping only describes observed presentation timestamps;
it cannot certify a decoder, infer VFR frames, or discover visual duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .video_generation import (
    VideoContractError, VideoSourceAsset, _bounded_json, _fields, _hash,
    _integer, _merged, _text, validate_interval_scope,
)


DERIVATIVE_SCHEMA = "collectigen.video-derivative.v1"
TRAINING_SCHEMA = "collectigen.video-training-projection.v1"


@dataclass(frozen=True)
class FrameCorrespondence:
    """One observed output frame and every observed source frame used for it."""

    output_pts: int
    source_pts: tuple[int, ...]
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _integer(self.output_pts, "Output PTS")
        if not isinstance(self.source_pts, tuple) or not self.source_pts:
            raise VideoContractError("Frame correspondence needs explicit source PTS.")
        for pts in self.source_pts:
            _integer(pts, "Source PTS")
        if len(set(self.source_pts)) != len(self.source_pts):
            raise VideoContractError("Source PTS cannot repeat within one correspondence.")
        _bounded_json(dict(self.extra_fields), "Correspondence fields")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"output_pts": self.output_pts, "source_pts": list(self.source_pts)})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FrameCorrespondence":
        return cls(value["output_pts"], tuple(value["source_pts"]),
                   _fields(value, {"output_pts", "source_pts"}, "Correspondence"))


@dataclass(frozen=True)
class DerivedClipMapping:
    """Exact source-to-output frame relation for one verified derivative clip."""

    source_asset: VideoSourceAsset
    output_asset: VideoSourceAsset
    source_start_pts: int
    source_end_pts: int
    output_start_pts: int
    output_end_pts: int
    decoder_id: str
    transform_sha256: str
    frames: tuple[FrameCorrespondence, ...]
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    schema_id: str = DERIVATIVE_SCHEMA

    def __post_init__(self) -> None:
        _integer(self.schema_version, "Derivative schema version", minimum=1)
        _text(self.schema_id, "Derivative schema ID", maximum=256)
        if self.schema_version != 1 or self.schema_id != DERIVATIVE_SCHEMA:
            raise VideoContractError("Unknown derivative contract version.")
        if not isinstance(self.source_asset, VideoSourceAsset) or not isinstance(self.output_asset, VideoSourceAsset):
            raise VideoContractError("Derivative needs typed source and output assets.")
        validate_interval_scope(self.source_start_pts, self.source_end_pts, self.source_asset.timing)
        validate_interval_scope(self.output_start_pts, self.output_end_pts, self.output_asset.timing)
        _text(self.decoder_id, "Decoder identity", maximum=512)
        _hash(self.transform_sha256, "Transform")
        if not isinstance(self.frames, tuple) or not self.frames or any(
            not isinstance(item, FrameCorrespondence) for item in self.frames
        ):
            raise VideoContractError("Derivative needs explicit frame correspondences.")
        source_pts = self.source_asset.timing.frame_pts
        output_pts = self.output_asset.timing.frame_pts
        if not source_pts or not output_pts:
            raise VideoContractError("Derivative mapping needs both observed frame timelines.")
        selected_output = tuple(pts for pts in output_pts if self.output_start_pts <= pts < self.output_end_pts)
        if tuple(item.output_pts for item in self.frames) != selected_output:
            raise VideoContractError("Derivative mapping must cover each selected observed output frame once in order.")
        allowed_source = {pts for pts in source_pts if self.source_start_pts <= pts < self.source_end_pts}
        if not allowed_source or any(pts not in allowed_source for item in self.frames for pts in item.source_pts):
            raise VideoContractError("Derivative mapping references an unobserved or out-of-range source frame.")
        _bounded_json(dict(self.extra_fields), "Derivative fields")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {
            "schema_id": self.schema_id, "schema_version": self.schema_version,
            "source_asset": self.source_asset.to_dict(), "output_asset": self.output_asset.to_dict(),
            "source_start_pts": self.source_start_pts, "source_end_pts": self.source_end_pts,
            "output_start_pts": self.output_start_pts, "output_end_pts": self.output_end_pts,
            "decoder_id": self.decoder_id, "transform_sha256": self.transform_sha256,
            "frames": [item.to_dict() for item in self.frames],
        })

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DerivedClipMapping":
        known = {"schema_id", "schema_version", "source_asset", "output_asset", "source_start_pts",
                 "source_end_pts", "output_start_pts", "output_end_pts", "decoder_id", "transform_sha256", "frames"}
        return cls(VideoSourceAsset.from_dict(value["source_asset"]),
                   VideoSourceAsset.from_dict(value["output_asset"]),
                   value["source_start_pts"], value["source_end_pts"],
                   value["output_start_pts"], value["output_end_pts"], value["decoder_id"],
                   value["transform_sha256"], tuple(FrameCorrespondence.from_dict(item) for item in value["frames"]),
                   _fields(value, known, "Derivative"), value.get("schema_version", 1),
                   value.get("schema_id", DERIVATIVE_SCHEMA))


@dataclass(frozen=True)
class SplitCandidate:
    """An exact sample identity and known clip/duplicate lineage; no similarity claim."""

    sample_id: str
    asset_sha256: str
    source_clip_sha256: str
    known_duplicate_sha256: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.sample_id, "Sample ID", maximum=256)
        _hash(self.asset_sha256, "Sample asset")
        _hash(self.source_clip_sha256, "Source clip")
        for digest in self.known_duplicate_sha256:
            _hash(digest, "Known duplicate")


@dataclass(frozen=True)
class SplitGroup:
    member_ids: tuple[str, ...]
    unknown_similarity_unresolved: bool = True


def transitive_split_groups(candidates: tuple[SplitCandidate, ...]) -> tuple[SplitGroup, ...]:
    """Connect same clips, derivatives, and asserted full-SHA duplicate edges."""
    if any(not isinstance(item, SplitCandidate) for item in candidates):
        raise VideoContractError("Split grouping needs typed candidates.")
    if len({item.sample_id for item in candidates}) != len(candidates):
        raise VideoContractError("Split sample IDs must be unique.")
    parent = {item.sample_id: item.sample_id for item in candidates}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: str, second: str) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[max(left, right)] = min(left, right)

    seen: dict[str, str] = {}
    for item in sorted(candidates, key=lambda candidate: candidate.sample_id):
        for digest in (item.asset_sha256, item.source_clip_sha256, *item.known_duplicate_sha256):
            if digest in seen:
                union(item.sample_id, seen[digest])
            else:
                seen[digest] = item.sample_id
    grouped: dict[str, list[str]] = {}
    for item in candidates:
        grouped.setdefault(find(item.sample_id), []).append(item.sample_id)
    return tuple(SplitGroup(tuple(sorted(group))) for group in sorted(grouped.values(), key=lambda ids: min(ids)))


@dataclass(frozen=True)
class VideoTrainingProjection:
    """Additive caption/manifest evidence; never an executable training admission."""

    source_asset: VideoSourceAsset
    start_pts: int
    end_pts: int
    sample_sha256: str
    mapping: DerivedClipMapping | None
    caption_profile: Mapping[str, Any]
    ordered_scopes: tuple[Mapping[str, Any], ...]
    full_knowledge: tuple[Mapping[str, Any], ...]
    loss_report: Mapping[str, Any]
    target: str
    method: str
    split_group: str
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    schema_id: str = TRAINING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.schema_id != TRAINING_SCHEMA:
            raise VideoContractError("Unknown training projection version.")
        validate_interval_scope(self.start_pts, self.end_pts, self.source_asset.timing)
        _hash(self.sample_sha256, "Training sample")
        if self.mapping is None and self.sample_sha256 != self.source_asset.sha256:
            raise VideoContractError("A changed training sample needs an explicit source-to-sample mapping.")
        if self.mapping is not None and (
            self.mapping.source_asset.to_dict() != self.source_asset.to_dict()
            or self.mapping.output_asset.sha256 != self.sample_sha256
            or (self.mapping.source_start_pts, self.mapping.source_end_pts) != (self.start_pts, self.end_pts)
        ):
            raise VideoContractError("Training projection mapping is stale or for another sample.")
        for label, value in (("caption profile", self.caption_profile), ("loss report", self.loss_report)):
            if not isinstance(value, Mapping):
                raise VideoContractError(f"Training {label} must be an object.")
            _bounded_json(dict(value), f"Training {label}")
        _text(self.caption_profile.get("profile_id"), "Caption profile ID", maximum=256)
        _integer(self.caption_profile.get("profile_version"), "Caption profile version", minimum=1)
        for label, values in (("ordered scopes", self.ordered_scopes), ("full knowledge", self.full_knowledge)):
            if not isinstance(values, tuple) or any(not isinstance(item, Mapping) for item in values):
                raise VideoContractError(f"Training {label} must be ordered objects.")
            _bounded_json([dict(item) for item in values], f"Training {label}")
        for item in self.ordered_scopes:
            scope = item.get("scope")
            if not isinstance(scope, Mapping):
                raise VideoContractError("Caption segment needs an explicit scope.")
            if scope.get("kind") == "interval":
                left, right = validate_interval_scope(scope.get("start_pts"), scope.get("end_pts"), self.source_asset.timing)
                if left < self.start_pts or right > self.end_pts:
                    raise VideoContractError("Caption segment falls outside selected sample range.")
            elif scope.get("kind") == "frame":
                pts = scope.get("pts")
                if (type(pts) is not int or pts not in self.source_asset.timing.frame_pts
                        or not self.start_pts <= pts < self.end_pts):
                    raise VideoContractError("Caption frame is not observed within the selected range.")
            else:
                raise VideoContractError("Caption segment needs a frame or interval scope.")
        for label, value in (("target", self.target), ("method", self.method), ("split group", self.split_group)):
            _text(value, label, maximum=256)
        _bounded_json(dict(self.extra_fields), "Training projection fields")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {
            "schema_id": self.schema_id, "schema_version": self.schema_version,
            "source_asset": self.source_asset.to_dict(), "start_pts": self.start_pts, "end_pts": self.end_pts,
            "sample_sha256": self.sample_sha256, "mapping": self.mapping.to_dict() if self.mapping else None,
            "caption_profile": _bounded_json(dict(self.caption_profile), "Caption profile"),
            "ordered_scopes": _bounded_json([dict(item) for item in self.ordered_scopes], "Ordered scopes"),
            "full_knowledge": _bounded_json([dict(item) for item in self.full_knowledge], "Full knowledge"),
            "loss_report": _bounded_json(dict(self.loss_report), "Loss report"),
            "target": self.target, "method": self.method, "split_group": self.split_group,
        })

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VideoTrainingProjection":
        known = {"schema_id", "schema_version", "source_asset", "start_pts", "end_pts", "sample_sha256",
                 "mapping", "caption_profile", "ordered_scopes", "full_knowledge", "loss_report", "target",
                 "method", "split_group"}
        return cls(VideoSourceAsset.from_dict(value["source_asset"]), value["start_pts"], value["end_pts"],
                   value["sample_sha256"], DerivedClipMapping.from_dict(value["mapping"]) if value.get("mapping") else None,
                   value["caption_profile"], tuple(value["ordered_scopes"]), tuple(value["full_knowledge"]),
                   value["loss_report"], value["target"], value["method"], value["split_group"],
                   _fields(value, known, "Training projection"), value.get("schema_version", 1),
                   value.get("schema_id", TRAINING_SCHEMA))
