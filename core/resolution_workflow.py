"""Reusable resolution preferences and bounded original-preserving batch work."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path

from .external_write_transaction import atomic_verified_bytes
from .image_derivatives import DerivativeStore, preview_resize
from .paths import USER_DATA_DIR
from .training_source_io import _assert_unredirected


def settings_path():
    return USER_DATA_DIR / "resolution_policy.json"


def validate_resolution_access(registry, paths, *, ingest_directory="", output_root=None):
    """Accept named libraries or explicit Ingest without widening their boundaries."""
    configured = str(ingest_directory or "").strip()
    ingest = Path(configured).expanduser() if configured else None
    # Empty/relative settings must never authorize the process working directory.
    ingest = Path(os.path.abspath(ingest)) if ingest is not None and ingest.is_absolute() else None
    for path in paths:
        profile = registry.profile_for_path(path)
        if profile is not None and profile.role == "local_only":
            raise ValueError("Resolution review and resizing are unavailable for Local Only images.")
        if profile is None:
            source = Path(os.path.abspath(Path(path).expanduser()))
            if ingest is None or not source.is_relative_to(ingest):
                raise ValueError("These images are outside the configured Ingest folder and registered libraries. Review folder locations in Setup, then retry.")
            # A junction/symlink under Ingest must not authorize another location.
            _assert_unredirected(source)
    if output_root:
        root = Path(os.path.abspath(Path(output_root).expanduser()))
        _assert_unredirected(root)
        bases = [Path(os.path.abspath(Path(profile.root).expanduser())) for profile in registry.profiles]
        if ingest is not None:
            bases.append(ingest)
        if any(root.is_relative_to(base) or base.is_relative_to(root) for base in bases):
            raise ValueError("Choose a protected version folder separate from Ingest and every collection and storage role.")


def _validate_automatic_adapter(value):
    method = value.get("automatic_upscale_method", "lanczos")
    if method not in {"lanczos", "esrgan"}:
        raise ValueError("Unsupported automatic upscale method; review the saved resolution policy.")
    if value.get("automatic_upscale") and method == "esrgan":
        spec = value.get("automatic_ai_model_spec")
        if not isinstance(spec, dict) or not spec.get("sha256") or spec.get("path") != value.get("ai_model_path"):
            raise ValueError("Automatic AI upscaling requires an exact verified manual model selection.")


def load_resolution_settings(path=None):
    from .resolution_policy import ResolutionPolicy
    defaults = {"schema_version": 1, "policy": asdict(ResolutionPolicy()),
                "automatic_upscale": False, "automatic_downscale": False,
                "upscale_output_root": "", "downscale_output_root": "", "output_root": ""}
    path = Path(path) if path is not None else settings_path()
    _assert_unredirected(path)
    if not path.exists():
        return defaults
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Unsupported resolution preferences; saved policy was left unchanged.")
    defaults.update(value)
    ResolutionPolicy(**defaults["policy"])
    _validate_automatic_adapter(defaults)
    for direction in ("upscale", "downscale"):
        if type(defaults["automatic_" + direction]) is not bool:
            raise ValueError("Automatic resize permissions must be explicit switches.")
        if defaults["automatic_" + direction] and not defaults[direction + "_output_root"]:
            raise ValueError("Choose a protected derivative destination for each automatic direction.")
    return defaults


def save_resolution_settings(value, path=None, *, expected=None):
    from .resolution_policy import ResolutionPolicy
    selected = dict(value)
    selected["schema_version"] = 1
    ResolutionPolicy(**selected["policy"])
    _validate_automatic_adapter(selected)
    for direction in ("upscale", "downscale"):
        if type(selected.get("automatic_" + direction, False)) is not bool:
            raise ValueError("Choose an explicit automatic resize permission.")
        if selected.get("automatic_" + direction) and not selected.get(direction + "_output_root"):
            raise ValueError("Choose a derivative destination for each enabled automatic direction.")
    path = Path(path) if path is not None else settings_path()
    _assert_unredirected(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.close(descriptor)
        if expected is not None and load_resolution_settings(path) != expected:
            raise ValueError("Resolution preferences changed elsewhere. Reopen the policy before saving.")
        if path.exists():
            atomic_verified_bytes(path.with_suffix(path.suffix + ".previous"), path.read_bytes(), label="Resolution preference recovery")
        atomic_verified_bytes(path, (json.dumps(selected, indent=2, allow_nan=False) + "\n").encode(), label="Resolution preferences")
    finally:
        lock.unlink()


def execute_resize_batch(plans, root, *, cancelled=lambda: False, progress=lambda *_: None, guard=lambda: None):
    """Each result is independently durable; cancellation preserves prior versions."""
    results = []
    store = DerivativeStore(root)
    for index, plan in enumerate(plans):
        if cancelled():
            break
        progress(index, len(plans), "Creating protected resize version")
        try:
            result = store.execute(plan, cancelled=cancelled, guard=guard)
        except (OSError, ValueError, RuntimeError) as exc:
            result = {"state": "failed", "fingerprint": plan["fingerprint"], "error": str(exc)}
        results.append(result)
        if result["state"] == "cancelled":
            break
    progress(len(results), len(plans), "Resize batch finished; originals preserved")
    return results
