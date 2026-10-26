"""Prepare one exact reviewed SDXL training launch without starting a process."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import math
import os
from pathlib import Path

from . import training_checkpoint as checkpoint
from . import training_continuation_execution as continuation
from . import training_execution_plan as execution_plan
from . import training_reference_tokenizers as reference_tokenizers
from .training_adapter_preview import SdxlLoraRecipe
from .training_configuration import TrainingConfiguration
from .training_dataset_preparation import binding as package_selection_binding
from .training_execution_runtime import dataset_identity
from .training_file_inventory import SoftwareInventoryService
from .training_local_components import LocalComponentSelection
from .training_package import MANIFEST_NAME, TrainingPackageService, TrainingPackageVerification
from .training_process import (LOCAL_ENVIRONMENT, ProcessInput, ProcessInventory,
    TrainingProcessPlan, _bytes, _directory, _read, _write_new)
from .training_runtime_contract import (ADAPTER_ID, ADAPTER_VERSION, SOURCE_REVISION,
    configuration_toml, validate_configuration)
from .training_tokenizer_files import TOKENIZER_FILES, files_identity, read_slot
from .training_source_io import _assert_unredirected


MAX_RECEIPT = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 86400
DEFAULT_LOG_LIMIT = 1024 * 1024


class TrainingExecutionPreparationError(ValueError):
    """Stable preparation failure without selected content in the message."""


@dataclass(frozen=True)
class QualifiedExecutionEnvironment:
    python: Path
    source_root: Path
    environment_root: Path
    python_base_root: Path
    prepared_patch: Path
    qualification_evidence: Path


@dataclass(frozen=True)
class TrainingExecutionPreparation:
    plan: TrainingProcessPlan
    contract: dict = field(repr=False)
    preparation_path: Path
    receipt_hashes: dict


def _cancel(callback):
    if callback and callback():
        raise InterruptedError("training_execution_preparation_cancelled")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _write(path, raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECEIPT:
        raise TrainingExecutionPreparationError("training_execution_preparation_receipt_invalid")
    from .training_execution import _write_exact_bytes
    _write_exact_bytes(Path(path), raw)
    if _read(Path(path), limit=MAX_RECEIPT) != raw:
        raise TrainingExecutionPreparationError("training_execution_preparation_readback_failed")
    return _hash(raw)


def _absolute(path, *, file=False, directory=False):
    value = Path(path).absolute()
    _assert_unredirected(value)
    if file and not value.is_file():
        raise TrainingExecutionPreparationError("training_execution_preparation_input_unavailable")
    if directory and not value.is_dir():
        raise TrainingExecutionPreparationError("training_execution_preparation_input_unavailable")
    return value


def _environment(value):
    if type(value) is not QualifiedExecutionEnvironment:
        raise TrainingExecutionPreparationError("training_execution_environment_invalid")
    result = QualifiedExecutionEnvironment(
        _absolute(value.python, file=True), _absolute(value.source_root, directory=True),
        _absolute(value.environment_root, directory=True), _absolute(value.python_base_root, directory=True),
        _absolute(value.prepared_patch, file=True), _absolute(value.qualification_evidence, file=True))
    roots = (result.source_root, result.environment_root, result.python_base_root)
    if (len(set(roots)) != 3 or not any(result.python.is_relative_to(root)
                       for root in (result.environment_root, result.python_base_root))):
        raise TrainingExecutionPreparationError("training_execution_environment_invalid")
    return result


def _runtime_configuration(manifest, recipe, base_path):
    config = TrainingConfiguration.from_dict(manifest["training_configuration"])
    config.require_single_checkpoint_adapter()
    value = {
        "network_module": "networks.lora", "network_dim": recipe.rank,
        "network_alpha": float(recipe.alpha), "network_train_unet_only": True,
        "optimizer_type": "AdamW", "lr_scheduler": "constant",
        "max_train_epochs": config.epochs,
        "gradient_accumulation_steps": config.gradient_accumulation,
        "learning_rate": config.learning_rate,
        "mixed_precision": {"float32": "no"}.get(config.precision, config.precision),
        "seed": config.seed, "gradient_checkpointing": recipe.gradient_checkpointing,
        "save_model_as": "safetensors", "save_precision": recipe.output_precision,
        "output_name": config.output_name, "no_half_vae": True,
        "cache_latents": False, "cache_latents_to_disk": False,
        "max_data_loader_n_workers": 0, "persistent_data_loader_workers": False,
        "save_state": False, "save_state_on_train_end": False,
        "pretrained_model_name_or_path": str(base_path),
    }
    return validate_configuration(value)


def _epoch_batches(manifest):
    config = TrainingConfiguration.from_dict(manifest["training_configuration"])
    artifacts = {row["path"]: row for row in manifest["artifacts"]}
    buckets = {}
    for row in manifest["package_plan"]["items"]:
        if row["role"] != "training":
            continue
        dimensions = artifacts[row["image_path"]]["transform"]["output_dimensions"]
        if type(dimensions) is not list or len(dimensions) != 2:
            raise TrainingExecutionPreparationError("training_execution_dataset_invalid")
        key = tuple(dimensions)
        buckets[key] = buckets.get(key, 0) + config.repeats
    if not buckets:
        raise TrainingExecutionPreparationError("training_execution_dataset_empty")
    return sum(math.ceil(count / config.batch_size) for count in buckets.values())


def _inventory(service, root, manifest_path, purpose, should_cancel, progress):
    captured = service.capture(root, should_cancel=should_cancel, progress=progress)
    service.save(captured, manifest_path)
    summary = captured.summary()
    selected = ProcessInventory(str(root), str(manifest_path), _hash(manifest_path.read_bytes()),
                                summary["files"], summary["bytes"])
    row = {"purpose": purpose, **asdict(selected), "inventory_sha256": summary["inventory_sha256"]}
    return selected, row


def _selected_resources(registry, selection, expected_registry, should_cancel, progress):
    if type(selection) is not LocalComponentSelection:
        raise TrainingExecutionPreparationError("training_execution_selection_invalid")
    selection.validate()
    if str(registry.path) != selection.registry:
        raise TrainingExecutionPreparationError("training_execution_registry_changed")
    _, revision = registry.snapshot()
    if revision != expected_registry or selection.registry_revision != expected_registry:
        raise TrainingExecutionPreparationError("training_execution_registry_changed")
    resources = []
    for role, selected in (("base", selection.base), ("vae", selection.vae)):
        if selected is None:
            resources.append(None)
            continue
        resolved = registry.resolve(selected.resource_id, role="checkpoint" if role == "base" else "vae",
                                    family="SDXL", progress=progress, should_cancel=should_cancel)
        if (resolved.status != "identity_verified" or resolved.inspection is None
                or str(resolved.inspection.path) != selected.path):
            raise TrainingExecutionPreparationError("training_execution_model_changed")
        resources.append({"id": selected.resource_id, "path": selected.path})
    return resources[0], resources[1]


def _tokenizers(selection):
    result, selected = [], []
    for role, location in ((1, selection.tokenizer_1), (2, selection.tokenizer_2)):
        blobs = read_slot(location)
        files = {name: _hash(blobs[name]) for name, _ in TOKENIZER_FILES}
        identity = files_identity(blobs)
        result.append({"role": role, "path": location, "files": files, "identity": identity})
        selected.append({"role": role, "identity": identity})
    binding = reference_tokenizer_binding(selected)
    return result, selected, binding


def reference_tokenizer_binding(tokenizer_identities):
    """Canonical production binding for the two qualified reference tokenizers."""
    return _hash(_bytes({"schema_version": 1, "kind": "sdxl_reference_tokenizer_selection",
        "profile": reference_tokenizers.PROFILE, "slots": deepcopy(tokenizer_identities)}))


def reference_component_binding(base_id, tokenizer_identities):
    """Canonical component binding shared with the native readiness worker."""
    return _hash(_bytes({"base": base_id,
                        "tokenizers": [row["identity"] for row in tokenizer_identities]}))


def _reference_admission(value, profile, base, tokenizer_identities,
                         tokenizer_binding_sha256, inventories, observation_sha256, resolved,
                         should_cancel=None):
    if type(value) is not dict or set(value) != execution_plan.REFERENCE_ADMISSION_FIELDS:
        raise TrainingExecutionPreparationError("training_execution_reference_admission_invalid")
    native = value.get("native_readiness_receipt")
    if (type(native) is not dict or set(native) != {"path", "sha256"}
            or type(native["path"]) is not str or not Path(native["path"]).is_absolute()
            or type(native["sha256"]) is not str):
        raise TrainingExecutionPreparationError("training_execution_reference_admission_invalid")
    native_path = _absolute(native["path"], file=True)
    native_input = ProcessInput.capture(native_path, should_cancel=should_cancel)
    if native_input.sha256 != native["sha256"]:
        raise TrainingExecutionPreparationError("training_execution_native_readiness_changed")
    from .training_execution_readiness import MAX_RECEIPT as MAX_NATIVE_RECEIPT, validate_reference_admission
    try:
        validate_reference_admission(value, _read(native_path, limit=MAX_NATIVE_RECEIPT))
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise TrainingExecutionPreparationError("training_execution_native_readiness_invalid") from None
    expected = {
        "schema_version": 1, "kind": "sdxl_reference_execution_admission",
        "qualification_profile": profile["qualification"]["profile"],
        "qualification_evidence_sha256": profile["qualification"]["evidence_sha256"],
        "runtime_profile": profile["qualification"]["runtime_profile"],
        "source_revision": profile["qualification"]["source_revision"],
        "base_id": base["id"],
        "component_binding_sha256": reference_component_binding(base["id"], tokenizer_identities),
        "tokenizer_binding_sha256": tokenizer_binding_sha256,
        "tokenizer_identities": tokenizer_identities,
        "software_inventories": {name: inventories[name + "_inventory"]["inventory_sha256"]
                                 for name in ("source", "environment", "python_base")},
        "device_observation_sha256": observation_sha256,
        "continuation_contract_sha256": continuation.digest(resolved),
        "native_readiness_receipt": {"path": str(native_path), "sha256": native_input.sha256},
    }
    if value != expected:
        raise TrainingExecutionPreparationError("training_execution_reference_admission_changed")
    return deepcopy(expected), native_input


def _resume_paths(previous):
    destination = Path(previous["destination"])
    work, name = destination / "work", previous["name"]
    receipt = work / (name + ".collectigen.json")
    return {
        "checkpoint_anchor": destination / "checkpoint-anchor.json",
        "journal_header": destination / "checkpoint-events/header.json",
        "journal_entry": destination / "checkpoint-events" / f"{previous['sequence']:04d}.json",
        "checkpoint_receipt": receipt,
        "continuation_receipt": receipt.with_suffix(".continuation.json"),
        "save_returned": receipt.with_suffix(".save-returned.json"),
        **{f"member:{member}": work / name / member for member in checkpoint.FILES},
    }


class TrainingExecutionPreparationService:
    """Create exact launch evidence for later explicit confirmation."""

    def prepare(self, package, registry, recipe, selection, reference_admission, resolved, environment,
                device_observation, preparation_root, run_root, run_name, *, tensor_specs,
                previous=None, expected_registry, timeout_seconds=DEFAULT_TIMEOUT,
                log_limit=DEFAULT_LOG_LIMIT, should_cancel=None, progress=None):
        _cancel(should_cancel)
        profile = execution_plan.qualified_profile()
        if profile is None:
            raise TrainingExecutionPreparationError("training_execution_profile_unqualified")
        environment = _environment(environment)
        if not isinstance(package, TrainingPackageVerification) or not package.valid:
            raise TrainingExecutionPreparationError("training_execution_package_invalid")
        fresh = TrainingPackageService().verify(package.destination, should_cancel=should_cancel)
        if not fresh.valid or fresh.package_fingerprint != package.package_fingerprint or fresh.manifest != package.manifest:
            raise TrainingExecutionPreparationError("training_execution_package_changed")
        if not isinstance(recipe, SdxlLoraRecipe):
            raise TrainingExecutionPreparationError("training_execution_recipe_invalid")
        recipe.validate()
        continuation.validate_contract(resolved)
        prerequisite = resolved["prerequisite"]
        selected = prerequisite["selection"]
        if (selected["adapter_id"] != ADAPTER_ID or selected["adapter_version"] != ADAPTER_VERSION
                or selected["source_revision"] != SOURCE_REVISION
                or selected["runtime_profile"] != profile["qualification"]["runtime_profile"]):
            raise TrainingExecutionPreparationError("training_execution_admission_changed")
        base, vae = _selected_resources(registry, selection, expected_registry, should_cancel, progress)
        manifest = fresh.manifest
        manifest_raw = _read(fresh.destination / MANIFEST_NAME, limit=execution_plan.MAX_PACKAGE_MANIFEST)
        manifest_sha = _hash(manifest_raw)
        configuration_sha = _hash(_bytes(manifest["training_configuration"]))
        expected_package = package_selection_binding({"root": str(fresh.destination),
            "package_fingerprint": fresh.package_fingerprint, "manifest_sha256": manifest_sha,
            "recipe": asdict(recipe)})
        if (selected["base_id"] != base["id"] or selected["local_vae_override_id"] != (vae["id"] if vae else None)
                or previous is None and selected["registry_revision"] != expected_registry
                or selected["package"] != expected_package
                or selected["caption_limit"] != manifest["training_configuration"]["captions"]["max_tokens"]
                or selected["configuration_sha256"] != configuration_sha):
            raise TrainingExecutionPreparationError("training_execution_selection_changed")
        configuration = _runtime_configuration(manifest, recipe, base["path"])
        package_configuration = TrainingConfiguration.from_dict(manifest["training_configuration"])
        expected_steps = package_configuration.epochs * math.ceil(
            resolved["epoch_batches"] / resolved["settings"]["gradient_accumulation_steps"])
        if (_hash(_read(environment.prepared_patch, limit=2 * 1024**2)) != resolved["prepared_source"]["patched_sha256"]
                or _hash(_read(environment.qualification_evidence, limit=MAX_RECEIPT)) != profile["qualification"]["evidence_sha256"]
                or dataset_identity(fresh.destination, manifest) != resolved["dataset_order_sha256"]
                or _epoch_batches(manifest) != resolved["epoch_batches"]
                or resolved["settings"]["max_train_steps"] != expected_steps):
            raise TrainingExecutionPreparationError("training_execution_evidence_changed")
        tokenizer_rows, tokenizer_identities, tokenizer_binding_sha = _tokenizers(selection)
        if type(tensor_specs) is not dict or _hash(_bytes(tensor_specs)) != profile["tensor_specs_sha256"]:
            raise TrainingExecutionPreparationError("training_execution_tensor_layout_changed")
        if previous is not None:
            if type(previous) is not dict:
                raise TrainingExecutionPreparationError("training_execution_resume_invalid")
            row = continuation.anchored_checkpoint(previous["destination"], previous["binding"], resolved,
                                                   previous["anchor_sha256"], previous["sequence"])
            if ({key: previous.get(key) for key in ("name", "step", "epoch")} !=
                    {key: row[key] for key in ("name", "step", "epoch")} or
                    {key: previous.get(key) for key in row["proof"]} != row["proof"]):
                raise TrainingExecutionPreparationError("training_execution_resume_changed")
            settings = resolved["settings"]
            if previous["step"] >= settings["max_train_steps"] or previous["step"] % settings["save_every_n_steps"]:
                raise TrainingExecutionPreparationError("training_execution_resume_complete_or_unaligned")
        preparation = Path(preparation_root).absolute()
        run_root = _absolute(run_root, directory=True)
        _assert_unredirected(preparation)
        if (os.path.lexists(preparation) or os.path.lexists(run_root / run_name)
                or preparation.is_relative_to(run_root) or run_root.is_relative_to(preparation)):
            raise TrainingExecutionPreparationError("training_execution_destination_exists")
        roots = (Path(fresh.destination), environment.source_root, environment.environment_root,
                 environment.python_base_root, Path(selection.tokenizer_1), Path(selection.tokenizer_2),
                 Path(base["path"]).parent, environment.prepared_patch.parent,
                 *((Path(vae["path"]).parent,) if vae else ()))
        if any(preparation.is_relative_to(root) or root.is_relative_to(preparation) for root in roots):
            raise TrainingExecutionPreparationError("training_execution_preparation_scope_invalid")
        preparation.mkdir()
        service = SoftwareInventoryService()
        inventory_pairs = []
        for purpose, root in (("source_inventory", environment.source_root),
                              ("environment_inventory", environment.environment_root),
                              ("python_base_inventory", environment.python_base_root),
                              ("package_inventory", Path(fresh.destination))):
            _cancel(should_cancel)
            inventory_pairs.append(_inventory(service, root, preparation / (purpose + ".json"),
                                               purpose, should_cancel, progress))
        inventories = tuple(pair[0] for pair in inventory_pairs)
        inventory_rows = [pair[1] for pair in inventory_pairs]
        prior_runtime = prerequisite["runtime"]["software_inventories"]
        current_inventory = {row["purpose"]: row for row in inventory_rows}
        if (prior_runtime["environment"]["inventory_sha256"] != current_inventory["environment_inventory"]["inventory_sha256"]
                or prior_runtime["python_base"]["inventory_sha256"] != current_inventory["python_base_inventory"]["inventory_sha256"]):
            raise TrainingExecutionPreparationError("training_execution_environment_changed")
        observation = deepcopy(device_observation)
        observation_sha = _hash(_bytes(observation))
        reference_admission, native_readiness_input = _reference_admission(
            reference_admission, profile, base, tokenizer_identities,
            tokenizer_binding_sha, current_inventory, observation_sha, resolved, should_cancel)
        reference_admission_path = preparation / "reference-admission.json"
        reference_admission_sha = _write(reference_admission_path, _bytes(reference_admission))
        config_path = preparation / "trainer.toml"
        config_sha = _write(config_path, configuration_toml(configuration).encode("utf-8"))
        roles = {}
        for item in manifest["package_plan"]["items"]:
            roles[item["role"]] = roles.get(item["role"], 0) + 1
        package_binding = {"path": str(fresh.destination), "fingerprint": fresh.package_fingerprint,
            "manifest_sha256": manifest_sha, "configuration_sha256": configuration_sha,
            "plan_fingerprint": manifest["candidate_plan"]["plan_fingerprint"]}
        resources = {"base": base, "vae": vae, "tokenizers": tokenizer_rows,
            "registry_revision": expected_registry,
            "component_binding_sha256": reference_admission["component_binding_sha256"],
            "tokenizer_binding_sha256": tokenizer_binding_sha, "eligibility_receipt_sha256": "0" * 64}
        images = {"count": manifest["counts"]["included_images"], "roles": roles,
                  "manifest_sha256": manifest_sha}
        device = {"schema_version": 1, "kind": "sdxl_device_observation",
            "qualification_profile": profile["qualification"]["profile"],
            "qualification_evidence_sha256": profile["qualification"]["evidence_sha256"],
            "environment_profile": LOCAL_ENVIRONMENT, "observation": observation,
            "observation_sha256": observation_sha}
        receipt_values = {
            "device_receipt": device,
            "selected_model_receipt": {"schema_version": 1, "kind": "sdxl_selected_model",
                "registry_revision": expected_registry, "base": base, "vae": vae},
            "selected_dataset_receipt": {"schema_version": 1, "kind": "sdxl_selected_dataset",
                "package": package_binding,
                "package_inventory_sha256": current_inventory["package_inventory"]["inventory_sha256"],
                "dataset_order_sha256": resolved["dataset_order_sha256"],
                "epoch_batches": resolved["epoch_batches"], "images": images},
            "component_receipt": {"schema_version": 1, "kind": "sdxl_component_selection",
                "binding_sha256": reference_admission["component_binding_sha256"], "base": base, "vae": vae},
            "tokenizer_receipt": {"schema_version": 1, "kind": "sdxl_tokenizer_selection",
                "binding_sha256": tokenizer_binding_sha, "tokenizers": tokenizer_rows},
        }
        receipt_paths, receipt_hashes, semantic_hashes = {}, {
            "configuration": config_sha,
            "reference_admission_receipt": reference_admission_sha,
            **{row["purpose"]: row["manifest_sha256"] for row in inventory_rows},
        }, {}
        for name, value in receipt_values.items():
            path = preparation / (name.replace("_receipt", "") + ".json")
            receipt_paths[name] = path
            semantic_hashes[name] = _write(path, _bytes(value))
            receipt_hashes[name] = semantic_hashes[name]
        eligibility = {"schema_version": 1, "kind": "sdxl_execution_eligibility",
            "qualification_profile": profile["qualification"]["profile"],
            "qualification_evidence_sha256": profile["qualification"]["evidence_sha256"],
            "package_fingerprint": fresh.package_fingerprint, "configuration_sha256": configuration_sha,
            "registry_revision": expected_registry, "base_id": base["id"], "vae_id": vae["id"] if vae else None,
            "component_binding_sha256": reference_admission["component_binding_sha256"],
            "tokenizer_binding_sha256": tokenizer_binding_sha,
            **{purpose + "_sha256": current_inventory[purpose]["inventory_sha256"]
               for purpose in ("source_inventory", "environment_inventory", "python_base_inventory", "package_inventory")},
            **{name + "_sha256": digest for name, digest in semantic_hashes.items()},
            "reference_admission_receipt_sha256": reference_admission_sha,
            "native_readiness_receipt_sha256": native_readiness_input.sha256}
        eligibility_path = preparation / "eligibility.json"
        resources["eligibility_receipt_sha256"] = _write(eligibility_path, _bytes(eligibility))
        receipt_hashes["eligibility_receipt"] = resources["eligibility_receipt_sha256"]
        _cancel(should_cancel)
        input_specs = [
            (environment.python, "python", "0"), (execution_plan.WORKER, "worker", "0"),
            (execution_plan.PROFILE_FILE, "qualification_profile", "0"),
            (environment.qualification_evidence, "qualification_evidence", "0"),
            (fresh.destination / MANIFEST_NAME, "package_manifest", "0"), (config_path, "configuration", "0"),
            (eligibility_path, "eligibility_receipt", "0"), (Path(base["path"]), "base_model", "0"),
            (environment.prepared_patch, "prepared_patch", "0"),
            (reference_admission_path, "reference_admission_receipt", "0"),
            (Path(native_readiness_input.path), "native_readiness_receipt", "0"),
            *((receipt_paths[name], name, "0") for name in receipt_values),
            *((Path(item.manifest_path), row["purpose"], "0") for item, row in inventory_pairs),
            *((path, "owned_code", name) for name, path in execution_plan.OWNED_CODE.items()),
            *((Path(row["path"]) / name, f"tokenizer_{row['role']}_file", name)
              for row in tokenizer_rows for name, _ in TOKENIZER_FILES),
            *(((Path(vae["path"]), "vae_model", "0"),) if vae else ()),
            *(((path, "resume_evidence", slot) for slot, path in _resume_paths(previous).items()) if previous else ()),
        ]
        inputs, input_rows = [], []
        for index, (path, purpose, slot) in enumerate(input_specs):
            _cancel(should_cancel)
            captured = ProcessInput.capture(path, should_cancel=should_cancel)
            inputs.append(captured)
            input_rows.append({"index": index, "purpose": purpose, "slot": slot,
                "path": captured.path, "state": list(captured.state), "sha256": captured.sha256})
        settings = resolved["settings"]
        prior_step = previous["step"] if previous else 0
        saves = (settings["max_train_steps"] - prior_step) // settings["save_every_n_steps"]
        per_epoch = math.ceil(resolved["epoch_batches"] / settings["gradient_accumulation_steps"])
        final_epoch = math.ceil(settings["max_train_steps"] / per_epoch)
        output_relative = "work/" + configuration["output_name"] + ".safetensors"
        arguments = {"network_module": "networks.lora", "network_train_unet_only": True,
            "network_dim": settings["network_dim"], "network_alpha": settings["network_alpha"],
            "learning_rate": settings["learning_rate"], "mixed_precision": settings["mixed_precision"],
            "save_precision": configuration["save_precision"],
            "gradient_checkpointing": settings["gradient_checkpointing"],
            "max_train_steps": settings["max_train_steps"],
            "gradient_accumulation_steps": settings["gradient_accumulation_steps"], "seed": settings["seed"],
            "save_every_n_steps": settings["save_every_n_steps"], "save_state": True, "workers": 0,
            "output_relative_path": output_relative}
        parents = [base["id"], *((vae["id"],) if vae else ())]
        result_binding = {"output_relative_path": output_relative, "role": "lora", "family": "SDXL",
            "parent_ids": parents, "package_path": str(fresh.destination),
            "package_fingerprint": fresh.package_fingerprint, "manifest_sha256": manifest_sha,
            "configuration_sha256": configuration_sha, "settings": deepcopy(settings),
            "settings_sha256": _hash(_bytes(settings)), "trainer_id": ADAPTER_ID,
            "trainer_version": SOURCE_REVISION, "continuation_contract": deepcopy(resolved),
            "expected_final_step": settings["max_train_steps"], "expected_final_epoch": final_epoch,
            "expected_checkpoint_sequence": saves - 1, "tensor_specs": deepcopy(tensor_specs),
            "required_metadata": {"ss_network_module": "networks.lora",
                                  "ss_network_dim": str(settings["network_dim"])}}
        contract_path = preparation / "execution-contract.json"
        command = [str(environment.python), "-I", "-S", "-B", "-X", "utf8",
                   str(execution_plan.WORKER), str(contract_path)]
        contract = {"schema_version": 1, "kind": execution_plan.KIND, "profile": execution_plan.PROFILE,
            "qualification": deepcopy(profile["qualification"]),
            "workspace": {"root": str(run_root), "root_identity": list(_directory(run_root)), "name": run_name},
            "process": {"environment_profile": LOCAL_ENVIRONMENT, "command": command,
                "contract_input_index": len(inputs), "inputs": input_rows, "inventories": inventory_rows},
            "package": package_binding,
            "recipe": {"adapter_id": ADAPTER_ID, "adapter_version": ADAPTER_VERSION,
                "source_revision": SOURCE_REVISION, "runtime_profile": selected["runtime_profile"],
                "proposal": asdict(recipe), "configuration": configuration},
            "resources": resources,
            "continuation": {"mode": "resume" if previous else "fresh",
                             "contract": deepcopy(resolved), "previous": deepcopy(previous)},
            "review": {"images": images, "configuration_sha256": configuration_sha,
                "configuration_file_sha256": config_sha, "trainer_arguments": arguments,
                "resources": {"base": base, "vae": vae, "tokenizers": tokenizer_rows,
                              "tokenizer_binding_sha256": tokenizer_binding_sha},
                "output": {"relative_path": output_relative, "collision": "reserved"},
                "estimates": {"optimizer_steps": settings["max_train_steps"],
                              "epochs": final_epoch, "checkpoints": saves},
                "sampling": {"mode": "none"},
                "checkpoint": {"save_every_n_steps": settings["save_every_n_steps"],
                    "resume_mode": "explicit_anchored_checkpoint" if previous else "fresh"},
                "privacy": {"compute": "local", "connectivity": "normal", "data_transfer": "none"}},
            "result_binding": result_binding}
        raw = _bytes(contract)
        receipt_hashes["execution_contract"] = _write(contract_path, raw)
        inputs.append(ProcessInput.capture(contract_path, should_cancel=should_cancel))
        plan = TrainingProcessPlan(_hash(raw + str(run_root).encode("utf-8") + run_name.encode("utf-8"))[:32],
            str(run_root), _directory(run_root), run_name, tuple(command), tuple(inputs), _hash(raw),
            timeout_seconds=timeout_seconds, log_limit=log_limit, schema_version=3,
            inventories=inventories, environment_profile=LOCAL_ENVIRONMENT)
        execution_plan.validate_worker_contract(contract, plan)
        execution_plan.validate_execution_contract(contract, plan)
        _cancel(should_cancel)
        return TrainingExecutionPreparation(plan, contract, preparation, dict(sorted(receipt_hashes.items())))
