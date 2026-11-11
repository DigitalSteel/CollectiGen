"""Read already anchored Training results without reauthorizing their old trainer.

The stopped journal and its exact input-verification receipts describe what ran.
Current output, package and checkpoint bytes still require inspection. New Training
uses its unchanged launch owners. This service never creates anchors, registers,
resumes or launches a trainer; callers supply their retained receipt hash.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path

from .training_process import (TrainingProcessPlan, TrainingProcessResult,
    TrainingProcessService, TrainingRunJournal, LOCAL_ENVIRONMENT)
from .training_verification import verified_receipt_hashes
from .training_execution_plan import (validate_contract_structure,
    SINGLETON_INPUT_PURPOSES, GROUP_INPUT_PURPOSES, MANIFEST_NAME,
    TrainingExecutionPlanError, _eligibility_receipt)
from .training_result import (TrainingResultError, VerifiedTrainingResult,
    _safe, _hash, _canonical, _read, _sha, _validate_binding,
    _anchor, _worker_result, _package, _checkpoint, _output, _cancel)


def _retained_contract(contract, plan):
    if type(plan) is not TrainingProcessPlan:
        raise TrainingResultError("training_result_process_invalid")
    plan.validate()
    raw = _canonical(contract)
    if len(raw) > 4 * 1024**2 or hashlib.sha256(raw).hexdigest() != plan.preview_fingerprint:
        raise TrainingResultError("training_result_contract_unbound")
    checked = validate_contract_structure(deepcopy(contract))
    if _canonical(checked) != raw:
        raise TrainingResultError("training_result_contract_invalid")
    singletons, _ = _recorded_plan_binding(checked, plan)
    _eligibility_receipt(checked, singletons)
    binding = checked["result_binding"]
    _validate_binding(binding, plan)
    return deepcopy(checked), deepcopy(binding), raw


def _recorded_plan_binding(contract, plan):
    plan.validate()
    process, workspace = contract["process"], contract["workspace"]
    if (
        plan.schema_version != 3 or plan.environment_profile != LOCAL_ENVIRONMENT
        or workspace != {"root": plan.root, "root_identity": list(plan.root_identity), "name": plan.name}
        or process["command"] != list(plan.command)
        or not 0 <= process["contract_input_index"] < len(plan.inputs)
        or len(plan.inputs) != len(process["inputs"]) + 1
        or len(plan.inventories) != len(process["inventories"])
    ):
        raise TrainingExecutionPlanError("training_execution_process_unbound")
    contract_input = plan.inputs[process["contract_input_index"]]
    if contract_input.sha256 != plan.preview_fingerprint or _read(Path(contract_input.path), limit=4 * 1024**2) != _canonical(contract):
        raise TrainingExecutionPlanError("training_execution_contract_input_changed")
    expected_inputs = []
    for row in process["inputs"]:
        if row["index"] >= len(plan.inputs):
            raise TrainingExecutionPlanError("training_execution_inputs_unbound")
        selected = plan.inputs[row["index"]]
        expected_inputs.append({"index": row["index"], "purpose": row["purpose"], "slot": row["slot"],
                                "path": selected.path, "state": list(selected.state), "sha256": selected.sha256})
    if _canonical(expected_inputs) != _canonical(process["inputs"]):
        raise TrainingExecutionPlanError("training_execution_inputs_unbound")
    singletons = {row["purpose"]: row for row in process["inputs"] if row["purpose"] in SINGLETON_INPUT_PURPOSES | {"vae_model"}}
    groups = {purpose: {row["slot"]: row for row in process["inputs"] if row["purpose"] == purpose}
              for purpose in GROUP_INPUT_PURPOSES}
    recorded_app = Path(singletons["worker"]["path"]).parent.parent
    recorded_worker = recorded_app / "tools" / "training_execution_worker.py"
    recorded_profile = recorded_app / "tools" / "training_execution_profile.json"
    if (
        singletons["python"]["index"] != 0 or singletons["python"]["path"] != plan.command[0]
        or singletons["worker"]["path"] != str(recorded_worker)
        or process["command"] != [singletons["python"]["path"], "-I", "-S", "-B", "-X", "utf8",
                                   singletons["worker"]["path"], contract_input.path]
        or singletons["package_manifest"]["path"] != str(Path(contract["package"]["path"]) / MANIFEST_NAME)
        or singletons["package_manifest"]["sha256"] != contract["package"]["manifest_sha256"]
        or singletons["configuration"]["sha256"] != contract["review"]["configuration_file_sha256"]
        or singletons["eligibility_receipt"]["sha256"] != contract["resources"]["eligibility_receipt_sha256"]
        or singletons["qualification_profile"]["path"] != str(recorded_profile)
        or singletons["qualification_evidence"]["sha256"] != contract["qualification"]["evidence_sha256"]
        or singletons["base_model"]["path"] != contract["resources"]["base"]["path"]
        or "sha256:" + singletons["base_model"]["sha256"] != contract["resources"]["base"]["id"]
        or singletons["prepared_patch"]["sha256"] != contract["continuation"]["contract"]["prepared_source"]["patched_sha256"]
    ):
        raise TrainingExecutionPlanError("training_execution_command_or_input_changed")
    vae = contract["resources"]["vae"]
    if ((vae is None) != ("vae_model" not in singletons)
            or vae is not None and (singletons["vae_model"]["path"] != vae["path"]
                                    or "sha256:" + singletons["vae_model"]["sha256"] != vae["id"])):
        raise TrainingExecutionPlanError("training_execution_resource_input_changed")
    expected_code = contract["qualification"]["owned_code_sha256"]
    if (set(groups["owned_code"]) != set(expected_code)
            or any(groups["owned_code"][name]["path"] != str(recorded_app / name)
                   or groups["owned_code"][name]["sha256"] != expected_code[name] for name in expected_code)):
        raise TrainingExecutionPlanError("training_execution_owned_code_input_changed")
    for role in (1, 2):
        group = groups[f"tokenizer_{role}_file"]
        resource = contract["resources"]["tokenizers"][role - 1]
        if (set(group) != set(resource["files"])
                or any(group[name]["path"] != str(Path(resource["path"]) / name)
                       or group[name]["sha256"] != resource["files"][name] for name in resource["files"])):
            raise TrainingExecutionPlanError("training_execution_tokenizer_input_changed")
    expected_inventories = []
    inventory_by_manifest = {item.manifest_path: item for item in plan.inventories}
    for row in process["inventories"]:
        item = inventory_by_manifest.get(row["manifest_path"])
        if item is None:
            raise TrainingExecutionPlanError("training_execution_inventories_unbound")
        expected_inventories.append({"purpose": row["purpose"], **asdict(item), "inventory_sha256": row["inventory_sha256"]})
        input_row = singletons[row["purpose"]]
        if input_row["path"] != item.manifest_path or input_row["sha256"] != item.manifest_sha256:
            raise TrainingExecutionPlanError("training_execution_inventories_unbound")
    if _canonical(expected_inventories) != _canonical(process["inventories"]):
        raise TrainingExecutionPlanError("training_execution_inventories_unbound")
    inventory_rows = {row["purpose"]: row for row in process["inventories"]}
    if inventory_rows["package_inventory"]["root"] != contract["package"]["path"]:
        raise TrainingExecutionPlanError("training_execution_inventory_scope_changed")
    return singletons, groups


def _retained_process_evidence(plan: TrainingProcessPlan, outcome: TrainingProcessResult) -> tuple[list[dict], str, list[str]]:
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
    return rows, tip, receipts


@_safe
def verify_retained_result(plan, outcome, contract, expected_anchor_sha256, *, expected_receipt_sha256, should_cancel=None) -> VerifiedTrainingResult:
    _, binding, contract_raw = _retained_contract(contract, plan)
    _, tip, receipts = _retained_process_evidence(plan, outcome)
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
    verified = VerifiedTrainingResult(plan, outcome, contract_raw, expected_anchor_sha256, _canonical(receipt))
    if not _hash(expected_receipt_sha256) or verified.receipt_sha256 != expected_receipt_sha256:
        raise TrainingResultError("training_result_retained_receipt_changed")
    return verified
