"""Reviewed exact cache COPY, manual-use pins and receipt-owned cleanup.

Archive originals never move. No automatic eviction or backend submission lives
here. Consumer-owned requirement reconstruction is the only completeness proof.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from itertools import islice
from uuid import uuid4

from .external_write_transaction import write_verified_json
from .model_file_retirement import pinned_model_retirement
from .model_cache_capacity import (CacheCapacityReview, review_capacity, retained_capacity,
    victim_usage_token, review_cleanup_recommendation)
from .model_resource_registry import ModelResourceError, ResourceInspection, _bounded_read, _bytes, _cancel, _identity, _json, inspect_model_resource
from .model_storage_policy import effective_policy, fast_destination, validate_policy_roots, model_storage_roots, root_generation
from .model_use_leases import add_manual_pin, cache_lock, cache_state, digest, now, pinned_entries
from .model_workspace import (_path, _pinned_model_file, _pinned_parent,
    _immutable_operation_reference, _notify_operation_started)

MAX_MEMBERS = 64
RECOVERY_SUMMARY_PAGE_SIZE = 25


def _key(path):
    return str(Path(path).absolute()).casefold()


def _plain(value):
    return json.loads(json.dumps(asdict(value), default=str))


def _policy_digest(value):
    workspace = value.get("workspace", {})
    return digest({key: workspace.get(key) for key in ("storage_policy", "default_destination", "family_destinations", "destination_overrides")})


@dataclass(frozen=True)
class CacheResource:
    resource_id: str
    archive_path: Path
    role: str
    family: str
    media_type: str = "Images"


@dataclass(frozen=True)
class ModelRequirementSet:
    resources: tuple[CacheResource, ...]
    qualification: str = "unknown"
    adapter_id: str = ""
    adapter_version: str = ""
    request_fingerprint: str = ""


@dataclass(frozen=True)
class CachePreparationRow:
    resource: CacheResource
    destination: Path | None
    status: str
    reason: str
    inspection: ResourceInspection | None = None
    entry_id: str = ""
    ancestor: Path | None = None
    ancestor_identity: tuple = ()


@dataclass(frozen=True)
class CachePreparationPreview:
    preview_id: str
    registry_token: str
    policy_digest: str
    requirements: ModelRequirementSet
    rows: tuple[CachePreparationRow, ...]
    qualification: str
    new_bytes: int
    charged_bytes: int
    cache_limit_bytes: int
    message: str
    capacity: CacheCapacityReview | None = None
    cache_limit_mode: str = "fixed"

    @property
    def can_prepare(self):
        return bool(self.rows and all(row.status in ("copy", "cached") for row in self.rows)
            and (self.capacity is None or self.capacity.ready))


@dataclass(frozen=True)
class CacheBinding:
    entry_id: str
    resource_id: str
    path: Path
    role: str
    family: str
    media_type: str


@dataclass(frozen=True)
class CachePreparationOutcome:
    operation_id: str
    status: str
    bindings: tuple[CacheBinding, ...]
    manual_pin_id: str
    qualification: str
    message: str
    execution_id: str = ""


@dataclass(frozen=True)
class CacheCleanupPreview:
    preview_id: str
    registry_token: str
    entry_ids: tuple[str, ...]
    entries_digest: str
    reclaimable_bytes: int
    message: str = "Delete only these unchanged cache copies; verified archive originals remain."
    usage_token: str = ""


@dataclass(frozen=True)
class CacheRecoveryPreview:
    preview_id: str
    registry_token: str
    operation_id: str
    operation_digest: str
    kind: str
    message: str
    paths: tuple[tuple[str, str, str], ...] = ()
    capacity: CacheCapacityReview | None = None


@dataclass(frozen=True)
class CacheOperationSummary:
    """Recorded identification only; never an admitted recovery preview."""
    operation_id: str
    archive_paths: tuple[str, ...] = ()
    message: str = "Model details unavailable — review required"


class ModelCacheService:
    def __init__(self, registry, *, protected_roots=(), managed_roots=(), requirement_resolver=None,
                 execution_reserver=None):
        self.registry = registry
        self.protected_roots, self.managed_roots = tuple(protected_roots), tuple(managed_roots)
        # Injected by an admitted consumer, never imported from model metadata.
        self.requirement_resolver = requirement_resolver
        # Trusted owner callback: bounded state-only reservation under cache ->
        # registry locks, before any member can become visible. Never UI input.
        self.execution_reserver = execution_reserver
        self._issued = {}

    def _issue(self, preview):
        if len(self._issued) >= 32:
            self._issued.pop(next(iter(self._issued)))
        self._issued[preview.preview_id] = digest(_plain(preview))
        return preview

    def _accept(self, preview, kind, confirmed):
        if not confirmed:
            raise ModelResourceError("Confirm the exact reviewed cache operation first.")
        if not isinstance(preview, kind) or self._issued.get(preview.preview_id) != digest(_plain(preview)):
            raise ModelResourceError("Use an unchanged cache preview issued in this session.")

    def _guard(self, path):
        return _path(path, (*self.protected_roots, *self.managed_roots))

    def _requirements(self, requirements):
        if (not isinstance(requirements, ModelRequirementSet) or not isinstance(requirements.resources, tuple)
                or not 1 <= len(requirements.resources) <= MAX_MEMBERS
                or any(not isinstance(row, CacheResource) or not isinstance(row.archive_path, Path) for row in requirements.resources)):
            raise ModelResourceError("Choose a bounded exact model and companion set.")
        if requirements.qualification == "unknown":
            return "unknown"
        if requirements.qualification != "adapter_qualified" or self.requirement_resolver is None:
            raise ModelResourceError("Model IDs alone do not prove complete companions. Use manual preparation or a qualified adapter request.")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in (requirements.adapter_id, requirements.adapter_version, requirements.request_fingerprint)):
            raise ModelResourceError("The adapter request needs its exact identity and version.")
        actual = self.requirement_resolver(requirements.adapter_id, requirements.adapter_version, requirements.request_fingerprint)
        if not isinstance(actual, tuple) or actual != requirements.resources:
            raise ModelResourceError("The admitted adapter did not reconstruct this exact complete resource set.")
        return "adapter_qualified"

    def _row(self, resource, value, policy, *, should_cancel=None, progress=None):
        target = None
        try:
            if os.name != "nt":
                raise ModelResourceError("Cache publication currently requires the qualified Windows file boundary.")
            if not isinstance(resource.resource_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", resource.resource_id):
                raise ModelResourceError("Use registered standalone safe tensors; managed packs need their component owner.")
            archive = self._guard(resource.archive_path)
            fast_root = Path(policy.fast_root).absolute()
            if archive == fast_root or fast_root in archive.parents:
                raise ModelResourceError("Choose a permanent archive original outside the Fast folder.")
            record = value["resources"].get(resource.resource_id)
            locations = [] if not record else [loc for loc in record["locations"] if _key(loc["path"]) == _key(archive)]
            if len(locations) != 1 or locations[0].get("cache_entry_id"):
                raise ModelResourceError("Choose one exact registered archive location, never another cache copy.")
            if not any(d.get("role") == resource.role and d.get("family") == resource.family for d in record["declarations"]):
                raise ModelResourceError("The selected model declaration changed.")
            if re.search(r"-\d{5}-of-\d{5}\.safetensors$", archive.name, re.I) or any((archive.parent / name).exists() for name in
                    ("model.safetensors.index.json", "model_index.json", "diffusion_pytorch_model.safetensors.index.json")):
                raise ModelResourceError("Keep this managed model bundle together; standalone preparation cannot prove its companions.")
            policy_result = effective_policy(policy, resource_id=resource.resource_id, role=resource.role, family=resource.family, media_type=resource.media_type)
            if policy_result.policy == "archive_only":
                raise ModelResourceError("Archive-only policy blocks preparation. Review its policy before preparing.")
            target = self._guard(fast_destination(policy, resource_id=resource.resource_id, role=resource.role, family=resource.family, media_type=resource.media_type))
            inspection = inspect_model_resource(archive, should_cancel=should_cancel, progress=progress)
            if inspection.stored_identity() != record["identity"]:
                raise ModelResourceError("Archive bytes no longer match their registered model. Locate an exact copy.")
            state = cache_state(value)
            found = [(key, item) for key, item in state["entries"].items() if _key(item.get("path", "")) == _key(target) and item.get("state") == "available"]
            if target.exists():
                if len(found) != 1 or found[0][1].get("resource_id") != resource.resource_id:
                    raise ModelResourceError("The Fast destination is occupied by an unowned file. Nothing will overwrite it.")
                key, entry = found[0]
                self._verify_entry(value, key, should_cancel=should_cancel, progress=progress)
                return CachePreparationRow(resource, target, "cached", "Verified owned fast copy; archive retained.", inspection, key)
            if found:
                raise ModelResourceError("A recorded fast copy is missing. Recover its existing operation first.")
            ancestor = target.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            self._guard(ancestor)
            info = ancestor.stat(follow_symlinks=False)
            if not ancestor.is_dir() or getattr(info, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000):
                raise ModelResourceError("Choose locally available Fast storage.")
            return CachePreparationRow(resource, target, "copy", "Copy the exact model; archive original remains unchanged.", inspection,
                ancestor=ancestor, ancestor_identity=(info.st_dev, info.st_ino))
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            return CachePreparationRow(resource, target, "held", str(exc))

    @staticmethod
    def _charged(state):
        charged = sum(entry["size"] for entry in state["entries"].values() if entry.get("state") == "available")
        for operation in state["operations"].values():
            if operation.get("kind") == "prepare" and operation.get("phase") != "complete":
                charged += sum(member["size"] for member in operation.get("members", []) if member.get("phase") not in ("published", "available"))
        return charged

    def preview_prepare(self, requirements, *, expected_registry, should_cancel=None, progress=None):
        if progress:
            progress(0, 0, "Reviewing the complete incoming resource set before any space decision")
        qualification = self._requirements(requirements)
        value, token = self.registry.snapshot()
        if token != expected_registry:
            raise ModelResourceError("Model settings changed. Reload before preparing.")
        policy = validate_policy_roots(value.get("workspace", {}), protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        if policy.mode != "tiered":
            raise ModelResourceError("Single-location mode uses models in place. Configure tiered storage to prepare fast copies.")
        state = cache_state(value)
        model_storage_roots(value)
        if any(op.get("phase") != "complete" for op in state["operations"].values()):
            raise ModelResourceError("An unfinished cache operation needs recovery before new preparation.")
        source_progress = (lambda current, total, message: progress(current, total,
            "Reviewing incoming resources: " + message)) if progress else None
        rows = tuple(self._row(resource, value, policy, should_cancel=should_cancel, progress=source_progress) for resource in requirements.resources)
        targets = [_key(row.destination) for row in rows if row.destination]
        if len(set(targets)) != len(targets):
            raise ModelResourceError("Choose each exact destination once; duplicate resource roles must share one reviewed binding.")
        new_bytes = sum(row.inspection.size for row in rows if row.status == "copy")
        charged = self._charged(state)
        capacity = None
        automatic = policy.automatic_cleanup == "least_used"
        if not automatic and policy.cache_limit_mode == "fixed" and new_bytes + charged > policy.cache_limit_bytes:
            raise ModelResourceError("Cache limit would be exceeded. Increase it or explicitly review eligible cleanup; pins are never evicted automatically.")
        ancestors = {row.ancestor for row in rows if row.status == "copy"}
        for root in ancestors:
            if not automatic and shutil.disk_usage(root).free < new_bytes + policy.minimum_free_bytes:
                raise ModelResourceError("Fast storage lacks the selected set's space plus its saved free-space reserve.")
        if automatic:
            volume_root = self._guard(Path(policy.fast_root))
            while not volume_root.exists() and volume_root != volume_root.parent:
                volume_root = volume_root.parent
            volume_id = volume_root.stat(follow_symlinks=False).st_dev
            if any(root.stat(follow_symlinks=False).st_dev != volume_id for root in ancestors):
                raise ModelResourceError("The incoming set crosses storage volumes; review its placement manually.")
            capacity = review_capacity(value, policy=policy,
                incoming_ids=tuple(row.resource.resource_id for row in rows),
                incoming_bytes=sum(row.inspection.size for row in rows if row.inspection),
                incoming_complete=qualification == "adapter_qualified" and all(row.status in ("copy", "cached") for row in rows),
                new_bytes=new_bytes, charged_bytes=charged, free_bytes=shutil.disk_usage(volume_root).free,
                volume_id=volume_id, qualify=lambda key: self._qualify_cleanup(value, key, should_cancel=should_cancel,
                    progress=(lambda current, total, message: progress(current, total,
                        "Checking eligible least-used Fast copies and Archive originals: " + message)) if progress else None))
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed during cache preview. Review again.")
        message = ("The adapter reconstructed this exact required set." if qualification == "adapter_qualified" else
            "Prepare only the selected files; complete adapter companions and compatibility are unverified.")
        message += " All completed copies remain manually pinned until explicitly released after use."
        if capacity is not None:
            message += " " + capacity.message
        return self._issue(CachePreparationPreview(uuid4().hex, token, _policy_digest(value), requirements, rows, qualification,
            new_bytes, charged, policy.cache_limit_bytes, message, capacity, policy.cache_limit_mode))

    def _directory(self, operation):
        if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
            raise ModelResourceError("Choose an exact cache operation ID.")
        return self._guard(self.registry.path.parent / "model-cache-recovery" / operation)

    def _save(self, value, token):
        model_storage_roots(value)
        self.registry._save(value, token)
        current, token = self.registry.snapshot()
        if current != value:
            raise ModelResourceError("The registry changed during cache publication. Inspect recovery.")
        return token

    def _journal(self, value, token, kind, baseline, operation):
        state = cache_state(value, create=True)
        if len(state["operations"]) >= 10000:
            raise ModelResourceError("Cache operation history is full. Review retained history first.")
        directory = self._directory(operation)
        directory.mkdir(parents=True, exist_ok=False)
        baseline = {**baseline, "schema_version": 1, "kind": kind, "operation_id": operation}
        if len(_bytes(baseline)) > 4 * 1024**2:
            raise ModelResourceError("This cache operation's recovery evidence exceeds its supported size.")
        write_verified_json(directory / "baseline.json", baseline)
        state["operations"][operation] = {"kind": kind, "phase": "prepared", "baseline_digest": digest(baseline), "created_at": now()}
        return baseline

    @staticmethod
    def _bindings(state, operation):
        result = []
        for key in operation.get("entry_ids", []):
            entry = state["entries"].get(key)
            if entry and entry.get("state") == "available":
                context = operation.get("binding_contexts", {}).get(key, entry)
                result.append(CacheBinding(key, entry["resource_id"], Path(entry["path"]), context["role"], context["family"], context["media_type"]))
        return tuple(result)

    def _outcome(self, value, operation_id, status, message):
        state = cache_state(value)
        operation = state["operations"][operation_id]
        return CachePreparationOutcome(operation_id, status, self._bindings(state, operation), operation.get("manual_pin_id", ""),
            operation.get("qualification", "unknown"), message, operation.get("execution_pin_id", ""))

    def _operation_reference(self, value, operation_id, *, preparation_owner=None):
        _, baseline = self._load_operation(value, operation_id)
        path = self._directory(operation_id) / "baseline.json"
        raw = _bounded_read(path, 4 * 1024**2)
        if raw is None or _json(raw) != baseline:
            raise ModelResourceError("Cache receipt changed before binding; inspect recovery.")
        related = []
        if preparation_owner is not None and (baseline["kind"] != "cleanup"
                or baseline.get("preparation_id") != preparation_owner or baseline.get("space_cleanup_id")):
            raise ModelResourceError("Cache cleanup lost its exact preparation owner; inspect recovery.")
        if baseline.get("space_cleanup_id"):
            if baseline["kind"] != "prepare":
                raise ModelResourceError("Invalid linked cache cleanup; inspect recovery.")
            related.append(self._operation_reference(value, baseline["space_cleanup_id"], preparation_owner=operation_id))
        return _immutable_operation_reference("model_cache", operation_id, path, raw,
            kind=baseline["kind"], related_receipts=related)

    def _bind_operation(self, value, token, operation_id, callback):
        if callback is None:
            return
        reference = self._operation_reference(value, operation_id)
        _notify_operation_started(callback, reference)
        if self.registry.snapshot()[1] != token or self._operation_reference(value, operation_id) != reference:
            raise ModelResourceError("Cache recovery evidence changed while binding; inspect recovery.")

    def apply_prepare(self, preview, *, confirmed=False, should_cancel=None, progress=None, operation_started=None):
        self._accept(preview, CachePreparationPreview, confirmed)
        if not preview.can_prepare:
            raise ModelResourceError("Resolve every held member before preparing the required set.")
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer(), ExitStack() as pins:
            value, token = self.registry.snapshot()
            if token != preview.registry_token or _policy_digest(value) != preview.policy_digest:
                raise ModelResourceError("The registry or model storage policy changed. Review preparation again.")
            fresh = self.preview_prepare(preview.requirements, expected_registry=token, should_cancel=should_cancel, progress=progress)
            if fresh.rows != preview.rows or fresh.new_bytes != preview.new_bytes or fresh.charged_bytes != preview.charged_bytes:
                raise ModelResourceError("The exact preparation changed. Review again.")
            if (not fresh.can_prepare or (fresh.capacity is None) != (preview.capacity is None)
                    or fresh.capacity is not None and (fresh.capacity.victims != preview.capacity.victims
                        or fresh.capacity.usage_token != preview.capacity.usage_token
                        or fresh.capacity.volume_id != preview.capacity.volume_id)):
                raise ModelResourceError("The exact capacity decision changed. Review preparation again.")
            self._issued.pop(fresh.preview_id, None)
            del self._issued[preview.preview_id]
            _cancel(should_cancel)
            # Pin originals and destination ancestors before any publication.
            for row in preview.rows:
                pins.enter_context(_pinned_model_file(row.resource.archive_path, self.protected_roots))
                current = inspect_model_resource(row.resource.archive_path, should_cancel=should_cancel, progress=progress)
                if current != row.inspection:
                    raise ModelResourceError("Archive source changed after review.")
                if row.status == "copy":
                    pins.enter_context(_pinned_parent(row.ancestor, self.protected_roots))
                    info = row.ancestor.stat(follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != row.ancestor_identity:
                        raise ModelResourceError("Fast destination ancestor changed.")
                    current_parent, missing = row.destination.parent, []
                    while not current_parent.exists():
                        missing.append(current_parent); current_parent = current_parent.parent
                    for directory in reversed(missing):
                        self._guard(directory); directory.mkdir(exist_ok=False)
                pins.enter_context(_pinned_parent(row.destination.parent, self.protected_roots))
            operation_id = uuid4().hex
            cleanup_id = uuid4().hex if preview.capacity is not None and preview.capacity.victims else ""
            baseline_rows = []
            for row in preview.rows:
                entry_id = row.entry_id or uuid4().hex
                record = value["resources"][row.resource.resource_id]
                source_row = next(deepcopy(loc) for loc in record["locations"] if _key(loc["path"]) == _key(row.resource.archive_path))
                parent = row.destination.parent.stat(follow_symlinks=False)
                baseline_rows.append({"resource": _plain(row.resource), "entry_id": entry_id, "destination": str(row.destination),
                    "source_row": source_row, "source_state": list(row.inspection.file_state), "identity": row.inspection.stored_identity(),
                    "parent_identity": [parent.st_dev, parent.st_ino], "existing": row.status == "cached",
                    "stage": str(row.destination.parent / (".collectigen-cache-" + operation_id + "-" + entry_id) / row.destination.name)})
            baseline = self._journal(value, token, "prepare", {"rows": baseline_rows, "requirements": _plain(preview.requirements),
                "policy_digest": preview.policy_digest,
                **({"space_cleanup_id": cleanup_id, "capacity": _plain(preview.capacity)} if cleanup_id else {})}, operation_id)
            state = cache_state(value)
            operation = state["operations"][operation_id]
            operation.update(qualification=preview.qualification, entry_ids=[], members=[{"entry_id": row["entry_id"], "phase": "pending", "size": 0 if row["existing"] else row["identity"]["size"]}
                for row in baseline_rows])
            operation["root_generation"] = root_generation(value["workspace"]["storage_policy"]["fast_root"])
            operation["binding_contexts"] = {row["entry_id"]: {key: row["resource"][key] for key in ("role", "family", "media_type")} for row in baseline_rows}
            if cleanup_id:
                # This parent-owned reservation survives manual-pin release,
                # partial cleanup, cancellation and the publication handoff.
                operation["reserved_entry_ids"] = [row["entry_id"] for row in baseline_rows]
                operation["space_cleanup_id"] = cleanup_id
            if self.execution_reserver is None:
                operation["manual_pin_id"] = add_manual_pin(state, [], operation_id)
            else:
                if preview.qualification != "adapter_qualified":
                    raise ModelResourceError("Execution preparation requires a qualified exact resource set.")
                pin_id = self.execution_reserver(value, operation_id, baseline)
                pin = state["leases"].get(pin_id, {})
                if (pin.get("kind") != "execution_preparation" or pin.get("state") != "active"
                        or pin.get("operation_id") != operation_id
                        or set(pin.get("reserved_entry_ids", [])) != {row["entry_id"] for row in baseline_rows}
                        or not {row["entry_id"] for row in baseline_rows if row["existing"]} <= set(pin.get("entry_ids", []))):
                    raise ModelResourceError("The execution owner did not reserve every cache member.")
                operation["execution_pin_id"] = pin_id
            if cleanup_id:
                entries = [deepcopy(self._qualify_cleanup(value, row.entry_id, should_cancel=should_cancel)) for row in preview.capacity.victims]
                cleanup_baseline = self._journal(value, token, "cleanup", {
                    "entry_ids": [row.entry_id for row in preview.capacity.victims], "entries": entries,
                    "preparation_id": operation_id}, cleanup_id)
                state["operations"][cleanup_id].update(entry_ids=cleanup_baseline["entry_ids"], retired=[], preparation_id=operation_id)
            try:
                token = self._save(value, token)
                # Empty destination directories may precede the receipt; they
                # are retained scaffolding, not discoverable operation authority.
                self._bind_operation(value, token, operation_id, operation_started)
                _cancel(should_cancel)
                return self._finish_prepare(value, token, operation_id, baseline, should_cancel, progress)
            except (OSError, RuntimeError, ValueError) as exc:
                return self._outcome(value, operation_id, "recovery_required", "Cache preparation stopped; originals remain. Inspect recovery. " + str(exc))

    def _member(self, operation, entry_id):
        return next(item for item in operation["members"] if item["entry_id"] == entry_id)

    def _finish_prepare(self, value, token, operation_id, baseline, should_cancel, progress):
        state = cache_state(value)
        operation = state["operations"][operation_id]
        execution_pin = operation.get("execution_pin_id")
        if execution_pin:
            pin = state["leases"].get(execution_pin, {})
            if (pin.get("kind") != "execution_preparation" or pin.get("state") != "active"
                    or pin.get("operation_id") != operation_id
                    or not {row["entry_id"] for row in baseline["rows"]} <= set(pin.get("reserved_entry_ids", []))):
                raise ModelResourceError("Execution preparation protection changed; preserve recovery evidence.")
        elif state["leases"].get(operation["manual_pin_id"], {}).get("state") != "active":
            operation["manual_pin_id"] = add_manual_pin(state, operation["entry_ids"], operation_id)
            token = self._save(value, token)
        if baseline.get("space_cleanup_id"):
            room_progress = (lambda current, total, message: progress(current, total,
                "Preparing complete set — phase 1/2, making room: " + message)) if progress else None
            token = self._make_room(value, token, operation_id, baseline, should_cancel, room_progress)
            if progress:
                original_progress = progress
                progress = lambda current, total, message: original_progress(current, total,
                    "Preparing complete set — phase 2/2, copying and verifying inputs: " + message)
        for row in baseline["rows"]:
            _cancel(should_cancel)
            member = self._member(operation, row["entry_id"])
            resource = row["resource"]
            record = value["resources"].get(resource["resource_id"])
            if (not record or record["identity"] != row["identity"] or
                    [loc for loc in record["locations"] if _key(loc["path"]) == _key(resource["archive_path"])] != [row["source_row"]]):
                raise ModelResourceError("The original archive registry projection changed. Preserve recovery evidence.")
            if member["phase"] == "available":
                self._verify_entry(value, row["entry_id"], should_cancel=should_cancel, progress=progress)
                continue
            archive, destination, stage = Path(resource["archive_path"]), Path(row["destination"]), Path(row["stage"])
            self._guard(archive); self._guard(destination); self._guard(stage)
            with _pinned_model_file(archive, self.protected_roots), _pinned_parent(destination.parent, self.protected_roots):
                original = inspect_model_resource(archive, should_cancel=should_cancel, progress=progress)
                if original.stored_identity() != row["identity"] or list(original.file_state) != row["source_state"]:
                    raise ModelResourceError("Archive source changed. Preserve the cache attempt for review.")
                if row["existing"]:
                    self._verify_entry(value, row["entry_id"], should_cancel=should_cancel, progress=progress)
                elif member["phase"] == "published":
                    self._verify_entry(value, row["entry_id"], should_cancel=should_cancel, progress=progress)
                    if stage.exists():
                        with pinned_model_retirement(stage, member["file_state"], protected_roots=self.protected_roots, expected_links=2) as owned:
                            if _identity(destination.stat(follow_symlinks=False)) != tuple(member["file_state"]):
                                raise ModelResourceError("Fast destination changed; preserve the staging link.")
                            owned.retire()
                else:
                    parent = destination.parent.stat(follow_symlinks=False)
                    if [parent.st_dev, parent.st_ino] != row["parent_identity"]:
                        raise ModelResourceError("Fast destination folder changed.")
                    if member["phase"] == "pending":
                        if destination.exists() or stage.exists():
                            raise ModelResourceError("An unowned destination or staging file appeared. Nothing is replaced.")
                        if stage.parent.exists():
                            parent = stage.parent.stat(follow_symlinks=False)
                            if member.get("stage_parent") != [parent.st_dev, parent.st_ino]:
                                raise ModelResourceError("The retained staging folder is not owned by this attempt.")
                        else:
                            stage.parent.mkdir(exist_ok=False)
                            parent = stage.parent.stat(follow_symlinks=False)
                            member["stage_parent"] = [parent.st_dev, parent.st_ino]
                            token = self._save(value, token)
                        with _pinned_parent(stage.parent, self.protected_roots), stage.open("xb") as writer:
                            created = os.fstat(writer.fileno())
                            member.update(phase="copying", stage_object=[created.st_dev, created.st_ino])
                            token = self._save(value, token)
                            hashed, copied = hashlib.sha256(), 0
                            with archive.open("rb") as reader:
                                while chunk := reader.read(1024 * 1024):
                                    _cancel(should_cancel)
                                    writer.write(chunk); hashed.update(chunk); copied += len(chunk)
                                    if progress:
                                        progress(copied, original.size, "Copying selected model to Fast storage")
                            writer.flush(); os.fsync(writer.fileno())
                            if copied != original.size or hashed.hexdigest() != original.resource_id[7:]:
                                raise ModelResourceError("Staged bytes differ from the archive original.")
                    if member["phase"] == "copying":
                        if not stage.exists():
                            # The archive is pinned and unchanged. Recreating a
                            # missing partial stage cannot discard user bytes;
                            # exclusive creation still guards any racing file.
                            member["phase"] = "pending"
                            token = self._save(value, token)
                            return self._finish_prepare(value, token, operation_id, baseline, should_cancel, progress)
                        # A clean interruption can retain an incomplete file.
                        # Recovery only accepts the exact receipt-owned object
                        # and a byte-for-byte prefix of the unchanged archive.
                        info = stage.stat(follow_symlinks=False)
                        if [info.st_dev, info.st_ino] != member["stage_object"]:
                            raise ModelResourceError("The staging object was replaced. Preserve it for review.")
                        if info.st_size != original.size:
                            self._retire_partial(stage, archive, member, original, should_cancel)
                            member.update(phase="pending")
                            token = self._save(value, token)
                            # Retained empty directory is ours. Restart the
                            # exact member from byte zero, never append to an
                            # unverified partial stream.
                            return self._finish_prepare(value, token, operation_id, baseline, should_cancel, progress)
                        with _pinned_model_file(stage, self.protected_roots):
                            staged = inspect_model_resource(stage, should_cancel=should_cancel, progress=progress)
                            if staged.stored_identity() != row["identity"]:
                                raise ModelResourceError("Staged model bytes changed; preserve them for review.")
                            member.update(phase="verified", file_state=list(staged.file_state))
                            token = self._save(value, token)
                    published_source = stage if stage.exists() else destination
                    with _pinned_model_file(published_source, self.protected_roots):
                        staged = inspect_model_resource(published_source, should_cancel=should_cancel, progress=progress)
                        if list(staged.file_state) != member["file_state"] or staged.stored_identity() != row["identity"]:
                            raise ModelResourceError("Verified staging changed. Preserve recovery evidence.")
                        if not destination.exists():
                            _cancel(should_cancel)
                            os.link(stage, destination)
                        with _pinned_model_file(destination, self.protected_roots):
                            published = inspect_model_resource(destination)
                            if published.file_state != staged.file_state:
                                raise ModelResourceError("An unrelated Fast destination exists. It will not be replaced.")
                            entry = {"resource_id": resource["resource_id"], "path": str(destination), "file_state": list(published.file_state),
                                "archive_path": str(archive), "archive_state": row["source_state"], "archive_row": row["source_row"],
                                "identity": row["identity"], "role": resource["role"], "family": resource["family"], "media_type": resource["media_type"],
                                "operation_id": operation_id, "root_generation": operation["root_generation"], "size": published.size, "state": "available", "last_prepared_at": now()}
                            locations = value["resources"][resource["resource_id"]]["locations"]
                            present = [loc for loc in locations if _key(loc["path"]) == _key(destination)]
                            location = {"path": str(destination), "file_state": list(published.file_state), "cache_entry_id": row["entry_id"]}
                            if present and present != [location]:
                                raise ModelResourceError("The Fast registry location changed. Preserve it for review.")
                            if not present:
                                locations.append(location)
                            state["entries"][row["entry_id"]] = entry
                            member["phase"] = "published"
                            self._pin_member(state, operation, row["entry_id"])
                            token = self._save(value, token)
                    # Delete only this operation's exact staging link while
                    # another exact same-object destination is still present.
                    if stage.exists():
                        with pinned_model_retirement(stage, member["file_state"], protected_roots=self.protected_roots, expected_links=2) as owned:
                            if _identity(destination.stat(follow_symlinks=False)) != tuple(member["file_state"]):
                                raise ModelResourceError("Fast destination changed; retain the staging link.")
                            owned.retire()
                context = operation["binding_contexts"][row["entry_id"]]
                contexts = state["entries"][row["entry_id"]].setdefault("contexts", [])
                if not isinstance(contexts, list) or any(not isinstance(item, dict) for item in contexts):
                    raise ModelResourceError("The cache copy's reviewed role contexts are invalid. Preserve it for review.")
                if context not in contexts:
                    contexts.append(deepcopy(context))
                self._pin_member(state, operation, row["entry_id"])
                with _pinned_model_file(destination, self.protected_roots):
                    self._verify_entry(value, row["entry_id"])
                    if destination.stat().st_nlink != 1:
                        raise ModelResourceError("The prepared destination has linked aliases. Inspect recovery.")
                member["phase"] = "available"
                token = self._save(value, token)
        # Do not expose a launch-ready set before ALL files are verified. The
        # manual pin survives partial failure and applies to every completed row.
        with ExitStack() as pins:
            for entry_id in operation["entry_ids"]:
                entry = state["entries"][entry_id]
                pins.enter_context(_pinned_model_file(Path(entry["path"]), self.protected_roots))
                self._verify_entry(value, entry_id)
            operation["phase"] = "complete"
            token = self._save(value, token)
        return self._outcome(value, operation_id, "complete", "Selected fast copies verified and protected; archive originals retained.")

    def _make_room(self, value, token, operation_id, baseline, should_cancel, progress):
        """Resume only the parent's fixed reviewed victims before any new copy."""
        state = cache_state(value)
        operation = state["operations"][operation_id]
        capacity = retained_capacity(baseline["capacity"])
        cleanup_id = baseline["space_cleanup_id"]
        cleanup, cleanup_baseline = self._load_operation(value, cleanup_id)
        if (operation.get("space_cleanup_id") != cleanup_id or cleanup.get("preparation_id") != operation_id
                or cleanup_baseline.get("preparation_id") != operation_id
                or cleanup_baseline["entry_ids"] != [row.entry_id for row in capacity.victims]
                or not capacity.ready or not capacity.victims
                or operation.get("reserved_entry_ids") != [row["entry_id"] for row in baseline["rows"]]):
            raise ModelResourceError("The capacity cleanup lost its exact incoming preparation owner.")
        if _policy_digest(value) != baseline["policy_digest"]:
            raise ModelResourceError("Storage policy changed; preserve this exact space-management recovery.")
        policy = validate_policy_roots(value["workspace"], protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        if policy.mode != "tiered" or policy.automatic_cleanup != "least_used":
            raise ModelResourceError("Automatic space management is no longer enabled; preserve recovery.")
        # Keep every incoming original and parent pinned until cleanup finishes.
        with ExitStack() as pins:
            for row in baseline["rows"]:
                _cancel(should_cancel)
                source = self._guard(row["resource"]["archive_path"])
                pins.enter_context(_pinned_model_file(source, self.protected_roots))
                inspected = inspect_model_resource(source, should_cancel=should_cancel, progress=progress)
                record = value["resources"].get(row["resource"]["resource_id"])
                if (inspected.stored_identity() != row["identity"] or list(inspected.file_state) != row["source_state"]
                        or not record or record["identity"] != row["identity"]
                        or [loc for loc in record["locations"] if _key(loc["path"]) == _key(source)] != [row["source_row"]]):
                    raise ModelResourceError("An incoming original changed. No further cache copies will be removed.")
                destination = self._guard(row["destination"])
                pins.enter_context(_pinned_parent(destination.parent, self.protected_roots))
                info = destination.parent.stat(follow_symlinks=False)
                if [info.st_dev, info.st_ino] != row["parent_identity"] or info.st_dev != capacity.volume_id:
                    raise ModelResourceError("The incoming Fast volume changed. Preserve recovery.")
                if row["existing"]:
                    pins.enter_context(_pinned_model_file(destination, self.protected_roots))
                    self._verify_entry(value, row["entry_id"], should_cancel=should_cancel, progress=progress)
                elif cleanup["phase"] != "complete" and (destination.exists() or self._guard(row["stage"]).exists()):
                    raise ModelResourceError("An incoming destination became occupied before cleanup. Nothing will be overwritten.")
            if cleanup["phase"] != "complete":
                if victim_usage_token(value, [row.resource_id for row in capacity.victims]) != capacity.usage_token:
                    raise ModelResourceError("Victim use history changed or was reset. Review the retained exact cleanup; no reselection is allowed.")
                incoming = {row["resource"]["resource_id"] for row in baseline["rows"]}
                for victim, entry in zip(capacity.victims, cleanup_baseline["entries"]):
                    if victim.resource_id in incoming or digest(entry) != victim.entry_digest:
                        raise ModelResourceError("The reviewed cleanup conflicts with incoming resources or changed ownership.")
                # Qualify EVERY remaining victim before the first irreversible
                # retirement; a partial set is never used to improvise space.
                remaining = []
                for key in cleanup_baseline["entry_ids"]:
                    if key in cleanup["retired"]:
                        continue
                    entry = self._qualify_cleanup(value, key,
                        allow_missing=cleanup.get("retiring") == key, should_cancel=should_cancel)
                    if entry["file_state"][0] != capacity.volume_id:
                        raise ModelResourceError("A cleanup candidate changed its Fast volume.")
                    remaining.append(entry)
                pending_bytes = sum(row["size"] for row in operation["members"] if row["phase"] not in ("published", "available"))
                free = shutil.disk_usage(policy.fast_root).free
                retiring_entry = state["entries"].get(cleanup.get("retiring"))
                already_retired_bytes = (retiring_entry["size"] if retiring_entry is not None
                    and cleanup.get("retiring") in cleanup_baseline["entry_ids"]
                    and not Path(retiring_entry["path"]).exists() else 0)
                shortfall = max(0, self._charged(state) - already_retired_bytes - policy.cache_limit_bytes
                    if policy.cache_limit_mode == "fixed" else 0,
                    pending_bytes + policy.minimum_free_bytes - free)
                reclaimable = sum(entry["size"] for entry in remaining if Path(entry["path"]).exists())
                if reclaimable < shortfall:
                    raise ModelResourceError("The complete incoming set no longer fits this exact cleanup. No additional victims will be selected.")
                if progress:
                    progress(0, 0, "Making room using the exact reviewed least-used Fast copies")
                self._finish_cleanup(value, token, cleanup_id, cleanup_baseline, should_cancel,
                    preparation_owner=operation_id, progress=progress)
                token = self.registry.snapshot()[1]
            pending_bytes = self._copy_space_needed(operation, baseline, pins)
            if ((policy.cache_limit_mode == "fixed" and self._charged(state) > policy.cache_limit_bytes)
                    or shutil.disk_usage(policy.fast_root).free < pending_bytes + policy.minimum_free_bytes):
                raise ModelResourceError("Space changed after cleanup. Retain this attempt; no extra copies will be removed.")
        return token

    def _copy_space_needed(self, operation, baseline, pins):
        """Credit only an exact owned staging object already occupying the disk.

        The copy/recovery boundary independently rechecks its bytes before reuse
        or prefix retirement. This calculation never deletes or trusts a foreign
        stage, and a verified stage needs no second file-sized space reservation.
        """
        required = 0
        for row in baseline["rows"]:
            member = self._member(operation, row["entry_id"])
            if member["phase"] in ("published", "available"):
                continue
            size = member["size"]
            if member["phase"] in ("copying", "verified"):
                stage = self._guard(row["stage"])
                if stage.exists():
                    pins.enter_context(_pinned_model_file(stage, self.protected_roots))
                    info = stage.stat(follow_symlinks=False)
                    expected = member.get("stage_object") if member["phase"] == "copying" else member.get("file_state", [])[:2]
                    if [info.st_dev, info.st_ino] != expected or not 0 <= info.st_size <= size:
                        raise ModelResourceError("The owned staging object changed; preserve recovery.")
                    size -= info.st_size
            required += size
        return required

    @staticmethod
    def _pin_member(state, operation, entry_id):
        if entry_id not in operation["entry_ids"]:
            operation["entry_ids"].append(entry_id)
        pin = state["leases"][operation.get("execution_pin_id") or operation["manual_pin_id"]]
        if pin.get("state") != "active":
            raise ModelResourceError("The preparation's protection changed. Preserve its copies.")
        if entry_id not in pin["entry_ids"]:
            pin["entry_ids"].append(entry_id)

    def _retire_partial(self, stage, archive, member, original, should_cancel):
        with pinned_model_retirement(stage, _identity(stage.stat(follow_symlinks=False)), protected_roots=self.protected_roots) as owned:
            if list(owned.expected[:2]) != member["stage_object"]:
                raise ModelResourceError("Partial staging was replaced. Preserve it for review.")
            with archive.open("rb") as reader:
                while chunk := owned.reader.read(1024 * 1024):
                    _cancel(should_cancel)
                    if reader.read(len(chunk)) != chunk:
                        raise ModelResourceError("Partial staging was modified. Preserve it for review.")
            owned.retire()

    def _verify_entry(self, value, entry_id, *, should_cancel=None, progress=None, allow_missing=False):
        return self._verify_entry_with_inspection(value, entry_id, should_cancel=should_cancel,
            progress=progress, allow_missing=allow_missing)[0]

    def _verify_entry_with_inspection(self, value, entry_id, *, should_cancel=None, progress=None, allow_missing=False):
        """Return the existing verification result and its one selected-file inspection."""
        entry = cache_state(value)["entries"].get(entry_id)
        if not entry or entry.get("state") != "available":
            raise ModelResourceError("Choose a recorded available cache copy.")
        owner, receipt = self._load_operation(value, entry.get("operation_id"))
        rows = [row for row in receipt.get("rows", []) if row.get("entry_id") == entry_id]
        if len(rows) != 1:
            raise ModelResourceError("The cache copy has no exact preparation receipt.")
        row = rows[0]
        member = self._member(owner, entry_id)
        if (entry["path"] != row["destination"] or entry["archive_path"] != row["resource"]["archive_path"]
                or entry["resource_id"] != row["resource"]["resource_id"] or entry["identity"] != row["identity"]
                or entry["archive_row"] != row["source_row"] or entry["archive_state"] != row["source_state"]
                or member.get("file_state") != entry["file_state"] or member.get("phase") not in ("published", "available")):
            raise ModelResourceError("The cache copy differs from its exact ownership receipt.")
        path = self._guard(entry["path"])
        record = value["resources"].get(entry["resource_id"])
        if not record or record["identity"] != entry["identity"]:
            raise ModelResourceError("The cached resource identity changed in the registry.")
        expected = {"path": str(path), "file_state": entry["file_state"], "cache_entry_id": entry_id}
        if [row for row in record["locations"] if _key(row["path"]) == _key(path)] != [expected]:
            raise ModelResourceError("The cache-owned registry location changed.")
        if not path.exists() and allow_missing:
            return entry, None
        inspected = inspect_model_resource(path, should_cancel=should_cancel, progress=progress)
        if inspected.stored_identity() != entry["identity"] or list(inspected.file_state) != entry["file_state"]:
            raise ModelResourceError("The cache file changed or was replaced. It is not eligible for reuse or cleanup.")
        return entry, inspected

    def _qualify_cleanup(self, value, entry_id, *, allow_missing=False, should_cancel=None, progress=None):
        state = cache_state(value)
        if entry_id in pinned_entries(state):
            raise ModelResourceError("This cache copy is manually pinned or has active/uncertain use. Release or reconcile its owner first.")
        entry = self._verify_entry(value, entry_id, allow_missing=allow_missing, should_cancel=should_cancel, progress=progress)
        policy = validate_policy_roots(value.get("workspace", {}), protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        contexts = entry.get("contexts", [{key: entry[key] for key in ("role", "family", "media_type")}])
        if not isinstance(contexts, list) or not contexts or any(not isinstance(item, dict) or set(item) != {"role", "family", "media_type"} for item in contexts):
            raise ModelResourceError("Cache role policy context is invalid. Preserve its copy.")
        for context in contexts:
            effective = effective_policy(policy, resource_id=entry["resource_id"], **context)
            if effective.policy == "keep_fast":
                raise ModelResourceError("Keep-on-Fast policy protects this copy. Review its retention preference first.")
        archive = self._guard(entry["archive_path"])
        if _key(archive) == _key(entry["path"]) or any(_key(other.get("path", "")) == _key(archive) for other in state["entries"].values()):
            raise ModelResourceError("Cleanup requires an independent permanent archive original, not another cache copy.")
        record = value["resources"][entry["resource_id"]]
        if [row for row in record["locations"] if _key(row["path"]) == _key(archive)] != [entry["archive_row"]]:
            raise ModelResourceError("The archive's registered location changed. Preserve the cache copy.")
        original = inspect_model_resource(archive, should_cancel=should_cancel, progress=progress)
        if original.stored_identity() != entry["identity"] or list(original.file_state) != entry["archive_state"]:
            raise ModelResourceError("The verified archive original is unavailable or changed. Protect the cache copy.")
        path = Path(entry["path"])
        if path.exists() and path.stat().st_nlink != 1:
            raise ModelResourceError("This cache copy has linked aliases. Preserve it for review.")
        return entry

    def recommend_cleanup(self, target_bytes, *, expected_registry, should_cancel=None):
        """Issue an explicit maintenance recommendation; never enable auto cleanup."""
        value,token=self.registry.snapshot()
        if token!=expected_registry:
            raise ModelResourceError("The registry changed. Review cleanup again.")
        policy=validate_policy_roots(value.get("workspace",{}),protected_roots=self.protected_roots,managed_roots=self.managed_roots)
        if policy.mode!="tiered":
            raise ModelResourceError("Configure Fast storage before reviewing expendable copies; permanent originals are not cleanup candidates.")
        state=cache_state(value);model_storage_roots(value)
        if any(operation.get("phase")!="complete" for operation in state["operations"].values()):
            raise ModelResourceError("Recover unfinished cache work before recommending cleanup.")
        root=self._guard(Path(policy.fast_root))
        while not root.exists() and root!=root.parent:root=root.parent
        volume_id=root.stat(follow_symlinks=False).st_dev
        recommendation=review_cleanup_recommendation(value,policy=policy,target_bytes=target_bytes,
            volume_id=volume_id,qualify=lambda key:self._qualify_cleanup(value,key,should_cancel=should_cancel),
            should_cancel=should_cancel)
        if self.registry.snapshot()[1]!=token:
            raise ModelResourceError("Registry or successful-use evidence changed during recommendation. Review again.")
        if not recommendation.ready:return recommendation,None
        preview=self.preview_cleanup(recommendation.entry_ids,expected_registry=token,should_cancel=should_cancel)
        preview=self._issue(replace(preview,usage_token=victim_usage_token(value,
            [row.resource_id for row in recommendation.victims])))
        return recommendation,preview

    def preview_cleanup(self, entry_ids, *, expected_registry, should_cancel=None):
        from itertools import islice
        ids = tuple(islice(iter(entry_ids), MAX_MEMBERS + 1))
        if not 1 <= len(ids) <= MAX_MEMBERS or any(not isinstance(key, str) for key in ids) or len(set(ids)) != len(ids):
            raise ModelResourceError("Choose distinct exact cache entries for cleanup.")
        value, token = self.registry.snapshot()
        if token != expected_registry:
            raise ModelResourceError("The registry changed. Review cleanup again.")
        if any(op.get("phase") != "complete" for op in cache_state(value)["operations"].values()):
            raise ModelResourceError("Recover unfinished cache work before reviewing cleanup.")
        entries = [self._qualify_cleanup(value, key, should_cancel=should_cancel) for key in ids]
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed during cleanup preview.")
        return self._issue(CacheCleanupPreview(uuid4().hex, token, ids, digest(entries), sum(entry["size"] for entry in entries)))

    def apply_cleanup(self, preview, *, confirmed=False, should_cancel=None, operation_started=None):
        self._accept(preview, CacheCleanupPreview, confirmed)
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            value, token = self.registry.snapshot()
            if token != preview.registry_token:
                raise ModelResourceError("The registry or active-use state changed. Review cleanup again.")
            entries = [self._qualify_cleanup(value, key, should_cancel=should_cancel) for key in preview.entry_ids]
            if digest(entries) != preview.entries_digest:
                raise ModelResourceError("The exact cache entries changed. Review cleanup again.")
            if preview.usage_token and victim_usage_token(value,[entry["resource_id"] for entry in entries])!=preview.usage_token:
                raise ModelResourceError("Successful-use evidence changed. Review the cleanup recommendation again.")
            _cancel(should_cancel)
            operation_id = uuid4().hex
            baseline = self._journal(value, token, "cleanup", {"entry_ids": list(preview.entry_ids), "entries": deepcopy(entries),
                **({"usage_token":preview.usage_token} if preview.usage_token else {})}, operation_id)
            operation = cache_state(value)["operations"][operation_id]
            operation.update(entry_ids=list(preview.entry_ids), retired=[])
            del self._issued[preview.preview_id]
            try:
                token = self._save(value, token)
                self._bind_operation(value, token, operation_id, operation_started)
                _cancel(should_cancel)
                return self._finish_cleanup(value, token, operation_id, baseline, should_cancel)
            except (OSError, RuntimeError, ValueError) as exc:
                return self._outcome(value, operation_id, "recovery_required", "Cleanup stopped; originals retained. Inspect recovery. " + str(exc))

    def _finish_cleanup(self, value, token, operation_id, baseline, should_cancel, *, preparation_owner=None, progress=None):
        if baseline.get("preparation_id") and baseline["preparation_id"] != preparation_owner:
            raise ModelResourceError("Recover this cleanup through its exact incoming preparation owner.")
        state = cache_state(value)
        operation = state["operations"][operation_id]
        if (baseline.get("usage_token") and victim_usage_token(value,
                [entry["resource_id"] for entry in baseline["entries"]])!=baseline["usage_token"]):
            raise ModelResourceError("Successful-use evidence changed after the recommended cleanup began. Preserve the remaining copies for exact review.")
        total_bytes = sum(entry["size"] for entry in baseline["entries"])
        for entry_id, original_entry in zip(baseline["entry_ids"], baseline["entries"]):
            if entry_id in operation["retired"]:
                continue
            if progress:
                progress(sum(entry["size"] for key, entry in zip(baseline["entry_ids"], baseline["entries"])
                    if key in operation["retired"]), total_bytes,
                    "Verifying Archive proof and removing the next exact reviewed Fast copy")
            _cancel(should_cancel)
            entry = self._qualify_cleanup(value, entry_id, allow_missing=operation.get("retiring") == entry_id, should_cancel=should_cancel)
            if entry != original_entry:
                raise ModelResourceError("Cleanup-owned entry changed. Preserve it for review.")
            archive, path = Path(entry["archive_path"]), Path(entry["path"])
            with _pinned_model_file(archive, self.protected_roots):
                inspected = inspect_model_resource(archive, should_cancel=should_cancel)
                if list(inspected.file_state) != entry["archive_state"] or inspected.stored_identity() != entry["identity"]:
                    raise ModelResourceError("Archive changed before cleanup. Protect the cache copy.")
                operation["retiring"] = entry_id
                token = self._save(value, token)
                if path.exists():
                    with pinned_model_retirement(path, entry["file_state"], protected_roots=self.protected_roots) as owned:
                        owned.verify(entry["resource_id"][7:], entry["size"], should_cancel=should_cancel)
                        owned.retire()
                locations = value["resources"][entry["resource_id"]]["locations"]
                exact_location = {"path": entry["path"], "file_state": entry["file_state"], "cache_entry_id": entry_id}
                value["resources"][entry["resource_id"]]["locations"] = [loc for loc in locations if loc != exact_location]
                entry["state"] = "retired"
                entry["retired_at"] = now()
                operation["retired"].append(entry_id)
                operation.pop("retiring", None)
                token = self._save(value, token)
        operation["phase"] = "complete"
        self._save(value, token)
        if progress:
            progress(total_bytes, total_bytes, "Reviewed Fast copies removed; Archive originals retained")
        return self._outcome(value, operation_id, "complete", "Only the reviewed owned fast copies were removed; archive originals verified and retained.")

    def _load_operation(self, value, operation_id):
        model_storage_roots(value)
        operation = cache_state(value)["operations"].get(operation_id)
        if not operation or operation.get("kind") not in ("prepare", "cleanup"):
            raise ModelResourceError("Choose a recorded cache operation.")
        raw = _bounded_read(self._directory(operation_id) / "baseline.json", 4 * 1024**2)
        baseline = _json(raw or b"null")
        if not isinstance(baseline, dict) or type(baseline.get("schema_version")) is not int or baseline.get("schema_version") != 1 or baseline.get("operation_id") != operation_id or digest(baseline) != operation.get("baseline_digest"):
            raise ModelResourceError("Cache recovery evidence changed or is missing. Preserve it for review.")
        return operation, baseline

    def recovery_summaries(self, operation_ids, *, expected_registry, should_cancel=None):
        """Read one explicit receipt page without inspecting models or writing."""
        ids = tuple(islice(iter(operation_ids),RECOVERY_SUMMARY_PAGE_SIZE+1))
        if (len(ids) > RECOVERY_SUMMARY_PAGE_SIZE
                or any(not isinstance(key,str) or not key or len(key)>256 for key in ids)
                or len(set(ids)) != len(ids)):
            raise ModelResourceError("Choose a bounded page of recorded cache operations.")
        value,token = self.registry.snapshot()
        if token != expected_registry:
            raise ModelResourceError("Recorded state changed. Reload before reading operation details.")
        summaries=[]
        for key in ids:
            _cancel(should_cancel)
            try:
                operation,baseline=self._load_operation(value,key)
                if baseline.get("kind") != operation.get("kind"):
                    raise ModelResourceError("Recorded operation kinds differ.")
                rows=baseline.get("rows") if operation["kind"]=="prepare" else baseline.get("entries")
                if not isinstance(rows,list) or not 1 <= len(rows) <= MAX_MEMBERS:
                    raise ModelResourceError("Recorded model set is unavailable.")
                archives=[]
                for row in rows:
                    resource=row.get("resource") if operation["kind"]=="prepare" and isinstance(row,dict) else row
                    if (not isinstance(resource,dict) or not isinstance(resource.get("resource_id"),str)
                            or re.fullmatch(r"sha256:[0-9a-f]{64}",resource["resource_id"]) is None
                            or not isinstance(resource.get("archive_path"),str)
                            or not resource["archive_path"] or len(resource["archive_path"])>4096):
                        raise ModelResourceError("Recorded model identification is unavailable.")
                    archives.append(resource["archive_path"])
                summaries.append(CacheOperationSummary(key,tuple(archives),"Recorded model set; review before recovery"))
            except (ModelResourceError,OSError,ValueError,TypeError,KeyError):
                summaries.append(CacheOperationSummary(key))
        _cancel(should_cancel)
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("Recorded state changed. Reload before reading operation details.")
        return tuple(summaries)

    def preview_recovery(self, operation_id, *, should_cancel=None):
        value, token = self.registry.snapshot()
        operation, baseline = self._load_operation(value, operation_id)
        if baseline.get("preparation_id"):
            child_id = operation_id
            operation_id = baseline["preparation_id"]
            operation, baseline = self._load_operation(value, operation_id)
            if baseline.get("space_cleanup_id") != child_id:
                raise ModelResourceError("The cleanup lost its exact preparation owner.")
        _cancel(should_cancel)
        for row in baseline.get("rows", []):
            self._guard(row["resource"]["archive_path"]); self._guard(row["destination"]); self._guard(row["stage"])
        for entry in baseline.get("entries", []):
            self._guard(entry["archive_path"]); self._guard(entry["path"])
        message = "Resume only this receipt's exact original paths; current protected boundaries still apply."
        capacity = retained_capacity(baseline.get("capacity"))
        if capacity is not None:
            message += " Resume the same reviewed space-management attempt; already retired Fast copies remain removed and Archive originals remain. No new victims will be selected."
        if operation["kind"] == "prepare" and baseline["policy_digest"] != _policy_digest(value):
            message += " Storage preferences changed; existing cache generation stays separately owned and manually pinned."
        paths = (tuple((row["resource"]["resource_id"], row["resource"]["archive_path"], row["destination"])
                       for row in baseline.get("rows", [])) if operation["kind"] == "prepare" else
                 tuple((entry["resource_id"], entry["archive_path"], entry["path"])
                       for entry in baseline.get("entries", [])))
        if baseline.get("space_cleanup_id"):
            _, cleanup_baseline = self._load_operation(value, baseline["space_cleanup_id"])
            for entry in cleanup_baseline["entries"]:
                self._guard(entry["archive_path"]); self._guard(entry["path"])
            paths += tuple((entry["resource_id"], entry["archive_path"], entry["path"]) for entry in cleanup_baseline["entries"])
        return self._issue(CacheRecoveryPreview(uuid4().hex, token, operation_id, digest(operation), operation["kind"], message, paths, capacity))

    def recover(self, preview, *, confirmed=False, should_cancel=None, progress=None, operation_started=None):
        self._accept(preview, CacheRecoveryPreview, confirmed)
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            value, token = self.registry.snapshot()
            operation, baseline = self._load_operation(value, preview.operation_id)
            if token != preview.registry_token or digest(operation) != preview.operation_digest:
                raise ModelResourceError("Cache recovery state changed. Inspect it again.")
            del self._issued[preview.preview_id]
            if operation["phase"] == "complete":
                return self._outcome(value, preview.operation_id, "complete", "This cache operation is already complete.")
            try:
                _cancel(should_cancel)
                self._bind_operation(value, token, preview.operation_id, operation_started)
                _cancel(should_cancel)
                if operation["kind"] == "prepare":
                    return self._finish_prepare(value, token, preview.operation_id, baseline, should_cancel, progress)
                return self._finish_cleanup(value, token, preview.operation_id, baseline, should_cancel)
            except (OSError, RuntimeError, ValueError) as exc:
                return self._outcome(value, preview.operation_id, "recovery_required", "Cache recovery remains held. " + str(exc))
