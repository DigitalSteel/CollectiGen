"""Explicit inert LM Studio preferences in the canonical settings document.

No default profile, credential custody, network, provider activation or model
ownership. Save/Forget use the existing guarded AppSettings transaction.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from core.generation_transport import strict_json
from core.history_configuration import MAX_SETTINGS_BYTES
from core.lmstudio_discovery import MAX_ID, _text
from core.lmstudio_transport import _endpoint
from core.settings import AppSettings, SettingsRecoveryRequiredError, _settings_writer
from core.training_source_io import _assert_unredirected

KEY = "lmstudio_analysis_connection"
SCHEMA_VERSION = 1
_CREDENTIAL_FIELDS = frozenset({"token", "api_token", "api_key", "authorization",
                                "password", "credentials", "headers"})


class SettingsError(ValueError):
    """Content-free preference error; never includes settings or path values."""


class SettingsRecoveryRequired(SettingsRecoveryRequiredError):
    """Keep the canonical recovery category while hiding private diagnostics."""


@contextmanager
def _errors(code):
    try:
        yield
    except (SettingsError, SettingsRecoveryRequired):
        raise
    except SettingsRecoveryRequiredError:
        raise SettingsRecoveryRequired("settings_recovery_required") from None
    except Exception:
        raise SettingsError(code) from None


def _require(value, code):
    if not value:
        raise SettingsError(code)


@dataclass(frozen=True, slots=True)
class Selection:
    endpoint: str = field(repr=False)
    preferred_model_key: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    digest: str | None
    selection: Selection | None
    _origin: Path = field(repr=False)
    _raw: bytes | None = field(repr=False)


def _selection(payload):
    if KEY not in payload:
        return None
    value = payload[KEY]
    _require(type(value) is dict and type(value.get("schema_version")) is int
             and value["schema_version"] == SCHEMA_VERSION, "unsupported_settings_schema")
    _require(not any(key.casefold() in _CREDENTIAL_FIELDS for key in value),
             "credential_field_refused")
    endpoint = _endpoint(value.get("endpoint")).geturl()
    key = value.get("preferred_model_key")
    if key is not None:
        key = _text(key, MAX_ID, "invalid_model_preference")
    return Selection(endpoint, key)


def _encoded(payload, *, bounded):
    # Match the canonical patch writer's exact bytes. Finite validation does not
    # normalize, drop or otherwise reinterpret unrelated future fields.
    raw = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if bounded:
        _require(len(raw) <= MAX_SETTINGS_BYTES, "settings_size_limit")
    return raw


def _payload(raw):
    if raw is None:
        return {}
    value = strict_json(raw)
    _require(type(value) is dict, "settings_object_required")
    AppSettings._from_payload(value)
    _encoded(value, bounded=False)  # Also rejects overflowed JSON floats.
    return value


@dataclass(frozen=True, slots=True, init=False)
class LmStudioConnectionSettings:
    _path: Path = field(repr=False)

    def __init__(self, settings_path):
        with _errors("invalid_settings_path"):
            path = Path(settings_path)
            _require(path.is_absolute() and ".." not in path.parts,
                     "explicit_settings_path_required")
            object.__setattr__(self, "_path", path)

    def observe(self):
        """Explicit bounded read; absent authority stays absent, never repaired."""
        with _errors("settings_invalid_or_unavailable"):
            _assert_unredirected(self._path)
            try:
                with self._path.open("rb") as stream:
                    raw = stream.read(MAX_SETTINGS_BYTES + 1)
            except FileNotFoundError:
                raw = None
            _require(raw is None or len(raw) <= MAX_SETTINGS_BYTES, "settings_size_limit")
            payload = _payload(raw)
            return SettingsSnapshot(None if raw is None else hashlib.sha256(raw).hexdigest(),
                                    _selection(payload), self._path, raw)

    def save_selection(self, endpoint, *, preferred_model_key=None, expected):
        """Save a declared preference only, never a model/capability admission."""
        with _errors("invalid_selection"):
            endpoint = _endpoint(endpoint).geturl()
            if preferred_model_key is not None:
                preferred_model_key = _text(preferred_model_key, MAX_ID, "invalid_model_preference")
        return self._edit(expected, (endpoint, preferred_model_key))

    def forget_selection(self, *, expected):
        """Forget this preference only; no server stop, credential or AI action."""
        return self._edit(expected, None)

    def _edit(self, expected, selection):
        _require(type(expected) is SettingsSnapshot and expected._origin == self._path,
                 "settings_snapshot_required")
        with _errors("settings_publication_failed"):
            with _settings_writer(self._path):
                current = self.observe()
                _require(current.digest == expected.digest and current._raw == expected._raw,
                         "settings_changed_reload_setup")
                payload = _payload(current._raw)
                if selection is None:
                    if KEY not in payload:
                        return current
                    payload.pop(KEY, None)
                else:
                    previous = payload.get(KEY, {})
                    payload[KEY] = {**deepcopy(previous), "schema_version": SCHEMA_VERSION,
                                    "endpoint": selection[0], "preferred_model_key": selection[1]}
                accepted = _encoded(payload, bounded=True)
                saved = SettingsSnapshot(hashlib.sha256(accepted).hexdigest(),
                                         _selection(payload), self._path, accepted)
                writer = AppSettings._from_payload(payload)
                # Candidate validation/encoding is fallible work. Recheck the
                # reviewed state after it, before the canonical writer begins.
                latest = self.observe()
                _require(latest.digest == current.digest and latest._raw == current._raw,
                         "settings_changed_reload_setup")
                writer._save_locked(self._path, _patch_payload=payload)
                # Original transactional readback already verified these bytes.
                return saved
