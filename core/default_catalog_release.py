"""Approved default Pack observations in the existing native default-state ledger.

Pure admission/state staging only. Canonical knowledge_pack owns all disk I/O,
participant exclusion, exact backup/read-back and recovery; no parallel store.
"""
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json

STATE_KEY = "approved_pack_releases"
RELEASE_STATE_SCHEMA = 1


def _text(value, name, maximum=8192):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError("Explicit bounded approved release " + name + " required")


def _digest(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Approved release fingerprint must be an exact SHA256")


@dataclass(frozen=True)
class ApprovedDefaultPackRelease:
    pack_id: str
    content_version: str
    released_on: str
    whats_new: tuple
    source_ref: str
    snapshot_fingerprint: str
    release_fingerprint: str
    previous_release_fingerprint: str | None = None

    def __post_init__(self):
        _text(self.pack_id, "identity", 256)
        _text(self.content_version, "content version", 256)
        _text(self.source_ref, "source")
        if type(self.released_on) is not str or date.fromisoformat(self.released_on).isoformat() != self.released_on:
            raise ValueError("Approved release date requires YYYY-MM-DD")
        if type(self.whats_new) is not tuple or len(self.whats_new) > 100:
            raise ValueError("Approved release notes require a bounded immutable tuple")
        for line in self.whats_new:
            _text(line, "notes")
        _digest(self.snapshot_fingerprint)
        _digest(self.release_fingerprint)
        metadata = (self.pack_id, self.content_version, self.snapshot_fingerprint,
                    self.released_on, self.whats_new, self.source_ref)
        expected = sha256(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if expected != self.release_fingerprint:
            raise ValueError("Approved release metadata differs from its exact release fingerprint")
        if self.previous_release_fingerprint is not None:
            _digest(self.previous_release_fingerprint)

    def encode(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def decode(cls, raw):
        if type(raw) is not str or len(raw) > 128 * 1024:
            raise ValueError("Approved release requires bounded explicit JSON")
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate approved release authority field")
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=unique)
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("Approved native release observation has unknown/missing authority fields")
        if type(value["whats_new"]) is not list:
            raise ValueError("Approved release notes require a list on the wire")
        value["whats_new"] = tuple(value["whats_new"])
        return cls(**value)


def release_records(state):
    if type(state) is not dict:
        raise ValueError("Native default tracking must be an object")
    if STATE_KEY not in state:
        return {}
    node = state[STATE_KEY]
    if (type(node) is not dict or type(node.get("schema_version")) is not int
            or node["schema_version"] != RELEASE_STATE_SCHEMA or type(node.get("releases")) is not dict):
        raise ValueError("Unsupported approved release-state ledger; preserve it and review")
    if len(node["releases"]) > 1000:
        raise ValueError("Approved release-state ledger exceeds its bound")
    return node["releases"]


def require_previous(state, approved):
    records = release_records(state)
    previous = records.get(approved.pack_id)
    if previous is not None and type(previous) is not dict:
        raise ValueError("Malformed installed approved Pack state")
    observed = previous.get("release_fingerprint") if previous else None
    if observed != approved.previous_release_fingerprint:
        raise RuntimeError("Installed approved Pack release changed or is unknown; review its exact baseline")
    if previous is not None:
        known = {key: previous.get(key) for key in ApprovedDefaultPackRelease.__dataclass_fields__}
        ApprovedDefaultPackRelease.decode(json.dumps(known))
        _digest(previous.get("native_pack_fingerprint"))
    return previous


def stage_release_state(state, approved, *, pack_fingerprint):
    require_previous(state, approved)
    _digest(pack_fingerprint)
    result = json.loads(json.dumps(state))  # Preserve every existing unknown/native field.
    result.setdefault("schema_version", 6)  # Existing default_sync ledger schema.
    node = result.setdefault(STATE_KEY, {"schema_version": RELEASE_STATE_SCHEMA, "releases": {}})
    previous = node["releases"].get(approved.pack_id, {})
    entry = dict(previous)  # Keep future fields on this approved Pack's record too.
    entry.update(json.loads(approved.encode()), native_pack_fingerprint=pack_fingerprint)
    node["releases"][approved.pack_id] = entry
    return result
