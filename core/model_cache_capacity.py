"""Bounded least-used capacity review; removal authority stays with model_cache.

Only retained, opted-in successful-use evidence ranks candidates. Unknown or
compacted history is never interpreted as zero use. This module does not mutate
the registry, remove files, or infer companion completeness.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model_resource_registry import ModelResourceError
from .model_storage_policy import effective_policy
from .model_usage import successful_use_summary, usage_state, _time
from .model_use_leases import cache_state, digest, pinned_entries

MAX_VICTIMS = 64


@dataclass(frozen=True)
class CacheCapacityVictim:
    entry_id: str
    resource_id: str
    path: Path
    bytes: int
    successful_use_count: int
    last_success_at: str
    entry_digest: str


@dataclass(frozen=True)
class CacheCapacityReview:
    ready: bool
    required_bytes: int
    reclaimable_bytes: int
    new_bytes: int
    charged_bytes: int
    cache_limit_bytes: int
    free_bytes: int
    minimum_free_bytes: int
    volume_id: int
    cache_limit_mode: str = "fixed"
    victims: tuple[CacheCapacityVictim, ...] = ()
    usage_token: str = ""
    held: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class CacheCleanupExclusion:
    entry_id: str
    path: Path
    bytes: int
    reason: str


@dataclass(frozen=True)
class CacheCleanupRecommendation:
    ready: bool
    target_bytes: int
    reclaimable_bytes: int
    protected_count: int
    protected_bytes: int
    retained_count: int
    retained_bytes: int
    victims: tuple[CacheCapacityVictim, ...] = ()
    exclusions: tuple[CacheCleanupExclusion, ...] = ()
    message: str = ""

    @property
    def entry_ids(self):
        return tuple(row.entry_id for row in self.victims)


def review_cleanup_recommendation(snapshot, *, policy, target_bytes, volume_id,
                                  qualify, should_cancel=None):
    """Recommend standalone maintenance, without an invented incoming set.

    Metadata inventory is bounded by cache_state; at most 64 candidates are
    read through the canonical owner. Exclusion detail is bounded independently
    while aggregate counts include every retained nonretired entry.
    """
    if type(target_bytes) is not int or not 0 < target_bytes <= 2**63-1:
        raise ModelResourceError("Choose a positive, supported number of bytes to reclaim.")
    state=cache_state(snapshot)
    entries={key:entry for key,entry in state["entries"].items() if entry.get("state")!="retired"}
    size=lambda entry: entry["size"] if type(entry.get("size")) is int and entry["size"]>0 else 0
    total_bytes=sum(size(entry) for entry in entries.values())
    exclusions=[];protected_count=0;protected_bytes=0
    def cancelled():
        if should_cancel and should_cancel():
            raise InterruptedError("Cleanup recommendation cancelled before changing files.")
    def exclude(key,entry,reason):
        nonlocal protected_count,protected_bytes
        protected_count+=1;protected_bytes+=size(entry)
        if len(exclusions)<MAX_VICTIMS:
            raw_path=entry.get("path")
            exclusions.append(CacheCleanupExclusion(key,Path(raw_path if isinstance(raw_path,str) and raw_path else "."),size(entry),reason))
    def result(ready,victims,reclaimed,message):
        return CacheCleanupRecommendation(ready,target_bytes,reclaimed,protected_count,
            protected_bytes,len(entries)-len(victims),total_bytes-reclaimed,
            tuple(victims),tuple(exclusions),message)
    cancelled()
    summary=successful_use_summary(snapshot)
    if not summary["enabled"] or summary["history_compacted"]:
        for key,entry in entries.items():
            cancelled();exclude(key,entry,"Successful-use history is disabled, unknown or compacted.")
        return result(False,(),0,"A least-used recommendation needs enabled, uncompacted successful-use history. Review exact copies manually; no files were selected.")
    pinned=pinned_entries(state)
    fast_root=Path(policy.fast_root).absolute()
    candidates=[];seen_paths=set();seen_files=set()
    for key,entry in entries.items():
        cancelled()
        reason=""
        raw_path=entry.get("path")
        path=Path(raw_path if isinstance(raw_path,str) and raw_path else ".")
        identity=entry.get("resource_id")
        file_state=entry.get("file_state",[])
        if entry.get("state")!="available" or not isinstance(identity,str):reason="Recorded copy is unavailable or has uncertain ownership."
        elif key in pinned:reason="Manual pin or active/uncertain use protects this copy."
        elif (not path.is_absolute() or fast_root not in path.parents
                or not isinstance(file_state,list) or len(file_state)<2
                or any(type(part) is not int for part in file_state[:2]) or file_state[0]!=volume_id):
            reason="Copy is outside the current Fast volume or its recorded identity is uncertain."
        contexts=entry.get("contexts",[{k:entry.get(k) for k in ("role","family","media_type")}])
        if not reason:
            try:
                if not contexts or any(effective_policy(policy,resource_id=identity,**row).policy=="keep_fast" for row in contexts):
                    reason="Keep on Fast storage policy protects this copy."
            except (TypeError,ValueError,RuntimeError):reason="Retention policy is uncertain; preserve this copy."
        history=summary["resources"].get(identity) if isinstance(identity,str) else None
        if not reason and (not history or history["successful_use_count"]<1):
            reason="Successful use is unknown; it cannot be ranked as zero use."
        if not reason and not size(entry):reason="The recorded copy size is unavailable."
        if not reason:
            path_key,file_key=str(path).casefold(),tuple(file_state[:2])
            if path_key in seen_paths or file_key in seen_files:reason="Another recorded entry claims this file; preserve it for exact review."
            else:seen_paths.add(path_key);seen_files.add(file_key)
        if reason:
            exclude(key,entry,reason);continue
        candidates.append((history["successful_use_count"],_time(history["last_success_at"]).timestamp(),key,entry,history))
    candidates.sort(key=lambda row:row[:3])
    for _,_,key,entry,_ in candidates[MAX_VICTIMS:]:
        exclude(key,entry,"Outside this bounded 64-candidate review; retained without reading files.")
    victims=[];reclaimed=0
    for _,_,key,entry,history in candidates[:MAX_VICTIMS]:
        cancelled()
        try:
            qualified=qualify(key)
            if qualified!=entry:
                raise ModelResourceError("Recorded copy changed during qualification.")
        except InterruptedError:raise
        except (OSError,ValueError,RuntimeError) as exc:
            exclude(key,entry,str(exc));continue
        victims.append(CacheCapacityVictim(key,entry["resource_id"],Path(entry["path"]),size(entry),
            history["successful_use_count"],history["last_success_at"],digest(entry)))
        reclaimed+=size(entry)
        if reclaimed>=target_bytes:break
    if reclaimed<target_bytes:
        return result(False,(),0,"The target cannot be met by verified, known-use Fast copies within this bounded review. Lower the target or review exact copies manually; no files were selected.")
    return result(True,victims,reclaimed,"Review these least-used eligible Fast copies before removal. Independent Archive originals were verified and will be retained; usage counts describe recorded supported successes only.")


def retained_capacity(value):
    if value is None:
        return None
    try:
        return CacheCapacityReview(**{**value, "held": tuple(value["held"]),
            "victims": tuple(CacheCapacityVictim(**{**row, "path": Path(row["path"])}) for row in value["victims"])})
    except (KeyError, TypeError, ValueError):
        raise ModelResourceError("The retained capacity review is invalid; preserve its recovery evidence.") from None


def victim_usage_token(snapshot, resource_ids):
    """Unrelated successes do not stale a plan; reset or victim use does."""
    state = usage_state(snapshot)
    summary = successful_use_summary(snapshot)
    if not state["enabled"] or state["compacted"]:
        raise ModelResourceError("Least-used cleanup needs enabled, uncompacted successful-use history.")
    facts = {}
    for identity in sorted(set(resource_ids)):
        row = summary["resources"].get(identity)
        if not row or row["successful_use_count"] < 1:
            raise ModelResourceError("Unknown use is not evidence that a model is least used. Review cleanup manually.")
        facts[identity] = row
    return digest({"generation": state["generation"], "resources": facts})


def review_capacity(snapshot, *, policy, incoming_ids, incoming_bytes, incoming_complete,
                    new_bytes, charged_bytes, free_bytes, volume_id, qualify):
    """Rank bounded metadata, then ask the canonical owner to qualify candidates.

    qualify(entry_id) verifies the exact owned Fast file and its independent
    Archive original. It may read only that candidate; no directory discovery.
    """
    required = max(0, new_bytes + charged_bytes - policy.cache_limit_bytes
        if policy.cache_limit_mode == "fixed" else 0,
        new_bytes + policy.minimum_free_bytes - free_bytes)
    base = dict(required_bytes=required, new_bytes=new_bytes, charged_bytes=charged_bytes,
        cache_limit_bytes=policy.cache_limit_bytes, free_bytes=free_bytes,
        minimum_free_bytes=policy.minimum_free_bytes, volume_id=volume_id,
        cache_limit_mode=policy.cache_limit_mode)

    def held(message):
        # An insufficient whole-set plan exposes no actionable partial victims.
        return CacheCapacityReview(False, reclaimable_bytes=0, **base, held=(message,), message=message)

    if not required:
        return CacheCapacityReview(True, reclaimable_bytes=0, **base,
            message="The complete selected set fits without removing any Fast copies.")
    if not incoming_complete:
        return held("Automatic space management requires an adapter-qualified complete companion set. Review manual cleanup instead.")
    if policy.cache_limit_mode == "fixed" and incoming_bytes > policy.cache_limit_bytes:
        return held("The complete incoming set exceeds the Fast cache limit. No copies will be removed.")
    summary = successful_use_summary(snapshot)
    if not summary["enabled"] or summary["history_compacted"]:
        return held("Successful-use history is disabled, unknown or compacted. Review cleanup manually; no copies will be removed.")
    state = cache_state(snapshot)
    pinned = pinned_entries(state)
    candidates = []
    seen_paths, seen_files = set(), set()
    incoming = set(incoming_ids)
    fast_root = Path(policy.fast_root).absolute()
    for entry_id, entry in state["entries"].items():
        identity = entry.get("resource_id")
        if entry.get("state") != "available" or entry_id in pinned or identity in incoming:
            continue
        path = Path(entry.get("path", ""))
        file_state = entry.get("file_state", [])
        if (not path.is_absolute() or fast_root not in path.parents
                or len(file_state) < 2 or file_state[0] != volume_id):
            continue
        contexts = entry.get("contexts", [{k: entry.get(k) for k in ("role", "family", "media_type")}])
        try:
            if not contexts or any(effective_policy(policy, resource_id=identity, **row).policy == "keep_fast" for row in contexts):
                continue
        except (TypeError, ValueError, RuntimeError):
            continue
        history = summary["resources"].get(identity)
        if not history or history["successful_use_count"] < 1:
            continue
        path_key, file_key = str(path).casefold(), tuple(file_state[:2])
        if path_key in seen_paths or file_key in seen_files:
            continue
        seen_paths.add(path_key); seen_files.add(file_key)
        candidates.append((history["successful_use_count"], _time(history["last_success_at"]).timestamp(),
            entry_id, entry, history))
    candidates.sort(key=lambda row: row[:3])
    victims, reclaimable = [], 0
    for _, _, entry_id, entry, history in candidates[:MAX_VICTIMS]:
        try:
            qualified = qualify(entry_id)
            if qualified != entry:
                continue
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError):
            continue
        size = entry.get("size")
        if type(size) is not int or size <= 0:
            continue
        victims.append(CacheCapacityVictim(entry_id, entry["resource_id"], Path(entry["path"]), size,
            history["successful_use_count"], history["last_success_at"], digest(entry)))
        reclaimable += size
        if reclaimable >= required:
            break
    if reclaimable < required:
        return held("Too little eligible, known-use Fast storage is reclaimable within this bounded review. No copies will be removed; review storage or manual cleanup.")
    return CacheCapacityReview(True, reclaimable_bytes=reclaimable, **base, victims=tuple(victims),
        usage_token=victim_usage_token(snapshot, [row.resource_id for row in victims]),
        message="Make room using these least-used eligible Fast copies, then prepare the complete incoming set. Archive originals remain; counts describe retained supported successes only.")
