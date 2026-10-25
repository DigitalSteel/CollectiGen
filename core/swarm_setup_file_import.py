"""Read-only proposals and selected evidence from a local SwarmUI preset export.

No account access, artwork loading, setup save, or generator authority is owned
here. The caller must disclose held fields and recheck the captured source before
publishing a separately reviewed Generation Setup.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Callable, Literal

from .generation_prompt_templates import imported_prompt_kind
from .training_source_io import _assert_unredirected, _identity


SOURCE_DIALECT = "swarmui-title-keyed-json-e2c35f354dd9411b64037e18abf319927260609d"
MAX_FILE_BYTES = 1024 * 1024
MAX_PRESETS = 256
MAX_FIELDS = 128
MAX_FIELD_CHARS = 16384
MAX_COMPOSITION_PRESETS = 8
MAX_IMPORT_EVIDENCE_BYTES = 2 * MAX_FILE_BYTES + 65536
_SETTINGS = {"steps": "steps", "width": "width", "height": "height",
             "cfgscale": "cfg_scale", "sampler": "sampler", "scheduler": "scheduler"}
_RESOURCE_FIELDS = frozenset({"model", "vae", "loras", "loraweights", "loraweight",
                              "refinermodel", "refinervae", "cliplmodel", "t5xxlmodel",
                              "qwenmodel", "mistralmodel"})
_ROW_FIELDS = frozenset({"author", "title", "description", "preview_image", "is_starred", "param_map"})


def translate_text_setup_to_swarm(recipe, *, include_prompts=False):
    """Translate the qualified text/scalar subset, never silently discard controls.

    Registry labels are not SwarmUI executable names. Resource-bound setups
    remain native-only until an exact version-qualified binding is reviewed.
    """
    from .generation_recipes import _validate, render_prompt_parts
    recipe = _validate(deepcopy(recipe))[0]
    if (recipe.get("resources") or recipe.get("stages") or recipe.get("source_binding")
            or recipe.get("source_settings") or recipe.get("finishing")
            or any(key in recipe["settings"] for key in ("model", "model_hash", "vae"))):
        raise ValueError("This setup contains model components, Remix or finishing controls. "
                         "Export it as CollectiGen JSON; SwarmUI translation does not yet preserve those choices.")
    params = {}
    prompts = recipe.get("prompts", {})
    positive = (render_prompt_parts(recipe.get("prompt_parts", []))
                if recipe["schema_version"] == 3 else prompts.get("positive_prompt"))
    negative = (recipe.get("negative_prompt") if recipe["schema_version"] == 3
                else prompts.get("negative_prompt"))
    for key, value in (("prompt", positive), ("negativeprompt", negative)):
        if not include_prompts:
            continue
        if value is None or key == "prompt" and not value:
            continue
        if imported_prompt_kind(value) != "literal" or key == "prompt" and len(value) > 4096:
            raise ValueError("This setup contains prompt syntax outside the qualified literal subset. "
                             "Export it as CollectiGen JSON instead.")
        params[key] = value
    reverse = {value: key for key, value in _SETTINGS.items()}
    for key, value in recipe["settings"].items():
        if key in {"model", "model_hash", "vae"}:
            continue
        field = reverse[key]
        if type(value) not in (str, int, float) or type(value) is bool:
            raise ValueError(f"The {key} setting is outside the qualified SwarmUI subset.")
        rendered = str(value)
        if _setting(field, rendered) != value:
            raise ValueError(f"The {key} setting cannot round-trip exactly. Export CollectiGen JSON instead.")
        params[field] = rendered
    if ("width" in params) != ("height" in params):
        raise ValueError("SwarmUI export needs both width and height, or neither. Export CollectiGen JSON instead.")
    if not params:
        raise ValueError("This setup has no qualified text or settings to export to SwarmUI.")
    return {recipe["name"]: {"title": recipe["name"], "description": "",
                              "preview_image": "", "param_map": params}}


@dataclass(frozen=True)
class SwarmSetupHeldField:
    field: str
    reason: str
    value: str = ""


@dataclass(frozen=True)
class SwarmSetupPreset:
    key: str
    title: str
    description: str
    positive_prompt: str | None
    negative_prompt: str | None
    prompt_state: Literal["absent", "literal", "held"]
    negative_prompt_state: Literal["absent", "literal", "held"]
    settings: dict[str, object]
    held_fields: tuple[SwarmSetupHeldField, ...]
    source_row: dict[str, object]


@dataclass(frozen=True)
class SwarmSetupFileReview:
    path: Path
    source_sha256: str
    source_identity: tuple[int, int, int, int, int]
    presets: tuple[SwarmSetupPreset, ...]
    source_dialect: str = SOURCE_DIALECT


def _cancel(should_cancel):
    if should_cancel is not None and should_cancel():
        raise InterruptedError("SwarmUI setup file review cancelled; nothing was saved.")


def _read_source(path, should_cancel):
    source = Path(path).absolute()
    _cancel(should_cancel)
    _assert_unredirected(source)
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Choose one regular SwarmUI JSON export file.")
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError("SwarmUI preset file exceeds the 1 MiB import limit.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        _assert_unredirected(source)
        if (not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened)
                or _identity(opened) != _identity(source.stat(follow_symlinks=False))):
            raise ValueError("SwarmUI preset file changed before reading; choose it again.")
        blocks, total = [], 0
        while True:
            _cancel(should_cancel)
            block = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise ValueError("SwarmUI preset file exceeds the 1 MiB import limit.")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _assert_unredirected(source)
    current = source.stat(follow_symlinks=False)
    raw = b"".join(blocks)
    if (_identity(opened) != _identity(after) or _identity(after) != _identity(current)
            or len(raw) != current.st_size):
        raise ValueError("SwarmUI preset file changed while reading; choose it again.")
    _cancel(should_cancel)
    return source, raw, _identity(current)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("SwarmUI preset JSON has duplicate keys; resolve them before importing.")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("SwarmUI preset JSON must contain finite standard JSON values.")


def _bounded_tree(value):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 8 or count > 40000:
            raise ValueError("SwarmUI preset JSON exceeds its structural bounds.")
        if isinstance(item, dict):
            if len(item) > MAX_PRESETS or any(
                    len(key) > 256 or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
                                         for char in key) for key in item):
                raise ValueError("SwarmUI preset JSON has too many or oversized fields.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > MAX_PRESETS:
                raise ValueError("SwarmUI preset JSON has an oversized list.")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > MAX_FIELD_CHARS or any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ValueError("SwarmUI preset JSON contains oversized or invalid text.")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("SwarmUI preset JSON must contain finite values.")


def _short_text(value, *, limit=128, required=False):
    return (isinstance(value, str) and len(value) <= limit
            and (not required or bool(value.strip()))
            and not any(ord(char) < 32 for char in value))


def _setting(key, value):
    if key in {"steps", "width", "height"} and re.fullmatch(r"[0-9]{1,5}", value):
        number = int(value)
        if key == "steps" and 1 <= number <= 150:
            return number
        if key in {"width", "height"} and 64 <= number <= 2048 and number % 64 == 0:
            return number
    elif key == "cfgscale" and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        number = float(value)
        # The shared composer retains two decimal places. Trailing zeroes
        # are harmless, but finer source precision must not round silently.
        fraction = value.partition(".")[2].rstrip("0")
        if math.isfinite(number) and 0 <= number <= 50 and len(fraction) <= 2:
            return number
    elif key in {"sampler", "scheduler"} and re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", value):
        if value.strip():
            return value
    return None


def _proposal(key, row):
    if (not _short_text(key, required=True) or not isinstance(row, dict)
            or len(row) > MAX_FIELDS or row.get("title") != key):
        raise ValueError("Each exported preset needs an exact matching title and a bounded object.")
    params = row.get("param_map")
    if (not isinstance(params, dict) or len(params) > MAX_FIELDS
            or any(not _short_text(field, limit=256, required=True) or not isinstance(value, str)
                   for field, value in params.items())):
        raise ValueError("Each preset needs a bounded string-valued param_map.")
    description = row.get("description", "")
    if not isinstance(description, str):
        raise ValueError("The preset description must be text.")
    if "author" in row and not _short_text(row["author"]):
        raise ValueError("The preset author must be bounded text.")
    if "is_starred" in row and type(row["is_starred"]) is not bool:
        raise ValueError("The preset star flag must be a JSON boolean.")
    if "preview_image" in row and not isinstance(row["preview_image"], str):
        raise ValueError("The preset preview reference must be text.")
    held = []
    if len(key) > 120:
        held.append(SwarmSetupHeldField("title", "Choose a setup name of at most 120 characters.", key))
    prompts, states = {}, {}
    for field in ("prompt", "negativeprompt"):
        if field not in params:
            prompts[field], states[field] = None, "absent"
            continue
        value = params[field]
        reason = ""
        if imported_prompt_kind(value) != "literal":
            reason = "Dynamic, composed, weighted or otherwise unsupported prompt syntax stays source evidence."
        elif field == "prompt" and not value.strip():
            reason = "An empty positive prompt would clear text; a setup cannot represent that imported action."
        elif field == "prompt" and len(value) > 4096:
            reason = "The source prompt exceeds one setup part; edit it explicitly before reuse."
        states[field] = "held" if reason else "literal"
        prompts[field] = None if reason else value
        if reason:
            held.append(SwarmSetupHeldField(field, reason, value))
    settings = {}
    for field, value in params.items():
        if field in prompts:
            continue
        parsed = _setting(field, value) if field in _SETTINGS else None
        if parsed is not None:
            settings[_SETTINGS[field]] = parsed
            continue
        if field in _RESOURCE_FIELDS:
            reason = "A source resource name is not an exact registered model, LoRA or component selection."
        elif field == "seed":
            reason = "Generation Setups do not currently store seed; choose it separately in the image request."
        elif field in _SETTINGS:
            reason = "This setting value is outside the qualified local-file import subset."
        else:
            reason = "This source field is unsupported or task-specific and is not included in the setup."
        held.append(SwarmSetupHeldField(field, reason, value))
    # The shared composer selects size as one pair. Never fill an omitted or
    # invalid companion with that editor's current/default other dimension.
    if ("width" in settings) != ("height" in settings):
        for field in ("width", "height"):
            if field in settings:
                settings.pop(field)
                held.append(SwarmSetupHeldField(
                    field, "Import image size only when both width and height are present and valid.",
                    params[field],
                ))
    if row.get("preview_image"):
        held.append(SwarmSetupHeldField("preview_image", "Preview artwork is not read, fetched or imported."))
    for field in sorted(row.keys() - _ROW_FIELDS):
        value = json.dumps(row[field], ensure_ascii=False, allow_nan=False)
        if len(value) > MAX_FIELD_CHARS:
            raise ValueError("An unsupported source field exceeds the evidence display limit.")
        held.append(SwarmSetupHeldField("source." + field, "Source metadata stays in the original file.", value))
    return SwarmSetupPreset(key, key, description, prompts["prompt"], prompts["negativeprompt"],
                            states["prompt"], states["negativeprompt"], settings, tuple(held), deepcopy(row))


def read_swarm_setup_file(path: str | Path, *, should_cancel: Callable[[], bool] | None = None
                          ) -> SwarmSetupFileReview:
    """Read only the chosen export; return proposals, never executable presets.

    The dialect identifies the translator qualification, not the unknowable
    version of the installation that produced an unversioned export.
    """
    source, raw, identity = _read_source(path, should_cancel)
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("Choose a bounded UTF-8 SwarmUI title-keyed JSON export.") from exc
    _bounded_tree(payload)
    if not isinstance(payload, dict) or not 1 <= len(payload) <= MAX_PRESETS:
        raise ValueError("Choose an export containing one to 256 named presets.")
    proposals = []
    for key, row in payload.items():
        _cancel(should_cancel)
        proposals.append(_proposal(key, row))
    return SwarmSetupFileReview(source, hashlib.sha256(raw).hexdigest(), identity, tuple(proposals))


def select_swarm_setup_preset(review: SwarmSetupFileReview, key: str) -> SwarmSetupPreset:
    """Choose one exact entry from the reviewed file, without reading again."""
    if not isinstance(review, SwarmSetupFileReview):
        raise ValueError("Read and review a SwarmUI preset file first.")
    for preset in review.presets:
        if preset.key == key:
            return preset
    raise ValueError("Choose a preset from this exact file review.")


def inspect_swarm_setup_resource_references(review: SwarmSetupFileReview,
                                             key: str) -> tuple[dict[str, str], ...]:
    """Name source singleton references without treating their names as identities.

    Multi-LoRA, refiner and encoder syntax is not qualified for this local-file
    dialect; those fields remain held as inert source evidence.
    """
    preset = select_swarm_setup_preset(review, key)
    params = preset.source_row["param_map"]
    return tuple({"field": field, "source_value": params[field], "role": role}
                 for field, role in (("model", "checkpoint"), ("vae", "vae"))
                 if field in params and params[field].strip())


def inspect_swarm_setup_composition_resources(review: SwarmSetupFileReview,
                                               keys) -> tuple[dict[str, str], ...]:
    """List each ordered contributor's inert singleton source references."""
    presets = _composition_presets(review, keys)
    return tuple({"preset_key": preset.key, **reference}
                 for preset in presets
                 for reference in inspect_swarm_setup_resource_references(review, preset.key))


