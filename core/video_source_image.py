"""Pure, bounded conversion of one selected still for native I2V upload.

The original bytes remain authoritative. The returned PNG is a reproducible
derived input, never a replacement for the selected image or its evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO

from PIL import Image, ImageOps


MAX_ORIGINAL_BYTES = 32 * 1024 * 1024
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 16_777_216
RECIPE_ID = "collectigen.video-source-png.v1"
_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


@dataclass(frozen=True)
class VideoSourceImage:
    original_sha256: str
    png_sha256: str
    png_bytes: bytes
    original_format: str
    original_size: tuple[int, int]
    png_size: tuple[int, int]
    recipe: str

    def lineage(self) -> dict:
        return {
            "original_image_sha256": self.original_sha256,
            "source_png_sha256": self.png_sha256,
            "original_format": self.original_format,
            "original_size": list(self.original_size),
            "source_png_size": list(self.png_size),
            "conversion_recipe": self.recipe,
        }


def prepare_video_source(raw: bytes, suffix: str) -> VideoSourceImage:
    """Validate selected bytes and make deterministic PNG without file I/O.

    JPEG/WebP conversion applies EXIF orientation, retains RGB or RGBA pixels,
    and strips nonpixel metadata. PNG input is verified and left byte-identical.
    """
    expected = _FORMATS.get(str(suffix).casefold())
    if expected is None:
        raise ValueError("Choose a PNG, JPEG or WebP source image.")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ORIGINAL_BYTES:
        raise ValueError("The selected source exceeds the bounded image limit.")
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != expected or getattr(image, "n_frames", 1) != 1:
                raise ValueError("The selected image format differs from its extension or is animated.")
            original_size = image.size
            if not all(0 < value <= 8192 for value in original_size) or image.width * image.height > MAX_PIXELS:
                raise ValueError("The selected image dimensions exceed the video source limit.")
            image.load()
            if expected == "PNG":
                orientation = image.getexif().get(274, 1)
                if orientation not in (None, 1):
                    raise ValueError("This PNG has EXIF orientation. Rotate and save it as a new PNG before video generation so preview and native crop agree.")
                png = raw
                png_size = original_size
                recipe = "identity.png.v1"
            else:
                oriented = ImageOps.exif_transpose(image)
                pixels = oriented.convert("RGBA" if "A" in oriented.getbands() else "RGB")
                png_size = pixels.size
                output = BytesIO()
                pixels.save(output, format="PNG", optimize=False)
                png = output.getvalue()
                recipe = RECIPE_ID + ".exif-transpose.rgb-or-rgba.strip-metadata"
    except (OSError, SyntaxError) as exc:
        raise ValueError("The selected image could not be decoded as a stable still.") from exc
    if len(png) > MAX_PNG_BYTES:
        raise ValueError("The derived PNG exceeds the 4 MiB native video source limit; the original is unchanged.")
    return VideoSourceImage(sha256(raw).hexdigest(), sha256(png).hexdigest(), png,
                            expected, original_size, png_size, recipe)
