"""Pure, reviewable ComfyUI API graph for the native Wan2.2 TI2V-5B recipe.

Bindings and object_info are caller-supplied evidence, not model discovery or
runtime admission. A GraphPlan is never authority to upload, submit, or publish.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping

from core.video_generation import VideoGenerationRequest


GRAPH_SCHEMA = "collectigen.comfy-wan22-ti2v5b-graph.v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,190}\.safetensors\Z")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{11,63}\Z")
_UPLOAD = re.compile(r"collectigen_[a-z0-9][a-z0-9_-]{11,63}_source\.png\Z")


class GraphContractError(ValueError):
    """The requested graph would lack exact evidence or change intent silently."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GraphContractError(message)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise GraphContractError("Graph evidence must be finite JSON.") from exc


@dataclass(frozen=True)
class ResourceBinding:
    role: str
    resource_id: str
    file_name: str

    def __post_init__(self) -> None:
        _require(self.role in {"transformer", "text_encoder", "vae"}, "Unknown resource role.")
        _require(isinstance(self.resource_id, str) and bool(_RESOURCE.fullmatch(self.resource_id)),
                 "Resource binding needs a full SHA-256 identity.")
        _require(isinstance(self.file_name, str) and bool(_FILE.fullmatch(self.file_name)),
                 "Resource binding needs one safe explicit safetensors name.")

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "resource_id": self.resource_id, "file_name": self.file_name}


@dataclass(frozen=True)
class UploadedImageReference:
    """An owned upload receipt; no path or media bytes are read here."""

    source_sha256: str
    source_state_sha256: str
    byte_length: int
    width: int
    height: int
    upload_name: str
    transform_sha256: str = ""

    def __post_init__(self) -> None:
        for label, value in (("source", self.source_sha256),
                             ("state", self.source_state_sha256)):
            _require(isinstance(value, str) and bool(_SHA.fullmatch(value)),
                     f"Uploaded image {label} needs a full SHA-256 hash.")
        _require(type(self.byte_length) is int and self.byte_length > 0,
                 "Uploaded image length must be positive.")
        _require(type(self.width) is int and type(self.height) is int
                 and 1 <= self.width <= 8192 and 1 <= self.height <= 8192,
                 "Uploaded image dimensions are invalid.")
        _require(isinstance(self.upload_name, str) and bool(_UPLOAD.fullmatch(self.upload_name)),
                 "Upload needs a bounded owned PNG name, not a path.")
        _require(not self.transform_sha256 or bool(_SHA.fullmatch(self.transform_sha256)),
                 "Image transform needs a full SHA-256 hash.")

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


@dataclass(frozen=True)
class GraphPlan:
    graph: Mapping[str, Any]
    manifest: Mapping[str, Any]
    fingerprint: str
    translation_report: tuple[Mapping[str, str], ...]
    requires_review: bool

    def to_dict(self) -> dict[str, Any]:
        return {"graph": self.graph, "manifest": self.manifest,
                "fingerprint": self.fingerprint,
                "translation_report": list(self.translation_report),
                "requires_review": self.requires_review}


def _input_schema(info: Mapping[str, Any], node: str, name: str) -> Any:
    declaration = info.get(node)
    _require(isinstance(declaration, Mapping), f"Runtime lacks {node}.")
    _require(declaration.get("name", node) == node, f"Runtime class mismatch for {node}.")
    inputs = declaration.get("input")
    _require(isinstance(inputs, Mapping), f"Runtime lacks {node} input schema.")
    for group in ("required", "optional"):
        values = inputs.get(group, {})
        if isinstance(values, Mapping) and name in values:
            return values[name]
    raise GraphContractError(f"Runtime {node} lacks input {name}.")


def _validate_input(info: Mapping[str, Any], node: str, name: str, value: Any) -> None:
    schema = _input_schema(info, node, name)
    _require(isinstance(schema, (tuple, list)) and bool(schema),
             f"Runtime {node}.{name} schema is malformed.")
    kind = schema[0]
    options = schema[1] if len(schema) > 1 and isinstance(schema[1], Mapping) else {}
    if isinstance(value, list) and len(value) == 2:
        _require(type(value[0]) is str and type(value[1]) is int,
                 f"Runtime {node}.{name} has an invalid link.")
        return
    if isinstance(kind, list):
        _require(value in kind, f"Runtime {node}.{name} does not admit {value!r}.")
    elif kind == "INT":
        _require(type(value) is int, f"Runtime {node}.{name} needs an integer.")
    elif kind == "FLOAT":
        _require(type(value) in (int, float) and math.isfinite(value),
                 f"Runtime {node}.{name} needs a finite number.")
    elif kind == "STRING":
        _require(isinstance(value, str), f"Runtime {node}.{name} needs text.")
    for boundary, check in (("min", lambda b: value >= b), ("max", lambda b: value <= b)):
        if boundary in options and type(value) in (int, float):
            _require(check(options[boundary]), f"Runtime {node}.{name} violates {boundary}.")
    if kind == "INT" and "step" in options:
        base = options.get("min", 0)
        _require((value - base) % options["step"] == 0,
                 f"Runtime {node}.{name} violates step.")


