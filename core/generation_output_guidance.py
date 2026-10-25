"""Deterministic, advisory output sizing across image and short-video workflows.

This module does not revise a GenerationPlan or grant adapter execution. It
keeps the desired final canvas separate from the model-sized first pass and
requires independent, exact installed-stage proof for every later operation.
"""
from __future__ import annotations

from math import sqrt, isfinite, gcd
import re
from collections.abc import Mapping


_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROFILES = {
    ("stabilityai/sdxl-base-1.0", "1.0"): ("image", 1024 * 1024, 64, "SDXL 1.0 base"),
    ("black-forest-labs/flux.1-dev", "1"): ("image", 1024 * 1024, 32, "FLUX.1 dev"),
    ("circlestone-labs/anima-base-v1.0", "Base-v1.0"): ("image", 1024 * 1024, 32, "Anima Base v1.0"),
    ("wan-ai/wan2.2-ti2v-5b", "TI2V-5B"): ("video", 1280 * 704, 32, "Wan2.2 TI2V-5B"),
}
_FAMILY_PROFILES = {
    'sdxl': ('image', 1024 * 1024, 64, 'SDXL family baseline'),
    'stable-diffusion-xl-v1': ('image', 1024 * 1024, 64, 'SDXL family baseline'),
    'sd1': ('image', 512 * 512, 64, 'SD1 family baseline'),
    'stable-diffusion-v1': ('image', 512 * 512, 64, 'SD1 family baseline'),
    'flux-1': ('image', 1024 * 1024, 32, 'FLUX.1 family baseline'),
    'flux.1': ('image', 1024 * 1024, 32, 'FLUX.1 family baseline'),
    'anima': ('image', 1024 * 1024, 32, 'Anima family baseline'),
}


def validate_base_canvas(width, height, *, family, measured_limit=None):
    """Bound direct diffusion work; a final output target cannot enlarge this budget.

    A larger limit needs a separately validated exact model/runtime/hardware
    measurement from the caller. Family defaults are conservative planning
    limits, not claims that every checkpoint of that family has equal quality.
    """
    width = _positive_int(width, 'Base width', maximum=2048)
    height = _positive_int(height, 'Base height', maximum=2048)
    profile = _FAMILY_PROFILES.get(str(family).casefold())
    limit = profile[1] if profile else 1024 * 1024
    if measured_limit is not None:
        # This helper accepts no opaque request-supplied memory override.
        raise ValueError('A larger measured workflow needs its own qualified adapter.')
    if width * height > limit:
        raise ValueError('The base canvas exceeds this family\'s conservative generation size. '
                         'Choose a smaller base and an explicit upscale or refinement workflow for the final size.')
    return limit


def _positive_int(value, label, *, maximum=16384):
    if type(value) is not int or not 64 <= value <= maximum:
        raise ValueError(f"{label} must be an explicit bounded pixel dimension.")
    return value


def _base_size(final_width, final_height, pixels, multiple):
    area = min(final_width * final_height, pixels)
    ratio = final_width / final_height
    ideal_width = sqrt(area * ratio)
    ideal_height = sqrt(area / ratio)
    candidates = []
    for width in range(max(1, int(ideal_width / multiple) - 2) * multiple,
                       (int(ideal_width / multiple) + 3) * multiple, multiple):
        for height in range(max(1, int(ideal_height / multiple) - 2) * multiple,
                            (int(ideal_height / multiple) + 3) * multiple, multiple):
            if 64 <= width <= 2048 and 64 <= height <= 2048 and width * height <= pixels:
                error = abs(width / height - ratio) / ratio
                candidates.append((error, abs(width * height - area), width, height))
    if not candidates:
        raise ValueError("The requested aspect ratio has no bounded aligned base canvas.")
    _, _, width, height = min(candidates)
    return width, height


IMAGE_ASPECT_RATIOS = (("Square · 1:1", (1, 1)), ("Portrait · 2:3", (2, 3)),
    ("Landscape · 3:2", (3, 2)), ("Portrait · 3:4", (3, 4)),
    ("Landscape · 4:3", (4, 3)), ("Widescreen · 16:9", (16, 9)),
    ("Vertical · 9:16", (9, 16)))


