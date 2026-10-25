"""Receipt-bound, source-preserving finishing of generated image winners.

The caller supplies a verifier for one exact GenerationExecutionService receipt.
No path supplied by the UI is authority on its own. Versions use the existing
DerivativeStore transaction and never replace the generated source.
"""
from __future__ import annotations

from pathlib import Path
import re

from PIL import Image, ImageOps

from .image_derivatives import DerivativeStore, MAX_PIXELS, MAX_SOURCE_BYTES, preview_resize
from .training_source_io import _assert_unredirected, read_stable_training_source


def _source(source, storage_root, verified_source):
    if not callable(verified_source):
        raise ValueError("An exact generation receipt verifier is required.")
    root = Path(storage_root).absolute()
    path = Path(source).absolute()
    _assert_unredirected(root)
    _assert_unredirected(path)
    if (path.name != "result.png" or path.parent.parent != root
            or re.fullmatch(r"gen-[a-f0-9]{32}", path.parent.name) is None):
        raise ValueError("Finishing requires a retained generated result in this workspace.")
    verified = [Path(value).absolute() for value in verified_source()]
    if verified.count(path) != 1:
        raise ValueError("The selected result is no longer in the exact verified generation receipt.")
    return path


def inspect_generation_finish_source(source, *, storage_root, verified_source):
    """Read a selected, verified source only; return bounded display dimensions."""
    path = _source(source, storage_root, verified_source)
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("The generated source exceeds the bounded resize input limit.")
    raw, identity = read_stable_training_source(path)
    from io import BytesIO
    with Image.open(BytesIO(raw)) as image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise ValueError("Finishing supports a single-frame generated PNG.")
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("The generated source exceeds 40 megapixels.")
        size = ImageOps.exif_transpose(image).size
    _source(path, storage_root, verified_source)
    return {"source": str(path), "width": size[0], "height": size[1],
            "sha256": identity["sha256"]}


def _dimensions(width, height, source_size):
    if (type(width) is not int or type(height) is not int or
            not 64 <= width <= 8192 or not 64 <= height <= 8192 or
            width * height > MAX_PIXELS):
        raise ValueError("Choose final dimensions from 64 to 8192 pixels per side, up to 40 megapixels.")
    if width * source_size[1] != height * source_size[0]:
        raise ValueError("Final dimensions must preserve the exact source aspect ratio.")


def _faithful_resize_dimensions(width, height, source_size):
    if (width, height) == tuple(source_size):
        raise ValueError("Choose a final size different from the native result.")
    _dimensions(width, height, source_size)


def preview_generation_finish(source, width, height, *, storage_root, verified_source):
    """Freeze a faithful Lanczos recipe; this call does not create directories."""
    detail = inspect_generation_finish_source(source, storage_root=storage_root,
                                               verified_source=verified_source)
    _faithful_resize_dimensions(width, height, (detail["width"], detail["height"]))
    plan = preview_resize(detail["source"], (width, height),
        {"source_kind": "verified_generation", "detail_preference": "faithful",
         "source_sha256": detail["sha256"]}, allow_derivative=True)
    _source(source, storage_root, verified_source)
    return plan


def execute_generation_finish(plan, *, storage_root, verified_source, cancelled=lambda: False):
    """Publish a separate version after pre/post receipt checks."""
    if not isinstance(plan, dict) or plan.get("recipe", {}).get("policy", {}).get("source_kind") != "verified_generation":
        raise ValueError("A reviewed generation finishing preview is required.")
    source = _source(plan.get("source", ""), storage_root, verified_source)
    if plan["recipe"]["policy"].get("source_sha256") != plan.get("source_family", {}).get(source.suffix, {}).get("sha256"):
        raise ValueError("The reviewed source identity is inconsistent.")
    _faithful_resize_dimensions(*plan["target_size"], plan["source_size"])
    store = DerivativeStore(Path(storage_root).absolute() / "finishing")
    result = store.execute(plan, cancelled=cancelled,
                           guard=lambda: _source(source, storage_root, verified_source))
    if result.get("state") == "verified":
        verify_generation_finish(result, storage_root=storage_root, verified_source=verified_source)
    return result


def verify_generation_finish(result, *, storage_root, verified_source):
    """Recheck both protected source and immutable derivative before handoff."""
    if not isinstance(result, dict) or result.get("state") != "verified":
        raise ValueError("A verified finishing version is required.")
    root = Path(storage_root).absolute() / "finishing"
    if Path(result.get("store_root", "")).absolute() != root:
        raise ValueError("The finishing version belongs to another workspace.")
    recovered = DerivativeStore(root).recover(result.get("fingerprint", ""))
    if recovered.get("state") != "verified" or recovered.get("output") != result.get("output"):
        raise ValueError("The finishing version changed after review.")
    plan = recovered["plan"]
    if plan.get("recipe", {}).get("policy", {}).get("source_kind") != "verified_generation":
        raise ValueError("The finishing source authority is missing.")
    _source(plan["source"], storage_root, verified_source)
    return Path(recovered["output"])


def verified_generation_refinement(source, width, height, *, storage_root, verified_source):
    """Return only a source-bound Remix intent; no generation is performed."""
    detail = inspect_generation_finish_source(source, storage_root=storage_root,
                                               verified_source=verified_source)
    _dimensions(width, height, (detail["width"], detail["height"]))
    return {"path": Path(detail["source"]), "source_sha256": detail["sha256"],
            "task": "remix", "strength": 0.25,
            "output_goal": {"schema_id": "collectigen.output-goal.v1",
                            "final_width": width, "final_height": height,
                            "detail_preference": "generative"}}


def discover_generation_finishes(storage_root, sources, verified_source, *,
                                 cancelled=lambda: False, max_entries=256,
                                 max_results_per_source=32):
    """Bounded, cancellable recovery of only exact selected generated sources."""
    if (type(max_entries) is not int or not 1 <= max_entries <= 1024
            or type(max_results_per_source) is not int or not 1 <= max_results_per_source <= 32):
        raise ValueError("Choose bounded finishing discovery limits.")
    root = Path(storage_root).absolute() / "finishing"
    _assert_unredirected(root)
    selected = tuple(Path(value).absolute() for value in sources)
    if not selected or len(selected) > 32 or len(set(selected)) != len(selected):
        raise ValueError("Choose distinct selected generation sources.")
    versions = {source: [] for source in selected}
    if not root.exists():
        return {"versions": versions, "unavailable": 0, "truncated": False}
    if not root.is_dir():
        raise ValueError("The saved finishing folder is unavailable.")
    store = DerivativeStore(root)
    unavailable = 0; truncated = False
    for index, entry in enumerate(root.iterdir()):
        if cancelled():
            raise InterruptedError("Saved version search cancelled.")
        if index >= max_entries:
            truncated = True; break
        if re.fullmatch(r"[a-f0-9]{64}", entry.name) is None:
            continue
        try:
            result = store.recover(entry.name)
            source = Path(result.get("plan", {}).get("source", "")).absolute()
            if source not in versions:
                continue
            verify_generation_finish(result, storage_root=storage_root,
                                     verified_source=verified_source)
            if len(versions[source]) < max_results_per_source:
                versions[source].append(result)
            else:
                truncated = True
        except InterruptedError:
            raise
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            unavailable += 1
    return {"versions": versions, "unavailable": unavailable, "truncated": truncated}
