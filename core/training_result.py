"""Verified SDXL training results and atomic derived-resource registration.

The trainer process writes observations only.  This controller-side boundary
anchors a stopped successful process, independently checks its final output and
checkpoint, and is the only managed-training path that records verified
lineage.  It never loads tensor values or treats worker readiness claims as
authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re

from . import model_resource_registry as resources
from . import training_checkpoint
from . import training_continuation as continuation
from . import training_continuation_execution as continuation_execution
from .model_resource_registry import ModelResourceRegistry, ResourceInspection
from .training_package import MANIFEST_NAME, TrainingPackageService
from .training_process import (
    ProcessInput,
    TrainingProcessPlan,
    TrainingProcessResult,
    TrainingProcessService,
    TrainingRunJournal,
    _bytes,
    _read,
    _sha,
    _write_new,
)
from .training_verification import verified_receipt_hashes
from .training_progress import read_progress


ANCHOR_NAME = "training-result-anchor.json"
WORKER_RESULT_NAME = "training-result.json"
MAX_WORKER_RESULT_BYTES = 64 * 1024
MAX_ANCHOR_BYTES = 16 * 1024
MAX_TENSOR_NAME_BYTES = 1024
MAX_METADATA_BYTES = 128 * 1024
MAX_LINEAGE_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 512 * 1024**2
SHA = re.compile(r"[0-9a-f]{64}\Z")

RESULT_BINDING_FIELDS = {
    "output_relative_path",
    "role",
    "family",
    "parent_ids",
    "package_path",
    "package_fingerprint",
    "manifest_sha256",
    "configuration_sha256",
    "settings",
    "settings_sha256",
    "trainer_id",
    "trainer_version",
    "continuation_contract",
    "expected_final_step",
    "expected_final_epoch",
    "expected_checkpoint_sequence",
    "tensor_specs",
    "required_metadata",
}
WORKER_RESULT_FIELDS = {
    "schema_version",
    "kind",
    "status",
    "process_fingerprint",
    "execution_contract_sha256",
    "output_relative_path",
    "output_sha256",
    "output_bytes",
    "final_step",
    "final_epoch",
    "checkpoint_sequence",
    "checkpoint_tip",
    "checkpoint_name",
    "checkpoint_receipt_sha256",
    "continuation_receipt_sha256",
    "progress_tip",
    "model_state_sha256",
}
ANCHOR_FIELDS = {
    "schema_version",
    "kind",
    "process_fingerprint",
    "execution_contract_sha256",
    "process_journal_tip",
    "input_verification_sha256",
    "worker_result_sha256",
}


class TrainingResultError(ValueError):
    """Stable result-verification failure without selected path disclosure."""


def validate_execution_contract(contract, plan):
    """Late import seam for the production launch authority.

    Tests may replace this single seam with a strict generated contract.  The
    production verifier never accepts a standalone boolean or worker claim.
    """
    from .training_execution_plan import validate_execution_contract as validate

    return validate(contract, plan)


def _hash(value) -> bool:
    return type(value) is str and SHA.fullmatch(value) is not None


def _canonical(value) -> bytes:
    try:
        return _bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise TrainingResultError("training_result_contract_invalid") from exc


def _cancel(callback):
    if callback and callback():
        raise InterruptedError("Training result verification was cancelled.")


def _safe(operation):
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (TrainingResultError, InterruptedError):
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
            raise TrainingResultError("training_result_unavailable_or_changed") from None

    return wrapped


def _validated_contract(contract, plan: TrainingProcessPlan) -> tuple[dict, dict, bytes]:
    if type(plan) is not TrainingProcessPlan:
        raise TrainingResultError("training_result_process_invalid")
    plan.validate()
    raw = _canonical(contract)
    if len(raw) > 4 * 1024**2 or hashlib.sha256(raw).hexdigest() != plan.preview_fingerprint:
        raise TrainingResultError("training_result_contract_unbound")
    checked = validate_execution_contract(deepcopy(contract), plan)
    if _canonical(checked) != raw or type(checked) is not dict or set(checked).issuperset({"result_binding"}) is False:
        raise TrainingResultError("training_result_contract_invalid")
    binding = checked["result_binding"]
    if type(binding) is not dict or set(binding) != RESULT_BINDING_FIELDS:
        raise TrainingResultError("training_result_binding_invalid")
    _validate_binding(binding, plan)
    return deepcopy(checked), deepcopy(binding), raw


def _validate_binding(value: dict, plan: TrainingProcessPlan):
    strings = ("role", "family", "package_path", "trainer_id", "trainer_version")
    if any(type(value[key]) is not str or not value[key] or len(value[key]) > 2000 for key in strings):
        raise TrainingResultError("training_result_binding_invalid")
    if value["role"] != "lora" or value["family"].casefold() != "sdxl":
        raise TrainingResultError("training_result_binding_unsupported")
    if not Path(value["package_path"]).is_absolute():
        raise TrainingResultError("training_result_binding_invalid")
    for key in ("package_fingerprint", "manifest_sha256", "configuration_sha256", "settings_sha256"):
        if not _hash(value[key]):
            raise TrainingResultError("training_result_binding_invalid")
    if hashlib.sha256(_canonical(value["settings"])).hexdigest() != value["settings_sha256"]:
        raise TrainingResultError("training_result_settings_changed")
    parents = value["parent_ids"]
    if (
        type(parents) is not list
        or not 1 <= len(parents) <= 64
        or len(set(parents)) != len(parents)
        or any(type(item) is not str or not item.startswith("sha256:") or not _hash(item[7:]) for item in parents)
    ):
        raise TrainingResultError("training_result_parents_invalid")
    for key in ("expected_final_step", "expected_final_epoch"):
        if type(value[key]) is not int or not 1 <= value[key] <= 1_000_000_000:
            raise TrainingResultError("training_result_position_invalid")
    sequence = value["expected_checkpoint_sequence"]
    if type(sequence) is not int or not 0 <= sequence < continuation_execution.MAX_SAVES:
        raise TrainingResultError("training_result_position_invalid")
    relative = value["output_relative_path"]
    if type(relative) is not str or len(relative) > 512:
        raise TrainingResultError("training_result_output_path_invalid")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 2
        or relative_path.parts[0] != "work"
        or relative_path.parts[1] in {"", ".", ".."}
        or Path(relative_path.parts[1]).suffix.casefold() != ".safetensors"
        or str(relative_path).replace("\\", "/") != relative.replace("\\", "/")
    ):
        raise TrainingResultError("training_result_output_path_invalid")
    output = (plan.destination / relative_path).absolute()
    if output.parent != (plan.destination / "work").absolute():
        raise TrainingResultError("training_result_output_path_invalid")
    tensor_specs = value["tensor_specs"]
    if type(tensor_specs) is not dict or not 1 <= len(tensor_specs) <= resources.MAX_TENSORS:
        raise TrainingResultError("training_result_layout_invalid")
    for name, spec in tensor_specs.items():
        if (
            type(name) is not str
            or name == "__metadata__"
            or not name
            or len(name.encode("utf-8")) > MAX_TENSOR_NAME_BYTES
            or type(spec) is not dict
            or set(spec) != {"shape", "dtype"}
            or spec["dtype"] not in {"F16", "BF16"}
            or type(spec["shape"]) is not list
            or len(spec["shape"]) > 32
            or any(type(number) is not int or not 0 <= number <= 2**63 - 1 for number in spec["shape"])
        ):
            raise TrainingResultError("training_result_layout_invalid")
    metadata = value["required_metadata"]
    if (
        type(metadata) is not dict
        or len(metadata) > 256
        or any(type(key) is not str or not key or type(item) is not str for key, item in metadata.items())
        or len(_canonical(metadata)) > MAX_METADATA_BYTES
    ):
        raise TrainingResultError("training_result_metadata_invalid")
    continuation_execution.validate_contract(value["continuation_contract"])
    if "collectigen_execution_contract_sha256" in metadata:
        raise TrainingResultError("training_result_metadata_invalid")


def _process_evidence(plan: TrainingProcessPlan, outcome: TrainingProcessResult) -> tuple[list[dict], str, list[str]]:
    if (
        type(outcome) is not TrainingProcessResult
        or outcome.status != "exited_unverified"
        or outcome.exit_code != 0
        or outcome.tree_stopped is not True
        or outcome.destination.absolute() != plan.destination.absolute()
        or not _hash(outcome.journal_tip)
    ):
        raise TrainingResultError("training_result_process_unsuccessful")
    rows, tip = TrainingRunJournal(plan.destination).read()
    if not rows or tip != outcome.journal_tip or rows[-1]["state"] != "exited_unverified":
        raise TrainingResultError("training_result_process_changed")
    first = rows[0]
    expected_plan = TrainingProcessService._receipt(plan)
    if (
        first["state"] != "reserved"
        or set(first["details"]) != {"plan", "fingerprint"}
        or _canonical(first["details"]["plan"]) != _canonical(expected_plan)
        or first["details"]["fingerprint"] != _sha(_canonical(expected_plan))
    ):
        raise TrainingResultError("training_result_process_changed")
    details = rows[-1]["details"]
    required = {"exit_code", "tree_stopped", "message", "log_sha256", "outputs_verified", "input_verification_sha256"}
    if (
        type(details) is not dict
        or not required <= set(details)
        or not set(details) <= required | {"native_observation_sha256", "preflight_receipt_sha256"}
        or details["exit_code"] != 0
        or details["tree_stopped"] is not True
        or details["message"] != outcome.message
        or details["outputs_verified"] is not False
        or not _hash(details["log_sha256"])
    ):
        raise TrainingResultError("training_result_process_changed")
    for key in set(details) - required:
        if not _hash(details[key]):
            raise TrainingResultError("training_result_process_changed")
    if hashlib.sha256(_read(plan.destination / "process.log", limit=plan.log_limit)).hexdigest() != details["log_sha256"]:
        raise TrainingResultError("training_result_log_changed")
    receipts = verified_receipt_hashes(plan)
    if type(receipts) is not list or len(receipts) != 3 or details["input_verification_sha256"] != receipts:
        raise TrainingResultError("training_result_receipts_changed")
    for selected in plan.inputs:
        if ProcessInput.capture(selected.path) != selected:
            raise TrainingResultError("training_result_process_input_changed")
    return rows, tip, receipts


def _read_canonical(path: Path, limit: int, fields: set[str]) -> tuple[dict, bytes]:
    raw = _read(path, limit=limit)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise TrainingResultError("training_result_receipt_invalid") from None
    if type(value) is not dict or set(value) != fields or _canonical(value) != raw:
        raise TrainingResultError("training_result_receipt_invalid")
    return value, raw


def _worker_result(plan: TrainingProcessPlan, binding: dict) -> tuple[dict, bytes]:
    value, raw = _read_canonical(plan.destination / "work" / WORKER_RESULT_NAME, MAX_WORKER_RESULT_BYTES, WORKER_RESULT_FIELDS)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != "sdxl_training_result"
        or value["status"] != "completed"
        or value["process_fingerprint"] != plan.fingerprint
        or value["execution_contract_sha256"] != plan.preview_fingerprint
        or value["output_relative_path"] != binding["output_relative_path"]
        or type(value["output_bytes"]) is not int
        or not 1 <= value["output_bytes"] <= MAX_OUTPUT_BYTES
        or value["final_step"] != binding["expected_final_step"]
        or value["final_epoch"] != binding["expected_final_epoch"]
        or value["checkpoint_sequence"] != binding["expected_checkpoint_sequence"]
        or any(not _hash(value[key]) for key in (
            "output_sha256",
            "checkpoint_tip",
            "checkpoint_receipt_sha256",
            "continuation_receipt_sha256",
            "progress_tip",
            "model_state_sha256",
        ))
        or type(value["checkpoint_name"]) is not str
    ):
        raise TrainingResultError("training_result_worker_receipt_invalid")
    progress, progress_sha256 = read_progress(
        plan.destination / "work", plan.fingerprint, plan.preview_fingerprint)
    if (progress_sha256 != value["progress_tip"] or progress["state"] != "completed"
            or progress["optimizer_step"] != value["final_step"]
            or progress["max_train_steps"] != binding["expected_final_step"]
            or progress["epoch"] != value["final_epoch"]
            or progress["checkpoint_sequence"] != value["checkpoint_sequence"]
            or progress["checkpoint_tip"] != value["checkpoint_tip"]):
        raise TrainingResultError("training_result_progress_changed")
    return value, raw


def _anchor(plan: TrainingProcessPlan, expected_sha256: str) -> tuple[dict, bytes]:
    if not _hash(expected_sha256):
        raise TrainingResultError("training_result_anchor_unbound")
    value, raw = _read_canonical(plan.destination / ANCHOR_NAME, MAX_ANCHOR_BYTES, ANCHOR_FIELDS)
    if (
        hashlib.sha256(raw).hexdigest() != expected_sha256
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != "sdxl_training_result_anchor"
        or value["process_fingerprint"] != plan.fingerprint
        or value["execution_contract_sha256"] != plan.preview_fingerprint
        or not _hash(value["process_journal_tip"])
        or type(value["input_verification_sha256"]) is not list
        or len(value["input_verification_sha256"]) != 3
        or any(not _hash(item) for item in value["input_verification_sha256"])
        or not _hash(value["worker_result_sha256"])
    ):
        raise TrainingResultError("training_result_anchor_changed")
    return value, raw


def _package(binding: dict, should_cancel=None) -> tuple[dict, str]:
    _cancel(should_cancel)
    verification = TrainingPackageService().verify(binding["package_path"], should_cancel=should_cancel)
    if not verification.valid or verification.package_fingerprint != binding["package_fingerprint"]:
        raise TrainingResultError("training_result_package_changed")
    manifest_sha256 = ProcessInput.capture(verification.destination / MANIFEST_NAME, should_cancel).sha256
    configuration = verification.manifest.get("training_configuration")
    if (
        manifest_sha256 != binding["manifest_sha256"]
        or hashlib.sha256(_canonical(configuration)).hexdigest() != binding["configuration_sha256"]
    ):
        raise TrainingResultError("training_result_package_changed")
    target = configuration.get("target") if type(configuration) is dict else None
    base = target.get("base_model_id") if type(target) is dict else None
    if base not in binding["parent_ids"]:
        raise TrainingResultError("training_result_package_base_changed")
    return deepcopy(verification.manifest), manifest_sha256


def _checkpoint(plan: TrainingProcessPlan, binding: dict, worker: dict, should_cancel=None) -> dict:
    checkpoint_binding = training_checkpoint.binding_for(plan)
    journal = continuation_execution.CheckpointJournal(
        plan.destination,
        checkpoint_binding,
        binding["continuation_contract"],
    )
    rows = journal.read(worker["checkpoint_sequence"], worker["checkpoint_tip"])
    row = rows[-1]
    if (
        len(rows) != worker["checkpoint_sequence"] + 1
        or row["name"] != worker["checkpoint_name"]
        or row["step"] != worker["final_step"]
        or row["epoch"] != worker["final_epoch"]
        or row["proof"] != {
            "checkpoint_receipt_sha256": worker["checkpoint_receipt_sha256"],
            "continuation_receipt_sha256": worker["continuation_receipt_sha256"],
        }
        or row["state"]["model"] != worker["model_state_sha256"]
    ):
        raise TrainingResultError("training_result_checkpoint_changed")
    checked = continuation.inspect_checkpoint(
        plan.destination / "work",
        row["name"],
        checkpoint_binding,
        binding["continuation_contract"]["prerequisite"],
        **row["proof"],
        should_cancel=should_cancel,
    )
    if checked["step"] != row["step"] or checked["epoch"] != row["epoch"]:
        raise TrainingResultError("training_result_checkpoint_changed")
    return deepcopy(row)


def _output(plan: TrainingProcessPlan, binding: dict, worker: dict, should_cancel=None) -> tuple[ResourceInspection, dict]:
    expected = binding["tensor_specs"]
    observed: dict = {}

    def header(raw):
        value = resources._json(raw)
        observed["metadata"] = deepcopy(value.pop("__metadata__", {}))
        observed["tensors"] = {
            name: {"shape": deepcopy(spec["shape"]), "dtype": spec["dtype"]}
            for name, spec in value.items()
        }

    output_path = (plan.destination / Path(binding["output_relative_path"])).absolute()
    inspection = resources.inspect_model_resource(
        output_path,
        should_cancel=should_cancel,
        header_observer=header,
    )
    if (
        inspection.resource_id in binding["parent_ids"]
        or inspection.resource_id != "sha256:" + worker["output_sha256"]
        or inspection.size != worker["output_bytes"]
        or _canonical(observed.get("tensors")) != _canonical(expected)
    ):
        raise TrainingResultError("training_result_output_changed")
    metadata = observed.get("metadata")
    required_metadata = {
        **binding["required_metadata"],
        "collectigen_execution_contract_sha256": plan.preview_fingerprint,
    }
    if type(metadata) is not dict or any(metadata.get(key) != item for key, item in required_metadata.items()):
        raise TrainingResultError("training_result_metadata_changed")
    return inspection, observed


@dataclass(frozen=True)
class VerifiedTrainingResult:
    """Immutable proof inputs; registration always re-verifies their bytes."""

    plan: TrainingProcessPlan = field(repr=False)
    outcome: TrainingProcessResult = field(repr=False)
    contract_json: bytes = field(repr=False)
    anchor_sha256: str
    receipt_json: bytes

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(self.receipt_json).hexdigest()

    @property
    def receipt(self) -> dict:
        return deepcopy(resources._json(self.receipt_json))


class SdxlTrainingResultService:
    """Anchor, verify, and register one supported managed SDXL LoRA output."""

    @_safe
    def anchor_stopped_result(self, plan, outcome, contract, *, should_cancel=None) -> str:
        _, binding, _ = _validated_contract(contract, plan)
        _, tip, receipts = _process_evidence(plan, outcome)
        worker, worker_raw = _worker_result(plan, binding)
        _cancel(should_cancel)
        value = {
            "schema_version": 1,
            "kind": "sdxl_training_result_anchor",
            "process_fingerprint": plan.fingerprint,
            "execution_contract_sha256": plan.preview_fingerprint,
            "process_journal_tip": tip,
            "input_verification_sha256": receipts,
            "worker_result_sha256": hashlib.sha256(worker_raw).hexdigest(),
        }
        raw = _canonical(value)
        path = plan.destination / ANCHOR_NAME
        if os.path.lexists(path):
            if _read(path, limit=MAX_ANCHOR_BYTES) != raw:
                raise TrainingResultError("training_result_anchor_changed")
        else:
            _write_new(path, raw)
        if _read(path, limit=MAX_ANCHOR_BYTES) != raw:
            raise TrainingResultError("training_result_anchor_changed")
        return hashlib.sha256(raw).hexdigest()

    @_safe
    def verify(self, plan, outcome, contract, expected_anchor_sha256, *, should_cancel=None) -> VerifiedTrainingResult:
        _, binding, contract_raw = _validated_contract(contract, plan)
        _, tip, receipts = _process_evidence(plan, outcome)
        anchor, _ = _anchor(plan, expected_anchor_sha256)
        worker, worker_raw = _worker_result(plan, binding)
        if (
            anchor["process_journal_tip"] != tip
            or anchor["input_verification_sha256"] != receipts
            or anchor["worker_result_sha256"] != hashlib.sha256(worker_raw).hexdigest()
        ):
            raise TrainingResultError("training_result_anchor_changed")
        manifest, manifest_sha256 = _package(binding, should_cancel)
        checkpoint = _checkpoint(plan, binding, worker, should_cancel)
        inspection, observed = _output(plan, binding, worker, should_cancel)
        _cancel(should_cancel)
        receipt = {
            "schema_version": 1,
            "kind": "verified_sdxl_training_result",
            "process_fingerprint": plan.fingerprint,
            "execution_contract_sha256": plan.preview_fingerprint,
            "process_journal_tip": tip,
            "input_verification_sha256": receipts,
            "worker_result_sha256": anchor["worker_result_sha256"],
            "result_anchor_sha256": expected_anchor_sha256,
            "package_fingerprint": binding["package_fingerprint"],
            "manifest_sha256": manifest_sha256,
            "configuration_sha256": binding["configuration_sha256"],
            "settings_sha256": binding["settings_sha256"],
            "parent_ids": deepcopy(binding["parent_ids"]),
            "trainer_id": binding["trainer_id"],
            "trainer_version": binding["trainer_version"],
            "output": {
                "resource_id": inspection.resource_id,
                "relative_path": binding["output_relative_path"],
                "bytes": inspection.size,
                "header_sha256": inspection.container["header_sha256"],
                "tensor_count": inspection.container["tensor_count"],
                "metadata_sha256": hashlib.sha256(_canonical(observed["metadata"])).hexdigest(),
                "role": binding["role"],
                "family": binding["family"],
            },
            "checkpoint": {
                "sequence": checkpoint["sequence"],
                "name": checkpoint["name"],
                "step": checkpoint["step"],
                "epoch": checkpoint["epoch"],
                "tip": worker["checkpoint_tip"],
                "model_state_sha256": checkpoint["state"]["model"],
            },
            "quality": "unmeasured",
        }
        # Re-check the package once after output/checkpoint hashing so the
        # receipt never spans two different package states.
        final_manifest, final_manifest_sha256 = _package(binding, should_cancel)
        if final_manifest_sha256 != manifest_sha256 or _canonical(final_manifest) != _canonical(manifest):
            raise TrainingResultError("training_result_package_changed")
        return VerifiedTrainingResult(plan, outcome, contract_raw, expected_anchor_sha256, _canonical(receipt))

    @_safe
    def register(self, registry: ModelResourceRegistry, verified: VerifiedTrainingResult,
                 expected_registry: str, *, should_cancel=None) -> str:
        if type(registry) is not ModelResourceRegistry or type(verified) is not VerifiedTrainingResult or not _hash(expected_registry):
            raise TrainingResultError("training_result_registration_invalid")
        contract = resources._json(verified.contract_json)
        checked = self.verify(
            verified.plan,
            verified.outcome,
            contract,
            verified.anchor_sha256,
            should_cancel=should_cancel,
        )
        if checked.receipt_json != verified.receipt_json:
            raise TrainingResultError("training_result_verification_changed")
        _, binding, _ = _validated_contract(contract, verified.plan)
        parents = []
        for resource_id in binding["parent_ids"]:
            resolution = registry.resolve(resource_id, progress=None, should_cancel=should_cancel)
            if resolution.status != "identity_verified" or resolution.inspection is None:
                raise TrainingResultError("training_result_parent_unavailable")
            parents.append(resolution.inspection)
        with registry._writer():
            value, current = registry.snapshot()
            if current != expected_registry:
                raise TrainingResultError("training_result_registry_changed")
            # Repeat every non-parent proof while holding the publication lock.
            final = self.verify(
                verified.plan,
                verified.outcome,
                contract,
                verified.anchor_sha256,
                should_cancel=should_cancel,
            )
            if final.receipt_json != verified.receipt_json:
                raise TrainingResultError("training_result_verification_changed")
            receipt = final.receipt
            output_path = (verified.plan.destination / Path(binding["output_relative_path"])).absolute()
            inspection = resources.inspect_model_resource(output_path, should_cancel=should_cancel)
            if inspection.resource_id != receipt["output"]["resource_id"]:
                raise TrainingResultError("training_result_output_changed")
            for parent in parents:
                resources._current(parent)
            resources._current(inspection)
            lineage = {
                "schema_version": 1,
                "operation": "training",
                "parent_ids": deepcopy(binding["parent_ids"]),
                "package_fingerprint": binding["package_fingerprint"],
                "plan_fingerprint": verified.plan.fingerprint,
                "training_configuration": deepcopy(_package(binding, should_cancel)[0]["training_configuration"]),
                "trainer_id": binding["trainer_id"],
                "trainer_version": binding["trainer_version"],
                "settings": deepcopy(binding["settings"]),
                "settings_sha256": binding["settings_sha256"],
                "output_id": inspection.resource_id,
                "authority": "collectigen_verified",
                "execution_receipt": "sha256:" + final.receipt_sha256,
                "quality": "unmeasured",
            }
            if len(_canonical(lineage)) > MAX_LINEAGE_BYTES:
                raise TrainingResultError("training_result_lineage_too_large")
            record = {
                "identity": inspection.stored_identity(),
                "locations": [{"path": str(inspection.path), "file_state": list(inspection.file_state)}],
                "declarations": [{
                    "role": binding["role"],
                    "family": binding["family"],
                    "source": "",
                    "license_note": "",
                    "authority": "collectigen_verified",
                    "compatibility": "adapter_verified",
                }],
                "lineage": [lineage],
            }
            _cancel(should_cancel)
            for parent in parents:
                resources._current(parent)
            resources._current(inspection)
            existing = value["resources"].get(inspection.resource_id)
            if existing is not None:
                # Reopening a completed run is read-only only when the entire
                # independently verified record, receipt and lineage agree.
                if _canonical(existing) != _canonical(record):
                    raise TrainingResultError("training_result_output_collision")
                return inspection.resource_id
            value["resources"][inspection.resource_id] = record
            registry._save(value, current)
        return inspection.resource_id
