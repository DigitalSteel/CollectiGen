"""Read-only display representation for explicit description analysis.

No family/sidecar reads, resizing, derivative publication or metadata transfer.
This follows existing RGB analysis conventions, not color-managed fidelity.
"""
from dataclasses import dataclass
import hashlib
import io

from PIL import Image, ImageOps, __version__ as PILLOW_VERSION

from core.image_derivatives import _decode, MAX_SOURCE_BYTES
from core.lmstudio_description import MAX_IMAGE_BYTES
from core.training_source_io import read_stable_training_source

PREPARATION_ID = "collectigen.lmstudio.display-rgb-png.v1"


class ImagePreparationError(ValueError):
    pass


def check_cancelled(should_cancel):
    stopped = False
    if should_cancel is not None:
        if not callable(should_cancel):
            raise ImagePreparationError("invalid_cancellation")
        try:
            stopped = bool(should_cancel())
        except Exception:
            stopped = True
    if stopped:
        raise InterruptedError("image_preparation_cancelled")


def _read_source(path):
    code = None
    try:
        raw, identity = read_stable_training_source(path, max_bytes=MAX_SOURCE_BYTES)
    except ValueError:
        code = "source_byte_limit"
    except RuntimeError:
        code = "source_identity_unavailable"
    except Exception:
        code = "source_unavailable"
    if code is not None:
        raise ImagePreparationError(code)
    return raw, identity


class _BoundedPNG(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_IMAGE_BYTES:
            raise ImagePreparationError("prepared_image_too_large")
        return super().write(value)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedImage:
    encoded: bytes
    mime: str
    source_sha256: str
    source_bytes: int
    source_mtime_ns: int
    payload_sha256: str
    source_size: tuple[int, int]
    display_size: tuple[int, int]
    source_format: str
    source_mode: str
    orientation: int | None
    alpha_composited: bool

    def audit(self):
        return dict(recipe=PREPARATION_ID, pillow_version=PILLOW_VERSION,
                    source_sha256=self.source_sha256, payload_sha256=self.payload_sha256,
                    source_bytes=self.source_bytes, source_size=list(self.source_size),
                    display_size=list(self.display_size), source_format=self.source_format,
                    source_mode=self.source_mode, source_orientation=self.orientation,
                    alpha_background="white" if self.alpha_composited else None,
                    output_format="PNG", output_mode="RGB", metadata_copied=False)


def prepare_image(path, *, should_cancel=None):
    check_cancelled(should_cancel)
    raw, identity = _read_source(path)
    check_cancelled(should_cancel)
    owned = []
    code = None
    result = None

    def own(image):
        owned.append(image)
        return image

    try:
        source = own(_decode(raw))
        check_cancelled(should_cancel)
        source_size, source_format, source_mode = source.size, source.format, source.mode
        orientation = source.getexif().get(274, 1)
        if type(orientation) is not int or orientation not in range(1, 9):
            orientation = None
        display = own(ImageOps.exif_transpose(source))
        alpha = display.mode in {"RGBA", "LA"}
        if alpha:
            rgba = own(display.convert("RGBA"))
            background = own(Image.new("RGBA", display.size, "WHITE"))
            background.alpha_composite(rgba)
            pixels = own(background.convert("RGB"))
        else:
            pixels = own(display.convert("RGB"))
        clean = own(Image.new("RGB", pixels.size))
        clean.paste(pixels)
        check_cancelled(should_cancel)
        with _BoundedPNG() as buffer:
            clean.save(buffer, format="PNG")
            encoded = buffer.getvalue()
        check_cancelled(should_cancel)
        result = PreparedImage(
            encoded, "image/png", identity["sha256"], identity["size"],
            identity["mtime_ns"], hashlib.sha256(encoded).hexdigest(),
            tuple(source_size), tuple(clean.size), source_format, source_mode,
            orientation, alpha,
        )
    except InterruptedError:
        code = "image_preparation_cancelled"
    except ImagePreparationError as error:
        code = "prepared_image_too_large" if str(error) == "prepared_image_too_large" else "image_preparation_failed"
    except Exception:
        code = "image_preparation_failed"
    finally:
        for image in reversed(owned):
            try:
                image.close()
            except Exception:
                code = "image_cleanup_failed"
        owned.clear()
        raw = None
    if code == "image_preparation_cancelled":
        raise InterruptedError(code)
    if code is not None:
        result = None
        raise ImagePreparationError(code)
    return result


def verify_source_current(path, prepared, *, should_cancel=None):
    check_cancelled(should_cancel)
    _raw, identity = _read_source(path)
    check_cancelled(should_cancel)
    if (identity["sha256"], identity["size"], identity["mtime_ns"]) != (
            prepared.source_sha256, prepared.source_bytes, prepared.source_mtime_ns):
        raise ImagePreparationError("source_changed")
