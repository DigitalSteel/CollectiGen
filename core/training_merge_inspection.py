"""Read-only comparison of explicitly selected Merge inputs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .model_resource_registry import _json, inspect_model_resource
from .training_configuration import _looks_machine_local
from .training_merge_policy import MAX_ELEMENTS, OPERATIONS


MAX_FILE_BYTES = 256 * 1024
MODULE_FAMILIES = {
    "networks.lora_anima": "anima",
    "networks.lora_krea2": "krea2",
    "musubi_tuner.networks.lora_krea2": "krea2",
}
_TARGET = re.compile(r"lora_unet_[A-Za-z0-9_]+\Z")
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True)
class MergeFile:
    path: Path
    resource_id: str
    size: int
    header: bytes
    family: str
    base_sha256: str
    targets: tuple[tuple[str, int, int, int], ...]  # name, rank, input, output
    issues: tuple[str, ...]


def _cancel(callback):
    if callback is not None and callback():
        raise InterruptedError("Merge cancelled; retain any recovery folder for review.")


def inspect_merge_file(path, *, progress=None, should_cancel=None):
    """Validate metadata and hash selected bytes; never deserialize tensor values.

    Family/base declarations remain untrusted evidence, not architecture proof.
    Filenames and matching dimensions never supply missing family evidence.
    """
    headers = []
    inspection = inspect_model_resource(path, progress=progress, should_cancel=should_cancel,
                                        header_observer=headers.append)
    raw = headers[0]
    header = _json(raw)
    metadata = header.get("__metadata__", {})
    module = metadata.get("ss_network_module", "")
    family = MODULE_FAMILIES.get(module, "unknown")
    issues = []
    if any(_looks_machine_local(text) for pair in metadata.items() for text in pair):
        # Preserve originals; portable provenance must not copy local metadata paths.
        # Both keys and values are untrusted, including embedded JSON strings.
        issues.append("Input metadata contains machine-local paths; portable merge is held. Originals are unchanged.")
    if family == "unknown":
        issues.append("Missing or unsupported network-module family declaration; filenames cannot identify compatibility.")
    expected_hint = {"anima": "anima", "krea2": "krea"}.get(family, "")
    for field in ("ss_base_model_version", "modelspec.architecture"):
        declared = metadata.get(field, "").lower()
        if declared and (not expected_hint or expected_hint not in declared or
                         ("anima" in declared and family != "anima") or
                         ("krea" in declared and family != "krea2")):
            issues.append("Conflicting or unrecognized family declarations; inspect the source adapter.")
    base = metadata.get("ss_new_sd_model_hash", "")
    if not _SHA.fullmatch(base):
        issues.append("An exact 64-digit base-model SHA-256 declaration is required; short hashes are insufficient.")
        base = ""
    try:
        args = json.loads(metadata.get("ss_network_args", "{}"))
    except (ValueError, TypeError):
        args = None
    if args != {}:
        issues.append("Network arguments/scaling extensions are not qualified; only ordinary alpha/rank LoRA is admitted.")
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    targets = []
    consumed = set()
    for key, row in tensors.items():
        if not key.endswith(".lora_down.weight"):
            continue
        name = key.removesuffix(".lora_down.weight")
        up_name, alpha_name = name + ".lora_up.weight", name + ".alpha"
        up, alpha = tensors.get(up_name), tensors.get(alpha_name)
        if not _TARGET.fullmatch(name) or up is None or alpha is None:
            issues.append("Each ordinary DiT target needs matching down/up weights and an explicit alpha tensor.")
            continue
        down_shape, up_shape = row["shape"], up["shape"]
        if (len(down_shape) != 2 or len(up_shape) != 2 or
                not all(down_shape + up_shape) or down_shape[0] != up_shape[1] or
                alpha["shape"] not in ([], [1])):
            issues.append("Only paired nonempty linear LoRA matrices and scalar alpha are supported; convolution/split layouts are held.")
            continue
        targets.append((name, down_shape[0], down_shape[1], up_shape[0]))
        consumed.update((key, up_name, alpha_name))
    if not targets or consumed != set(tensors):
        issues.append("Unrecognized or incomplete tensors would be lost; this file cannot use the ordinary LoRA adapter.")
    if any(row["dtype"] != "F32" for row in tensors.values()):
        issues.append("This component writes F32 only; BF16/FP16, quantized and mixed-precision execution remain unqualified.")
    return MergeFile(inspection.path, inspection.resource_id, inspection.size, raw, family,
                     base.lower(), tuple(sorted(targets)), tuple(dict.fromkeys(issues)))


def _compare_inputs(inputs, operation):
    issues = [issue for item in inputs for issue in item.issues]
    if operation != "combine_loras":
        issues.append("Checkpoint blending and baking need qualified checkpoint target mappings; this file adapter only combines LoRAs.")
    if len({item.family for item in inputs}) != 1:
        issues.append("Cross-family/architecture merging is unsupported; use target-family training for concept transfer.")
    if len({item.base_sha256 for item in inputs}) != 1 or not inputs[0].base_sha256:
        issues.append("Adapter base-model SHA-256 declarations must match exactly.")
    if len({item.resource_id for item in inputs}) != len(inputs):
        issues.append("The same input bytes were selected more than once; remove duplicate inputs.")
    layouts = [tuple((name, width, height) for name, _rank, width, height in item.targets) for item in inputs]
    if any(layout != layouts[0] for layout in layouts[1:]):
        issues.append("Adapter target sets or linear dimensions differ; partial-target mapping is not qualified.")
    ranks = ()
    output_bytes = 0
    if layouts[0] and all(layout == layouts[0] for layout in layouts):
        ranks = tuple((name, sum(item.targets[index][1] for item in inputs))
                      for index, (name, _width, _height) in enumerate(layouts[0]))
        for (name, width, height), (_, rank) in zip(layouts[0], ranks):
            output_bytes += 4 * (rank * (width + height) + 1)
            if max(width * height, rank * width, rank * height) > MAX_ELEMENTS:
                issues.append("Concatenated rank or target size exceeds this bounded component's 4096-element limit.")
    if any(item.size > MAX_FILE_BYTES for item in inputs) or output_bytes > MAX_FILE_BYTES // 2:
        issues.append("Real-size execution is held: this component admits only small files up to 256 KiB and bounded output.")
    return issues, ranks, output_bytes


@dataclass(frozen=True)
class MergeInputReview:
    operation: str
    inputs: tuple[MergeFile, ...]
    issues: tuple[str, ...]
    output_ranks: tuple[tuple[str, int], ...]
    output_data_bytes: int


def inspect_merge_inputs(paths, *, operation="combine_loras", progress=None, should_cancel=None):
    """Read-only exact input comparison: no destination, capacity query or writer."""
    if operation not in OPERATIONS:
        raise ValueError("Unknown merge operation.")
    paths = tuple(paths)
    if not 2 <= len(paths) <= 4:
        raise ValueError("Select two to four explicit adapter files.")
    inputs = []
    for index, path in enumerate(paths):
        _cancel(should_cancel)
        def report(current, total, message, index=index):
            if progress: progress(current, total, f"File {index + 1}/{len(paths)} — {message}")
        inputs.append(inspect_merge_file(path, progress=report, should_cancel=should_cancel))
    inputs = tuple(inputs)
    issues, ranks, output_bytes = _compare_inputs(inputs, operation)
    for index, item in enumerate(inputs):
        _cancel(should_cancel)
        if progress: progress(0, 0, f"Rechecking file {index + 1}/{len(inputs)} identity")
        if inspect_model_resource(item.path, should_cancel=should_cancel).resource_id != item.resource_id:
            raise ValueError("A selected file changed during inspection. Choose Inspect again.")
    _cancel(should_cancel)
    return MergeInputReview(operation, inputs, tuple(dict.fromkeys(issues)), ranks, output_bytes)