def build_wan22_ti2v_graph(
    request: VideoGenerationRequest, *, transformer: ResourceBinding,
    text_encoder: ResourceBinding, vae: ResourceBinding,
    object_info: Mapping[str, Any], template_sha256: str,
    runtime_version: str, adapter_version: str, output_token: str,
    uploaded_image: UploadedImageReference | None = None,
    planned_owned_upload: bool = False,
) -> GraphPlan:
    """Build an offline API-format graph after exact binding/schema validation.

    Nonempty goal/motion/camera and I2V resize are reported as review-required
    semantic losses; a caller must prevent submission until explicitly reviewed.
    """
    _require(isinstance(request, VideoGenerationRequest) and request.schema_supported,
             "Only the supported v1 silent T2V/I2V request can be graphed.")
    from .video_output_goal import validate_video_output_goal
    try:
        validate_video_output_goal(request)
    except ValueError as exc:
        raise GraphContractError("Unknown or unmapped output request: " + str(exc)) from exc
    _require(not request.fps.extra_fields
             and not request.overrides and not request.stages,
             "Unknown or unmapped request fields block graph execution.")
    _require(all(isinstance(x, ResourceBinding) for x in (transformer, text_encoder, vae)),
             "Exact resource bindings are required.")
    _require((transformer.role, text_encoder.role, vae.role)
             == ("transformer", "text_encoder", "vae"), "Resource roles are mismatched.")
    _require(len({x.resource_id for x in (transformer, text_encoder, vae)}) == 3,
             "Three distinct exact resource identities are required.")
    _require(request.model_resource_id == transformer.resource_id,
             "Selected model differs from transformer binding.")
    if request.resources:
        _require(set(request.resources) == {text_encoder.resource_id, vae.resource_id},
                 "Request companion identities differ from bindings.")
    _require(isinstance(template_sha256, str) and bool(_SHA.fullmatch(template_sha256)),
             "An exact template hash is required.")
    _require(request.template_version == f"sha256:{template_sha256}",
             "Request template differs from graph template.")
    _require(bool(runtime_version) and request.runtime_version == runtime_version,
             "Request runtime differs from graph runtime.")
    _require(bool(adapter_version) and request.adapter_version == adapter_version,
             "Request adapter differs from graph adapter.")
    _require(isinstance(output_token, str) and bool(_TOKEN.fullmatch(output_token)),
             "Output needs a unique caller-owned token.")
    _require(isinstance(object_info, Mapping), "A bounded runtime object_info snapshot is required.")
    encoded_info = _canonical(object_info)
    _require(len(encoded_info) <= 2_000_000, "Runtime object_info snapshot is too large.")
    _require(request.width % 32 == 0 and request.height % 32 == 0,
             "Wan2.2 geometry must use multiples of 32.")
    _require(request.width <= 1280 and request.height <= 1280 and request.width * request.height <= 1280 * 704,
             "The qualified native video canvas is at most 1280×704 pixels; use final resizing for larger output.")
    _require(1 <= request.frame_count <= 49 and (request.frame_count - 1) % 4 == 0,
             "Wan2.2 initial recipe needs 4n+1 frames, at most 49; longer clips need separate memory qualification.")
    _require(request.seed <= 2**63 - 1, "Seed exceeds bounded runtime adapter range.")
    fps = request.fps.fraction
    _require(Fraction(1) <= fps <= Fraction(120), "Comfy CreateVideo supports 1–120 fps.")
    fps_float = float(fps)
    _require(Fraction.from_float(fps_float) == fps,
             "Rational FPS is not exactly representable by Comfy's float input.")
    _require(request.audio_policy == "silent", "Only silent output is admitted.")
    if request.task == "i2v":
        _require(isinstance(uploaded_image, UploadedImageReference),
                 "I2V needs a typed owned upload receipt.")
        source = request.source_image
        _require(source is not None and not source.extra_fields,
                 "I2V source has unsupported fields.")
        _require((uploaded_image.source_sha256, uploaded_image.source_state_sha256,
                  uploaded_image.byte_length, uploaded_image.transform_sha256)
                 == (source.sha256, source.source_state_sha256,
                     source.byte_length, source.transform_sha256),
                 "I2V uploaded image does not match exact request source.")
        _require(source.schema_version == 1 and source.schema_id == "collectigen.video-image-source.v1",
                 "Future I2V source schema cannot be graphed.")
        _require(not uploaded_image.transform_sha256 or
                 uploaded_image.transform_sha256 == source.transform_sha256,
                 "I2V source transform differs from request.")
    else:
        _require(uploaded_image is None, "T2V cannot silently carry an uploaded image.")
    _require(type(planned_owned_upload) is bool and (not planned_owned_upload or uploaded_image is not None),
             "A planned owned upload requires one exact I2V source.")
    losses: list[dict[str, str]] = []
    for field in ("goal", "motion", "camera"):
        if getattr(request, field).strip():
            losses.append({"field": field, "status": "untranslated",
                           "reason": "No distinct native 5B node input; positive prompt is unchanged."})
    if uploaded_image and (uploaded_image.width, uploaded_image.height) != (request.width, request.height):
        _require(bool(uploaded_image.transform_sha256),
                 "I2V resize/crop needs an exact reviewed transform identity.")
        losses.append({"field": "source_image.geometry", "status": "translated",
                       "reason": "Wan22ImageToVideoLatent center-crops and bilinear-resizes the uploaded image."})

    def node(class_type: str, **inputs: Any) -> dict[str, Any]:
        for name, value in inputs.items():
            if class_type == "LoadImage" and name == "image" and planned_owned_upload:
                schema = _input_schema(object_info, class_type, name)
                _require(isinstance(schema, (tuple, list)) and bool(schema)
                         and isinstance(schema[0], list)
                         and all(isinstance(choice, str) for choice in schema[0])
                         and value == uploaded_image.upload_name,
                         "Planned upload needs a valid LoadImage image choice schema and exact owned name.")
            else:
                _validate_input(object_info, class_type, name, value)
        return {"class_type": class_type, "inputs": inputs}

    graph: dict[str, Any] = {
        "37": node("UNETLoader", unet_name=transformer.file_name, weight_dtype="default"),
        "38": node("CLIPLoader", clip_name=text_encoder.file_name, type="wan", device="default"),
        "39": node("VAELoader", vae_name=vae.file_name),
        "6": node("CLIPTextEncode", text=request.positive_prompt, clip=["38", 0]),
        "7": node("CLIPTextEncode", text=request.negative_prompt, clip=["38", 0]),
        "48": node("ModelSamplingSD3", model=["37", 0], shift=8.0),
    }
    latent_inputs: dict[str, Any] = {"vae": ["39", 0], "width": request.width,
                                     "height": request.height, "length": request.frame_count,
                                     "batch_size": 1}
    if uploaded_image:
        graph["56"] = node("LoadImage", image=uploaded_image.upload_name)
        latent_inputs["start_image"] = ["56", 0]
    graph["55"] = node("Wan22ImageToVideoLatent", **latent_inputs)
    graph["3"] = node("KSampler", model=["48", 0], positive=["6", 0],
                      negative=["7", 0], latent_image=["55", 0], seed=request.seed,
                      steps=20, cfg=5.0, sampler_name="uni_pc", scheduler="simple", denoise=1.0)
    graph["8"] = node("VAEDecode", samples=["3", 0], vae=["39", 0])
    graph["57"] = node("CreateVideo", images=["8", 0], fps=fps_float)
    graph["58"] = node("SaveVideo", video=["57", 0],
                       filename_prefix=f"video/collectigen_{output_token}", format="mp4", codec="h264")
    _require(object_info["SaveVideo"].get("output_node") is True,
             "SaveVideo must be a runtime output node.")
    manifest = {"schema_id": GRAPH_SCHEMA, "request": request.to_dict(),
                "resources": [x.to_dict() for x in (transformer, text_encoder, vae)],
                "template_sha256": template_sha256, "runtime_version": runtime_version,
                "adapter_version": adapter_version, "object_info_sha256": sha256(encoded_info).hexdigest(),
                "uploaded_image": uploaded_image.to_dict() if uploaded_image else None,
                "planned_owned_upload": planned_owned_upload,
                "output_token": output_token, "container": "mp4", "codec": "h264",
                "audio_policy": "silent"}
    fingerprint = sha256(_canonical({"graph": graph, "manifest": manifest,
                                     "translation_report": losses})).hexdigest()
    return GraphPlan(graph, manifest, fingerprint, tuple(losses), bool(losses))