def _validate_resolved_resources(evidence, preset, recipe):
    decisions = evidence.get("resolved_resources", [])
    if not isinstance(decisions, list) or len(decisions) > 2:
        raise ValueError("SwarmUI resource choices must be a bounded list.")
    params = preset.source_row["param_map"]
    expected = {field: {"source_value": params[field], "role": role}
                for field, role in (("model", "checkpoint"), ("vae", "vae"))
                if field in params and params[field].strip()}
    if recipe is not None and (not isinstance(recipe, dict)
                               or not isinstance(recipe.get("resources"), list)):
        raise ValueError("A saved setup is required to verify SwarmUI resource choices.")
    seen = set()
    for item in decisions:
        if (not isinstance(item, dict) or set(item) != {
                "field", "source_value", "role", "resource_id", "sha256"}):
            raise ValueError("A SwarmUI resource choice is malformed.")
        if any(not isinstance(item[key], str) for key in item):
            raise ValueError("A SwarmUI resource choice is malformed.")
        field = item["field"]
        reference = expected.get(field)
        if (field in seen or reference is None
                or item["source_value"] != reference["source_value"]
                or item["role"] != reference["role"]
                or len(item["resource_id"]) > 256
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or not item["resource_id"]):
            raise ValueError("A SwarmUI resource choice does not match its source reference.")
        seen.add(field)
        if recipe is not None and not any(
                isinstance(row, dict) and row.get("id") == item["resource_id"] and row.get("role") == item["role"]
                and row.get("hashes", {}).get("sha256") == item["sha256"]
                for row in recipe.get("resources", [])):
            raise ValueError("A SwarmUI resource choice is not in the saved setup.")


