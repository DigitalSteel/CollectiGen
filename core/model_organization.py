"""Pure destination and provenance helpers for the shared model registry.

Folder placement is an organizational declaration, never execution evidence.
These functions neither inspect nor create directories.
"""
from __future__ import annotations

from pathlib import Path
import re


FAMILY_DESTINATIONS = (
    "Images:SDXL", "Images:Flux.1", "Images:Flux.2", "Images:Anima", "Images:Krea",
    "Video:Wan2.2", "Shared", "Upscalers", "Analysis", "Unclassified",
)
ROLE_FOLDERS = {
    "checkpoint": "Main Models", "unet": "Main Models", "lora": "LoRAs",
    "vae": "VAEs", "text_encoder": "Text Encoders", "embedding": "Embeddings",
    "extension": "Extensions", "vision_encoder": "Vision Encoders",
    "control_model": "Control Models", "video_companion": "Companions",
    "upscaler": "Upscalers", "tagger": "Taggers", "captioner": "Captioners",
}


def normalize_model_family(value):
    """Canonicalize only recognized aliases; retain unrecognized declarations."""
    raw = str(value or "").strip()
    compact = re.sub(r"[\s_.-]+", "", raw).casefold()
    return {
        "sdxl": "SDXL", "sdxl10": "SDXL", "stablediffusionxl": "SDXL",
        "stablediffusionxl10": "SDXL",
        "flux1": "Flux.1", "flux2": "Flux.2",
        "anima": "Anima", "krea": "Krea", "wan22": "Wan2.2",
    }.get(compact, raw)


def family_options(media_type="Images"):
    """Suggested labels for an editor; other explicit families remain valid."""
    return ("SDXL", "Flux.1", "Flux.2", "Anima", "Krea") if media_type == "Images" else ("Wan2.2",)


def family_destination_template(root):
    """Return illustrative destinations below a user-chosen root, without I/O."""
    base = Path(root)
    if not base.is_absolute():
        raise ValueError("Choose an absolute model root before previewing a family layout.")
    return {key: str(base.joinpath(*key.split(":"))) for key in FAMILY_DESTINATIONS}


def destination_path(workspace, *, role, family="", media_type="Images"):
    """Resolve an acquisition destination and its source from saved settings.

    Existing explicit role:family paths retain priority. Generic legacy role
    paths remain usable when no configured family applies.
    """
    if media_type not in ("Images", "Video"):
        raise ValueError("Choose Images or Video for a model destination.")
    overrides = workspace.get("destination_overrides", {})
    normalized = normalize_model_family(family)
    # Legacy role[:family] overrides predate Video; using one for Video can
    # silently deposit a video model in a configured Images destination.
    exact = (overrides.get(f"{role}:{family}") or overrides.get(f"{role}:{normalized}")) if family and media_type == "Images" else None
    if exact:
        return exact, "explicit role and family override"
    destinations = workspace.get("family_destinations", {})
    if role == "upscaler":
        key = "Upscalers"
    elif role in ("tagger", "captioner"):
        key = "Analysis"
    elif family == "Shared":
        key = "Shared"
    elif family:
        key = f"{media_type}:{normalized}"
    else:
        key = "Unclassified"
    base = destinations.get(key)
    if ":" in key:
        matching = {path for saved_key, path in destinations.items()
                    if ":" in saved_key and saved_key.split(":", 1)[0] == media_type
                    and normalize_model_family(saved_key.split(":", 1)[1]) == normalized and path}
        if len(matching) > 1:
            return "", "conflicting configured family destinations"
        if not base and matching:
            base = next(iter(matching))
    if base:
        return str(Path(base) / ROLE_FOLDERS.get(role, role)), "configured family destination"
    if media_type == "Images" and overrides.get(role):
        return overrides[role], "legacy role override"
    if destinations.get("Unclassified"):
        return (str(Path(destinations["Unclassified"]) / ROLE_FOLDERS.get(role, role)),
                "configured unclassified destination")
    if media_type == "Video":
        return "", "video family destination not configured"
    return workspace.get("default_destination", ""), "legacy default destination"


def configured_family_hints(workspace, paths):
    """Return all configured family hints matching registered file locations.

    Overlapping locations and duplicate copies can yield multiple hints. A
    caller must resolve disagreement; this function never picks the first path.
    """
    destinations = workspace.get("family_destinations", {})
    legacy = workspace.get("destination_overrides", {})
    hints = set()
    for path in paths:
        candidate = Path(path)
        for key, root in destinations.items():
            if ":" not in key or not root:
                continue
            base = Path(root)
            if candidate == base or base in candidate.parents:
                media_type, family = key.split(":", 1)
                hints.add((media_type, normalize_model_family(family)))
        for key, root in legacy.items():
            if ":" not in key or not root:
                continue
            _, family = key.split(":", 1)
            base = Path(root)
            if candidate == base or base in candidate.parents:
                # Legacy declarations predate the explicit Video locations.
                hints.add(("Images", normalize_model_family(family)))
    return tuple(sorted(hints))
