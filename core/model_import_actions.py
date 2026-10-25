"""Production model-import ports over the existing registry and operation owners.

Plans retain choices and exact consequences. Only fresh owner previews execute;
opening a saved plan does not inspect a model, contact Swarm, or resume a write.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
from uuid import uuid4

from .model_import_plan import ActionOutcome, ActionPreview, OwnerReceipt
from .model_resource_registry import ModelResourceError, _bounded_read, _bytes, _cancel, inspect_model_resource
from .model_scan_review import _decode
from .model_workspace import ModelWorkspace, IndexPreview, _path


def registration_effects(choices):
    draft = _decode(choices["review"])
    selected = set(draft.selected_paths)
    assignments = {row.path: row for row in draft.declarations}
    if not selected or any(path not in assignments for path in selected):
        raise ModelResourceError("Choose the type and family for every selected import file.")
    return {"action": "register", "files": [
        {"path": str(row.path), "resource_id": row.resource_id, "size": row.size,
         "role": assignments[row.path].role, "family": assignments[row.path].family.strip()}
        for row in draft.candidates if row.path in selected], "source_files": "unchanged"}


def acquisition_effects(choices):
    return {"action": "download", "file": deepcopy(choices["plan"]),
            "existing_files": "never replaced", "registration": "separate reviewed step"}


def companion_effects(choices):
    return {"action": "verify_companions", "request": deepcopy(choices["request"]),
            "choices": deepcopy(choices["choices"]), "runtime_readiness": "not granted"}


class ImportActionEnvironment:
    def __init__(self, registry, paths, *, protected_roots=(), managed_roots=(),
                 source_binding_reader=None, acquisition_stream=None, boundary_provider=None):
        from .model_relocation import ModelRelocationService
        from .model_cache import ModelCacheService
        from .model_companion_readiness import CompanionReadinessService
        from .model_hot_paths import ModelHotPathService
        from .model_storage_policy import retained_fast_roots
        self.registry, self.paths = registry, paths
        self.protected_roots, self.managed_roots = tuple(protected_roots), tuple(managed_roots)
        source_roots = (*self.protected_roots, *self.managed_roots, *retained_fast_roots(registry.snapshot()[0]))
        self.workspace = ModelWorkspace(registry, protected_roots=source_roots)
        self.relocation = ModelRelocationService(registry, protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        self.cache = ModelCacheService(registry, protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        self.companions = CompanionReadinessService(registry, protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        self.hot = ModelHotPathService(registry, protected_roots=self.protected_roots, managed_roots=self.managed_roots)
        self.source_binding_reader = source_binding_reader or (lambda: paths._binding(paths._record()))
        self.acquisition_stream = acquisition_stream
        self.boundary_provider = boundary_provider
        self.should_cancel = self.progress = None

    @contextmanager
    def operation(self, *, should_cancel=None, progress=None):
        previous = self.should_cancel, self.progress
        self.should_cancel, self.progress = should_cancel, progress
        try:
            yield self
        finally:
            self.should_cancel, self.progress = previous

    def observe(self, context, *, strict_token=True):
        _cancel(self.should_cancel)
        if self.boundary_provider is not None:
            protected, managed = self.boundary_provider()
            if tuple(protected) != self.protected_roots or tuple(managed) != self.managed_roots:
                raise ModelResourceError("Protected or managed model locations changed. Reopen this import before any file access.")
        if self.source_binding_reader() != context.source_binding:
            raise ModelResourceError("The enrolled SwarmUI source changed. Review the combined import again.")
        value, token = self.registry.snapshot()
        if strict_token and token != context.registry_token:
            raise ModelResourceError("The model registry changed. Review the combined import again.")
        return value, token

    def waiting(self, step, context):
        _, token = self.observe(context)
        if not set(step.depends_on) <= {row.step_id for row in context.completed if row.status == "completed"}:
            return ActionPreview("waiting", deepcopy(step.expected_effects), token, deepcopy(context.source_binding),
                message="Waiting for the displayed earlier steps; exact consequences will be checked again before applying.")
        return None

    def preview(self, context, effects, handle, *, message="", status="ready"):
        _, token = self.observe(context)
        return ActionPreview(status, effects, token, deepcopy(context.source_binding), handle, message)

    @staticmethod
    def bind(kind, on_started):
        def started(reference):
            on_started(OwnerReceipt(kind, reference["operation_id"], reference["locator"], reference["fingerprint"],
                {"canonical_reference": deepcopy(reference)}))
        return started

    def check_reference(self, receipt, expected_locator):
        reference = deepcopy(receipt.evidence.get("canonical_reference"))
        if (not isinstance(reference, dict)
                or any(reference.get(key) != getattr(receipt, key) for key in ("operation_id", "locator", "fingerprint"))
                or Path(receipt.locator) != Path(expected_locator)):
            raise ModelResourceError("The import receipt differs from its exact canonical operation.")
        path = _path(expected_locator, self.protected_roots)
        if reference.get("reference_kind") == "registry_transition":
            if path != self.registry.path or hashlib.sha256(_bytes(reference["evidence"])).hexdigest() != receipt.fingerprint:
                raise ModelResourceError("The recorded registry transition changed.")
        elif reference.get("reference_kind") == "immutable_receipt":
            info = path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000)):
                raise ModelResourceError("The operation receipt must remain an independent local regular file.")
            raw = _bounded_read(path, 8 * 1024**2)
            if raw is None or hashlib.sha256(raw).hexdigest() != receipt.fingerprint:
                raise ModelResourceError("The exact operation receipt is missing or changed.")
        else:
            raise ModelResourceError("Unsupported import recovery reference.")
        return reference

    def outcome(self, step, status, receipt=None, message=""):
        return ActionOutcome(status, deepcopy(step.expected_effects) if status in {"completed", "recoverable"} else {},
            self.registry.snapshot()[1], receipt, message)

    def ports(self):
        from .model_import_storage_actions import storage_ports
        return {"registration": RegistrationPort(self), "acquisition": AcquisitionPort(self),
                "companions": CompanionPort(self), **storage_ports(self)}


class RegistrationPort:
    def __init__(self, environment): self.env = environment

    def _inspect(self, step, context):
        draft = _decode(step.choices["review"])
        effects = registration_effects(step.choices)
        selected = {Path(row["path"]): row for row in effects["files"]}
        # Validate all boundaries before the first file read.
        _path(draft.root, self.env.workspace.protected_roots)
        for path in selected: _path(path, self.env.workspace.protected_roots)
        inspections = []
        for index, (path, row) in enumerate(selected.items()):
            _cancel(self.env.should_cancel)
            if self.env.progress: self.env.progress(index, len(selected), f"Checking import file: {path.name}")
            inspection = inspect_model_resource(path, should_cancel=self.env.should_cancel)
            if inspection.resource_id != row["resource_id"] or inspection.size != row["size"]:
                raise ModelResourceError("A selected model differs from the reviewed full-file identity.")
            inspections.append(inspection)
        self.env.observe(context)
        declarations = tuple(row for row in draft.declarations if row.path in selected)
        return effects, IndexPreview(draft.root, tuple(inspections), (), context.registry_token), declarations

    def preview(self, step, context):
        waiting = self.env.waiting(step, context)
        if waiting: return waiting
        if context.prior_receipt is not None:
            return self.env.preview(context, step.expected_effects, None, status="held",
                message="A recorded registration needs reconciliation; it cannot be repeated blindly.")
        effects, preview, declarations = self._inspect(step, context)
        return self.env.preview(context, effects, (preview, declarations))

    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None):
        self.env.observe(context)
        result = self.env.workspace.apply_index_selection(*preview.handle, should_cancel=should_cancel,
            operation_started=self.env.bind(step.kind, on_started))
        return ActionOutcome("completed", registration_effects(step.choices), result.registry_token,
            message="Exact selected files registered; source files remain unchanged.")

    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None):
        reference = self.env.check_reference(receipt, self.env.registry.path)
        evidence = reference["evidence"]
        if evidence.get("kind") != "index_selection":
            raise ModelResourceError("This receipt does not identify a model registration.")
        value, token = self.env.observe(context, strict_token=False)
        if token == evidence.get("before_registry") and token != evidence.get("expected_after_registry"):
            return self.env.outcome(step, "held", receipt, "Registration did not publish. Review a fresh combined plan before retrying.")
        if token != evidence.get("expected_after_registry"):
            # Later completed canonical operations may legitimately change the
            # registry. Their receipts still need independent reconciliation.
            later_proofs = [row for row in context.completed if row.status == "completed" and row.receipt is not None]
            if not later_proofs or token != context.registry_token:
                return self.env.outcome(step, "held", receipt, "Registration outcome cannot be established from this registry revision.")
        effects = registration_effects(step.choices)
        for row in effects["files"]:
            _cancel(should_cancel)
            record = value["resources"].get(row["resource_id"], {})
            path = Path(row["path"])
            if not any(Path(location["path"]) == path for location in record.get("locations", ())):
                from .model_import_storage_actions import proven_relocated_path
                path = proven_relocated_path(self.env, context, row["resource_id"], path)
            _path(path, self.env.workspace.protected_roots)
            inspection = inspect_model_resource(path, should_cancel=should_cancel, progress=progress)
            if (inspection.resource_id != row["resource_id"] or inspection.size != row["size"]
                    or record.get("identity") != inspection.stored_identity()
                    or not any(Path(location["path"]) == path and tuple(location.get("file_state", ())) == inspection.file_state for location in record.get("locations", ()))
                    or not any(item.get("role") == row["role"] and item.get("family") == row["family"] for item in record.get("declarations", ()))):
                raise ModelResourceError("A registered import result changed; retain its receipt for review.")
        if self.env.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed while checking the import result.")
        return self.env.outcome(step, "completed", receipt, "Registration result reverified against current files and canonical owners.")


class AcquisitionPort:
    def __init__(self, environment): self.env = environment

    def _recovery(self, step, receipt, *, should_cancel=None):
        from .model_workspace import _acquisition_operation_reference, preview_acquisition_recovery
        destination = Path(step.choices["plan"]["destination"])
        attempt = destination.parent / (".collectigen-acquisition-" + receipt.operation_id)
        _path(attempt, self.env.workspace.protected_roots)
        self.env.check_reference(receipt, attempt / "operation.json")
        if _acquisition_operation_reference(attempt, self.env.workspace.protected_roots) != receipt.evidence["canonical_reference"]:
            raise ModelResourceError("The acquisition owner differs from the recorded import attempt.")
        baseline = json.loads(_bounded_read(attempt / "operation.json", 32 * 1024))
        if baseline["plan"] != step.choices["plan"]:
            raise ModelResourceError("The retained acquisition has different provider, file, terms or destination choices.")
        return preview_acquisition_recovery(attempt, protected_roots=self.env.workspace.protected_roots, should_cancel=should_cancel)

    def preview(self, step, context):
        from .model_workspace import preview_acquisition
        waiting = self.env.waiting(step, context)
        if waiting: return waiting
        if context.prior_receipt:
            handle = self._recovery(step, context.prior_receipt, should_cancel=self.env.should_cancel)
        else:
            values = {key: value for key, value in step.choices["plan"].items() if key != "schema_version"}
            handle = preview_acquisition(**values, protected_roots=self.env.workspace.protected_roots)
        return self.env.preview(context, acquisition_effects(step.choices), handle)

    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None):
        from .model_workspace import acquire_model, recover_acquisition
        self.env.observe(context)
        options = dict(confirmed=True, protected_roots=self.env.workspace.protected_roots, should_cancel=should_cancel,
                       operation_started=self.env.bind(step.kind, on_started))
        if context.prior_receipt:
            result = recover_acquisition(preview.handle, **options)
        else:
            result = acquire_model(preview.handle, progress=progress, stream=self.env.acquisition_stream, **options)
        if result.resource_id != "sha256:" + step.choices["plan"]["sha256"]:
            raise ModelResourceError("The acquired resource differs from the approved exact file.")
        return self.env.outcome(step, "completed", message="Exact download verified and published; its retained attempt remains available.")

    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None):
        _, token = self.env.observe(context, strict_token=False)
        recovery = self._recovery(step, receipt, should_cancel=should_cancel)
        if not recovery.destination_present and any(row.status == "completed" and row.receipt is not None
                and row.receipt.owner == "move" and row.effects.get("selection", {}).get("source") == str(recovery.destination)
                for row in context.completed):
            from .model_import_storage_actions import proven_relocated_path
            destination = proven_relocated_path(self.env, context, "sha256:" + recovery.sha256, recovery.destination)
            inspection = inspect_model_resource(destination, should_cancel=should_cancel, progress=progress)
            if inspection.resource_id != "sha256:" + recovery.sha256 or inspection.size != recovery.size:
                raise ModelResourceError("The moved download differs from its exact retained acquisition identity.")
            if self.env.registry.snapshot()[1] != token:
                raise ModelResourceError("The registry changed while checking the moved download.")
            return self.env.outcome(step, "completed", receipt, "The exact download is retained at its proven organized location; its original path was not republished.")
        if self.env.registry.snapshot()[1] != token:
            raise ModelResourceError("The registry changed while checking the retained acquisition.")
        return self.env.outcome(step, "completed" if recovery.destination_present else "recoverable", receipt,
            "Verified the exact retained download attempt; no download was repeated.")


class CompanionPort:
    def __init__(self, environment): self.env = environment

    def _verify(self, step, context, *, should_cancel=None, progress=None):
        from .model_companion_readiness import CompanionChoice, CompanionReadinessRequest
        self.env.observe(context)
        request = CompanionReadinessRequest(**step.choices["request"],
            choices=tuple(CompanionChoice(**row) for row in step.choices["choices"]))
        return self.env.companions.verify(request, expected_registry=context.registry_token,
            should_cancel=should_cancel, progress=progress)

    def preview(self, step, context):
        waiting = self.env.waiting(step, context)
        if waiting: return waiting
        report = self._verify(step, context, should_cancel=self.env.should_cancel, progress=self.env.progress)
        return self.env.preview(context, companion_effects(step.choices), report,
            status="held" if report.held else "ready", message="; ".join(report.held) or "Exact choices verified; unknown compatibility stays unknown.")

    def execute(self, step, preview, context, *, on_started, should_cancel=None, progress=None):
        report = self._verify(step, context, should_cancel=should_cancel, progress=progress)
        if report.held: return self.env.outcome(step, "not_started", message="; ".join(report.held))
        evidence = {"kind": "companion_verification", "before_registry": context.registry_token,
            "expected_after_registry": context.registry_token, "catalog_token": report.catalog_token,
            "selection_fingerprint": hashlib.sha256(_bytes(companion_effects(step.choices))).hexdigest()}
        reference = {"schema_version": 1, "reference_kind": "registry_transition", "owner": "model_registration",
            "operation_id": uuid4().hex, "locator": str(self.env.registry.path),
            "fingerprint": hashlib.sha256(_bytes(evidence)).hexdigest(), "evidence": evidence}
        self.env.bind(step.kind, on_started)(reference)
        self.env.observe(context)
        return self.env.outcome(step, "completed", message="Selected companion files verified; no execution readiness was granted.")

    def reconcile(self, step, receipt, context, *, should_cancel=None, progress=None):
        reference = self.env.check_reference(receipt, self.env.registry.path)
        evidence = reference["evidence"]
        _, token = self.env.observe(context, strict_token=False)
        context = replace(context, registry_token=token)
        report = self._verify(step, context, should_cancel=should_cancel, progress=progress)
        if (evidence.get("kind") != "companion_verification" or evidence.get("catalog_token") != report.catalog_token
                or evidence.get("selection_fingerprint") != hashlib.sha256(_bytes(companion_effects(step.choices))).hexdigest() or report.held):
            return self.env.outcome(step, "held", receipt, "Companion requirements or selected files changed. Review the combined choices again.")
        return self.env.outcome(step, "completed", receipt, "Companion choices reverified against the current catalog and files.")
