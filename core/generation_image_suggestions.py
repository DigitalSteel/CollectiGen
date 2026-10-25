"""Explicit, ephemeral local captions for editable Generation Setup proposals.

This module never saves Image Knowledge, evidence, prompts or setup files.
The caller owns review, source selection, and any later deliberate setup save.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import sys
from typing import Callable, Collection

from .ai_interrogation import configured_plain_language_backend
from .dependencies import DependencyService
from .training_source_io import read_stable_training_source


PART_KINDS = ("character", "scene", "pose", "style", "other")
PURPOSE_PROMPTS = {
    "character": "Describe only visible character appearance and outfit traits useful for a new image prompt. Do not infer identity or hidden traits. Return one concise phrase without explanation.",
    "scene": "Describe only visible setting, objects, lighting, and atmosphere useful for a new image prompt. Do not infer unseen details. Return one concise phrase without explanation.",
    "pose": "Describe only visible pose, action, and composition useful for a new image prompt. Do not infer unseen motion or intent. Return one concise phrase without explanation.",
    "style": "Describe only visible artistic style, medium, colors, and rendering treatment useful for a new image prompt. Do not infer the artist or generating model. Return one concise phrase without explanation.",
}
PURPOSE_LABELS = {"character": "character traits", "scene": "scene elements",
                  "pose": "pose or action", "style": "style"}
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_PROPOSAL_TEXT = 4096


@dataclass(frozen=True)
class ImageSetupProposal:
    kind: str
    text: str
    source_label: str
    model_id: str


@dataclass(frozen=True)
class ImageSetupSuggestionResult:
    proposals: tuple[ImageSetupProposal, ...]
    notes: tuple[str, ...]


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError("Image setup suggestions cancelled.")


def _local_image_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("Choose one readable local image file.")
    text = str(value)
    if "://" in text or text.startswith(("\\\\", "//")):
        raise ValueError("Choose a local image; network paths and URLs are not supported.")
    path = Path(value).absolute()
    if os.name == "nt":
        import ctypes

        # A mapped network drive is not made local by its drive-letter spelling.
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("Choose a local image; network drives are not supported.")
    if not path.is_file():
        raise ValueError("Choose one readable local image file.")
    return path


def _snapshot(path: Path, cancelled: Callable[[], bool], *, verify_image: bool):
    _check_cancelled(cancelled)
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("The image exceeds the 64 MiB suggestion limit.")
    raw, identity = read_stable_training_source(path)
    _check_cancelled(cancelled)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("The image exceeds the 64 MiB suggestion limit.")
    if verify_image:
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(BytesIO(raw)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("The image exceeds the suggestion pixel limit.")
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ValueError("Choose one readable image file.") from exc
    _check_cancelled(cancelled)
    return identity


def suggest_image_setup_parts(
    image_path: str | Path,
    settings,
    *,
    selected_kinds: Collection[str],
    cancelled: Callable[[], bool] = lambda: False,
    backend=None,
) -> ImageSetupSuggestionResult:
    """Return ephemeral, uncertain local proposals without evidence writes.

    Only an explicitly instruction-capable backend receives purpose prompts.
    A caption-only backend returns one unclassified proposal. The purpose is
    the question asked, not a verified classification of the pictured content.
    """
    _check_cancelled(cancelled)
    if not isinstance(selected_kinds, (tuple, list, set, frozenset)):
        raise ValueError("Choose one or more supported suggestion goals.")
    kinds = tuple(selected_kinds)
    if (not kinds or len(kinds) > len(PART_KINDS)
            or any(not isinstance(kind, str) or kind not in PART_KINDS for kind in kinds)
            or len(set(kinds)) != len(kinds)):
        raise ValueError("Choose distinct character, scene, pose, style or other goals.")
    path = _local_image_path(image_path)
    owned = backend is None
    if owned:
        if not DependencyService(settings).capability_ready("joy"):
            raise RuntimeError(
                "Local image suggestions are unavailable. Set up a Plain Language "
                "Description model in AI Tools, or continue editing the setup manually."
            )
        _check_cancelled(cancelled)
        backend = configured_plain_language_backend(settings)
    try:
        analyze = getattr(backend, "analyze_with_cancellation", None)
        if not callable(analyze):
            raise ValueError("This local caption backend does not support cancellation.")
        before = _snapshot(path, cancelled, verify_image=True)
        guided = bool(owned and getattr(backend, "supports_purpose_prompts", False))
        purpose_kinds = tuple(kind for kind in PART_KINDS if kind in kinds and kind in PURPOSE_PROMPTS)
        requests = ((purpose_kinds + (("other",) if "other" in kinds else ()))
                    if guided and purpose_kinds else ("other",))
        proposals = []
        for kind in requests:
            _check_cancelled(cancelled)
            result = (analyze(path, cancelled, prompt_override=PURPOSE_PROMPTS[kind])
                      if kind != "other" else analyze(path, cancelled))
            _check_cancelled(cancelled)
            text = getattr(result, "plain_language", None)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("The local model returned no usable text. Continue manually or retry.")
            text = text.strip()
            if len(text) > MAX_PROPOSAL_TEXT:
                raise ValueError("The local caption exceeds the setup text limit. Continue manually.")
            model_id = getattr(result, "model_id", None)
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 256:
                raise ValueError("The local caption has no usable model attribution.")
            label = (f"Local AI suggestion for {PURPOSE_LABELS[kind]} (uncertain)"
                     if kind != "other" else "Local AI suggestion (uncertain)")
            proposals.append(ImageSetupProposal(kind, text, label, model_id.strip()))
        if _snapshot(path, cancelled, verify_image=False) != before:
            raise ValueError("The selected image changed. Reselect it before requesting suggestions.")
        _check_cancelled(cancelled)
        return ImageSetupSuggestionResult(
            proposals=tuple(proposals),
            notes=((
                "Purpose-targeted questions are uncertain AI suggestions, not verified image facts. "
                "Edit, reassign or reject each answer before use."
                if guided and purpose_kinds else
                "Unclassified description: this caption backend does not reliably separate "
                "character, scene, pose or style. Edit, reassign or reject it before use."
            ),
                "Suggestions are not selected automatically and do not change Image Knowledge.",
            ),
        )
    finally:
        if owned:
            release = getattr(backend, "release", None)
            if callable(release):
                pending_error = sys.exc_info()[0] is not None
                try:
                    release()
                except Exception:
                    # Preserve cancellation or the actionable inference failure;
                    # a release failure on success still refuses publication.
                    if not pending_error:
                        raise
