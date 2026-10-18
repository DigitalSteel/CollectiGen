"""Pure masked-remix data contract and protected-pixel compositing.

The full-resolution grayscale mask uses 0 for protected source pixels and 255
for editable pixels. Feathering is inward only: every zero-mask pixel keeps its
exact decoded RGBA source value. This is a local planning/result helper, not an
admitted masked-generation adapter or an authorization to write files.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import re

from PIL import Image, ImageChops, ImageFilter


MASK_OPERATION_VERSION = 1
CROP_OPERATION_VERSION = 2
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_FEATHER_PX = 128
MAX_CROP_PADDING_PX = 1024
MAX_MASK_DOCUMENT_BYTES = 64 * 1024 * 1024
MASK_DOCUMENT_VERSION = 1


def _open_image(data: bytes, label: str, *, png_only=False):
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{label} bytes are missing or exceed the supported size.")
    try:
        image = Image.open(BytesIO(data))
        if png_only and image.format != "PNG":
            raise ValueError(f"{label} must be a PNG image.")
        width, height = image.size
        if (not 0 < width <= MAX_IMAGE_SIDE or not 0 < height <= MAX_IMAGE_SIDE
                or width * height > MAX_IMAGE_PIXELS):
            raise ValueError(f"{label} exceeds the supported image dimensions.")
        image.load()
        return image
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"{label} cannot be decoded safely.") from exc


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", compress_level=6)
    return output.getvalue()


@dataclass(frozen=True)
class MaskPayload:
    source_sha256: str
    mask_sha256: str
    width: int
    height: int
    mask_png: bytes
    feather_px: int
    operation_version: int = MASK_OPERATION_VERSION
    crop_region: tuple[int, int, int, int] | None = None
    crop_padding_px: int = 0

    def to_dict(self):
        """Return JSON-safe lineage data without copying the source image."""
        result = {"source_sha256": self.source_sha256, "mask_sha256": self.mask_sha256,
                "width": self.width, "height": self.height,
                "mask_png_base64": b64encode(self.mask_png).decode("ascii"),
                "feather_px": self.feather_px, "operation_version": self.operation_version}
        if self.operation_version == CROP_OPERATION_VERSION:
            result.update(crop_region=list(self.crop_region), crop_padding_px=self.crop_padding_px)
        return result

    @classmethod
    def from_dict(cls, value):
        try:
            mask = b64decode(value["mask_png_base64"], validate=True)
            version = value["operation_version"]
            region = value.get("crop_region")
            if version == CROP_OPERATION_VERSION:
                if not isinstance(region, (list, tuple)) or len(region) != 4:
                    raise ValueError("The saved crop region is invalid.")
                region = tuple(region)
            elif region is not None or value.get("crop_padding_px", 0) != 0:
                raise ValueError("A legacy mask cannot contain crop context.")
            payload = cls(str(value["source_sha256"]), str(value["mask_sha256"]),
                          value["width"], value["height"], mask,
                          value["feather_px"], version, region,
                          value.get("crop_padding_px", 0))
            validate_mask_payload(payload)
            return payload
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("The saved mask payload is invalid.") from exc


def _crop_region(mask: Image.Image, padding: int) -> tuple[int, int, int, int]:
    bounds = mask.getbbox()
    if bounds is None:
        raise ValueError("Paint at least one editable area before applying the mask.")
    return (max(0, bounds[0] - padding), max(0, bounds[1] - padding),
            min(mask.width, bounds[2] + padding), min(mask.height, bounds[3] + padding))


def create_mask_payload(source_bytes: bytes, mask_png: bytes, *, feather_px=0,
                        crop_context=False, crop_padding_px=0) -> MaskPayload:
    source = _open_image(source_bytes, "Source image")
    mask = _open_image(mask_png, "Mask", png_only=True)
    if mask.mode != "L":
        raise ValueError("The mask must be an 8-bit grayscale PNG.")
    if mask.size != source.size:
        raise ValueError("The mask must match the full source image dimensions.")
    if mask.getextrema()[1] == 0:
        raise ValueError("Paint at least one editable area before applying the mask.")
    if type(feather_px) is not int or not 0 <= feather_px <= MAX_FEATHER_PX:
        raise ValueError("Use an inward feather from 0 to 128 pixels.")
    if type(crop_context) is not bool or type(crop_padding_px) is not int or not 0 <= crop_padding_px <= MAX_CROP_PADDING_PX:
        raise ValueError("Use crop context with padding from 0 to 1024 source pixels.")
    if not crop_context and crop_padding_px:
        raise ValueError("Crop padding requires crop context.")
    canonical_mask = _png(mask)
    region = _crop_region(mask, crop_padding_px) if crop_context else None
    return MaskPayload(sha256(source_bytes).hexdigest(), sha256(canonical_mask).hexdigest(),
                       source.width, source.height, canonical_mask, feather_px,
                       CROP_OPERATION_VERSION if crop_context else MASK_OPERATION_VERSION,
                       region, crop_padding_px)


def validate_mask_payload(payload: MaskPayload, *, source_bytes: bytes | None = None) -> None:
    if (not isinstance(payload, MaskPayload) or type(payload.operation_version) is not int
            or payload.operation_version not in {MASK_OPERATION_VERSION, CROP_OPERATION_VERSION}):
        raise ValueError("This mask operation version is unsupported.")
    if (not isinstance(payload.source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload.source_sha256)
            or not isinstance(payload.mask_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload.mask_sha256)):
        raise ValueError("The mask or source identity is invalid.")
    if (type(payload.width) is not int or type(payload.height) is not int
            or not 0 < payload.width <= MAX_IMAGE_SIDE or not 0 < payload.height <= MAX_IMAGE_SIDE
            or payload.width * payload.height > MAX_IMAGE_PIXELS):
        raise ValueError("The saved mask dimensions are invalid.")
    if type(payload.feather_px) is not int or not 0 <= payload.feather_px <= MAX_FEATHER_PX:
        raise ValueError("The mask feather is invalid.")
    mask = _open_image(payload.mask_png, "Mask", png_only=True)
    if mask.mode != "L" or mask.size != (payload.width, payload.height):
        raise ValueError("The saved mask dimensions or format changed.")
    if mask.getextrema()[1] == 0:
        raise ValueError("The saved mask has no editable pixels.")
    if sha256(payload.mask_png).hexdigest() != payload.mask_sha256:
        raise ValueError("The saved mask bytes changed.")
    if payload.operation_version == MASK_OPERATION_VERSION:
        if payload.crop_region is not None or payload.crop_padding_px != 0:
            raise ValueError("A legacy mask cannot contain crop context.")
    else:
        if (type(payload.crop_padding_px) is not int
                or not 0 <= payload.crop_padding_px <= MAX_CROP_PADDING_PX
                or not isinstance(payload.crop_region, tuple)
                or len(payload.crop_region) != 4
                or any(type(value) is not int for value in payload.crop_region)):
            raise ValueError("The saved crop region or padding is invalid.")
        if payload.crop_region != _crop_region(mask, payload.crop_padding_px):
            raise ValueError("The saved crop region differs from the exact painted area.")
    if source_bytes is not None:
        source = _open_image(source_bytes, "Source image")
        if (source.size != mask.size or sha256(source_bytes).hexdigest() != payload.source_sha256):
            raise ValueError("The source image no longer matches this mask.")


def export_mask_document(payload: MaskPayload, source_bytes: bytes) -> bytes:
    """Serialize one reusable mask bound to the exact source PNG bytes."""
    validate_mask_payload(payload, source_bytes=source_bytes)
    raw = json.dumps({"format": "collectigen-mask", "version": MASK_DOCUMENT_VERSION,
                      "payload": payload.to_dict()}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_MASK_DOCUMENT_BYTES:
        raise ValueError("The mask document exceeds the supported size.")
    return raw


def import_mask_document(raw: bytes, source_bytes: bytes) -> MaskPayload:
    """Read only the exact documented fields and reject another source image."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_MASK_DOCUMENT_BYTES:
        raise ValueError("The mask document is missing or exceeds the supported size.")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The mask document is not valid UTF-8 JSON.") from exc
    if (not isinstance(document, dict) or set(document) != {"format", "version", "payload"}
            or document["format"] != "collectigen-mask"
            or type(document["version"]) is not int
            or document["version"] != MASK_DOCUMENT_VERSION):
        raise ValueError("The mask document format or version is unsupported.")
    fields = {"source_sha256", "mask_sha256", "width", "height",
              "mask_png_base64", "feather_px", "operation_version"}
    optional = {"crop_region", "crop_padding_px"}
    if (not isinstance(document["payload"], dict)
            or set(document["payload"]) - fields - optional
            or not fields.issubset(document["payload"])):
        raise ValueError("The mask document contains missing or unsupported fields.")
    payload = MaskPayload.from_dict(document["payload"])
    validate_mask_payload(payload, source_bytes=source_bytes)
    return payload


