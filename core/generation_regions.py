"""Bounded, literal rectangular regional prompts for an exact image canvas.

This module only validates and renders a saved prompt plan. Runtime and adapter
admission remain with the generation execution owner.
"""
from __future__ import annotations

from copy import deepcopy
import math


MAX_REGIONS = 2
MAX_PROMPT_LENGTH = 16384


def _canvas(width, height):
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= 32768 or not 1 <= height <= 32768):
        raise ValueError("The regional prompt needs an exact positive image canvas.")
    return {"width": width, "height": height}


def empty_region_spec(width, height):
    return {"schema_version": 1, "base_canvas": _canvas(width, height), "regions": []}


def _literal_prompt(text, *, name, allow_empty=False):
    if (not isinstance(text, str) or (not allow_empty and not text.strip()) or len(text) > MAX_PROMPT_LENGTH
            or "<" in text or ">" in text or "embedding:" in text.casefold()
            or any(ord(char) < 32 and char not in "\t\n\r" for char in text)):
        raise ValueError(f"{name} must be bounded literal text without <, > or embedding: markup.")
    return text


def _number(value, *, name, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if not (0 < value <= 1 if positive else 0 <= value <= 1):
        raise ValueError(f"{name} must be within the normalized canvas.")
    return value


def validate_region_spec(spec, *, width, height):
    """Return a detached spec; reject stale canvas, overlap, and prompt markup."""
    canvas = _canvas(width, height)
    if not isinstance(spec, dict) or type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        raise ValueError("Unsupported regional prompt version.")
    if set(spec) != {"schema_version", "base_canvas", "regions"}:
        raise ValueError("Unsupported regional prompt fields.")
    if not isinstance(spec.get("base_canvas"), dict) or set(spec["base_canvas"]) != {"width", "height"}:
        raise ValueError("Unsupported regional prompt canvas fields.")
    if (type(spec["base_canvas"]["width"]) is not int
            or type(spec["base_canvas"]["height"]) is not int
            or spec["base_canvas"] != canvas):
        raise ValueError("The image canvas changed. Reopen regional prompts for the current size.")
    regions = spec.get("regions")
    if not isinstance(regions, list) or len(regions) > MAX_REGIONS:
        raise ValueError("Use no more than two rectangular regions.")
    boxes = []
    for index, region in enumerate(regions, 1):
        if not isinstance(region, dict):
            raise ValueError(f"Region {index} is invalid.")
        if set(region) != {"x", "y", "width", "height", "strength", "prompt"}:
            raise ValueError(f"Region {index} contains unsupported fields.")
        x = _number(region.get("x"), name=f"Region {index} left")
        y = _number(region.get("y"), name=f"Region {index} top")
        w = _number(region.get("width"), name=f"Region {index} width", positive=True)
        h = _number(region.get("height"), name=f"Region {index} height", positive=True)
        _number(region.get("strength"), name=f"Region {index} strength", positive=True)
        _literal_prompt(region.get("prompt"), name=f"Region {index} prompt")
        if x + w > 1 or y + h > 1:
            raise ValueError(f"Region {index} extends beyond the image canvas.")
        if w * width < 1 or h * height < 1:
            raise ValueError(f"Region {index} must cover at least one canvas pixel in each direction.")
        for left, top, right, bottom in boxes:
            if (x < right - 1e-12 and x + w > left + 1e-12
                    and y < bottom - 1e-12 and y + h > top + 1e-12):
                raise ValueError("Regional rectangles may touch but cannot overlap.")
        boxes.append((x, y, x + w, y + h))
    return deepcopy(spec)


def render_region_prompt(global_prompt, spec, *, width, height):
    """Render SwarmUI's literal region delimiters without changing global text."""
    checked = validate_region_spec(spec, width=width, height=height)
    if not checked["regions"]:
        if not isinstance(global_prompt, str) or len(global_prompt) > MAX_PROMPT_LENGTH:
            raise ValueError("Global prompt must be bounded text.")
        return global_prompt
    _literal_prompt(global_prompt, name="Global prompt", allow_empty=True)
    parts = []
    for region in checked["regions"]:
        coords = ",".join(str(float(region[key])) for key in ("x", "y", "width", "height", "strength"))
        parts.append(f"<region:{coords}>{region['prompt']}<region:end>")
    return global_prompt + (" " if global_prompt else "") + " ".join(parts)
