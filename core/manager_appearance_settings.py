"""Manager-local display preferences in its explicit canonical AppSettings file.

No default profile, INI parsing, receiver activation, network or module writes.
The canonical writer retains its existing backup, readback and rollback behavior.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from core.generation_transport import strict_json
from core.history_configuration import MAX_SETTINGS_BYTES
from core.managed_settings_protection import GROUPS, metadata
from core.settings import AppSettings, _settings_writer

THEMES = ("system", "light", "dark", "midnight")
DEFAULTS = {"ui_theme": "system", "color_blind_mode": False, "show_tooltips": True}


class AppearanceSettingsError(ValueError):
    """Content-free UI reason; do not expose settings payloads or paths."""


def _values(payload):
    values = {key: payload.get(key, default) for key, default in DEFAULTS.items()}
    if (type(values["ui_theme"]) is not str or values["ui_theme"] not in THEMES
            or any(type(values[key]) is not bool for key in ("color_blind_mode", "show_tooltips"))):
        raise AppearanceSettingsError("unsupported_appearance")
    return values


@dataclass(frozen=True)
class AppearanceSnapshot:
    digest: str | None
    theme: str
    color_blind_mode: bool
    show_tooltips: bool
    managed_fields: frozenset[str]
    _payload: dict = field(repr=False, compare=False)

    @property
    def values(self):
        return dict(ui_theme=self.theme, color_blind_mode=self.color_blind_mode,
                    show_tooltips=self.show_tooltips)


def _snapshot(raw, payload):
    values = _values(payload)
    groups = metadata(payload).get("groups", {})
    managed = frozenset(key for group in groups for key in GROUPS[group])
    return AppearanceSnapshot(None if raw is None else hashlib.sha256(raw).hexdigest(),
        values["ui_theme"], values["color_blind_mode"], values["show_tooltips"],
        managed, deepcopy(payload))


class ManagerAppearanceSettings:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.suffix.casefold() == ".ini":
            raise ValueError("explicit_json_settings_path_required")

    def observe(self):
        try:
            from core.training_source_io import _assert_unredirected
            _assert_unredirected(self.path)
            raw, payload = None, {}
            if self.path.exists():
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_SETTINGS_BYTES + 1)
                if len(raw) > MAX_SETTINGS_BYTES:
                    raise AppearanceSettingsError("settings_size_limit")
                payload = strict_json(raw)
                if type(payload) is not dict:
                    raise AppearanceSettingsError("settings_object_required")
                AppSettings._from_payload(payload)
            return _snapshot(raw, payload)
        except AppearanceSettingsError:
            raise
        except Exception:
            raise AppearanceSettingsError("settings_unavailable") from None

    def save(self, changes, *, expected):
        if (type(expected) is not AppearanceSnapshot or type(changes) is not dict
                or not changes or not set(changes).issubset(DEFAULTS)):
            raise AppearanceSettingsError("appearance_changes_required")
        _values({**expected.values, **changes})
        try:
            with _settings_writer(self.path):
                current = self.observe()
                if current.digest != expected.digest:
                    raise AppearanceSettingsError("settings_changed")
                if current.managed_fields.intersection(changes):
                    raise AppearanceSettingsError("managed_appearance")
                payload = {**deepcopy(current._payload), **changes}
                attempted = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                if len(attempted) > MAX_SETTINGS_BYTES:
                    raise AppearanceSettingsError("settings_size_limit")
                saved = _snapshot(attempted, payload)
                writer = AppSettings._from_payload(payload)
                writer._save_locked(self.path, _patch_payload=payload)
                return saved
        except AppearanceSettingsError:
            raise
        except Exception:
            raise AppearanceSettingsError("appearance_save_failed") from None
