"""Strict scalar contract shared by trainer review and isolated parser probes.

Only the current SDXL ordinary LoRA proposal is supported. This does not accept
arbitrary trainer configurations, resolve paths, or authorize execution. Keep
this module standard-library-only so a selected Python can load it with -I -S.
"""
from __future__ import annotations

import json
import math
import re


ADAPTER_ID = "collectigen.sd-scripts-sdxl-lora-preview"
ADAPTER_VERSION = 2
RUNTIME_PROFILE_NAMES = frozenset({"sd-scripts-requirements", "kohya-uv-45088f04"})
CONTRACT_VERSION = 1
PYTHON_VERSIONS = {(3, 10), (3, 11)}
SOURCE_REVISION = "6721028c79ee85a78b3a06dfd8954dae310a1cce"
FIXED = {
    "network_module": "networks.lora", "network_train_unet_only": True,
    "optimizer_type": "AdamW", "lr_scheduler": "constant", "save_model_as": "safetensors",
    "no_half_vae": True, "cache_latents": False, "cache_latents_to_disk": False,
    "max_data_loader_n_workers": 0, "persistent_data_loader_workers": False,
    "save_state": False, "save_state_on_train_end": False,
}
RANGES = {"network_dim": (4, 128), "max_train_epochs": (1, 10_000),
          "gradient_accumulation_steps": (1, 128), "seed": (0, 4_294_967_295)}
REQUIRED = frozenset(FIXED) | frozenset(RANGES) | {
    "network_alpha", "learning_rate", "mixed_precision", "save_precision",
    "gradient_checkpointing", "output_name",
}
OPTIONAL = {"pretrained_model_name_or_path"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def validate_configuration(value):
    """Reject unknown fields, nested TOML, coercion and unsupported capabilities.

    sd-scripts reads TOML into an argparse namespace, which bypasses ordinary
    CLI type/choice checks. No extra field is harmless just because it parses.
    """
    if type(value) is not dict or not REQUIRED <= value.keys() <= REQUIRED | OPTIONAL:
        raise ValueError("Trainer settings contain missing or unsupported fields.")
    for key, expected in FIXED.items():
        if type(value[key]) is not type(expected) or value[key] != expected:
            raise ValueError(f"Unsupported trainer setting: {key}.")
    for key, (low, high) in RANGES.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f"Trainer setting is outside its supported integer range: {key}.")
    for key, low, high in (("network_alpha", 1, value["network_dim"]), ("learning_rate", 0, .01)):
        number = value[key]
        if type(number) not in {int, float} or not low <= number <= high or not math.isfinite(number) or (key == "learning_rate" and number == 0):
            raise ValueError(f"Trainer setting is outside its supported numeric range: {key}.")
    for key, allowed in (("mixed_precision", {"no", "fp16", "bf16"}), ("save_precision", {"float", "fp16", "bf16"})):
        if type(value[key]) is not str or value[key] not in allowed:
            raise ValueError(f"Unsupported trainer precision: {key}.")
    if type(value["gradient_checkpointing"]) is not bool:
        raise ValueError("Gradient checkpointing must be an explicit boolean.")
    name = value["output_name"]
    if type(name) is not str or not name.strip() or len(name) > 100 or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError("The proposed output name is invalid.")
    if "pretrained_model_name_or_path" in value:
        path = value["pretrained_model_name_or_path"]
        if type(path) is not str or not path or len(path) > 32_000 or any(ord(c) < 32 for c in path):
            raise ValueError("The proposed base location is invalid.")
    return value


def configuration_toml(value):
    validate_configuration(value)
    return "\n".join(f"{key} = {canonical(item)}" for key, item in sorted(value.items())) + "\n"


def synthetic_configurations():
    """No actual model/dataset/output location or user content is accepted here."""
    baseline = {**FIXED, "network_dim": 32, "network_alpha": 16.0,
        "max_train_epochs": 2, "gradient_accumulation_steps": 4, "seed": 42,
        "learning_rate": .0001, "mixed_precision": "bf16", "save_precision": "fp16",
        "gradient_checkpointing": True, "output_name": "synthetic LoRA ü",
        "pretrained_model_name_or_path": "__COLLECTIGEN_SYNTHETIC__/absent-base.safetensors"}
    return tuple(validate_configuration({**baseline, **change}) for change in (
        {}, {"mixed_precision": "no", "save_precision": "float", "gradient_checkpointing": False},
        {"mixed_precision": "fp16", "save_precision": "bf16", "network_dim": 4, "network_alpha": 1.0},
        {"network_dim": 128, "network_alpha": 128.0, "seed": 4_294_967_295},
    ))
