"""Optional, non-persistent identity inventory; never lifecycle or memory authority."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
import unicodedata
from uuid import UUID

ROUTE = "CollectiGenReadOnlyInventory"
SCHEMA = "collectigen.swarm.inventory.v1"
COMPANION_VERSION = "0.1.0"
API_PIN = "d8be0f949b602495616d15574b7b8a9504e4b711"
MAX_BYTES = 65536
MAX_ITEMS = 64
PATH_PROVENANCE = "effective-public-runtime-field"
UNAVAILABLE = (
    "Optional runtime companion: unavailable (absent, denied, incompatible or stale). "
    "Stock connection and generation remain available."
)


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("Unsupported inventory fields.")
    return value


def _text(value, limit=256):
    if (type(value) is not str or not 0 < len(value) <= limit
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise ValueError("Unsupported inventory string.")
    return value


def _guid(value):
    _text(value, 36)
    if str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError("Unsupported identity.")


def _time(value):
    _text(value, 40)
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("Inventory timestamps must be UTC.")
    return stamp


def validate_inventory(payload, *, server_id, nonce, now):
    """Validate the whole bounded response before returning any authoritative field."""
    _object(payload, ("schema", "companion_version", "api_pin", "nonce", "server_id",
                      "captured_at", "process", "paths", "extensions", "backends",
                      "source_identity"))
    if len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Inventory exceeds response budget.")
    if (payload["schema"] != SCHEMA or payload["companion_version"] != COMPANION_VERSION
            or payload["api_pin"] != API_PIN or payload["nonce"] != nonce
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or payload["server_id"] != server_id or payload["source_identity"] is not None):
        raise ValueError("Unsupported or different server inventory.")
    _guid(server_id)
    captured = _time(payload["captured_at"])
    if now.utcoffset() is None or not -5 <= (now - captured).total_seconds() <= 30:
        raise ValueError("Stale inventory.")
    process = _object(payload["process"], ("pid", "started_at"))
    if type(process["pid"]) is not int or not 1 <= process["pid"] <= 2**31 - 1:
        raise ValueError("Unsupported process identity.")
    if _time(process["started_at"]) > captured:
        raise ValueError("Process incarnation is newer than the observation.")
    paths = _object(payload["paths"], ("data", "settings", "backends"))
    for path in paths.values():
        _object(path, ("value", "provenance"))
        if path["provenance"] != PATH_PROVENANCE:
            raise ValueError("Unknown path provenance.")
        if path["value"] is not None:
            _text(path["value"], 2048)
            # A path is display-only: never resolve, inspect, or pass it to a writer.
            if not (path["value"].startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path["value"])
                    or path["value"].startswith("\\\\")):
                raise ValueError("Effective path must be absolute.")
    for kind in ("extensions", "backends"):
        rows = payload[kind]
        if type(rows) is not list or len(rows) > MAX_ITEMS:
            raise ValueError("Inventory exceeds item budget.")
        identities = set()
        for row in rows:
            if kind == "extensions":
                _object(row, ("name", "reported_version", "module_id"))
                _text(row["name"])
                if row["reported_version"] is not None:
                    _text(row["reported_version"])
                identity = (row["name"], row["module_id"])
            else:
                _object(row, ("id", "type", "status", "enabled", "module_id", "native_version"))
                if type(row["id"]) is not int or not 0 <= row["id"] <= 2**31 - 1:
                    raise ValueError("Unsupported backend ID.")
                for field in ("type", "status"):
                    _text(row[field])
                if type(row["enabled"]) is not bool or row["native_version"] is not None:
                    raise ValueError("Unsupported backend evidence.")
                identity = row["id"]
            _guid(row["module_id"])
            if identity in identities:
                raise ValueError("Duplicate inventory identity.")
            identities.add(identity)
    return payload


def review_companion_inventory(transport, session, *, now=None, nonce=None):
    """One best-effort request on the existing explicit Installation Review path."""
    try:
        challenge = secrets.token_hex(16) if nonce is None else nonce
        payload = transport.post(ROUTE, {"session_id": session["session_id"], "nonce": challenge},
                                 timeout=2, max_bytes=MAX_BYTES)
        result = validate_inventory(payload, server_id=session["server_id"], nonce=challenge,
                                    now=now or datetime.now(timezone.utc))
    except Exception:
        # Do not expose response bodies, exceptions, arbitrary fields or partial claims.
        return [UNAVAILABLE]
    lines = [
        "Optional runtime companion: fresh identity snapshot (self-reported; no control authority).",
        f"Companion server incarnation: {result['server_id']}; observed {result['captured_at']}.",
        f"Companion process: PID {result['process']['pid']}; started {result['process']['started_at']}.",
    ]
    for field, value in result["paths"].items():
        lines.append(f"Effective runtime {field} path: {value['value'] or 'unavailable'} (public runtime field; not a backup target).")
    lines.append("Loaded extensions (reported versions and loaded module IDs; not source-byte proof):")
    lines.extend(f"- {r['name']}: reported version {r['reported_version'] or 'unavailable'}; module {r['module_id']}."
                 for r in result["extensions"])
    if not result["extensions"]:
        lines.append("- None reported.")
    lines.append("Loaded backend wrappers (native engine versions unavailable):")
    lines.extend(f"- {r['id']}: {r['type']}; {r['status']}; enabled {r['enabled']}; module {r['module_id']}."
                 for r in result["backends"])
    if not result["backends"]:
        lines.append("- None reported.")
    lines.append("Source-byte identity and native backend versions: unavailable. This snapshot does not qualify memory peaks, exclusive process control, backup, restore or updates.")
    return lines
