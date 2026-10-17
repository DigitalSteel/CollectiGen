from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable

from .annotation import (
    GenerationInput,
    GenerationRecord,
    GenerationResource,
    GenerationSettingResult,
    GenerationStage,
)


_LORA_PATTERN = re.compile(r"<lora:([^:>]+)(?::([^>]+))?>", re.IGNORECASE)
_CONTROL_MODEL_PATTERN = re.compile(r"(?:model\s*[:=]\s*)?([^,;]+?\.(?:safetensors|pth|pt))", re.IGNORECASE)
_COMFY_RESOURCE_CLASSES = {
    "checkpointloadersimple": ("checkpoint", "ckpt_name"),
    "unetloader": ("unet", "unet_name"),
    "cliploader": ("text_encoder", "clip_name"),
    "dualcliploader": ("text_encoder", "clip_name1"),
    "vaeloader": ("vae", "vae_name"),
    "loraloader": ("lora", "lora_name"),
    "loraloadermodelonly": ("lora", "lora_name"),
    "controlnetloader": ("controlnet", "control_net_name"),
    "upscalemodelloader": ("upscaler", "model_name"),
}
_TARGET_CAPABILITIES = {
    "automatic1111": {
        "resources": {"checkpoint", "vae", "lora", "embedding", "controlnet", "upscaler", "extension"},
        "stages": {"first_pass", "hires", "refiner", "control", "post_process"},
        "inputs": {"init_image", "mask", "control_image"},
    },
    "forge": {
        "resources": {"checkpoint", "unet", "text_encoder", "vae", "lora", "embedding", "controlnet", "upscaler", "extension"},
        "stages": {"first_pass", "hires", "refiner", "control", "post_process"},
        "inputs": {"init_image", "mask", "control_image"},
    },
    "sdnext": {
        "resources": {"checkpoint", "unet", "text_encoder", "vae", "lora", "embedding", "controlnet", "upscaler", "extension"},
        "stages": {"first_pass", "hires", "refiner", "control", "post_process"},
        "inputs": {"init_image", "mask", "control_image"},
    },
    "swarmui": {
        "resources": {"checkpoint", "unet", "text_encoder", "vae", "lora", "embedding", "controlnet", "upscaler", "extension"},
        "stages": {"first_pass", "hires", "refiner", "control", "post_process"},
        "inputs": {"init_image", "mask", "control_image", "reference_image"},
    },
    "comfyui": {
        "resources": {"checkpoint", "unet", "text_encoder", "vae", "lora", "embedding", "controlnet", "upscaler", "extension"},
        "stages": {"first_pass", "hires", "refiner", "control", "post_process"},
        "inputs": {"init_image", "mask", "control_image"},
    },
}


@dataclass
class GenerationTranslation:
    """A target-neutral payload plus an explicit preview of translation loss."""

    target_format: str
    settings: dict[str, Any] = field(default_factory=dict)
    report: list[GenerationSettingResult] = field(default_factory=list)

    @property
    def lossy(self) -> bool:
        return any(item.status in {"approximated", "untranslated", "unsupported"} for item in self.report)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_format": self.target_format,
            "settings": dict(self.settings),
            "lossy": self.lossy,
            "report": [item.to_dict() for item in self.report],
        }


def _number(value: Any) -> int | float | str:
    if isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    try:
        number = float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return text


def _optional_int(value: Any) -> int | None:
    parsed = _number(value)
    return parsed if isinstance(parsed, int) else None


def _optional_float(value: Any) -> float | None:
    parsed = _number(value)
    return float(parsed) if isinstance(parsed, (int, float)) else None


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                result.update(_flatten(item, name))
            else:
                result[name] = item
    return result


def _normalized_key(value: str) -> str:
    return value.casefold().replace("_", " ").strip()


def _find(flat: dict[str, Any], *names: str) -> Any:
    normalized = {_normalized_key(key): value for key, value in flat.items()}
    for name in names:
        wanted = _normalized_key(name)
        for key, value in normalized.items():
            if key == wanted or key.endswith(f".{wanted}"):
                return value
    return None


def _parse_setting_pairs(text: str) -> dict[str, str]:
    """Split infotext settings without treating quoted commas as boundaries."""
    parts: list[str] = []
    start = 0
    quote = ""
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            following = text[index + 1 :]
            if re.match(r"\s*[^,:]+:\s*", following):
                parts.append(text[start:index].strip())
                start = index + 1
    parts.append(text[start:].strip())
    result: dict[str, str] = {}
    for part in parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def _report(
    record: GenerationRecord,
    source_path: str,
    neutral_path: str,
    status: str,
    value: Any,
    detail: str = "",
) -> None:
    record.adapter_report.append(
        GenerationSettingResult(
            source_path=source_path,
            neutral_path=neutral_path,
            status=status,
            value=value,
            detail=detail,
        )
    )


def _mark_mapped(
    record: GenerationRecord,
    source_path: str,
    neutral_path: str,
    *,
    status: str = "translated",
    detail: str = "",
) -> None:
    """Replace an initial unknown-setting result after a specialized adapter maps it."""
    for item in record.adapter_report:
        if item.source_path == source_path:
            item.neutral_path = neutral_path
            item.status = status
            item.detail = detail
            return


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:48] or "unnamed"


