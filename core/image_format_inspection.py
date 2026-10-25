"""Read-only EDIT-01 inspection of explicitly supplied image bytes.

Findings are preference comparisons, never codec qualification or permission to
convert. No files, profiles, companions, network services or writers are opened.
Pillow's decoded mode is not a claim about original encoded precision or complete
Metadata preservation. Original bytes and unknown fields remain the caller's
unchanged evidence; the existing derivative/recovery owner must admit any write.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Callable

from PIL import Image, __version__ as PILLOW_VERSION

from .image_derivatives import MAX_PIXELS, MAX_SOURCE_BYTES


_TARGETS = frozenset({"PNG", "JPEG", "WEBP", "TIFF", "AVIF"})
_INITIAL_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
_INITIAL_MODES = frozenset({"RGB", "RGBA", "L", "LA"})


class FormatInspectionError(ValueError):
    """A bounded inspection could not establish a trustworthy observation."""


@dataclass(frozen=True)
class FormatInspection:
    source_sha256: str
    source_bytes: int
    actual_format: str
    decoded_mode: str
    width: int
    height: int
    multiple_frames: bool | None
    single_frame_pixels_verified: bool
    has_alpha_channel_or_transparency: bool
    decoded_info_keys: tuple[str, ...]
    preference: str
    status: str
    reasons: tuple[str, ...]
    target_encoder_installed: bool | None
    pillow_version: str
    # These remain false even for a successful same-format observation.
    conversion_qualified: bool = False
    metadata_preservation_assessed: bool = False
    encoded_precision_assessed: bool = False


def inspect_image_format(
    raw: bytes,
    preference: str = "keep-original",
    *,
    should_continue: Callable[[], bool] | None = None,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_pixels: int = MAX_PIXELS,
) -> FormatInspection:
    """Inspect one bounded immutable input; raise InterruptedError on cancel.

    No extension is accepted: actual format comes from Pillow identification.
    No frame seek is performed. PNG/WebP animation flags must already exist in
    codec-open metadata; other unqualified formats retain unknown frame count.
    Supported single-frame inputs are verified, reopened and fully decoded. Cancellation is checked between codec
    calls; an individual synchronous Pillow call cannot be interrupted here.
    """
    if type(raw) is not bytes:
        raise TypeError("Supply immutable, explicitly selected image bytes.")
    if preference != "keep-original" and preference not in _TARGETS:
        raise ValueError("Choose keep-original, PNG, JPEG, WEBP, TIFF or AVIF.")
    for value, ceiling in ((max_source_bytes, MAX_SOURCE_BYTES), (max_pixels, MAX_PIXELS)):
        if type(value) is not int or not 0 < value <= ceiling:
            raise ValueError("Inspection limits must be positive and cannot exceed derivative limits.")
    if not raw or len(raw) > max_source_bytes:
        raise FormatInspectionError("source_byte_limit")

    def checkpoint():
        if should_continue is not None and not should_continue():
            raise InterruptedError("Image-format inspection cancelled.")

    checkpoint()
    Image.init()
    target_encoder = None if preference == "keep-original" else preference in Image.SAVE
    checkpoint()
    try:
        with Image.open(BytesIO(raw)) as image:
            actual_format, mode = image.format, image.mode
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise FormatInspectionError("source_pixel_limit")
            alpha = "A" in image.getbands() or "transparency" in image.info
            info_keys = tuple(sorted(str(key) for key in image.info))
            # A seek may allocate a later canvas before any new size check.
            # Pillow's PNG/WebP _open sets this boolean without a frame seek by
            # this caller. Do not invoke lazy n_frames/is_animated properties on
            # unqualified formats or enumerate frames to guess a count.
            if actual_format == "JPEG":
                multiple_frames = False
            elif actual_format in {"PNG", "WEBP"}:
                animation = vars(image).get("is_animated")
                multiple_frames = animation if type(animation) is bool else None
            else:
                multiple_frames = None
        checkpoint()
        reasons = []
        if multiple_frames is None:
            reasons.append("frame_count_unassessed")
        elif multiple_frames:
            reasons.append("multiple_frames_inspection_only")
        if actual_format not in _INITIAL_FORMATS:
            reasons.append("source_format_inspection_only")
        if mode not in _INITIAL_MODES:
            reasons.append("source_mode_inspection_only")
        single_frame_pixels_verified = not reasons
        if single_frame_pixels_verified:
            with Image.open(BytesIO(raw)) as verified:
                verified.verify()
            checkpoint()
            with Image.open(BytesIO(raw)) as decoded:
                decoded.load()
                if (decoded.format, decoded.mode, decoded.size) != (actual_format, mode, (width, height)):
                    raise FormatInspectionError("source_decode_identity_changed")
            checkpoint()
    except (InterruptedError, FormatInspectionError):
        raise
    except Exception as error:
        # Do not expose source Metadata or raw decoder diagnostics as UI text.
        raise FormatInspectionError("source_decode_failed") from error

    if preference == "keep-original":
        status = "keep-original"
    elif reasons:
        status = "needs-review"
    elif preference == actual_format:
        status = "matches-preference"
    elif preference not in _INITIAL_FORMATS:
        status = "format-unavailable"
        reasons.append("target_variant_not_qualified")
    elif not target_encoder:
        status = "format-unavailable"
        reasons.append("target_encoder_unavailable")
    elif preference == "JPEG" and alpha:
        status = "needs-review"
        reasons.append("alpha_requires_explicit_review")
    else:
        status = "different-format"
    checkpoint()
    return FormatInspection(
        sha256(raw).hexdigest(), len(raw), actual_format or "unknown", mode,
        width, height, multiple_frames, single_frame_pixels_verified, alpha, info_keys,
        preference, status, tuple(reasons), target_encoder, PILLOW_VERSION,
    )
