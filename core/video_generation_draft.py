"""Exclusive, verified publication of a user-chosen typed video draft."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .generation_transport import strict_json
from .training_source_io import _assert_unredirected, read_stable_training_source
from .video_generation import MAX_JSON_BYTES, VideoGenerationRequest


DRAFT_SCHEMA_ID = "collectigen.video-draft.v1"
AUTHORING_SCHEMA_ID = "collectigen.video-draft-authoring.v1"


@dataclass(frozen=True)
class VideoAuthoringDraft:
    """Non-executable, bounded controls for an unfinished video workspace."""

    controls: Mapping[str, object]
    baseline: VideoGenerationRequest | None = None

    def __post_init__(self):
        values = self.controls
        expected = {"task", "positive", "negative", "motion", "camera", "append_intent",
            "width", "height", "frames", "fps", "seed", "final_width", "final_height",
            "detail_preference", "output_cap_mib", "reserve_mib", "models", "source_image"}
        if not isinstance(values, Mapping) or set(values) != expected:
            raise ValueError("The unfinished video draft has invalid controls.")
        if values["task"] not in {"t2v", "i2v"} or type(values["append_intent"]) is not bool:
            raise ValueError("The unfinished video task or prompt option is invalid.")
        for key, maximum in (("positive", 100_000), ("negative", 100_000),
                             ("motion", 100_000), ("camera", 100_000), ("seed", 256)):
            value = values[key]
            if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
                raise ValueError(f"The unfinished video {key} is invalid.")
        for key, lower, upper in (("width", 32, 4096), ("height", 32, 4096),
                                  ("frames", 1, 512), ("fps", 1, 120),
                                  ("final_width", 0, 4096), ("final_height", 0, 4096),
                                  ("output_cap_mib", 1, 256), ("reserve_mib", 2, 4096)):
            value = values[key]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"The unfinished video {key} is invalid.")
        if values["detail_preference"] not in {"faithful", "generative"}:
            raise ValueError("The unfinished video detail preference is invalid.")
        models = values["models"]
        if not isinstance(models, Mapping) or set(models) != {"transformer", "text_encoder", "vae"}:
            raise ValueError("The unfinished video model choices are invalid.")
        from .video_generation import _hash
        for identity in models.values():
            if identity:
                _hash(identity, "Video model", resource=True)
            elif identity != "":
                raise ValueError("The unfinished video model choice is invalid.")
        source = values["source_image"]
        if values["task"] == "t2v" and source is not None:
            raise ValueError("A text-to-video authoring draft cannot retain an image source.")
        if source is not None:
            from .video_generation import ImageSourceReference
            ImageSourceReference.from_dict(source)
        if self.baseline is not None and not isinstance(self.baseline, VideoGenerationRequest):
            raise TypeError("An unfinished video baseline must be an exact request.")
        json.dumps(values, ensure_ascii=False, allow_nan=False)
        object.__setattr__(self, "controls", deepcopy(dict(values)))

    def to_dict(self):
        return {"controls": deepcopy(dict(self.controls)),
            "baseline": self.baseline.to_dict() if self.baseline is not None else None}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"controls", "baseline"}:
            raise ValueError("The unfinished video draft is invalid.")
        baseline = value["baseline"]
        return cls(value["controls"], VideoGenerationRequest.from_dict(baseline)
                   if baseline is not None else None)


def scoped_video_draft_path(directory: Path, path: str | Path) -> Path:
    """Accept only an explicit direct child of the admitted draft directory."""
    directory = Path(directory)
    target = Path(path).absolute()
    if not directory.is_absolute() or target.parent != directory or ":" in target.name or target.suffix.casefold() != ".json":
        raise ValueError("Choose a .json draft in this workspace's video draft folder.")
    _assert_unredirected(target)
    return target


def save_video_draft(path: str | Path, request: VideoGenerationRequest | VideoAuthoringDraft) -> Path:
    """Create a complete new draft, refusing a raced destination or redirect."""
    if not isinstance(request, (VideoGenerationRequest, VideoAuthoringDraft)):
        raise TypeError("A video request or unfinished authoring draft is required.")
    target = Path(path).absolute()
    if target.suffix.casefold() != ".json":
        raise ValueError("Choose a .json draft filename.")
    _assert_unredirected(target)
    if not target.parent.is_dir():
        raise FileNotFoundError("Choose an existing draft directory.")
    payload = ({"schema_id": DRAFT_SCHEMA_ID, "request": request.to_dict()}
        if isinstance(request, VideoGenerationRequest) else
        {"schema_id": AUTHORING_SCHEMA_ID, "authoring": request.to_dict()})
    raw = (json.dumps(payload,
            ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("The video draft exceeds the supported size.")
    _parse_video_draft(raw)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_unredirected(target)
        if os.name == "nt":
            os.rename(temporary, target)  # Windows refuses an existing target.
        else:
            os.link(temporary, target)  # Atomic exclusive publish on POSIX.
            temporary.unlink()
        verified, _ = read_stable_training_source(target, max_bytes=MAX_JSON_BYTES)
        if verified != raw:
            raise RuntimeError("The saved video draft failed read-back verification.")
        return target
    finally:
        if temporary.exists():
            temporary.unlink()


def load_video_draft(path: str | Path) -> VideoGenerationRequest | VideoAuthoringDraft:
    raw, _ = read_stable_training_source(path, max_bytes=MAX_JSON_BYTES)
    return _parse_video_draft(raw)


def _parse_video_draft(raw: bytes) -> VideoGenerationRequest | VideoAuthoringDraft:
    try:
        payload = strict_json(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("The video draft contains invalid or ambiguous JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("This is not a video draft.")
    if payload.get("schema_id") == DRAFT_SCHEMA_ID:
        if set(payload) != {"schema_id", "request"} or not isinstance(payload["request"], dict):
            raise ValueError("The video draft has an invalid request envelope.")
        return VideoGenerationRequest.from_dict(payload["request"])
    if payload.get("schema_id") == AUTHORING_SCHEMA_ID:
        if set(payload) != {"schema_id", "authoring"} or not isinstance(payload["authoring"], dict):
            raise ValueError("The video draft has an invalid authoring envelope.")
        return VideoAuthoringDraft.from_dict(payload["authoring"])
    raise ValueError("This is not a video draft.")
