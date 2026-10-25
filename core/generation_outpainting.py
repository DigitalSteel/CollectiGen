"""Exact source-preserving canvas expansion for a reviewed image request."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from hashlib import sha256
from io import BytesIO
import re

from PIL import Image

from .generation_masks import create_mask_payload


@dataclass(frozen=True)
class CanvasExpansion:
    source_sha256: str
    source_width: int
    source_height: int
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0
    version: int = 1
    fill: str = "edge"

    def __post_init__(self):
        if self.version != 1 or self.fill != "edge" or not re.fullmatch(r"[a-f0-9]{64}", self.source_sha256):
            raise ValueError("Unsupported canvas expansion or source identity.")
        if any(type(value) is not int or not 1 <= value <= 2048 for value in (self.source_width, self.source_height)):
            raise ValueError("Canvas source dimensions are outside the supported range.")
        borders = (self.left, self.top, self.right, self.bottom)
        if any(type(value) is not int or not 0 <= value <= 1024 for value in borders) or not any(borders):
            raise ValueError("Add between 1 and 1024 pixels on at least one canvas edge.")
        if self.width > 2048 or self.height > 2048 or self.width % 8 or self.height % 8:
            raise ValueError("Expanded canvas must fit 2048 pixels per side and whole blocks of eight.")

    @property
    def width(self): return self.source_width + self.left + self.right

    @property
    def height(self): return self.source_height + self.top + self.bottom

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("The saved canvas expansion contains unsupported fields.")
        return cls(**value)


def expanded_canvas(source_bytes, expansion: CanvasExpansion):
    if not isinstance(source_bytes, bytes) or len(source_bytes) > 4 * 1024 * 1024 or sha256(source_bytes).hexdigest() != expansion.source_sha256:
        raise ValueError("The source changed before canvas expansion.")
    with Image.open(BytesIO(source_bytes)) as source:
        if source.size != (expansion.source_width, expansion.source_height) or getattr(source, "n_frames", 1) != 1:
            raise ValueError("Canvas expansion source dimensions changed.")
        source = source.convert("RGBA")
    # Repeat boundary pixels into new context without scaling any original pixel.
    canvas = Image.new("RGBA", (expansion.width, expansion.height))
    canvas.paste(source, (expansion.left, expansion.top))
    if expansion.left:
        canvas.paste(source.crop((0, 0, 1, source.height)).resize((expansion.left, source.height)), (0, expansion.top))
    if expansion.right:
        canvas.paste(source.crop((source.width - 1, 0, source.width, source.height)).resize((expansion.right, source.height)),
            (expansion.left + source.width, expansion.top))
    if expansion.top:
        canvas.paste(canvas.crop((0, expansion.top, canvas.width, expansion.top + 1)).resize((canvas.width, expansion.top)), (0, 0))
    if expansion.bottom:
        canvas.paste(canvas.crop((0, expansion.top + source.height - 1, canvas.width, expansion.top + source.height)).resize((canvas.width, expansion.bottom)),
            (0, expansion.top + source.height))
    mask = Image.new("L", canvas.size, 255)
    mask.paste(0, (expansion.left, expansion.top, expansion.left + source.width, expansion.top + source.height))
    encoded = BytesIO(); canvas.save(encoded, format="PNG")
    encoded_mask = BytesIO(); mask.save(encoded_mask, format="PNG")
    raw = encoded.getvalue()
    return {"source_png": raw, "source_sha256": sha256(raw).hexdigest(),
            "mask": create_mask_payload(raw, encoded_mask.getvalue()),
            "width": canvas.width, "height": canvas.height, "expansion": expansion.to_dict()}