def _add_resource(
    record: GenerationRecord,
    role: str,
    name: Any,
    *,
    hashes: dict[str, Any] | None = None,
    weights: dict[str, Any] | None = None,
    source_id: Any = "",
    version_id: Any = "",
    compatibility: Iterable[Any] = (),
    trigger_words: Iterable[Any] = (),
    provenance: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> GenerationResource | None:
    display = str(name or "").strip()
    if not display:
        return None
    for existing in record.resources:
        if existing.role == role and existing.name.casefold() == display.casefold():
            if hashes:
                existing.hashes.update({str(key): str(value) for key, value in hashes.items() if value not in (None, "")})
            if weights:
                existing.weights.update({str(key): value for key, value in weights.items() if value not in (None, "")})
            return existing
    base = f"res_{role}_{_slug(display)}"
    used = {item.id for item in record.resources}
    resource_id = base
    suffix = 2
    while resource_id in used:
        resource_id = f"{base}_{suffix}"
        suffix += 1
    resource = GenerationResource(
        id=resource_id,
        role=role,
        name=display,
        hashes={str(key): str(value) for key, value in (hashes or {}).items() if value not in (None, "")},
        source_id=str(source_id or ""),
        version_id=str(version_id or ""),
        compatibility=[str(item) for item in compatibility if item not in (None, "")],
        weights={str(key): value for key, value in (weights or {}).items() if value not in (None, "")},
        trigger_words=[str(item) for item in trigger_words if item not in (None, "")],
        provenance=dict(provenance or {}),
        extras=dict(extras or {}),
    )
    record.resources.append(resource)
    return resource


def _parse_named_hashes(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    result: dict[str, str] = {}
    for part in str(value or "").strip('"').split(","):
        if ":" in part:
            name, item_hash = part.split(":", 1)
            if name.strip() and item_hash.strip():
                result[name.strip()] = item_hash.strip()
    return result


def _loras(prompt: str) -> list[dict[str, Any]]:
    return [
        {"name": match.group(1), "weight": _number(match.group(2) if match.group(2) else 1)}
        for match in _LORA_PATTERN.finditer(prompt)
    ]


def _apply_settings(
    record: GenerationRecord,
    settings: dict[str, Any],
    *,
    source_prefix: str = "settings",
) -> None:
    mapping = {
        "steps": ("steps", lambda value: _optional_int(value)),
        "sampler": ("sampler", lambda value: str(value)),
        "sampler name": ("sampler", lambda value: str(value)),
        "schedule type": ("scheduler", lambda value: str(value)),
        "scheduler": ("scheduler", lambda value: str(value)),
        "schedule": ("scheduler", lambda value: str(value)),
        "cfg scale": ("cfg_scale", lambda value: _optional_float(value)),
        "cfg": ("cfg_scale", lambda value: _optional_float(value)),
        "cfgscale": ("cfg_scale", lambda value: _optional_float(value)),
        "seed": ("seed", _number),
        "width": ("width", _optional_int),
        "height": ("height", _optional_int),
        "model": ("model", lambda value: str(value)),
        "model hash": ("model_hash", lambda value: str(value)),
        "modelhash": ("model_hash", lambda value: str(value)),
        "vae": ("vae", lambda value: str(value)),
        "vae name": ("vae", lambda value: str(value)),
        "denoising strength": ("denoising_strength", _optional_float),
        "denoise": ("denoising_strength", _optional_float),
    }
    for original, value in settings.items():
        key = _normalized_key(original)
        path = f"{source_prefix}.{original}" if source_prefix else str(original)
        if key in {"version", "software", "app"}:
            neutral = "generator_version" if key == "version" else "generator_name"
            _report(record, path, neutral, "translated", value)
            continue
        if key == "size":
            match = re.match(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", str(value))
            if match:
                record.width, record.height = int(match.group(1)), int(match.group(2))
                _report(record, path, "first_pass.size", "translated", value)
                continue
        if key in mapping:
            field_name, converter = mapping[key]
            parsed = converter(value)
            if parsed is not None:
                setattr(record, field_name, parsed)
                status = "supported" if key.replace(" ", "_") == field_name else "translated"
                _report(record, path, field_name, status, value)
            else:
                record.extras[original] = value
                _report(record, path, "", "untranslated", value, "Value could not be normalized.")
        else:
            record.extras[original] = value
            _report(record, path, "", "untranslated", value, "Retained as a generator-specific extra.")


def _sync_first_stage(record: GenerationRecord, *, batch_index: Any = None, batch_size: Any = None) -> GenerationStage:
    seeds = [record.seed] if record.seed not in (None, "") else []
    stage = GenerationStage(
        id="stage_first_pass",
        kind="first_pass",
        label="First pass",
        positive_prompt=record.positive_prompt,
        negative_prompt=record.negative_prompt,
        seeds=seeds,
        batch_index=_optional_int(batch_index),
        batch_size=_optional_int(batch_size),
        width=record.width,
        height=record.height,
        steps=record.steps,
        cfg_scale=record.cfg_scale,
        sampler=record.sampler,
        scheduler=record.scheduler,
        denoising_strength=record.denoising_strength,
        resource_ids=[item.id for item in record.resources if item.role in {"checkpoint", "unet", "text_encoder", "vae", "lora", "embedding"}],
    )
    record.stages.insert(0, stage)
    return stage


def _parse_infotext(record: GenerationRecord, text: str) -> None:
    settings_start = None
    for match in re.finditer(r"(?:^|\n)(Steps|Sampler|Seed|CFG scale):\s*", text, re.IGNORECASE):
        settings_start = match.start() + (1 if text[match.start():].startswith("\n") else 0)
        break
    prompt_block = text[:settings_start].strip() if settings_start is not None else ""
    settings_block = text[settings_start:].strip() if settings_start is not None else text
    marker = re.search(r"\n?Negative prompt:\s*", prompt_block, re.IGNORECASE)
    if marker:
        record.positive_prompt = prompt_block[: marker.start()].strip()
        record.negative_prompt = prompt_block[marker.end() :].strip()
    else:
        record.positive_prompt = prompt_block
    settings = _parse_setting_pairs(settings_block)
    identity = str(settings.get("Version") or settings.get("Software") or settings.get("App") or "")
    software = identity.casefold()
    if "sd.next" in software or "sdnext" in software:
        record.format = "sdnext"
        record.generator_name = "SD.Next"
    elif "forge" in software:
        record.format = "forge"
        record.generator_name = "Forge"
    else:
        record.format = "automatic1111"
        record.generator_name = "Automatic1111"
    if identity:
        record.generator_version = re.sub(
            r"^(?:stable diffusion webui\s*)?(?:forge|sd\.next|sdnext|automatic1111|a1111)\s*",
            "",
            identity,
            flags=re.IGNORECASE,
        ).strip()
    _apply_settings(record, settings)

    lora_hashes = _parse_named_hashes(settings.get("Lora hashes", settings.get("LoRA hashes", "")))
    embedding_hashes = _parse_named_hashes(settings.get("TI hashes", settings.get("Embedding hashes", "")))
    record.loras = _loras(record.positive_prompt)
    checkpoint = _add_resource(
        record,
        "checkpoint",
        record.model,
        hashes={"model_hash": record.model_hash},
        source_id=settings.get("Model ID", ""),
        version_id=settings.get("Model version ID", ""),
        compatibility=[settings.get("Base model", "")] if settings.get("Base model") else [],
        provenance={"generator": record.format, "source_path": "settings.Model"},
    )
    vae = _add_resource(
        record,
        "vae",
        record.vae,
        hashes={"sha256": settings.get("VAE hash", "")},
        provenance={"generator": record.format, "source_path": "settings.VAE"},
    )
    for item in record.loras:
        name = str(item.get("name") or "")
        _add_resource(
            record,
            "lora",
            name,
            hashes={"sha256": lora_hashes.get(name, "")},
            weights={"model": item.get("weight", 1), "clip": item.get("weight", 1)},
            provenance={"generator": record.format, "source_path": "positive_prompt"},
        )
    for name, item_hash in embedding_hashes.items():
        _add_resource(
            record,
            "embedding",
            name,
            hashes={"sha256": item_hash},
            provenance={"generator": record.format, "source_path": "settings.TI hashes"},
        )
    specialized_paths = {
        "Model ID": "resources.checkpoint.source_id",
        "Model version ID": "resources.checkpoint.version_id",
        "Base model": "resources.checkpoint.compatibility",
        "VAE hash": "resources.vae.hashes",
        "Lora hashes": "resources.lora.hashes",
        "LoRA hashes": "resources.lora.hashes",
        "TI hashes": "resources.embedding.hashes",
        "Embedding hashes": "resources.embedding.hashes",
    }
    for setting_name, neutral_path in specialized_paths.items():
        if setting_name in settings:
            _mark_mapped(record, f"settings.{setting_name}", neutral_path)

    first = _sync_first_stage(
        record,
        batch_index=settings.get("Batch pos", settings.get("Batch index")),
        batch_size=settings.get("Batch size"),
    )
    for setting_name, kind in (("Init image", "init_image"), ("Mask", "mask")):
        if settings.get(setting_name):
            first.inputs.append(
                GenerationInput(
                    kind=kind,
                    reference=str(settings[setting_name]),
                    extras={"source_setting": setting_name},
                )
            )
            _mark_mapped(
                record,
                f"settings.{setting_name}",
                f"stages.first_pass.inputs.{kind}",
            )
    for resource in (checkpoint, vae):
        if resource and resource.id not in first.resource_ids:
            first.resource_ids.append(resource.id)

    extra_seeds = [
        _number(settings[name])
        for name in ("Variation seed", "Subseed")
        if settings.get(name) not in (None, "", "-1")
    ]
    first.seeds.extend(item for item in extra_seeds if item not in first.seeds)
    for setting_name in ("Variation seed", "Subseed"):
        if setting_name in settings:
            _mark_mapped(record, f"settings.{setting_name}", "stages.first_pass.seeds")
    for setting_name, neutral_path in (
        ("Batch pos", "stages.first_pass.batch_index"),
        ("Batch index", "stages.first_pass.batch_index"),
        ("Batch size", "stages.first_pass.batch_size"),
    ):
        if setting_name in settings:
            _mark_mapped(record, f"settings.{setting_name}", neutral_path)

    hires_enabled = any(
        settings.get(name) not in (None, "", "None", "0", 0)
        for name in ("Hires upscale", "Hires upscaler", "Hires steps", "Hires resize")
    )
    if hires_enabled:
        size = str(settings.get("Hires resize") or "")
        size_match = re.match(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", size)
        upscaler = _add_resource(
            record,
            "upscaler",
            settings.get("Hires upscaler", "Latent"),
            provenance={"generator": record.format, "source_path": "settings.Hires upscaler"},
        )
        hires_checkpoint = _add_resource(
            record,
            "checkpoint",
            settings.get("Hires checkpoint", ""),
            provenance={"generator": record.format, "source_path": "settings.Hires checkpoint"},
        )
        record.stages.append(
            GenerationStage(
                id="stage_hires",
                kind="hires",
                label="Hires pass",
                seeds=list(first.seeds),
                width=int(size_match.group(1)) if size_match else None,
                height=int(size_match.group(2)) if size_match else None,
                steps=_optional_int(settings.get("Hires steps")),
                sampler=str(settings.get("Hires sampler") or record.sampler),
                scheduler=str(settings.get("Hires scheduler") or record.scheduler),
                denoising_strength=_optional_float(settings.get("Denoising strength")),
                resource_ids=[item.id for item in (upscaler, hires_checkpoint) if item],
                extras={"scale": _number(settings["Hires upscale"])} if settings.get("Hires upscale") else {},
            )
        )
        for setting_name in (
            "Hires upscale", "Hires upscaler", "Hires steps", "Hires resize",
            "Hires checkpoint", "Hires sampler", "Hires scheduler",
        ):
            if setting_name in settings:
                _mark_mapped(record, f"settings.{setting_name}", "stages.hires")

    refiner_name = settings.get("Refiner", settings.get("Refiner model", ""))
    if refiner_name:
        refiner = _add_resource(
            record,
            "checkpoint",
            refiner_name,
            hashes={"model_hash": settings.get("Refiner hash", "")},
            provenance={"generator": record.format, "source_path": "settings.Refiner"},
        )
        record.stages.append(
            GenerationStage(
                id="stage_refiner",
                kind="refiner",
                label="Refiner",
                seeds=list(first.seeds),
                resource_ids=[refiner.id] if refiner else [],
                extras={"switch_at": _number(settings.get("Refiner switch at", ""))},
            )
        )
        for setting_name in ("Refiner", "Refiner model", "Refiner hash", "Refiner switch at"):
            if setting_name in settings:
                _mark_mapped(record, f"settings.{setting_name}", "stages.refiner")

    control_resources: list[str] = []
    control_inputs: list[GenerationInput] = []
    for key, value in settings.items():
        if not _normalized_key(key).startswith("controlnet"):
            continue
        match = _CONTROL_MODEL_PATTERN.search(str(value))
        resource = _add_resource(
            record,
            "controlnet",
            match.group(1).strip() if match else str(value),
            provenance={"generator": record.format, "source_path": f"settings.{key}"},
            extras={"raw_setting": value},
        )
        if resource:
            control_resources.append(resource.id)
        image_match = re.search(r"(?:image|input)\s*[:=]\s*([^,;]+)", str(value), re.IGNORECASE)
        if image_match:
            control_inputs.append(
                GenerationInput(kind="control_image", reference=image_match.group(1).strip(), extras={"source_setting": key})
            )
        _mark_mapped(record, f"settings.{key}", "stages.control")
    if control_resources or control_inputs:
        record.stages.append(
            GenerationStage(
                id="stage_control",
                kind="control",
                label="Control inputs",
                resource_ids=control_resources,
                inputs=control_inputs,
            )
        )
    post_extras = {
        key: value
        for key, value in settings.items()
        if _normalized_key(key).startswith(("adetailer", "detailer", "postprocess"))
    }
    if post_extras:
        detailer_name = next((value for key, value in post_extras.items() if "model" in key.casefold()), "")
        detailer = _add_resource(
            record,
            "extension",
            detailer_name or "detailer",
            provenance={"generator": record.format, "source_path": "settings.detailer"},
            extras={"extension": "detailer"},
        )
        record.stages.append(
            GenerationStage(
                id="stage_post_process",
                kind="post_process",
                label="Post-processing",
                resource_ids=[detailer.id] if detailer else [],
                extras=post_extras,
            )
        )
        for key in post_extras:
            _mark_mapped(record, f"settings.{key}", "stages.post_process")


def _parse_swarm(record: GenerationRecord, parsed: dict[str, Any]) -> None:
    record.format = "swarmui"
    record.generator_name = "SwarmUI"
    params = parsed.get("sui_image_params", parsed)
    if not isinstance(params, dict):
        params = parsed
    record.generator_version = str(params.get("swarm_version") or params.get("version") or "")
    flat = _flatten(params)
    record.positive_prompt = str(_find(flat, "prompt", "positive prompt") or "")
    record.negative_prompt = str(_find(flat, "negativeprompt", "negative prompt") or "")
    recognized = {
        "model": _find(flat, "model"),
        "modelhash": _find(flat, "modelhash", "model hash"),
        "vae": _find(flat, "vae", "vae name"),
        "sampler": _find(flat, "sampler", "sampler name"),
        "scheduler": _find(flat, "scheduler"),
        "seed": _find(flat, "seed"),
        "steps": _find(flat, "steps"),
        "cfgscale": _find(flat, "cfgscale", "cfg scale", "cfg"),
        "width": _find(flat, "width"),
        "height": _find(flat, "height"),
        "denoise": _find(flat, "denoise", "denoising strength"),
    }
    _apply_settings(record, {key: value for key, value in recognized.items() if value is not None}, source_prefix="sui_image_params")
    known_leafs = {
        "prompt", "positive prompt", "negativeprompt", "negative prompt", "model", "modelhash", "model hash",
        "vae", "vae name", "sampler", "sampler name", "scheduler", "seed", "steps", "cfgscale", "cfg scale",
        "cfg", "width", "height", "denoise", "denoising strength", "loras", "loraweights", "lorahashes",
        "embeddings", "controlnets", "upscaler", "refinermodel", "refinercontrol", "batchindex", "batchsize",
        "swarm version", "version", "modelid", "modelversionid", "base model", "vaehash", "refinerhash",
        "preset", "backend", "backend workflow",
    }
    if record.generator_version:
        _report(
            record,
            "sui_image_params.swarm_version",
            "generator_version",
            "translated",
            record.generator_version,
        )
    for path, value in flat.items():
        structured_prefix = path.casefold().startswith("lorahashes.")
        if _normalized_key(path.split(".")[-1]) not in known_leafs and not structured_prefix:
            record.extras[path] = value
            _report(record, f"sui_image_params.{path}", "", "untranslated", value, "Retained as a SwarmUI-specific extra.")

    model = _add_resource(
        record,
        "checkpoint",
        record.model,
        hashes={"sha256": params.get("modelhash", params.get("model_hash", ""))},
        source_id=params.get("modelid", ""),
        version_id=params.get("modelversionid", ""),
        compatibility=[params.get("base_model", "")] if params.get("base_model") else [],
        provenance={"generator": "swarmui", "source_path": "sui_image_params.model"},
    )
    vae = _add_resource(
        record,
        "vae",
        params.get("vae", ""),
        hashes={"sha256": params.get("vaehash", "")},
        provenance={"generator": "swarmui", "source_path": "sui_image_params.vae"},
    )
    for setting_name, neutral_path in (
        ("modelid", "resources.checkpoint.source_id"),
        ("modelversionid", "resources.checkpoint.version_id"),
        ("base_model", "resources.checkpoint.compatibility"),
        ("vaehash", "resources.vae.hashes"),
        ("lorahashes", "resources.lora.hashes"),
        ("embeddings", "resources.embedding"),
    ):
        if params.get(setting_name) not in (None, "", [], {}):
            _report(record, f"sui_image_params.{setting_name}", neutral_path, "translated", params[setting_name])
    names = params.get("loras", [])
    weights = params.get("loraweights", [])
    hashes = params.get("lorahashes", {})
    if isinstance(names, list):
        record.loras = []
        for index, name in enumerate(names):
            weight = weights[index] if isinstance(weights, list) and index < len(weights) else 1
            record.loras.append({"name": str(name), "weight": _number(weight)})
            item_hash = hashes.get(str(name), "") if isinstance(hashes, dict) else ""
            _add_resource(
                record,
                "lora",
                name,
                hashes={"sha256": item_hash},
                weights={"model": _number(weight), "clip": _number(weight)},
                provenance={"generator": "swarmui", "source_path": f"sui_image_params.loras[{index}]"},
            )
        if names:
            _report(record, "sui_image_params.loras", "resources.lora", "translated", names)
        if isinstance(weights, list) and weights:
            _report(record, "sui_image_params.loraweights", "resources.lora.weights", "translated", weights)
    embeddings = params.get("embeddings", [])
    if isinstance(embeddings, list):
        for index, item in enumerate(embeddings):
            value = item if isinstance(item, dict) else {"name": item}
            _add_resource(
                record,
                "embedding",
                value.get("name", ""),
                hashes={"sha256": value.get("hash", "")},
                trigger_words=value.get("trigger_words", []) if isinstance(value.get("trigger_words"), list) else [],
                provenance={"generator": "swarmui", "source_path": f"sui_image_params.embeddings[{index}]"},
            )

    first = _sync_first_stage(record, batch_index=params.get("batchindex"), batch_size=params.get("batchsize"))
    for setting_name, neutral_path in (
        ("batchindex", "stages.first_pass.batch_index"),
        ("batchsize", "stages.first_pass.batch_size"),
    ):
        if params.get(setting_name) not in (None, ""):
            _report(record, f"sui_image_params.{setting_name}", neutral_path, "translated", params[setting_name])
    for resource in (model, vae):
        if resource and resource.id not in first.resource_ids:
            first.resource_ids.append(resource.id)

    controls = params.get("controlnets", [])
    control_resources: list[str] = []
    control_inputs: list[GenerationInput] = []
    if isinstance(controls, list):
        for index, item in enumerate(controls):
            value = item if isinstance(item, dict) else {"model": item}
            resource = _add_resource(
                record,
                "controlnet",
                value.get("model", ""),
                hashes={"sha256": value.get("hash", "")},
                weights={"strength": value.get("strength", 1)},
                provenance={"generator": "swarmui", "source_path": f"sui_image_params.controlnets[{index}]"},
                extras={key: item for key, item in value.items() if key not in {"model", "hash", "strength", "image"}},
            )
            if resource:
                control_resources.append(resource.id)
            if value.get("image"):
                control_inputs.append(
                    GenerationInput(
                        kind="control_image",
                        reference=str(value["image"]),
                        strength=_optional_float(value.get("strength")),
                        extras={"index": index},
                    )
                )
    if control_resources or control_inputs:
        record.stages.append(
            GenerationStage(
                id="stage_control",
                kind="control",
                label="Control inputs",
                resource_ids=control_resources,
                inputs=control_inputs,
            )
        )
        _report(record, "sui_image_params.controlnets", "stages.control", "translated", controls)

    refiner_name = params.get("refinermodel", params.get("refiner_model", ""))
    if refiner_name:
        refiner = _add_resource(
            record,
            "checkpoint",
            refiner_name,
            hashes={"sha256": params.get("refinerhash", "")},
            provenance={"generator": "swarmui", "source_path": "sui_image_params.refinermodel"},
        )
        record.stages.append(
            GenerationStage(
                id="stage_refiner",
                kind="refiner",
                label="Refiner",
                seeds=list(first.seeds),
                resource_ids=[refiner.id] if refiner else [],
                extras={"control": params.get("refinercontrol")},
            )
        )
        _report(record, "sui_image_params.refinermodel", "stages.refiner", "translated", refiner_name)
        for setting_name in ("refinerhash", "refinercontrol"):
            if params.get(setting_name) not in (None, ""):
                _report(
                    record,
                    f"sui_image_params.{setting_name}",
                    "stages.refiner",
                    "translated",
                    params[setting_name],
                )

    upscaler_name = params.get("upscaler", "")
    if upscaler_name:
        upscaler = _add_resource(
            record,
            "upscaler",
            upscaler_name,
            provenance={"generator": "swarmui", "source_path": "sui_image_params.upscaler"},
        )
        record.stages.append(
            GenerationStage(
                id="stage_post_process",
                kind="post_process",
                label="Post-processing",
                resource_ids=[upscaler.id] if upscaler else [],
                extras={
                    "preset": params.get("preset"),
                    "backend": params.get("backend"),
                    "backend_workflow": params.get("backend_workflow"),
                },
            )
        )
        for setting_name in ("upscaler", "preset", "backend", "backend_workflow"):
            if params.get(setting_name) not in (None, ""):
                _report(
                    record,
                    f"sui_image_params.{setting_name}",
                    "stages.post_process",
                    "translated",
                    params[setting_name],
                )


def _workflow_node_inputs(node: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node.get("inputs"), dict):
        return dict(node["inputs"])
    node_type = _normalized_key(str(node.get("type") or "")).replace(" ", "")
    widgets = node.get("widgets_values", [])
    values = widgets if isinstance(widgets, list) else []
    if node_type == "checkpointloadersimple":
        return {"ckpt_name": values[0] if values else ""}
    if node_type in {"loraloader", "loraloadermodelonly"}:
        return {
            "lora_name": values[0] if values else "",
            "strength_model": values[1] if len(values) > 1 else None,
            "strength_clip": values[2] if len(values) > 2 else None,
        }
    if node_type == "controlnetloader":
        return {"control_net_name": values[0] if values else ""}
    if node_type == "upscalemodelloader":
        return {"model_name": values[0] if values else ""}
    if node_type == "emptylatentimage":
        return {
            "width": values[0] if values else None,
            "height": values[1] if len(values) > 1 else None,
            "batch_size": values[2] if len(values) > 2 else None,
        }
    if node_type == "cliptextencode":
        return {"text": values[0] if values else ""}
    if node_type in {"loadimage", "loadimagemask"}:
        return {"image": values[0] if values else ""}
    if node_type in {"ksampler", "ksampleradvanced"}:
        offset = 0 if node_type == "ksampler" else 2
        return {
            "seed": values[offset] if len(values) > offset else None,
            "steps": values[offset + 2] if len(values) > offset + 2 else None,
            "cfg": values[offset + 3] if len(values) > offset + 3 else None,
            "sampler_name": values[offset + 4] if len(values) > offset + 4 else "",
            "scheduler": values[offset + 5] if len(values) > offset + 5 else "",
            "denoise": values[offset + 6] if len(values) > offset + 6 else None,
        }
    return {"widgets_values": values} if values else {}


def _comfy_nodes(parsed: dict[str, Any]) -> tuple[list[tuple[str, str, dict[str, Any], bool]], dict[str, Any]]:
    candidate = parsed.get("prompt", parsed)
    if isinstance(candidate, dict) and candidate and all(isinstance(item, dict) for item in candidate.values()):
        nodes = []
        for node_id, node in candidate.items():
            node_type = str(node.get("class_type") or node.get("type") or "")
            if node_type:
                inputs = node.get("inputs", {}) if isinstance(node.get("inputs"), dict) else {}
                nodes.append((str(node_id), node_type, dict(inputs), False))
        if nodes:
            return nodes, {node_id: {"type": node_type, "inputs": inputs} for node_id, node_type, inputs, _ in nodes}
    workflow = parsed.get("workflow") if isinstance(parsed.get("workflow"), dict) else parsed
    workflow_nodes = workflow.get("nodes", [])
    nodes = []
    if isinstance(workflow_nodes, list):
        for node in workflow_nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", len(nodes)))
            node_type = str(node.get("type") or node.get("class_type") or "")
            if node_type:
                nodes.append((node_id, node_type, _workflow_node_inputs(node), True))
    return nodes, {node_id: {"type": node_type, "inputs": inputs} for node_id, node_type, inputs, _ in nodes}


def _linked_node_id(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return ""


def _upstream_resource_ids(
    node_id: str,
    node_map: dict[str, Any],
    resources_by_node: dict[str, GenerationResource],
    seen: set[str] | None = None,
) -> list[str]:
    if not node_id:
        return []
    visited = seen if seen is not None else set()
    if node_id in visited:
        return []
    visited.add(node_id)
    result: list[str] = []
    resource = resources_by_node.get(node_id)
    if resource:
        result.append(resource.id)
    node = node_map.get(node_id, {})
    inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
    if isinstance(inputs, dict):
        for value in inputs.values():
            upstream = _linked_node_id(value)
            for resource_id in _upstream_resource_ids(upstream, node_map, resources_by_node, visited):
                if resource_id not in result:
                    result.append(resource_id)
    return result


def _parse_comfy(record: GenerationRecord, parsed: dict[str, Any]) -> None:
    record.format = "comfyui"
    record.generator_name = "ComfyUI"
    workflow = parsed.get("workflow") if isinstance(parsed.get("workflow"), dict) else parsed
    extra = workflow.get("extra", {})
    if isinstance(extra, dict):
        record.generator_version = str(extra.get("frontendVersion") or "")
    nodes, node_map = _comfy_nodes(parsed)
    resources_by_node: dict[str, GenerationResource] = {}
    text_by_node: dict[str, str] = {}
    latent_size: tuple[int | None, int | None, int | None] = (None, None, None)
    loaded_inputs: dict[str, GenerationInput] = {}
    samplers: list[tuple[str, dict[str, Any], bool]] = []
    has_upscale = False
    has_postprocess = False
    control_input_nodes = {
        _linked_node_id(inputs.get("image"))
        for _, node_type, inputs, _ in nodes
        if "controlnet" in _normalized_key(node_type).replace(" ", "")
    }
    control_input_nodes.discard("")

    for node_id, node_type, inputs, heuristic in nodes:
        normalized = _normalized_key(node_type).replace(" ", "")
        status = "approximated" if heuristic else "translated"
        if normalized in _COMFY_RESOURCE_CLASSES:
            role, name_key = _COMFY_RESOURCE_CLASSES[normalized]
            name = inputs.get(name_key, "")
            resource = _add_resource(
                record,
                role,
                name,
                weights={
                    "model": inputs.get("strength_model"),
                    "clip": inputs.get("strength_clip"),
                },
                provenance={"generator": "comfyui", "node_id": node_id, "node_type": node_type},
            )
            if resource:
                resources_by_node[node_id] = resource
                _report(record, f"nodes.{node_id}", f"resources.{resource.id}", status, inputs, "Workflow widgets were inferred by position." if heuristic else "")
            if normalized == "dualcliploader" and inputs.get("clip_name2"):
                second = _add_resource(
                    record,
                    "text_encoder",
                    inputs["clip_name2"],
                    provenance={"generator": "comfyui", "node_id": node_id, "node_type": node_type, "slot": 2},
                )
                if second:
                    _report(record, f"nodes.{node_id}.inputs.clip_name2", f"resources.{second.id}", status, inputs["clip_name2"])
            if role == "upscaler":
                has_upscale = True
        elif normalized == "cliptextencode":
            text_by_node[node_id] = str(inputs.get("text") or "")
            _report(record, f"nodes.{node_id}.inputs.text", "stage.prompt", status, inputs.get("text"))
        elif normalized in {"ksampler", "ksampleradvanced"}:
            samplers.append((node_id, inputs, heuristic))
        elif normalized == "emptylatentimage":
            latent_size = (
                _optional_int(inputs.get("width")),
                _optional_int(inputs.get("height")),
                _optional_int(inputs.get("batch_size")),
            )
            _report(record, f"nodes.{node_id}", "first_pass.size", status, inputs)
        elif normalized in {"loadimage", "loadimagemask"}:
            kind = (
                "mask"
                if normalized == "loadimagemask"
                else "control_image"
                if node_id in control_input_nodes
                else "init_image"
            )
            loaded_inputs[node_id] = GenerationInput(
                kind=kind,
                reference=str(inputs.get("image") or ""),
                source_node=node_id,
            )
            _report(record, f"nodes.{node_id}.inputs.image", f"inputs.{kind}", status, inputs.get("image"))
        elif "controlnet" in normalized:
            if normalized not in _COMFY_RESOURCE_CLASSES:
                _report(record, f"nodes.{node_id}", "stages.control", status, inputs)
        elif "upscale" in normalized:
            has_upscale = True
            if normalized not in _COMFY_RESOURCE_CLASSES:
                _report(record, f"nodes.{node_id}", "stages.hires", status, inputs)
        elif normalized in {"saveimage", "previewimage"}:
            has_postprocess = True
            _report(record, f"nodes.{node_id}", "stages.post_process", "supported", inputs)
        else:
            record.extras[f"nodes.{node_id}"] = {"class_type": node_type, "inputs": inputs}
            _report(record, f"nodes.{node_id}", "", "untranslated", inputs, f"Unknown ComfyUI node type {node_type} was retained.")

    width, height, batch_size = latent_size
    for index, (node_id, inputs, heuristic) in enumerate(samplers):
        positive_node = _linked_node_id(inputs.get("positive"))
        negative_node = _linked_node_id(inputs.get("negative"))
        positive = text_by_node.get(positive_node, "")
        negative = text_by_node.get(negative_node, "")
        if not positive and index == 0 and text_by_node:
            positive = next(iter(text_by_node.values()))
        resource_ids: list[str] = []
        for input_name in ("model", "positive", "negative"):
            upstream = _linked_node_id(inputs.get(input_name))
            for resource_id in _upstream_resource_ids(upstream, node_map, resources_by_node):
                if resource_id not in resource_ids:
                    resource_ids.append(resource_id)
        kind = "first_pass" if index == 0 else "refiner"
        stage = GenerationStage(
            id=f"stage_{kind}" if index < 2 else f"stage_refiner_{index + 1}",
            kind=kind,
            label="First pass" if index == 0 else "Refiner",
            positive_prompt=positive,
            negative_prompt=negative,
            seeds=[_number(inputs.get("seed"))] if inputs.get("seed") not in (None, "") else [],
            batch_size=batch_size,
            width=width,
            height=height,
            steps=_optional_int(inputs.get("steps")),
            cfg_scale=_optional_float(inputs.get("cfg")),
            sampler=str(inputs.get("sampler_name") or ""),
            scheduler=str(inputs.get("scheduler") or ""),
            denoising_strength=_optional_float(inputs.get("denoise")),
            resource_ids=resource_ids,
            inputs=[item for item in loaded_inputs.values() if item.kind != "control_image"] if index == 0 else [],
            extras={"node_id": node_id},
        )
        record.stages.append(stage)
        _report(
            record,
            f"nodes.{node_id}",
            f"stages.{kind}",
            "approximated" if heuristic or index > 0 else "translated",
            inputs,
            "Stage role was inferred from workflow order." if heuristic or index > 0 else "",
        )

    if not record.stages:
        _sync_first_stage(record, batch_size=batch_size)
    first = record.stages[0]
    record.positive_prompt = first.positive_prompt
    record.negative_prompt = first.negative_prompt
    record.seed = first.seeds[0] if first.seeds else None
    record.steps = first.steps
    record.cfg_scale = first.cfg_scale
    record.sampler = first.sampler
    record.scheduler = first.scheduler
    record.width, record.height = first.width, first.height
    record.denoising_strength = first.denoising_strength
    checkpoint = next((item for item in record.resources if item.role == "checkpoint"), None)
    vae = next((item for item in record.resources if item.role == "vae"), None)
    record.model = checkpoint.name if checkpoint else ""
    record.vae = vae.name if vae else ""
    record.loras = [
        {"name": item.name, "weight": item.weights.get("model", 1)}
        for item in record.resources
        if item.role == "lora"
    ]

    control_ids = [item.id for item in record.resources if item.role == "controlnet"]
    if control_ids:
        control_inputs = [
            GenerationInput(kind="control_image", reference=item.reference, source_node=item.source_node)
            for item in loaded_inputs.values()
            if item.kind == "control_image"
        ]
        record.stages.append(
            GenerationStage(
                id="stage_control",
                kind="control",
                label="Control inputs",
                resource_ids=control_ids,
                inputs=control_inputs,
            )
        )
    if has_upscale:
        record.stages.append(GenerationStage(id="stage_hires", kind="hires", label="Upscale / hires"))
    if has_postprocess:
        record.stages.append(GenerationStage(id="stage_post_process", kind="post_process", label="Output / post-process"))
    stage_order = {"first_pass": 0, "control": 1, "hires": 2, "refiner": 3, "post_process": 4}
    record.stages.sort(key=lambda item: stage_order.get(item.kind, 99))


def _parse_generic_json(record: GenerationRecord, parsed: dict[str, Any]) -> None:
    record.format = "json"
    record.generator_name = "JSON"
    flat = _flatten(parsed)
    record.positive_prompt = str(_find(flat, "positive prompt", "prompt") or "")
    record.negative_prompt = str(_find(flat, "negative prompt", "negativeprompt") or "")
    mapped = {
        "model": _find(flat, "model"),
        "model hash": _find(flat, "model hash", "modelhash"),
        "vae": _find(flat, "vae", "vae name"),
        "sampler": _find(flat, "sampler", "sampler name", "sampler_name"),
        "scheduler": _find(flat, "scheduler", "schedule"),
        "seed": _find(flat, "seed"),
        "steps": _find(flat, "steps"),
        "cfg scale": _find(flat, "cfg scale", "cfgscale", "cfg"),
        "width": _find(flat, "width"),
        "height": _find(flat, "height"),
        "denoise": _find(flat, "denoising strength", "denoise"),
    }
    _apply_settings(record, {key: item for key, item in mapped.items() if item is not None}, source_prefix="json")
    known = {_normalized_key(name) for name in {"prompt", "positive prompt", "negative prompt", "negativeprompt", *mapped.keys()}}
    for path, value in flat.items():
        if _normalized_key(path.split(".")[-1]) not in known:
            record.extras[path] = value
            _report(record, path, "", "untranslated", value, "Retained as an unknown JSON setting.")
    record.loras = _loras(record.positive_prompt)
    _add_resource(record, "checkpoint", record.model, hashes={"model_hash": record.model_hash}, provenance={"generator": "json"})
    _add_resource(record, "vae", record.vae, provenance={"generator": "json"})
    for item in record.loras:
        _add_resource(record, "lora", item.get("name"), weights={"model": item.get("weight", 1)}, provenance={"generator": "json"})
    _sync_first_stage(record)


def parse_generation_payload(
    value: Any, *, source: str = "Image metadata", source_field: str = ""
) -> GenerationRecord:
    """Parse supported generator metadata while retaining the exact input in ``raw``."""
    record = GenerationRecord(source=source, source_field=source_field, raw=value)
    parsed: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = value

    if isinstance(parsed, dict):
        flat_keys = " ".join(_flatten(parsed)).casefold()
        is_comfy = (
            "nodes" in parsed
            or "workflow" in parsed
            or "class_type" in flat_keys
            or (
                isinstance(parsed.get("prompt"), dict)
                and any(isinstance(item, dict) and "class_type" in item for item in parsed["prompt"].values())
            )
        )
        if is_comfy:
            _parse_comfy(record, parsed)
        elif "sui_image_params" in parsed or "sui image params" in flat_keys or "swarm" in flat_keys:
            _parse_swarm(record, parsed)
        else:
            _parse_generic_json(record, parsed)
    else:
        _parse_infotext(record, str(value or ""))
    return record


def translate_generation_record(record: GenerationRecord, target_format: str) -> GenerationTranslation:
    """Assess a neutral record for a target generator without claiming lossless parity."""
    target = _normalized_key(target_format).replace(" ", "")
    aliases = {
        "a1111": "automatic1111",
        "automatic1111": "automatic1111",
        "forge": "forge",
        "sdnext": "sdnext",
        "swarm": "swarmui",
        "swarmui": "swarmui",
        "comfy": "comfyui",
        "comfyui": "comfyui",
    }
    target = aliases.get(target, target)
    if target not in _TARGET_CAPABILITIES:
        raise ValueError(f"Unsupported generation target: {target_format}")
    capabilities = _TARGET_CAPABILITIES[target]
    report: list[GenerationSettingResult] = []
    scalar_fields = {
        "positive_prompt": record.positive_prompt,
        "negative_prompt": record.negative_prompt,
        "sampler": record.sampler,
        "scheduler": record.scheduler,
        "seed": record.seed,
        "steps": record.steps,
        "cfg_scale": record.cfg_scale,
        "width": record.width,
        "height": record.height,
        "denoising_strength": record.denoising_strength,
    }
    for path, value in scalar_fields.items():
        if value not in (None, ""):
            report.append(GenerationSettingResult(source_path=path, neutral_path=path, status="supported", value=value))
    for resource in record.resources:
        if resource.role not in capabilities["resources"]:
            status = "unsupported"
            detail = f"{target} has no neutral mapping for {resource.role} resources."
        elif resource.role == "extension":
            status = "approximated"
            detail = "Extension behavior depends on a matching target installation."
        else:
            status = "translated"
            detail = f"Mapped by typed {resource.role} role; local availability is not guaranteed."
        report.append(
            GenerationSettingResult(
                source_path=f"resources.{resource.id}",
                neutral_path=f"resources.{resource.id}",
                status=status,
                detail=detail,
                value=resource.to_dict(),
            )
        )
    for stage in record.stages:
        status = "translated" if stage.kind in capabilities["stages"] else "unsupported"
        detail = ""
        if stage.kind == "post_process" and target != record.format:
            status = "approximated"
            detail = "Post-processing order and extension behavior may differ on the target."
        report.append(
            GenerationSettingResult(
                source_path=f"stages.{stage.id}",
                neutral_path=f"stages.{stage.id}",
                status=status,
                detail=detail,
                value=stage.to_dict(),
            )
        )
        for index, item in enumerate(stage.inputs):
            input_status = "translated" if item.kind in capabilities["inputs"] else "unsupported"
            report.append(
                GenerationSettingResult(
                    source_path=f"stages.{stage.id}.inputs[{index}]",
                    neutral_path=f"inputs.{item.kind}",
                    status=input_status,
                    detail="The durable reference must resolve on the target system." if input_status == "translated" else "",
                    value=item.to_dict(),
                )
            )
    mapped_extras = {
        item.source_path.rsplit(".", 1)[-1]
        for item in record.adapter_report
        if item.neutral_path and item.status in {"supported", "translated", "approximated"}
    }
    for path, value in record.extras.items():
        if path in mapped_extras:
            continue
        report.append(
            GenerationSettingResult(
                source_path=f"extras.{path}",
                neutral_path="",
                status="untranslated",
                detail="The exact value remains in raw/extras but has no target-neutral mapping.",
                value=value,
            )
        )
    settings = {
        "source_format": record.format,
        "positive_prompt": record.positive_prompt,
        "negative_prompt": record.negative_prompt,
        "resources": [item.to_dict() for item in record.resources],
        "stages": [item.to_dict() for item in record.stages],
    }
    return GenerationTranslation(target_format=target, settings=settings, report=report)


def format_generation_record(record: GenerationRecord) -> str:
    lines = [f"Generation reference ({record.format})"]
    if record.source_field:
        lines.append(f"Source: {record.source} / {record.source_field}")
    fields = (
        ("Generator", record.generator_name),
        ("Generator version", record.generator_version),
        ("Positive prompt", record.positive_prompt),
        ("Negative prompt", record.negative_prompt),
        ("Model", record.model),
        ("Model hash", record.model_hash),
        ("VAE", record.vae),
        ("Sampler", record.sampler),
        ("Scheduler", record.scheduler),
        ("Seed", record.seed),
        ("Steps", record.steps),
        ("CFG scale", record.cfg_scale),
        ("Size", f"{record.width}x{record.height}" if record.width and record.height else ""),
        ("Denoising strength", record.denoising_strength),
    )
    lines.extend(f"{label}: {value}" for label, value in fields if value not in (None, ""))
    if record.resources:
        lines.append("Resources: " + ", ".join(f"{item.role}: {item.name}" for item in record.resources))
    elif record.loras:
        lines.append("LoRAs: " + ", ".join(f"{item.get('name')} ({item.get('weight')})" for item in record.loras))
    if record.stages:
        lines.append("Stages: " + ", ".join(
            item.label or item.kind.replace("_"," ").title()
            for item in record.stages
        ))
    if record.adapter_report:
        counts: dict[str, int] = {}
        for item in record.adapter_report:
            counts[item.status] = counts.get(item.status, 0) + 1
        lines.append("Adapter result: " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items())))
    if record.extras:
        lines.append("Additional settings:")
        lines.extend(f"  {key}: {value}" for key, value in record.extras.items())
    return "\n".join(lines)


def format_raw_generation_payload(value: Any) -> str:
    """Render a retained payload for inspection without changing the stored value."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)
