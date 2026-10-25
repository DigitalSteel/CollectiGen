"""Explicit local generation and scoped observations, separate from image truth."""
from __future__ import annotations

import base64
from copy import copy, deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from PIL import Image

from .annotation import GenerationRecord
from .external_write_transaction import atomic_verified_bytes, write_verified_json
from .generation_metadata import parse_generation_payload, translate_generation_record
from .generation_inputs import freeze_init_image
from .generation_resource_guard import (assess_memory_evidence, memory_request_key,
    classify_memory_failure, normalize_swarm_memory, device_fingerprint,
    request_cost_key, summarize_job_costs)
from .generation_masks import MaskPayload, composite_masked_result, validate_mask_payload, prepare_masked_input
from .generation_transport import LocalTransport, strict_json
from .model_resource_registry import inspect_model_resource
from .training_source_io import _assert_unredirected, _identity

ADAPTER_ID = "collectigen.swarmui.t2i.v1"
_LOCK = threading.RLock()
_REQUIRED_SWARM_PARAMETERS = frozenset({
    "prompt", "negativeprompt", "model", "width", "height", "seed", "steps",
    "cfgscale", "sampler", "scheduler", "donotsave", "exactbackendid", "automaticvae",
})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tensor_hash(path):
    _assert_unredirected(path)
    before = path.stat()
    result = hashlib.sha256()
    with path.open("rb") as handle:
        offset = int.from_bytes(handle.read(8), "little") + 8
        if not 8 < offset < before.st_size:
            raise ValueError("Invalid safe tensor header")
        handle.seek(offset)
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    if _identity(path.stat()) != _identity(before):
        raise ValueError("Model changed while its tensor identity was checked.")
    return result.hexdigest()


def _hash(value):
    return str(value or "").lower().removeprefix("0x")


class SwarmCompatibilityError(ValueError):
    """A bounded incompatibility in an otherwise identified SwarmUI response."""

    _REASONS = frozenset({"api_malformed", "parameter_missing"})

    def __init__(self, reason_code: str, *, missing_parameters=()):
        if reason_code not in self._REASONS:
            raise ValueError("Unsupported SwarmUI compatibility reason.")
        missing = tuple(sorted(
            {str(value) for value in missing_parameters if str(value) in _REQUIRED_SWARM_PARAMETERS}
        ))
        if reason_code == "parameter_missing":
            message = "SwarmUI lacks required generation parameters: " + ", ".join(missing or ("unavailable",))
        else:
            message = "SwarmUI returned an unsupported compatibility response."
        super().__init__(message)
        self.reason_code = reason_code
        self.missing_parameters = missing


class GenerationMemoryLimitedError(ValueError):
    """A fresh, measured resource observation blocked submission before send."""


