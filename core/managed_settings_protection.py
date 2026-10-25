"""Ordinary settings save protection; no receiver transactions or activation.

Canonical primitives and metadata checks are re-exported by the suite contracts.
"""
from copy import deepcopy
import hashlib
import json
from types import MappingProxyType

SCHEMA = 1
MANAGED_KEY = "suite_managed_settings"
GROUPS = MappingProxyType({
    "appearance.theme": ("ui_theme",),
    "appearance.accessibility": ("color_blind_mode", "show_tooltips"),
    "library.locations": ("library_profiles", "active_library_id", "library_scope_ids",
                          "dataset_directory", "working_directory", "ingest_directory"),
})
MAX_CONTRACT_BYTES = 16 * 1024 * 1024


class SharedSettingsHold(ValueError):
    """Content-free reason; no private payload/path in displayed errors."""


def require(condition, reason):
    if not condition:
        raise SharedSettingsHold(reason)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode(value):
    require(type(value) is str and len(value.encode("utf-8")) <= MAX_CONTRACT_BYTES,
            "contract_size_limit")
    try:
        return json.loads(value, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(SharedSettingsHold("nonfinite_json")))
    except (TypeError, json.JSONDecodeError, RecursionError) as error:
        raise SharedSettingsHold("invalid_contract_json") from error


def encode(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as error:
        raise SharedSettingsHold("invalid_contract_value") from error
    decode(result)
    return result


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()



def metadata(payload):
    value = payload.get(MANAGED_KEY)
    if value is None:
        return {"schema": SCHEMA, "groups": {}, "receipts": {}}
    require(type(value) is dict and type(value.get("schema")) is int
            and value.get("schema") == SCHEMA, "unsupported_managed_schema")
    require(type(value.get("groups")) is dict and type(value.get("receipts")) is dict,
            "invalid_managed_metadata")
    require(len(value["receipts"]) <= 128, "receipt_capacity_hold")
    require(type(value.get("binding")) is str and len(value["binding"]) == 64
            and type(value.get("suite_id")) is str and bool(value["suite_id"])
            and type(value.get("revision")) is int and value["revision"] > 0
            and type(value.get("suite_fingerprint")) is str and len(value["suite_fingerprint"]) == 64
            and type(value.get("detached")) is bool, "invalid_managed_binding")
    for group, origin in value["groups"].items():
        require(group in GROUPS and type(origin) is dict and origin.get("mode") in {"inherited", "override"},
                "invalid_setting_origin")
        require(type(origin.get("value_fingerprint")) is str, "invalid_setting_origin")
    for intent_id, receipt in value["receipts"].items():
        require(type(intent_id) is str and len(intent_id) == 64 and type(receipt) is dict
                and receipt.get("intent_id") == intent_id and type(receipt.get("activated")) is bool
                and type(receipt.get("groups")) is list
                and all(g in GROUPS for g in receipt["groups"]), "invalid_participant_receipt")
    return deepcopy(value)


def effective_group(payload, group):
    from .settings import AppSettings
    settings = AppSettings._from_payload(payload)
    return {key: deepcopy(payload[key] if key in payload else getattr(settings, key)) for key in GROUPS[group]}


def group_fingerprint(payload, group):
    return fingerprint(encode(effective_group(payload, group)))


def capture_baseline(settings):
    """Capture recognized values only; never a load-time write or private payload copy."""
    settings._suite_settings_baseline = {
        group: encode({key: deepcopy(getattr(settings, key)) for key in keys})
        for group, keys in GROUPS.items()}


def _save_metadata(payload):
    value = payload.get(MANAGED_KEY)
    if type(value) is dict and type(value.get("schema")) is int and value["schema"] > 1:
        # Future sharing is unavailable; preserve its opaque extension and every
        # currently allowlisted group while permitting unrelated ordinary saves.
        return {"groups": {group: {} for group in GROUPS}}
    return metadata(payload)


def preserve_managed_save(settings, proposed, current, *, explicit=False, raw_patch=False):
    """An unrelated stale save must preserve newer managed values AND origins.

    Deliberate changes require SettingsReceiver's explicit intent, including an
    equal-value override. Nonmanaged ordinary save behavior remains canonical.
    """
    meta = _save_metadata(current)
    if explicit:
        proposed_meta = metadata(proposed)
        require(bool(proposed_meta.get("binding")), "managed_receiver_binding_required")
        if meta.get("binding"):
            require(proposed_meta["binding"] == meta["binding"], "receiver_binding_changed")
        return proposed
    if not current.get(MANAGED_KEY):
        require(MANAGED_KEY not in proposed or explicit, "managed_enrollment_intent_required")
        return proposed
    require(proposed.get(MANAGED_KEY, current[MANAGED_KEY]) == current[MANAGED_KEY] or not raw_patch,
            "managed_metadata_intent_required")
    baseline = getattr(settings, "_suite_settings_baseline", {})
    result = deepcopy(proposed)
    for group in meta["groups"]:
        local = encode({key: deepcopy(getattr(settings, key)) for key in GROUPS[group]})
        expected = baseline.get(group, encode(effective_group(current, group)))
        if raw_patch:
            # Existing explicit writers for unrelated groups cannot bypass origins.
            require(effective_group(proposed, group) == effective_group(current, group),
                    "explicit_local_override_intent_required")
        else:
            require(local == expected, "explicit_local_override_intent_required")
        for key in GROUPS[group]:
            if key in current:
                result[key] = deepcopy(current[key])
            else:
                result.pop(key, None)
    result[MANAGED_KEY] = deepcopy(current[MANAGED_KEY])
    return result


def accept_saved_managed(settings, payload):
    if MANAGED_KEY in payload:
        settings._extra[MANAGED_KEY] = deepcopy(payload[MANAGED_KEY])
        for group in _save_metadata(payload)["groups"]:
            for key, value in effective_group(payload, group).items():
                setattr(settings, key, deepcopy(value))
    capture_baseline(settings)

