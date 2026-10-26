"""Bounded, exact local image bytes for a reviewed generation input."""
from __future__ import annotations

import base64
import hashlib
import io
import re
from collections.abc import Mapping

from PIL import Image, UnidentifiedImageError


MAX_INIT_IMAGE_BYTES = 4 * 1024 * 1024


def freeze_init_image(provider, expected_sha256: str) -> dict[str, object]:
    """Read stable bytes through the caller, then verify image and full identity."""
    if not callable(provider) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Select one exact local source image for Remix.")
    supplied = provider()
    if not isinstance(supplied, Mapping):
        raise ValueError("The source provider did not return stable image bytes and identity.")
    raw = supplied.get("bytes")
    claimed = supplied.get("sha256")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INIT_IMAGE_BYTES:
        raise ValueError("The source image must be a nonempty image of at most 4 MiB for this local adapter.")
    actual = hashlib.sha256(raw).hexdigest()
    if claimed != actual or actual != expected_sha256:
        raise ValueError("The source image bytes changed or differ from the reviewed identity.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError("This local Remix adapter accepts one PNG or JPEG source image.")
            if image.width * image.height > 16_777_216:
                raise ValueError("The source image is too large for bounded local Remix review.")
            media_type = "image/png" if image.format == "PNG" else "image/jpeg"
            width, height = image.size
            image.verify()
    except UnidentifiedImageError as exc:
        raise ValueError("This local Remix adapter accepts one PNG or JPEG source image.") from exc
    return {"sha256": actual, "size": len(raw), "width": width, "height": height, "data_url":
            f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"}
