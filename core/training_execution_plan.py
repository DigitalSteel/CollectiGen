"""Strict launch contract for the supported managed SDXL LoRA worker.

Structure validation is pure and can assemble qualification evidence.  Product
validation additionally requires a real pinned qualification profile, exact
process bindings, current software inventories, and a freshly verified package.
No worker field or caller boolean grants launch authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re

from . import training_continuation_execution as continuation
from . import training_continuation as resume_contract
from . import training_checkpoint as checkpoint
from .training_file_inventory import SoftwareInventoryService
from .training_process import LOCAL_ENVIRONMENT, ProcessInput, TrainingProcessPlan, _bytes, _read
from .training_runtime_contract import ADAPTER_ID, ADAPTER_VERSION, SOURCE_REVISION, validate_configuration
from .training_tokenizer_files import TOKENIZER_FILES
from .training_source_loader import EXECUTION_MODULES


PROFILE = "collectigen.sdxl-training-execution-v1"
KIND = "collectigen_sdxl_training_execution"
WORKER = Path(__file__).resolve().parents[1] / "tools" / "training_execution_worker.py"
PROFILE_FILE = Path(__file__).resolve().parents[1] / "tools" / "training_execution_profile.json"
MANIFEST_NAME = "collectigen-training-manifest.json"
MAX_PACKAGE_MANIFEST = 128 * 1024**2
MAX_NATIVE_READINESS_RECEIPT = 4 * 1024**2
_ROOT = Path(__file__).resolve().parents[1]
_CORE_CLOSURE = tuple(name + '.py' for name in EXECUTION_MODULES)
OWNED_CODE = {
    **{f"core/{name}": _ROOT / "core" / name for name in _CORE_CLOSURE},
    **{f"tools/{name}": _ROOT / "tools" / name for name in (
        "training_runtime_probe.py", "training_runtime_sources.json", "training_sdxl_layout.json",
        "training_verification_worker.py")},
}
SHA = re.compile(r"[0-9a-f]{64}\Z")
RESOURCE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
TOKENIZER_ID = re.compile(r"clip-files-v1:[0-9a-f]{64}\Z")
SINGLETON_INPUT_PURPOSES = {
    "python", "worker", "configuration", "eligibility_receipt", "base_model",
    "package_manifest", "qualification_profile", "qualification_evidence",
    "prepared_patch", "device_receipt", "selected_model_receipt",
    "selected_dataset_receipt", "component_receipt", "tokenizer_receipt",
    "reference_admission_receipt", "native_readiness_receipt",
    "source_inventory", "environment_inventory", "python_base_inventory", "package_inventory",
}
GROUP_INPUT_PURPOSES = {"owned_code", "tokenizer_1_file", "tokenizer_2_file", "resume_evidence"}
INPUT_PURPOSES = SINGLETON_INPUT_PURPOSES | GROUP_INPUT_PURPOSES | {"vae_model"}
INVENTORY_PURPOSES = {"source_inventory", "environment_inventory", "python_base_inventory", "package_inventory"}
TOP_FIELDS = {
    "schema_version", "kind", "profile", "qualification", "workspace",
    "process", "package", "recipe", "resources", "continuation", "review",
    "result_binding",
}
QUALIFICATION_FIELDS = {
    "profile", "evidence_sha256", "adapter_id", "adapter_version",
    "source_revision", "runtime_profile", "worker_sha256", "owned_code_sha256",
}
RESULT_FIELDS = {
    "output_relative_path", "role", "family", "parent_ids", "package_path",
    "package_fingerprint", "manifest_sha256", "configuration_sha256", "settings",
    "settings_sha256", "trainer_id", "trainer_version", "continuation_contract",
    "expected_final_step", "expected_final_epoch", "expected_checkpoint_sequence",
    "tensor_specs", "required_metadata",
}
ELIGIBILITY_FIELDS = {
    "schema_version", "kind", "qualification_profile", "qualification_evidence_sha256",
    "package_fingerprint", "configuration_sha256", "registry_revision", "base_id", "vae_id",
    "component_binding_sha256", "tokenizer_binding_sha256", "source_inventory_sha256",
    "environment_inventory_sha256", "python_base_inventory_sha256", "device_receipt_sha256",
    "package_inventory_sha256", "selected_model_receipt_sha256",
    "selected_dataset_receipt_sha256", "component_receipt_sha256",
    "tokenizer_receipt_sha256", "reference_admission_receipt_sha256",
    "native_readiness_receipt_sha256",
}
REFERENCE_ADMISSION_FIELDS = {
    "schema_version", "kind", "qualification_profile", "qualification_evidence_sha256",
    "runtime_profile", "source_revision", "base_id", "component_binding_sha256",
    "tokenizer_binding_sha256", "tokenizer_identities", "software_inventories",
    "device_observation_sha256", "continuation_contract_sha256", "native_readiness_receipt",
}


class TrainingExecutionPlanError(ValueError):
    """Stable validation failure without selected input details."""


def _safe(operation):
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (TrainingExecutionPlanError, InterruptedError):
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
            raise TrainingExecutionPlanError("training_execution_unavailable_or_changed") from None
    return wrapped


def qualified_profile():
    """Return real pinned learned-resource qualification, once incorporated.

    ``None`` is deliberate: generated diagnostics and an in-progress reference
    run cannot authorize product training.
    """
    if not PROFILE_FILE.is_file():
        return None
    raw = _read(PROFILE_FILE, limit=512 * 1024)
    value = _strict_json(raw)
    if _canonical(value) != raw:
        raise TrainingExecutionPlanError("training_execution_profile_invalid")
    return value


def _hash(value) -> bool:
    return type(value) is str and SHA.fullmatch(value) is not None


def _resource(value) -> bool:
    return type(value) is str and RESOURCE_ID.fullmatch(value) is not None


def _canonical(value) -> bytes:
    try:
        return _bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise TrainingExecutionPlanError("training_execution_contract_invalid") from exc


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _exact_dict(value, fields, reason):
    if type(value) is not dict or set(value) != fields:
        raise TrainingExecutionPlanError(reason)
    return value


def _relative_output(value) -> bool:
    if type(value) is not str or len(value) > 512:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and len(path.parts) == 2
        and path.parts[0] == "work"
        and path.parts[1] not in {"", ".", ".."}
        and Path(path.parts[1]).suffix.casefold() == ".safetensors"
        and str(path).replace("\\", "/") == value.replace("\\", "/")
    )


def _qualification(value):
    _exact_dict(value, QUALIFICATION_FIELDS, "training_execution_qualification_invalid")
    if (
        type(value["profile"]) is not str or not value["profile"]
        or not _hash(value["evidence_sha256"])
        or value["adapter_id"] != ADAPTER_ID
        or type(value["adapter_version"]) is not int or value["adapter_version"] != ADAPTER_VERSION
        or value["source_revision"] != SOURCE_REVISION
        or type(value["runtime_profile"]) is not str or not value["runtime_profile"]
        or not _hash(value["worker_sha256"])
        or type(value["owned_code_sha256"]) is not dict
        or set(value["owned_code_sha256"]) != set(OWNED_CODE)
        or any(not _hash(item) for item in value["owned_code_sha256"].values())
    ):
        raise TrainingExecutionPlanError("training_execution_qualification_invalid")


def _workspace(value):
    _exact_dict(value, {"root", "root_identity", "name"}, "training_execution_workspace_invalid")
    if (
        type(value["root"]) is not str or not Path(value["root"]).is_absolute()
        or type(value["root_identity"]) is not list or len(value["root_identity"]) != 2
        or any(type(item) is not int or item < 0 for item in value["root_identity"])
        or type(value["name"]) is not str or not value["name"]
    ):
        raise TrainingExecutionPlanError("training_execution_workspace_invalid")


def _process(value):
    _exact_dict(value, {"environment_profile", "command", "contract_input_index", "inputs", "inventories"},
                "training_execution_process_invalid")
    if value["environment_profile"] != LOCAL_ENVIRONMENT:
        raise TrainingExecutionPlanError("training_execution_environment_unsupported")
    command = value["command"]
    if type(command) is not list or len(command) != 8 or any(type(item) is not str for item in command):
        raise TrainingExecutionPlanError("training_execution_command_invalid")
    if command[1:6] != ["-I", "-S", "-B", "-X", "utf8"]:
        raise TrainingExecutionPlanError("training_execution_command_invalid")
    if type(value["contract_input_index"]) is not int or value["contract_input_index"] < 0:
        raise TrainingExecutionPlanError("training_execution_inputs_invalid")
    inputs, purposes, indices, slots = value["inputs"], set(), set(), set()
    if type(inputs) is not list or not 1 <= len(inputs) <= 127:
        raise TrainingExecutionPlanError("training_execution_inputs_invalid")
    for row in inputs:
        _exact_dict(row, {"index", "purpose", "slot", "path", "state", "sha256"}, "training_execution_inputs_invalid")
        if (
            type(row["index"]) is not int or row["index"] < 0
            or row["index"] == value["contract_input_index"] or row["index"] in indices
            or row["purpose"] not in INPUT_PURPOSES
            or row["purpose"] in SINGLETON_INPUT_PURPOSES and row["purpose"] in purposes
            or type(row["slot"]) is not str or not row["slot"] or len(row["slot"]) > 160
            or row["purpose"] in SINGLETON_INPUT_PURPOSES | {"vae_model"} and row["slot"] != "0"
            or (row["purpose"], row["slot"]) in slots
            or type(row["path"]) is not str or not Path(row["path"]).is_absolute()
            or type(row["state"]) is not list or len(row["state"]) != 5
            or any(type(item) is not int or item < 0 for item in row["state"])
            or not _hash(row["sha256"])
        ):
            raise TrainingExecutionPlanError("training_execution_inputs_invalid")
        indices.add(row["index"]); purposes.add(row["purpose"]); slots.add((row["purpose"], row["slot"]))
    required = SINGLETON_INPUT_PURPOSES | {"owned_code", "tokenizer_1_file", "tokenizer_2_file"}
    if not required <= purposes or purposes - required not in (set(), {"vae_model"}, {"resume_evidence"}, {"vae_model", "resume_evidence"}):
        raise TrainingExecutionPlanError("training_execution_inputs_invalid")
    if value["contract_input_index"] != len(inputs) or indices != set(range(len(inputs))):
        raise TrainingExecutionPlanError("training_execution_inputs_invalid")
    counts = {purpose: sum(row["purpose"] == purpose for row in inputs) for purpose in GROUP_INPUT_PURPOSES}
    if counts["owned_code"] != len(OWNED_CODE) or counts["tokenizer_1_file"] != len(TOKENIZER_FILES) \
            or counts["tokenizer_2_file"] != len(TOKENIZER_FILES) or counts["resume_evidence"] > 12:
        raise TrainingExecutionPlanError("training_execution_inputs_invalid")
    inventories, inventory_purposes = value["inventories"], set()
    if type(inventories) is not list or len(inventories) != 4:
        raise TrainingExecutionPlanError("training_execution_inventories_invalid")
    for row in inventories:
        _exact_dict(row, {"purpose", "root", "manifest_path", "manifest_sha256", "inventory_sha256", "file_count", "byte_count"},
                    "training_execution_inventories_invalid")
        if (
            row["purpose"] not in INVENTORY_PURPOSES or row["purpose"] in inventory_purposes
            or type(row["root"]) is not str or not Path(row["root"]).is_absolute()
            or type(row["manifest_path"]) is not str or not Path(row["manifest_path"]).is_absolute()
            or not _hash(row["manifest_sha256"]) or not _hash(row["inventory_sha256"])
            or type(row["file_count"]) is not int or not 1 <= row["file_count"] <= 200_000
            or type(row["byte_count"]) is not int or not 1 <= row["byte_count"] <= 64 * 1024**3
        ):
            raise TrainingExecutionPlanError("training_execution_inventories_invalid")
        inventory_purposes.add(row["purpose"])
    if inventory_purposes != INVENTORY_PURPOSES:
        raise TrainingExecutionPlanError("training_execution_inventories_invalid")


def _package(value):
    _exact_dict(value, {"path", "fingerprint", "manifest_sha256", "configuration_sha256", "plan_fingerprint"},
                "training_execution_package_invalid")
    if (
        type(value["path"]) is not str or not Path(value["path"]).is_absolute()
        or any(not _hash(value[key]) for key in ("fingerprint", "manifest_sha256", "configuration_sha256", "plan_fingerprint"))
    ):
        raise TrainingExecutionPlanError("training_execution_package_invalid")


def _recipe(value):
    _exact_dict(value, {"adapter_id", "adapter_version", "source_revision", "runtime_profile", "proposal", "configuration"},
                "training_execution_recipe_invalid")
    if (
        value["adapter_id"] != ADAPTER_ID
        or type(value["adapter_version"]) is not int or value["adapter_version"] != ADAPTER_VERSION
        or value["source_revision"] != SOURCE_REVISION
        or type(value["runtime_profile"]) is not str or not value["runtime_profile"]
    ):
        raise TrainingExecutionPlanError("training_execution_recipe_invalid")
    proposal = value["proposal"]
    _exact_dict(proposal, {"rank", "alpha", "output_precision", "gradient_checkpointing", "schema_version"},
                "training_execution_recipe_invalid")
    if (type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
            or type(proposal["rank"]) is not int or not 4 <= proposal["rank"] <= 128
            or type(proposal["alpha"]) not in {int, float} or not math.isfinite(proposal["alpha"])
            or not 1 <= proposal["alpha"] <= proposal["rank"]
            or proposal["output_precision"] not in {"fp16", "bf16", "float"}
            or type(proposal["gradient_checkpointing"]) is not bool):
        raise TrainingExecutionPlanError("training_execution_recipe_invalid")
    validate_configuration(value["configuration"])


def _resources(value):
    _exact_dict(value, {"base", "vae", "tokenizers", "registry_revision", "component_binding_sha256",
                        "tokenizer_binding_sha256", "eligibility_receipt_sha256"},
                "training_execution_resources_invalid")
    base = value["base"]
    _exact_dict(base, {"id", "path"}, "training_execution_resources_invalid")
    vae = value["vae"]
    if vae is not None:
        _exact_dict(vae, {"id", "path"}, "training_execution_resources_invalid")
    tokenizers = value["tokenizers"]
    if (
        not _resource(base["id"]) or type(base["path"]) is not str or not Path(base["path"]).is_absolute()
        or vae is not None and (not _resource(vae["id"]) or type(vae["path"]) is not str or not Path(vae["path"]).is_absolute())
        or type(tokenizers) is not list or len(tokenizers) != 2
        or any(not _hash(value[key]) for key in ("registry_revision", "component_binding_sha256",
                                                  "tokenizer_binding_sha256", "eligibility_receipt_sha256"))
    ):
        raise TrainingExecutionPlanError("training_execution_resources_invalid")
    expected_names = {name for name, _ in TOKENIZER_FILES}
    for role, tokenizer in enumerate(tokenizers, 1):
        _exact_dict(tokenizer, {"role", "path", "files", "identity"}, "training_execution_resources_invalid")
        if (tokenizer["role"] != role or type(tokenizer["path"]) is not str or not Path(tokenizer["path"]).is_absolute()
                or type(tokenizer["files"]) is not dict or set(tokenizer["files"]) != expected_names
                or any(not _hash(item) for item in tokenizer["files"].values())
                or type(tokenizer["identity"]) is not str or TOKENIZER_ID.fullmatch(tokenizer["identity"]) is None):
            raise TrainingExecutionPlanError("training_execution_resources_invalid")


def _continuation(value):
    _exact_dict(value, {"mode", "contract", "previous"}, "training_execution_continuation_invalid")
    continuation.validate_contract(value["contract"])
    if value["mode"] == "fresh":
        if value["previous"] is not None:
            raise TrainingExecutionPlanError("training_execution_continuation_invalid")
    elif value["mode"] == "resume":
        previous = value["previous"]
        _exact_dict(previous, {"destination", "binding", "anchor_sha256", "sequence", "name", "step", "epoch",
                               "checkpoint_receipt_sha256", "continuation_receipt_sha256"},
                    "training_execution_continuation_invalid")
        if (
            type(previous["destination"]) is not str or not Path(previous["destination"]).is_absolute()
            or not _hash(previous["anchor_sha256"])
            or type(previous["sequence"]) is not int or not 0 <= previous["sequence"] < continuation.MAX_SAVES
            or type(previous["name"]) is not str or checkpoint.NAME.fullmatch(previous["name"]) is None
            or type(previous["step"]) is not int or type(previous["epoch"]) is not int
            or not _hash(previous["checkpoint_receipt_sha256"])
            or not _hash(previous["continuation_receipt_sha256"])
        ):
            raise TrainingExecutionPlanError("training_execution_continuation_invalid")
        checkpoint._binding(previous["binding"])
        continuation.saved_position(value["contract"], previous["step"], previous["epoch"], resume=True)
        settings = value["contract"]["settings"]
        if previous["step"] >= settings["max_train_steps"]:
            raise TrainingExecutionPlanError("training_execution_continuation_complete")
        if previous["step"] % settings["save_every_n_steps"]:
            raise TrainingExecutionPlanError("training_execution_continuation_invalid")
    else:
        raise TrainingExecutionPlanError("training_execution_continuation_invalid")


def _review(value):
    fields = {"images", "configuration_sha256", "configuration_file_sha256", "trainer_arguments",
              "resources", "output", "estimates", "sampling", "checkpoint", "privacy"}
    _exact_dict(value, fields, "training_execution_review_invalid")
    images = _exact_dict(value["images"], {"count", "roles", "manifest_sha256"}, "training_execution_review_invalid")
    if (
        type(images["count"]) is not int or images["count"] < 1
        or type(images["roles"]) is not dict or not images["roles"]
        or any(type(key) is not str or type(count) is not int or count < 0 for key, count in images["roles"].items())
        or sum(images["roles"].values()) != images["count"]
        or not _hash(images["manifest_sha256"])
        or not _hash(value["configuration_sha256"])
        or not _hash(value["configuration_file_sha256"])
    ):
        raise TrainingExecutionPlanError("training_execution_review_invalid")
    arguments = value["trainer_arguments"]
    expected_argument_fields = {"network_module", "network_train_unet_only", "network_dim", "network_alpha",
        "learning_rate", "mixed_precision", "save_precision", "gradient_checkpointing", "max_train_steps",
        "gradient_accumulation_steps", "seed", "save_every_n_steps", "save_state", "workers", "output_relative_path"}
    _exact_dict(arguments, expected_argument_fields, "training_execution_arguments_invalid")
    if (
        arguments["network_module"] != "networks.lora" or arguments["network_train_unet_only"] is not True
        or arguments["save_state"] is not True or arguments["workers"] != 0
        or not _relative_output(arguments["output_relative_path"])
    ):
        raise TrainingExecutionPlanError("training_execution_arguments_invalid")
    review_resources = _exact_dict(value["resources"], {"base", "vae", "tokenizers", "tokenizer_binding_sha256"},
                                   "training_execution_review_invalid")
    output = _exact_dict(value["output"], {"relative_path", "collision"}, "training_execution_review_invalid")
    estimates = _exact_dict(value["estimates"], {"optimizer_steps", "epochs", "checkpoints"}, "training_execution_review_invalid")
    sampling = _exact_dict(value["sampling"], {"mode"}, "training_execution_review_invalid")
    checkpoint_review = _exact_dict(value["checkpoint"], {"save_every_n_steps", "resume_mode"}, "training_execution_review_invalid")
    privacy = _exact_dict(value["privacy"], {"compute", "connectivity", "data_transfer"}, "training_execution_review_invalid")
    if (
        type(review_resources["base"]) is not dict
        or review_resources["vae"] is not None and type(review_resources["vae"]) is not dict
        or type(review_resources["tokenizers"]) is not list or len(review_resources["tokenizers"]) != 2
        or not _hash(review_resources["tokenizer_binding_sha256"])
        or not _relative_output(output["relative_path"]) or output["collision"] != "reserved"
        or any(type(estimates[key]) is not int or estimates[key] < 1 for key in estimates)
        or sampling["mode"] != "none"
        or type(checkpoint_review["save_every_n_steps"]) is not int or checkpoint_review["save_every_n_steps"] < 1
        or checkpoint_review["resume_mode"] not in {"fresh", "explicit_anchored_checkpoint"}
        or privacy != {"compute": "local", "connectivity": "normal", "data_transfer": "none"}
    ):
        raise TrainingExecutionPlanError("training_execution_review_invalid")


def _result(value):
    _exact_dict(value, RESULT_FIELDS, "training_execution_result_binding_invalid")
    if (
        not _relative_output(value["output_relative_path"])
        or value["role"] != "lora" or value["family"].casefold() != "sdxl"
        or type(value["parent_ids"]) is not list or not 1 <= len(value["parent_ids"]) <= 2
        or len(set(value["parent_ids"])) != len(value["parent_ids"])
        or any(not _resource(item) for item in value["parent_ids"])
        or type(value["package_path"]) is not str or not Path(value["package_path"]).is_absolute()
        or any(not _hash(value[key]) for key in ("package_fingerprint", "manifest_sha256", "configuration_sha256", "settings_sha256"))
        or _digest(value["settings"]) != value["settings_sha256"]
        or type(value["trainer_id"]) is not str or not value["trainer_id"]
        or type(value["trainer_version"]) is not str or not value["trainer_version"]
        or type(value["expected_final_step"]) is not int or value["expected_final_step"] < 1
        or type(value["expected_final_epoch"]) is not int or value["expected_final_epoch"] < 1
        or type(value["expected_checkpoint_sequence"]) is not int or not 0 <= value["expected_checkpoint_sequence"] < continuation.MAX_SAVES
        or type(value["tensor_specs"]) is not dict or not value["tensor_specs"]
        or type(value["required_metadata"]) is not dict
        or "collectigen_execution_contract_sha256" in value["required_metadata"]
    ):
        raise TrainingExecutionPlanError("training_execution_result_binding_invalid")
    continuation.validate_contract(value["continuation_contract"])
    for name, spec in value["tensor_specs"].items():
        if (
            type(name) is not str or not name or name == "__metadata__"
            or type(spec) is not dict or set(spec) != {"shape", "dtype"}
            or spec["dtype"] not in {"F16", "BF16"}
            or type(spec["shape"]) is not list or len(spec["shape"]) > 32
            or any(type(item) is not int or item < 0 for item in spec["shape"])
        ):
            raise TrainingExecutionPlanError("training_execution_result_layout_invalid")
    if any(type(key) is not str or not key or type(item) is not str for key, item in value["required_metadata"].items()):
        raise TrainingExecutionPlanError("training_execution_result_metadata_invalid")


def _cross_validate(value):
    qualification, package, recipe = value["qualification"], value["package"], value["recipe"]
    resources, resolved, review, output = value["resources"], value["continuation"]["contract"], value["review"], value["result_binding"]
    proposal, configuration, settings = recipe["proposal"], recipe["configuration"], resolved["settings"]
    parents = [resources["base"]["id"], *((resources["vae"]["id"],) if resources["vae"] else ())]
    input_purposes = {row["purpose"] for row in value["process"]["inputs"]}
    prior_step = value["continuation"]["previous"]["step"] if value["continuation"]["mode"] == "resume" else 0
    saves = (settings["max_train_steps"] - prior_step) // settings["save_every_n_steps"]
    per_epoch = math.ceil(resolved["epoch_batches"] / settings["gradient_accumulation_steps"])
    final_epoch = math.ceil(settings["max_train_steps"] / per_epoch)
    expected_arguments = {
        "network_module": "networks.lora", "network_train_unet_only": True,
        "network_dim": settings["network_dim"], "network_alpha": settings["network_alpha"],
        "learning_rate": settings["learning_rate"], "mixed_precision": settings["mixed_precision"],
        "save_precision": configuration["save_precision"], "gradient_checkpointing": settings["gradient_checkpointing"],
        "max_train_steps": settings["max_train_steps"], "gradient_accumulation_steps": settings["gradient_accumulation_steps"],
        "seed": settings["seed"], "save_every_n_steps": settings["save_every_n_steps"],
        "save_state": True, "workers": 0, "output_relative_path": output["output_relative_path"],
    }
    if (
        recipe["runtime_profile"] != qualification["runtime_profile"]
        or proposal["rank"] != settings["network_dim"] or float(proposal["alpha"]) != float(settings["network_alpha"])
        or proposal["output_precision"] != configuration["save_precision"]
        or proposal["gradient_checkpointing"] != settings["gradient_checkpointing"]
        or configuration["network_dim"] != settings["network_dim"]
        or float(configuration["network_alpha"]) != float(settings["network_alpha"])
        or configuration["learning_rate"] != settings["learning_rate"]
        or configuration["mixed_precision"] != settings["mixed_precision"]
        or configuration["gradient_accumulation_steps"] != settings["gradient_accumulation_steps"]
        or configuration["seed"] != settings["seed"]
        or package["fingerprint"] != output["package_fingerprint"]
        or package["path"] != output["package_path"]
        or package["manifest_sha256"] != output["manifest_sha256"]
        or package["configuration_sha256"] != output["configuration_sha256"]
        or output["continuation_contract"] != resolved
        or output["settings"] != settings
        or output["parent_ids"] != parents
        or output["trainer_id"] != qualification["adapter_id"]
        or output["trainer_version"] != qualification["source_revision"]
        or output["expected_final_step"] != settings["max_train_steps"]
        or output["expected_final_epoch"] != final_epoch
        or output["expected_checkpoint_sequence"] != saves - 1
        or review["configuration_sha256"] != package["configuration_sha256"]
        or review["images"]["manifest_sha256"] != package["manifest_sha256"]
        or review["resources"] != {"base": resources["base"], "vae": resources["vae"],
                                   "tokenizers": resources["tokenizers"],
                                   "tokenizer_binding_sha256": resources["tokenizer_binding_sha256"]}
        or review["output"] != {"relative_path": output["output_relative_path"], "collision": "reserved"}
        or output["output_relative_path"] != "work/" + configuration["output_name"] + ".safetensors"
        or review["trainer_arguments"] != expected_arguments
        or review["estimates"] != {"optimizer_steps": settings["max_train_steps"],
                                   "epochs": final_epoch, "checkpoints": saves}
        or review["checkpoint"] != {"save_every_n_steps": settings["save_every_n_steps"],
                                     "resume_mode": "fresh" if value["continuation"]["mode"] == "fresh" else "explicit_anchored_checkpoint"}
        or (value["continuation"]["mode"] == "resume") != ("resume_evidence" in input_purposes)
    ):
        raise TrainingExecutionPlanError("training_execution_cross_binding_changed")


@_safe
def validate_contract_structure(contract):
    """Pure exact-schema validation; does not qualify or authorize execution."""
    _exact_dict(contract, TOP_FIELDS, "training_execution_contract_invalid")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["kind"] != KIND or contract["profile"] != PROFILE:
        raise TrainingExecutionPlanError("training_execution_contract_invalid")
    _qualification(contract["qualification"])
    _workspace(contract["workspace"])
    _process(contract["process"])
    _package(contract["package"])
    _recipe(contract["recipe"])
    _resources(contract["resources"])
    _continuation(contract["continuation"])
    _review(contract["review"])
    _result(contract["result_binding"])
    _cross_validate(contract)
    if len(_canonical(contract)) > 4 * 1024**2:
        raise TrainingExecutionPlanError("training_execution_contract_too_large")
    return contract


def _plan_binding(contract, plan):
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
        if ProcessInput.capture(selected.path) != selected:
            raise TrainingExecutionPlanError("training_execution_input_changed")
    if _canonical(expected_inputs) != _canonical(process["inputs"]):
        raise TrainingExecutionPlanError("training_execution_inputs_unbound")
    singletons = {row["purpose"]: row for row in process["inputs"] if row["purpose"] in SINGLETON_INPUT_PURPOSES | {"vae_model"}}
    groups = {purpose: {row["slot"]: row for row in process["inputs"] if row["purpose"] == purpose}
              for purpose in GROUP_INPUT_PURPOSES}
    if (
        singletons["python"]["index"] != 0 or singletons["python"]["path"] != plan.command[0]
        or singletons["worker"]["path"] != str(WORKER.absolute())
        or process["command"] != [singletons["python"]["path"], "-I", "-S", "-B", "-X", "utf8",
                                   singletons["worker"]["path"], contract_input.path]
        or singletons["package_manifest"]["path"] != str(Path(contract["package"]["path"]) / MANIFEST_NAME)
        or singletons["package_manifest"]["sha256"] != contract["package"]["manifest_sha256"]
        or singletons["configuration"]["sha256"] != contract["review"]["configuration_file_sha256"]
        or singletons["eligibility_receipt"]["sha256"] != contract["resources"]["eligibility_receipt_sha256"]
        or singletons["qualification_profile"]["path"] != str(PROFILE_FILE.absolute())
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
            or any(groups["owned_code"][name]["path"] != str(OWNED_CODE[name].absolute())
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


def _eligibility_receipt(contract, singletons):
    input_row = singletons["eligibility_receipt"]
    raw = _read(Path(input_row["path"]), limit=64 * 1024)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise TrainingExecutionPlanError("training_execution_eligibility_invalid") from None
    if (type(value) is not dict or set(value) != ELIGIBILITY_FIELDS or _canonical(value) != raw
            or hashlib.sha256(raw).hexdigest() != contract["resources"]["eligibility_receipt_sha256"]
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "sdxl_execution_eligibility"):
        raise TrainingExecutionPlanError("training_execution_eligibility_invalid")
    resources = contract["resources"]
    inventories = {row["purpose"]: row for row in contract["process"]["inventories"]}
    expected = {
        "qualification_profile": contract["qualification"]["profile"],
        "qualification_evidence_sha256": contract["qualification"]["evidence_sha256"],
        "package_fingerprint": contract["package"]["fingerprint"],
        "configuration_sha256": contract["package"]["configuration_sha256"],
        "registry_revision": resources["registry_revision"],
        "base_id": resources["base"]["id"],
        "vae_id": resources["vae"]["id"] if resources["vae"] else None,
        "component_binding_sha256": resources["component_binding_sha256"],
        "tokenizer_binding_sha256": resources["tokenizer_binding_sha256"],
        "source_inventory_sha256": inventories["source_inventory"]["inventory_sha256"],
        "environment_inventory_sha256": inventories["environment_inventory"]["inventory_sha256"],
        "python_base_inventory_sha256": inventories["python_base_inventory"]["inventory_sha256"],
        "package_inventory_sha256": inventories["package_inventory"]["inventory_sha256"],
        "device_receipt_sha256": singletons["device_receipt"]["sha256"],
        "selected_model_receipt_sha256": singletons["selected_model_receipt"]["sha256"],
        "selected_dataset_receipt_sha256": singletons["selected_dataset_receipt"]["sha256"],
        "component_receipt_sha256": singletons["component_receipt"]["sha256"],
        "tokenizer_receipt_sha256": singletons["tokenizer_receipt"]["sha256"],
        "reference_admission_receipt_sha256": singletons["reference_admission_receipt"]["sha256"],
        "native_readiness_receipt_sha256": singletons["native_readiness_receipt"]["sha256"],
    }
    if any(value[key] != item for key, item in expected.items()):
        raise TrainingExecutionPlanError("training_execution_eligibility_changed")
    _semantic_receipts(contract, singletons, inventories)


def _receipt(row, fields, kind):
    raw = _read(Path(row["path"]), limit=256 * 1024)
    value = _strict_json(raw)
    if (type(value) is not dict or set(value) != fields or _canonical(value) != raw
            or hashlib.sha256(raw).hexdigest() != row["sha256"]
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != kind):
        raise TrainingExecutionPlanError("training_execution_receipt_invalid")
    return value


def _semantic_receipts(contract, singletons, inventories):
    qualification, resources = contract["qualification"], contract["resources"]
    device = _receipt(singletons["device_receipt"], {
        "schema_version", "kind", "qualification_profile", "qualification_evidence_sha256",
        "environment_profile", "observation", "observation_sha256"}, "sdxl_device_observation")
    observation = device["observation"]
    _exact_dict(observation, {"device_type", "cuda_index", "name", "compute_capability",
                              "total_memory_bytes", "torch_version", "cuda_version"},
                "training_execution_device_receipt_invalid")
    if (device["qualification_profile"] != qualification["profile"]
            or device["qualification_evidence_sha256"] != qualification["evidence_sha256"]
            or device["environment_profile"] != contract["process"]["environment_profile"]
            or _digest(observation) != device["observation_sha256"]
            or observation["device_type"] != "cuda"
            or type(observation["cuda_index"]) is not int or observation["cuda_index"] != 0
            or type(observation["name"]) is not str or not observation["name"]
            or type(observation["compute_capability"]) is not list or len(observation["compute_capability"]) != 2
            or type(observation["compute_capability"][0]) is not int or observation["compute_capability"][0] < 1
            or type(observation["compute_capability"][1]) is not int or observation["compute_capability"][1] < 0
            or type(observation["total_memory_bytes"]) is not int or observation["total_memory_bytes"] < 1024**3
            or any(type(observation[key]) is not str or not observation[key] for key in ("torch_version", "cuda_version"))):
        raise TrainingExecutionPlanError("training_execution_device_receipt_invalid")
    model = _receipt(singletons["selected_model_receipt"], {
        "schema_version", "kind", "registry_revision", "base", "vae"}, "sdxl_selected_model")
    if model != {"schema_version": 1, "kind": "sdxl_selected_model",
                 "registry_revision": resources["registry_revision"], "base": resources["base"],
                 "vae": resources["vae"]}:
        raise TrainingExecutionPlanError("training_execution_model_receipt_invalid")
    dataset = _receipt(singletons["selected_dataset_receipt"], {
        "schema_version", "kind", "package", "package_inventory_sha256", "dataset_order_sha256",
        "epoch_batches", "images"}, "sdxl_selected_dataset")
    expected_dataset = {"schema_version": 1, "kind": "sdxl_selected_dataset",
        "package": contract["package"], "package_inventory_sha256": inventories["package_inventory"]["inventory_sha256"],
        "dataset_order_sha256": contract["continuation"]["contract"]["dataset_order_sha256"],
        "epoch_batches": contract["continuation"]["contract"]["epoch_batches"],
        "images": contract["review"]["images"]}
    if dataset != expected_dataset:
        raise TrainingExecutionPlanError("training_execution_dataset_receipt_invalid")
    component = _receipt(singletons["component_receipt"], {
        "schema_version", "kind", "binding_sha256", "base", "vae"}, "sdxl_component_selection")
    if component != {"schema_version": 1, "kind": "sdxl_component_selection",
                     "binding_sha256": resources["component_binding_sha256"],
                     "base": resources["base"], "vae": resources["vae"]}:
        raise TrainingExecutionPlanError("training_execution_component_receipt_invalid")
    tokenizer = _receipt(singletons["tokenizer_receipt"], {
        "schema_version", "kind", "binding_sha256", "tokenizers"}, "sdxl_tokenizer_selection")
    if tokenizer != {"schema_version": 1, "kind": "sdxl_tokenizer_selection",
                     "binding_sha256": resources["tokenizer_binding_sha256"],
                     "tokenizers": resources["tokenizers"]}:
        raise TrainingExecutionPlanError("training_execution_tokenizer_receipt_invalid")
    reference = _receipt(singletons["reference_admission_receipt"], REFERENCE_ADMISSION_FIELDS,
                         "sdxl_reference_execution_admission")
    native = reference["native_readiness_receipt"]
    _exact_dict(native, {"path", "sha256"}, "training_execution_reference_admission_invalid")
    tokenizer_identities = [{"role": row["role"], "identity": row["identity"]}
                            for row in resources["tokenizers"]]
    software = {name: inventories[name + "_inventory"]["inventory_sha256"]
                for name in ("source", "environment", "python_base")}
    if (reference != {
            "schema_version": 1, "kind": "sdxl_reference_execution_admission",
            "qualification_profile": qualification["profile"],
            "qualification_evidence_sha256": qualification["evidence_sha256"],
            "runtime_profile": qualification["runtime_profile"],
            "source_revision": qualification["source_revision"],
            "base_id": resources["base"]["id"],
            "component_binding_sha256": resources["component_binding_sha256"],
            "tokenizer_binding_sha256": resources["tokenizer_binding_sha256"],
            "tokenizer_identities": tokenizer_identities,
            "software_inventories": software,
            "device_observation_sha256": device["observation_sha256"],
            "continuation_contract_sha256": _digest(contract["continuation"]["contract"]),
            "native_readiness_receipt": native,
        }
            or type(native["path"]) is not str or not Path(native["path"]).is_absolute()
            or not _hash(native["sha256"])
            or native != {"path": singletons["native_readiness_receipt"]["path"],
                          "sha256": singletons["native_readiness_receipt"]["sha256"]}):
        raise TrainingExecutionPlanError("training_execution_reference_admission_invalid")
    native_raw = _read(Path(native["path"]), limit=MAX_NATIVE_READINESS_RECEIPT)
    if (hashlib.sha256(native_raw).hexdigest() != native["sha256"]
            or _canonical(_strict_json(native_raw)) != native_raw):
        raise TrainingExecutionPlanError("training_execution_native_readiness_invalid")


def _profile(contract):
    profile = qualified_profile()
    if profile is None:
        raise TrainingExecutionPlanError("training_execution_profile_unqualified")
    _exact_dict(profile, {"schema_version", "kind", "qualification", "base_id", "recipe_policy", "tensor_specs_sha256"},
                "training_execution_profile_invalid")
    policy = profile["recipe_policy"]
    _exact_dict(policy, {"ranks", "mixed_precision", "output_precision", "gradient_checkpointing"},
                "training_execution_profile_invalid")
    if (type(profile["schema_version"]) is not int or profile["schema_version"] != 1
            or profile["kind"] != "sdxl_training_execution_profile"
            or type(policy["ranks"]) is not list or not policy["ranks"]
            or any(type(item) is not int or not 4 <= item <= 128 for item in policy["ranks"])
            or type(policy["mixed_precision"]) is not list or not set(policy["mixed_precision"]) <= {"fp16", "bf16"}
            or not policy["mixed_precision"]
            or type(policy["output_precision"]) is not list or not set(policy["output_precision"]) <= {"F16", "BF16", "fp16", "bf16"}
            or not policy["output_precision"]
            or type(policy["gradient_checkpointing"]) is not list
            or not policy["gradient_checkpointing"] or any(type(item) is not bool for item in policy["gradient_checkpointing"])):
        raise TrainingExecutionPlanError("training_execution_profile_invalid")
    proposal = contract["recipe"]["proposal"]
    if (
        _canonical(contract["qualification"]) != _canonical(profile["qualification"])
        or contract["resources"]["base"]["id"] != profile["base_id"]
        or proposal["rank"] not in policy["ranks"]
        or contract["recipe"]["configuration"]["mixed_precision"] not in policy["mixed_precision"]
        or proposal["output_precision"] not in policy["output_precision"]
        or proposal["gradient_checkpointing"] not in policy["gradient_checkpointing"]
        or _digest(contract["result_binding"]["tensor_specs"]) != profile["tensor_specs_sha256"]
    ):
        raise TrainingExecutionPlanError("training_execution_profile_mismatch")
    qualification = profile["qualification"]
    if not WORKER.is_file() or hashlib.sha256(_read(WORKER, limit=2 * 1024**2)).hexdigest() != qualification["worker_sha256"]:
        raise TrainingExecutionPlanError("training_execution_worker_changed")
    for name, path in OWNED_CODE.items():
        if not path.is_file() or hashlib.sha256(_read(path, limit=2 * 1024**2)).hexdigest() != qualification["owned_code_sha256"][name]:
            raise TrainingExecutionPlanError("training_execution_owned_code_changed")


def _fresh_package(contract):
    from .training_package import TrainingPackageService
    expected = contract["package"]
    verified = TrainingPackageService().verify(expected["path"])
    if not verified.valid or verified.package_fingerprint != expected["fingerprint"]:
        raise TrainingExecutionPlanError("training_execution_package_changed")
    manifest_raw = _read(verified.destination / MANIFEST_NAME, limit=MAX_PACKAGE_MANIFEST)
    _manifest_bindings(contract, manifest_raw, verified.manifest)


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("nonfinite_number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _manifest_bindings(contract, manifest_raw, known=None):
    manifest = _strict_json(manifest_raw)
    if type(manifest) is not dict or known is not None and _canonical(manifest) != _canonical(known):
        raise TrainingExecutionPlanError("training_execution_package_changed")
    expected = contract["package"]
    configuration = manifest.get("training_configuration")
    candidate = manifest.get("candidate_plan")
    target = configuration.get("target") if type(configuration) is dict else None
    output = configuration.get("expected_output") if type(configuration) is dict else None
    package_plan = manifest.get("package_plan")
    items = package_plan.get("items") if type(package_plan) is dict else None
    roles = {}
    if type(items) is list:
        for item in items:
            role = item.get("role") if type(item) is dict else None
            if type(role) is not str:
                raise TrainingExecutionPlanError("training_execution_package_changed")
            roles[role] = roles.get(role, 0) + 1
    if (
        hashlib.sha256(manifest_raw).hexdigest() != expected["manifest_sha256"]
        or _digest(configuration) != expected["configuration_sha256"]
        or type(candidate) is not dict or candidate.get("plan_fingerprint") != expected["plan_fingerprint"]
        or type(target) is not dict or target.get("base_model_id") != contract["resources"]["base"]["id"]
        or str(target.get("model_family", "")).casefold() != "sdxl"
        or type(output) is not dict or str(output.get("kind", "")).casefold() != "lora"
        or contract["review"]["images"]["count"] != manifest.get("counts", {}).get("included_images")
        or contract["review"]["images"]["roles"] != roles
    ):
        raise TrainingExecutionPlanError("training_execution_package_changed")
    return manifest


def _worker_package(contract, singletons):
    row = singletons["package_manifest"]
    if row["path"] != str(Path(contract["package"]["path"]) / MANIFEST_NAME):
        raise TrainingExecutionPlanError("training_execution_package_changed")
    return _manifest_bindings(contract, _read(Path(row["path"]), limit=MAX_PACKAGE_MANIFEST))


def _resume_inputs(contract, rows, selected):
    previous = contract["continuation"]["previous"]
    destination = Path(previous["destination"])
    work = destination / "work"
    name = selected["name"]
    receipt = work / (name + ".collectigen.json")
    resume_contract.inspect_checkpoint(
        work, name, previous["binding"], contract["continuation"]["contract"]["prerequisite"],
        checkpoint_receipt_sha256=selected["proof"]["checkpoint_receipt_sha256"],
        continuation_receipt_sha256=selected["proof"]["continuation_receipt_sha256"],
    )
    expected_paths = {
        "checkpoint_anchor": destination / "checkpoint-anchor.json",
        "journal_header": destination / "checkpoint-events" / "header.json",
        "journal_entry": destination / "checkpoint-events" / f"{previous['sequence']:04d}.json",
        "checkpoint_receipt": receipt,
        "continuation_receipt": receipt.with_suffix(".continuation.json"),
        "save_returned": receipt.with_suffix(".save-returned.json"),
        **{f"member:{member}": work / name / member for member in checkpoint.FILES},
    }
    if set(rows) != set(expected_paths) or any(rows[slot]["path"] != str(path.absolute())
                                                   for slot, path in expected_paths.items()):
        raise TrainingExecutionPlanError("training_execution_resume_inputs_changed")
    if (rows["checkpoint_anchor"]["sha256"] != previous["anchor_sha256"]
            or rows["checkpoint_receipt"]["sha256"] != selected["proof"]["checkpoint_receipt_sha256"]
            or rows["continuation_receipt"]["sha256"] != selected["proof"]["continuation_receipt_sha256"]):
        raise TrainingExecutionPlanError("training_execution_resume_inputs_changed")


def _validate_bound_contract(contract, plan):
    validate_contract_structure(contract)
    if type(plan) is not TrainingProcessPlan:
        raise TrainingExecutionPlanError("training_execution_process_invalid")
    raw = _canonical(contract)
    if hashlib.sha256(raw).hexdigest() != plan.preview_fingerprint:
        raise TrainingExecutionPlanError("training_execution_contract_unbound")
    _profile(contract)
    singletons, groups = _plan_binding(contract, plan)
    _eligibility_receipt(contract, singletons)
    for row in contract["process"]["inventories"]:
        summary = SoftwareInventoryService().verify(row["root"], row["manifest_path"])
        if (summary["inventory_sha256"] != row["inventory_sha256"]
                or summary["files"] != row["file_count"] or summary["bytes"] != row["byte_count"]
                or summary["scope"] != "explicit_software_directory"):
            raise TrainingExecutionPlanError("training_execution_inventory_changed")
    if contract["continuation"]["mode"] == "resume":
        previous = contract["continuation"]["previous"]
        selected = continuation.anchored_checkpoint(
            previous["destination"], previous["binding"], contract["continuation"]["contract"],
            previous["anchor_sha256"], previous["sequence"])
        if ({key: selected[key] for key in ("name", "step", "epoch")} !=
                {key: previous[key] for key in ("name", "step", "epoch")}
                or selected["proof"] != {"checkpoint_receipt_sha256": previous["checkpoint_receipt_sha256"],
                                         "continuation_receipt_sha256": previous["continuation_receipt_sha256"]}):
            raise TrainingExecutionPlanError("training_execution_resume_selection_changed")
        _resume_inputs(contract, groups["resume_evidence"], selected)
    elif groups["resume_evidence"]:
        raise TrainingExecutionPlanError("training_execution_resume_inputs_unexpected")
    return singletons


@_safe
def validate_worker_contract(contract, plan):
    """Native-safe recheck of the exact qualified launch and package manifest."""
    singletons = _validate_bound_contract(contract, plan)
    _worker_package(contract, singletons)
    return contract


@_safe
def validate_execution_contract(contract, plan):
    """Parent-side full package verification over the same worker contract."""
    validate_worker_contract(contract, plan)
    _fresh_package(contract)
    return contract
