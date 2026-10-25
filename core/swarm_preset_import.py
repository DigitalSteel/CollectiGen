"""Reviewed pinned Swarm preset projection; transport is an injected owner."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re

from .generation_prompt_templates import PromptPreset, imported_prompt_kind
from .generation_saved_work import PromptPresetCollection

PINNED_REVISION = "ec823c3f750ce9b5103e623f3ad9918d02b8ddde"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _cancel(cancelled):
    if callable(cancelled) and cancelled():
        raise InterruptedError("Preset import cancelled; completed items remain in the collection.")


def _projection(value):
    keys = {"schema_version", "source_revision", "server_fingerprint", "account_fingerprint", "presets"}
    if (not isinstance(value, dict) or set(value) != keys or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("source_revision") != PINNED_REVISION
            or any(not isinstance(value.get(key), str) or not _HASH.fullmatch(value[key])
                for key in ("server_fingerprint", "account_fingerprint"))
            or not isinstance(value.get("presets"), list) or len(value["presets"]) > 256):
        raise ValueError("The selected Swarm preset projection or source identity is unsupported.")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise ValueError("The selected preset category exceeds its bounded size.")
    return deepcopy(value)


def _source_key(projection, row):
    if (not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key]
            or len(row[key]) > 128 for key in ("author", "title"))):
        raise ValueError("A source preset needs a bounded exact author and title.")
    return _digest({key: projection[key] for key in ("source_revision", "server_fingerprint", "account_fingerprint")} |
                   {"author": row["author"], "title": row["title"]})


def _translate(projection, row, source_key):
    params = row.get("param_map")
    if (not isinstance(params, dict) or len(params) > 128
            or any(not isinstance(key, str) or len(key) > 256 or not isinstance(value, str)
                   for key, value in params.items())):
        raise ValueError("The source parameter map is malformed.")
    preview = row.get("preview_image", "")
    if not isinstance(preview, str) or len(preview) > 2048 or preview.casefold().startswith("data:"):
        raise ValueError("Embedded or oversized preview artwork is not imported.")
    fields, sides, settings, held = {}, {}, {}, []
    for source, target in (("prompt", "positive_prompt"), ("negativeprompt", "negative_prompt")):
        text = params.get(source, "")
        fields[target] = text
        sides[target] = imported_prompt_kind(text) if source in params else "absent"
        if sides[target] == "held": held.append(source)
    mapping = {"steps": "steps", "seed": "seed", "width": "width", "height": "height",
               "cfgscale": "cfg_scale", "sampler": "sampler", "scheduler": "scheduler"}
    for key, value in params.items():
        if key in {"prompt", "negativeprompt"}: continue
        target = mapping.get(key)
        parsed = None
        if key in {"steps", "seed", "width", "height"} and re.fullmatch(r"[0-9]{1,19}", value):
            number = int(value)
            if (key == "steps" and 1 <= number <= 150 or key == "seed" and 0 <= number < 2**63
                    or key in {"width", "height"} and 64 <= number <= 2048 and number % 64 == 0):
                parsed = number
        elif key == "cfgscale" and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            number = float(value)
            if math.isfinite(number) and 0 <= number <= 100: parsed = number
        elif key in {"sampler", "scheduler"} and re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", value):
            parsed = value
        if target is None or parsed is None:
            held.append(key)
        else:
            settings[target] = parsed
    content = _digest(row)
    return PromptPreset.from_dict({"schema_version": 2, "preset_id": "swarm_" + source_key[:56],
        "name": row["title"], **fields, "settings": settings,
        "import_semantics": {"version": 1, "sides": sides, "held_fields": held},
        "swarm_import": {"version": 1, "translator_version": 1, "source_key": source_key,
            "source_digest": content, "source_revision": projection["source_revision"],
            "server_fingerprint": projection["server_fingerprint"],
            "account_fingerprint": projection["account_fingerprint"],
            "source": deepcopy(row), "imported_content": [content]}})


class SwarmPresetImportService:
    def __init__(self, collection: PromptPresetCollection, source_reader):
        self.collection = collection
        self.source_reader = source_reader

    def read(self, *, consent=False, cancelled=None):
        if consent is not True:
            raise ValueError("Accept the disclosed combined Swarm account/preset read first.")
        _cancel(cancelled)
        result = _projection(self.source_reader(consent=True, cancelled=cancelled))
        _cancel(cancelled)
        return result

    def candidates(self, projection):
        projection = _projection(projection)
        result, keys = [], []
        for row in projection["presets"]:
            source_key = _source_key(projection, row)
            keys.append(source_key)
            try:
                preset = _translate(projection, row, source_key)
                held = list(preset.extra_fields["import_semantics"]["held_fields"])
            except (ValueError, TypeError, OverflowError):
                preset, held = None, ["Source row cannot be translated within the supported bounds"]
            result.append({"source_key": source_key, "title": row["title"], "source_digest": _digest(row),
                           "preset": preset, "held": held})
        for row in result:
            if keys.count(row["source_key"]) > 1:
                row["preset"] = None; row["held"] = ["Duplicate source author/title identity; resolve it in Swarm first"]
        return result

    def preview(self, projection, selected_keys, *, actions=None):
        projection = _projection(projection)
        if (not isinstance(selected_keys, (list, tuple)) or not 1 <= len(selected_keys) <= 32
                or any(not isinstance(key, str) for key in selected_keys)
                or len(set(selected_keys)) != len(selected_keys)):
            raise ValueError("Choose between one and 32 distinct source presets.")
        actions = dict(actions or {})
        if set(actions) - set(selected_keys) or any(action not in {"keep_local", "update", "copy"} for action in actions.values()):
            raise ValueError("Choose a reviewed action for each selected source preset.")
        rows = {row["source_key"]: row for row in self.candidates(projection)}
        if set(selected_keys) - set(rows):
            raise ValueError("Choose presets from this exact source projection.")
        operations, held = [], []
        for key in selected_keys:
            row = rows[key]
            if row["preset"] is None:
                held.append({"source_key": key, "title": row["title"], "reasons": row["held"]}); continue
            incoming = row["preset"]
            path = self.collection._path(incoming.preset_id)
            current = self.collection.load(incoming.preset_id) if path.exists() else None
            action = actions.get(key, "keep_local" if current else "create")
            if current is None and action != "copy":
                action = "create"
            before = current["preset"].to_dict() if current else None
            if current:
                claim = current["preset"].extra_fields.get("swarm_import", {})
                if not isinstance(claim, dict) or not isinstance(claim.get("source"), dict):
                    raise ValueError("The local preset source identity or import history needs review.")
                history = claim.get("imported_content")
                if (claim.get("source_key") != key or not isinstance(history, list) or len(history) > 128
                        or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in history)):
                    raise ValueError("The local preset source identity or import history needs review.")
                if row["source_digest"] in history and action != "copy": action = "keep_local"
                if action == "update":
                    if len(history) >= 128:
                        raise ValueError("Preset import history is full; keep local or save a separate copy.")
                    merged = {**before, **incoming.to_dict()}
                    merged["settings"] = {**current["preset"].settings, **incoming.settings}
                    merged["swarm_import"]["imported_content"] = [*history, row["source_digest"]]
                    # A local friendly name correction is unrelated to a source
                    # parameter update and stays unless edited in the collection.
                    if current["preset"].name != claim.get("source", {}).get("title"):
                        merged["name"] = current["preset"].name
                    incoming = PromptPreset.from_dict(merged)
            review = self.collection.preview_save(incoming, action=action,
                expected_digest=current["storage_digest"] if current else None)
            operations.append({"source_key": key, "title": row["title"], "source_digest": row["source_digest"],
                "action": action, "before": before, "after": review["after"], "held_fields": row["held"], "review": review})
        result = {"schema_version": 1, "source_digest": _digest(projection),
                  "operations": operations, "held": held}
        return {**result, "review_digest": _digest(result)}

    def apply(self, review, *, confirmed=False, cancelled=None):
        if confirmed is not True:
            raise ValueError("Confirm the displayed preset imports before writing the collection.")
        if (not isinstance(review, dict) or set(review) != {"schema_version", "source_digest", "operations", "held", "review_digest"}
                or type(review.get("schema_version")) is not int or review["schema_version"] != 1
                or not isinstance(review.get("operations"), list) or len(review["operations"]) > 32
                or not isinstance(review.get("held"), list) or len(review["held"]) > 32
                or review["review_digest"] != _digest({key: value for key, value in review.items() if key != "review_digest"})):
            raise ValueError("The preset import review changed; preview again.")
        refreshed = self.read(consent=True, cancelled=cancelled)
        if _digest(refreshed) != review["source_digest"]:
            raise ValueError("Swarm preset content or account changed after review; read and review again.")
        seen = set()
        for operation in review["operations"]:
            if (not isinstance(operation, dict) or not isinstance(operation.get("source_key"), str)
                    or operation["source_key"] in seen):
                raise ValueError("Preset import operations are invalid or duplicated; preview again.")
            seen.add(operation["source_key"])
        outcomes = []
        for index, operation in enumerate(review["operations"]):
            try:
                _cancel(cancelled)
                if index:
                    refreshed = self.read(consent=True, cancelled=cancelled)
                    if _digest(refreshed) != review["source_digest"]:
                        raise ValueError("Swarm preset content or account changed during import.")
                self._revalidate_operation(refreshed, operation)
                outcome = self.collection.apply(operation["review"], confirmed=True, cancelled=cancelled)
            except (ValueError, RuntimeError, OSError, InterruptedError):
                if not outcomes: raise
                return {"state": "partial", "outcomes": outcomes, "held": deepcopy(review["held"]),
                    "message": "Import stopped. Completed items remain saved; review pending collection recovery and preview remaining items again."}
            outcomes.append({**outcome, "action": operation["action"], "source_key": operation["source_key"]})
        return {"state": "completed" if not review["held"] else "partially_held", "outcomes": outcomes,
                "held": deepcopy(review["held"])}

    def _revalidate_operation(self, projection, operation):
        """Re-derive semantics; a caller-recomputed review hash grants no authority."""
        key = operation.get("source_key")
        action = operation.get("action")
        if not isinstance(action, str) or action not in {"create", "keep_local", "update", "copy"}:
            raise ValueError("Preset import action changed; preview again.")
        regenerated = self.preview(projection, [key], actions={} if action == "create" else {key: action})
        if len(regenerated["operations"]) != 1:
            raise ValueError("Preset source is no longer translatable; preview again.")
        expected = deepcopy(regenerated["operations"][0])
        checked = self.collection._check_review(operation.get("review"))
        actual = deepcopy(operation)
        if action == "copy":
            identifier = checked["item_id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
                raise ValueError("The reviewed separate-copy identity is invalid.")
            expected["after"]["preset_id"] = identifier
            expected["review"]["item_id"] = identifier
            expected["review"]["after"]["preset_id"] = identifier
        expected["review"].pop("review_digest")
        actual["review"].pop("review_digest")
        if actual != expected:
            raise ValueError("Preset import content or destination changed; preview again.")
