"""Pure, bounded placement suggestions; never preparation or cleanup authority.

The caller supplies exact ModelRequirementSet values from a trusted resource-only
adapter resolver. Usage identities alone do not establish companion completeness
or role/family destinations. Snapshot locations are estimates until the canonical
cache preview revalidates bytes, roots, policies, ownership and free space.
"""
from __future__ import annotations

from pathlib import Path
from dataclasses import asdict

from .model_cache import CacheResource, ModelRequirementSet
from .model_resource_registry import MAX_RESOURCES, ModelResourceError
from .model_storage_policy import effective_policy, fast_destination, load_policy, retained_fast_roots
from .model_organization import normalize_model_family
from .model_usage import _ID, _time, successful_use_summary
from .model_use_leases import cache_state, pinned_entries, digest

MAX_SUGGESTIONS = 200


def known_placement_requirements(snapshot):
    """Pure Anima resource-only contract projection from recorded metadata.

    Only an unambiguous permanent location and matching role/family declaration
    for EVERY exact contract member produce a set. Files/existence are unverified.
    Never substitute metadata heuristics for this narrowly qualified owner.
    """
    from . import training_anima as anima
    records = snapshot.get("resources", {})
    if not isinstance(records, dict) or len(records) > MAX_RESOURCES:
        raise ModelResourceError("Placement requires a bounded resource snapshot.")
    if anima.BASE_ID not in records:
        return {}
    owner = anima.anima_resource_contract(workflow_kind="training", profile_id=anima.RESOURCE_PROFILE_ID,
        adapter_id=anima.RESOURCE_ADAPTER_ID, adapter_version=anima.SOURCE_REVISION, main_resource_id=anima.BASE_ID)
    fast_roots = tuple(Path(path) for path in retained_fast_roots(snapshot))
    resources = []
    for member in owner.requirements:
        record = records.get(member.resource_id)
        if not isinstance(record, dict):
            return {}
        declarations = record.get("declarations", [])
        if not isinstance(declarations, list) or not declarations or len(declarations) > 256:
            return {}
        declared = set()
        for row in declarations:
            if not isinstance(row, dict) or not isinstance(row.get("family"), str) or not isinstance(row.get("role"), str):
                return {}
            declared.add((row["role"], normalize_model_family(row["family"])))
        if declared != {(member.role, normalize_model_family(member.family))}:
            return {}
        locations = record.get("locations", [])
        if not isinstance(locations, list) or len(locations) > 256:
            return {}
        permanent = {}
        for row in locations:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not Path(row["path"]).is_absolute():
                return {}
            path = Path(row["path"])
            if row.get("cache_entry_id") or any(path == root or root in path.parents for root in fast_roots):
                continue
            # Lexical parent traversal is ambiguous; no resolve/stat is allowed.
            if ".." in path.parts:
                return {}
            permanent.setdefault(_key(path), path)
        if len(permanent) != 1:
            return {}
        resources.append(CacheResource(member.resource_id, next(iter(permanent.values())), member.role, member.family))
    return {anima.BASE_ID: ModelRequirementSet(tuple(resources), "adapter_qualified", owner.adapter_id,
        owner.adapter_version, digest(asdict(owner)))}


def _bytes(value):
    if type(value) is not int or not 0 <= value <= 1024**5:
        raise ModelResourceError("Placement estimates require bounded nonnegative byte counts.")
    return value


def _key(path):
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ModelResourceError("Placement needs absolute canonical destination paths.")
    return str(Path(path)).casefold()