def selected_swarm_setup_evidence(review: SwarmSetupFileReview, key: str, *,
                                  resolved_resources=()) -> dict[str, object]:
    """Retain only the selected source object and translator decisions, inertly."""
    if not isinstance(review, SwarmSetupFileReview) or review.source_dialect != SOURCE_DIALECT:
        raise ValueError("Read and review a supported SwarmUI preset file first.")
    preset = select_swarm_setup_preset(review, key)
    evidence = {
        "source_kind": "swarmui_preset_file", "source_dialect": review.source_dialect,
        "source_sha256": review.source_sha256, "selected_key": preset.key,
        "selected_row": deepcopy(preset.source_row),
        "mapped": {"positive_prompt": preset.positive_prompt,
                   "negative_prompt": preset.negative_prompt,
                   "prompt_state": preset.prompt_state,
                   "negative_prompt_state": preset.negative_prompt_state,
                   "settings": deepcopy(preset.settings)},
        "held_fields": [{"field": item.field, "reason": item.reason, "value": item.value}
                        for item in preset.held_fields],
    }
    if resolved_resources:
        evidence["resolved_resources"] = deepcopy(list(resolved_resources))
    return validate_swarm_setup_evidence(evidence)


def _composition_presets(review, keys):
    if not isinstance(review, SwarmSetupFileReview) or review.source_dialect != SOURCE_DIALECT:
        raise ValueError("Read and review a supported SwarmUI preset file first.")
    if (not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= MAX_COMPOSITION_PRESETS
            or any(not _short_text(key, required=True) for key in keys)
            or len(set(keys)) != len(keys)):
        raise ValueError("Choose one to eight unique presets in the intended order.")
    return tuple(select_swarm_setup_preset(review, key) for key in keys)


