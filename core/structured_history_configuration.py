"""Explicit private enrollment for saved global data; metadata-only until guarded use."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .history_configuration import (
    HistoryConfigurationError, MAX_BINDINGS, MAX_SETTINGS_BYTES,
    check_storage, history_roots, known_sync_roots, overlaps, path_key, role_roots,
)

DOMAIN_VERSIONS = {"collections": 1, "allowed_details": 1}
DOMAIN_LABELS = {"collections": "Training Collections", "allowed_details": "Allowed Details policies"}


def authority_paths(settings_path):
    root = path_key(settings_path).parent
    return {
        "collections": {"collections": str(root / "concept_groups.json")},
        "allowed_details": {"tags": str(root / "tags.json"), "relationships": str(root / "relationships.json")},
    }


def configuration(value):
    if value == {}:
        return {"schema_version": 1, "zones": {}, "bindings": {}}
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or not isinstance(value.get("zones"), dict)
            or not isinstance(value.get("bindings"), dict)
            or len(value["zones"]) > MAX_BINDINGS or len(value["bindings"]) > MAX_BINDINGS):
        raise HistoryConfigurationError("Saved global-data history configuration is unsupported and remains unchanged.")
    for key, zone in value["zones"].items():
        if not isinstance(zone, dict) or not key or zone.get("zone_id") != key or zone.get("role") != "private_local":
            raise HistoryConfigurationError("An unsupported global-history storage role remains read-only.")
        path_key(zone.get("zone_root", ""))
    active = 0
    for key, binding in value["bindings"].items():
        if (not isinstance(binding, dict) or not key or binding.get("binding_id") != key
                or binding.get("zone_id") not in value["zones"]
                or type(binding.get("generation")) is not int or binding["generation"] < 1
                or not isinstance(binding.get("profile_authority_id"), str) or not binding["profile_authority_id"]
                or type(binding.get("enabled")) is not bool or type(binding.get("retired")) is not bool
                or not isinstance(binding.get("domains"), dict) or not binding["domains"]):
            raise HistoryConfigurationError("An unsupported global-history binding remains read-only.")
        path_key(binding.get("profile_settings_path", "")); path_key(binding.get("history_root", ""))
        for domain, coverage in binding["domains"].items():
            if (domain not in DOMAIN_VERSIONS or not isinstance(coverage, dict)
                    or type(coverage.get("adapter_version")) is not int
                    or coverage["adapter_version"] != DOMAIN_VERSIONS[domain]
                    or type(coverage.get("enabled")) is not bool
                    or not isinstance(coverage.get("authorities"), dict) or not coverage["authorities"]):
                raise HistoryConfigurationError("A global-history domain or adapter version is unsupported and remains read-only.")
            for identity, location in coverage["authorities"].items():
                if not isinstance(identity, str) or not identity:
                    raise HistoryConfigurationError("Global-history authority identity is missing.")
                path_key(location)
        if not binding["retired"]: active += 1
        if binding["retired"] and binding["enabled"]:
            raise HistoryConfigurationError("A retired global-history store cannot be enabled.")
    if active > 1:
        raise HistoryConfigurationError("More than one current global-history binding claims this profile.")
    return deepcopy(value)


def structured_history_roots(value):
    return tuple(row["history_root"] for row in configuration(value)["bindings"].values())


def validate_configuration(value, settings, *, settings_path, extra_roots=()):
    value = configuration(value)
    protected = (*role_roots(settings), *history_roots(settings.history_storage),
        str(path_key(settings_path).parent), *tuple(root for root in extra_roots if root))
    seen = []
    for binding in value["bindings"].values():
        history = path_key(binding["history_root"])
        zone = path_key(value["zones"][binding["zone_id"]]["zone_root"])
        if str(zone).startswith("\\\\") or str(history).startswith("\\\\") or zone == Path(zone.anchor):
            raise HistoryConfigurationError("Choose a bounded machine-local private zone, not a network or drive-wide folder.")
        if not history.is_relative_to(zone):
            raise HistoryConfigurationError("Global history must stay inside its explicitly selected private zone.")
        shared = known_sync_roots() + tuple(profile.root for profile in settings.library_registry().profiles
            if profile.root and profile.role in {"network_shared", "cloud_synced"})
        if any(overlaps(zone, root) for root in shared):
            raise HistoryConfigurationError("Global history needs private storage outside known shared or synchronized folders.")
        authorities = tuple(location for coverage in binding["domains"].values() for location in coverage["authorities"].values())
        if any(overlaps(history, root) for root in (*protected, *authorities)):
            raise HistoryConfigurationError("Keep global history separate from the profile, image history, libraries, models and other storage roles.")
        if any(overlaps(history, root) for root in seen):
            raise HistoryConfigurationError("Retained global-history stores cannot be reused or overlapped.")
        seen.append(history)
    return value


def authority_signature(settings, settings_path, authorities=None):
    """Metadata declarations only; never read the canonical domain files."""
    return json.dumps({"profile": str(path_key(settings_path)),
        "authorities": authorities if authorities is not None else authority_paths(settings_path),
        "roles": sorted(role_roots(settings)), "image_history": settings.history_storage,
        "shared": sorted(known_sync_roots())}, sort_keys=True, ensure_ascii=False)


def stage_binding(settings, value, settings_path, zone_root, history_root, *, domains,
                  private_local, enabled=True, authorities=None):
    value = configuration(value)
    if not private_local:
        raise HistoryConfigurationError("Explicitly designate private machine-local storage that is not shared or synchronized.")
    selected = tuple(sorted(set(domains)))
    if not selected or any(domain not in DOMAIN_VERSIONS for domain in selected):
        raise HistoryConfigurationError("Choose the supported global-data domains to record.")
    declared = authorities if authorities is not None else authority_paths(settings_path)
    if any(domain not in declared for domain in selected):
        raise HistoryConfigurationError("The selected domain has no current canonical authority declaration.")
    profile = str(path_key(settings_path)); path_key(zone_root); path_key(history_root)
    old = next((item for item in value["bindings"].values() if not item["retired"]), None)
    zone = next((item for item in value["zones"].values() if path_key(item["zone_root"]) == path_key(zone_root)), None)
    if zone is None:
        zone = {"zone_id": str(uuid4()), "zone_root": str(path_key(zone_root)), "role": "private_local"}
        value["zones"][zone["zone_id"]] = zone
    same = bool(old and old["profile_settings_path"] == profile and old["zone_id"] == zone["zone_id"]
        and path_key(old["history_root"]) == path_key(history_root))
    if same and not enabled:
        old["enabled"] = False
        return validate_configuration(value, settings, settings_path=settings_path)
    coverage = {domain: {"adapter_version": DOMAIN_VERSIONS[domain], "enabled": True,
        "authorities": {key: str(path_key(path)) for key, path in declared[domain].items()}} for domain in selected}
    if same:
        for domain, item in old["domains"].items():
            if domain in coverage and item["authorities"] != coverage[domain]["authorities"]:
                raise HistoryConfigurationError("Canonical authority locations changed. Choose a separate history store for the new authority.")
        merged = deepcopy(old["domains"])
        for item in merged.values(): item["enabled"] = False
        for domain, item in coverage.items(): merged[domain] = {**merged.get(domain, {}), **item}
        old["domains"] = merged; old["enabled"] = bool(enabled)
    else:
        if old: old["retired"] = True; old["enabled"] = False
        identity = str(uuid4())
        profile_id = old["profile_authority_id"] if old and old["profile_settings_path"] == profile else str(uuid4())
        value["bindings"][identity] = {"binding_id": identity,
            "generation": old["generation"] + 1 if old else 1, "profile_authority_id": profile_id,
            "profile_settings_path": profile, "zone_id": zone["zone_id"],
            "history_root": str(path_key(history_root)), "domains": coverage,
            "enabled": bool(enabled), "retired": False}
    return validate_configuration(value, settings, settings_path=settings_path)


@dataclass(frozen=True)
class StructuredHistoryBindingResult:
    status: str
    reason: str
    domain: str
    token: str = ""
    binding_json: str = ""
    zone_json: str = ""
    read_only: bool = False

    def scope(self):
        if self.status not in {"configured", "retained"}:
            raise HistoryConfigurationError(self.reason)
        from .structured_history import DomainScopeRef
        binding, zone = json.loads(self.binding_json), json.loads(self.zone_json)
        return DomainScopeRef(binding["binding_id"], binding["generation"], binding["profile_authority_id"],
            path_key(zone["zone_root"]), path_key(binding["history_root"]),
            tuple(sorted(domain + ":" + authority for domain, item in binding["domains"].items()
                for authority in item["authorities"])), self.token,
            tuple(sorted((domain, item["adapter_version"]) for domain, item in binding["domains"].items())),
            writable_domains=() if self.read_only else (self.domain,))


class StructuredHistoryConfigurationService:
    """Lazy metadata-only resolver; guarded use reads only the exact settings file."""
    def __init__(self, settings, *, settings_path=None, authorities=None):
        from .settings import DEFAULT_SETTINGS_PATH
        self.settings = settings
        self.settings_path = path_key(settings_path or DEFAULT_SETTINGS_PATH)
        declared = authorities if authorities is not None else authority_paths(self.settings_path)
        self.authorities = {domain: {key: str(path_key(path)) for key, path in values.items()}
            for domain, values in declared.items()}
        self._stores = {}

    def resolve(self, domain):
        return self._resolve(domain, read_only=False)

    def read_descriptor(self, domain):
        """Read retained configured history without granting new write admission."""
        return self._resolve(domain, read_only=True)

    def _resolve(self, domain, *, read_only):
        try:
            if domain not in DOMAIN_VERSIONS:
                return StructuredHistoryBindingResult("unsupported", "This domain has no qualified saved-history adapter.", domain)
            value = validate_configuration(self.settings.structured_history_storage, self.settings, settings_path=self.settings_path)
            binding = next((item for item in value["bindings"].values() if not item["retired"]), None)
            if binding is None:
                return StructuredHistoryBindingResult("not_configured", "Configure saved global-data history in Setup before recording this domain.", domain)
            if binding["profile_settings_path"] != str(self.settings_path):
                return StructuredHistoryBindingResult("needs_review", "The profile location changed; explicitly configure a new global-history binding.", domain)
            coverage = binding["domains"].get(domain)
            if coverage is None:
                return StructuredHistoryBindingResult("not_configured", "This domain was not included in the selected history scope.", domain)
            if not read_only and (not binding["enabled"] or not coverage["enabled"]):
                return StructuredHistoryBindingResult("disabled", "Capture is off for this domain. Retained history stays protected.", domain)
            if coverage["authorities"] != self.authorities.get(domain):
                return StructuredHistoryBindingResult("needs_review", "Canonical authority locations changed. Review this domain's history binding.", domain)
            zone = value["zones"][binding["zone_id"]]
            material = json.dumps([binding, zone, authority_signature(self.settings, self.settings_path, self.authorities)], sort_keys=True)
            if read_only:
                material += ":retained-read-only"
            return StructuredHistoryBindingResult("retained" if read_only else "configured",
                "Retained history is read-only; capture and restoration require current enabled authority." if read_only else
                "Exact storage and domain authority are checked again before use.", domain,
                hashlib.sha256(material.encode()).hexdigest(), json.dumps(binding), json.dumps(zone), read_only)
        except (ValueError, TypeError, KeyError) as exc:
            return StructuredHistoryBindingResult("needs_review", str(exc), domain)

    def require_current(self, descriptor):
        current = self.read_descriptor(descriptor.domain) if descriptor.read_only else self.resolve(descriptor.domain)
        if current.status not in {"configured", "retained"} or current.token != descriptor.token:
            raise HistoryConfigurationError("Global history configuration changed. Reopen the operation before writing.")
        return current

    def store(self, descriptor):
        from .structured_history import StructuredHistoryStore
        scope = descriptor.scope()
        key = (descriptor.domain, descriptor.read_only)
        cached = self._stores.get(key)
        if cached is not None and cached.scope == scope:
            return cached
        @contextmanager
        def guard(selected):
            if selected != scope:
                raise HistoryConfigurationError("Global history scope changed before admission.")
            with self.write_guard(descriptor):
                yield
        store = StructuredHistoryStore(scope, authority_guard=guard)
        self._stores[key] = store
        return store

    @contextmanager
    def off_guard(self, domain):
        """Prove saved Off authority and exclude enrollment through the native write.

        Configured callers use store.locked instead; never acquire both guards.
        This reads only the exact settings file, not any retained history store.
        """
        from .settings import AppSettings, _settings_writer
        from .model_resource_registry import ModelResourceError, _bounded_read
        with _settings_writer(self.settings_path):
            try:
                if self.settings.load_report.requires_decision:
                    raise HistoryConfigurationError("Resolve settings recovery before saving Collections.")
                current = self.resolve(domain)
                if current.status not in {"not_configured", "disabled"}:
                    raise HistoryConfigurationError("History is no longer Off. Reload the Collection before saving.")
                memory_value = validate_configuration(self.settings.structured_history_storage,
                    self.settings, settings_path=self.settings_path)
                raw = _bounded_read(self.settings_path, MAX_SETTINGS_BYTES)
                if raw is None:
                    raise HistoryConfigurationError("Save the profile before using application Collection writes.")
                disk = AppSettings._from_bytes(raw)
                disk_value = validate_configuration(disk.structured_history_storage, disk,
                    settings_path=self.settings_path)
                disk_current = StructuredHistoryConfigurationService(disk,
                    settings_path=self.settings_path, authorities=self.authorities).resolve(domain)
                if (disk_current.status != current.status or disk_value != memory_value
                        or authority_signature(disk, self.settings_path, self.authorities)
                        != authority_signature(self.settings, self.settings_path, self.authorities)):
                    raise HistoryConfigurationError("Saved history enrollment or authority differs from this window. Reload Settings before saving Collections.")
            except (OSError, ValueError, TypeError, AttributeError, KeyError, ModelResourceError) as exc:
                raise HistoryConfigurationError("Collection Off admission is unavailable or changed. " + str(exc)) from exc
            yield current

    @contextmanager
    def write_guard(self, descriptor):
        from .settings import AppSettings, _settings_writer
        with _settings_writer(self.settings_path):
            current = self.require_current(descriptor)
            try:
                with self.settings_path.open("rb") as stream: raw = stream.read(MAX_SETTINGS_BYTES + 1)
                if len(raw) > MAX_SETTINGS_BYTES:
                    raise HistoryConfigurationError("Settings exceed the global-history authority limit.")
                disk = AppSettings._from_bytes(raw)
                StructuredHistoryConfigurationService(disk, settings_path=self.settings_path, authorities=self.authorities).require_current(descriptor)
                binding, zone = json.loads(current.binding_json), json.loads(current.zone_json)
                check_storage(zone["zone_root"], binding["history_root"])
            except (OSError, ValueError, TypeError) as exc:
                raise HistoryConfigurationError("Saved global-data history authority is unavailable or changed. Reload Settings before writing.") from exc
            yield current