def recommend_placements(snapshot, requirements, *, free_bytes=None, limit=MAX_SUGGESTIONS):
    """Rank registered resources, counting each destination copy once across sets.

    requirements maps a primary exact resource ID to its trusted adapter set.
    Missing/unqualified sets remain visible and held for manual review. free_bytes
    is a caller-captured Fast-volume measurement; None holds new-copy suggestions.
    No resource files, directories, registry files or runtime history are read.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_SUGGESTIONS:
        raise ModelResourceError("Choose a bounded placement suggestion page.")
    records = snapshot.get("resources")
    if not isinstance(records, dict) or len(records) > MAX_RESOURCES or not isinstance(requirements, dict) or len(requirements) > MAX_RESOURCES:
        raise ModelResourceError("Placement requires a bounded registry and exact requirement projection.")
    if free_bytes is not None:
        _bytes(free_bytes)
    policy = load_policy(snapshot.get("workspace", {}))
    usage = successful_use_summary(snapshot)
    cache = cache_state(snapshot)
    pins = pinned_entries(cache)
    existing, charged, uncertain = {}, 0, False
    for entry_id, entry in cache["entries"].items():
        if entry.get("state") != "available":
            continue
        key = _key(entry.get("path")); size = _bytes(entry.get("size"))
        identity = entry.get("resource_id")
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ModelResourceError("A cache entry has an invalid exact identity.")
        copy = {"resource_id": identity, "size": size, "pinned": entry_id in pins}
        if key in existing:
            if any(existing[key][field] != copy[field] for field in ("resource_id", "size")):
                raise ModelResourceError("Conflicting cache copies require recovery before placement review.")
            existing[key]["pinned"] |= copy["pinned"]
        else:
            existing[key] = copy; charged += size
    for operation in cache["operations"].values():
        if operation.get("phase") != "complete":
            uncertain = True
    candidates = []
    for identity, record in records.items():
        if not isinstance(identity, str) or not isinstance(record, dict):
            raise ModelResourceError("Placement requires valid registered resources.")
        user = record.get("user", {})
        if not isinstance(user, dict):
            raise ModelResourceError("Model preferences are invalid.")
        history = usage["resources"].get(identity, {})
        count = history.get("successful_use_count", 0)
        last = history.get("last_success_at", "")
        name = user.get("display_name")
        candidates.append({"resource_id": identity, "name": name[:160] if isinstance(name, str) and name.strip() else identity,
            "favorite": user.get("favorite") is True, "successful_use_count": count,
            "last_success_at": last})
    candidates.sort(key=lambda row: (-int(row["favorite"]), -(_time(row["last_success_at"]).timestamp() if row["last_success_at"] else 0),
                                     -row["successful_use_count"], row["resource_id"]))
    selected = {}; rows = []
    for base in candidates[:limit]:
        identity = base["resource_id"]
        row = {**base, "status": "held", "complete": False, "required_ids": [], "copies": [],
               "total_bytes": None, "additional_bytes": None, "pinned": False, "keep_fast": False,
               "protected": False, "reason": "Complete companion requirements are unknown. Review manually."}
        row["pinned"] = any(copy["resource_id"] == identity and copy["pinned"] for copy in existing.values())
        # Exact-resource overrides do not need inferred family/role metadata.
        row["keep_fast"] = dict(policy.resource_policies).get(identity) == "keep_fast"
        row["protected"] = row["pinned"] or row["keep_fast"]
        rank_reason = []
        if row["favorite"]:
            rank_reason.append("Favorite informs ranking; it is not an eviction pin.")
        if row["successful_use_count"]:
            rank_reason.append(f"{row['successful_use_count']} retained verified successes; latest {row['last_success_at']}.")
        else:
            rank_reason.append("No retained successful use; historical and external use is unknown.")
        row["ranking_reason"] = " ".join(rank_reason)
        required = requirements.get(identity)
        if (not isinstance(required, ModelRequirementSet) or required.qualification != "adapter_qualified"
                or not required.adapter_id or not required.adapter_version or not required.request_fingerprint
                or not isinstance(required.resources, tuple) or not 1 <= len(required.resources) <= 64
                or any(not isinstance(member, CacheResource) for member in required.resources)
                or identity not in {member.resource_id for member in required.resources}):
            rows.append(row); continue
        row["required_ids"] = sorted({member.resource_id for member in required.resources})
        if any(key not in records for key in row["required_ids"]):
            row["reason"] = "A required companion is missing from the registered set. Review manually."
            rows.append(row); continue
        row["complete"] = True
        if policy.mode == "single":
            row.update(status="in_place", reason="Single-location mode uses this set in place; no Fast copies are proposed.", additional_bytes=0)
            rows.append(row); continue
        copies = {}; policies = []
        try:
            for member in required.resources:
                size = _bytes(records[member.resource_id].get("identity", {}).get("size"))
                effective = effective_policy(policy, resource_id=member.resource_id, role=member.role,
                    family=member.family, media_type=member.media_type).policy
                policies.append(effective)
                target = fast_destination(policy, resource_id=member.resource_id, role=member.role,
                    family=member.family, media_type=member.media_type)
                key = _key(target)
                copy = {"resource_id": member.resource_id, "destination": str(target), "size": size,
                        "recorded_prepared": key in existing, "shared_with_earlier_suggestion": key in selected}
                if key in existing and any(existing[key][field] != copy[field] for field in ("resource_id", "size")):
                    raise ModelResourceError("A destination has conflicting cache evidence. Review recovery.")
                if key in copies and copies[key] != copy:
                    raise ModelResourceError("Required copies disagree about a shared destination.")
                copies[key] = copy
            row["copies"] = list(copies.values())
            row["total_bytes"] = sum(copy["size"] for copy in copies.values())
            row["additional_bytes"] = sum(copy["size"] for key, copy in copies.items() if key not in existing and key not in selected)
            row["pinned"] = any(existing.get(key, {}).get("pinned", False) for key in copies)
            row["keep_fast"] = "keep_fast" in policies
            row["protected"] = row["pinned"] or row["keep_fast"]
            additional = row["additional_bytes"]
            selected_bytes = sum(copy["size"] for copy in selected.values())
            if uncertain:
                row["reason"] = "An unfinished cache operation needs recovery; available capacity is uncertain."
            elif "archive_only" in policies:
                row["reason"] = "A required resource stays in Archive until explicitly prepared. Review Prepare for Use."
            elif additional and free_bytes is None:
                row["reason"] = "Fast free space is unknown. Measure space before preparing this complete set."
            elif policy.cache_limit_mode == "fixed" and charged + selected_bytes + additional > policy.cache_limit_bytes:
                row["reason"] = "The complete set exceeds the remaining cache capacity. Review storage or eligible cleanup."
            elif additional and selected_bytes + additional + policy.minimum_free_bytes > free_bytes:
                row["reason"] = "The complete set would violate the Fast free-space reserve."
            else:
                row.update(status="recorded_prepared" if all(key in existing for key in copies) else "suggested",
                    reason="Review this complete set in Prepare for Use; recorded copies and space still require fresh verification.")
                selected.update({key: copy for key, copy in copies.items() if key not in existing})
        except (ModelResourceError, KeyError, TypeError) as exc:
            row.update(status="held", reason=str(exc))
        rows.append(row)
    return {"rows": rows, "resource_count": len(records), "truncated": len(candidates) > limit,
            "mode": policy.mode, "cache_limit_bytes": policy.cache_limit_bytes,
            "cache_limit_mode": policy.cache_limit_mode,
            "charged_bytes": charged, "proposed_new_bytes": sum(copy["size"] for copy in selected.values()),
            "free_bytes": free_bytes, "minimum_free_bytes": policy.minimum_free_bytes,
            "usage_enabled": usage["enabled"], "history_compacted": usage["history_compacted"],
            "message": "Sizes and copy availability are registry estimates; files have not been inspected. Suggestions do not move files, change policy, release pins or authorize cleanup. Counts cover retained successes only."}
