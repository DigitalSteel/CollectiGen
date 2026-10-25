"""Reviewable reusable settings; never persist a source plan or execution permission."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

from .annotation import GenerationRecord, GenerationResource, GenerationStage
from .generation_planning import GenerationPlan
from .training_source_io import _assert_unredirected

MAX_BYTES = 256 * 1024
SETTINGS = ("model", "model_hash", "vae", "width", "height", "steps", "sampler", "scheduler", "cfg_scale")
MODEL_SETTINGS = ("model", "model_hash", "vae")
SAMPLING_SETTINGS = ("width", "height", "steps", "sampler", "scheduler", "cfg_scale")
STAGE_SETTINGS = ("kind", "width", "height", "steps", "cfg_scale", "sampler", "scheduler", "denoising_strength", "resource_ids")
GOAL_KEYS = {"schema_id", "final_width", "final_height", "detail_preference", "preset"}
PLAN_KEYS = {"schema_id", "kind", "base_width", "base_height", "refine_width", "refine_height",
             "final_width", "final_height", "strength", "steps", "filter", "reviewed"}
SHA = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}\Z")
PROMPT_PART_KINDS = ("character", "outfit", "scene", "pose", "style", "other")


def render_prompt_parts(parts):
    """Render authored parts in their chosen order, without parsing old prompts."""
    if not isinstance(parts, (list, tuple)) or len(parts) > 32:
        raise ValueError("Choose up to 32 structured prompt parts.")
    result = []
    for part in parts:
        if (not isinstance(part, dict) or set(part) != {"kind", "text"} or
                part["kind"] not in PROMPT_PART_KINDS or
                not isinstance(part["text"], str) or not part["text"].strip() or
                len(part["text"]) > 4096):
            raise ValueError("Each prompt part needs a supported purpose and bounded text.")
        result.append(part["text"])
    rendered = ", ".join(result)
    if len(rendered) > 16384:
        raise ValueError("The combined prompt exceeds the setup limit.")
    return rendered


def _display_label(value):
    return (isinstance(value, str) and bool(value) and len(value) <= 160
            and not any(char in value for char in ("/", "\\", "\r", "\n", "\t"))
            and all(ord(char) >= 32 for char in value))


def _family_name(value):
    return (isinstance(value, str) and bool(value) and len(value) <= 160
            and not value.startswith("/") and not any(char in value for char in ("\\", ":"))
            and all(ord(char) >= 32 for char in value))


def _resource_label_key(row):
    families = row.get("compatibility", ())
    family = families[0] if isinstance(families, (list, tuple)) and len(families) == 1 else ""
    return row.get("id", ""), row.get("role", ""), family


def _hash(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("Recipe resources and source binding require exact SHA-256 hashes.")
    return value.lower()


def portable_recipe_resource(resource, *, display_label=""):
    """Project a selected exact registry component without machine paths."""
    if not isinstance(resource, GenerationResource):
        raise ValueError("Choose an exact registered model component.")
    row = {"id": resource.id, "role": resource.role, "name": resource.name,
           "hashes": {"sha256": _hash(resource.hashes.get("sha256"))},
           "compatibility": list(resource.compatibility),
           "weights": {key: resource.weights[key] for key in ("model", "text_encoder")
                       if key in resource.weights}, "provenance": {}}
    if display_label:
        if not _display_label(display_label):
            raise ValueError("Choose a safe path-free model label.")
        row["display_label"] = display_label
    return row


def _name(value):
    if not isinstance(value, str) or len(value) > 240 or any(ord(c) < 32 for c in value):
        raise ValueError("Recipe names must be bounded text.")
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value or ".." in value.split("/"):
        raise ValueError("A machine path cannot be saved in a recipe.")
    return value


def _binding(identity):
    rows = identity.get("collection") if isinstance(identity, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 4:
        raise ValueError("Source-bound stages require exact image hashes.")
    return [_hash(row.get("image_sha256") if isinstance(row, dict) else None) for row in rows]


def _validate(recipe):
    version = recipe.get("schema_version") if isinstance(recipe, dict) else None
    allowed = ({"schema_version", "name", "settings", "resources", "stages", "source_binding",
                "source_settings", "finishing", "prompts"} if version == 2 else
               {"schema_version", "name", "settings", "resources", "resource_policy",
                "finishing", "prompt_parts", "negative_prompt"} if version == 3 else set())
    if not allowed or set(recipe) - allowed:
        raise ValueError("Unsupported recipe version or fields.")
    if version == 3:
        render_prompt_parts(recipe.get("prompt_parts", []))
        negative = recipe.get("negative_prompt")
        if negative is not None and (not isinstance(negative, str) or len(negative) > 16384):
            raise ValueError("The negative prompt exceeds the setup limit.")
        if recipe.get("resource_policy") not in (None, "merge_selected"):
            raise ValueError("Choose a supported resource application policy.")
        if "resources" in recipe and recipe.get("resource_policy") != "merge_selected":
            raise ValueError("Structured setups must name how selected resources apply.")
    name = recipe.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("Give this setup a name of 1 to 120 characters.")
    settings = recipe.get("settings")
    if not isinstance(settings, dict) or set(settings) - set(SETTINGS):
        raise ValueError("Recipe settings contain unsupported fields.")
    for key in ("model", "vae"):
        if key in settings: _name(settings[key])
    if settings.get("model"):
        _hash(settings.get("model_hash"))
    elif settings.get("model_hash"):
        if settings.get("model") != "":
            raise ValueError("A model hash needs a model name or an explicit registered checkpoint.")
        _hash(settings["model_hash"])
    resources = recipe.get("resources", [])
    if not isinstance(resources, list) or len(resources) > 32:
        raise ValueError("Recipe resources are invalid.")
    required = {"id", "role", "name", "hashes", "weights", "provenance"}
    for row in resources:
        if not isinstance(row, dict) or not required <= set(row) or set(row) - (required | {"compatibility", "display_label"}):
            raise ValueError("Recipe resource fields are unsupported.")
        if not isinstance(row["id"], str) or not row["id"] or len(row["id"]) > 128:
            raise ValueError("Recipe resource ID is invalid.")
        if not isinstance(row["role"], str) or not row["role"] or len(row["role"]) > 64:
            raise ValueError("Recipe resource role is invalid.")
        if version == 3 and row["role"] not in {"checkpoint", "lora", "vae"}:
            raise ValueError("The manual setup supports exact checkpoints, LoRAs and VAEs only.")
        _name(row["name"])
        families = row.get("compatibility", [])
        if (not isinstance(families, list) or len(families) > 8 or
                any(not _family_name(family) for family in families)):
            raise ValueError("Recipe resource families are invalid.")
        if "display_label" in row and not _display_label(row["display_label"]):
            raise ValueError("Recipe resource display label is invalid.")
        if not isinstance(row["hashes"], dict) or set(row["hashes"]) != {"sha256"}:
            raise ValueError("Recipe resource needs an exact hash.")
        _hash(row["hashes"]["sha256"])
        if not isinstance(row["weights"], dict) or set(row["weights"]) - {"model", "text_encoder"}:
            raise ValueError("Recipe resource weights are unsupported.")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in row["weights"].values()):
            raise ValueError("Recipe resource weights must be finite.")
        provenance = row["provenance"]
        allowed_slots = {"cliplmodel", "t5xxlmodel", "qwenmodel", "mistralmodel"}
        if not isinstance(provenance, dict):
            raise ValueError("Recipe resource provenance is invalid.")
        if ((row["role"] == "text_encoder" and
             (set(provenance) != {"swarm_parameter"} or provenance.get("swarm_parameter") not in allowed_slots))
                or (row["role"] != "text_encoder" and provenance)):
            raise ValueError("Recipe text encoder requires one supported parameter slot.")
    checkpoint = [row for row in resources if row["role"] == "checkpoint"]
    if checkpoint and (len(checkpoint) != 1 or _hash(settings.get("model_hash")) != _hash(checkpoint[0]["hashes"]["sha256"])):
        raise ValueError("Recipe model and checkpoint hashes differ.")
    if settings.get("model_hash") and settings.get("model") == "" and not checkpoint:
        raise ValueError("A hash-only model setup needs its exact registered checkpoint.")
    stages = recipe.get("stages", [])
    if not isinstance(stages, list) or len(stages) > 8:
        raise ValueError("Recipe stages are invalid.")
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) - set(STAGE_SETTINGS) or not stage.get("kind"):
            raise ValueError("Recipe stage fields are unsupported.")
        _name(stage["kind"])
        ids = stage.get("resource_ids", [])
        if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(item, str) for item in ids):
            raise ValueError("Recipe stage resources are invalid.")
        if set(ids) - {row["id"] for row in resources}:
            raise ValueError("Recipe stage resources differ from the saved resources.")
    binding = recipe.get("source_binding", [])
    if not isinstance(binding, list) or len(binding) > 4:
        raise ValueError("Recipe source binding is invalid.")
    for value in binding: _hash(value)
    source_settings = recipe.get("source_settings", {})
    if (not isinstance(source_settings, dict) or set(source_settings) - {"denoising_strength"}
            or source_settings and not binding):
        raise ValueError("Recipe source settings require a source binding.")
    if "denoising_strength" in source_settings and (type(source_settings["denoising_strength"]) not in (int, float)
            or not math.isfinite(source_settings["denoising_strength"]) or not 0 < source_settings["denoising_strength"] <= 1):
        raise ValueError("Recipe remix strength is invalid.")
    finishing = recipe.get("finishing", {})
    if not isinstance(finishing, dict) or set(finishing) - {"output_goal", "output_plan"}:
        raise ValueError("Recipe finishing fields are unsupported.")
    for key, allowed in (("output_goal", GOAL_KEYS), ("output_plan", PLAN_KEYS)):
        if key in finishing and (not isinstance(finishing[key], dict) or set(finishing[key]) - allowed):
            raise ValueError("Recipe finishing fields are unsupported.")
    if ("preset" in finishing.get("output_goal", {}) and
            (not isinstance(finishing["output_goal"]["preset"], str) or
             finishing["output_goal"]["preset"] not in {"quick_preview", "balanced", "final_output", "lower_memory"})):
        raise ValueError("Recipe output goal preset is unsupported.")
    prompts = recipe.get("prompts")
    if prompts is not None and (not isinstance(prompts, dict) or set(prompts) != {"positive_prompt", "negative_prompt"}
                                or any(not isinstance(v, str) or len(v) > 16384 for v in prompts.values())):
        raise ValueError("Recipe prompt templates are invalid.")
    raw = json.dumps(recipe, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Recipe exceeds its size limit.")
    return deepcopy(recipe), raw


def make_recipe(name, plan, *, include_prompts=False, include_model=True,
                include_sampling=True, include_finishing=True, include_source_stages=True):
    """Capture only deliberately selected reusable fields from a neutral plan."""
    if not isinstance(plan, GenerationPlan):
        raise ValueError("Choose a valid generation plan.")
    request = plan.request
    selected_settings = (MODEL_SETTINGS if include_model else (),
                         SAMPLING_SETTINGS if include_sampling else ())
    settings = {key: deepcopy(getattr(request, key)) for group in selected_settings
                for key in group if getattr(request, key, None) not in (None, "")}
    if include_model and request.model_hash and not request.model:
        # A registered checkpoint has no executable generator name. Preserve
        # that deliberate absence so applying the setup clears any stale name.
        settings["model"] = ""
    labels = plan.target_context.get("resource_display_labels", ())
    labels = labels if isinstance(labels, (list, tuple)) else ()
    resources = [{"id": r.id, "role": r.role, "name": r.name,
                  "hashes": {"sha256": _hash(r.hashes.get("sha256"))},
                  "compatibility": list(r.compatibility),
                  "weights": {key: r.weights[key] for key in ("model", "text_encoder") if key in r.weights},
                  "provenance": {"swarm_parameter": r.provenance.get("swarm_parameter")}
                      if r.role == "text_encoder" else {},
                  **({"display_label": label["label"]} if (label := next((item for item in labels
                      if isinstance(item, dict) and
                      (item.get("resource_id"), item.get("role"), item.get("family")) == _resource_label_key(r.to_dict())
                      and _display_label(item.get("label"))), None)) else {})}
                 for r in request.resources] if include_model else []
    include_stages = include_source_stages and include_model and include_sampling
    stages = [{key: deepcopy(getattr(s, key)) for key in STAGE_SETTINGS if getattr(s, key, None) not in (None, "")}
              for s in request.stages] if include_stages else []
    context = plan.target_context
    finishing = {key: {field: deepcopy(value[field]) for field in allowed if field in value}
                 for key, allowed in (("output_goal", GOAL_KEYS), ("output_plan", PLAN_KEYS))
                 if isinstance(value := context.get(key), dict)} if include_finishing else {}
    recipe = {"schema_version": 2, "name": name.strip() if isinstance(name, str) else name,
              "settings": settings}
    if include_model: recipe["resources"] = resources
    if include_stages: recipe["stages"] = stages
    if include_finishing: recipe["finishing"] = finishing
    if include_stages and any(s.inputs for s in request.stages):
        recipe["source_binding"] = _binding(plan.source_identity)
        if request.denoising_strength is not None:
            recipe["source_settings"] = {"denoising_strength": request.denoising_strength}
    if include_prompts:
        recipe["prompts"] = {"positive_prompt": request.positive_prompt, "negative_prompt": request.negative_prompt}
    return _validate(recipe)[0]


def make_manual_recipe(name, prompt_parts, *, negative_prompt=None, settings=None,
                       resources=(), finishing=None):
    """Create a source-free setup from explicitly authored choices only."""
    if not isinstance(prompt_parts, (list, tuple)):
        raise ValueError("Choose up to 32 structured prompt parts.")
    recipe = {"schema_version": 3,
              "name": name.strip() if isinstance(name, str) else name,
              "settings": deepcopy(settings if settings is not None else {}),
              "prompt_parts": deepcopy(list(prompt_parts))}
    if negative_prompt is not None:
        recipe["negative_prompt"] = negative_prompt
    if resources:
        recipe["resources"] = deepcopy(list(resources))
        recipe["resource_policy"] = "merge_selected"
    if finishing is not None:
        recipe["finishing"] = deepcopy(finishing)
    if not (recipe["prompt_parts"] or negative_prompt is not None or recipe["settings"]
            or resources or finishing):
        raise ValueError("Choose prompt text, an exact resource, or a setting to save.")
    return _validate(recipe)[0]


def make_image_inspired_recipe(name, record, *, selected_text=(),
                               include_recorded_prompt=False, include_recorded_negative=False,
                               include_model=True, include_sampling=True,
                               selected_lora_indices=None, lora_weight_overrides=None,
                               selected_parts=(), selected_recorded_resource_indices=None,
                               authored_resources=None):
    """Propose reusable settings from recorded evidence, never source pixels or guesses.

    The caller chooses every text fragment. Missing or conflicting resource
    identity is held for a later manual choice in Generate, not inferred from a
    filename, a LoRA mention, or visual appearance.

    Explicit recorded indices and/or authored portable resources opt into a
    structured, selectively merged setup. None preserves the legacy recorded
    selection; an empty sequence deliberately selects no recorded components.
    Authored resources are independent of the include-recorded-model switch.
    """
    if record is not None and not isinstance(record, GenerationRecord):
        raise ValueError("Choose a recorded generation or a text-only setup.")
    custom_resources = selected_recorded_resource_indices is not None or authored_resources is not None
    if selected_recorded_resource_indices is not None:
        indices = selected_recorded_resource_indices
        if (not isinstance(indices, (list, tuple)) or len(indices) > 32 or
                any(type(index) is not int or record is None or index < 0
                    or index >= len(record.resources)
                    or record.resources[index].role not in {"checkpoint", "lora", "vae"}
                    for index in indices)):
            raise ValueError("Choose valid individual recorded checkpoints, LoRAs or VAEs.")
        if len(set(indices)) != len(indices):
            raise ValueError("Choose each recorded component only once.")
        if indices and not include_model:
            raise ValueError("Choose recorded components only when including recorded model components.")
    if authored_resources is not None and (
            not isinstance(authored_resources, (list, tuple)) or len(authored_resources) > 32):
        raise ValueError("Choose up to 32 authored exact model components.")
    if selected_lora_indices is not None:
        if not include_model or record is None or not isinstance(selected_lora_indices, (list, tuple)):
            raise ValueError("Choose recorded LoRAs only when including recorded model components.")
        if (len(selected_lora_indices) > 32 or
                any(type(index) is not int or index < 0 or index >= len(record.resources)
                    or record.resources[index].role != "lora" for index in selected_lora_indices)):
            raise ValueError("Choose valid individual recorded LoRAs.")
        if len(set(selected_lora_indices)) != len(selected_lora_indices):
            raise ValueError("Choose each recorded LoRA only once.")
        if selected_recorded_resource_indices is not None and set(selected_lora_indices) != {
                index for index in selected_recorded_resource_indices if record.resources[index].role == "lora"}:
            raise ValueError("Recorded component and LoRA selections disagree; review the selected components.")
    if lora_weight_overrides is None:
        lora_weight_overrides = {}
    if not isinstance(lora_weight_overrides, dict) or len(lora_weight_overrides) > 32:
        raise ValueError("Choose valid recorded LoRA strength changes.")
    selected_loras_for_overrides = ({index for index in selected_recorded_resource_indices
                                    if record.resources[index].role == "lora"}
                                   if selected_recorded_resource_indices is not None
                                   else set(selected_lora_indices or ()))
    for index, weights in lora_weight_overrides.items():
        if (type(index) is not int or index not in selected_loras_for_overrides
                or record is None or not include_model
                or not isinstance(weights, dict) or not weights
                or set(weights) - {"model", "text_encoder"}):
            raise ValueError("Choose strength changes only for selected recorded LoRAs.")
        resource = record.resources[index]
        try:
            _hash(resource.hashes.get("sha256"))
        except ValueError as exc:
            raise ValueError("Choose an exact recorded LoRA before changing its strength.") from exc
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not -10 <= value <= 10 for value in weights.values()):
            raise ValueError("LoRA strength changes must be finite values from -10 to 10.")
    if not isinstance(selected_text, (list, tuple)) or len(selected_text) > 200:
        raise ValueError("Choose up to 200 saved text fragments.")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 4096
           for value in selected_text):
        raise ValueError("Choose nonempty saved text fragments.")
    if not isinstance(selected_parts, (list, tuple)):
        raise ValueError("Choose up to 32 edited prompt parts.")
    # These are user-selected/edited prompt instructions, not Image Knowledge.
    render_prompt_parts(selected_parts)
    notes = []
    source = GenerationRecord.from_dict(record.to_dict()) if record is not None else GenerationRecord()
    if record is None and (include_model or include_sampling or include_recorded_prompt
                           or include_recorded_negative):
        raise ValueError("No generation settings were recorded for this image. Choose saved text only.")
    chosen_resources = []
    if custom_resources:
        # Explicitly chosen rows must be exact; never silently omit one or
        # reconstruct a checkpoint from a legacy model name/hash declaration.
        if include_model and record is not None:
            indices = selected_recorded_resource_indices
            if indices is None:
                indices = []
                for index, resource in enumerate(source.resources):
                    if resource.role not in {"checkpoint", "lora", "vae"}:
                        notes.append("Other recorded component roles are not supported by structured setups; add them in Generate.")
                        continue
                    if (resource.role == "lora" and selected_lora_indices is not None
                            and index not in selected_lora_indices):
                        continue
                    try:
                        _hash(resource.hashes.get("sha256"))
                    except ValueError:
                        notes.append("A component without an exact SHA-256 identity was not copied.")
                        continue
                    indices.append(index)
            for index in indices:
                resource = source.resources[index]
                if resource.role == "lora" and index in lora_weight_overrides:
                    resource.weights.update(lora_weight_overrides[index])
                chosen_resources.append(portable_recipe_resource(resource))
        chosen_resources.extend(deepcopy(list(authored_resources or ())))
        # The selected exact rows below supply all model authority. In
        # particular, omitted source checkpoint/VAE names cannot enter settings.
        source.resources = []
        source.model = source.model_hash = source.vae = ""
    elif include_model and record is not None:
        exact_resources = []
        selected_loras = set(selected_lora_indices) if selected_lora_indices is not None else None
        for index, resource in enumerate(source.resources):
            if resource.role not in {"checkpoint", "lora", "vae", "text_encoder", "embedding"}:
                notes.append("A source-bound or unsupported component was not copied.")
                continue
            try:
                _hash(resource.hashes.get("sha256"))
            except ValueError:
                notes.append("A component without an exact SHA-256 identity was not copied.")
                continue
            if resource.role == "lora" and selected_loras is not None and index not in selected_loras:
                continue
            if resource.role == "lora" and index in lora_weight_overrides:
                resource.weights.update(lora_weight_overrides[index])
            exact_resources.append(resource)
        source.resources = exact_resources
        checkpoint = [item for item in exact_resources if item.role == "checkpoint"]
        if len(checkpoint) > 1:
            raise ValueError("Several exact checkpoints were recorded. Choose or correct the model manually in Generate.")
        if source.model_hash:
            try:
                source.model_hash = _hash(source.model_hash)
            except ValueError:
                notes.append("The recorded model hash is incomplete; its name is not an exact model choice.")
                source.model_hash = ""
        if checkpoint:
            exact = _hash(checkpoint[0].hashes["sha256"])
            if source.model_hash and source.model_hash != exact:
                raise ValueError("The recorded checkpoint and model hash disagree. Choose the model manually in Generate.")
            source.model_hash = exact
        if source.model:
            try:
                _name(source.model)
            except ValueError:
                source.model = ""
                notes.append("A path-like recorded model name was not copied.")
        if not source.model_hash:
            source.model = ""
            notes.append("No exact recorded main-model identity is available; choose one in Generate.")
        elif not source.model and not checkpoint:
            source.model_hash = ""
            notes.append("The exact model hash has no registered checkpoint declaration; choose one in Generate.")
        if source.loras:
            notes.append("LoRA names without exact resource declarations were not copied; add them manually in Generate.")
    source.stages = []
    source.denoising_strength = None
    source.loras = []
    selected = [value.strip() for value in selected_text]
    prompts = []
    if include_recorded_prompt and record is not None and record.positive_prompt.strip():
        prompts.append(record.positive_prompt.strip())
    prompts.extend(selected)
    positive = ", ".join(prompts)
    negative = record.negative_prompt if include_recorded_negative and record is not None else ""
    if len(positive) > 16384 or len(negative) > 16384:
        raise ValueError("Selected prompt text exceeds the setup limit.")
    if not (positive or negative or selected_parts or chosen_resources or
            (record is not None and (include_model or include_sampling))):
        raise ValueError("Choose recorded settings or saved text to reuse.")
    source.positive_prompt = positive
    source.negative_prompt = negative
    plan = GenerationPlan.create(source if record is not None else None)
    if record is None:
        plan = plan.revise({"positive_prompt": positive, "negative_prompt": negative},
                           source="selected_image_text", consequence="User selected saved image text")
    recipe = make_recipe(name, plan, include_prompts=bool(positive or negative),
                         include_model=include_model and not custom_resources, include_sampling=include_sampling,
                         include_finishing=False, include_source_stages=False)
    if selected_parts or custom_resources:
        # Version 2 remains the exact compatible path for existing image setups.
        # Edited, purpose-labelled parts use the canonical version-3 structure;
        # the source record and its evidence never become recipe authority.
        structured = [{"kind": "other", "text": value}
                      for value in ([record.positive_prompt.strip()]
                                    if include_recorded_prompt and record is not None
                                    and record.positive_prompt.strip() else [])]
        structured.extend({"kind": "other", "text": value} for value in selected)
        structured.extend(deepcopy(list(selected_parts)))
        if any(len(part["text"]) > 4096 for part in structured):
            raise ValueError("A recorded prompt is too long for one structured part. Uncheck Recorded positive prompt or save those choices separately.")
        supported = chosen_resources if custom_resources else [row for row in recipe.get("resources", [])
                     if row["role"] in {"checkpoint", "lora", "vae"}]
        if not custom_resources and len(supported) != len(recipe.get("resources", [])):
            notes.append("Other recorded component roles are not supported by structured setups; add them in Generate.")
        recipe = {"schema_version": 3, "name": recipe["name"],
                  "settings": recipe["settings"], "prompt_parts": structured}
        if custom_resources:
            # Validate portable row structure before inspecting its fields.
            candidate = {**recipe, "resources": supported, "resource_policy": "merge_selected"}
            checkpoints = [row for row in supported if isinstance(row, dict) and row.get("role") == "checkpoint"]
            if len(checkpoints) > 1:
                raise ValueError("Choose only one exact checkpoint for this setup.")
            if checkpoints:
                hashes = checkpoints[0].get("hashes")
                digest = _hash(hashes.get("sha256") if isinstance(hashes, dict) else None)
                candidate["settings"].update(model="", model_hash=digest)
            _validate(candidate)
            identities, hashes, single_roles = set(), set(), set()
            for row in supported:
                digest = _hash(row["hashes"]["sha256"]).removeprefix("sha256:")
                if row["id"] in identities or (row["role"], digest) in hashes:
                    raise ValueError("Choose each exact component only once; recorded and authored choices overlap.")
                identities.add(row["id"]); hashes.add((row["role"], digest))
                if row["id"].startswith("sha256:") and row["id"].lower() != "sha256:" + digest:
                    raise ValueError("The selected component ID and exact SHA-256 hash disagree.")
                if row["role"] in {"checkpoint", "vae"}:
                    if row["role"] in single_roles:
                        raise ValueError("Choose only one exact checkpoint and one VAE for this setup.")
                    single_roles.add(row["role"])
                if (row["role"] != "lora" and row["weights"] or
                        any(not -10 <= value <= 10 for value in row["weights"].values())):
                    raise ValueError("Only LoRAs support strength weights, from -10 to 10.")
            if "vae" in single_roles:
                recipe["settings"]["vae"] = ""
            recipe["resource_policy"] = "merge_selected"
            if not (structured or negative or supported or recipe["settings"]):
                raise ValueError("Choose prompt text, an exact resource, or a setting to save.")
        if negative:
            recipe["negative_prompt"] = negative
        if supported:
            recipe["resources"] = supported
            recipe["resource_policy"] = "merge_selected"
        recipe = _validate(recipe)[0]
    return recipe, tuple(dict.fromkeys(notes))


def preview_recipe(recipe, current, *, include_prompts=False):
    recipe, raw = _validate(recipe)
    if not isinstance(current, GenerationPlan):
        raise ValueError("Choose a current generation plan.")
    request = current.request.to_dict()
    changes = deepcopy(recipe["settings"])
    recipe_resources = deepcopy(recipe.get("resources", []))
    if "resources" in recipe:
        selected = [{key: value for key, value in row.items() if key != "display_label"}
                    for row in recipe_resources]
        if recipe["schema_version"] == 3:
            merged = deepcopy(request["resources"])
            for row in selected:
                digest = row["hashes"]["sha256"].removeprefix("sha256:")
                merged = [item for item in merged if not (
                    (row["role"] in {"checkpoint", "vae"} and item["role"] == row["role"]) or
                    (row["role"] == "lora" and item["role"] == "lora" and
                     (item["id"] == row["id"] or
                      item.get("hashes", {}).get("sha256", "").removeprefix("sha256:") == digest)))]
                merged.append(row)
            changes["resources"] = merged
        else:
            changes["resources"] = selected
    if include_prompts:
        if recipe["schema_version"] == 3:
            if recipe.get("prompt_parts"):
                changes["positive_prompt"] = render_prompt_parts(recipe["prompt_parts"])
            if "negative_prompt" in recipe:
                changes["negative_prompt"] = recipe["negative_prompt"]
        elif "prompts" in recipe:
            changes.update(deepcopy(recipe["prompts"]))
    notes = []
    if recipe["schema_version"] == 3 and recipe_resources:
        notes.append("Only the selected exact components are updated. Other request components remain; review their compatibility and readiness before generation.")
    source_bound = bool(recipe.get("source_binding"))
    try:
        current_binding = _binding(current.source_identity) if source_bound else []
    except ValueError:
        current_binding = []
    same_source = source_bound and recipe["source_binding"] == current_binding
    if source_bound and not same_source:
        notes.append("This recipe's source-bound stages belong to another source. Current masks and references are kept; review them separately.")
    if same_source:
        changes.update(deepcopy(recipe.get("source_settings", {})))
    stages = deepcopy(request["stages"])
    templates = recipe.get("stages", [])
    if templates and len(stages) == len(templates) and all(
            stage["kind"] == template["kind"] for stage, template in zip(stages, templates)):
        for stage, template in zip(stages, templates):
            stage.update({key: deepcopy(value) for key, value in template.items()
                          if key != "kind" and (key != "denoising_strength" or not source_bound or same_source)})
    elif templates and not source_bound and not stages:
        stages = [GenerationStage(**template).to_dict() for template in templates]
    after = {**request, **changes}
    for stage in stages:
        for key in ("positive_prompt", "negative_prompt", "width", "height", "steps", "sampler", "scheduler", "cfg_scale"):
            if stage.get(key) not in (None, ""):
                stage[key] = after.get(key)
        if stage.get("seeds"):
            stage["seeds"] = [request["seed"]] if request.get("seed") is not None else []
        if same_source and "denoising_strength" in changes:
            if stage.get("denoising_strength") is not None:
                stage["denoising_strength"] = changes["denoising_strength"]
            for item in stage.get("inputs", []):
                if item.get("kind") == "init_image":
                    item["strength"] = changes["denoising_strength"]
    if stages: changes["stages"] = stages
    context = current.target_context
    if "finishing" in recipe:
        context.pop("output_goal", None); context.pop("output_plan", None)
        context.update(deepcopy(recipe["finishing"]))
    if "resources" in recipe:
        retained = context.get("resource_display_labels", ())
        retained = retained if isinstance(retained, (list, tuple)) else ()
        keys = {_resource_label_key(row) for row in changes["resources"]}
        labels = [item for item in retained if isinstance(item, dict)
                  and (item.get("resource_id"), item.get("role"), item.get("family")) in keys
                  and _display_label(item.get("label"))]
        for row in recipe_resources:
            if "display_label" not in row:
                continue
            key = _resource_label_key(row)
            labels = [item for item in labels if
                      (item["resource_id"], item["role"], item["family"]) != key]
            labels.append({"resource_id": key[0], "role": key[1], "family": key[2],
                           "label": row["display_label"]})
        if labels:
            context["resource_display_labels"] = labels
        else:
            context.pop("resource_display_labels", None)
    context["recipe"] = {"name": recipe["name"], "recipe_sha256": hashlib.sha256(raw).hexdigest()}
    changes["target_context"] = context
    candidate = current.revise(changes, source="generation_recipe", consequence="Apply reviewed reusable setup")
    delta = [{"setting": key, "before": request.get(key), "after": value}
             for key, value in changes.items() if key != "target_context" and request.get(key) != value]
    if any(context.get(key) != current.target_context.get(key) for key in ("output_goal", "output_plan")):
        delta.append({"setting": "Finishing", "before": current.target_context.get("output_plan"),
                      "after": context.get("output_plan")})
    regional = context.get("regional_prompt")
    if isinstance(regional, dict) and isinstance(regional.get("regions"), list) and regional["regions"]:
        count = len(regional["regions"])
        saved_canvas = regional.get("base_canvas")
        saved_size = ((saved_canvas.get("width"), saved_canvas.get("height"))
                      if isinstance(saved_canvas, dict) else (None, None))
        current_size = (candidate.request.width, candidate.request.height)
        saved_label = (f"{saved_size[0]} × {saved_size[1]}" if all(type(v) is int for v in saved_size)
                       else "an invalid saved canvas")
        current_label = (f"{current_size[0]} × {current_size[1]}"
                         if all(type(v) is int for v in current_size) else "not selected")
        delta.append({"setting": "Regional prompts",
                      "before": f"{count} saved rectangle(s) on {saved_label}",
                      "after": f"retained on {saved_label}; new base {current_label}"})
        if saved_size != current_size:
            notes.append("The saved regional rectangles stay attached to their old canvas. "
                         "Before generation, open Review Regional Prompts and explicitly use the rectangles "
                         "on the current canvas or remove all regions. No position is moved automatically.")
        elif candidate.request.positive_prompt != current.request.positive_prompt:
            notes.append("The global prompt changes, but the saved regional text and rectangles stay as they were. "
                         "Review Regional Prompts before preparing this combination.")
        else:
            notes.append("Existing regional text and rectangles stay attached; this setup does not replace them. "
                         "Review Regional Prompts to change or remove them.")
    return {"plan": candidate, "changes": delta, "notes": notes}


def save_recipe(path, recipe):
    _checked, raw = _validate(recipe)
    path = Path(path).absolute(); _assert_unredirected(path)
    if path.exists():
        raise ValueError("Choose a new recipe filename to preserve the existing recipe.")
    with path.open("xb") as stream:
        stream.write(raw); stream.flush()
        import os
        os.fsync(stream.fileno())
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(raw).digest():
        raise ValueError("Recipe read-back verification failed.")


def recipe_storage_digest(recipe):
    """Digest the exact canonical bytes written by save_recipe."""
    return hashlib.sha256(_validate(recipe)[1]).hexdigest()


def load_recipe(path):
    path = Path(path).absolute(); _assert_unredirected(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Recipe exceeds its size limit.")
    return _validate(json.loads(path.read_bytes()))[0]
