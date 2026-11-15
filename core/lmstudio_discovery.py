"""Decode explicit LM Studio v1 model-list bytes; no connection or readiness grant.

Discovery is an external declaration. Keys are not hashes, loaded-instance IDs
are not model keys, and advertised capabilities do not qualify inference.
"""
from dataclasses import dataclass
import math
import unicodedata

from core.generation_transport import strict_json

MAX_BYTES = 4 * 1024 * 1024
MAX_MODELS = 256
MAX_INSTANCES = 128
MAX_ID = 1024
MAX_LABEL = 256
MAX_CONTEXT = 2**31 - 1
MAX_SIZE = 2**63 - 1


class DiscoveryError(ValueError):
    """Stable, content-free diagnostic; never includes a response or identifier."""


def _require(condition, code):
    if not condition:
        raise DiscoveryError(code)


def _text(value, limit, code):
    _require(type(value) is str and 0 < len(value) <= limit, code)
    _require(value == value.strip() and not any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value
    ), code)
    return value


def _integer(value, minimum, maximum, code):
    _require(type(value) is int and minimum <= value <= maximum, code)
    return value


def _optional_integer(mapping, key, *, nullable=False):
    if key not in mapping:
        return None
    value = mapping[key]
    if nullable and value is None:
        return None
    return _integer(value, 1, MAX_CONTEXT, "invalid_context_config")


def _optional_bool(mapping, key):
    if key not in mapping:
        return None
    value = mapping[key]
    _require(type(value) is bool, "invalid_capability_or_config")
    return value


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    context_length: int | None
    eval_batch_size: int | None
    parallel: int | None
    flash_attention: bool | None
    num_experts: int | None
    offload_kv_cache_to_gpu: bool | None


@dataclass(frozen=True, slots=True)
class LoadedInstance:
    instance_id: str
    config: InstanceConfig


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    key: str
    model_type: str
    display_name: str
    size_bytes: int
    max_context_length: int | None
    vision: bool | None
    trained_for_tool_use: bool | None
    loaded_instances: tuple[LoadedInstance, ...]

    @property
    def known_model_type(self):
        """Recognized metadata type only; not support for a model execution."""
        return self.model_type in {"llm", "embedding", "decision"}


@dataclass(frozen=True, slots=True)
class ModelInventory:
    models: tuple[ModelDescriptor, ...]

    def model(self, key: str) -> ModelDescriptor:
        _text(key, MAX_ID, "invalid_selection")
        for model in self.models:
            if model.key == key:
                return model
        raise DiscoveryError("model_not_found")

    def loaded_instance(self, instance_id: str) -> tuple[ModelDescriptor, LoadedInstance]:
        _text(instance_id, MAX_ID, "invalid_selection")
        for model in self.models:
            for instance in model.loaded_instances:
                if instance.instance_id == instance_id:
                    return model, instance
        raise DiscoveryError("instance_not_found")


def parse_models(raw: bytes) -> ModelInventory:
    """Parse a bounded UTF-8 GET /api/v1/models response without performing IO.

    Unknown fields are ignored, not retained as arbitrary private diagnostic
    payloads. Missing capability/context claims remain unknown. No fallback,
    default selection, availability assertion or server-policy inference occurs.
    """
    _require(type(raw) is bytes, "response_bytes_required")
    _require(0 < len(raw) <= MAX_BYTES, "response_size_limit")
    try:
        data = strict_json(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise DiscoveryError("invalid_json") from None
    # The stock decoder rejects named NaN/Infinity. Also reject overflowed JSON
    # floats (e.g. 1e999), including those in forward-compatible unknown fields.
    pending = [data]
    while pending:
        value = pending.pop()
        if type(value) is float:
            _require(math.isfinite(value), "invalid_json_number")
        elif type(value) is dict:
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)
    _require(type(data) is dict, "invalid_response")
    records = data.get("models")
    _require(type(records) is list and len(records) <= MAX_MODELS, "invalid_models")
    models, keys, instance_ids = [], set(), set()
    for record in records:
        _require(type(record) is dict, "invalid_model")
        key = _text(record.get("key"), MAX_ID, "invalid_model_key")
        _require(key not in keys, "duplicate_model_key")
        keys.add(key)
        kind = _text(record.get("type"), 64, "invalid_model_type")
        label = _text(record.get("display_name"), MAX_LABEL, "invalid_model_label")
        size = _integer(record.get("size_bytes"), 0, MAX_SIZE, "invalid_model_size")
        maximum = _optional_integer(record, "max_context_length", nullable=True)
        capabilities = record.get("capabilities", {})
        _require(type(capabilities) is dict, "invalid_capabilities")
        vision = _optional_bool(capabilities, "vision")
        tools = _optional_bool(capabilities, "trained_for_tool_use")
        instances = record.get("loaded_instances")
        _require(type(instances) is list and len(instances) <= MAX_INSTANCES,
                 "invalid_loaded_instances")
        loaded = []
        for instance in instances:
            _require(type(instance) is dict, "invalid_instance")
            identity = _text(instance.get("id"), MAX_ID, "invalid_instance_id")
            _require(identity not in instance_ids, "duplicate_instance_id")
            instance_ids.add(identity)
            config = instance.get("config")
            _require(type(config) is dict, "invalid_instance_config")
            values = InstanceConfig(
                _optional_integer(config, "context_length"),
                _optional_integer(config, "eval_batch_size"),
                _optional_integer(config, "parallel"),
                _optional_bool(config, "flash_attention"),
                _optional_integer(config, "num_experts"),
                _optional_bool(config, "offload_kv_cache_to_gpu"),
            )
            loaded.append(LoadedInstance(identity, values))
        models.append(ModelDescriptor(key, kind, label, size, maximum,
                                      vision, tools, tuple(loaded)))
    return ModelInventory(tuple(models))
