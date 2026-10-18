"""Compose verified selected inputs into one production SDXL execution selection."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path

from . import training_continuation as prerequisite_contract
from . import training_continuation_execution as continuation
from . import training_execution_preparation as preparation
from . import training_execution_readiness as readiness
from .training_adapter_preview import SdxlLoraRecipe
from .training_configuration import TrainingConfiguration
from .training_dataset_preparation import binding as package_binding
from .training_execution_runtime import dataset_identity
from .training_file_inventory import SoftwareInventoryService
from .training_local_components import LocalComponentSelection
from .training_package import MANIFEST_NAME, TrainingPackageService, TrainingPackageVerification
from .training_process import LOCAL_ENVIRONMENT, _bytes, _read
from .training_runtime_contract import ADAPTER_ID, ADAPTER_VERSION, SOURCE_REVISION


MAX_SOURCE = 2 * 1024**2
MAX_CATALOG = 2 * 1024**2


class TrainingExecutionSelectionError(ValueError):
    """Stable failure without selected paths or content in the message."""


@dataclass(frozen=True)
class PreparedTrainingSource:
    original: Path
    catalog: Path
    patched: Path


@dataclass(frozen=True)
class TrainingExecutionSelection:
    """Immutable canonical result whose mapping properties return fresh copies."""
    _admission: bytes = field(repr=False)
    _resolved: bytes = field(repr=False)
    _device: bytes = field(repr=False)
    _schedule: bytes = field(repr=False)

    @staticmethod
    def _value(raw):
        return json.loads(raw)

    @property
    def admission(self):
        return self._value(self._admission)

    @property
    def resolved(self):
        return self._value(self._resolved)

    @property
    def device_observation(self):
        return self._value(self._device)

    @property
    def schedule(self):
        return self._value(self._schedule)


def _cancel(callback):
    if callback and callback():
        raise InterruptedError("training_execution_selection_cancelled")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_field")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("nonfinite_number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _source(value, should_cancel):
    if type(value) is not PreparedTrainingSource:
        raise TrainingExecutionSelectionError("training_execution_source_invalid")
    paths = tuple(Path(item).absolute() for item in (value.original, value.catalog, value.patched))
    if any(not path.is_file() for path in paths) or len(set(paths)) != 3:
        raise TrainingExecutionSelectionError("training_execution_source_unavailable")
    _cancel(should_cancel)
    original = _read(paths[0], limit=MAX_SOURCE)
    _cancel(should_cancel)
    catalog_raw = _read(paths[1], limit=MAX_CATALOG)
    try:
        catalog = _strict(catalog_raw)
    except (ValueError, TypeError, UnicodeError):
        raise TrainingExecutionSelectionError("training_execution_source_catalog_invalid") from None
    _cancel(should_cancel)
    patched = _read(paths[2], limit=MAX_SOURCE)
    try:
        return continuation.source_identity(original, catalog, patched)
    except (ValueError, RuntimeError, UnicodeError):
        raise TrainingExecutionSelectionError("training_execution_source_changed") from None


def _native_receipt(value, should_cancel):
    if (type(value) is not dict or set(value) != {"path", "sha256"}
            or type(value["path"]) is not str or not Path(value["path"]).is_absolute()
            or type(value["sha256"]) is not str):
        raise TrainingExecutionSelectionError("training_execution_native_receipt_invalid")
    _cancel(should_cancel)
    raw = _read(Path(value["path"]), limit=readiness.MAX_RECEIPT)
    if _hash(raw) != value["sha256"]:
        raise TrainingExecutionSelectionError("training_execution_native_receipt_changed")
    try:
        receipt = readiness.strict(raw)
    except (ValueError, TypeError, UnicodeError):
        raise TrainingExecutionSelectionError("training_execution_native_receipt_invalid") from None
    fields = {"schema_version", "kind", "plan", "request", "native", "native_sha256",
              "process_journal_tip", "input_verification_sha256"}
    if (type(receipt) is not dict or set(receipt) != fields
            or receipt["schema_version"] != 1 or receipt["kind"] != readiness.RECEIPT_KIND
            or type(receipt["native_sha256"]) is not str
            or _hash(_bytes(receipt["native"])) != receipt["native_sha256"]):
        raise TrainingExecutionSelectionError("training_execution_native_receipt_invalid")
    try:
        readiness.validate_request(receipt["request"])
    except (ValueError, TypeError, KeyError):
        raise TrainingExecutionSelectionError("training_execution_native_request_invalid") from None
    return deepcopy(value), raw, receipt


def _inventories(request, should_cancel, progress):
    rows = request["process"]["inventories"]
    expected = ["source_inventory", "environment_inventory", "python_base_inventory"]
    if [row.get("purpose") for row in rows] != expected:
        raise TrainingExecutionSelectionError("training_execution_native_inventories_invalid")
    result = {}
    service = SoftwareInventoryService()
    for row in rows:
        _cancel(should_cancel)
        try:
            summary = service.verify(row["root"], row["manifest_path"],
                                     should_cancel=should_cancel, progress=progress)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            raise TrainingExecutionSelectionError("training_execution_native_inventory_changed") from None
        if (summary["inventory_sha256"] != row["inventory_sha256"]
                or summary["files"] != row["file_count"] or summary["bytes"] != row["byte_count"]
                or summary["scope"] != "explicit_software_directory"):
            raise TrainingExecutionSelectionError("training_execution_native_inventory_changed")
        result[row["purpose"].removesuffix("_inventory")] = summary
    return result


def _interval(maximum, requested):
    if requested is None:
        saves = max(count for count in range(1, min(5, maximum) + 1) if maximum % count == 0)
        return maximum // saves, saves
    if (type(requested) is not int or not 1 <= requested <= maximum
            or maximum % requested or maximum // requested > continuation.MAX_SAVES):
        raise TrainingExecutionSelectionError("training_execution_checkpoint_interval_invalid")
    return requested, maximum // requested


class TrainingExecutionSelectionService:
    """Recheck selected facts and construct the only supported execution records."""

    def select(self, package, registry, recipe, components, native_receipt, prepared_source, *,
               expected_registry, save_every_n_steps=None, resume_contract=None,
               should_cancel=None, progress=None):
        _cancel(should_cancel)
        if not isinstance(package, TrainingPackageVerification) or not package.valid:
            raise TrainingExecutionSelectionError("training_execution_package_invalid")
        fresh = TrainingPackageService().verify(package.destination, should_cancel=should_cancel)
        if (not fresh.valid or fresh.package_fingerprint != package.package_fingerprint
                or fresh.manifest != package.manifest):
            raise TrainingExecutionSelectionError("training_execution_package_changed")
        if type(recipe) is not SdxlLoraRecipe:
            raise TrainingExecutionSelectionError("training_execution_recipe_invalid")
        recipe.validate()
        if type(components) is not LocalComponentSelection or components.vae is not None:
            raise TrainingExecutionSelectionError("training_execution_components_unsupported")
        base, vae = preparation._selected_resources(
            registry, components, expected_registry, should_cancel, progress)
        if vae is not None:
            raise TrainingExecutionSelectionError("training_execution_components_unsupported")
        native_reference, native_raw, evidence = _native_receipt(native_receipt, should_cancel)
        request = evidence["request"]
        selection = request["selection"]
        token_root = Path(components.tokenizer_1).parent
        if (Path(components.tokenizer_1) != token_root / "tokenizer"
                or Path(components.tokenizer_2) != token_root / "tokenizer_2"
                or selection["tokenizers_root"] != str(token_root)
                or selection["base_id"] != base["id"]):
            raise TrainingExecutionSelectionError("training_execution_native_selection_changed")
        configuration = preparation._runtime_configuration(fresh.manifest, recipe, base["path"])
        if selection["configuration"] != configuration:
            raise TrainingExecutionSelectionError("training_execution_native_configuration_changed")
        qualification = selection["qualification"]
        if (request["source_revision"] != SOURCE_REVISION
                or qualification["runtime_profile"] != prerequisite_contract.profile_contract()["qualification"]["runtime_profile"]):
            raise TrainingExecutionSelectionError("training_execution_native_qualification_changed")
        inventories = _inventories(request, should_cancel, progress)
        manifest_raw = _read(fresh.destination / MANIFEST_NAME,
                             limit=preparation.execution_plan.MAX_PACKAGE_MANIFEST)
        manifest_sha = _hash(manifest_raw)
        package_selection = {"root": str(fresh.destination),
            "package_fingerprint": fresh.package_fingerprint, "manifest_sha256": manifest_sha,
            "recipe": asdict(recipe)}
        package_contract = package_binding(package_selection)
        tokenizer_identities = [{"role": role, "identity": identity}
                                for role, identity in enumerate(selection["tokenizer_identities"], 1)]
        component_binding = preparation.reference_component_binding(base["id"], tokenizer_identities)
        tokenizer_binding = preparation.reference_tokenizer_binding(tokenizer_identities)
        package_configuration = TrainingConfiguration.from_dict(fresh.manifest["training_configuration"])
        if (package_configuration.target_model_family.casefold() != "sdxl"
                or package_configuration.base_model_id != base["id"]
                or package_configuration.output_kind != "lora"):
            raise TrainingExecutionSelectionError("training_execution_package_target_changed")
        # Checkpoint receipts retain the original selection provenance. Current
        # resource admission above and launch/registration revisions stay current.
        historical_revision = expected_registry
        if resume_contract is not None:
            try:
                continuation.validate_contract(resume_contract)
                historical_revision = resume_contract["prerequisite"]["selection"]["registry_revision"]
            except (ValueError, RuntimeError, TypeError, KeyError):
                raise TrainingExecutionSelectionError("training_execution_resume_contract_invalid") from None
        prerequisite = {"schema_version": 1, "profile": prerequisite_contract.PROFILE,
            "continuation_profile": prerequisite_contract.profile_contract(),
            "selection": {"adapter_id": ADAPTER_ID, "adapter_version": ADAPTER_VERSION,
                "source_revision": SOURCE_REVISION, "runtime_profile": qualification["runtime_profile"],
                "package": package_contract,
                "configuration_sha256": _hash(_bytes(fresh.manifest["training_configuration"])),
                "component_binding_sha256": component_binding, "registry_revision": historical_revision,
                "base_id": base["id"], "local_vae_override_id": None,
                "caption_limit": package_configuration.max_caption_tokens},
            "runtime": {"inspection_sha256": evidence["native_sha256"],
                "software_inventories": {name: deepcopy(inventories[name])
                                         for name in ("environment", "python_base")},
                "environment_profile": LOCAL_ENVIRONMENT}}
        try:
            prerequisite_contract.validate_contract(prerequisite)
        except (ValueError, RuntimeError, TypeError, KeyError):
            raise TrainingExecutionSelectionError("training_execution_prerequisite_invalid") from None
        epoch_batches = preparation._epoch_batches(fresh.manifest)
        accumulation = package_configuration.gradient_accumulation
        maximum = package_configuration.epochs * math.ceil(epoch_batches / accumulation)
        interval, saves = _interval(maximum, save_every_n_steps)
        settings = {**continuation.FIXED, "network_dim": recipe.rank,
            "network_alpha": float(recipe.alpha), "learning_rate": package_configuration.learning_rate,
            "mixed_precision": {"float32": "no"}.get(package_configuration.precision,
                                                       package_configuration.precision),
            "gradient_checkpointing": recipe.gradient_checkpointing, "max_train_steps": maximum,
            "gradient_accumulation_steps": accumulation, "seed": package_configuration.seed,
            "save_every_n_steps": interval}
        source = _source(prepared_source, should_cancel)
        source_root = Path(request["process"]["inventories"][0]["root"])
        if not Path(prepared_source.original).absolute().is_relative_to(source_root):
            raise TrainingExecutionSelectionError("training_execution_source_scope_changed")
        resolved = continuation.resolved_contract(prerequisite, settings, epoch_batches,
            dataset_identity(fresh.destination, fresh.manifest), source)
        if resume_contract is not None:
            try:
                continuation.validate_contract(resume_contract)
            except (ValueError, RuntimeError, TypeError, KeyError):
                raise TrainingExecutionSelectionError("training_execution_resume_contract_invalid") from None
            if _bytes(resume_contract) != _bytes(resolved):
                raise TrainingExecutionSelectionError("training_execution_resume_contract_changed")
        device = deepcopy(evidence["native"]["device_observation"])
        admission = {"schema_version": 1, "kind": "sdxl_reference_execution_admission",
            "qualification_profile": qualification["profile"],
            "qualification_evidence_sha256": qualification["evidence_sha256"],
            "runtime_profile": qualification["runtime_profile"], "source_revision": SOURCE_REVISION,
            "base_id": base["id"], "component_binding_sha256": component_binding,
            "tokenizer_binding_sha256": tokenizer_binding, "tokenizer_identities": tokenizer_identities,
            "software_inventories": {name: inventories[name]["inventory_sha256"]
                                     for name in ("source", "environment", "python_base")},
            "device_observation_sha256": readiness.digest(device),
            "continuation_contract_sha256": continuation.digest(resolved),
            "native_readiness_receipt": native_reference}
        try:
            readiness.validate_reference_admission(admission, native_raw)
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            raise TrainingExecutionSelectionError("training_execution_reference_admission_invalid") from None
        _cancel(should_cancel)
        summary = {"mode": "resume" if resume_contract is not None else "fresh",
            "optimizer_steps": maximum, "epochs": package_configuration.epochs,
            "epoch_batches": epoch_batches, "save_every_n_steps": interval, "checkpoints": saves}
        return TrainingExecutionSelection(_bytes(admission), _bytes(resolved), _bytes(device), _bytes(summary))
