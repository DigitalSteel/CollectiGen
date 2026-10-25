"""Owner-bound preparation protection, retained through all input consumers.

No process inference or automatic eviction. The admitted owner supplies bounded
facts; expensive owner checks happen before locks, and exact state is compared
again under cache-lock then registry-writer. A stopped process alone is never a
reason to drop this reservation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .model_cache import ModelCacheService
from .model_resource_registry import ModelResourceError, _bounded_read, _bytes
from .model_use_leases import cache_lock, cache_state, digest, now
from .model_workspace import _path, _pinned_parent

RECEIPT_NAME = "anima-resources.json"
_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class ExecutionCacheStatus:
    execution_id: str
    receipt_path: Path
    status: str
    operation_id: str = ""
    bindings: tuple[tuple[str, str], ...] = ()
    message: str = ""


class ModelExecutionCacheService:
    def __init__(self, registry, *, owner_id, owner_version, protected_roots=(), managed_roots=(), transition_verifier=None):
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in (owner_id, owner_version)):
            raise ModelResourceError("Choose an admitted execution owner and version.")
        self.registry = registry
        self.owner_id, self.owner_version = owner_id, owner_version
        self.protected_roots, self.managed_roots = tuple(protected_roots), tuple(managed_roots)
        self.transition_verifier = transition_verifier

    def _read(self, path):
        path = _path(path, (*self.protected_roots, *self.managed_roots))
        raw = _bounded_read(path, 64 * 1024)
        try:
            receipt = json.loads(raw)
            if (not isinstance(receipt, dict) or raw != _bytes(receipt)
                    or receipt.get("schema_version") != 1
                    or receipt.get("kind") != "model_execution_preparation"
                    or receipt.get("owner_id") != self.owner_id
                    or receipt.get("owner_version") != self.owner_version
                    or not _ID.fullmatch(receipt.get("execution_id", ""))
                    or path.name != RECEIPT_NAME):
                raise ValueError()
        except (TypeError, ValueError, UnicodeError):
            raise ModelResourceError("The exact execution preparation receipt is invalid.") from None
        return path, receipt, digest(receipt)

    def read(self, path):
        """Read only the caller's exact receipt; never enumerate preparations."""
        path, receipt, receipt_digest = self._read(path)
        value, _ = self.registry.snapshot()
        record = cache_state(value)["leases"].get(receipt["execution_id"])
        if record is not None:
            self._match(record, receipt, receipt_digest)
            if Path(record["receipt_path"]) != path:
                raise ModelResourceError("Use the owner's original exact receipt locator.")
        return receipt, deepcopy(record)

    def _match(self, record, receipt, receipt_digest):
        if (record.get("kind") != "execution_preparation"
                or record.get("owner_id") != self.owner_id
                or record.get("owner_version") != self.owner_version
                or record.get("receipt_digest") != receipt_digest
                or record.get("owner_fingerprint") != receipt.get("owner_fingerprint")
                or record.get("resource_ids") != receipt.get("resource_ids")
                or record.get("required_set_digest") != receipt.get("required_set_digest")
                or record.get("state") not in {"active", "released"}
                or record.get("phase") not in {"prepared", "bound", "submitted", "stopped", "uncertain", "discarded"}
                or not isinstance(record.get("bindings"), list)
                or len(record["bindings"]) != len(receipt["resource_ids"])
                or any(not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(v, str) for v in pair) or not Path(pair[1]).is_absolute()
                    for pair in record["bindings"])
                or sorted(pair[0] for pair in record["bindings"]) != receipt["resource_ids"]):
            raise ModelResourceError("The execution owner receipt no longer matches its protection.")

    def prepare(self, *, receipt_root, owner_fingerprint, owner_context, requirements,
                expected_registry, cache_preview=None, requirement_resolver=None, should_cancel=None, progress=None):
        """Create one immutable locator before the cache can publish any file."""
        if requirements.qualification != "adapter_qualified":
            raise ModelResourceError("An exact owner-qualified resource set is required.")
        if not isinstance(owner_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", owner_fingerprint):
            raise ModelResourceError("An exact owner selection fingerprint is required.")
        if (requirement_resolver is None or requirement_resolver(requirements.adapter_id,
                requirements.adapter_version, requirements.request_fingerprint) != requirements.resources):
            raise ModelResourceError("The trusted owner did not reconstruct the exact resource set.")
        path = _path(Path(receipt_root) / RECEIPT_NAME, (*self.protected_roots, *self.managed_roots))
        execution_id = uuid4().hex
        receipt = {"schema_version": 1, "kind": "model_execution_preparation",
            "owner_id": self.owner_id, "owner_version": self.owner_version,
            "execution_id": execution_id, "owner_fingerprint": owner_fingerprint,
            "owner_context": owner_context, "resource_ids": sorted(r.resource_id for r in requirements.resources),
            "source_bindings": [[r.resource_id, str(r.archive_path)] for r in requirements.resources],
            "required_set_digest": digest(sorted((r.resource_id, r.role, r.family) for r in requirements.resources)),
            "mode": "tiered" if cache_preview is not None else "single"}
        raw = _bytes(receipt)
        if len(raw) > 64 * 1024:
            raise ModelResourceError("Execution preparation exceeds its bounded receipt size.")
        with _pinned_parent(path.parent, self.protected_roots):
            with path.open("xb") as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            if _bounded_read(path, 64 * 1024) != raw:
                raise ModelResourceError("Execution preparation receipt failed readback.")

        def reserve(value, operation_id, baseline):
            # This closure contains only reviewed owner facts; no external I/O,
            # callbacks, registry reentry or runtime validation under the locks.
            state = cache_state(value, create=True)
            if _bounded_read(path, 64 * 1024) != raw:
                raise ModelResourceError("The owner receipt changed before resource reservation.")
            if len(state["leases"]) >= 10000 or execution_id in state["leases"]:
                raise ModelResourceError("Execution preparation history is full or changed.")
            state["leases"][execution_id] = {
                "kind": "execution_preparation", "state": "active", "phase": "prepared",
                "entry_ids": [r["entry_id"] for r in baseline.get("rows", []) if r["existing"]],
                "reserved_entry_ids": [r["entry_id"] for r in baseline.get("rows", [])],
                "operation_id": operation_id, "created_at": now(),
                "owner_id": self.owner_id, "owner_version": self.owner_version,
                "receipt_digest": digest(receipt), "receipt_path": str(path),
                "owner_fingerprint": owner_fingerprint, "resource_ids": receipt["resource_ids"],
                "required_set_digest": receipt["required_set_digest"],
                "policy_digest": baseline.get("policy_digest", ""),
                "bindings": [[r["resource"]["resource_id"], r["destination"]] for r in baseline.get("rows", [])]
                    if cache_preview is not None else [[r.resource_id, str(r.archive_path)] for r in requirements.resources]}
            return execution_id

        if cache_preview is None:
            with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
                value, token = self.registry.snapshot()
                if token != expected_registry:
                    raise ModelResourceError("The reviewed resource registry changed before reservation.")
                reserve(value, "", {})
                self.registry._save(value, token)
        else:
            service = ModelCacheService(self.registry, protected_roots=self.protected_roots,
                managed_roots=self.managed_roots, requirement_resolver=requirement_resolver,
                execution_reserver=reserve)
            # Preview capability comes from the same trusted owner, preserving
            # the reviewed registry/policy/source snapshot across this wrapper.
            service._issue(cache_preview)
            service.apply_prepare(cache_preview, confirmed=True, should_cancel=should_cancel, progress=progress)
        return self.status(path)

    def abandon_unreserved(self, path):
        """Tombstone an unsubmitted receipt before a pending reserve can race it."""
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            path, receipt, receipt_digest = self._read(path)
            value, token = self.registry.snapshot()
            state = cache_state(value, create=True)
            if receipt["execution_id"] in state["leases"]:
                return "uncertain"
            if len(state["leases"]) >= 10000:
                raise ModelResourceError("Execution history is full; preserve this receipt.")
            state["leases"][receipt["execution_id"]] = {"kind": "execution_preparation",
                "state": "released", "phase": "discarded", "entry_ids": [], "reserved_entry_ids": [],
                "operation_id": "", "created_at": now(), "released_at": now(),
                "owner_id": self.owner_id, "owner_version": self.owner_version,
                "receipt_digest": receipt_digest, "receipt_path": str(path),
                "owner_fingerprint": receipt["owner_fingerprint"], "resource_ids": receipt["resource_ids"],
                "required_set_digest": receipt["required_set_digest"], "policy_digest": "",
                "bindings": receipt["source_bindings"]}
            self.registry._save(value, token)
        return "released"

    def status(self, path):
        receipt, record = self.read(path)
        if record is None:
            return ExecutionCacheStatus(receipt["execution_id"], Path(path), "unreserved",
                message="No cache publication was reserved. Discard this unsubmitted preparation and review again.")
        state = "released" if record["state"] == "released" else "ready"
        if record["operation_id"]:
            value, _ = self.registry.snapshot()
            operation = cache_state(value)["operations"].get(record["operation_id"], {})
            if operation.get("phase") != "complete":
                state = "recovery_required"
        return ExecutionCacheStatus(receipt["execution_id"], Path(path), state, record["operation_id"],
            tuple(tuple(pair) for pair in record["bindings"]),
            "Exact inputs remain protected until this preparation is explicitly discarded.")

    def recover(self, path, *, should_cancel=None, progress=None):
        status = self.status(path)
        if status.status != "recovery_required":
            return status
        cache = ModelCacheService(self.registry, protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        reviewed = cache.preview_recovery(status.operation_id, should_cancel=should_cancel)
        cache.recover(reviewed, confirmed=True, should_cancel=should_cancel, progress=progress)
        return self.status(path)

    def update(self, path, expected, proof):
        """Owner-only compare-and-swap; callers must supply prevalidated facts.

        This is deliberately not a public success/stop claim API. Only the
        admitted controller invokes it after its own process/journal checks.
        """
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            # A bounded exact receipt read is the only owner I/O under locks.
            _, receipt, receipt_digest = self._read(path)
            value, token = self.registry.snapshot()
            record = cache_state(value)["leases"].get(receipt["execution_id"])
            if record is None:
                raise ModelResourceError("No execution preparation was reserved.")
            self._match(record, receipt, receipt_digest)
            if Path(record["receipt_path"]) != Path(path):
                raise ModelResourceError("Use the owner's original exact receipt locator.")
            if record != expected:
                raise ModelResourceError("Execution ownership changed; reopen its exact preparation.")
            changes = self.transition_verifier(receipt["execution_id"], deepcopy(record), proof) if self.transition_verifier else None
            if not isinstance(changes, dict):
                raise ModelResourceError("A trusted owner must verify this exact consumer transition.")
            allowed = {"phase", "plan_fingerprint", "review_path", "usage_admission", "usage_attempted",
                "verified_completed_at", "verified_journal_tip", "state", "released_at"}
            if set(changes) - allowed:
                raise ModelResourceError("Unsupported execution owner transition.")
            record.update(deepcopy(changes))
            self.registry._save(value, token)
        return deepcopy(record)