class SwarmLocalAdapter:
    """Bounded image mapping with exact local main and companion bindings.

    Raw graphs, prompt directives, external resources and unknown settings remain
    export-only. Swarm tensor identity is verified separately from full-file hash.
    """
    def __init__(self, endpoint, *, transport=None, memory_evidence_provider=None):
        self.transport = transport or LocalTransport(endpoint)
        self.endpoint = self.transport.endpoint
        self.capabilities = None
        self.resource_observation = None
        # Trusted integration hook, never loaded from plans, recipes or receipts.
        # No native runtime/hardware qualification provider ships by default.
        self.memory_evidence_provider = memory_evidence_provider
        self._registry = None
        self.source_current_provider = None
        self.source_image_provider = None
        # Optional hash-addressed provider for additional reviewed Reference Only
        # images. The ordinary single-source provider remains unchanged.
        self.reference_image_provider = None

    def _session(self):
        session = self.transport.post("GetNewSession", {})
        if not isinstance(session, dict) or any(
            not isinstance(session.get(key), str) or not session[key].strip() or len(session[key]) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in session[key])
            for key in ("session_id", "version", "server_id")
        ):
            raise SwarmCompatibilityError("api_malformed")
        return session

    def probe(self):
        # A later failed probe must never leave a previous connection authoritative.
        self.capabilities = None
        self.resource_observation = None
        session = self._session()
        sid = {"session_id": session["session_id"]}
        status = self.transport.post("GetCurrentStatus", sid)
        params = self.transport.post("ListT2IParams", sid)
        backends = self.transport.post("ListBackends", {**sid, "full_data": True})
        if (
            not isinstance(status, dict)
            or not isinstance(status.get("backend_status"), dict)
            or not isinstance(status["backend_status"].get("status"), str)
            or not isinstance(params, dict)
            or not isinstance(params.get("list"), list)
            or not isinstance(backends, dict)
        ):
            raise SwarmCompatibilityError("api_malformed")
        ids = {}
        for row in params["list"]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                raise SwarmCompatibilityError("api_malformed")
            if row["id"] in ids:
                raise SwarmCompatibilityError("api_malformed")
            ids[row["id"]] = row
        # Only a managed local Comfy process is qualified, never a Swarm remote relay.
        local = []
        local_features = {}
        for key, backend in backends.items():
            if (
                not isinstance(backend, dict)
                or not isinstance(backend.get("type"), str)
                or type(backend.get("enabled")) is not bool
                or not isinstance(backend.get("status"), str)
                or type(backend.get("id", key)) not in {str, int}
            ):
                raise SwarmCompatibilityError("api_malformed")
            if (backend["type"] == "comfyui_selfstart"
                    and backend["enabled"] is True and backend["status"] in {"running", "idle"}):
                features = backend.get("features", [])
                if (not isinstance(features, list) or len(features) > 256
                        or any(not isinstance(item, str) or not item or len(item) > 128 for item in features)):
                    raise SwarmCompatibilityError("api_malformed")
                backend_id = str(backend.get("id", key))
                local.append(backend_id)
                local_features[backend_id] = sorted(set(features))
        if not local:
            raise ValueError("No running local managed Comfy backend is available in SwarmUI.")
        missing = _REQUIRED_SWARM_PARAMETERS - set(ids)
        if missing:
            raise SwarmCompatibilityError("parameter_missing", missing_parameters=missing)
        result = {"healthy": True, "adapter_id": ADAPTER_ID, "target_format": "swarmui",
                  "version": session["version"], "server_id": session["server_id"],
                  "endpoint": self.endpoint, "backend_ids": sorted(local),
                  "resource_roles": ["checkpoint"], "stage_kinds": ["first_pass"],
                  "supported_families": ["stable-diffusion-v1", "stable-diffusion-xl-v1", "sdxl", "sd1"],
                  "input_kinds": (["init_image", "mask"] if {"initimage", "initimagecreativity", "maskimage"}.issubset(ids)
                                  else ["init_image"] if {"initimage", "initimagecreativity"}.issubset(ids) else []),
                  "features": local_features[sorted(local)[0]],
                  "parameters": ids,
                  "status": status.get("backend_status", {}).get("status", "unknown")}
        if "controlnet" in result["features"] and {"controlnetmodel", "controlnetimageinput",
                "controlnetstrength", "controlnetstart", "controlnetend", "controlnetpreprocessor"}.issubset(ids):
            result["input_kinds"].append("control_image")
            result["resource_roles"].append("controlnet")
        # Swarm's managed Comfy workflow implements Reference Only with its
        # bundled SwarmReferenceOnly node. T2IEngine intentionally disregards
        # supports_reference_only as a backend flag, so it is not advertised
        # by ListBackends. Keep this source-derived contract separate from
        # reported backend features (WorkflowGeneratorSteps.cs, SwarmReference.py).
        if "comfyui" in result["features"] and {"promptimages", "usereferenceonly",
                "revisionstrength", "smartimagepromptresizing"}.issubset(ids):
            result["input_kinds"].append("reference_image")
            result["derived_features"] = {"supports_reference_only":
                "managed_swarm_comfy_reference_only_workflow_v1"}
        if "comfyui" in result["features"] and "globalregionfactor" in ids:
            # Native PromptRegion + CreateConditioning rectangle path. This
            # admits only typed rectangles below, never arbitrary markup.
            result.setdefault("derived_features", {})["supports_rectangular_regions"] = (
                "managed_swarm_comfy_rectangular_regions_v1")
        if {"loras", "loraweights", "loratencweights"}.issubset(ids):
            result["resource_roles"].append("lora")
        if "vae" in ids:
            result["resource_roles"].append("vae")
        if any(slot in ids for slot in ("cliplmodel", "t5xxlmodel", "qwenmodel", "mistralmodel")):
            result["resource_roles"].append("text_encoder")
        for family, required in (("flux-1", {"vae", "cliplmodel", "t5xxlmodel"}),
                                 ("anima", {"vae", "qwenmodel"}), ("flux-2", {"vae", "mistralmodel"})):
            if required.issubset(ids):
                result["supported_families"].append(family)
        stable = {k: v for k, v in result.items() if k != "status"}
        result["fingerprint"] = digest(stable)
        self.capabilities = result
        return deepcopy(result)

    def observe_resources(self, session_id=None, *, quick=False):
        """Read fresh Swarm memory numbers; never retain its raw device payload."""
        if not self.capabilities:
            raise ValueError("Check the local connection before observing generator resources.")
        try:
            sid = session_id or self._session()["session_id"]
            options = {"timeout": .25, "max_bytes": 64 * 1024} if quick and isinstance(self.transport, LocalTransport) else {}
            raw = self.transport.post("GetServerResourceInfo", {"session_id": sid}, **options)
            observation = normalize_swarm_memory(raw)
            fingerprint = device_fingerprint(raw)
            if fingerprint:
                observation["gpu_fingerprint"] = fingerprint
        except Exception:
            observation = normalize_swarm_memory(None)
        observation["observed_at"] = _now()
        self.resource_observation = observation
        return deepcopy(observation)

    def assess_resources(self, prepared, observation):
        """Recheck trusted evidence for the exact frozen request, without writes."""
        evidence = {}
        if callable(self.memory_evidence_provider):
            try:
                evidence = self.memory_evidence_provider(deepcopy(prepared), deepcopy(observation))
            except Exception:
                pass  # Unavailable qualification cannot manufacture a peak.
        evidence = evidence if isinstance(evidence, dict) else {}
        context = evidence.get("context")
        context = dict(context) if isinstance(context, dict) else {}
        context["request_fingerprint"] = memory_request_key(prepared)
        stages = prepared.get("plan", {}).get("request", {}).get("stages", [])
        return assess_memory_evidence(stages, context, observation, evidence.get("records", ()))

    def discover_registered_model(self, resource_id, registry, *, role="checkpoint"):
        if not self.capabilities:
            raise ValueError("Check the local connection first.")
        resolution = registry.resolve(resource_id, role="control_model" if role == "controlnet" else role)
        if resolution.status != "identity_verified":
            raise ValueError("The selected main model is unavailable or changed. Recheck it in Models.")
        from .generation_resources import discover_swarm_models
        path = resolution.inspection.path
        session = self._session()
        candidates = discover_swarm_models(self.transport, session["session_id"], _tensor_hash(Path(path)), role=role)
        if inspect_model_resource(path).resource_id != resource_id:
            raise ValueError("The selected model changed during discovery. Recheck it in Models.")
        return {"path": str(path), "resource_id": resource_id, "candidates": candidates}

    def bind_model(self, name, path, registry, *, expected_resource_id="", role="checkpoint", slot=""):
        if not self.capabilities:
            raise ValueError("Check the local connection first.")
        if not name or ".." in str(name).replace("\\", "/").split("/"):
            raise ValueError("Choose the exact generator model name.")
        inspection = inspect_model_resource(path)
        if expected_resource_id and inspection.resource_id != expected_resource_id:
            raise ValueError("The local file no longer matches the selected model.")
        session = self._session()
        from .generation_resources import SWARM_SUBTYPES
        if role not in SWARM_SUBTYPES:
            raise ValueError("This resource role has no exact Swarm binding.")
        model = self.transport.post("DescribeModel", {"session_id": session["session_id"],
            "modelName": name, "subtype": SWARM_SUBTYPES[role]}).get("model", {})
        family = str(model.get("compat_class") or "").lower()
        if (model.get("local") is not True or model.get("is_supported_model_format") is not True
                or role == "checkpoint" and family not in self.capabilities["supported_families"]):
            raise ValueError("This is not an identified local resource supported by the current adapter.")
        tensor_hash = _tensor_hash(Path(path))
        if _hash(model.get("hash_sha256") or model.get("hash")) != tensor_hash:
            raise ValueError("The generator's cached tensor hash is missing or differs; resolve the model identity in Setup.")
        _, expected = registry.snapshot()
        registry.register(inspection, role="control_model" if role == "controlnet" else role, family=family or "unknown", expected_registry=expected,
                          source="Explicit local generator binding")
        self._registry = registry
        return {"name": str(model.get("name") or name), "path": str(inspection.path),
                "resource_id": inspection.resource_id, "tensor_sha256": tensor_hash,
                "family": family, "model_class": str(model.get("architecture") or "").lower(),
                "role": role, "slot": slot, "verified": True,
                "capability_fingerprint": self.capabilities["fingerprint"]}

    def prepare(self, plan, binding, registry, *, source_current=None):
        caps = self.capabilities
        if not caps or binding.get("capability_fingerprint") != caps["fingerprint"]:
            raise ValueError("Connection changed; check and bind the exact model again.")
        request = plan.request
        resolution = registry.resolve(binding["resource_id"], role="checkpoint")
        if resolution.status != "identity_verified":
            raise ValueError("The registered model is unavailable or stale.")
        if str(resolution.inspection.path) != binding["path"]:
            # Duplicate locations are valid only after checking the chosen server binding too.
            if inspect_model_resource(binding["path"]).resource_id != binding["resource_id"]:
                raise ValueError("Bound generator model changed.")
        if request.model_hash.removeprefix("sha256:") != binding["resource_id"].removeprefix("sha256:"):
            raise ValueError("The request must explicitly select the bound full SHA-256 model identity.")
        if request.model and request.model != binding["name"]:
            raise ValueError("Request model name differs from the bound generator name.")
        if (request.loras or request.extras or request.extra_fields
                or len(request.stages) > 1):
            raise ValueError("This request needs a broader adapter; preserve it with neutral export.")
        inputs = [item for stage in request.stages for item in stage.inputs]
        guided = (len(inputs) == 1 and inputs[0].kind in {"control_image", "reference_image"}
                  or 2 <= len(inputs) <= 4 and all(row.kind == "reference_image" for row in inputs))
        remix = bool(inputs) and not guided
        source_hash = ""
        frozen_source = None
        frozen_references = []
        conditioning = None
        mask_payload = None
        masked_input = None
        outpaint_input = None
        expansion = None
        task = str(request.stages[0].extras.get("image_task") or "") if inputs and request.stages else "new"
        if guided:
            item = inputs[0]
            collection = plan.source_identity.get("collection", [])
            if (task != "guided" or plan.intent != "variation" or len(request.stages) != 1
                    or not isinstance(collection, list) or len(collection) != len(inputs)
                    or any(not isinstance(row, dict) for row in collection)
                    or any(row.source_node or row.extra_fields or row.strength is not None for row in inputs)
                    or request.denoising_strength not in (None, 1, 1.0)):
                raise ValueError("Image guidance needs one reviewed standalone source stage.")
            source_hash = str(collection[0].get("image_sha256") or "")
            if (any(row.reference != "sha256:" + str(identity.get("image_sha256") or "")
                    or row.extras.get("sha256") != identity.get("image_sha256")
                    or row.kind not in caps["input_kinds"]
                    for row, identity in zip(inputs, collection))):
                raise ValueError("Image guidance source or live adapter capability changed.")
            frozen_source = freeze_init_image(self.source_image_provider, source_hash)
            frozen_references = [frozen_source]
            if len(inputs) > 1:
                if not callable(self.reference_image_provider):
                    raise ValueError("Additional references need an exact hash-addressed image provider.")
                for identity in collection[1:]:
                    reference_hash = str(identity.get("image_sha256") or "")
                    frozen_references.append(freeze_init_image(
                        lambda sha=reference_hash: self.reference_image_provider(sha), reference_hash))
            selected = binding.get("conditioning") if item.kind == "control_image" else None
            if item.kind == "control_image":
                if (not isinstance(selected, dict) or selected.get("role") not in {"control_model", "controlnet"}
                        or selected.get("resource_id") != item.extras.get("resource_id")
                        or selected.get("capability_fingerprint") != caps["fingerprint"]):
                    raise ValueError("Bind the selected exact ControlNet model before guidance.")
                resolution = registry.resolve(selected["resource_id"], role="control_model")
                if resolution.status != "identity_verified" or inspect_model_resource(selected["path"]).resource_id != selected["resource_id"]:
                    raise ValueError("The selected ControlNet model is unavailable or changed.")
            from .generation_conditioning import prepare_conditioning, prepare_reference_batch
            conditioning_caps = deepcopy(caps)
            if item.kind == "reference_image" and caps.get("derived_features", {}).get("supports_reference_only"):
                conditioning_caps["features"].append("supports_reference_only")
            conditioning_inputs = []
            for row, frozen in zip(inputs, frozen_references):
                selected_input = {"kind": row.kind, "source_sha256": frozen["sha256"], **row.extras}
                selected_input.pop("sha256", None)
                if (row.extras.get("source_width"), row.extras.get("source_height")) != (
                        frozen["width"], frozen["height"]):
                    raise ValueError("The reviewed guide dimensions changed.")
                conditioning_inputs.append(selected_input)
            frozen_bytes = [base64.b64decode(row["data_url"].split(",", 1)[1], validate=True)
                            for row in frozen_references]
            conditioning = (prepare_conditioning(conditioning_inputs[0], frozen_bytes[0],
                            selected, conditioning_caps, binding["family"])
                            if len(inputs) == 1 else prepare_reference_batch(
                                conditioning_inputs, frozen_bytes, conditioning_caps, binding["family"]))
        if remix:
            if plan.intent != "variation" or len(request.stages) != 1 or len(inputs) not in (1, 2):
                raise ValueError("Local Remix requires one reviewed variation with one source image.")
            item = inputs[0]
            collection = plan.source_identity.get("collection", [])
            if (item.kind != "init_image" or item.source_node or item.extra_fields
                    or set(item.extras) != ({"sha256", "canvas_expansion"} if task == "outpaint" else {"sha256"}) or not isinstance(collection, list)
                    or len(collection) != 1 or not isinstance(collection[0], dict)):
                raise ValueError("Local Remix requires one exact source image without masks or extra inputs.")
            source_hash = str(collection[0].get("image_sha256") or "")
            if item.reference != "sha256:" + source_hash or item.extras.get("sha256") != source_hash:
                raise ValueError("Remix input differs from the reviewed source identity.")
            if task == "outpaint":
                from .generation_outpainting import CanvasExpansion
                expansion = CanvasExpansion.from_dict(item.extras["canvas_expansion"])
                if expansion.source_sha256 != source_hash:
                    raise ValueError("Canvas expansion differs from the reviewed source.")
            strength = request.denoising_strength
            if (type(strength) not in {int, float} or not math.isfinite(strength)
                    or not 0 < strength <= 1 or item.strength not in (None, strength)):
                raise ValueError("Choose an explicit Remix change strength greater than zero and at most one.")
            if "init_image" not in caps["input_kinds"]:
                raise ValueError("This local generator does not offer image-guided Remix.")
            if len(inputs) == 2:
                masked = inputs[1]
                if (masked.kind != "mask" or masked.source_node or masked.extra_fields
                        or masked.strength is not None or set(masked.extras) != {"mask_payload"}
                        or not isinstance(masked.extras.get("mask_payload"), dict)):
                    raise ValueError("Paint Area requires one exact saved mask input.")
                if "mask" not in caps["input_kinds"]:
                    raise ValueError("This local generator does not offer masked image editing.")
                mask_payload = MaskPayload.from_dict(masked.extras["mask_payload"])
                if masked.reference != "sha256:" + mask_payload.mask_sha256:
                    raise ValueError("Paint Area mask reference differs from the reviewed mask bytes.")
                if task == "upscale":
                    raise ValueError("Masked generative upscale needs a separate reviewed method.")
            if task not in {"remix", "upscale", "masked", "outpaint"}:
                raise ValueError("Choose a supported image task for this source request.")
            if task in {"masked", "outpaint"} and mask_payload is None:
                raise ValueError("Paint Area requires an exact saved mask.")
            if task == "remix" and mask_payload is not None:
                raise ValueError("Choose Paint Area for an exact saved mask.")
        elif not guided and request.denoising_strength not in (None, 1, 1.0):
            raise ValueError("A change strength requires one reviewed source image.")
        components = None
        if binding.get("components"):
            from .generation_swarm_components import prepare_swarm_components
            components = prepare_swarm_components(request, binding["components"], caps)
            main = [row for row in components["resources"] if row["role"] == "checkpoint"]
            if len(main) != 1 or main[0]["swarm_name"] != binding["name"] or main[0]["full_file_sha256"] != binding["resource_id"].removeprefix("sha256:"):
                raise ValueError("Component bindings disagree with the selected main model.")
            controls = [row for row in components["resources"] if row["role"] == "controlnet"]
            if controls and not (guided and item.kind == "control_image"):
                raise ValueError("A ControlNet model requires one reviewed control image.")
            if conditioning is not None and item.kind == "control_image":
                if (len(controls) != 1 or controls[0]["full_file_sha256"] != binding["conditioning"]["resource_id"].removeprefix("sha256:")
                        or controls[0]["tensor_sha256"] != binding["conditioning"]["tensor_sha256"]):
                    raise ValueError("ControlNet component differs from the selected exact guidance binding.")
        else:
            if request.vae or binding["family"] not in {"stable-diffusion-v1", "stable-diffusion-xl-v1", "sdxl", "sd1"}:
                raise ValueError("Bind all selected companions before preparing this model.")
            for resource in request.resources:
                expected_hash = (binding["conditioning"]["resource_id"].removeprefix("sha256:")
                    if guided and item.kind == "control_image" and resource.role == "controlnet" else request.model_hash)
                if (resource.role not in ({"checkpoint", "controlnet"} if guided and item.kind == "control_image" else {"checkpoint"})
                        or resource.hashes.get("sha256") != expected_hash
                        or resource.extras or resource.extra_fields or resource.weights):
                    raise ValueError("Bind every selected model addition before preparing.")
            if guided and item.kind == "control_image" and sum(r.role == "controlnet" for r in request.resources) != 1:
                raise ValueError("Choose one exact ControlNet resource in the neutral plan.")
        for stage in request.stages:
            if (stage.kind != "first_pass" or stage.extra_fields
                    or (stage.extras and (set(stage.extras) != {"image_task"}
                        or stage.extras["image_task"] != task))):
                raise ValueError("Stage extensions require a qualified graph adapter.")
            if stage.inputs and (not (remix or guided) or stage.inputs != inputs or stage.denoising_strength not in (None, request.denoising_strength)):
                raise ValueError("Only one matching first-pass Remix source is supported.")
            for key in ("positive_prompt", "negative_prompt", "width", "height", "steps", "sampler", "scheduler", "cfg_scale"):
                value = getattr(stage, key, None)
                if value not in (None, "") and value != getattr(request, key):
                    raise ValueError("Stage and request settings disagree; review both before execution.")
            if stage.seeds and stage.seeds != [request.seed]:
                raise ValueError("Stage seeds differ from the reviewed request.")
            if stage.batch_size not in (None, 1):
                raise ValueError("Only a single-image request is qualified.")
        loss = translate_generation_record(request, "swarmui").to_dict()
        if loss["lossy"] or any(row.status in {"approximated", "untranslated", "unsupported"} for row in request.adapter_report):
            raise ValueError("The request still contains translation loss; use neutral export.")
        # Swarm angle directives can load resources or execute dynamic prompt expansion.
        if any(token in request.positive_prompt + request.negative_prompt for token in ("<", ">", "embedding:")):
            raise ValueError("Prompt directives/embeddings need explicit resource mapping; this adapter accepts literal prompts.")
        if not request.positive_prompt or len(request.positive_prompt) + len(request.negative_prompt) > 100_000:
            raise ValueError("Enter a bounded literal prompt (up to 100,000 characters).")
        for value, lower, upper, label in ((request.width, 64, 2048, "width"), (request.height, 64, 2048, "height"),
                                         (request.steps, 1, 150, "steps"), (request.seed, 0, 2**63-1, "seed")):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"Choose an explicit supported {label}.")
        if request.width % 8 or request.height % 8:
            raise ValueError("Image dimensions must be multiples of eight.")
        regional = plan.target_context.get("regional_prompt")
        rendered_prompt = request.positive_prompt
        if regional is not None:
            from .generation_regions import validate_region_spec, render_region_prompt
            regional = validate_region_spec(regional, width=request.width, height=request.height)
            if regional["regions"]:
                if (binding["family"] not in {"sdxl", "stable-diffusion-xl-v1"}
                        or caps.get("derived_features", {}).get("supports_rectangular_regions") !=
                        "managed_swarm_comfy_rectangular_regions_v1"):
                    raise ValueError("Regional prompts need the supported local Swarm Comfy path and an exact SDXL model.")
                if task != "new" or remix or guided or plan.target_context.get("output_plan", {}).get("kind") == "generative_refine":
                    raise ValueError("Regional prompts currently support new images with optional faithful resizing; remove regions for Remix, guidance or refinement.")
                rendered_prompt = render_region_prompt(request.positive_prompt, regional,
                    width=request.width, height=request.height)
            else:
                regional = None
        output_context = plan.target_context
        output_goal = output_context.get("output_goal")
        proposed_output_plan = output_context.get("output_plan")
        goal_differs = isinstance(output_goal, dict) and (
            output_goal.get("final_width"), output_goal.get("final_height")) != (request.width, request.height)
        if goal_differs and proposed_output_plan is None:
            raise ValueError("The desired final size needs one reviewed executable follow-up stage.")
        output_plan = None
        refinement = None
        if proposed_output_plan is not None:
            if task not in {"new", "remix", "guided"} or mask_payload is not None:
                raise ValueError("Faithful final resize is limited to a plain new, Remix, or guided image.")
            if proposed_output_plan.get("kind") == "generative_refine":
                from .generation_refinement import prepare_refinement
                refinement = prepare_refinement(proposed_output_plan, width=request.width, height=request.height,
                    output_goal=output_goal, binding=binding, capabilities=caps)
                if request.seed == 2**63-1:
                    raise ValueError("Refinement uses the next seed; choose a seed below the maximum supported value.")
            else:
                from .generation_output_processing import validate_faithful_resize
                output_plan = validate_faithful_resize(proposed_output_plan,
                    base_width=request.width, base_height=request.height, output_goal=output_goal)
        if not isinstance(request.cfg_scale, (int, float)) or not math.isfinite(request.cfg_scale) or not 0 <= request.cfg_scale <= 30:
            raise ValueError("Choose a finite CFG scale between 0 and 30.")
        for key in ("sampler", "scheduler"):
            values = [str(v).split("///")[0] for v in caps["parameters"][key].get("values", [])]
            if getattr(request, key) not in values:
                raise ValueError(f"The selected {key} is not offered by this adapter.")
        source = plan.to_dict().get("source_identity")
        if source and (source_current is None or source_current != source):
            raise ValueError("Recheck the source image identity before generation.")
        if remix:
            frozen_source = freeze_init_image(self.source_image_provider, source_hash)
            if task == "upscale" and not (request.width > frozen_source["width"] and request.height > frozen_source["height"]):
                raise ValueError("Generative upscale needs explicit target width and height above the source dimensions.")
            if mask_payload is not None:
                source_bytes = base64.b64decode(frozen_source["data_url"].split(",", 1)[1], validate=True)
                if expansion is not None:
                    from .generation_outpainting import expanded_canvas
                    outpaint_input = expanded_canvas(source_bytes, expansion)
                    if mask_payload.to_dict() != outpaint_input["mask"].to_dict():
                        raise ValueError("The canvas mask must protect every original source pixel.")
                    source_bytes = outpaint_input["source_png"]
                validate_mask_payload(mask_payload, source_bytes=source_bytes)
                if (request.width, request.height) != (mask_payload.width, mask_payload.height):
                    raise ValueError("Paint Area output dimensions must match the masked source.")
                if mask_payload.crop_region is not None:
                    masked_input = prepare_masked_input(source_bytes, mask_payload)
        from .generation_planning import assess_readiness
        def resolver(reference):
            for frozen in frozen_references or ([frozen_source] if frozen_source is not None else []):
                if reference == "sha256:" + frozen["sha256"]:
                    return {"available": True, "verified": True, "sha256": frozen["sha256"]}
            if mask_payload is not None and reference == "sha256:" + mask_payload.mask_sha256:
                return {"available": True, "verified": True, "sha256": mask_payload.mask_sha256}
            return {"available": False}
        readiness = assess_readiness(plan, "swarmui", registry=registry, capabilities=caps,
            source_current=source_current, input_resolver=resolver if remix or guided else None)
        if not readiness.ready:
            raise ValueError("Generation readiness is blocked: " + "; ".join(issue.message for issue in readiness.issues))
        payload = {"images": 1, "batchsize": 1, "prompt": rendered_prompt,
                   "negativeprompt": request.negative_prompt, "model": binding["name"],
                   "width": request.width, "height": request.height, "seed": request.seed,
                   "steps": request.steps, "cfgscale": request.cfg_scale, "sampler": request.sampler,
                   "scheduler": request.scheduler, "donotsave": True, "automaticvae": False,
                   "nopreviews": True, "exactbackendid": caps["backend_ids"][0], "imageformat": "PNG"}
        if regional is not None:
            payload["globalregionfactor"] = .5
        if components is not None:
            payload.update(components["payload"])
        if refinement is not None:
            payload.update(refinement["payload"])
        if frozen_source is not None and remix:
            payload.update(initimage=frozen_source["data_url"], initimagecreativity=request.denoising_strength)
        if conditioning is not None:
            payload.update(conditioning["wireparams"])
        if outpaint_input is not None:
            payload["initimage"] = "data:image/png;base64," + base64.b64encode(outpaint_input["source_png"]).decode("ascii")
        if mask_payload is not None:
            payload["maskimage"] = "data:image/png;base64," + base64.b64encode(mask_payload.mask_png).decode("ascii")
        if masked_input is not None:
            # The adapter's canvas uses whole latent blocks; padding remains outside
            # the editable crop and is removed before protected-pixel compositing.
            width = max(64, (masked_input["width"] + 7) // 8 * 8)
            height = max(64, (masked_input["height"] + 7) // 8 * 8)
            for key, source_key, mode in (("initimage", "source_png", "RGBA"), ("maskimage", "mask_png", "L")):
                with Image.open(io.BytesIO(masked_input[source_key])) as original:
                    canvas = Image.new(mode, (width, height), 0)
                    canvas.paste(original.convert(mode), (0, 0))
                    encoded = io.BytesIO()
                    canvas.save(encoded, format="PNG")
                payload[key] = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")
            payload.update(width=width, height=height)
        from .generation_output_guidance import validate_base_canvas
        validate_base_canvas(payload["width"], payload["height"], family=binding["family"])
        result = {"plan": plan.to_dict(), "request": request.to_dict(), "payload": payload,
                  "binding": deepcopy(binding), "capabilities": deepcopy(caps), "loss": loss,
                  "preview": "One local image; checkpoint's embedded VAE; no generator history save. "
                             "Verified Metadata and lineage return for ordinary Ingest review."}
        if components is not None:
            result["components"] = components
            result["preview"] = "One local image using every explicitly bound model component. Verified output returns through Ingest."
        if frozen_source is not None:
            result["source_image"] = {"sha256": source_hash, "size": frozen_source["size"],
                "width": frozen_source["width"], "height": frozen_source["height"], "task": task}
        if conditioning is not None:
            result["conditioning"] = conditioning
            result["preview"] += " One exact locally bound guidance image; returned metadata and source lineage must match."
        if regional is not None:
            result["regional_prompt"] = regional
            result["preview"] += " Reviewed rectangular prompts use equal global/region influence on this SDXL base canvas. Regions guide composition; they do not guarantee object placement."
        if output_plan is not None:
            result["output_plan"] = output_plan
            result["preview"] += " The reviewed faithful resize follows verification of the base image; both exact raw and final bytes are retained."
        if refinement is not None:
            result["refinement"] = refinement
            result["preview"] += " The same exact model performs a second detail pass within its base canvas budget, followed by faithful final sizing if needed. This may change details; no extra model is loaded."
        if mask_payload is not None:
            result["mask"] = {"sha256": mask_payload.mask_sha256,
                "operation_version": mask_payload.operation_version, "feather_px": mask_payload.feather_px}
        if outpaint_input is not None:
            result["source_image"].update(composite_source_sha256=outpaint_input["source_sha256"],
                canvas_expansion=expansion.to_dict())
            result["preview"] += " Canvas expansion protects the original image at its reviewed offset and generates only the added border."
        if masked_input is not None:
            result["source_image"]["full_source_data_url"] = frozen_source["data_url"]
            result["mask"].update(crop_region=list(mask_payload.crop_region),
                crop_padding_px=mask_payload.crop_padding_px,
                generator_canvas=[payload["width"], payload["height"]],
                padding="right_bottom_zero_protected")
            result["preview"] += " Paint Area sends the reviewed crop with protected canvas padding; the result is restored to full source size."
        from .model_workflow_guidance import ModelWorkflowGuidanceService
        from .paths import USER_DATA_DIR
        try:
            catalog = ModelWorkflowGuidanceService(update_path=USER_DATA_DIR / "model_workflow_knowledge.json").catalog
            family = {"sdxl": "SDXL", "stable-diffusion-xl-v1": "SDXL",
                "sd1": "SD1", "stable-diffusion-v1": "SD1", "flux-1": "Flux.1",
                "flux-2": "Flux.2", "anima": "Anima"}.get(binding["family"], binding["family"])
            result["model_guidance"] = {"authority": "reviewed_local_guidance", "family": family,
                "model_sha256": binding["resource_id"], "as_of": catalog["as_of"],
                "catalog_version": catalog["catalog_version"], "goal": plan.goal,
                "records": [{k: record.get(k) for k in ("id", "family", "version", "summary", "sources", "catalog_status")}
                            for record in catalog["records"] if record["family"].casefold() == family.casefold()],
                "limitation": "Family context is not a measured result for this checkpoint. Training recipes do not authorize generation settings."}
        except (OSError, ValueError):
            result["model_guidance"] = {"authority": "unavailable", "limitation": "Local catalog unavailable; exact adapter readiness is independent."}
        result["fingerprint"] = digest(result)
        self._registry = registry
        return result

    def revalidate(self, prepared):
        if prepared["plan"].get("source_identity"):
            if self.source_current_provider is None or self.source_current_provider() != prepared["plan"]["source_identity"]:
                raise ValueError("Source image changed or could not be rechecked after review.")
        previous = prepared["capabilities"]
        if self.probe()["fingerprint"] != previous["fingerprint"]:
            raise ValueError("Generator capabilities changed after review.")
        binding = prepared["binding"]
        if inspect_model_resource(binding["path"]).resource_id != binding["resource_id"]:
            raise ValueError("The model changed after review.")
        session = self._session()
        model = self.transport.post("DescribeModel", {"session_id": session["session_id"],
            "modelName": binding["name"], "subtype": "Stable-Diffusion"}).get("model", {})
        if (model.get("local") is not True or model.get("is_supported_model_format") is not True
                or str(model.get("compat_class", "")).lower() != binding["family"]
                or _hash(model.get("hash_sha256") or model.get("hash")) != binding["tensor_sha256"]):
            raise ValueError("Generator resource identity changed after review.")
        if binding.get("components"):
            from .generation_resources import SWARM_SUBTYPES
            for component in binding["components"].values():
                if inspect_model_resource(component["path"]).resource_id != component["resource_id"]:
                    raise ValueError("A selected companion changed after review.")
                remote = self.transport.post("DescribeModel", {"session_id": session["session_id"],
                    "modelName": component["name"], "subtype": SWARM_SUBTYPES[component["role"]]}).get("model", {})
                if (remote.get("local") is not True or remote.get("is_supported_model_format") is not True
                        or _hash(remote.get("hash_sha256") or remote.get("hash")) != component["tensor_sha256"]
                        or str(remote.get("compat_class") or "").lower() != component["family"]
                        or str(remote.get("architecture") or "").lower() != component["model_class"]):
                    raise ValueError("A generator companion changed after review.")
        if prepared.get("conditioning") and binding.get("conditioning"):
            selected = binding["conditioning"]
            if inspect_model_resource(selected["path"]).resource_id != selected["resource_id"]:
                raise ValueError("The ControlNet resource changed after review.")
            remote = self.transport.post("DescribeModel", {"session_id": session["session_id"],
                "modelName": selected["name"], "subtype": "ControlNet"}).get("model", {})
            if (remote.get("local") is not True or remote.get("is_supported_model_format") is not True
                    or _hash(remote.get("hash_sha256") or remote.get("hash")) != selected["tensor_sha256"]):
                raise ValueError("The local ControlNet binding changed after review.")
        from .generation_planning import GenerationPlan
        plan = GenerationPlan.from_dict(prepared["plan"])
        rebuilt = self.prepare(plan, binding, self._registry,
            source_current=self.source_current_provider() if self.source_current_provider else None)
        if rebuilt["fingerprint"] != prepared["fingerprint"]:
            raise ValueError("Execution payload no longer matches the validated neutral plan.")
        observation = self.observe_resources(session["session_id"])
        assessment = self.assess_resources(prepared, observation)
        if assessment["assessment"] == "limited":
            raise GenerationMemoryLimitedError(assessment["message"])
        return session


class GenerationExecutionService:
    def __init__(self, storage_root, jobs):
        self.root = Path(storage_root).absolute()
        _assert_unredirected(self.root)
        self.jobs = jobs

    def _path(self, job_id):
        if not re.fullmatch(r"gen-[a-f0-9]{32}", job_id):
            raise ValueError("Invalid generation job identity")
        path = self.root / job_id
        _assert_unredirected(path)
        return path

    def _read(self, job_id):
        path = self._path(job_id) / "receipt.json"
        _assert_unredirected(path)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Generation receipt is too large.")
        result = strict_json(path.read_bytes())
        if (result.get("job_id") != job_id or result.get("schema_version") != 1
                or not re.fullmatch(r"[a-f0-9]{64}", str(result.get("fingerprint", "")))):
            raise ValueError("Unsupported generation receipt")
        prepared = deepcopy(result.get("prepared", {}))
        fingerprint = prepared.pop("fingerprint", None)
        if fingerprint != result["fingerprint"] or digest(prepared) != fingerprint:
            raise ValueError("Retained generation request is corrupt or changed.")
        for output in result.get("outputs", []):
            if output.get("path") != str(self._path(job_id) / "result.png"):
                raise ValueError("Retained output path is outside its exact job.")
        return result

    def _write(self, receipt):
        if len(json.dumps(receipt, ensure_ascii=False).encode()) > 8 * 1024 * 1024:
            raise ValueError("Generation receipt exceeds its bounded storage limit; use neutral export.")
        target = self._path(receipt["job_id"]) / "receipt.json"
        _assert_unredirected(target)
        if target.exists():
            atomic_verified_bytes(target.with_suffix(".previous.json"), target.read_bytes())
        write_verified_json(target, receipt, label="Generation job receipt")

    def history(self):
        _assert_unredirected(self.root)
        if not self.root.exists(): return []
        paths = sorted(self.root.glob("gen-*/receipt.json"), key=lambda p: p.parent.name)
        if len(paths) > 1000:
            raise ValueError("Generation history exceeds this bounded view; archive reviewed jobs first.")
        if sum(path.stat().st_size for path in paths) > 32 * 1024 * 1024:
            raise ValueError("Generation history exceeds its bounded memory view; review a specific saved job.")
        return [self._read(path.parent.name) for path in paths]

    def submit(self, adapter, prepared, *, confirmed=False, listener=None):
        if not confirmed:
            raise ValueError("Explicit reviewed generation submission is required.")
        frozen = deepcopy(prepared)
        fingerprint = frozen.pop("fingerprint", "")
        if digest(frozen) != fingerprint:
            raise ValueError("The reviewed request changed; prepare it again.")
        frozen["fingerprint"] = fingerprint
        if len(json.dumps(frozen, ensure_ascii=False).encode()) > 7 * 1024 * 1024:
            raise ValueError("Prepared generation exceeds receipt limits; use neutral export.")
        with _LOCK:
            for existing in self.history():
                if existing.get("state") in {"submitting", "running", "cancelling", "outcome_unknown"} and not existing.get("server_stopped"):
                    raise ValueError("A previous local generation may still be running; reconcile its retained session before submitting again.")
                if existing.get("fingerprint") == fingerprint and not existing.get("proven_unsubmitted"):
                    raise ValueError("This exact submission already exists; review its retained result before making a new plan.")
            job_id = "gen-" + uuid4().hex
            self.root.mkdir(parents=True, exist_ok=True)
            claim = self.root / (fingerprint + ".claim")
            _assert_unredirected(claim)
            try:
                with claim.open("x", encoding="ascii") as handle:
                    handle.write(job_id); handle.flush(); os.fsync(handle.fileno())
            except FileExistsError:
                raise ValueError("This reviewed submission is already claimed; inspect retained recovery.") from None
            receipt = {"schema_version": 1, "job_id": job_id, "fingerprint": fingerprint,
                       "state": "queued", "created_at": _now(), "prepared": frozen, "outputs": []}
            try:
                self._write(receipt)
            except Exception:
                self._release_claim(receipt)
                raise
        try:
            options = dict(resource="gpu", key="generation:" + fingerprint,
                           recover_on_shutdown=True, reclaim_ai_runtime=True)
            if listener is not None:
                def deliver_with_retained_receipt(progress):
                    notification = progress
                    if progress.state in {"failed", "cancelled"}:
                        try:
                            with _LOCK:
                                retained = self._read(job_id)
                                if retained["state"] == "queued":
                                    # The coordinator ended before _execute
                                    # could submit anything to Swarm.
                                    retained.update(state=progress.state,
                                                    proven_unsubmitted=True, finished_at=_now())
                                    self._write(retained)
                                    self._release_claim(retained)
                            notification = copy(progress)
                            notification.result = retained
                        except Exception:
                            # Preserve the manager's terminal notification even
                            # when its separate durable receipt needs recovery.
                            notification = progress
                    listener(notification)
                return self.jobs.submit_observed("Generate reviewed local image", lambda job: self._execute(adapter, receipt, job),
                                                deliver_with_retained_receipt, **options)[0]
            return self.jobs.submit("Generate reviewed local image", lambda job: self._execute(adapter, receipt, job), **options)
        except Exception:
            # A synchronous worker failure already classified its submission state.
            current = self._read(job_id)
            if current["state"] == "queued":
                current.update(state="failed", proven_unsubmitted=True)
                self._write(current); self._release_claim(current)
            raise

    def _release_claim(self, receipt):
        claim = self.root / (receipt["fingerprint"] + ".claim")
        _assert_unredirected(claim)
        if claim.is_file() and claim.read_text(encoding="ascii") == receipt["job_id"]:
            claim.unlink()

    def _execute(self, adapter, receipt, job):
        submitted = False
        started = None
        samples, attempts, peak, last_sample = 0, 0, None, 0.0
        initial_gpu = ""
        def sample_resources(*, force=False):
            nonlocal samples, attempts, peak, last_sample
            now = time.monotonic()
            if not isinstance(adapter, SwarmLocalAdapter) or attempts >= 120 or (not force and now - last_sample < 5):
                return
            attempts += 1
            last_sample = now
            try:
                value = adapter.observe_resources(session["session_id"], quick=True)
                if value.get("gpu_fingerprint") != initial_gpu or not initial_gpu:
                    return
                used = value["total_vram_gib"] - value["available_vram_gib"]
                peak = used if peak is None else max(peak, used)
                samples += 1
            except Exception:
                pass  # Advisory observation failure never changes execution authority.
        try:
            if job.cancel_requested:
                receipt.update(state="cancelled", proven_unsubmitted=True)
                self._write(receipt); self._release_claim(receipt); return receipt
            session = adapter.revalidate(receipt["prepared"])
            if isinstance(adapter, SwarmLocalAdapter) and adapter.resource_observation is not None:
                receipt["resource_observation"] = deepcopy(adapter.resource_observation)
                initial_gpu = adapter.resource_observation.get("gpu_fingerprint", "")
            if job.cancel_requested:
                receipt.update(state="cancelled", proven_unsubmitted=True)
                self._write(receipt); self._release_claim(receipt); return receipt
            receipt.update(state="submitting", session_id=session["session_id"])
            self._write(receipt)
            payload = deepcopy(receipt["prepared"]["payload"])
            payload.update(session_id=session["session_id"], extra_metadata={"collectigen_job": receipt["job_id"],
                "collectigen_request": receipt["fingerprint"]})
            if receipt["prepared"].get("source_image"):
                payload["extra_metadata"]["collectigen_source_image"] = receipt["prepared"]["source_image"]["sha256"]
                payload["extra_metadata"]["collectigen_media_task"] = receipt["prepared"]["source_image"]["task"]
            if receipt["prepared"].get("mask"):
                payload["extra_metadata"]["collectigen_mask"] = receipt["prepared"]["mask"]["sha256"]
            if receipt["prepared"].get("regional_prompt"):
                payload["extra_metadata"]["collectigen_regions"] = digest(receipt["prepared"]["regional_prompt"])
            if receipt["prepared"].get("conditioning"):
                summary = receipt["prepared"]["conditioning"]["source_summary"]
                payload["extra_metadata"]["collectigen_conditioning_kind"] = summary["kind"]
                payload["extra_metadata"]["collectigen_conditioning_source"] = summary["source_sha256"]
                payload["extra_metadata"]["collectigen_conditioning_wire"] = summary["wire_sha256"]
            submitted = True
            started = time.monotonic()
            receipt["state"] = "running"; self._write(receipt)
            results = []
            def interrupt():
                adapter.transport.post("InterruptAll", {"session_id": session["session_id"], "other_sessions": False})
                receipt["state"] = "cancelling"; self._write(receipt)
            for event in adapter.transport.events(payload, cancelled=lambda: job.cancel_requested,
                    interrupt=interrupt, progress=lambda message: job.update(0, 0, message)):
                if not job.cancel_requested:
                    sample_resources()
                if event.get("error") or event.get("error_id"):
                    receipt["failure_class"] = ("out_of_memory" if classify_memory_failure(event)
                                                else "generator_error")
                    raise RuntimeError("Generator reported a failure; inspect the retained local job.")
                if event.get("discard_indices"):
                    raise ValueError("Generator discarded a result; no output is accepted.")
                if "gen_progress" in event:
                    progress = event["gen_progress"]
                    fraction = progress.get("overall_percent", 0)
                    if isinstance(fraction, (int, float)) and math.isfinite(fraction):
                        job.update(int(max(0, min(1, fraction)) * 100), 100, "Local generator progress")
                if "image" in event:
                    # Current Swarm source uses top-level fields; earlier documented envelopes are accepted.
                    result = event["image"] if isinstance(event["image"], dict) else event
                    if len(results) or str(result.get("batch_index")) != "0":
                        raise ValueError("Unexpected output count or batch identity.")
                    results.append(result)
                    # Keep exact event durably before verification/recovery. Never log the payload.
                    write_verified_json(self._path(receipt["job_id"]) / "returned.json", event)
            receipt["stream_completed"] = True
            self._write(receipt)
            if job.cancel_requested:
                receipt["state"] = "cancelled"
            elif len(results) != 1:
                raise ValueError("Generator did not return exactly one complete result.")
            else:
                receipt["outputs"] = [self._verify_result(receipt, results[0])]
                receipt["state"] = "verified"
                sample_resources(force=True)
                try:
                    key = request_cost_key(receipt["prepared"])
                    if key and initial_gpu:
                        retained_bytes = 0
                        for path in self._path(receipt["job_id"]).iterdir():
                            _assert_unredirected(path)
                            if path.is_file():
                                retained_bytes += path.stat().st_size
                        receipt["observed_cost"] = {"request_key": key,
                            "gpu_fingerprint": initial_gpu,
                            "elapsed_seconds": time.monotonic() - started,
                            "retained_bytes": retained_bytes,
                            "sampled_device_used_gib": peak, "memory_samples": samples}
                except Exception:
                    pass
            receipt["finished_at"] = _now(); self._write(receipt)
            return deepcopy(receipt)
        except Exception as exc:
            receipt["state"] = "outcome_unknown" if submitted else "failed"
            receipt["error"] = str(exc)
            if isinstance(adapter, SwarmLocalAdapter) and adapter.resource_observation is not None:
                receipt["resource_observation"] = deepcopy(adapter.resource_observation)
            receipt["failure_class"] = receipt.get("failure_class") or (
                "resource_limited" if isinstance(exc, GenerationMemoryLimitedError)
                else "out_of_memory" if classify_memory_failure(exc) else "unclassified")
            if submitted and not receipt.get("stream_completed"):
                receipt["server_stopped"] = self._stop_session(adapter, receipt)
            if not submitted: receipt["proven_unsubmitted"] = True
            self._write(receipt)
            if not submitted: self._release_claim(receipt)
            raise

    def resource_guidance(self, prepared, observation, *, count=1):
        """Read existing owned receipts; do not launch calibration or mutate them."""
        return summarize_job_costs(prepared, observation, self.history(), count=count)

    def record_memory_evidence(self, job_id, measurement, *, expected_receipt_digest):
        """Retain bounded raw evidence on the exact verified job, without qualification.

        This explicit operation reuses receipt backups/publication/read-back. A
        stale writer or failed write never starts, repeats or approves generation.
        """
        from .generation_memory_evidence import retain_measurement, MAX_RECORDS_PER_JOB
        with _LOCK:
            receipt = self._read(job_id)
            if digest(receipt) != expected_receipt_digest:
                raise ValueError("The generation receipt changed; review it before retaining memory evidence.")
            self._check_outputs(receipt)
            retained = retain_measurement(measurement, receipt["prepared"], receipt["outputs"][0]["sha256"])
            records = receipt.get("memory_evidence", [])
            if not isinstance(records, list) or len(records) > MAX_RECORDS_PER_JOB:
                raise ValueError("Retained memory evidence is unsupported or exceeds its limit.")
            if retained in records:
                return deepcopy(receipt)
            if len(records) == MAX_RECORDS_PER_JOB:
                raise ValueError("This job has reached its bounded memory-evidence history limit.")
            receipt["memory_evidence"] = [*records, retained]
            # Recheck exact receipt/output bytes at the final publication boundary.
            current = self._read(job_id)
            if digest(current) != expected_receipt_digest:
                raise ValueError("The generation receipt changed during memory-evidence review.")
            self._check_outputs(current)
            self._write(receipt)
            saved = self._read(job_id)
            if saved != receipt:
                raise ValueError("Retained memory evidence failed receipt read-back; review recovery.")
            self._check_outputs(saved)
            return deepcopy(saved)

    def _memory_evidence_history(self, prepared):
        from .generation_memory_evidence import (retain_measurement, MAX_RECORDS_PER_JOB,
                                                 MAX_PROVIDER_RECORDS, STORED_SCHEMA)
        key, found = memory_request_key(prepared), []
        if not key:
            return found
        with _LOCK:
            candidates = []
            # Bound the whole review before hashing any output bytes. A partial
            # sample of history must not become an optimistic admission profile.
            for receipt in self.history():
                if receipt.get("state") != "verified" or memory_request_key(receipt["prepared"]) != key:
                    continue
                records = receipt.get("memory_evidence", [])
                if not isinstance(records, list) or len(records) > MAX_RECORDS_PER_JOB:
                    raise ValueError("Retained memory evidence exceeds its bounded history.")
                if not records:
                    continue
                candidates.append(receipt)
                if len(candidates) > 16:
                    raise ValueError("Memory evidence exceeds the 16-job verification limit.")
            remaining = 64 * 1024 * 1024
            declared_bytes = 0
            for receipt in candidates:
                output = self._path(receipt["job_id"]) / "result.png"
                targets = [output, output.with_suffix(".tags.json")]
                if any("raw_generator_sha256" in row for row in receipt.get("outputs", [])):
                    targets.append(output.with_name("raw-result.png"))
                for target in targets:
                    _assert_unredirected(target)
                    declared_bytes += target.stat().st_size
                    if declared_bytes > remaining:
                        raise ValueError("Memory evidence exceeds the 64 MiB output-verification limit.")
            for receipt in candidates:
                remaining -= self._check_outputs(receipt, max_verification_bytes=remaining)
                records = receipt["memory_evidence"]
                for retained in records:
                    if isinstance(retained, dict) and retained.get("schema_id") == STORED_SCHEMA:
                        rebuilt = retain_measurement(retained.get("measurement"), prepared,
                                                     receipt["outputs"][0]["sha256"])
                        if rebuilt["measurement_sha256"] != retained.get("measurement_sha256"):
                            raise ValueError("Retained memory measurement changed.")
                    found.append(deepcopy(retained))
                    if len(found) > MAX_PROVIDER_RECORDS:
                        raise ValueError("Memory evidence exceeds the bounded provider review.")
        return found

    def memory_evidence_provider(self, verifier=None):
        """Construct a lazy consumer; default native verification is unavailable."""
        from .generation_memory_evidence import GenerationMemoryEvidenceProvider
        return GenerationMemoryEvidenceProvider(self._memory_evidence_history, verifier)

    @staticmethod
    def _stop_session(adapter, receipt):
        """Best-effort own-session cleanup; no other session can be interrupted."""
        if not receipt.get("session_id"): return False
        try:
            adapter.transport.post("InterruptAll", {"session_id": receipt["session_id"], "other_sessions": False})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = adapter.transport.post("GetCurrentStatus", {"session_id": receipt["session_id"]}).get("status", {})
                fields = ("waiting_gens", "loading_models", "waiting_backends", "live_gens")
                if all(type(status.get(key)) is int and status[key] == 0 for key in fields): return True
                time.sleep(.25)
        except Exception:
            return False
        return False

    def _verify_result(self, receipt, event):
        if isinstance(event.get("image"), dict): event = event["image"]
        value = event.get("image", "")
        if not isinstance(value, str) or not value.startswith("data:image/png;base64,") or len(value) > 90 * 1024 * 1024:
            raise ValueError("Expected one bounded transient PNG; remote references are never fetched.")
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
        metadata = event.get("metadata")
        if not isinstance(metadata, str) or len(metadata) > 4 * 1024 * 1024:
            raise ValueError("Missing or oversized generation Metadata.")
        parsed = strict_json(metadata)
        params = parsed.get("sui_image_params", {})
        extra = parsed.get("sui_extra_data", {})
        if extra.get("collectigen_job") != receipt["job_id"] or extra.get("collectigen_request") != receipt["fingerprint"]:
            raise ValueError("Returned Metadata does not match the durable request identity.")
        source_image = receipt["prepared"].get("source_image")
        if source_image and extra.get("collectigen_source_image") != source_image["sha256"]:
            raise ValueError("Returned Metadata does not match the exact Remix source identity.")
        if source_image and extra.get("collectigen_media_task") != source_image["task"]:
            raise ValueError("Returned Metadata does not match the reviewed image task.")
        mask_info = receipt["prepared"].get("mask")
        if mask_info and extra.get("collectigen_mask") != mask_info["sha256"]:
            raise ValueError("Returned Metadata does not match the reviewed Paint Area mask.")
        conditioning = receipt["prepared"].get("conditioning")
        if conditioning:
            summary = conditioning["source_summary"]
            if (extra.get("collectigen_conditioning_kind") != summary["kind"]
                    or extra.get("collectigen_conditioning_source") != summary["source_sha256"]
                    or extra.get("collectigen_conditioning_wire") != summary["wire_sha256"]):
                raise ValueError("Returned Metadata differs from the exact guidance source.")
        expected = receipt["prepared"]["payload"]
        if receipt["prepared"].get("regional_prompt"):
            # T2IParamTypes.GlobalRegionFactor IgnoreIf="0.5" omits the
            # default; CreateConditioning also reads a 0.5 default. The exact
            # rendered prompt plus owned job/request/regions digests still match.
            if (extra.get("collectigen_regions") != digest(receipt["prepared"]["regional_prompt"])
                    or params.get("globalregionfactor", .5) != .5):
                raise ValueError("Returned Metadata differs from the reviewed regional prompts.")
        refinement = receipt["prepared"].get("refinement")
        if refinement is not None:
            from .generation_refinement import prepare_refinement
            context = receipt["prepared"]["plan"].get("target_context", {})
            checked = prepare_refinement(context.get("output_plan"), width=expected["width"], height=expected["height"],
                output_goal=context.get("output_goal"), binding=receipt["prepared"]["binding"],
                capabilities=receipt["prepared"]["capabilities"])
            if refinement != checked:
                raise ValueError("The retained refinement differs from the reviewed output plan.")
            for key, value in checked["payload"].items():
                default = 1 if key == "refinerupscale" else None
                if params.get(key, default) != value:
                    raise ValueError(f"Returned Metadata disagrees with reviewed {key}.")
            if params.get("refinermodel", "(Use Base)") != "(Use Base)" or params.get("refinervae", "None") != "None":
                raise ValueError("Refinement loaded an unreviewed model or VAE.")
        model_rows = parsed.get("sui_models", [])
        binding = receipt["prepared"]["binding"]
        matched_models = [row for row in model_rows if isinstance(row, dict) and row.get("param") == "model"]
        if (len(matched_models) != 1 or matched_models[0].get("name") != expected["model"]
                or _hash(matched_models[0].get("hash")) != binding["tensor_sha256"]):
            raise ValueError("Returned model tensor identity differs from the verified local resource.")
        if params.get("model") not in {expected["model"], expected["model"].removesuffix(".safetensors")}:
            raise ValueError("Returned model name differs from the bound resource.")
        components = receipt["prepared"].get("components")
        if (refinement is not None or receipt["prepared"].get("regional_prompt")) and components is None and conditioning is None:
            if (len(model_rows) != 1 or model_rows[0].get("param") != "model"):
                raise ValueError("The request loaded an unreviewed model resource.")
        if components is not None:
            observed = sorted((str(row.get("param")), str(row.get("name")), _hash(row.get("hash")))
                for row in model_rows if isinstance(row, dict))
            required = sorted((row["param"], row["name"], row["tensor_sha256"]) for row in components["expected_models"])
            if observed != required:
                raise ValueError("Returned component identities differ from the exact reviewed model set.")
            for key, value in components["expected_params"].items():
                if key == "model":
                    continue
                if key == "automaticvae":
                    if params.get(key, False) is not False:
                        raise ValueError("The generator changed automatic VAE selection.")
                elif params.get(key) != value and not (
                        isinstance(value, str) and params.get(key) == value.removesuffix(".safetensors")):
                    raise ValueError(f"Returned Metadata disagrees with reviewed {key}.")
        if conditioning:
            defaults = {"revisionstrength": 0, "controlnetstart": 0.0,
                        "controlnetend": 1.0, "controlnetpreprocessor": "None"}
            for key, value in conditioning["expectedparams"].items():
                if key in {"controlnetimageinput", "promptimages"}:
                    continue  # Swarm may omit large image bytes from returned Metadata.
                actual = params.get(key, defaults.get(key))
                if actual != value and not (
                        key == "controlnetmodel" and actual == value.removesuffix(".safetensors")):
                    raise ValueError(f"Returned Metadata disagrees with reviewed {key}.")
            if conditioning["expected_models"]:
                expected_control = conditioning["expected_models"][0]
                controls = [row for row in model_rows if isinstance(row, dict)
                            and row.get("param") == "controlnetmodel"]
                if (len(controls) != 1 or controls[0].get("name") != expected_control["swarm_name"]
                        or _hash(controls[0].get("hash")) != expected_control["tensor_sha256"]):
                    raise ValueError("Returned ControlNet tensor identity differs from the reviewed model.")
            elif any(isinstance(row, dict) and row.get("param") == "controlnetmodel" for row in model_rows):
                raise ValueError("Reference Only unexpectedly loaded a ControlNet model.")
            if components is None:
                expected_params = {"model"} | ({"controlnetmodel"} if conditioning["expected_models"] else set())
                if len(model_rows) != len(expected_params) or {
                        row.get("param") for row in model_rows if isinstance(row, dict)} != expected_params:
                    raise ValueError("Returned guidance model set includes an unreviewed resource.")
        for key in ("prompt", "negativeprompt", "width", "height", "seed", "steps", "cfgscale", "sampler", "scheduler"):
            if params.get(key, "" if key == "negativeprompt" else None) != expected[key]:
                raise ValueError(f"Returned Metadata disagrees with reviewed {key}.")
        if source_image and "initimagecreativity" in expected and params.get("initimagecreativity") != expected["initimagecreativity"]:
            raise ValueError("Returned Metadata disagrees with reviewed Remix change strength.")
        with Image.open(io.BytesIO(raw)) as im:
            canvas = tuple(refinement["generator_canvas"]) if refinement else (expected["width"], expected["height"])
            if im.format != "PNG" or im.size != canvas or getattr(im, "n_frames", 1) != 1:
                raise ValueError("Returned image type/dimensions do not match the request.")
            im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
        directory = self._path(receipt["job_id"])
        output = directory / "result.png"
        raw_output = None
        if mask_info:
            inputs = receipt["prepared"]["request"]["stages"][0]["inputs"]
            mask_payload = MaskPayload.from_dict(inputs[1]["extras"]["mask_payload"])
            source_bytes = base64.b64decode(source_image.get("full_source_data_url", expected["initimage"]).split(",", 1)[1], validate=True)
            if hashlib.sha256(source_bytes).hexdigest() != source_image.get("composite_source_sha256", source_image["sha256"]):
                raise ValueError("Frozen source image differs from the reviewed Remix identity.")
            compositing_input = raw
            if mask_payload.crop_region is not None:
                region = mask_payload.crop_region
                with Image.open(io.BytesIO(raw)) as generated:
                    cropped = generated.crop((0, 0, region[2] - region[0], region[3] - region[1]))
                    encoded = io.BytesIO()
                    cropped.save(encoded, format="PNG")
                    compositing_input = encoded.getvalue()
            composed = composite_masked_result(source_bytes, compositing_input, mask_payload)
            raw_output = directory / "raw-result.png"
            _assert_unredirected(raw_output)
            if raw_output.exists() and raw_output.read_bytes() != raw:
                raise ValueError("Raw generator output collision; retained files are never overwritten.")
            atomic_verified_bytes(raw_output, raw, label="Raw masked generation")
            raw = composed
        output_plan = receipt["prepared"].get("output_plan")
        if refinement is not None and refinement["resize"] is not None:
            from .generation_output_processing import render_faithful_resize
            raw_output = directory / "raw-result.png"
            _assert_unredirected(raw_output)
            if raw_output.exists() and raw_output.read_bytes() != raw:
                raise ValueError("Raw refinement output collision; retained files are never overwritten.")
            atomic_verified_bytes(raw_output, raw, label="Raw refined image")
            raw = render_faithful_resize(raw, refinement["resize"])
        if output_plan is not None:
            if mask_info:
                raise ValueError("A protected-pixel edit cannot also use faithful final resize.")
            from .generation_output_processing import validate_faithful_resize, render_faithful_resize
            context = receipt["prepared"]["plan"].get("target_context", {})
            reviewed = validate_faithful_resize(context.get("output_plan"),
                base_width=expected["width"], base_height=expected["height"],
                output_goal=context.get("output_goal"))
            if output_plan != reviewed:
                raise ValueError("The retained final-size stage differs from the reviewed plan.")
            raw_output = directory / "raw-result.png"
            _assert_unredirected(raw_output)
            if raw_output.exists() and raw_output.read_bytes() != raw:
                raise ValueError("Raw generator output collision; retained files are never overwritten.")
            atomic_verified_bytes(raw_output, raw, label="Raw generated base image")
            raw = render_faithful_resize(raw, reviewed)
        _assert_unredirected(output)
        _assert_unredirected(output.with_suffix(".tags.json"))
        if output.exists() and output.read_bytes() != raw:
            raise ValueError("Output collision; retained files are never overwritten.")
        atomic_verified_bytes(output, raw, label="Generated image")
        # Initialize the existing authoritative schema; prompts never become confirmed tags.
        from .image_record import ImageRecord
        record = ImageRecord(output, auto_load=False)
        gen = parse_generation_payload(metadata, source="SwarmUI local generation", source_field="sui_image_params")
        gen.id = receipt["job_id"]
        if components is not None or conditioning is not None:
            from .annotation import GenerationResource
            gen.extra_fields["runtime_resource_metadata"] = [item.to_dict() for item in gen.resources]
            gen.resources = [GenerationResource.from_dict(value) for value in receipt["prepared"]["request"]["resources"]]
            gen.model_hash = binding["resource_id"].removeprefix("sha256:")
        if conditioning:
            gen.extra_fields["collectigen_conditioning"] = deepcopy(conditioning["source_summary"])
        gen.extra_fields["collectigen_execution"] = {"job_id": receipt["job_id"], "request_fingerprint": receipt["fingerprint"],
            "adapter_id": ADAPTER_ID, "adapter_version": receipt["prepared"]["capabilities"]["version"],
            "resources": [{"sha256": receipt["prepared"]["binding"]["resource_id"].removeprefix("sha256:"),
                           "tensor_sha256": receipt["prepared"]["binding"]["tensor_sha256"]}]}
        if components is not None:
            gen.extra_fields["collectigen_execution"]["resources"] = deepcopy(components["resources"])
        if source_image:
            gen.extra_fields["collectigen_execution"]["source_image_sha256"] = source_image["sha256"]
            gen.extra_fields["collectigen_execution"]["media_task"] = source_image["task"]
            if source_image.get("canvas_expansion"):
                gen.extra_fields["collectigen_execution"]["canvas_expansion"] = source_image["canvas_expansion"]
        if conditioning:
            gen.extra_fields["collectigen_execution"]["conditioning"] = {
                "source_summary": deepcopy(conditioning["source_summary"]),
                "expected_models": deepcopy(conditioning["expected_models"])}
        if output_plan is not None:
            gen.extra_fields["collectigen_execution"]["final_size"] = {
                "plan": deepcopy(output_plan), "raw_generator_sha256": hashlib.sha256(raw_output.read_bytes()).hexdigest(),
                "final_sha256": hashlib.sha256(raw).hexdigest()}
        if refinement is not None:
            gen.extra_fields["collectigen_execution"]["refinement"] = deepcopy(refinement)
            gen.extra_fields["collectigen_execution"]["refinement"]["refiner_seed"] = expected["seed"] + 1
            if raw_output is not None:
                gen.extra_fields["collectigen_execution"]["raw_generator_sha256"] = hashlib.sha256(raw_output.read_bytes()).hexdigest()
        if mask_info:
            gen.extra_fields["collectigen_execution"]["mask_sha256"] = mask_info["sha256"]
            gen.extra_fields["collectigen_execution"]["raw_generator_sha256"] = hashlib.sha256(raw_output.read_bytes()).hexdigest()
            gen.extra_fields["collectigen_execution"]["compositing"] = {
                "operation": "protected_source_pixels", "version": mask_payload.operation_version,
                "feather_px": mask_payload.feather_px, "feather_direction": "inward",
                "source_sha256": mask_payload.source_sha256, "mask_sha256": mask_payload.mask_sha256,
            }
            if mask_payload.crop_region is not None:
                gen.extra_fields["collectigen_execution"]["compositing"].update(
                    crop_region=list(mask_payload.crop_region), crop_padding_px=mask_payload.crop_padding_px,
                    generator_canvas=mask_info["generator_canvas"], padding=mask_info["padding"])
        record.annotation.generation_records = [gen]
        record.annotation.source = {"generation_job": receipt["job_id"], "request_fingerprint": receipt["fingerprint"]}
        if source_image:
            record.annotation.source["source_image_sha256"] = source_image["sha256"]
        if mask_info:
            record.annotation.source["mask_sha256"] = mask_info["sha256"]
        record.annotation.review_status = "needs_review"
        record.loaded = True; record.modified = True
        if record.sidecar_path.exists():
            existing = ImageRecord(output)
            stored = existing.annotation.generation_records
            if (existing.annotation.source != record.annotation.source or len(stored) != 1
                    or stored[0].id != gen.id or stored[0].raw != gen.raw
                    or stored[0].extra_fields.get("collectigen_execution") != gen.extra_fields["collectigen_execution"]):
                raise ValueError("Existing output knowledge differs; review recovery manually.")
        else:
            record.save_sidecar()
        verified = ImageRecord(output)
        if verified.annotation.source != record.annotation.source or verified.annotation.generation_records[0].raw != metadata:
            raise ValueError("Output Image Knowledge read-back did not preserve generation evidence.")
        result = {"path": str(output), "sha256": hashlib.sha256(raw).hexdigest(),
                "metadata_sha256": hashlib.sha256(metadata.encode()).hexdigest(),
                "sidecar_sha256": hashlib.sha256(record.sidecar_path.read_bytes()).hexdigest()}
        if raw_output is not None:
            result["raw_generator_sha256"] = hashlib.sha256(raw_output.read_bytes()).hexdigest()
        return result

    def recover(self, job_id, *, adapter=None):
        """Read/verify a retained response; never submits or assumes a lost job stopped."""
        with _LOCK:
            receipt = self._read(job_id)
            if any(job.key == "generation:" + receipt["fingerprint"] for job in getattr(self.jobs, "active", lambda: [])()):
                raise ValueError("This generation is still tracked; use its active progress or cancel action.")
            if adapter is not None and receipt.get("state") in {"submitting", "running", "cancelling", "outcome_unknown"}:
                if adapter.endpoint != receipt["prepared"]["capabilities"].get("endpoint"):
                    raise ValueError("Recovery must use the original local endpoint.")
                receipt["server_stopped"] = self._stop_session(adapter, receipt)
            returned = self._path(job_id) / "returned.json"
            if receipt["state"] == "verified":
                self._check_outputs(receipt)
            elif returned.exists() and receipt.get("stream_completed") and receipt["state"] not in {"cancelled", "cancelling"}:
                _assert_unredirected(returned)
                if returned.stat().st_size > 96 * 1024 * 1024:
                    raise ValueError("Retained response exceeds recovery limit.")
                receipt["outputs"] = [self._verify_result(receipt, strict_json(returned.read_bytes()))]
                receipt["state"] = "verified"; self._write(receipt)
            elif receipt["state"] not in {"cancelled", "failed"}:
                receipt["state"] = "outcome_unknown"; self._write(receipt)
            return receipt

    def verified_output_paths(self, expected_receipt):
        """Recheck the exact displayed receipt and files before an Ingest handoff."""
        with _LOCK:
            receipt = self._read(expected_receipt.get("job_id", ""))
            if receipt != expected_receipt:
                raise ValueError("The retained result changed; recover and review it before sending to Ingest.")
            self._check_outputs(receipt)
            return [output["path"] for output in receipt["outputs"]]

    def _check_outputs(self, receipt, *, max_verification_bytes=None):
        if receipt.get("state") != "verified" or len(receipt.get("outputs", [])) != 1:
            raise ValueError("A verified result is required.")
        output = receipt["outputs"][0]
        path = self._path(receipt["job_id"]) / "result.png"
        consumed = 0
        def checked_digest(target):
            nonlocal consumed
            limit = 96 * 1024 * 1024
            if max_verification_bytes is not None:
                limit = min(limit, max_verification_bytes - consumed)
            if limit < 0 or target.stat().st_size > limit:
                raise ValueError("Output verification exceeds its remaining byte budget.")
            with target.open("rb") as handle:
                raw = handle.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("Output grew beyond its verification byte budget.")
            consumed += len(raw)
            return hashlib.sha256(raw).hexdigest()
        for target, key in ((path, "sha256"), (path.with_suffix(".tags.json"), "sidecar_sha256")):
            _assert_unredirected(target)
            if target.stat().st_size > 96 * 1024 * 1024:
                raise ValueError("Retained output exceeds verification limits.")
            if checked_digest(target) != output[key]:
                raise ValueError("A verified output changed; review before reuse.")
        if "raw_generator_sha256" in output:
            raw_target = self._path(receipt["job_id"]) / "raw-result.png"
            _assert_unredirected(raw_target)
            if (raw_target.stat().st_size > 96 * 1024 * 1024
                    or checked_digest(raw_target) != output["raw_generator_sha256"]):
                raise ValueError("The retained raw generator output changed; review before reuse.")
        return consumed

    def record_outcome(self, job_id, outcome, reasons, *, ingest_identity, ingest_history=None):
        if outcome not in {"useful", "failed", "mixed"} or not isinstance(reasons, str) or len(reasons) > 4000:
            raise ValueError("Choose useful, failed or mixed and bounded review reasons.")
        with _LOCK:
            receipt = self._read(job_id); self._check_outputs(receipt)
            # A normal Ingest return must preserve both output bytes and exact generation lineage.
            path = Path(ingest_identity.get("path", ""))
            _assert_unredirected(path)
            if not path.is_file():
                raise ValueError("The normal Ingest result identity is missing or changed.")
            retained = self._path(job_id) / "result.png"
            if path.resolve().is_relative_to(self.root.resolve()) or path.samefile(retained):
                raise ValueError("Retained generation files are not Ingested results; complete normal Ingest transfer first.")
            if ingest_history is None or not callable(getattr(ingest_history, "record_for_path", None)):
                raise ValueError("Normal Ingest transfer history is required before recording an outcome review.")
            transfer = ingest_history.record_for_path(path)
            transfer_kind = {"Add Images: Copy to Ingest": "copy", "Add Images: Move to Ingest": "move"}
            expected_kind = transfer_kind.get(getattr(transfer, "reason", ""))
            def same_record_path(value, expected):
                return (isinstance(value, str) and Path(value).is_absolute()
                        and os.path.normcase(os.path.normpath(value))
                        == os.path.normcase(os.path.normpath(str(expected))))
            image_transfer = any(
                isinstance(item, dict)
                and same_record_path(item.get("from"), retained)
                and same_record_path(item.get("to"), path)
                and item.get("sha256") == receipt["outputs"][0]["sha256"]
                for item in getattr(transfer, "files", ())
            )
            if (transfer is None or transfer.undone or not transfer.operation_id
                    or expected_kind is None or transfer.operation_kind != expected_kind
                    or not image_transfer):
                raise ValueError("This result lacks an exact completed normal Ingest transfer; review its Ingest history.")
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["outputs"][0]["sha256"]:
                raise ValueError("The normal Ingest result identity is missing or changed.")
            from .image_record import ImageRecord
            record = ImageRecord(path)
            if (record.annotation.source.get("generation_job") != job_id
                    or record.annotation.source.get("request_fingerprint") != receipt["fingerprint"]):
                raise ValueError("Ingest has not retained this generation lineage.")
            records = [r for r in record.annotation.generation_records if r.id == job_id]
            if len(records) != 1 or not isinstance(records[0].raw, str):
                raise ValueError("Ingest generation evidence is missing or ambiguous.")
            generation = records[0]
            reference = ImageRecord(receipt["outputs"][0]["path"]).annotation.generation_records[0]
            if (hashlib.sha256(generation.raw.encode()).hexdigest() != receipt["outputs"][0]["metadata_sha256"]
                    or generation.extra_fields.get("collectigen_execution") != reference.extra_fields.get("collectigen_execution")):
                raise ValueError("Ingest generation evidence differs from the verified request lineage.")
            observation = {"outcome": outcome, "reasons": reasons, "reviewed_at": _now(),
                           "job_id": job_id, "ingest_sha256": receipt["outputs"][0]["sha256"],
                           "goal": receipt["prepared"]["plan"].get("goal", ""),
                           "request_fingerprint": receipt["fingerprint"],
                           "resource_id": receipt["prepared"]["binding"]["resource_id"],
                           "adapter_id": ADAPTER_ID, "adapter_version": receipt["prepared"]["capabilities"]["version"],
                           "prompt_role": "whole_request", "authority": "reviewed_generation_observation"}
            receipt.setdefault("observation_history", []).extend(receipt.get("observations", []))
            receipt["observations"] = [observation]
            self._write(receipt)
            return deepcopy(observation)

    def guidance(self, *, resource_id, adapter_version, goal):
        observations = [deepcopy(o) for receipt in self.history() for o in receipt.get("observations", [])
                        if o["resource_id"] == resource_id and o["adapter_version"] == adapter_version and o["goal"] == goal]
        return {"evidence": observations, "sample_size": len(observations), "authority": "observed_outcomes",
                "uncertainty": "Small, self-reviewed observations; no universal quality or tag score.",
                "effect": "Recommendation only; apply future request changes explicitly."}
