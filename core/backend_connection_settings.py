"""Minimal Ollama metadata configuration in the existing AppSettings document.

No default path, AppState, network, session/credential store or recovery replay.
Open/Save/Refresh callers explicitly request access; construction is inert.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from core.settings import AppSettings, SettingsRecoveryRequiredError, _settings_writer
from core.generation_transport import local_endpoint, strict_json
from core.history_configuration import MAX_SETTINGS_BYTES

KEY = "ollama_metadata_connection"
SCHEMA_VERSION = 1


class BackendSettingsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BackendSettingsRecoveryRequiredError(SettingsRecoveryRequiredError):
    """Preserve original recovery typing without exposing its private message."""
    code = "settings_recovery_required"

    def __init__(self):
        super().__init__(self.code)


@contextmanager
def _private_errors(code):
    try:
        yield
    except BackendSettingsError:
        raise
    except BackendSettingsRecoveryRequiredError:
        raise
    except SettingsRecoveryRequiredError:
        raise BackendSettingsRecoveryRequiredError() from None
    except Exception:
        raise BackendSettingsError(code) from None


def _require(value, code):
    if not value: raise BackendSettingsError(code)


def validated_endpoint(value):
    with _private_errors("invalid_ollama_endpoint"):
        _require(type(value) is str and value.strip() == value and value
                 and not any(c.isspace() or ord(c) < 32 for c in value), "invalid_ollama_endpoint")
        # Existing normalizer uses port 80 when omitted; metadata setup requires
        # an explicit port rather than silently inventing a selected service.
        parts = urlsplit(value)
        _require(parts.username is None and parts.password is None, "invalid_ollama_endpoint")
        _require(parts.port is not None, "explicit_ollama_port_required")
        _require(1 <= parts.port <= 65535, "invalid_ollama_endpoint")
        endpoint = local_endpoint(value)
        _require(1 <= urlsplit(endpoint).port <= 65535, "invalid_ollama_endpoint")
        return endpoint


@dataclass(frozen=True, slots=True)
class ConnectionSelection:
    endpoint: str = field(repr=False)
    endpoint_network_allowed: bool


def _selection(payload):
    value = payload.get(KEY)
    if value is None: return None
    _require(type(value) is dict and type(value.get("schema_version")) is int
             and value["schema_version"] == SCHEMA_VERSION, "unsupported_backend_settings_schema")
    _require(type(value.get("endpoint_network_allowed")) is bool, "explicit_endpoint_network_policy_required")
    return ConnectionSelection(validated_endpoint(value.get("endpoint")), value["endpoint_network_allowed"])


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    digest: str | None
    selection: ConnectionSelection | None
    _raw: bytes | None = field(repr=False)
    _payload: dict = field(repr=False)

    @property
    def backend_value(self): return deepcopy(self._payload.get(KEY))


class BackendConnectionSettings:
    """One extension of the canonical settings file, never a second endpoint store."""
    def __init__(self, settings_path):
        self.path = Path(settings_path)
        _require(self.path.is_absolute(), "explicit_settings_path_required")

    def observe(self):
        with _private_errors("settings_invalid_or_unavailable"):
            # Same redirect guard and bound as existing grouped preference edits.
            from core.training_source_io import _assert_unredirected
            _assert_unredirected(self.path)
            if self.path.exists():
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_SETTINGS_BYTES + 1)
                _require(len(raw) <= MAX_SETTINGS_BYTES, "settings_size_limit")
                payload = strict_json(raw)
                _require(type(payload) is dict, "settings_object_required")
                AppSettings._from_payload(payload)  # Reuse original schema validation, not AppState.load.
            else:
                raw, payload = None, {}
            return SettingsSnapshot(None if raw is None else hashlib.sha256(raw).hexdigest(),
                                    _selection(payload), raw, deepcopy(payload))

    def save_connection(self, endpoint, *, endpoint_network_allowed, expected):
        """Explicit Save only: under-lock stale check, original backup/rollback writer."""
        endpoint = validated_endpoint(endpoint)
        _require(type(endpoint_network_allowed) is bool, "explicit_endpoint_network_policy_required")
        _require(type(expected) is SettingsSnapshot, "settings_snapshot_required")
        with _private_errors("settings_publication_failed"):
            with _settings_writer(self.path):
                current = self.observe()
                _require(current.digest == expected.digest, "settings_changed_reload_setup")
                payload = deepcopy(current._payload)
                previous = payload.get(KEY) or {}
                # Keep valid future fields in schema 1 and unrelated settings exactly.
                payload[KEY] = {**deepcopy(previous), "schema_version": SCHEMA_VERSION,
                    "endpoint": endpoint, "endpoint_network_allowed": endpoint_network_allowed}
                # Match AppSettings._save_locked's exact publication bytes. Check
                # size before it creates history or publishes; unknown fields stay.
                attempted = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                _require(len(attempted) <= MAX_SETTINGS_BYTES, "settings_size_limit")
                saved = SettingsSnapshot(hashlib.sha256(attempted).hexdigest(),
                    _selection(payload), attempted, deepcopy(payload))
                writer = AppSettings._from_payload(payload)
                # Original transactional readback owns rollback/recovery. Avoid a
                # second, out-of-transaction readback after a successful publish.
                writer._save_locked(self.path, _patch_payload=payload)
                return saved
