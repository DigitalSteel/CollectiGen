"""Exact Wan2.2 TI2V-5B binding for one held, local Comfy runtime.

Preparation only reads the held runtime and caller-provided source. The one
owned I2V upload is deferred until VideoExecutionService has saved its receipt.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4
import re

from PIL import Image

from .comfy_video_runtime import _read_owned_file, verify_runtime
from .comfy_video_transport import ComfyVideoTransport, MAX_SOURCE_BYTES
from .generation_resource_guard import assess_memory
from .video_generation import VideoGenerationRequest
from .video_generation_graph import (
    ResourceBinding, UploadedImageReference, build_wan22_ti2v_graph,
)


ADAPTER_VERSION = "collectigen-wan22-ti2v5b-comfy-v1"
_ADMITTED_COMFY_VERSIONS = frozenset({"0.3.59", "0.36.0"})
_VERSION_LINE = re.compile(rb"__version__\s*=\s*['\"]([^'\"\r\n]+)['\"]")
_RECIPE = {"graph": "collectigen.comfy-wan22-ti2v5b-graph.v1",
           "nodes": ["UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
                     "ModelSamplingSD3", "Wan22ImageToVideoLatent", "LoadImage",
                     "KSampler", "VAEDecode", "CreateVideo", "SaveVideo"],
           "latent": "Wan22ImageToVideoLatent.center-bilinear",
           "sampler": {"steps": 20, "cfg": 5.0, "sampler": "uni_pc", "scheduler": "simple"},
           "output": {"container": "mp4", "codec": "h264", "audio": "silent"}}
TEMPLATE_SHA256 = hashlib.sha256(json.dumps(_RECIPE, sort_keys=True,
    separators=(",", ":")).encode()).hexdigest()
_NODES = tuple(dict.fromkeys(_RECIPE["nodes"]))
_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")
_MAX_VRAM_BYTES = (1 << 64) - 1


def validate_protected_usage_context(value, prepared):
    """Validate retained shape/bindings; this alone grants no live authority."""
    from .comfy_video_lifecycle import OWNER_ID, OWNER_VERSION, INTENT_NAME
    keys = {"execution_id", "owner_id", "owner_version", "protected_mode", "owner_locator",
            "runtime_fingerprint", "resource_ids", "required_set_digest", "binding_digest"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("protected_mode") is not True
            or value.get("owner_id") != OWNER_ID or value.get("owner_version") != OWNER_VERSION
            or not isinstance(value.get("execution_id"), str) or not _ATTEMPT.fullmatch(value["execution_id"])
            or not isinstance(value.get("owner_locator"), str) or len(value["owner_locator"]) > 4096
            or not Path(value["owner_locator"]).is_absolute()
            or Path(value["owner_locator"]).name != INTENT_NAME
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                for key in ("runtime_fingerprint", "required_set_digest", "binding_digest"))):
        raise ValueError("The exact protected video runtime ownership is unavailable.")
    rows = prepared.get("graph_plan", {}).get("manifest", {}).get("resources")
    ids = value["resource_ids"]
    if (not isinstance(rows, list) or len(rows) != 3 or any(not isinstance(row, dict) for row in rows)
            or {row.get("role") for row in rows} != {"transformer", "text_encoder", "vae"}
            or not isinstance(ids, (list, tuple)) or len(ids) != 3
            or any(not isinstance(identity, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity) for identity in ids)
            or list(ids) != sorted(set(ids)) or set(ids) != {row.get("resource_id") for row in rows}
            or value["runtime_fingerprint"] != prepared.get("runtime_fingerprint")):
        raise ValueError("The protected runtime differs from the complete reviewed video input set.")
    return {**deepcopy(value), "resource_ids": list(ids)}


def expected_resize_transform(source_sha256: str, source_width: int, source_height: int,
                              target_width: int, target_height: int) -> str:
    """Identity for native Wan22ImageToVideoLatent center-crop/bilinear behavior."""
    evidence = {"operation": "comfy.Wan22ImageToVideoLatent.center-bilinear.v1",
                "source_sha256": source_sha256, "source_size": [source_width, source_height],
                "target_size": [target_width, target_height]}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def _png_identity(raw: bytes) -> tuple[int, int]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("The I2V source must be bounded exact PNG bytes.")
    with Image.open(BytesIO(raw)) as image:
        if (image.format != "PNG" or getattr(image, "n_frames", 1) != 1
                or image.width * image.height > 16_777_216):
            raise ValueError("I2V needs one bounded PNG image.")
        size = image.size
        image.verify()
    return size


class NativeComfyVideoAdapter:
    def __init__(self, runtime_receipt, source_image_provider=None,
                 source_state_provider=None, transport=None):
        self.runtime_receipt = runtime_receipt
        self.source_image_provider = source_image_provider
        self.source_state_provider = source_state_provider
        self.transport = transport or ComfyVideoTransport(
            f"http://127.0.0.1:{runtime_receipt.preview.config.port}")
        self.prepared_memory_observation = None

    @staticmethod
    def _memory_observation(device):
        """Keep only bounded byte counts from the one qualified execution GPU."""
        total, free = device.get("vram_total"), device.get("vram_free")
        if (type(total) is not int or type(free) is not int
                or not 0 < total <= _MAX_VRAM_BYTES
                or not 0 <= free <= total):
            return {"status": "unknown", "available_vram_gib": None,
                    "total_vram_gib": None,
                    "message": "Current free VRAM is unavailable from this native runtime."}
        return {"status": "measured", "available_vram_gib": free / 1024**3,
                "total_vram_gib": total / 1024**3,
                "message": "Current free VRAM was observed on the qualified GPU; it can change before allocation."}

    def _runtime(self):
        preview = verify_runtime(self.runtime_receipt)
        if preview.fingerprint != self.runtime_receipt.preview.fingerprint:
            raise ValueError("The owned video runtime changed.")
        version_path = Path(preview.config.source_root) / "comfyui_version.py"
        version_raw = _read_owned_file(version_path, 16_384)
        if hashlib.sha256(version_raw).hexdigest() != preview.source_hashes.get("comfyui_version.py"):
            raise ValueError("The owned Comfy source version file changed.")
        matches = _VERSION_LINE.findall(version_raw)
        if len(matches) != 1:
            raise ValueError("The owned Comfy source has no unambiguous version declaration.")
        source_version = matches[0].decode("ascii")
        if source_version not in _ADMITTED_COMFY_VERSIONS:
            raise ValueError("The owned Comfy source version is not admitted.")
        stats = self.transport.system_stats()
        system = stats.get("system") if isinstance(stats, dict) else None
        if not isinstance(system, dict) or system.get("comfyui_version") != source_version:
            raise ValueError("The live Comfy version differs from its exact held source.")
        devices = stats.get("devices")
        device = devices[0] if isinstance(devices, list) and len(devices) == 1 else None
        if (not isinstance(device, dict) or device.get("type") != "cuda"
                or type(device.get("vram_total")) is not int or device["vram_total"] < 15 * 1024**3):
            raise ValueError("This first video recipe requires a qualified 16 GiB-class CUDA GPU. Smaller-memory or other devices need separate qualification; total capacity does not guarantee free memory.")
        info = {}
        for node in _NODES:
            answer = self.transport.object_info(node)
            if not isinstance(answer, dict) or not isinstance(answer.get(node), dict):
                raise ValueError(f"Native Comfy lacks the required {node} node schema.")
            info[node] = deepcopy(answer[node])
        # LoadImage's choices include unrelated user uploads. They cannot be
        # graph identity. Keep the schema/options, but hash an empty choice list;
        # verify the exact owned name against fresh choices after upload.
        load = info["LoadImage"].get("input", {}).get("required", {}).get("image")
        if (not isinstance(load, (list, tuple)) or not load or not isinstance(load[0], list)
                or any(not isinstance(choice, str) for choice in load[0])):
            raise ValueError("Native LoadImage lacks an enumerated image choice.")
        load[0] = []
        return preview, info, source_version, self._memory_observation(device)

    @staticmethod
    def _bindings(preview):
        bound = preview.model_bindings
        return tuple(ResourceBinding(role, bound[role]["resource_id"], bound[role]["file_name"])
                     for role in ("transformer", "text_encoder", "vae"))

    def bind_request_versions(self, request: VideoGenerationRequest) -> VideoGenerationRequest:
        """Fill only explicit adapter/runtime versions and selected companions."""
        if not isinstance(request, VideoGenerationRequest):
            raise ValueError("A typed video request is required.")
        preview, _, version, _ = self._runtime()
        transformer, encoder, vae = self._bindings(preview)
        if request.model_resource_id != transformer.resource_id:
            raise ValueError("The requested transformer differs from the selected runtime model.")
        required = {"resources": (encoder.resource_id, vae.resource_id),
                    "template_version": "sha256:" + TEMPLATE_SHA256,
                    "adapter_version": ADAPTER_VERSION, "runtime_version": version}
        if any(getattr(request, key) not in ((), "", value)
               for key, value in required.items()):
            raise ValueError("Explicit video resource or version settings differ from the held runtime.")
        return replace(request, resources=(encoder.resource_id, vae.resource_id),
                       template_version="sha256:" + TEMPLATE_SHA256,
                       adapter_version=ADAPTER_VERSION, runtime_version=version)

    def _source(self, request):
        if request.task != "i2v":
            return None, None, None
        if not callable(self.source_image_provider) or not callable(self.source_state_provider):
            raise ValueError("I2V needs exact source bytes and source-state providers.")
        source = request.source_image
        raw = self.source_image_provider(source)
        width, height = _png_identity(raw)
        if len(raw) != source.byte_length or hashlib.sha256(raw).hexdigest() != source.sha256:
            raise ValueError("The I2V source bytes differ from the reviewed source.")
        lineage = self.source_state_provider(source)
        if isinstance(lineage, str):
            lineage = {"source_state_sha256": lineage}
        if not isinstance(lineage, dict) or lineage.get("source_state_sha256") != source.source_state_sha256:
            raise ValueError("The I2V source state differs from the reviewed source.")
        if len(json.dumps(lineage, sort_keys=True, allow_nan=False).encode()) > 16_384:
            raise ValueError("I2V source lineage exceeds the bounded snapshot.")
        if (width, height) != (request.width, request.height):
            expected = expected_resize_transform(source.sha256, width, height,
                                                 request.width, request.height)
            if source.transform_sha256 != expected:
                raise ValueError("The I2V center-crop transform lacks its exact reviewed identity.")
        elif source.transform_sha256:
            raise ValueError("An unchanged I2V image cannot claim a resize transform.")
        return raw, (width, height), deepcopy(lineage)

    def _build(self, request, preview, info, version, output_token, source_size):
        transformer, encoder, vae = self._bindings(preview)
        if (request.model_resource_id != transformer.resource_id
                or tuple(request.resources) != (encoder.resource_id, vae.resource_id)):
            raise ValueError("Video request models differ from the exact runtime bindings.")
        uploaded = None
        if source_size is not None:
            source = request.source_image
            uploaded = UploadedImageReference(source.sha256, source.source_state_sha256,
                source.byte_length, source_size[0], source_size[1],
                f"collectigen_{output_token}_source.png", source.transform_sha256)
        return build_wan22_ti2v_graph(request, transformer=transformer,
            text_encoder=encoder, vae=vae, object_info=info,
            template_sha256=TEMPLATE_SHA256, runtime_version=version,
            adapter_version=ADAPTER_VERSION, output_token=output_token,
            uploaded_image=uploaded, planned_owned_upload=uploaded is not None)

    def prepare(self, request: VideoGenerationRequest, *, reviewed_loss=False):
        if not isinstance(request, VideoGenerationRequest):
            raise ValueError("A typed video request is required.")
        preview, info, version, memory = self._runtime()
        raw, source_size, lineage = self._source(request)
        token = uuid4().hex
        plan = self._build(request, preview, info, version, token, source_size)
        if plan.requires_review and reviewed_loss is not True:
            # Still return the reviewable plan; execution enforces the review gate.
            pass
        result = {"graph_plan": plan.to_dict(), "reviewed_loss": reviewed_loss is True,
                  "runtime_fingerprint": preview.fingerprint,
                  "runtime_nonce": self.runtime_receipt.nonce}
        self.prepared_memory_observation = {**memory,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "assessment": assess_memory(memory)}
        if raw is not None:
            result["source_image_base64"] = base64.b64encode(raw).decode("ascii")
            result["source_lineage"] = lineage
        return result

    def revalidate(self, prepared):
        if not isinstance(prepared, dict) or not isinstance(prepared.get("graph_plan"), dict):
            raise ValueError("A prepared exact video graph is required.")
        preview, info, version, memory = self._runtime()
        if (prepared.get("runtime_fingerprint") != preview.fingerprint
                or prepared.get("runtime_nonce") != self.runtime_receipt.nonce):
            raise ValueError("The reviewed video runtime changed.")
        plan = prepared["graph_plan"]
        request = VideoGenerationRequest.from_dict(plan["manifest"]["request"])
        raw, source_size, lineage = self._source(request)
        if raw is None:
            if "source_image_base64" in prepared or "source_lineage" in prepared:
                raise ValueError("A T2V graph cannot carry an image source.")
        elif (prepared.get("source_image_base64") != base64.b64encode(raw).decode("ascii")
              or prepared.get("source_lineage") != lineage):
            raise ValueError("The reviewed I2V source snapshot changed.")
        rebuilt = self._build(request, preview, info, version,
                              plan["manifest"]["output_token"], source_size)
        if rebuilt.to_dict() != plan:
            raise ValueError("The reviewed native graph or node schema changed.")
        if "protected_usage_context" in prepared:
            context = self._protected_usage_context(prepared)
            if context is None or context != prepared["protected_usage_context"]:
                raise ValueError("The video attempt's protected runtime ownership changed.")
        return {**memory, "observed_at": datetime.now(timezone.utc).isoformat(),
                "assessment": assess_memory(memory)}

    def _protected_usage_context(self, prepared):
        if not getattr(self.runtime_receipt.preview.config, "protected_loader", False):
            return None
        verifier = getattr(self.runtime_receipt, "protected_verifier", None)
        if not callable(verifier):
            raise ValueError("Protected video usage needs the held lifecycle owner.")
        context = validate_protected_usage_context(verifier(self.runtime_receipt), prepared)
        expected = sorted(binding.resource_id for binding in self._bindings(self.runtime_receipt.preview))
        if context["resource_ids"] != expected:
            raise ValueError("The held runtime's protected model set changed.")
        return context

    def protected_usage_context(self, prepared):
        """Read fresh owned authority; manual/legacy runtimes have none."""
        self.revalidate(prepared)
        return self._protected_usage_context(prepared)

    def upload_source(self, prepared):
        """Call only after the execution service durably retains this attempt."""
        self.revalidate(prepared)
        plan = prepared["graph_plan"]
        uploaded = plan["manifest"]["uploaded_image"]
        if uploaded is None:
            return None
        identity = prepared.get("execution_identity")
        if (not isinstance(identity, dict)
                or any(not isinstance(identity.get(key), str)
                       or not _ATTEMPT.fullmatch(identity[key])
                       for key in ("job_id", "attempt_id"))):
            raise ValueError("The I2V upload needs a durably assigned execution attempt.")
        raw = base64.b64decode(prepared["source_image_base64"], validate=True)
        receipt = self.transport.upload_source(raw,
            source_sha256=uploaded["source_sha256"],
            output_token=plan["manifest"]["output_token"])
        if (not isinstance(receipt, dict) or receipt.get("name") != uploaded["upload_name"]
                or receipt.get("type") != "input" or receipt.get("subfolder", "") != ""):
            raise ValueError("Native upload did not retain the exact planned owned name.")
        answer = self.transport.object_info("LoadImage")
        schema = (answer.get("LoadImage", {}).get("input", {}).get("required", {})
                  .get("image")) if isinstance(answer, dict) else None
        choices = schema[0] if isinstance(schema, (list, tuple)) and schema else None
        if not isinstance(choices, list) or uploaded["upload_name"] not in choices:
            raise ValueError("Native LoadImage does not list the exact uploaded source.")
        return {"name": uploaded["upload_name"], "source_sha256": uploaded["source_sha256"],
                "source_state_sha256": uploaded["source_state_sha256"],
                "byte_length": uploaded["byte_length"],
                "transform_sha256": uploaded["transform_sha256"]}
