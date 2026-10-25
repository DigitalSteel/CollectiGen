"""Versioned, pure temporal identity and silent video-request contracts.

These values describe evidence and a neutral request. They do not decode media,
read files, qualify an adapter, execute a graph, or write image/video records.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
import json
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
SCHEMA_ID = "collectigen.video-request.v1"
TIMING_SCHEMA_ID = "collectigen.video-timing.v1"
SOURCE_SCHEMA_ID = "collectigen.video-source.v1"
FRAME_SCHEMA_ID = "collectigen.video-frame-reference.v1"
IMAGE_SOURCE_SCHEMA_ID = "collectigen.video-image-source.v1"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 32_000_000
MAX_FRAMES = 1000
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class VideoContractError(ValueError):
    """A temporal request or evidence value is ambiguous or outside bounds."""


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if (type(value) is not int or abs(value) > 2**63 - 1
            or (minimum is not None and value < minimum)):
        raise VideoContractError(f"{label} must be an integer" + (f" at least {minimum}." if minimum is not None else "."))
    return value


def _text(value: Any, label: str, *, required: bool = True, maximum: int = 4096) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum or (required and not value.strip()):
        raise VideoContractError(f"{label} must be bounded text" + (" with a value." if required else "."))
    return value


def _hash(value: Any, label: str, *, resource: bool = False) -> str:
    if not isinstance(value, str) or not (_RESOURCE if resource else _SHA).fullmatch(value):
        raise VideoContractError(f"{label} needs an exact full SHA-256 identity.")
    return value


def _bounded_json(value: Any, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise VideoContractError(f"{label} must contain finite JSON values.") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise VideoContractError(f"{label} exceeds the bounded temporal contract.")
    return deepcopy(value)


def _fields(value: Any, known: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise VideoContractError(f"{label} must be an object with text fields.")
    return {key: _bounded_json(item, f"{label}.{key}") for key, item in value.items() if key not in known}


def _merged(extra: Mapping[str, Any], known: dict[str, Any]) -> dict[str, Any]:
    result = _bounded_json(dict(extra), "Unknown temporal fields")
    result.update(known)
    return _bounded_json(result, "Temporal record")


@dataclass(frozen=True)
class Rational:
    numerator: int
    denominator: int
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _integer(self.numerator, "Rational numerator", minimum=1)
        _integer(self.denominator, "Rational denominator", minimum=1)
        _bounded_json(dict(self.extra_fields), "Rational fields")

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"numerator": self.numerator, "denominator": self.denominator})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Rational":
        return cls(value["numerator"], value["denominator"], _fields(value, {"numerator", "denominator"}, "Rational"))


@dataclass(frozen=True)
class TemporalTiming:
    time_base: Rational
    start_pts: int
    frame_count: int
    nominal_fps: Rational
    average_fps: Rational | None = None
    duration_ticks: int | None = None
    frame_pts: tuple[int, ...] = ()
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    schema_id: str = TIMING_SCHEMA_ID

    def __post_init__(self) -> None:
        _integer(self.schema_version, "Timing schema version", minimum=1)
        _text(self.schema_id, "Timing schema ID", maximum=256)
        if not isinstance(self.time_base, Rational) or not isinstance(self.nominal_fps, Rational):
            raise VideoContractError("Timing needs rational time base and nominal FPS.")
        if self.average_fps is not None and not isinstance(self.average_fps, Rational):
            raise VideoContractError("Average FPS must be rational when observed.")
        _integer(self.start_pts, "Stream start PTS")
        _integer(self.frame_count, "Frame count", minimum=1)
        if self.frame_count > MAX_FRAMES:
            raise VideoContractError("Frame count exceeds the bounded temporal contract.")
        if self.duration_ticks is not None:
            _integer(self.duration_ticks, "Duration ticks", minimum=1)
        if self.frame_pts and len(self.frame_pts) != self.frame_count:
            raise VideoContractError("Frame PTS count differs from the observed frame count.")
        if self.frame_pts:
            validate_frame_pts(self.frame_pts, start_pts=self.start_pts,
                end_pts=self.end_pts)
        _bounded_json(dict(self.extra_fields), "Timing fields")

    @property
    def end_pts(self) -> int | None:
        return self.start_pts + self.duration_ticks if self.duration_ticks is not None else None

    @property
    def duration(self) -> Fraction | None:
        return self.duration_ticks * self.time_base.fraction if self.duration_ticks is not None else None

    def seconds_at(self, pts: int) -> Fraction:
        _integer(pts, "Presentation timestamp")
        return (pts - self.start_pts) * self.time_base.fraction

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"schema_version": self.schema_version, "schema_id": self.schema_id,
            "time_base": self.time_base.to_dict(),
            "start_pts": self.start_pts, "frame_count": self.frame_count,
            "nominal_fps": self.nominal_fps.to_dict(),
            "average_fps": self.average_fps.to_dict() if self.average_fps else None,
            "duration_ticks": self.duration_ticks, "frame_pts": list(self.frame_pts)})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TemporalTiming":
        fields = {"schema_version", "schema_id", "time_base", "start_pts", "frame_count", "nominal_fps", "average_fps", "duration_ticks", "frame_pts"}
        return cls(Rational.from_dict(value["time_base"]), value["start_pts"], value["frame_count"],
            Rational.from_dict(value["nominal_fps"]),
            Rational.from_dict(value["average_fps"]) if value.get("average_fps") is not None else None,
            value.get("duration_ticks"), tuple(value.get("frame_pts", ())), _fields(value, fields, "Timing"),
            value.get("schema_version", SCHEMA_VERSION), value.get("schema_id", TIMING_SCHEMA_ID))


def validate_frame_pts(pts_values: Sequence[int], *, start_pts: int,
                       end_pts: int | None = None, final_duration_ticks: int | None = None) -> tuple[int, ...]:
    """Validate presentation order and a known final endpoint without guessing VFR duration."""
    _integer(start_pts, "Stream start PTS")
    if end_pts is not None:
        _integer(end_pts, "Stream end PTS")
        if end_pts <= start_pts:
            raise VideoContractError("Stream end must follow its start.")
    if not isinstance(pts_values, (tuple, list)) or not pts_values or len(pts_values) > MAX_FRAMES:
        raise VideoContractError("A bounded nonempty frame PTS list is required.")
    pts = tuple(_integer(value, "Frame PTS") for value in pts_values)
    if pts[0] < start_pts or any(right <= left for left, right in zip(pts, pts[1:])):
        raise VideoContractError("Frame PTS must be strictly increasing in presentation order.")
    if end_pts is not None and pts[-1] >= end_pts:
        raise VideoContractError("The final frame PTS must precede the stream endpoint.")
    if final_duration_ticks is not None:
        _integer(final_duration_ticks, "Final frame duration", minimum=1)
        if end_pts is not None and pts[-1] + final_duration_ticks != end_pts:
            raise VideoContractError("Final frame duration conflicts with the stream endpoint.")
    return pts


def validate_interval_scope(start_pts: int, end_pts: int, timing: TemporalTiming) -> tuple[int, int]:
    """Validate a half-open source interval [start, end) against a known stream end."""
    _integer(start_pts, "Interval start PTS")
    _integer(end_pts, "Interval end PTS")
    if timing.end_pts is None:
        raise VideoContractError("Interval scope needs a verified stream endpoint.")
    if not timing.start_pts <= start_pts < end_pts <= timing.end_pts:
        raise VideoContractError("Interval scope falls outside the source timeline.")
    return start_pts, end_pts


def duration_from_frame_pts(pts_values: Sequence[int], timing: TemporalTiming,
                            *, final_duration_ticks: int | None = None) -> Fraction:
    """Compute exact observed duration only with a validated endpoint or last duration."""
    if len(pts_values) != timing.frame_count:
        raise VideoContractError("Frame PTS count differs from observed frame count.")
    if timing.end_pts is None and final_duration_ticks is None:
        raise VideoContractError("Observed duration needs a stream endpoint or final frame duration.")
    pts = validate_frame_pts(pts_values, start_pts=timing.start_pts,
        end_pts=timing.end_pts, final_duration_ticks=final_duration_ticks)
    end = timing.end_pts if timing.end_pts is not None else pts[-1] + final_duration_ticks
    return (end - timing.start_pts) * timing.time_base.fraction


@dataclass(frozen=True)
class VideoSourceAsset:
    asset_id: str
    sha256: str
    byte_length: int
    stream_id: str
    timing: TemporalTiming
    raw_metadata: Any
    probe_provenance: Mapping[str, Any]
    privacy_disposition: str
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    schema_id: str = SOURCE_SCHEMA_ID

    def __post_init__(self) -> None:
        _integer(self.schema_version, "Source schema version", minimum=1)
        _text(self.schema_id, "Source schema ID", maximum=256)
        _text(self.asset_id, "Asset ID", maximum=256)
        _hash(self.sha256, "Source asset")
        _integer(self.byte_length, "Source byte length", minimum=1)
        _text(self.stream_id, "Stream ID", maximum=256)
        if not isinstance(self.timing, TemporalTiming):
            raise VideoContractError("Source asset needs typed stream timing.")
        _bounded_json(self.raw_metadata, "Raw source metadata")
        _bounded_json(dict(self.probe_provenance), "Probe provenance")
        _text(self.privacy_disposition, "Privacy disposition", maximum=100)
        _bounded_json(dict(self.extra_fields), "Source asset fields")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"schema_version": self.schema_version, "schema_id": self.schema_id,
            "asset_id": self.asset_id, "sha256": self.sha256,
            "byte_length": self.byte_length, "stream_id": self.stream_id,
            "timing": self.timing.to_dict(), "raw_metadata": _bounded_json(self.raw_metadata, "Raw source metadata"),
            "probe_provenance": _bounded_json(dict(self.probe_provenance), "Probe provenance"),
            "privacy_disposition": self.privacy_disposition})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VideoSourceAsset":
        known = {"schema_version", "schema_id", "asset_id", "sha256", "byte_length", "stream_id", "timing", "raw_metadata", "probe_provenance", "privacy_disposition"}
        return cls(value["asset_id"], value["sha256"], value["byte_length"], value["stream_id"],
            TemporalTiming.from_dict(value["timing"]), value["raw_metadata"], value["probe_provenance"],
            value["privacy_disposition"], _fields(value, known, "Source asset"),
            value.get("schema_version", SCHEMA_VERSION), value.get("schema_id", SOURCE_SCHEMA_ID))


@dataclass(frozen=True)
class DerivedFrameReference:
    source_asset_id: str
    source_sha256: str
    stream_id: str
    pts: int
    ordinal: int
    decoder_id: str
    transform_sha256: str
    output_sha256: str
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    schema_id: str = FRAME_SCHEMA_ID

    def __post_init__(self) -> None:
        _integer(self.schema_version, "Frame reference schema version", minimum=1)
        _text(self.schema_id, "Frame reference schema ID", maximum=256)
        _text(self.source_asset_id, "Source asset ID", maximum=256)
        _hash(self.source_sha256, "Source asset")
        _text(self.stream_id, "Stream ID", maximum=256)
        _integer(self.pts, "Frame PTS")
        _integer(self.ordinal, "Presentation ordinal", minimum=0)
        _text(self.decoder_id, "Decoder identity", maximum=512)
        _hash(self.transform_sha256, "Frame transform")
        _hash(self.output_sha256, "Derived frame")
        _bounded_json(dict(self.extra_fields), "Frame reference fields")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"schema_version": self.schema_version, "schema_id": self.schema_id,
            "source_asset_id": self.source_asset_id,
            "source_sha256": self.source_sha256, "stream_id": self.stream_id,
            "pts": self.pts, "ordinal": self.ordinal, "decoder_id": self.decoder_id,
            "transform_sha256": self.transform_sha256, "output_sha256": self.output_sha256})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DerivedFrameReference":
        known = {"schema_version", "schema_id", "source_asset_id", "source_sha256", "stream_id", "pts", "ordinal", "decoder_id", "transform_sha256", "output_sha256"}
        return cls(*(value[key] for key in ("source_asset_id", "source_sha256", "stream_id", "pts", "ordinal", "decoder_id", "transform_sha256", "output_sha256")),
            _fields(value, known, "Frame reference"), value.get("schema_version", SCHEMA_VERSION),
            value.get("schema_id", FRAME_SCHEMA_ID))


@dataclass(frozen=True)
class ImageSourceReference:
    sha256: str
    byte_length: int
    source_state_sha256: str
    transform_sha256: str = ""
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    schema_id: str = IMAGE_SOURCE_SCHEMA_ID

    def __post_init__(self) -> None:
        _integer(self.schema_version, "I2V source schema version", minimum=1)
        _text(self.schema_id, "I2V source schema ID", maximum=256)
        _hash(self.sha256, "I2V source image")
        _integer(self.byte_length, "I2V source byte length", minimum=1)
        _hash(self.source_state_sha256, "I2V source state")
        if self.transform_sha256:
            _hash(self.transform_sha256, "I2V transform")
        _bounded_json(dict(self.extra_fields), "I2V source fields")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"schema_version": self.schema_version, "schema_id": self.schema_id,
            "sha256": self.sha256, "byte_length": self.byte_length,
            "source_state_sha256": self.source_state_sha256, "transform_sha256": self.transform_sha256})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ImageSourceReference":
        known = {"schema_version", "schema_id", "sha256", "byte_length", "source_state_sha256", "transform_sha256"}
        return cls(value["sha256"], value["byte_length"], value["source_state_sha256"],
            value.get("transform_sha256", ""), _fields(value, known, "I2V source"),
            value.get("schema_version", SCHEMA_VERSION), value.get("schema_id", IMAGE_SOURCE_SCHEMA_ID))


@dataclass(frozen=True)
class VideoGenerationRequest:
    task: str
    model_resource_id: str
    positive_prompt: str
    negative_prompt: str
    goal: str
    motion: str
    camera: str
    width: int
    height: int
    frame_count: int
    fps: Rational
    seed: int
    source_image: ImageSourceReference | None = None
    intent: str = "new"
    audio_policy: str = "silent"
    stages: tuple[Mapping[str, Any], ...] = ()
    resources: tuple[str, ...] = ()
    template_version: str = ""
    adapter_version: str = ""
    runtime_version: str = ""
    overrides: Mapping[str, Any] = field(default_factory=dict)
    translation_report: Mapping[str, Any] = field(default_factory=dict)
    expected_output_bytes: int | None = None
    storage_budget_bytes: int | None = None
    schema_version: int = SCHEMA_VERSION
    schema_id: str = SCHEMA_ID
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _integer(self.schema_version, "Video schema version", minimum=1)
        _text(self.schema_id, "Video schema ID", maximum=256)
        _text(self.task, "Video task", maximum=32)
        _hash(self.model_resource_id, "Video model", resource=True)
        for label, value in (("Positive prompt", self.positive_prompt), ("Negative prompt", self.negative_prompt),
                             ("Goal", self.goal), ("Motion", self.motion), ("Camera", self.camera)):
            _text(value, label, required=label == "Positive prompt", maximum=100_000)
        for label, value in (("Width", self.width), ("Height", self.height)):
            _integer(value, label, minimum=64)
            if value > MAX_SIDE:
                raise VideoContractError(f"{label} exceeds the bounded video geometry.")
        if self.width * self.height > MAX_PIXELS:
            raise VideoContractError("Video geometry exceeds the bounded pixel area.")
        _integer(self.frame_count, "Frame count", minimum=1)
        if self.frame_count > MAX_FRAMES:
            raise VideoContractError("Video frame count exceeds the bounded request.")
        if not isinstance(self.fps, Rational) or self.fps.fraction > 240:
            raise VideoContractError("Video FPS needs a positive rational no greater than 240.")
        _integer(self.seed, "Seed", minimum=0)
        if self.seed > 2**63 - 1:
            raise VideoContractError("Seed exceeds the supported integer range.")
        if self.schema_version == SCHEMA_VERSION and self.schema_id == SCHEMA_ID and self.task in {"t2v", "i2v"}:
            if self.task == "i2v" and not isinstance(self.source_image, ImageSourceReference):
                raise VideoContractError("I2V requires one exact source image reference.")
            if self.task == "t2v" and self.source_image is not None:
                raise VideoContractError("T2V cannot silently carry an image source.")
            if self.audio_policy != "silent":
                raise VideoContractError("This video contract admits silent output only.")
        if self.source_image is not None and not isinstance(self.source_image, ImageSourceReference):
            raise VideoContractError("Video source image reference is invalid.")
        if self.intent not in {"new", "variation", "reproduction"}:
            raise VideoContractError("Choose a neutral video intent.")
        if not isinstance(self.stages, (tuple, list)) or len(self.stages) > 32:
            raise VideoContractError("Video stages must be a bounded ordered list.")
        if any(not isinstance(stage, Mapping) for stage in self.stages):
            raise VideoContractError("Each ordered video stage must be an object.")
        _bounded_json(list(self.stages), "Video stages")
        if not isinstance(self.resources, (tuple, list)) or len(self.resources) > 64:
            raise VideoContractError("Video resources must be bounded.")
        for resource in self.resources:
            _hash(resource, "Video companion", resource=True)
        for label, value in (("Template version", self.template_version), ("Adapter version", self.adapter_version),
                             ("Runtime version", self.runtime_version)):
            _text(value, label, required=False, maximum=256)
        _bounded_json(dict(self.overrides), "Video overrides")
        _bounded_json(dict(self.translation_report), "Video translation report")
        for label, value in (("Expected output bytes", self.expected_output_bytes),
                             ("Storage budget bytes", self.storage_budget_bytes)):
            if value is not None:
                _integer(value, label, minimum=1)
        _bounded_json(dict(self.extra_fields), "Unknown video request fields")

    @property
    def requested_duration(self) -> Fraction:
        """Planned constant-frame-rate duration; never substitutes for decoded stream duration."""
        return Fraction(self.frame_count, 1) / self.fps.fraction

    @property
    def schema_supported(self) -> bool:
        return (self.schema_version == SCHEMA_VERSION and self.schema_id == SCHEMA_ID
                and self.task in {"t2v", "i2v"} and self.audio_policy == "silent")

    def to_dict(self) -> dict[str, Any]:
        return _merged(self.extra_fields, {"schema_version": self.schema_version,
            "schema_id": self.schema_id, "task": self.task, "model_resource_id": self.model_resource_id,
            "positive_prompt": self.positive_prompt, "negative_prompt": self.negative_prompt,
            "goal": self.goal, "motion": self.motion, "camera": self.camera,
            "width": self.width, "height": self.height, "frame_count": self.frame_count,
            "fps": self.fps.to_dict(), "seed": self.seed,
            "source_image": self.source_image.to_dict() if self.source_image else None,
            "intent": self.intent, "audio_policy": self.audio_policy,
            "stages": _bounded_json(list(self.stages), "Video stages"), "resources": list(self.resources),
            "template_version": self.template_version, "adapter_version": self.adapter_version,
            "runtime_version": self.runtime_version,
            "overrides": _bounded_json(dict(self.overrides), "Video overrides"),
            "translation_report": _bounded_json(dict(self.translation_report), "Video translation report"),
            "expected_output_bytes": self.expected_output_bytes,
            "storage_budget_bytes": self.storage_budget_bytes})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VideoGenerationRequest":
        known = {"schema_version", "schema_id", "task", "model_resource_id", "positive_prompt",
            "negative_prompt", "goal", "motion", "camera", "width", "height", "frame_count", "fps",
            "seed", "source_image", "intent", "audio_policy", "stages", "resources", "template_version",
            "adapter_version", "runtime_version", "overrides", "translation_report",
            "expected_output_bytes", "storage_budget_bytes"}
        return cls(task=value["task"], model_resource_id=value["model_resource_id"],
            positive_prompt=value["positive_prompt"], negative_prompt=value["negative_prompt"],
            goal=value["goal"], motion=value["motion"], camera=value["camera"],
            width=value["width"], height=value["height"], frame_count=value["frame_count"],
            fps=Rational.from_dict(value["fps"]), seed=value["seed"],
            source_image=ImageSourceReference.from_dict(value["source_image"]) if value.get("source_image") is not None else None,
            intent=value.get("intent", "new"), audio_policy=value.get("audio_policy", "silent"),
            stages=tuple(value.get("stages", ())), resources=tuple(value.get("resources", ())),
            template_version=value.get("template_version", ""), adapter_version=value.get("adapter_version", ""),
            runtime_version=value.get("runtime_version", ""), overrides=value.get("overrides", {}),
            translation_report=value.get("translation_report", {}),
            expected_output_bytes=value.get("expected_output_bytes"),
            storage_budget_bytes=value.get("storage_budget_bytes"),
            schema_version=value.get("schema_version", SCHEMA_VERSION),
            schema_id=value.get("schema_id", SCHEMA_ID), extra_fields=_fields(value, known, "Video request"))
