"""Reviewed import adapters for existing model relocation, cache and path owners.

Pure effect builders support explicit future prerequisites. Execution always
requires a fresh canonical preview with exactly those displayed consequences.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from pathlib import Path

from .model_cache import CacheResource, ModelRequirementSet, _policy_digest
from .model_engine_locations import project_swarm_hot_folders
from .model_import_plan import OwnerReceipt
from .model_relocation import RelocationSelection, _settings
from .model_resource_registry import ModelResourceError, _cancel, inspect_model_resource
from .model_storage_policy import fast_destination, load_policy
from .model_use_leases import cache_state
from .model_workspace import _path, _pinned_model_file
from .swarm_model_paths import ModelPathInventory


def _plain(value):
    return json.loads(json.dumps(value, default=str))


def desired_move_effects(selection, destination, layout_digest):
    return {"action": "move", "selection": deepcopy(selection), "destination": str(destination),
        "layout_digest": layout_digest, "source_retirement": "after_verified_publication",
        "adjacent_files": "unchanged"}


def move_effects(choices, snapshot):
    return desired_move_effects(choices["selection"], choices["destination"], _settings(snapshot))


def desired_cache_effects(resources, destinations, policy_digest, cleanup, qualification="unknown"):
    return {"action": "prepare_cache", "resources": deepcopy(resources),
        "destinations": [str(path) for path in destinations], "policy_digest": policy_digest,
        "cleanup": deepcopy(cleanup), "qualification": qualification,
        "archive_originals": "retained", "manual_external_use_pin": True}


def _manual_requirements(choices):
    raw = choices["requirements"]
    qualified_request = raw.get("companion_request") is not None
    if (set(raw) - {"resources", "qualification", "adapter_id", "adapter_version", "request_fingerprint", "companion_request"}
            or raw.get("qualification", "unknown") not in ({"unknown", "adapter_qualified"} if qualified_request else {"unknown"})
            or any(raw.get(key, "") for key in ("adapter_id", "adapter_version", "request_fingerprint"))):
        raise ModelResourceError("Manual import preparation has unknown companion qualification; saved adapter labels do not grant readiness.")
    if choices["approved_cleanup"] and not qualified_request:
        raise ModelResourceError("Unknown manual sets cannot trigger automatic cleanup. Review eligible cleanup in Models, then review this import again.")
    if qualified_request:
        _companion_request(raw["companion_request"])
    resources = []
    for row in raw["resources"]:
        if not isinstance(row, dict) or set(row) != {"resource_id", "archive_path", "role", "family", "media_type"}:
            raise ModelResourceError("Choose the exact resource, archive path, type and family for each preparation member.")
        resources.append(CacheResource(**{**row, "archive_path": Path(row["archive_path"])}))
    return ModelRequirementSet(tuple(resources))


def _companion_request(payload):
    from .model_companion_readiness import CompanionChoice, CompanionReadinessRequest
    allowed = {"main_resource_id", "workflow_kind", "target_id", "adapter_id", "adapter_version"}
    if (not isinstance(payload, dict) or set(payload) != {"request", "choices"}
            or not isinstance(payload["request"], dict) or set(payload["request"]) - allowed
            or "main_resource_id" not in payload["request"] or not isinstance(payload["choices"], list)
            or not 1 <= len(payload["choices"]) <= 64):
        raise ModelResourceError("Choose an explicit companion profile and bounded exact slots; saved verification reports are not authority.")
    rows = payload["choices"]
    if any(not isinstance(row, dict) or set(row) != {"slot", "resource_id", "path", "role", "family"} for row in rows):
        raise ModelResourceError("Every companion slot needs its exact selected model, path, role and family.")
    return CompanionReadinessRequest(**payload["request"], choices=tuple(CompanionChoice(**row) for row in rows))


def manual_cache_effects(choices, snapshot):
    requirements = _manual_requirements(choices)
    policy = load_policy(snapshot.get("workspace", {}))
    destinations = [fast_destination(policy, resource_id=row.resource_id, role=row.role,
        family=row.family, media_type=row.media_type) for row in requirements.resources]
    return desired_cache_effects(_plain([asdict(row) for row in requirements.resources]),
        destinations, _policy_digest(snapshot), choices["approved_cleanup"],
        "adapter_qualified" if choices["requirements"].get("companion_request") is not None else "unknown")


def paths_effects(choices, snapshot, *, exposed_resources=()):
    inventory = choices["inventory"]
    return {"action": "share_model_paths", "mode": choices["mode"],
        "before": {"model_root": inventory["model_root"], "values": deepcopy(inventory["values"])},
        "after": {"model_root": choices["model_root"], "values": deepcopy(choices["values"])},
        "policy_digest": _policy_digest(snapshot), "prepared_steps": list(choices["prepared_steps"]),
        "exposed_resources": sorted(deepcopy(list(exposed_resources)), key=lambda row: (row["path"], row["resource_id"])),
        "external_runtime_readiness": "unverified"}


def _same_effects(step, effects):
    if effects != step.expected_effects:
        raise ModelResourceError("The exact import consequences changed. Review the combined plan again.")


def _reference(env, receipt, kind, owner, locator):
    if receipt is None or receipt.owner != kind:
        raise ModelResourceError("Choose the recorded operation for this exact import action.")
    reference = env.check_reference(receipt, locator)
    if reference.get("owner") != owner or reference.get("reference_kind") != "immutable_receipt":
        raise ModelResourceError("The import receipt belongs to a different canonical owner.")
    return reference


def _finish(env, step, context, token, status, receipt=None, message=""):
    if env.observe(context, strict_token=False)[1] != token:
        raise ModelResourceError("The registry changed while verifying this import operation.")
    result = env.outcome(step, status, receipt, message)
    if result.registry_token != token:
        raise ModelResourceError("The registry changed before the import result could be recorded.")
    return result


def _move_record(env, receipt, snapshot):
    path = env.relocation._directory(receipt.operation_id) / "baseline.json"
    reference = _reference(env, receipt, "move", "model_relocation", path)
    if reference["evidence"] != {"kind": "relocate"}:
        raise ModelResourceError("The relocation reference changed.")
    return env.relocation._load_operation(snapshot, receipt.operation_id)


def _verify_moved_file(env, baseline, marker, *, should_cancel=None, progress=None):
    source = _path(baseline["selection"]["source"], (*env.protected_roots, *env.managed_roots))
    destination = _path(baseline["destination"], (*env.protected_roots, *env.managed_roots))
    if source.exists():
        raise ModelResourceError("The retired move source exists again; review it before resuming import.")
    with _pinned_model_file(destination, env.protected_roots):
        inspection = inspect_model_resource(destination, should_cancel=should_cancel, progress=progress)
        if (inspection.stored_identity() != baseline["identity"]
                or list(inspection.file_state) != marker.get("destination_row", {}).get("file_state")):
            raise ModelResourceError("The moved model changed; preserve its exact recovery evidence.")
    return destination


def proven_relocated_path(env, context, resource_id, original_path):
    """Follow only reverified canonical completed moves, never names or claims."""
    snapshot, token = env.observe(context, strict_token=False)
    current, seen = Path(original_path), {Path(original_path)}
    for _ in range(128):
        matches = []
        for result in context.completed:
            if result.status != "completed" or result.receipt is None or result.receipt.owner != "move":
                continue
            selection = result.effects.get("selection", {})
            if selection.get("resource_id") != resource_id or Path(selection.get("source", "")) != current:
                continue
            baseline, marker = _move_record(env, result.receipt, snapshot)
            expected = desired_move_effects(baseline["selection"], baseline["destination"], baseline["settings_digest"])
            if marker["phase"] != "complete" or expected != result.effects:
                raise ModelResourceError("The claimed later move has no matching completed canonical proof.")
            matches.append((baseline, marker))
        if not matches:
            break
        if len(matches) != 1:
            raise ModelResourceError("More than one move claims this source; preserve the ambiguous import evidence.")
        current = _verify_moved_file(env, *matches[0], should_cancel=env.should_cancel, progress=env.progress)
        if current in seen:
            raise ModelResourceError("The recorded relocation chain is cyclic.")
        seen.add(current)
    else:
        raise ModelResourceError("The relocation chain exceeds its supported bound.")
    if current == Path(original_path):
        raise ModelResourceError("No proven completed move accounts for this missing registered location.")
    if env.observe(context, strict_token=False)[1] != token:
        raise ModelResourceError("The registry changed while checking relocation lineage.")
    return current


class MovePort:
    def __init__(self, environment): self.env = environment

    def _record(self, step, receipt, snapshot):
        baseline, marker = _move_record(self.env, receipt, snapshot)
        _same_effects(step, desired_move_effects(baseline["selection"], baseline["destination"], baseline["settings_digest"]))
        if baseline["selection"] != step.choices["selection"] or baseline["destination"] != step.choices["destination"]:
            raise ModelResourceError("The retained move differs from the approved selection.")
        return baseline, marker

    def preview(self, step, context):
        waiting = self.env.waiting(step, context)
        if waiting: return waiting
        value, token = self.env.observe(context)
        effects = move_effects(step.choices, value)
        _same_effects(step, effects)
        if context.prior_receipt:
            self._record(step, context.prior_receipt, value)
            handle = self.env.relocation.preview_recovery(context.prior_receipt.operation_id,
                should_cancel=self.env.should_cancel, progress=self.env.progress)
        else:
            selection = RelocationSelection(**{**step.choices["selection"], "source": Path(step.choices["selection"]["source"])})
            handle = self.env.relocation.preview((selection,), expected_registry=token,
                should_cancel=self.env.should_cancel, progress=self.env.progress)
            row, = handle.rows
            if row.status != "ready" or str(row.destination) != step.choices["destination"]:
                return self.env.preview(context, effects, handle, status="held", message=row.reason or "The move destination changed.")
        return self.env.preview(context, effects, handle)

    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None):
        self.env.observe(context)
        _same_effects(step, preview.effects)
        options = dict(confirmed=True, should_cancel=should_cancel, progress=progress,
            operation_started=self.env.bind(step.kind, on_started))
        if context.prior_receipt:
            result = self.env.relocation.recover(preview.handle, **options)
        else:
            result, = self.env.relocation.apply(preview.handle, [preview.handle.rows[0].row_id], **options)
        status = {"complete": "completed", "recovery_required": "recoverable", "cancelled": "cancelled"}.get(result.status, "held")
        return self.env.outcome(step, status, message=result.message)

    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None):
        value, token = self.env.observe(context, strict_token=False)
        baseline, marker = self._record(step, receipt, value)
        if marker["phase"] == "complete":
            _verify_moved_file(self.env, baseline, marker, should_cancel=should_cancel, progress=progress)
            status = "completed"
        elif marker["phase"] in {"prepared", "abandoned"}:
            status = "held"  # canonical prepared recovery abandons; it never guesses a fresh move
        else:
            self.env.relocation.preview_recovery(receipt.operation_id, should_cancel=should_cancel, progress=progress)
            status = "recoverable"
        return _finish(self.env, step, context, token, status, receipt, "Checked the exact retained relocation; no move was repeated.")


class ManualCachePort:
    def __init__(self, environment): self.env = environment

    def _requirements(self, step, token, *, should_cancel=None, progress=None):
        declared = _manual_requirements(step.choices)
        payload = step.choices["requirements"].get("companion_request")
        if payload is None:
            return declared
        report = self.env.companions.verify(_companion_request(payload), expected_registry=token,
            should_cancel=should_cancel, progress=progress)
        if not report.can_prepare or report.qualification != "adapter_qualified":
            raise ModelResourceError("The explicit companion request did not verify an adapter-qualified complete set. Review its selected slots.")
        requirements = self.env.companions.requirements(report)
        if requirements.resources != declared.resources:
            raise ModelResourceError("Import resources differ from the canonical complete companion set or its slot order.")
        return requirements

    @contextmanager
    def _resolver(self, *, should_cancel=None, progress=None):
        previous = self.env.cache.requirement_resolver
        self.env.cache.requirement_resolver = self.env.companions.resolver(should_cancel=should_cancel, progress=progress)
        try:
            yield
        finally:
            self.env.cache.requirement_resolver = previous

    def _record(self, step, receipt, snapshot, requirements):
        path = self.env.cache._directory(receipt.operation_id) / "baseline.json"
        reference = _reference(self.env, receipt, "manual_cache", "model_cache", path)
        if self.env.cache._operation_reference(snapshot, receipt.operation_id) != reference:
            raise ModelResourceError("The cache operation differs from its bound receipt.")
        operation, baseline = self.env.cache._load_operation(snapshot, receipt.operation_id)
        resources = _plain([asdict(row) for row in requirements.resources])
        expected = _plain(asdict(requirements))
        retained = deepcopy(baseline["requirements"])
        # Session-issued fingerprints are never persisted as reusable authority.
        # Fresh companion verification above proves the same semantic contract.
        if requirements.qualification == "adapter_qualified":
            expected.pop("request_fingerprint")
            retained.pop("request_fingerprint", None)
        cleanup = baseline.get("capacity", {}).get("victims", [])
        if (baseline["kind"] != "prepare" or retained != expected
                or cleanup != step.choices["approved_cleanup"] or operation.get("qualification") != requirements.qualification
                or not operation.get("manual_pin_id") or operation.get("execution_pin_id")):
            raise ModelResourceError("This is not the approved manually pinned preparation set.")
        _same_effects(step, desired_cache_effects(resources, [row["destination"] for row in baseline["rows"]],
            baseline["policy_digest"], cleanup, requirements.qualification))
        return operation, baseline

    def preview(self, step, context):
        waiting = self.env.waiting(step, context)
        if waiting: return waiting
        value, token = self.env.observe(context)
        effects = manual_cache_effects(step.choices, value)
        _same_effects(step, effects)
        requirements = self._requirements(step, token, should_cancel=self.env.should_cancel, progress=self.env.progress)
        if context.prior_receipt:
            self._record(step, context.prior_receipt, value, requirements)
            handle = self.env.cache.preview_recovery(context.prior_receipt.operation_id, should_cancel=self.env.should_cancel)
        else:
            with self._resolver(should_cancel=self.env.should_cancel, progress=self.env.progress):
                handle = self.env.cache.preview_prepare(requirements, expected_registry=token,
                    should_cancel=self.env.should_cancel, progress=self.env.progress)
            if not handle.can_prepare:
                return self.env.preview(context, effects, handle, status="held",
                    message="; ".join(row.reason for row in handle.rows if row.status == "held") or handle.message)
            actual = desired_cache_effects(_plain([asdict(row.resource) for row in handle.rows]),
                [row.destination for row in handle.rows], handle.policy_digest,
                _plain([asdict(row) for row in handle.capacity.victims]) if handle.capacity else [], handle.qualification)
            _same_effects(step, actual)
        return self.env.preview(context, effects, handle)

    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None):
        value, token = self.env.observe(context)
        _same_effects(step, preview.effects)
        options = dict(confirmed=True, should_cancel=should_cancel, progress=progress,
            operation_started=self.env.bind(step.kind, on_started))
        if context.prior_receipt:
            requirements = self._requirements(step, token, should_cancel=should_cancel, progress=progress)
            self._record(step, context.prior_receipt, value, requirements)
        with self._resolver(should_cancel=should_cancel, progress=progress):
            result = (self.env.cache.recover(preview.handle, **options) if context.prior_receipt
                else self.env.cache.apply_prepare(preview.handle, **options))
        return self.env.outcome(step, "completed" if result.status == "complete" else "recoverable", message=result.message)

    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None):
        value, token = self.env.observe(context, strict_token=False)
        requirements = self._requirements(step, token, should_cancel=should_cancel, progress=progress)
        operation, baseline = self._record(step, receipt, value, requirements)
        state = cache_state(value)
        pin = state["leases"].get(operation["manual_pin_id"], {})
        if (pin.get("state") != "active" or pin.get("kind") != "manual"
                or pin.get("operation_id") != receipt.operation_id):
            raise ModelResourceError("The import's manual external-use pin was released; review preparation again.")
        if operation["phase"] != "complete":
            self.env.cache.preview_recovery(receipt.operation_id, should_cancel=should_cancel)
            return _finish(self.env, step, context, token, "recoverable", receipt, "Resume only this recorded preparation.")
        expected = {row["entry_id"] for row in baseline["rows"]}
        if set(operation["entry_ids"]) != expected or not expected <= set(pin.get("entry_ids", ())):
            raise ModelResourceError("The prepared whole set no longer has its original manual protection.")
        for entry_id in sorted(expected):
            _cancel(should_cancel)
            self.env.cache._verify_entry(value, entry_id, should_cancel=should_cancel, progress=progress)
        for row in baseline["rows"]:
            archive = _path(row["resource"]["archive_path"], (*self.env.protected_roots, *self.env.managed_roots))
            with _pinned_model_file(archive, self.env.protected_roots):
                original = inspect_model_resource(archive, should_cancel=should_cancel, progress=progress)
                record = value["resources"].get(row["resource"]["resource_id"], {})
                if (original.stored_identity() != row["identity"] or list(original.file_state) != row["source_state"]
                        or row["source_row"] not in record.get("locations", ())):
                    raise ModelResourceError("A prepared model's retained archive original changed; review the import.")
        return _finish(self.env, step, context, token, "completed", receipt,
            "Every prepared member and its active external-use pin were reverified; execution readiness remains separate.")


def _exposure(value, projection):
    state = cache_state(value)
    return [{"resource_id": state["entries"][key]["resource_id"], "path": state["entries"][key]["path"],
        "contexts": deepcopy(state["entries"][key].get("contexts", []))} for key in projection.entry_ids]


class PathsPort:
    def __init__(self, environment): self.env = environment

    @staticmethod
    def _prepared(step, context):
        prepared = []
        for dependency in step.choices["prepared_steps"]:
            rows = [row for row in context.completed if row.step_id == dependency and row.status == "completed"]
            if len(rows) != 1 or rows[0].receipt is None or rows[0].receipt.owner != "manual_cache":
                raise ModelResourceError("Every shared set needs its exact completed manual preparation dependency.")
            prepared.append({"step_id": dependency, "receipt": asdict(rows[0].receipt)})
        return prepared

    def _projection(self, step, context, value, token, inventory, prepared=None):
        prepared = self._prepared(step, context) if prepared is None else prepared
        if (not isinstance(prepared, list) or len(prepared) != len(step.choices["prepared_steps"])
                or any(not isinstance(row, dict) or set(row) != {"step_id", "receipt"} for row in prepared)
                or [row["step_id"] for row in prepared] != step.choices["prepared_steps"]):
            raise ModelResourceError("The settings handoff lost its exact preparation dependency references.")
        ids = []
        for row in prepared:
            raw = row["receipt"]
            if not isinstance(raw, dict) or set(raw) != {"owner", "operation_id", "locator", "fingerprint", "evidence"}:
                raise ModelResourceError("The settings handoff contains an unsupported preparation reference.")
            receipt = OwnerReceipt(**deepcopy(raw))
            reference = _reference(self.env, receipt, "manual_cache", "model_cache",
                self.env.cache._directory(receipt.operation_id) / "baseline.json")
            if self.env.cache._operation_reference(value, receipt.operation_id) != reference:
                raise ModelResourceError("A prepared set's canonical receipt changed.")
            operation, baseline = self.env.cache._load_operation(value, receipt.operation_id)
            state = cache_state(value)
            pin = state["leases"].get(operation.get("manual_pin_id"), {})
            if (operation.get("phase") != "complete" or baseline.get("kind") != "prepare"
                    or pin.get("kind") != "manual" or pin.get("state") != "active"
                    or pin.get("operation_id") != receipt.operation_id
                    or not set(operation.get("entry_ids", ())) <= set(pin.get("entry_ids", ()))):
                raise ModelResourceError("The settings dependency lost its completed preparation or original manual pin.")
            ids.append(receipt.operation_id)
        projection = project_swarm_hot_folders(value, inventory, tuple(ids), registry_token=token)
        if projection.held:
            raise ModelResourceError(" ".join(projection.held))
        if projection.values != step.choices["values"] or projection.after_model_root != step.choices["model_root"]:
            raise ModelResourceError("The exact prepared role folders changed; renew the combined review.")
        return projection

    def _review(self, step, context, value, token, *, should_cancel=None, progress=None, prepared=None):
        inventory = ModelPathInventory(**deepcopy(step.choices["inventory"]))
        policy = load_policy(value.get("workspace", {}))
        if policy.mode != step.choices["mode"]:
            raise ModelResourceError("The model storage mode changed; review paths again.")
        qualification = None
        exposure = ()
        if policy.mode == "tiered":
            projection = self._projection(step, context, value, token, inventory, prepared)
            exposure = _exposure(value, projection)
            qualification = self.env.hot.qualify(projection, should_cancel=should_cancel, progress=progress)
        elif step.choices["model_root"] != inventory.model_root:
            raise ModelResourceError("Single-location import cannot change the engine's root implicitly.")
        effects = paths_effects(step.choices, value, exposed_resources=exposure)
        _same_effects(step, effects)
        return inventory, qualification, effects

    def preview(self, step, context):
        waiting = self.env.waiting(step, context)
        if waiting: return waiting
        if context.prior_receipt:
            return self.env.preview(context, step.expected_effects, None, status="held",
                message="Reconcile the recorded settings operation; it cannot be resent automatically.")
        value, token = self.env.observe(context)
        inventory, qualification, effects = self._review(step, context, value, token,
            should_cancel=self.env.should_cancel, progress=self.env.progress)
        if qualification:
            with self.env.hot.guard(qualification, should_cancel=self.env.should_cancel, progress=self.env.progress):
                handle = self.env.paths.preview_managed_paths(step.choices["values"], model_root=step.choices["model_root"], expected=inventory)
        else:
            handle = self.env.paths.preview(step.choices["values"], expected=inventory)
        if not handle.changes and not handle.root_changed:
            return self.env.preview(context, effects, None, status="held", message="These engine paths already match; remove this no-op from the plan.")
        return self.env.preview(context, effects, (handle, qualification))

    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None):
        self.env.observe(context)
        _same_effects(step, preview.effects)
        handle, qualification = preview.handle
        _cancel(should_cancel)
        prepared = self._prepared(step, context)
        def bind_dependency_proof(receipt):
            on_started(replace(receipt, evidence={**receipt.evidence, "prepared_receipts": deepcopy(prepared)}))
        started = self.env.bind(step.kind, bind_dependency_proof)
        if qualification:
            with self.env.hot.guard(qualification, should_cancel=should_cancel, progress=progress):
                result = self.env.paths.apply(handle, operation_started=started)
        else:
            result = self.env.paths.apply(handle, operation_started=started)
        return self.env.outcome(step, "completed" if result.status == "applied" else "uncertain", message=result.message)

    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None):
        value, token = self.env.observe(context, strict_token=False)
        path = self.env.paths._directory(create=False) / (receipt.operation_id + ".json")
        reference = _reference(self.env, receipt, "paths", "swarm_model_paths", path)
        if reference["evidence"] != {"kind": "apply", "parent_id": ""}:
            raise ModelResourceError("This is not the import's original settings apply operation.")
        journal = self.env.paths._journal()
        if not journal or journal[0]["operation_id"] != receipt.operation_id:
            raise ModelResourceError("Another settings operation superseded this import receipt; review recovery.")
        baseline = journal[1]
        if (baseline["before"] != step.choices["inventory"]["values"] or baseline["model_root"] != step.choices["inventory"]["model_root"]
                or baseline["after"] != step.choices["values"] or baseline.get("after_model_root", baseline["model_root"]) != step.choices["model_root"]):
            raise ModelResourceError("The settings receipt differs from the exact approved before/after paths.")
        if baseline["binding"] != step.choices["inventory"]["binding"]:
            raise ModelResourceError("The settings receipt belongs to a different enrolled runtime.")
        prepared = receipt.evidence.get("prepared_receipts", [])
        if step.choices["mode"] == "single" and prepared:
            raise ModelResourceError("Single-location settings cannot claim prepared cache dependencies.")
        _, qualification, _ = self._review(step, context, value, token, should_cancel=should_cancel, progress=progress,
            prepared=prepared)
        _cancel(should_cancel)
        if qualification:
            with self.env.hot.guard(qualification, should_cancel=should_cancel, progress=progress):
                result = self.env.paths.reconcile()
        else:
            result = self.env.paths.reconcile()  # readback only; never a ChangeServerSettings retry
        if result is None or result.operation_id != receipt.operation_id:
            raise ModelResourceError("The settings operation changed during reconciliation.")
        status = "completed" if result.status == "applied" else "uncertain" if result.status == "uncertain" else "held"
        return _finish(self.env, step, context, token, status, receipt,
            "Exact engine readback checked; an unconfirmed settings write is never resent.")


def storage_ports(environment):
    return {"move": MovePort(environment), "manual_cache": ManualCachePort(environment), "paths": PathsPort(environment)}
