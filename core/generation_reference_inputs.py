"""Explicitly selected additional reference files with bounded stale checks."""
from __future__ import annotations
from copy import deepcopy
import hashlib
from io import BytesIO
import os
from pathlib import Path
import stat
from PIL import Image
from .generation_planning import GenerationPlan
from .training_source_io import _assert_unredirected, _identity


def read_reference(path):
    path = Path(path).absolute()
    _assert_unredirected(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
            raise ValueError("Each reference must be one regular file of at most 4 MiB.")
        raw = stream.read(4 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    _assert_unredirected(path)
    if len(raw) > 4 * 1024 * 1024 or _identity(before) != _identity(after) or _identity(after) != _identity(path.stat(follow_symlinks=False)):
        raise ValueError("The reference changed while its bounded bytes were read.")
    with Image.open(BytesIO(raw)) as image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1 or image.width * image.height > 16_777_216:
            raise ValueError("Visual references require one bounded PNG per image.")
        width, height = image.size
        image.verify()
    return {"path": path, "bytes": raw, "sha256": hashlib.sha256(raw).hexdigest(), "width": width, "height": height}


def rebase_reference_identity(plan, identity):
    if plan.source_identity == identity:
        return plan
    safety = deepcopy(plan.safety_policy); safety["reviewed"] = False
    request = {key: value for key, value in plan.request.to_dict().items()
               if key not in {"raw", "source", "source_field"}}
    return GenerationPlan.create(plan.source, intent=plan.intent, goal=plan.goal,
        source_identity=identity).revise({"request": request,
        "target_context": plan.target_context, "knowledge_fragments": list(plan.knowledge_fragments),
        "safety_policy": safety}, source="selected_reference_images",
        consequence="Explicitly changed source references; all execution readiness must be checked again")