def inspect_swarm_setup_composition(review, keys) -> dict[str, object]:
    """Inspect an ordered literal subset, not SwarmUI's executable overlay rules.

    Options retain their contributing preset key. Only unequal present values
    conflict; absent/held values contribute nothing, while a literal negative
    blank is an explicit clearing choice. Size is always one complete pair.
    """
    presets = _composition_presets(review, keys)
    prompts = [preset.positive_prompt for preset in presets if preset.prompt_state == "literal"]
    positive = ", ".join(prompts) if prompts else None
    if positive is not None and len(positive) > 4096:
        raise ValueError("Combined prompt exceeds 4096 characters; remove a preset before composing.")
    fields = {field: [] for field in
              ("negative_prompt", "size", "steps", "cfg_scale", "sampler", "scheduler")}
    held = []
    for preset in presets:
        values = {field: value for field, value in preset.settings.items()
                  if field in fields}
        if "width" in preset.settings and "height" in preset.settings:
            values["size"] = {field: preset.settings[field] for field in ("width", "height")}
        if preset.negative_prompt_state == "literal":
            values["negative_prompt"] = preset.negative_prompt
        for field, value in values.items():
            fields[field].append({"key": preset.key, "value": deepcopy(value)})
        held.extend({"preset_key": preset.key, "field": item.field,
                     "reason": item.reason, "value": item.value} for item in preset.held_fields)
    conflicts = {field: deepcopy(options) for field, options in fields.items()
                 if options and any(option["value"] != options[0]["value"] for option in options[1:])}
    return {"keys": list(keys), "positive_prompt": positive, "fields": fields,
            "conflicts": conflicts, "held_fields": held}


