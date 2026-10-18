"""Bounded, explicitly reviewed final-size image processing.

The diffusion request remains at its reviewed base size. This module only
performs a deterministic faithful resize after the raw local result verifies.
"""
from __future__ import annotations

from io import BytesIO
from collections.abc import Mapping

from PIL import Image


_SCHEMA = "collectigen.output-plan.v1"
_KEYS = {"schema_id", "kind", "base_width", "base_height", "final_width",
         "final_height", "filter", "reviewed"}


def validate_faithful_resize(plan, *, base_width, base_height, output_goal=None):
    """Return an exact normalized postprocess contract or reject a stale plan."""
    if not isinstance(plan, Mapping) or set(plan) != _KEYS:
        raise ValueError("Review one exact final-size stage before generation.")
    if (plan["schema_id"] != _SCHEMA or plan["kind"] != "faithful_resize"
            or plan["filter"] != "lanczos" or plan["reviewed"] is not True):
        raise ValueError("The final-size stage is unsupported or unreviewed.")
    values = (base_width, base_height, plan["base_width"], plan["base_height"],
              plan["final_width"], plan["final_height"])
    if any(type(value) is not int for value in values):
        raise ValueError("Final-size dimensions must be exact integers.")
    base_w, base_h = base_width, base_height
    final_w, final_h = plan["final_width"], plan["final_height"]
    if (plan["base_width"], plan["base_height"]) != (base_w, base_h):
        raise ValueError("The reviewed final-size base differs from the generation request.")
    if (not 64 <= base_w <= 2048 or not 64 <= base_h <= 2048
            or not 64 <= final_w <= 8192 or not 64 <= final_h <= 8192
            or final_w * final_h > 32_000_000
            or final_w <= base_w or final_h <= base_h):
        raise ValueError("Choose a bounded final size larger than the reviewed base.")
    if base_w * final_h != base_h * final_w:
        raise ValueError("Faithful resize needs the exact same aspect ratio; review padding or crop separately.")
    if not isinstance(output_goal, Mapping) or output_goal.get("schema_id") != "collectigen.output-goal.v1":
        raise ValueError("A reviewed final output goal is required.")
    if output_goal.get("detail_preference", "faithful") != "faithful":
        raise ValueError("The final-size stage differs from the requested detail preference.")
    if (output_goal.get("final_width"), output_goal.get("final_height")) != (final_w, final_h):
        raise ValueError("The final-size stage differs from the saved output goal.")
    return {key: plan[key] for key in sorted(_KEYS)}


def render_faithful_resize(raw_png: bytes, plan: Mapping) -> bytes:
    """Return Lanczos PNG bytes; caller retains the exact generator bytes."""
    checked = validate_faithful_resize(plan, base_width=plan.get("base_width"),
        base_height=plan.get("base_height"), output_goal={"schema_id": "collectigen.output-goal.v1",
            "final_width": plan.get("final_width"), "final_height": plan.get("final_height")})
    if not isinstance(raw_png, bytes) or not raw_png or len(raw_png) > 96 * 1024 * 1024:
        raise ValueError("The verified base image exceeds the final-size processing bound.")
    with Image.open(BytesIO(raw_png)) as image:
        if (image.format != "PNG" or image.size != (checked["base_width"], checked["base_height"])
                or getattr(image, "n_frames", 1) != 1):
            raise ValueError("The raw generator image differs from the reviewed base canvas.")
        image.load()
        resized = image.resize((checked["final_width"], checked["final_height"]),
                               Image.Resampling.LANCZOS)
        output = BytesIO()
        resized.save(output, format="PNG")
    result = output.getvalue()
    if len(result) > 96 * 1024 * 1024:
        raise ValueError("The final-size PNG exceeds the retained output bound.")
    return result
