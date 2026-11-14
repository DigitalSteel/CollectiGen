"""Explicit Manager-local caption settings over the canonical guarded writer.

No model probing, downloads, runtime construction or store/profile discovery.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path

from core.generation_transport import strict_json
from core.history_configuration import MAX_SETTINGS_BYTES
from core.managed_settings_protection import metadata
from core.settings import (AppSettings, _settings_writer, FLORENCE2_MODEL_ID,
                           JOYCAPTION_MODEL_ID, PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR)

# Match the existing Setup page's caption controls; UI and validation share one table.
NUMERIC_FIELDS = {
    "joycaption_max_new_tokens": ("Maximum new tokens", PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR, 1000, 1, int),
    "joycaption_min_new_tokens": ("Minimum new tokens", 0, 1000, 1, int),
    "joycaption_num_beams": ("Beams", 1, 12, 1, int),
    "joycaption_length_penalty": ("Length penalty", .1, 3., .1, float),
    "joycaption_temperature": ("Temperature", .1, 2., .1, float),
    "joycaption_top_k": ("Top K", 0, 200, 1, int),
    "joycaption_top_p": ("Top P", .1, 1., .05, float),
    "joycaption_repetition_penalty": ("Repetition penalty", .5, 2., .05, float),
    "joycaption_no_repeat_ngram_size": ("No-repeat n-gram size", 0, 10, 1, int),
    "joycaption_gpu_index": ("GPU index", 0, 16, 1, int),
}
FIELDS = frozenset(NUMERIC_FIELDS) | {"plain_language_model", "ai_models_directory", "joycaption_prompt",
    "joycaption_do_sample", "joycaption_quantization", "joycaption_quantization_explicit"}


class DescriptionSettingsError(ValueError):
    """Content-free reason for the UI; never expose raw configuration."""


def _validate(values):
    if values["plain_language_model"] not in (JOYCAPTION_MODEL_ID, FLORENCE2_MODEL_ID):
        raise DescriptionSettingsError("unsupported_description_model")
    folder = values["ai_models_directory"]
    if (type(folder) is not str or not folder or len(folder) > 4096
            or any(ord(c) < 32 for c in folder) or not Path(folder).is_absolute()):
        raise DescriptionSettingsError("absolute_models_directory_required")
    prompt = values["joycaption_prompt"]
    if type(prompt) is not str or len(prompt.encode("utf-8")) > 64 * 1024:
        raise DescriptionSettingsError("description_prompt_too_large")
    if values["joycaption_quantization"] not in ("bf16", "nf4"):
        raise DescriptionSettingsError("unsupported_description_quantization")
    for key in ("joycaption_do_sample", "joycaption_quantization_explicit"):
        if type(values[key]) is not bool:
            raise DescriptionSettingsError("description_boolean_required")
    for key, (_label, low, high, _step, kind) in NUMERIC_FIELDS.items():
        value = values[key]
        valid_type = type(value) is int if kind is int else type(value) in (int, float)
        if not valid_type or not math.isfinite(value) or not low <= value <= high:
            raise DescriptionSettingsError("description_control_out_of_range")
    if values["joycaption_min_new_tokens"] > values["joycaption_max_new_tokens"]:
        raise DescriptionSettingsError("description_length_order")
    return values


@dataclass(frozen=True)
class DescriptionSnapshot:
    digest: str | None
    _values: dict = field(repr=False)
    _payload: dict = field(repr=False)

    @property
    def values(self):
        return deepcopy(self._values)


class ManagerDescriptionSettings:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.suffix.casefold() == ".ini":
            raise ValueError("explicit_json_settings_path_required")
        defaults = AppSettings(ai_models_directory=str(self.path.parent / "models"))
        self.defaults = {key: getattr(defaults, key) for key in FIELDS}

    def _snapshot(self, raw, payload):
        metadata(payload)  # Preserve the existing managed groups, never reclassify caption settings as a new group.
        values = _validate({key: payload.get(key, default) for key, default in self.defaults.items()})
        AppSettings._from_payload({**self.defaults, **payload})
        return DescriptionSnapshot(None if raw is None else sha256(raw).hexdigest(), deepcopy(values), deepcopy(payload))

    def observe(self):
        try:
            from core.training_source_io import _assert_unredirected
            _assert_unredirected(self.path)
            if not self.path.exists():
                return self._snapshot(None, {})
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise DescriptionSettingsError("settings_size_limit")
            payload = strict_json(raw)
            if type(payload) is not dict:
                raise DescriptionSettingsError("settings_object_required")
            return self._snapshot(raw, payload)
        except DescriptionSettingsError:
            raise
        except Exception:
            raise DescriptionSettingsError("description_settings_unavailable") from None

    def save(self, changes, *, expected):
        if (type(expected) is not DescriptionSnapshot or type(changes) is not dict
                or not changes or not set(changes) <= FIELDS):
            raise DescriptionSettingsError("description_changes_required")
        _validate({**expected.values, **changes})
        try:
            with _settings_writer(self.path):
                current = self.observe()
                if current.digest != expected.digest:
                    raise DescriptionSettingsError("settings_changed")
                payload = {**current._payload, **changes}
                attempted = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                if len(attempted) > MAX_SETTINGS_BYTES:
                    raise DescriptionSettingsError("settings_size_limit")
                saved = self._snapshot(attempted, payload)
                AppSettings._from_payload(payload)._save_locked(self.path, _patch_payload=payload)
                return saved
        except DescriptionSettingsError:
            raise
        except Exception:
            raise DescriptionSettingsError("description_save_failed") from None