def compose_swarm_setup_presets(review, keys, choices) -> dict[str, object]:
    """Resolve each conflict with its exact contributing preset key.

    Unknown, stale, unnecessary and missing choices are refused. No last-block
    precedence, negative-text concatenation or resource-name resolution occurs.
    This function reads no files: publication must recheck the captured review.
    """
    inspection = inspect_swarm_setup_composition(review, keys)
    if (not isinstance(choices, dict) or set(choices) != set(inspection["conflicts"])
            or any(not isinstance(value, str) for value in choices.values())):
        raise ValueError("Choose one source preset for every conflicting field, and review changed conflicts again.")
    resolved = {}
    for field, options in inspection["fields"].items():
        if not options:
            continue
        if field in choices:
            selected = next((option for option in options if option["key"] == choices[field]), None)
            if selected is None:
                raise ValueError(f"Choose a contributing source preset for {field}.")
        else:
            selected = options[0]
        resolved[field] = deepcopy(selected["value"])
    settings = {field: value for field, value in resolved.items()
                if field not in {"negative_prompt", "size"}}
    settings.update(resolved.get("size", {}))
    presets = _composition_presets(review, keys)
    return {"positive_prompt": inspection["positive_prompt"],
            "negative_prompt": resolved.get("negative_prompt"),
            "prompt_state": ("literal" if inspection["positive_prompt"] is not None else
                             "held" if any(p.prompt_state == "held" for p in presets) else "absent"),
            "negative_prompt_state": ("literal" if "negative_prompt" in resolved else
                                      "held" if any(p.negative_prompt_state == "held" for p in presets) else "absent"),
            "settings": settings}


def composed_swarm_setup_evidence(review, keys, choices, *,
                                  resolved_resources=()) -> dict[str, object]:
    """Retain selected ordered rows and explicit conflict choices, locally."""
    presets = _composition_presets(review, keys)
    evidence = {
        "source_kind": "swarmui_preset_composition", "source_dialect": review.source_dialect,
        "source_sha256": review.source_sha256,
        "selected_presets": [{"key": p.key, "row": deepcopy(p.source_row)} for p in presets],
        "choices": deepcopy(choices), "mapped": compose_swarm_setup_presets(review, keys, choices),
        "held_fields": inspect_swarm_setup_composition(review, keys)["held_fields"],
    }
    if resolved_resources:
        evidence["resolved_resources"] = deepcopy(list(resolved_resources))
    return validate_swarm_setup_evidence(evidence)


def _check_evidence_size(evidence):
    try:
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Saved SwarmUI evidence must contain bounded JSON values.") from exc
    if len(encoded) > MAX_IMPORT_EVIDENCE_BYTES:
        raise ValueError("Selected SwarmUI import evidence exceeds its local limit.")


def _held_value_key(field, value, reason):
    # Unknown source metadata is JSON rendered as inert display text. Preserve
    # its exact typed value while ignoring object-key order changed by storage.
    # Ordinary parameter strings must remain byte-for-byte distinct.
    if field.startswith("source.") and reason == "Source metadata stays in the original file.":
        try:
            value = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Saved SwarmUI held source metadata is invalid.") from exc
    return value


