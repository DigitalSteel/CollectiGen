"""Pure, exact single-image conditioning translation for local SwarmUI.

No runtime, filesystem, or acquisition authority is granted by this mapping.
The caller must verify the selected backend, source snapshot, and returned
model evidence independently before using the wire parameters.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
from io import BytesIO
import json
import math
import re

from PIL import Image


_SHA = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_FAMILIES = frozenset({"sd1", "sdxl", "stable-diffusion-v1", "stable-diffusion-xl-v1"})
_INPUT_KEYS = frozenset({"kind", "source_sha256", "source_width", "source_height",
    "target_width", "target_height", "resize", "resource_id", "strength",
    "start", "end", "preprocessor", "extras"})
_CONTROL_PARAMS = frozenset({"controlnetmodel", "controlnetimageinput",
    "controlnetstrength", "controlnetstart", "controlnetend", "controlnetpreprocessor"})
_REFERENCE_PARAMS = frozenset({"promptimages", "usereferenceonly",
    "revisionstrength", "smartimagepromptresizing"})
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _sha(value, label):
    matched = _SHA.fullmatch(value) if isinstance(value, str) else None
    if not matched:
        raise ValueError(f"{label} needs an exact SHA-256 identity.")
    return matched.group(1)


def _image(raw, label):
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(f"{label} must be bounded PNG bytes.")
    with Image.open(BytesIO(raw)) as image:
        if (image.format != "PNG" or getattr(image, "n_frames", 1) != 1
                or image.width * image.height > 16_777_216):
            raise ValueError(f"{label} must be one bounded PNG image.")
        dimensions = image.size
        image.verify()
    return dimensions


def _dimensions(value, label):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(side) is not int or side < 64 or side > 8192 for side in value)):
        raise ValueError(f"{label} needs explicit bounded dimensions.")
    return tuple(value)


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{label} must be a finite fraction from 0 to 1.")
    return float(value)


def _name(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or value.startswith(("/", "\\")) or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("The exact ControlNet model name is not safe for the wire.")
    return value


def prepare_conditioning(input, frozen_image, binding=None, capabilities=None, family=None):
    """Return exact wire params and provenance for one reviewed conditioning image.

    `frozen_image` is source PNG bytes, or a mapping with `source_bytes` and
    `wire_bytes` for an explicitly labeled, already performed resize. No
    resizing is performed here. `binding` is required only for ControlNet.
    """
    if not isinstance(input, Mapping) or set(input) - _INPUT_KEYS:
        raise ValueError("Conditioning input has unmapped fields.")
    kind = input.get("kind")
    if kind not in {"control_image", "reference_image"}:
        raise ValueError("Choose one supported conditioning image kind.")
    if family not in _FAMILIES:
        raise ValueError("Conditioning is limited to the qualified SD1/SDXL family mapping.")
    if not isinstance(capabilities, Mapping) or not isinstance(capabilities.get("parameters"), Mapping):
        raise ValueError("Check the local Swarm parameter catalog first.")
    required = _CONTROL_PARAMS if kind == "control_image" else _REFERENCE_PARAMS
    if not required.issubset(capabilities["parameters"]):
        raise ValueError("The local Swarm adapter lacks conditioning parameters.")
    features = capabilities.get("features")
    if not isinstance(features, (list, tuple, set, frozenset)):
        raise ValueError("The selected model has no verified feature flags.")
    needed_feature = "controlnet" if kind == "control_image" else "supports_reference_only"
    if needed_feature not in features:
        raise ValueError(f"The selected model does not advertise {needed_feature}.")
    if not isinstance(capabilities.get("fingerprint"), str) or not capabilities["fingerprint"]:
        raise ValueError("The local conditioning capability fingerprint is missing.")
    extras = input.get("extras", {})
    if not isinstance(extras, Mapping):
        raise ValueError("Conditioning provenance extras must be an object.")
    encoded_extras = json.dumps(extras, sort_keys=True, allow_nan=False,
                                separators=(",", ":")).encode()
    if len(encoded_extras) > 16_384:
        raise ValueError("Conditioning provenance extras exceed the bounded contract.")
    source_sha = _sha(input.get("source_sha256"), "Conditioning source")
    source_size = _dimensions((input.get("source_width"), input.get("source_height")), "Source image")
    target_size = _dimensions((input.get("target_width"), input.get("target_height")), "Target image")
    if isinstance(frozen_image, bytes):
        source_raw = wire_raw = frozen_image
    elif isinstance(frozen_image, Mapping) and set(frozen_image) == {"source_bytes", "wire_bytes"}:
        source_raw, wire_raw = frozen_image["source_bytes"], frozen_image["wire_bytes"]
    else:
        raise ValueError("Provide one exact source PNG or source and explicit resized PNG bytes.")
    if (_image(source_raw, "Source image") != source_size
            or hashlib.sha256(source_raw).hexdigest() != source_sha):
        raise ValueError("The conditioning source bytes differ from the selected image.")
    wire_size = _image(wire_raw, "Wire image")
    resize = input.get("resize")
    if source_size == target_size:
        if resize is not None or wire_raw != source_raw or wire_size != target_size:
            raise ValueError("An unchanged conditioning image cannot claim a resize.")
    else:
        if (not isinstance(resize, Mapping)
                or set(resize) != {"operation", "output_sha256"}
                or resize.get("operation") not in {"contain", "cover", "stretch"}
                or wire_size != target_size
                or _sha(resize.get("output_sha256"), "Resized image")
                   != hashlib.sha256(wire_raw).hexdigest()):
            raise ValueError("Resized conditioning requires labeled exact output bytes and dimensions.")
    data_url = "data:image/png;base64," + base64.b64encode(wire_raw).decode("ascii")
    summary = {"kind": kind, "source_sha256": source_sha,
               "source_byte_length": len(source_raw), "source_dimensions": list(source_size),
               "wire_sha256": hashlib.sha256(wire_raw).hexdigest(),
               "wire_byte_length": len(wire_raw), "wire_dimensions": list(wire_size),
               "resize": dict(resize) if resize is not None else None,
               "extras": dict(extras), "capability_fingerprint": capabilities["fingerprint"]}
    if kind == "control_image":
        if not isinstance(binding, Mapping) or binding.get("verified") is not True:
            raise ValueError("ControlNet needs one verified local model binding.")
        resource_id = input.get("resource_id")
        full_sha = _sha(resource_id, "ControlNet selected model")
        if (binding.get("role") not in {"controlnet", "control_model"}
                or _sha(binding.get("resource_id"), "ControlNet binding") != full_sha
                or binding.get("capability_fingerprint") != capabilities["fingerprint"]):
            raise ValueError("ControlNet binding differs from the exact selected model.")
        tensor_sha = _sha(binding.get("tensor_sha256"), "ControlNet cached tensor")
        model_name = _name(binding.get("name"))
        strength = _number(input.get("strength"), "ControlNet strength")
        start = _number(input.get("start"), "ControlNet start")
        end = _number(input.get("end"), "ControlNet end")
        if strength == 0 or start >= end:
            raise ValueError("ControlNet guidance must affect a nonempty sampling interval.")
        if input.get("preprocessor") != "None":
            raise ValueError("Only explicit None preprocessing is admitted.")
        payload = {"controlnetmodel": model_name, "controlnetimageinput": data_url,
                   "controlnetstrength": strength, "controlnetstart": start,
                   "controlnetend": end, "controlnetpreprocessor": "None"}
        expected_models = [{"role": "controlnet", "swarm_name": model_name,
                            "full_file_sha256": full_sha, "tensor_sha256": tensor_sha}]
    else:
        if binding not in (None, {}):
            raise ValueError("Reference Only cannot imply an extra model binding.")
        if any(key in input for key in ("resource_id", "strength", "start", "end", "preprocessor")):
            raise ValueError("Reference Only cannot carry ControlNet settings.")
        payload = {"promptimages": data_url, "usereferenceonly": True,
                   "revisionstrength": 0, "smartimagepromptresizing": False}
        expected_models = []
    return {"wireparams": payload, "expectedparams": dict(payload),
            "source_summary": summary, "expected_models": expected_models}


def prepare_reference_batch(inputs, frozen_images, capabilities, family):
    """Map two to four exact sources to Swarm's ordered Reference Only image list.

    Each source is checked by the same single-reference contract. The Swarm
    IMAGE_LIST wire format is `|` separated data URLs; base64 cannot contain `|`.
    The managed workflow batches the resulting reference latents. It does not
    assign a distinct composition, style, or identity role to any image.
    """
    if (not isinstance(inputs, (list, tuple)) or not isinstance(frozen_images, (list, tuple))
            or not 2 <= len(inputs) == len(frozen_images) <= 4):
        raise ValueError("Reference Only needs two to four reviewed images.")
    mapped = [prepare_conditioning(selected, raw, None, capabilities, family)
              for selected, raw in zip(inputs, frozen_images)]
    sources = [row["source_summary"]["source_sha256"] for row in mapped]
    if len(set(sources)) != len(sources):
        raise ValueError("Choose distinct exact reference images.")
    if sum(row["source_summary"]["wire_byte_length"] for row in mapped) > 16 * 1024 * 1024:
        raise ValueError("The combined reference images exceed the 16 MiB bound.")
    wire = "|".join(row["wireparams"]["promptimages"] for row in mapped)
    summary = dict(mapped[0]["source_summary"])
    summary["wire_sha256"] = hashlib.sha256(wire.encode("ascii")).hexdigest()
    summary["wire_byte_length"] = sum(row["source_summary"]["wire_byte_length"] for row in mapped)
    summary["references"] = [row["source_summary"] for row in mapped]
    summary["reference_count"] = len(mapped)
    summary["guidance_semantics"] = "general_reference_only_batched_latents"
    payload = dict(mapped[0]["wireparams"], promptimages=wire)
    return {"wireparams": payload, "expectedparams": dict(payload),
            "source_summary": summary, "expected_models": []}
