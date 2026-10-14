"""Explicit per-machine history locations; configuration never reads source content."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

SOURCE_ROLES = {"local_only", "network_shared", "cloud_synced", "removable_archive", "unclassified"}
MAX_BINDINGS = 256
MAX_SETTINGS_BYTES = 16 * 1024 * 1024


class HistoryConfigurationError(ValueError):
    pass


def path_key(value):
    path = Path(str(value))
    if not str(value).strip() or not path.is_absolute() or ".." in path.parts:
        raise HistoryConfigurationError("Choose an explicit absolute folder without parent traversal.")
    return Path(os.path.normcase(os.path.normpath(str(path))))


def overlaps(first, second):
    first, second = path_key(first), path_key(second)
    return first == second or first in second.parents or second in first.parents


def configuration(value):
    if value == {}:
        return {"schema_version": 1, "zones": {}, "bindings": {}}
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or not isinstance(value.get("zones"), dict)
            or not isinstance(value.get("bindings"), dict)):
        raise HistoryConfigurationError("History configuration is unsupported. It is retained unchanged; review it with a compatible version.")
    if len(value["bindings"]) > MAX_BINDINGS or len(value["zones"]) > MAX_BINDINGS:
        raise HistoryConfigurationError("History configuration exceeds its supported scope limit.")
    for key, zone in value["zones"].items():
        if not isinstance(zone, dict) or zone.get("zone_id") != key or not key or zone.get("role") != "private_local":
            raise HistoryConfigurationError("An unknown history storage role is retained read-only.")
        path_key(zone.get("zone_root", ""))
    for key, binding in value["bindings"].items():
        if (not isinstance(binding, dict) or binding.get("binding_id") != key or not key
                or binding.get("zone_id") not in value["zones"] or binding.get("source_role") not in SOURCE_ROLES
                or not isinstance(binding.get("library_id"), str) or not binding["library_id"]
                or not isinstance(binding.get("source_key"), str) or not binding["source_key"]
                or type(binding.get("source_schema_version")) is not int or binding["source_schema_version"] != 1
                or type(binding.get("enabled")) is not bool or type(binding.get("retired")) is not bool):
            raise HistoryConfigurationError("An unsupported history binding is retained read-only.")
        path_key(binding.get("source_root", "")); path_key(binding.get("history_root", ""))
    return deepcopy(value)


def sources(settings):
    rows = [{"source_key": "library:" + item.stable_id, "library_id": item.stable_id,
        "name": item.name, "root": item.root, "role": item.role, "schema_version": item.schema_version}
        for item in settings.library_registry().profiles]
    rows.append({"source_key": "ingest", "library_id": "", "name": "Incoming Images",
        "root": str(settings.ingest_directory or ""), "role": "unclassified", "schema_version": 1})
    return rows


def source_signature(settings):
    return sorted((row["source_key"], row["root"], row["role"], row["schema_version"]) for row in sources(settings))


def known_sync_roots():
    """Known environment declarations only; never discover or inspect providers."""
    return tuple(value for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
        if (value := os.environ.get(key, "")))


def role_roots(settings):
    return tuple(row["root"] for row in sources(settings) if row["root"]) + tuple(
        str(getattr(settings, key, "") or "") for key in (
            "problems_directory", "recycle_directory", "ai_models_directory", "external_write_backup_directory")
        if getattr(settings, key, ""))


def history_roots(value):
    """Retired and disabled stores remain protected, without inspecting them."""
    return tuple(row["history_root"] for row in configuration(value)["bindings"].values())


def validate_configuration(value, settings, *, extra_roots=()):
    value = configuration(value)
    from .structured_history_configuration import structured_history_roots
    protected = role_roots(settings) + tuple(root for root in extra_roots if root) + structured_history_roots(getattr(settings, "structured_history_storage", {}))
    seen = []
    active = set()
    for binding in value["bindings"].values():
        history = path_key(binding["history_root"])
        zone = path_key(value["zones"][binding["zone_id"]]["zone_root"])
        if str(zone).startswith("\\\\") or str(history).startswith("\\\\") or zone == Path(zone.anchor):
            raise HistoryConfigurationError("Choose a bounded private folder on this computer, not a network or drive-wide zone.")
        if not history.is_relative_to(zone):
            raise HistoryConfigurationError("The history folder must stay inside its explicitly selected private zone.")
        shared_roots = known_sync_roots() + tuple(row["root"] for row in sources(settings)
            if row["root"] and row["role"] in ("cloud_synced", "network_shared"))
        if any(overlaps(zone, root) for root in shared_roots):
            raise HistoryConfigurationError("The private history zone overlaps a known synchronized folder. Choose machine-local storage outside it.")
        if any(overlaps(history, root) for root in protected) or overlaps(history, binding["source_root"]):
            raise HistoryConfigurationError("History folders must stay separate from libraries, incoming images, models and other storage roles.")
        if any(overlaps(history, other) for other in seen):
            raise HistoryConfigurationError("Each history binding needs a separate folder; retained stores cannot be reused.")
        seen.append(history)
        if not binding["retired"]:
            if binding["source_key"] in active:
                raise HistoryConfigurationError("More than one current history binding claims this source.")
            active.add(binding["source_key"])
    return value


def stage_binding(settings, value, source_key, zone_root, history_root, *, private_local, enabled=True):
    """Build a reviewable configuration only. No file or directory operations."""
    value = configuration(value)
    source = next((row for row in sources(settings) if row["source_key"] == source_key), None)
    if source is None or source["role"] not in SOURCE_ROLES or not source["root"] or source["schema_version"] != 1:
        raise HistoryConfigurationError("Configure a supported source folder before registering history.")
    if not private_local:
        raise HistoryConfigurationError("Explicitly designate this zone as private local storage that is not shared or synchronized.")
    path_key(source["root"]); path_key(zone_root); path_key(history_root)
    old = next((row for row in value["bindings"].values() if row["source_key"] == source_key and not row["retired"]), None)
    zone = next((row for row in value["zones"].values() if path_key(row["zone_root"]) == path_key(zone_root)), None)
    if zone is None:
        zone = {"zone_id": str(uuid4()), "zone_root": str(zone_root), "role": "private_local"}
        value["zones"][zone["zone_id"]] = zone
    if old and not enabled and old["zone_id"] == zone["zone_id"] and path_key(old["history_root"]) == path_key(history_root):
        old["enabled"] = False
        return validate_configuration(value, settings)
    descriptor = {"source_key": source_key, "source_root": source["root"], "source_role": source["role"], "source_schema_version": source["schema_version"],
        "zone_id": zone["zone_id"], "history_root": str(history_root)}
    if old and all(old[key] == item for key, item in descriptor.items()):
        old["enabled"] = bool(enabled)
    else:
        if old: old["retired"] = True; old["enabled"] = False
        identity = str(uuid4())
        library_id = source["library_id"] or (old["library_id"] if old and old["source_root"] == source["root"] else "ingest:" + str(uuid4()))
        value["bindings"][identity] = dict(descriptor, binding_id=identity, library_id=library_id,
            enabled=bool(enabled), retired=False)
    return validate_configuration(value, settings)


def check_storage(zone_root, history_root):
    """Only explicitly selected storage metadata, never source/library metadata."""
    from .training_source_io import _assert_unredirected
    zone, history = path_key(zone_root), path_key(history_root)
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(zone.anchor)) == 4:
            raise HistoryConfigurationError("History needs machine-local storage, not a mapped network drive.")
    for path in (zone, history):
        _assert_unredirected(path)
        existing = path
        while not existing.exists() and existing != existing.parent: existing = existing.parent
        if not existing.is_dir() or not os.access(existing, os.R_OK | os.W_OK):
            raise HistoryConfigurationError("Selected history storage is unavailable or read-only. Restore access before saving.")


@dataclass(frozen=True)
class HistoryBindingResult:
    status: str
    reason: str
    token: str = ""
    binding_json: str = ""
    zone_json: str = ""

    def scope(self):
        if self.status != "configured": raise HistoryConfigurationError(self.reason)
        from .history_versions import HistoryScope
        binding, zone = json.loads(self.binding_json), json.loads(self.zone_json)
        return HistoryScope(zone_id=zone["zone_id"], library_id=binding["library_id"],
            zone_root=Path(zone["zone_root"]), library_root=Path(binding["source_root"]),
            history_root=Path(binding["history_root"]), source_zone_root=Path(binding["source_root"]))


class HistoryConfigurationService:
    """Shared lazy resolver. Construction and resolution perform no filesystem IO."""
    def __init__(self, settings, *, settings_path=None):
        from .settings import DEFAULT_SETTINGS_PATH
        self.settings = settings
        self.settings_path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH

    def resolve(self, *, source_key="", path=None):
        try:
            value = validate_configuration(self.settings.history_storage, self.settings)
            available = sources(self.settings)
            if path is not None:
                chosen = path_key(path)
                matches = [row for row in available if row["root"] and chosen.is_relative_to(path_key(row["root"]))]
                libraries = [row for row in matches if row["source_key"] != "ingest"]
                candidates = libraries or matches
                source_key = max(candidates, key=lambda row: len(path_key(row["root"]).parts))["source_key"] if candidates else ""
            source = next((row for row in available if row["source_key"] == source_key), None)
            binding = next((row for row in value["bindings"].values() if row["source_key"] == source_key and not row["retired"]), None)
            if source is not None and source["schema_version"] != 1:
                return HistoryBindingResult("needs_review", "The source profile version is unsupported. It cannot be enrolled in history.")
            if source is None or binding is None:
                return HistoryBindingResult("not_configured", "Configure this source in Setup → Libraries & Storage → History & Versions.")
            if source["schema_version"] != 1 or binding.get("source_schema_version") != source["schema_version"]:
                return HistoryBindingResult("needs_review", "The source profile version is unsupported. Its history binding is retained unchanged.")
            if source["root"] != binding["source_root"] or source["role"] != binding["source_role"]:
                return HistoryBindingResult("needs_review", "The source folder or privacy role changed. Review its history binding in Setup.")
            if (source["library_id"] and binding["library_id"] != source["library_id"]
                    or not source["library_id"] and not binding["library_id"].startswith("ingest:")):
                return HistoryBindingResult("needs_review", "The recorded source identity does not match this source. Review its history binding in Setup.")
            if not binding["enabled"]:
                return HistoryBindingResult("disabled", "Saved-data history is disabled for this source; its retained store remains protected.")
            zone = value["zones"][binding["zone_id"]]
            material = json.dumps([binding, zone, source_signature(self.settings), sorted(role_roots(self.settings)), sorted(known_sync_roots())], sort_keys=True, ensure_ascii=False, allow_nan=False)
            return HistoryBindingResult("configured", "Storage and source authority are checked again when used.",
                hashlib.sha256(material.encode()).hexdigest(), json.dumps(binding), json.dumps(zone))
        except (ValueError, TypeError, KeyError) as exc:
            return HistoryBindingResult("needs_review", str(exc))

    def require_current(self, descriptor, *, source_key="", path=None):
        """Check current in-memory authority; writes must also use write_guard."""
        current = self.resolve(source_key=source_key, path=path)
        if current.status != "configured" or current.token != descriptor.token:
            raise HistoryConfigurationError("History configuration changed. Reopen the operation before writing.")
        return current

    @contextmanager
    def transfer_guard(self,bindings):
        """Validate both frozen endpoints under one settings lock, before stores."""
        from .settings import AppSettings,_settings_writer
        with _settings_writer(self.settings_path):
            try:
                with self.settings_path.open("rb") as handle:raw=handle.read(MAX_SETTINGS_BYTES+1)
                if len(raw)>MAX_SETTINGS_BYTES:raise HistoryConfigurationError("Settings exceed the history authority limit.")
                disk=HistoryConfigurationService(AppSettings._from_bytes(raw),settings_path=self.settings_path)
                for descriptor,path in bindings:
                    current=self.resolve(path=path); saved=disk.resolve(path=path)
                    if current!=descriptor or saved!=descriptor:
                        raise HistoryConfigurationError("Move history configuration changed. Reopen the exact move preview.")
                    if descriptor.status not in {"configured","not_configured","disabled"}:
                        raise HistoryConfigurationError(descriptor.reason)
            except (OSError,ValueError,TypeError) as exc:
                raise HistoryConfigurationError("Move history authority is unavailable or changed: "+str(exc)) from exc
            yield

    @contextmanager
    def write_guard(self, descriptor, *, source_key="", path=None):
        """Hold settings authority across one admitted write, before history locks.

        Reads only the settings document. Cooperating publishers share this lock;
        arbitrary external editors are not excluded by it.
        """
        from .settings import AppSettings, _settings_writer
        with _settings_writer(self.settings_path):
            current = self.require_current(descriptor, source_key=source_key, path=path)
            try:
                with self.settings_path.open("rb") as handle:
                    raw = handle.read(MAX_SETTINGS_BYTES + 1)
                if len(raw) > MAX_SETTINGS_BYTES:
                    raise HistoryConfigurationError("Settings exceed the supported history authority limit.")
                disk = AppSettings._from_bytes(raw)
                HistoryConfigurationService(disk, settings_path=self.settings_path).require_current(
                    descriptor, source_key=source_key, path=path)
            except (OSError, ValueError, TypeError) as exc:
                raise HistoryConfigurationError("Saved history authority is unavailable or changed. Reload Settings before writing.") from exc
            yield current
