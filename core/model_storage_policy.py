"""One model-storage policy over the existing registry workspace destinations.

Preferences alone never scan, copy, delete or reconfigure an engine.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re

from .model_organization import destination_path, family_destination_template, normalize_model_family
from .model_resource_registry import ModelResourceError, RESOURCE_ROLES
from .model_workspace import _path

POLICIES = ("on_demand", "keep_fast", "archive_only")
AUTOMATIC_CLEANUP_POLICIES = ("off", "least_used")
CACHE_LIMIT_MODES = ("fixed", "unlimited")


@dataclass(frozen=True)
class ModelStoragePolicy:
    mode: str = "single"
    fast_root: str = ""
    cache_limit_bytes: int = 0
    minimum_free_bytes: int = 256 * 1024**2
    default_policy: str = "on_demand"
    family_policies: tuple[tuple[str, str], ...] = ()
    role_policies: tuple[tuple[str, str], ...] = ()
    resource_policies: tuple[tuple[str, str], ...] = ()
    automatic_cleanup: str = "off"
    cache_limit_mode: str = "fixed"


@dataclass(frozen=True)
class EffectiveModelPolicy:
    policy: str
    origin: str


def _family_key(value):
    if value in ("Shared", "Upscalers", "Analysis", "Unclassified"):
        return value
    if not isinstance(value, str) or ":" not in value:
        raise ModelResourceError("Use an explicit Images or Video family policy.")
    media, family = value.split(":", 1)
    if media not in ("Images", "Video") or not family.strip() or len(family) > 160 or any(c in family for c in '/\\:'):
        raise ModelResourceError("Use an explicit Images or Video family policy.")
    return media + ":" + normalize_model_family(family)


def load_policy(workspace) -> ModelStoragePolicy:
    raw = workspace.get("storage_policy", {})
    if not isinstance(raw, dict) or (raw and (type(raw.get("schema_version")) is not int or raw.get("schema_version") not in (1, 2))):
        raise ModelResourceError("Model storage policy is invalid or from a newer version; preserve it for review.")
    mode = raw.get("mode", "single")
    root = raw.get("fast_root", "")
    cap, reserve = raw.get("cache_limit_bytes", 0), raw.get("minimum_free_bytes", 256 * 1024**2)
    default = raw.get("default_policy", "on_demand")
    automatic_cleanup = raw.get("automatic_cleanup", "off")
    cache_limit_mode = raw.get("cache_limit_mode", "fixed") if raw.get("schema_version") == 2 else "fixed"
    if raw.get("schema_version") != 2 and "cache_limit_mode" in raw:
        raise ModelResourceError("A cache-limit choice needs the supported version 2 policy; preserve it for review.")
    if mode not in ("single", "tiered") or not isinstance(root, str) or len(root) > 4096 or default not in POLICIES:
        raise ModelResourceError("Choose a supported model storage mode and default policy.")
    if any(type(v) is not int or not 0 <= v <= 1024**5 for v in (cap, reserve)):
        raise ModelResourceError("Cache limit and free-space reserve must be nonnegative byte counts.")
    if type(automatic_cleanup) is not str or automatic_cleanup not in AUTOMATIC_CLEANUP_POLICIES:
        raise ModelResourceError("Choose a supported automatic Fast storage cleanup policy.")
    if type(cache_limit_mode) is not str or cache_limit_mode not in CACHE_LIMIT_MODES:
        raise ModelResourceError("Choose a supported fixed or no-fixed-cap cache limit.")
    if mode == "tiered" and (not root or not Path(root).is_absolute() or not cap):
        raise ModelResourceError("Tiered storage needs an absolute Fast folder and a positive cache limit.")
    groups = []
    for name in ("family_policies", "role_policies", "resource_policies"):
        values = raw.get(name, {})
        if not isinstance(values, dict) or len(values) > 20000:
            raise ModelResourceError("Model policy overrides exceed the supported bound.")
        selected = {}
        for key, policy in values.items():
            if policy not in POLICIES or not isinstance(key, str):
                raise ModelResourceError("Choose a supported model retention policy.")
            if name == "family_policies":
                key = _family_key(key)
            elif name == "role_policies" and key not in RESOURCE_ROLES:
                raise ModelResourceError("Choose a supported model role.")
            elif name == "resource_policies" and not re.fullmatch(r"sha256:[0-9a-f]{64}", key):
                raise ModelResourceError("Choose an exact registered standalone model identity.")
            folded = key.casefold()
            if folded in selected:
                raise ModelResourceError("Configure one policy for each canonical family, role or identity.")
            selected[folded] = (key, policy)
        groups.append(tuple(sorted(selected.values())))
    return ModelStoragePolicy(mode, root, cap, reserve, default, *groups, automatic_cleanup, cache_limit_mode)


def effective_policy(policy, *, resource_id, role, family, media_type="Images"):
    if role not in RESOURCE_ROLES or media_type not in ("Images", "Video"):
        raise ModelResourceError("Choose an explicit supported role and media type.")
    family_key = "Shared" if family == "Shared" else media_type + ":" + normalize_model_family(family)
    if role == "upscaler":
        family_key = "Upscalers"
    elif role in ("tagger", "captioner"):
        family_key = "Analysis"
    for values, key, label in ((policy.resource_policies, resource_id, "exact resource"),
            (policy.family_policies, family_key, "media/family"), (policy.role_policies, role, "model role")):
        found = next((value for saved, value in values if saved.casefold() == key.casefold()), None)
        if found is not None:
            return EffectiveModelPolicy(found, label)
    return EffectiveModelPolicy(policy.default_policy, "global default")


def fast_destination(policy, *, resource_id, role, family, media_type="Images") -> Path:
    if policy.mode != "tiered" or not re.fullmatch(r"sha256:[0-9a-f]{64}", resource_id):
        raise ModelResourceError("Prepare an exact standalone resource using tiered storage.")
    effective_policy(policy, resource_id=resource_id, role=role, family=family, media_type=media_type)
    layout = family_destination_template(policy.fast_root)
    # Unknown declared families use the existing Unclassified destination;
    # untrusted labels are never converted into arbitrary path components.
    directory, _ = destination_path({"family_destinations": layout}, role=role, family=family, media_type=media_type)
    return Path(directory) / (resource_id[7:] + ".safetensors")


def validate_policy_roots(workspace, *, protected_roots=(), managed_roots=()):
    policy = load_policy(workspace)
    if not policy.fast_root:
        return policy
    permanent = tuple(p for p in (workspace.get("default_destination", ""),
        *workspace.get("family_destinations", {}).values(), *workspace.get("destination_overrides", {}).values()) if p)
    # Guard lexically before any stat or filesystem inspection.
    _path(policy.fast_root, (*protected_roots, *managed_roots, *permanent))
    return policy


def root_generation(root):
    """Stable lexical key; does not resolve aliases or touch the filesystem."""
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        raise ModelResourceError("A retained model storage root must be an absolute path.")
    return hashlib.sha256(str(Path(root).absolute()).casefold().encode("utf-8")).hexdigest()


def model_storage_roots(snapshot):
    """Pure complete storage-boundary projection, including retired generations."""
    workspace = snapshot.get("workspace", {})
    if not isinstance(workspace, dict):
        raise ModelResourceError("Model workspace settings are invalid.")
    policy = load_policy(workspace)
    roots = [workspace.get("default_destination", "")]
    for name in ("family_destinations", "destination_overrides"):
        mapping = workspace.get(name, {})
        if not isinstance(mapping, dict):
            raise ModelResourceError("Model destination settings are invalid.")
        roots.extend(mapping.values())
    roots.append(policy.fast_root)
    state = snapshot.get("model_cache")
    if "model_cache" in snapshot and state is None:
        raise ModelResourceError("Cache boundary metadata is malformed; preserve retained roots for review.")
    if state is not None:
        if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state.get("schema_version") != 1:
            raise ModelResourceError("Unsupported cache boundary metadata must be preserved for review.")
        generations = state.get("root_generations", {})
        if not isinstance(generations, dict) or len(generations) > 10000:
            raise ModelResourceError("Retained cache roots are invalid or exceed their supported bound.")
        for key, record in generations.items():
            if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record.get("schema_version") != 1 or root_generation(record.get("root")) != key:
                raise ModelResourceError("A retained cache root changed or is unsupported.")
            roots.append(record["root"])
        for name in ("entries", "operations", "leases"):
            table = state.get(name)
            if not isinstance(table, dict) or len(table) > 10000 or any(not isinstance(record, dict) for record in table.values()):
                raise ModelResourceError("Cache boundary metadata is malformed; preserve every retained root.")
            for record in table.values():
                if name == "entries" or name == "operations" and record.get("kind") == "prepare":
                    key = record.get("root_generation")
                    if not isinstance(key, str) or key not in generations:
                        raise ModelResourceError("A cache record references an unavailable root generation.")
                    if name == "entries":
                        path = record.get("path")
                        if not isinstance(path, str) or not Path(path).is_absolute() or Path(generations[key]["root"]).absolute() not in Path(path).absolute().parents:
                            raise ModelResourceError("A cache location escapes its retained storage root.")
                elif name == "operations" and record.get("kind") != "cleanup":
                    raise ModelResourceError("A cache operation has unsupported boundary semantics.")
                if name in ("leases", "operations"):
                    ids = record.get("entry_ids")
                    if not isinstance(ids, list) or any(not isinstance(key, str) or key not in state["entries"] for key in ids):
                        raise ModelResourceError("A cache operation or pin lost its exact retained entries.")
    unique = {}
    for root in roots:
        if root == "":
            continue
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ModelResourceError("Model storage boundaries must contain absolute paths.")
        unique.setdefault(str(Path(root).absolute()).casefold(), str(Path(root).absolute()))
    return tuple(unique.values())


def retained_fast_roots(snapshot):
    """Pure current/retained Fast roots ONLY, after complete boundary validation."""
    model_storage_roots(snapshot)
    policy = load_policy(snapshot.get("workspace", {}))
    candidates = [policy.fast_root, *(record["root"] for record in
        snapshot.get("model_cache", {}).get("root_generations", {}).values())]
    unique = {}
    for root in candidates:
        if root:
            unique.setdefault(str(Path(root).absolute()).casefold(), str(Path(root).absolute()))
    return tuple(unique.values())


def prepare_policy_update(snapshot, changes, *, protected_roots=(), managed_roots=()):
    """Return a validated copy for one caller-owned atomic registry publication.

    No locks or writes. Callers first project proposed permanent destinations
    into snapshot.workspace, then use this helper while holding cache->registry.
    """
    from .model_use_leases import cache_state
    if not isinstance(changes, dict):
        raise ModelResourceError("Supply reviewed model storage preferences.")
    model_storage_roots(snapshot)  # never silently drop malformed old boundaries
    value = deepcopy(snapshot)
    workspace = value.setdefault("workspace", {})
    old = load_policy(workspace)
    raw = deepcopy(workspace.get("storage_policy", {}))
    raw.update(deepcopy(changes))
    raw.setdefault("schema_version", 1)
    workspace["storage_policy"] = raw
    policy = validate_policy_roots(workspace, protected_roots=protected_roots, managed_roots=managed_roots)
    if old.fast_root or policy.fast_root or "model_cache" in value:
        state = cache_state(value, create=True)
        generations = state.setdefault("root_generations", {})
        for root in (old.fast_root, policy.fast_root):
            if root:
                generations.setdefault(root_generation(root), {"schema_version": 1, "root": str(Path(root).absolute())})
        fast_roots = [row["root"] for row in generations.values()]
        permanent = (workspace.get("default_destination", ""), *workspace.get("family_destinations", {}).values(), *workspace.get("destination_overrides", {}).values())
        for root in fast_roots:
            _path(root, (*protected_roots, *managed_roots, *(p for p in permanent if p)))
        for index, root in enumerate(fast_roots):
            _path(root, fast_roots[:index])
    for identity, _ in policy.resource_policies:
        if identity not in value["resources"]:
            raise ModelResourceError("Resource policy references an unregistered model.")
    model_storage_roots(value)
    return value


def save_policy(registry, changes, *, expected_registry, protected_roots=(), managed_roots=()):
    from .model_use_leases import cache_lock
    if not isinstance(changes, dict):
        raise ModelResourceError("Supply reviewed model storage preferences.")
    with cache_lock(registry, protected_roots=protected_roots), registry._writer():
        value, token = registry.snapshot()
        if token != expected_registry:
            raise ModelResourceError("Model storage settings changed. Reload before saving.")
        value = prepare_policy_update(value, changes, protected_roots=protected_roots, managed_roots=managed_roots)
        registry._save(value, token)
        return registry.snapshot()[1]