def prepare_masked_input(source_bytes: bytes, payload: MaskPayload) -> dict[str, object]:
    """Return exact PNG inputs in source coordinates or a verified crop context."""
    validate_mask_payload(payload, source_bytes=source_bytes)
    source = _open_image(source_bytes, "Source image").convert("RGBA")
    mask = _open_image(payload.mask_png, "Mask", png_only=True)
    region = payload.crop_region or (0, 0, payload.width, payload.height)
    crop_source = source.crop(region)
    crop_mask = mask.crop(region)
    return {"source_png": _png(crop_source), "mask_png": _png(crop_mask),
            "width": crop_source.width, "height": crop_source.height,
            "crop_region": region}


def composite_masked_result(source_bytes: bytes, generated_bytes: bytes, payload: MaskPayload) -> bytes:
    """Return PNG with protected source pixels intact; never writes a file."""
    validate_mask_payload(payload, source_bytes=source_bytes)
    source = _open_image(source_bytes, "Source image").convert("RGBA")
    generated = _open_image(generated_bytes, "Generated image")
    region = payload.crop_region or (0, 0, payload.width, payload.height)
    if generated.size != (region[2] - region[0], region[3] - region[1]):
        raise ValueError("The generated result must match the source dimensions or exact crop context.")
    if payload.crop_region is not None:
        expanded = source.copy()
        expanded.paste(generated.convert("RGBA"), region[:2])
        generated = expanded
    mask = _open_image(payload.mask_png, "Mask", png_only=True)
    if payload.feather_px:
        # Multiplication prevents blur from leaking into any black protected pixel.
        mask = ImageChops.multiply(mask, mask.filter(ImageFilter.GaussianBlur(payload.feather_px)))
    return _png(Image.composite(generated.convert("RGBA"), source, mask))