def exact_image_base(final_width, final_height, pixel_budget):
    """Largest non-enlarging exact-aspect canvas accepted by the image adapter.

    Its 64-pixel alignment is deliberately stricter than some advisory family
    profiles. No crop, padding or relaxed adapter contract is inferred.
    """
    if (any(type(value) is not int for value in (final_width, final_height, pixel_budget))
            or not 64 <= final_width <= 8192 or not 64 <= final_height <= 8192
            or final_width * final_height > 32_000_000 or pixel_budget < 64 ** 2):
        return None
    divisor = gcd(final_width, final_height)
    x, y = final_width // divisor, final_height // divisor
    for factor in range(min(divisor, 2048 // max(x, y)), 0, -1):
        width, height = x * factor, y * factor
        if (width >= 64 and height >= 64 and width % 64 == height % 64 == 0
                and width * height <= pixel_budget):
            return width, height
    return None


def image_size_choices(aspect, *, model_evidence):
    """Final-size choices derived from existing model guidance, never fit proof."""
    if aspect not in [ratio for _, ratio in IMAGE_ASPECT_RATIOS]:
        return []
    width, height = aspect
    factor = min(2048, 8192 // max(width, height), int(sqrt(16_000_000 / (width * height))))
    factor = factor // 64 * 64
    preview = plan_image_output_resolution(width * factor, height * factor,
        model_evidence=model_evidence, installed_stages={}, detail_preference="faithful")
    if preview["model_evidence"] == "unknown" or preview["application_blocker"]:
        return []
    base = preview["base"]
    return [(f"{base['width'] * scale} × {base['height'] * scale}" +
             (" · family starting size" if scale == 1 else f" · {scale}× final size"),
             (base["width"] * scale, base["height"] * scale))
            for scale in (1, 2, 4)
            if max(base.values()) * scale <= 8192
            and base["width"] * base["height"] * scale ** 2 <= 32_000_000]


def plan_image_output_resolution(final_width, final_height, **kwargs):
    """Project image guidance onto existing adapter/finishing geometry limits.

    Preview and Apply share this result. General/video advice keeps its separate
    contract; this projection never grants adapter or memory qualification.
    """
    result = plan_output_resolution(final_width, final_height, **kwargs)
    result["application_blocker"] = ""
    if result["modality"] != "image":
        raise ValueError("Image output planning cannot change a video request.")
    base = result["base"]
    budget = base["width"] * base["height"]
    exact = exact_image_base(final_width, final_height, budget)
    if exact is None:
        result["can_execute"] = False
        result["application_blocker"] = ("No 64-pixel-aligned generation size preserves this exact aspect "
            "within the reviewed budget. Choose a standard aspect ratio or a smaller final size "
            "(up to 8192 per side and 32 million pixels). No crop or padding is applied.")
        result["messages"].append(result["application_blocker"])
        return result
    result["base"] = {"width": exact[0], "height": exact[1]}
    result["messages"] = [message for message in result["messages"]
        if not message.startswith("The base pass preserves the target aspect approximately")]
    result["messages"].append("The proposed generation size preserves the exact aspect with the adapter's 64-pixel alignment.")
    result["stages"][0].update(width=exact[0], height=exact[1])
    if result["goal_preset"] == "quick_preview":
        result["final"] = dict(result["base"])
    elif exact != (final_width, final_height) and len(result["stages"]) == 1:
        kind = "resize" if result["detail_preference"] == "faithful" else "generative_upscale"
        result["stages"].append(_stage(kind, final_width, final_height, kwargs["installed_stages"]))
    result["can_execute"] = result["can_execute"] and all(stage["available"] for stage in result["stages"])
    # Stage dimensions changed; never carry memory applicability from the
    # advisory geometry to this exact executable geometry.
    result["vram"] = _vram(result["stages"], kwargs.get("machine"),
                           kwargs.get("fit_evidence"), kwargs.get("request_context"))
    result["can_execute"] = result["can_execute"] and result["vram"]["assessment"] != "limited"
    return result


def _stage(kind, width, height, installed, *, selected_model=None):
    item = installed.get(kind)
    ready = (isinstance(item, Mapping) and item.get("installed") is True
             and item.get("supported") is True
             and isinstance(item.get("adapter_id"), str) and bool(item["adapter_id"])
             and (kind in {"resize", "video_resize"}
                  or isinstance(item.get("resource_id"), str)
                  and bool(_SHA.fullmatch(item["resource_id"]))))
    if ready and kind == "base" and item.get("resource_id") != selected_model:
        ready = False
    if ready:
        for field, actual in (("max_width", width), ("max_height", height),
                              ("max_pixels", width * height)):
            bound = item.get(field)
            if bound is not None and (type(bound) is not int or actual > bound):
                ready = False
    action = ("" if ready else f"Install and qualify an exact {kind.replace('_', ' ')} stage for "
              f"{width}×{height}, then review this plan again.")
    return {"kind": kind, "width": width, "height": height,
            "available": ready, "action": action,
            "adapter_id": item.get("adapter_id", "") if isinstance(item, Mapping) else "",
            "resource_id": item.get("resource_id", "") if isinstance(item, Mapping) else ""}


def _vram(stages, machine, fit_evidence, request_context):
    from .generation_resource_guard import assess_memory_evidence
    return assess_memory_evidence(stages, request_context, machine, fit_evidence)


def plan_output_resolution(final_width, final_height, *, model_evidence,
                           installed_stages, detail_preference, modality="image",
                           machine=None, fit_evidence=None, frame_count=None, fps=None,
                           refine=False, goal_preset="final_output", request_context=None, current_base=None):
    """Suggest a base canvas and explicit later stages without promising quality or fit."""
    final_width = _positive_int(final_width, "Final width")
    final_height = _positive_int(final_height, "Final height")
    if final_width * final_height > 64_000_000:
        raise ValueError("The requested final canvas exceeds the bounded planner.")
    if modality not in {"image", "video"} or detail_preference not in {"faithful", "generative"}:
        raise ValueError("Choose an explicit modality and detail preference.")
    if goal_preset not in {"quick_preview", "balanced", "final_output", "lower_memory"}:
        raise ValueError("Choose a supported output goal preset.")
    if modality != "image" and goal_preset != "final_output":
        raise ValueError("Image goal presets do not change the qualified video recipe.")
    if not isinstance(model_evidence, Mapping) or not isinstance(installed_stages, Mapping):
        raise ValueError("Provide model identity and installed stage evidence.")
    resource_id = model_evidence.get("resource_id")
    profile = _PROFILES.get((model_evidence.get("catalog_id"), model_evidence.get("version")))
    exact = (model_evidence.get("evidence_kind") == "exact_model"
             and model_evidence.get("verified") is True
             and isinstance(resource_id, str) and bool(_SHA.fullmatch(resource_id))
             and profile is not None and profile[0] == modality)
    messages = []
    authority = 'exact_model' if exact else 'unknown'
    if not exact:
        kind = model_evidence.get('evidence_kind')
        profile = _FAMILY_PROFILES.get(str(model_evidence.get('family', '')).casefold()) if modality == 'image' else None
        if profile and kind in {'verified_family', 'folder_hint', 'declared_family'}:
            authority = kind
            messages.append('Family baseline only: this checkpoint may need different settings. ' +
                            ('Folder placement is an unverified hint.' if kind == 'folder_hint' else 'Review the model author\'s guidance when available.'))
        else:
            profile = None
            messages.append("Model-specific sizing is unknown: select and verify a model; folder names are hints only.")
    if modality == "video":
        if frame_count is None or type(frame_count) is not int or not 1 <= frame_count <= 121 or (frame_count - 1) % 4:
            messages.append("Wan video frames need an explicit 4n+1 count from 1 to 121.")
        if fps is None or type(fps) not in (int, float) or not isfinite(fps) or fps != 24:
            messages.append("The documented Wan TI2V-5B target is 24 FPS; review another FPS explicitly.")
        if profile:
            base_width, base_height = ((1280, 704) if final_width >= final_height else (704, 1280))
            if abs(base_width / base_height - final_width / final_height) / (final_width / final_height) > 0.02:
                messages.append("Requested video aspect differs from the documented Wan canvas; choose crop or padding deliberately.")
        else:
            base_width, base_height = final_width, final_height
    elif profile:
        base_width, base_height = _base_size(final_width, final_height, profile[1], profile[2])
        if (base_width, base_height) != (final_width, final_height):
            messages.append("The base pass preserves the target aspect approximately; the final stage must reach exact requested pixels.")
    else:
        base_width, base_height = final_width, final_height
    requested_final = {"width": final_width, "height": final_height}
    if goal_preset == "lower_memory":
        if profile:
            # Reduce actual diffusion work, not merely the final resize. Keep
            # the established family alignment and never enlarge a small draft.
            area = min(profile[1], base_width * base_height)
            if current_base is not None:
                if not isinstance(current_base, Mapping):
                    raise ValueError("Current base must contain explicit dimensions.")
                area = min(area, _positive_int(current_base.get("width"), "Current base width")
                           * _positive_int(current_base.get("height"), "Current base height"))
            area = max(64 * 64, area // 2)
            base_width, base_height = _base_size(final_width, final_height, area, profile[2])
        detail_preference, refine = "faithful", False
        messages.append("Lower Memory reduces the first-pass pixel budget toward half (at least 64×64) and uses faithful resizing for the requested final size. "
                        "Fine detail and composition may suffer; this is an unmeasured lower-work option, not a memory-fit or quality guarantee. "
                        "Model, prompts, seeds and sampling stay unchanged." if profile else
                        "Lower Memory needs a known model canvas before a smaller first pass can be proposed. Select and verify a model first.")
    if goal_preset == "quick_preview":
        final_width, final_height = base_width, base_height
        detail_preference, refine = "faithful", False
        messages.append("Quick Preview produces one model-sized image without a finishing pass. "
                        "The preview size becomes the final size when applied; sampling settings stay unchanged."
                        if authority in {"exact_model", "verified_family"} else
                        "Quick Preview needs a verified model canvas before it can be applied; no model-specific preview size is known.")
    elif goal_preset == "balanced":
        detail_preference, refine = "faithful", False
        messages.append("Balanced uses a model-sized first pass and faithful resizing when needed; "
                        "it does not add a generative detail pass or change sampling settings.")
    stages = [_stage("base", base_width, base_height, installed_stages,
                     selected_model=resource_id)]
    if (base_width, base_height) != (final_width, final_height):
        followup = ("video_resize" if modality == "video" and detail_preference == "faithful"
                    else "video_upscale" if modality == "video" else
                    "resize" if detail_preference == "faithful" else "generative_upscale")
        stages.append(_stage(followup, final_width, final_height, installed_stages))
    if refine:
        stages.append(_stage("video_refine" if modality == "video" else "refine",
                             final_width, final_height, installed_stages))
    temporal_ok = modality != "video" or (frame_count is not None and type(frame_count) is int
        and 1 <= frame_count <= 121 and (frame_count - 1) % 4 == 0
        and type(fps) in (int, float) and fps == 24)
    vram = _vram(stages, machine, fit_evidence, request_context)
    return {"final": {"width": final_width, "height": final_height},
            "requested_final": requested_final, "goal_preset": goal_preset,
            "base": {"width": base_width, "height": base_height},
            "modality": modality, "detail_preference": detail_preference,
            "model_evidence": authority,
            "profile": profile[3] if profile else "", "stages": stages,
            "messages": messages,
            "can_execute": (exact or authority == 'verified_family') and temporal_ok and vram["assessment"] != "limited" and all(stage["available"] for stage in stages)
                           and not (modality == "video" and any("aspect differs" in m for m in messages)),
            "vram": vram}
