"""Explicit Swarm post-refinement followed by bounded faithful final sizing.

The selected main model is reused; no implicit refiner or upscaler is loaded.
Both diffusion passes stay inside the normal canvas budget.
"""
from fractions import Fraction
import math

from .generation_output_guidance import validate_base_canvas
from .generation_output_processing import validate_faithful_resize


def prepare_refinement(plan, *, width, height, output_goal, binding, capabilities):
    keys = {"schema_id", "kind", "base_width", "base_height", "refine_width", "refine_height",
            "final_width", "final_height", "strength", "steps", "reviewed"}
    if (not isinstance(plan, dict) or set(plan) != keys
            or plan.get("schema_id") != "collectigen.output-plan.v1"
            or plan.get("kind") != "generative_refine" or plan.get("reviewed") is not True):
        raise ValueError("Review an exact generative refinement plan.")
    if (plan["base_width"], plan["base_height"]) != (width, height):
        raise ValueError("The refinement base changed; review the plan again.")
    if not isinstance(output_goal, dict) or output_goal.get("detail_preference") != "generative":
        raise ValueError("The refinement stage differs from the final-output preference.")
    if (output_goal.get("final_width"), output_goal.get("final_height")) != (plan["final_width"], plan["final_height"]):
        raise ValueError("The refinement final size differs from the saved goal.")
    values = [plan[key] for key in ("base_width", "base_height", "refine_width", "refine_height", "final_width", "final_height", "steps")]
    if any(type(value) is not int for value in values):
        raise ValueError("Refinement sizes and steps must be whole numbers.")
    rw, rh = plan["refine_width"], plan["refine_height"]
    fw, fh = plan["final_width"], plan["final_height"]
    if (rw < width or rh < height or fw < rw or fh < rh or rw % 64 or rh % 64
            or width * rh != height * rw or rw * fh != rh * fw
            or fw > 8192 or fh > 8192 or fw * fh > 32_000_000):
        raise ValueError("Refinement needs bounded aligned canvases with one exact aspect ratio.")
    family = binding.get("family")
    if family not in {"sdxl", "stable-diffusion-xl-v1", "sd1", "stable-diffusion-v1"}:
        raise ValueError("Post-refinement is currently supported for a bound SD1 or SDXL model.")
    validate_base_canvas(width, height, family=family)
    validate_base_canvas(rw, rh, family=family)
    strength = plan["strength"]
    if (type(strength) not in {int, float} or not math.isfinite(strength)
            or not 0 < strength <= .5 or not 1 <= plan["steps"] <= 150):
        raise ValueError("Review a refinement strength above zero and at most 0.5, and 1–150 steps.")
    effective_steps = plan["steps"] - round(plan["steps"] * (1 - strength))
    if effective_steps < 1:
        raise ValueError("The reviewed strength and step budget would run no refinement steps.")
    params = capabilities.get("parameters", {})
    required = {"refinermethod", "refinercontrol", "refinersteps", "refinerupscale", "refinerupscalemethod"}
    if not {"comfyui", "refiners"}.issubset(capabilities.get("features", [])) or not required.issubset(params):
        raise ValueError("The selected local backend does not offer the reviewed refinement parameters.")
    for key, expected in (("refinermethod", "PostApply"), ("refinerupscalemethod", "pixel-lanczos")):
        if expected not in {str(value).split("///")[0] for value in params[key].get("values", [])}:
            raise ValueError("The selected backend does not offer the exact refinement method.")
    scale = Fraction(rw, width)
    if scale.denominator & (scale.denominator - 1):
        raise ValueError("Choose a refinement scale representable exactly by the local generator.")
    wire = {"refinermethod": "PostApply", "refinercontrol": strength,
            "refinersteps": plan["steps"], "refinerupscale": float(scale),
            "refinerupscalemethod": "pixel-lanczos"}
    resize = None
    if (fw, fh) != (rw, rh):
        resize = validate_faithful_resize({"schema_id": "collectigen.output-plan.v1", "kind": "faithful_resize",
            "base_width": rw, "base_height": rh, "final_width": fw, "final_height": fh,
            "filter": "lanczos", "reviewed": True}, base_width=rw, base_height=rh,
            output_goal={**output_goal, "detail_preference": "faithful"})
    return {"plan": dict(plan), "payload": wire, "generator_canvas": [rw, rh],
            "model_resource_id": binding["resource_id"], "effective_steps": effective_steps, "resize": resize}