def _validate_composed_evidence(evidence, recipe):
    standard_keys = {"source_kind", "source_dialect", "source_sha256",
                     "selected_presets", "choices", "mapped", "held_fields"}
    if set(evidence) not in (standard_keys, standard_keys | {"resolved_resources"}):
        raise ValueError("Saved SwarmUI composition evidence has unsupported fields.")
    if (evidence["source_dialect"] != SOURCE_DIALECT
            or not isinstance(evidence["source_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["source_sha256"])):
        raise ValueError("Saved SwarmUI source identity is invalid.")
    rows = evidence["selected_presets"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_COMPOSITION_PRESETS:
        raise ValueError("Choose one to eight unique presets in the intended order.")
    _check_evidence_size(evidence)
    presets = []
    for selected in rows:
        if not isinstance(selected, dict) or set(selected) != {"key", "row"}:
            raise ValueError("Saved SwarmUI composition source row is invalid.")
        if not isinstance(selected["row"], dict):
            raise ValueError("Saved SwarmUI composition source row is invalid.")
        _bounded_tree(selected["row"])
        presets.append(_proposal(selected["key"], selected["row"]))
    # Reconstruct only the retained translator input. Never open a path supplied
    # by evidence, and never mistake this consistency check for a source recheck.
    review = SwarmSetupFileReview(Path("."), evidence["source_sha256"], (), tuple(presets))
    keys = [preset.key for preset in presets]
    mapped = compose_swarm_setup_presets(review, keys, evidence["choices"])
    # JSON comparison preserves number/boolean types that Python equality would
    # otherwise conflate (for example an injected true versus one step).
    if json.dumps(evidence["mapped"], sort_keys=True, allow_nan=False) != json.dumps(mapped, sort_keys=True, allow_nan=False):
        raise ValueError("Saved SwarmUI composition differs from its source choices.")
    held = evidence["held_fields"]
    held_keys = ("preset_key", "field", "reason", "value")
    if (not isinstance(held, list)
            or any(not isinstance(item, dict) or set(item) != set(held_keys)
                   or any(not isinstance(item[key], str) for key in held_keys)
                   for item in held)):
        raise ValueError("Saved SwarmUI composition held fields are invalid.")
    expected_held = inspect_swarm_setup_composition(review, keys)["held_fields"]
    # JSON object serialization can reorder param_map keys and therefore the
    # reconstructed omissions. Match complete rows as a multiset: ordering is
    # immaterial, but duplicate count and exact preset association are not.
    def signature(item):
        return (item["preset_key"], item["field"], item["reason"],
                _held_value_key(item["field"], item["value"], item["reason"]))
    if sorted(signature(item) for item in held) != sorted(signature(item) for item in expected_held):
        raise ValueError("Saved SwarmUI composition held fields differ from its sources.")
    if "resolved_resources" in evidence:
        _validate_composed_resolved_resources(evidence, review, keys, recipe)
    return deepcopy(evidence)


def _validate_composed_resolved_resources(evidence, review, keys, recipe):
    decisions = evidence["resolved_resources"]
    if not isinstance(decisions, list) or len(decisions) > 2:
        raise ValueError("SwarmUI composition resource choices must be a bounded list.")
    expected = {(row["field"], row["preset_key"]): row for row in
                inspect_swarm_setup_composition_resources(review, keys)}
    if recipe is not None and (not isinstance(recipe, dict)
                               or not isinstance(recipe.get("resources"), list)):
        raise ValueError("A saved setup is required to verify SwarmUI resource choices.")
    seen = set()
    for item in decisions:
        if (not isinstance(item, dict) or set(item) != {
                "preset_key", "field", "source_value", "role", "resource_id", "sha256"}
                or any(not isinstance(value, str) for value in item.values())):
            raise ValueError("A SwarmUI composition resource choice is malformed.")
        reference = expected.get((item["field"], item["preset_key"]))
        if (item["field"] in seen or reference is None
                or item["source_value"] != reference["source_value"]
                or item["role"] != reference["role"]
                or not 0 < len(item["resource_id"]) <= 256
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("A SwarmUI composition resource choice does not match its source reference.")
        seen.add(item["field"])
        if recipe is not None and not any(
                isinstance(row, dict) and row.get("id") == item["resource_id"]
                and row.get("role") == item["role"]
                and row.get("hashes", {}).get("sha256") == item["sha256"]
                for row in recipe["resources"]):
            raise ValueError("A SwarmUI composition resource choice is not in the saved setup.")


def validate_swarm_setup_evidence(evidence: dict[str, object], *, recipe=None) -> dict[str, object]:
    """Reject malformed or oversized local evidence; never interpret source syntax."""
    if isinstance(evidence, dict) and evidence.get("source_kind") == "swarmui_preset_composition":
        return _validate_composed_evidence(evidence, recipe)
    standard_keys = {
            "source_kind", "source_dialect", "source_sha256", "selected_key",
            "selected_row", "mapped", "held_fields"}
    if not isinstance(evidence, dict) or set(evidence) not in (
            standard_keys, standard_keys | {"resolved_resources"}):
        raise ValueError("Saved SwarmUI import evidence has unsupported fields.")
    if (evidence["source_kind"] != "swarmui_preset_file"
            or evidence["source_dialect"] != SOURCE_DIALECT
            or not isinstance(evidence["source_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["source_sha256"])
            or not _short_text(evidence["selected_key"], required=True)):
        raise ValueError("Saved SwarmUI source identity is invalid.")
    row = evidence["selected_row"]
    if not isinstance(row, dict) or row.get("title") != evidence["selected_key"]:
        raise ValueError("Saved SwarmUI selected preset is invalid.")
    _bounded_tree(row)
    mapped = evidence["mapped"]
    if (not isinstance(mapped, dict) or set(mapped) != {
            "positive_prompt", "negative_prompt", "prompt_state",
            "negative_prompt_state", "settings"}
            or not isinstance(mapped["prompt_state"], str)
            or mapped["prompt_state"] not in {"absent", "literal", "held"}
            or not isinstance(mapped["negative_prompt_state"], str)
            or mapped["negative_prompt_state"] not in {"absent", "literal", "held"}
            or any(value is not None and not isinstance(value, str) for value in
                   (mapped["positive_prompt"], mapped["negative_prompt"]))
            or not isinstance(mapped["settings"], dict)):
        raise ValueError("Saved SwarmUI mapping is invalid.")
    held = evidence["held_fields"]
    if (not isinstance(held, list) or len(held) > MAX_FIELDS + len(_ROW_FIELDS)
            or any(not isinstance(item, dict) or set(item) != {"field", "reason", "value"}
                   or not all(isinstance(item[key], str) for key in ("field", "reason", "value"))
                   for item in held)):
        raise ValueError("Saved SwarmUI omitted-field review is invalid.")
    projected = _proposal(evidence["selected_key"], row)
    if mapped != {"positive_prompt": projected.positive_prompt,
                  "negative_prompt": projected.negative_prompt,
                  "prompt_state": projected.prompt_state,
                  "negative_prompt_state": projected.negative_prompt_state,
                  "settings": projected.settings}:
        raise ValueError("Saved SwarmUI mapping differs from its selected source.")
    expected_held = [{"field": item.field, "reason": item.reason, "value": item.value}
                     for item in projected.held_fields]
    def signature(item):
        return (item["field"], item["reason"], _held_value_key(item["field"], item["value"], item["reason"]))
    if sorted(signature(item) for item in held) != sorted(signature(item) for item in expected_held):
        raise ValueError("Saved SwarmUI held-field review differs from its selected source.")
    if "resolved_resources" in evidence:
        _validate_resolved_resources(evidence, projected, recipe)
    _check_evidence_size(evidence)
    return deepcopy(evidence)


def recheck_swarm_setup_file(review: SwarmSetupFileReview, *,
                             should_cancel: Callable[[], bool] | None = None) -> None:
    """Hold saving if the chosen file's identity or exact content changed."""
    if not isinstance(review, SwarmSetupFileReview) or review.source_dialect != SOURCE_DIALECT:
        raise ValueError("Read and review the supported SwarmUI preset file again.")
    _source, raw, identity = _read_source(review.path, should_cancel)
    if identity != review.source_identity or hashlib.sha256(raw).hexdigest() != review.source_sha256:
        raise ValueError("SwarmUI preset file changed after review; read it again while keeping your edited setup.")
