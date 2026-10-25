"""Issued, pinned references to completed manually protected Fast copies.

This is ownership/byte proof only. Consumers still qualify their source mapping,
review the payload, and publish through the existing registry/artwork owner.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import stat
from uuid import uuid4

from .model_cache import ModelCacheService, _key
from .model_resource_registry import ModelResourceError, ResourceInspection, _cancel
from .model_storage_policy import fast_destination, root_generation, validate_policy_roots
from .model_use_leases import cache_lock, cache_state, pinned_entries
from .model_workspace import _pinned_model_file


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                      allow_nan=False).encode("utf-8")


def _ancestor_states(selected):
    result = []
    for ancestor in selected.parents:
        info = ancestor.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode)
                or int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000)):
            raise ModelResourceError("Fast reference ancestors must be fully local directories.")
        result.append((str(ancestor), info.st_dev, info.st_ino))
    return result


@dataclass(frozen=True)
class CacheReferenceReview:
    review_id: str
    resource_id: str
    selected_path: Path
    entry_id: str
    manual_pin_id: str
    role: str
    family: str
    media_type: str
    registry_token: str
    proof_json: bytes


@dataclass(frozen=True)
class VerifiedCacheReference:
    review: CacheReferenceReview
    inspection: ResourceInspection


class ModelCacheReferenceService:
    def __init__(self, cache: ModelCacheService):
        self.cache = cache
        self._issued = {}

    @staticmethod
    def _fingerprint(review):
        return hashlib.sha256(_encoded(asdict(review))).hexdigest()

    def _observe(self, review):
        value, token = self.cache.registry.snapshot()
        if token != review.registry_token:
            raise ModelResourceError("The registry changed; review the Fast reference again.")
        try:
            state = cache_state(value)
            entry = state["entries"][review.entry_id]
            selected = review.selected_path
            if entry["state"] != "available" or entry["resource_id"] != review.resource_id or _key(entry["path"]) != _key(selected):
                raise ModelResourceError("Choose the exact available Fast entry and registered resource.")
            policy = validate_policy_roots(value.get("workspace", {}),
                protected_roots=self.cache.protected_roots, managed_roots=self.cache.managed_roots)
            context = {"role": review.role, "family": review.family, "media_type": review.media_type}
            destination = fast_destination(policy, resource_id=review.resource_id, **context)
            generation = root_generation(policy.fast_root)
            contexts = entry.get("contexts", [])
            if (not isinstance(contexts, list) or not contexts
                    or any(not isinstance(item, dict) or set(item) != set(context) for item in contexts)
                    or context not in contexts or entry["root_generation"] != generation
                    or _key(destination) != _key(selected)):
                raise ModelResourceError("The current Fast generation, destination or role context changed.")
            selected = self.cache._guard(selected)
            ancestors = _ancestor_states(selected)
            owner, receipt = self.cache._load_operation(value, entry["operation_id"])
            owner_members = [row for row in owner.get("members", []) if row.get("entry_id") == review.entry_id]
            if (owner["kind"] != "prepare" or owner["phase"] != "complete" or owner["root_generation"] != generation
                    or review.entry_id not in owner.get("entry_ids", []) or len(owner_members) != 1
                    or owner_members[0].get("phase") != "available"):
                raise ModelResourceError("The Fast copy needs a completed current preparation owner.")
            pin = state["leases"][review.manual_pin_id]
            if pin.get("kind") != "manual" or pin.get("state") != "active" or review.entry_id not in pin.get("entry_ids", []):
                raise ModelResourceError("An active exact manual external-use pin is required.")
            pin_owner, pin_receipt = self.cache._load_operation(value, pin["operation_id"])
            rows = [row for row in pin_receipt.get("rows", []) if row.get("entry_id") == review.entry_id]
            members = [row for row in pin_owner.get("members", []) if row.get("entry_id") == review.entry_id]
            if (pin_owner.get("kind") != "prepare" or pin_owner.get("phase") != "complete"
                    or pin_owner.get("manual_pin_id") != review.manual_pin_id
                    or pin_owner.get("root_generation") != generation
                    or review.entry_id not in pin_owner.get("entry_ids", [])
                    or pin_owner.get("binding_contexts", {}).get(review.entry_id) != context
                    or len(rows) != 1 or len(members) != 1 or members[0].get("phase") != "available"):
                raise ModelResourceError("The manual pin lost its completed exact preparation proof.")
            row = rows[0]
            if (row["destination"] != entry["path"] or row["identity"] != entry["identity"]
                    or row["resource"]["resource_id"] != review.resource_id
                    or {key: row["resource"][key] for key in context} != context
                    or row["source_row"] != entry["archive_row"] or row["source_state"] != entry["archive_state"]):
                raise ModelResourceError("The pin preparation receipt no longer describes this exact copy.")
            pinned_entries(state)  # Malformed active IDs cannot prove an unrelated owner.
            for lease_id, lease in state["leases"].items():
                if lease.get("state") == "released":
                    continue
                reserved = lease.get("reserved_entry_ids", [])
                if not isinstance(reserved, list) or any(not isinstance(key, str) for key in reserved):
                    raise ModelResourceError("A cache use reservation is uncertain; preserve its copies.")
                affected = review.entry_id in (*lease.get("entry_ids", []), *lease.get("reserved_entry_ids", []))
                if affected and lease.get("state") != "released" and (lease.get("kind") != "manual" or lease.get("state") != "active"):
                    raise ModelResourceError("An active or uncertain execution owner protects this copy.")
                if affected and lease.get("state") == "active":
                    related = state["operations"].get(lease.get("operation_id"), {})
                    if related.get("phase") != "complete" or related.get("manual_pin_id") != lease_id:
                        raise ModelResourceError("A manual use owner is uncertain; reconcile it first.")
            for operation in state["operations"].values():
                affected = review.entry_id in (*operation.get("entry_ids", []), *operation.get("reserved_entry_ids", []))
                if affected and operation.get("phase") != "complete":
                    raise ModelResourceError("Recover the unfinished owner of this Fast copy first.")
            record = value["resources"][review.resource_id]
            if [location for location in record["locations"] if _key(location["path"]) == _key(entry["archive_path"])] != [entry["archive_row"]]:
                raise ModelResourceError("The permanent Archive registration proof changed.")
            info = selected.stat(follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000)):
                raise ModelResourceError("The Fast copy must be one fully local, unaliased file.")
            proof = _encoded({"entry": entry, "owner": owner, "receipt": receipt,
                "pin": pin, "pin_owner": pin_owner, "pin_receipt": pin_receipt,
                "policy": asdict(policy), "context": context, "ancestors": ancestors})
            return value, selected, proof
        except (KeyError, TypeError, ValueError, StopIteration) as exc:
            raise ModelResourceError("The exact Fast reference proof is incomplete; preserve it for review.") from exc

    @contextmanager
    def _verified(self, review, *, should_cancel=None, progress=None):
        _cancel(should_cancel)
        with cache_lock(self.cache.registry, protected_roots=self.cache.protected_roots):
            value, selected, proof = self._observe(review)
            if review.proof_json and review.proof_json != proof:
                raise ModelResourceError("The reviewed Fast ownership proof changed.")
            # This existing guard pins every ancestor and denies file writes/rename
            # through publication. No registry writer is held until the consumer.
            with _pinned_model_file(selected, (*self.cache.protected_roots, *self.cache.managed_roots)):
                pinned_value, _, pinned_proof = self._observe(review)
                if pinned_proof != proof:
                    raise ModelResourceError("The Fast reference or ancestors changed before pinning.")
                _, inspected = self.cache._verify_entry_with_inspection(pinned_value, review.entry_id,
                    should_cancel=should_cancel, progress=progress)
                if inspected is None or self.cache.registry.snapshot()[1] != review.registry_token:
                    raise ModelResourceError("The Fast reference changed during verification.")
                _cancel(should_cancel)
                yield deepcopy(inspected), proof

    def review(self, resource_id, selected_path, *, entry_id, manual_pin_id, role, family,
               media_type="Images", expected_registry, should_cancel=None, progress=None):
        if not isinstance(selected_path, (str, Path)) or not Path(selected_path).is_absolute():
            raise ModelResourceError("Choose an explicit absolute Fast model location.")
        pending = CacheReferenceReview(uuid4().hex, resource_id, Path(selected_path).absolute(),
            entry_id, manual_pin_id, role, family, media_type, expected_registry, b"")
        with self._verified(pending, should_cancel=should_cancel, progress=progress) as (inspection, proof):
            issued = CacheReferenceReview(pending.review_id, resource_id, pending.selected_path, entry_id,
                manual_pin_id, role, family, media_type, expected_registry, proof)
            self._issued[issued.review_id] = self._fingerprint(issued)
            while len(self._issued) > 32:
                self._issued.pop(next(iter(self._issued)))
            return VerifiedCacheReference(issued, inspection)

    @contextmanager
    def pinned(self, review: CacheReferenceReview, *, should_cancel=None, progress=None):
        if (not isinstance(review, CacheReferenceReview)
                or self._issued.get(review.review_id) != self._fingerprint(review)):
            raise ModelResourceError("Use an unchanged Fast reference review issued in this session.")
        with self._verified(review, should_cancel=should_cancel, progress=progress) as (inspection, _):
            yield VerifiedCacheReference(deepcopy(review), inspection)
