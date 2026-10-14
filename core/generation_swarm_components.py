"""Pure exact-resource mapping to SwarmUI's selected model parameters.

The caller supplies independently verified local bindings. Mapping proves only
that each selected full-file identity has an explicit Swarm tensor identity; it
does not qualify a model/companion pair or authorize generation/downloads.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TEXT_SLOTS = frozenset({"cliplmodel", "t5xxlmodel", "qwenmodel", "mistralmodel"})
_FAMILY_CLASSES = {
    "flux-1": ({"flux.1-dev", "flux.1-schnell"}, {"vae", "cliplmodel", "t5xxlmodel"}),
    "anima": ({"anima"}, {"vae", "qwenmodel"}),
    "flux-2": ({"flux.2-dev"}, {"vae", "mistralmodel"}),
}
_SIMPLE_FAMILIES = frozenset({"sd1", "stable-diffusion-v1", "sdxl", "stable-diffusion-xl-v1"})
_MAX_RESOURCES = 24
_REQUEST_FIELDS = frozenset({"id", "source", "source_field", "format", "generator_name",
    "generator_version", "positive_prompt", "negative_prompt", "model", "model_hash",
    "vae", "sampler", "scheduler", "seed", "steps", "cfg_scale", "width", "height",
    "denoising_strength", "loras", "resources", "stages", "adapter_report", "extras", "raw"})
_RESOURCE_FIELDS = frozenset({"id", "role", "name", "hashes", "source_id",
    "version_id", "compatibility", "weights", "trigger_words", "provenance", "extras"})


def _value(item):
    if isinstance(item, Mapping):
        return item
    convert = getattr(item, "to_dict", None)
    if callable(convert):
        result = convert()
        if isinstance(result, Mapping):
            return result
    raise ValueError("A selected generation resource is malformed.")


def _sha(value):
    text = str(value or "").lower().removeprefix("sha256:").removeprefix("0x")
    if not _SHA.fullmatch(text):
        raise ValueError("A selected model lacks an exact SHA-256 identity.")
    return text


def _name(value, *, list_entry=False):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or value.startswith(("/", "\\")) or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or list_entry and ("," in value or "\n|||\n" in value)):
        raise ValueError("A bound Swarm model name cannot be sent exactly.")
    return value


def _weight(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not -10 <= value <= 10:
        raise ValueError("LoRA model and text-encoder weights must be explicit finite values from -10 to 10.")
    return format(value, ".17g")


def prepare_swarm_components(request, bindings: Mapping, capabilities: Mapping) -> dict[str, object]:
    """Map each selected resource once; return exact wire and metadata checks.

    `bindings` is keyed by the selected GenerationResource.id. Every binding
    must carry the selected full-file SHA and a distinct cached Swarm tensor SHA.
    Text encoder slots are explicit, never inferred from file names or family.
    """
    req = _value(request)
    if set(req) - _REQUEST_FIELDS:
        raise ValueError("Unknown request fields need a separate execution mapping.")
    if not isinstance(bindings, Mapping) or not isinstance(capabilities, Mapping):
        raise ValueError("Check exact local model bindings and generator capabilities first.")
    selected = req.get("resources")
    if not isinstance(selected, list) or not 1 <= len(selected) <= _MAX_RESOURCES:
        raise ValueError("Select a bounded exact model resource set including one checkpoint.")
    if req.get("loras") or req.get("extras") or req.get("extra_fields"):
        raise ValueError("Unmapped request extensions or LoRA directives need explicit review.")
    parameters = capabilities.get("parameters")
    fingerprint = capabilities.get("fingerprint")
    if not isinstance(parameters, Mapping) or not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Check the local Swarm parameter catalog again.")
    required_params = {"model", "automaticvae"}
    seen_ids = set()
    seen_full_hashes = set()
    seen_names = set()
    slots = {}
    mapped = []
    loras = []
    for item in selected:
        resource = _value(item)
        if set(resource) - _RESOURCE_FIELDS:
            raise ValueError("Unknown resource fields need a separate execution mapping.")
        resource_id = resource.get("id")
        role = resource.get("role")
        if (not isinstance(resource_id, str) or not resource_id or resource_id in seen_ids
                or role not in {"checkpoint", "lora", "vae", "text_encoder", "controlnet"}):
            raise ValueError("Every selected component needs one distinct supported role and identifier.")
        seen_ids.add(resource_id)
        hashes = resource.get("hashes")
        if (not isinstance(hashes, Mapping) or set(hashes) != {"sha256"}
                or resource.get("extras") or resource.get("extra_fields")
                or role != "lora" and resource.get("weights")):
            raise ValueError("A selected component has unknown execution fields or identity.")
        full_sha = _sha(hashes["sha256"])
        if full_sha in seen_full_hashes:
            raise ValueError("The same full-file model identity was selected more than once.")
        seen_full_hashes.add(full_sha)
        binding = bindings.get(resource_id)
        if not isinstance(binding, Mapping) or binding.get("verified") is not True:
            raise ValueError("Every selected component needs one verified local Swarm binding.")
        if (binding.get("role") not in ({"controlnet", "control_model"} if role == "controlnet" else {role})
                or _sha(binding.get("resource_id")) != full_sha
                or binding.get("capability_fingerprint") != fingerprint):
            raise ValueError("A component binding differs from its selected full-file identity or connection.")
        name = _name(binding.get("name"), list_entry=role == "lora")
        tensor_sha = _sha(binding.get("tensor_sha256"))
        if (role, name) in seen_names:
            raise ValueError("The same Swarm model name was selected more than once for a role.")
        seen_names.add((role, name))
        slot = "model" if role == "checkpoint" else "vae" if role == "vae" else (
            "controlnetmodel" if role == "controlnet" else
            binding.get("slot") if role == "text_encoder" else "loras")
        if role == "text_encoder" and slot not in _TEXT_SLOTS:
            raise ValueError("Choose the exact text-encoder parameter slot; file names cannot determine it.")
        if role != "text_encoder" and binding.get("slot") not in (None, "", slot):
            raise ValueError("A component binding has an unexpected parameter slot.")
        if role != "lora":
            if slot in slots:
                raise ValueError("Two selected components claim the same Swarm parameter slot.")
            slots[slot] = (resource, binding, name, tensor_sha)
            required_params.add(slot)
        else:
            weights = resource.get("weights")
            if not isinstance(weights, Mapping) or set(weights) != {"model", "text_encoder"}:
                raise ValueError("Each LoRA needs explicit model and text-encoder weights.")
            loras.append((name, _weight(weights["model"]),
                          _weight(weights["text_encoder"]), tensor_sha))
            required_params.update({"loras", "loraweights", "loratencweights"})
        mapped.append({"resource_id": resource_id, "role": role, "slot": slot,
                       "selected_name": resource.get("name", ""), "swarm_name": name,
                       "full_file_sha256": full_sha, "tensor_sha256": tensor_sha})
    if set(bindings) != seen_ids:
        raise ValueError("Bindings contain a missing or unselected component.")
    if "model" not in slots:
        raise ValueError("Select exactly one bound checkpoint.")
    checkpoint, main_binding, model_name, _ = slots["model"]
    if req.get("model") not in (None, "", model_name):
        raise ValueError("The request model name differs from its exact Swarm binding.")
    if _sha(req.get("model_hash")) != _sha(checkpoint["hashes"]["sha256"]):
        raise ValueError("The request checkpoint full-file SHA differs from its selection.")
    if req.get("vae") not in (None, "", slots["vae"][2] if "vae" in slots else None):
        raise ValueError("The request VAE differs from its exact selected binding.")
    family = str(main_binding.get("family") or "").lower()
    model_class = str(main_binding.get("model_class") or "").lower()
    if family in _FAMILY_CLASSES:
        classes, companions = _FAMILY_CLASSES[family]
        if model_class not in classes:
            raise ValueError("This modular model variant has no reviewed component mapping.")
        missing = companions - set(slots)
        if missing:
            raise ValueError("This modular model requires exact bound companions: " + ", ".join(sorted(missing)))
        if set(slots) - {"model"} - companions:
            raise ValueError("A modular model has an extra or ambiguous companion slot.")
    elif family in _SIMPLE_FAMILIES:
        if any(slot in _TEXT_SLOTS for slot in slots):
            raise ValueError("Text-encoder overrides need a reviewed modular model mapping.")
    else:
        raise ValueError("This model family has no reviewed Swarm component mapping.")
    offered = set(parameters)
    if missing := required_params - offered:
        raise ValueError("SwarmUI lacks selected component parameters: " + ", ".join(sorted(missing)))
    supported_families = capabilities.get("supported_families")
    if not isinstance(supported_families, (list, tuple, set)) or family not in supported_families:
        raise ValueError("This model family is not admitted by the local adapter capabilities.")
    payload = {"model": model_name, "automaticvae": False}
    for slot, (_resource, _binding, name, _tensor) in slots.items():
        if slot != "model":
            payload[slot] = name
    if loras:
        payload["loras"] = ",".join(entry[0] for entry in loras)
        payload["loraweights"] = ",".join(entry[1] for entry in loras)
        payload["loratencweights"] = ",".join(entry[2] for entry in loras)
    expected_params = dict(payload)
    if loras:
        expected_params["loras"] = [entry[0] for entry in loras]
        expected_params["loraweights"] = [entry[1] for entry in loras]
        expected_params["loratencweights"] = [entry[2] for entry in loras]
    expected_models = [
        {"param": item["slot"], "name": item["swarm_name"],
         "tensor_sha256": item["tensor_sha256"]}
        for item in mapped
    ]
    return {"payload": payload, "expected_params": expected_params,
            "expected_models": expected_models,
            "resources": mapped,
            "notes": {"family": family, "model_class": model_class,
                      "binding_proof_only": True, "pair_qualified": False,
                      "requires_separate_pair_qualification": bool(loras or len(slots) > 1)}}
