"""Pure authored prompt presets and bounded, reproducible wildcard resolution.

No preset silently changes a model binding or a request setting. The caller
reviews a preview, then sends only fully resolved prompt strings to execution.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
import random
import re
from collections.abc import Mapping


PRESET_VERSION = 1
IMPORTED_PRESET_VERSION = 2
EXPANSION_VERSION = 1
MAX_PRESET_BYTES = 64 * 1024
MAX_REQUEST_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_CATALOG_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_AUTHORED_PROMPT_BYTES = 16 * 1024
MAX_WILDCARDS = 256
MAX_CHOICES = 128
MAX_DEPTH = 8
MAX_EXPANSIONS = 128
SETTING_KEYS = frozenset({"seed", "steps", "cfg_scale", "width", "height",
                          "sampler", "scheduler", "denoising_strength"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TOKEN = re.compile(r"<wildcard:([^<>]*)>")


def imported_prompt_kind(text):
    """Conservative pinned Swarm subset; unsupported source remains inert."""
    if not isinstance(text, str):
        return "held"
    simple = text.replace("{value}", "", 1)
    # Only ordinary words and this small punctuation set are qualified. Swarm
    # tags, backend weighting/scheduling, escaped syntax and extension tokens
    # stay evidence, even when a particular installation treats them literally.
    if (any(not (char.isalnum() or char in " \t\r\n, .!?'-;/_") for char in simple)
            or "__" in simple or re.search(r"\b(?:BREAK|AND)\b", simple)):
        return "held"
    return "compose" if "{value}" in text else "literal"


def _import_semantics(preset):
    descriptor = preset.extra_fields.get("import_semantics")
    if (not isinstance(descriptor, dict) or set(descriptor) != {"version", "sides", "held_fields"}
            or type(descriptor.get("version")) is not int or descriptor["version"] != 1
            or not isinstance(descriptor.get("sides"), dict)
            or set(descriptor["sides"]) != {"positive_prompt", "negative_prompt"}
            or not isinstance(descriptor.get("held_fields"), list)
            or len(descriptor["held_fields"]) > 128
            or any(not isinstance(key, str) or len(key) > 256 for key in descriptor["held_fields"])):
        raise ValueError("Imported preset semantics are unsupported; keep this item as source evidence.")
    for side, kind in descriptor["sides"].items():
        if not isinstance(kind, str) or kind not in {"absent", "literal", "compose", "held"}:
            raise ValueError("Imported prompt mode is unsupported.")
        text = getattr(preset, side)
        if kind == "absent" and text or kind in {"literal", "compose"} and imported_prompt_kind(text) != kind:
            raise ValueError("Imported prompt syntax differs from its reviewed semantics.")
    return descriptor


def _canonical(value, limit, label):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must contain bounded JSON values.") from exc
    if len(encoded) > limit:
        raise ValueError(f"{label} exceeds the supported size.")
    return encoded


def _hash(value, limit, label):
    return sha256(_canonical(value, limit, label)).hexdigest()


def _prompt(value, label, *, limit=MAX_AUTHORED_PROMPT_BYTES):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds the supported prompt size.")
    return value


def _settings(value):
    if not isinstance(value, Mapping):
        raise ValueError("Preset settings must be a JSON object.")
    result = deepcopy(dict(value))
    for key in result:
        if not isinstance(key, str):
            raise ValueError("Preset setting names must be strings.")
        if key not in SETTING_KEYS:
            continue  # Retained for forward compatibility; never applied.
        item = result[key]
        if key in {"sampler", "scheduler"}:
            if not isinstance(item, str) or len(item) > 128:
                raise ValueError(f"Preset {key} must be a short text value.")
        elif key in {"seed", "steps", "width", "height"}:
            if type(item) is not int or item < 0 or item > 2**63 - 1:
                raise ValueError(f"Preset {key} must be a bounded integer.")
        elif type(item) not in {int, float} or not float("-inf") < item < float("inf"):
            raise ValueError(f"Preset {key} must be a finite number.")
    return result


@dataclass(frozen=True)
class PromptPreset:
    preset_id: str
    name: str
    positive_prompt: str
    negative_prompt: str
    settings: dict[str, object] = field(default_factory=dict)
    schema_version: int = PRESET_VERSION
    extra_fields: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError("A prompt preset must be a JSON object.")
        raw = deepcopy(dict(value))
        _canonical(raw, MAX_PRESET_BYTES, "Prompt preset")
        version = raw.get("schema_version")
        if type(version) is not int or version < 1:
            raise ValueError("The prompt preset version is invalid.")
        preset_id = raw.get("preset_id")
        name = raw.get("name")
        if not isinstance(preset_id, str) or not _NAME.fullmatch(preset_id):
            raise ValueError("The prompt preset needs a stable identifier.")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("The prompt preset needs a short name.")
        known = {"schema_version", "preset_id", "name", "positive_prompt",
                 "negative_prompt", "settings"}
        preset = cls(preset_id, name,
                   _prompt(raw.get("positive_prompt", ""), "Positive prompt"),
                   _prompt(raw.get("negative_prompt", ""), "Negative prompt"),
                   _settings(raw.get("settings", {})), version,
                   {key: value for key, value in raw.items() if key not in known})
        if version == 1 and ("import_semantics" in preset.extra_fields or "swarm_import" in preset.extra_fields):
            raise ValueError("Imported preset semantics require schema version 2.")
        if version == IMPORTED_PRESET_VERSION:
            _import_semantics(preset)
        return preset

    def to_dict(self):
        result = deepcopy(self.extra_fields)
        result.update(schema_version=self.schema_version, preset_id=self.preset_id,
                      name=self.name, positive_prompt=self.positive_prompt,
                      negative_prompt=self.negative_prompt, settings=deepcopy(self.settings))
        _canonical(result, MAX_PRESET_BYTES, "Prompt preset")
        return result

    @property
    def content_sha256(self):
        return _hash(self.to_dict(), MAX_PRESET_BYTES, "Prompt preset")


def preset_application_options(preset):
    """Read-only UI projection of canonical semantic gates."""
    preset = PromptPreset.from_dict(preset.to_dict())
    if preset.schema_version not in {PRESET_VERSION, IMPORTED_PRESET_VERSION}:
        raise ValueError("This prompt preset version cannot be applied.")
    semantics = _import_semantics(preset) if preset.schema_version == IMPORTED_PRESET_VERSION else None
    result = {"held_fields": tuple(semantics["held_fields"]) if semantics else (),
        "available_settings": tuple(key for key in preset.settings if key in SETTING_KEYS),
        "unsupported_settings": tuple(key for key in preset.settings if key not in SETTING_KEYS)}
    for side in ("positive_prompt", "negative_prompt"):
        kind = semantics["sides"][side] if semantics else "literal"
        result[side + "_modes"] = (("keep",) if kind in {"held", "absent"} else
            ("keep", "compose") if kind == "compose" else ("keep", "append", "replace"))
    return result


def preview_preset_application(preset: PromptPreset, current_request: Mapping,
                               *, positive_mode: str, negative_mode: str,
                               setting_keys=(), acknowledged_omissions=False) -> dict[str, object]:
    """Preview authored fields; selected settings are explicit opt-ins."""
    if not isinstance(preset, PromptPreset) or preset.schema_version not in {PRESET_VERSION, IMPORTED_PRESET_VERSION}:
        raise ValueError("This prompt preset version cannot be applied.")
    preset = PromptPreset.from_dict(preset.to_dict())
    if not isinstance(current_request, Mapping):
        raise ValueError("Provide the current generation request for preview.")
    current = deepcopy(dict(current_request))
    _canonical(current, MAX_REQUEST_PREVIEW_BYTES, "Current request preview")
    selected = tuple(setting_keys)
    if len(selected) != len(set(selected)) or any(key not in SETTING_KEYS for key in selected):
        raise ValueError("Choose distinct supported preset settings to apply.")
    if any(key not in preset.settings for key in selected):
        raise ValueError("The preset lacks a selected setting.")
    result = deepcopy(current)
    changes = {}
    semantics = _import_semantics(preset) if preset.schema_version == IMPORTED_PRESET_VERSION else None
    omitted = list(semantics["held_fields"]) if semantics else []
    if omitted and acknowledged_omissions is not True:
        raise ValueError("Review and acknowledge the unsupported preset fields before applying a supported subset.")
    for side, mode in (("positive_prompt", positive_mode), ("negative_prompt", negative_mode)):
        allowed = {"keep", "append", "replace", "compose"} if semantics else {"keep", "append", "replace"}
        if mode not in allowed:
            raise ValueError("Choose keep, append, or replace for each prompt.")
        before = _prompt(current.get(side, ""), side)
        authored = getattr(preset, side)
        kind = semantics["sides"][side] if semantics else "literal"
        if mode != "keep" and kind in {"absent", "held"}:
            raise ValueError(f"Keep {side}: its imported source is absent or unsupported.")
        if kind == "compose" and mode not in {"keep", "compose"} or mode == "compose" and kind != "compose":
            raise ValueError("Review the imported single {value} composition using Compose or Keep.")
        if mode == "compose":
            if imported_prompt_kind(before) != "literal" or any(token in before for token in ("$$", "$&", "$`", "$'")):
                raise ValueError("The current prompt needs a separate review before Swarm composition can be translated.")
            after = authored.replace("{value}", before, 1)
        else:
            after = before if mode == "keep" else authored if mode == "replace" else ", ".join(
                part for part in (before.strip(), authored.strip()) if part)
        _prompt(after, side)
        result[side] = after
        if after != before:
            changes[side] = {"before": before, "after": after, "mode": mode}
    for key in selected:
        before = current.get(key)
        after = deepcopy(preset.settings[key])
        result[key] = after
        if before != after:
            changes[key] = {"before": before, "after": after, "mode": "replace"}
    return {"proposed_request": result, "changes": changes,
            "available_settings": tuple(key for key in preset.settings if key in SETTING_KEYS),
            "skipped_settings": tuple(key for key in preset.settings if key not in selected),
            "held_fields": tuple(omitted), "omitted_fields": tuple(omitted),
            "preset_sha256": preset.content_sha256,
            "authored_positive_prompt": result["positive_prompt"],
            "authored_negative_prompt": result["negative_prompt"]}


def _catalog(value):
    if not isinstance(value, Mapping) or len(value) > MAX_WILDCARDS:
        raise ValueError("The wildcard catalog is missing or too large.")
    result = {}
    for name, choices in value.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("A wildcard name is invalid.")
        if (not isinstance(choices, (list, tuple)) or not 1 <= len(choices) <= MAX_CHOICES
                or any(not isinstance(choice, str) or not choice
                       or len(choice.encode("utf-8")) > 4096 for choice in choices)):
            raise ValueError(f"Wildcard {name} needs bounded nonempty choices.")
        result[name] = list(choices)
    _canonical(result, MAX_CATALOG_BYTES, "Wildcard catalog")
    return result


def resolve_prompts(authored_positive: str, authored_negative: str, catalog: Mapping,
                    *, seed: int, preset: PromptPreset | None = None) -> dict[str, object]:
    """Resolve every token locally or fail; never return a partial prompt."""
    positive = _prompt(authored_positive, "Positive prompt")
    negative = _prompt(authored_negative, "Negative prompt")
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError("Choose an explicit bounded wildcard seed.")
    if preset is not None and (not isinstance(preset, PromptPreset)
                               or preset.schema_version not in {PRESET_VERSION, IMPORTED_PRESET_VERSION}):
        raise ValueError("This prompt preset version cannot be resolved.")
    if preset is not None:
        preset = PromptPreset.from_dict(preset.to_dict())
    if preset is not None and preset.schema_version == IMPORTED_PRESET_VERSION:
        # An imported scalar or kept prompt never authorizes expanding a
        # different locally authored wildcard catalog. Composition was already
        # resolved explicitly by preview_preset_application against the draft.
        return {"operation_version": EXPANSION_VERSION,
            "authored_positive_prompt": positive, "authored_negative_prompt": negative,
            "resolved_positive_prompt": positive, "resolved_negative_prompt": negative,
            "seed": seed, "catalog_sha256": None, "catalog_applied": False,
            "preset_sha256": preset.content_sha256, "resolved_tokens": []}
    choices = _catalog(catalog)
    rng = random.Random(seed)
    tokens = []

    def expand(value, stack):
        if len(value.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("Resolved prompt exceeds the supported size.")
        def replace(match):
            name = match.group(1)
            if not _NAME.fullmatch(name):
                raise ValueError(f"Invalid wildcard token: {match.group(0)}")
            if name not in choices:
                raise ValueError(f"Wildcard {name} is missing from the selected catalog.")
            if name in stack:
                raise ValueError("Wildcard cycle: " + " → ".join((*stack, name)))
            if len(stack) >= MAX_DEPTH or len(tokens) >= MAX_EXPANSIONS:
                raise ValueError("Wildcard expansion exceeds the depth or count limit.")
            index = rng.randrange(len(choices[name]))
            selected = choices[name][index]
            tokens.append({"name": name, "choice_index": index, "choice": selected,
                           "choice_sha256": sha256(selected.encode("utf-8")).hexdigest()})
            return expand(selected, (*stack, name))
        result = _TOKEN.sub(replace, value)
        if "<wildcard:" in result:
            raise ValueError("A wildcard token is incomplete or malformed.")
        _prompt(result, "Resolved prompt", limit=MAX_PROMPT_BYTES)
        return result

    resolved_positive = expand(positive, ())
    resolved_negative = expand(negative, ())
    return {"operation_version": EXPANSION_VERSION,
            "authored_positive_prompt": positive, "authored_negative_prompt": negative,
            "resolved_positive_prompt": resolved_positive,
            "resolved_negative_prompt": resolved_negative,
            "seed": seed, "catalog_sha256": _hash(choices, MAX_CATALOG_BYTES, "Wildcard catalog"),
            "preset_sha256": preset.content_sha256 if preset is not None else None,
            "resolved_tokens": tokens}
