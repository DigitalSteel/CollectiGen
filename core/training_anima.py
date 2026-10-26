"""Separate, explicitly qualified Anima Base v1.0 DiT-only LoRA adapter.

This module never discovers models, downloads resources, or widens the SDXL
adapter. Trials use an explicit separate entry point; ordinary launch remains
held until a retained qualification receipt is reviewed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct
from uuid import uuid4

from .model_resource_registry import ModelResourceRegistry, inspect_model_resource
from .training_file_inventory import SoftwareInventoryService
from .training_package import MANIFEST_NAME, TrainingPackageService
from .training_process import (LOCAL_ENVIRONMENT, ProcessInput, ProcessInventory, TrainingProcessPlan, TrainingRunJournal,
    TrainingProcessResult, TrainingProcessService, _bytes, _read)
from .training_result import _process_evidence
from .training_runtime_qualification import TrainingRuntimeQualificationService
from .training_source_io import _assert_unredirected


SOURCE_REVISION = "6721028c79ee85a78b3a06dfd8954dae310a1cce"
SOURCE_SCRIPT_SHA256 = "0886548c19cd9fc80410cd0c625c99f7e63250dfb4624c2b353c7646f704e45d"
BASE_ID = "sha256:bd43b7cffe1ed1153d9c41e7beb2f18cb1273eafbaa3af3edd6a173dc90a006e"
QWEN_ID = "sha256:cd2a512003e2f9f3cd3c32a9c3573f820bb28c940f73c57b1ddaa983d9223eba"
VAE_ID = "sha256:a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f"
RESOURCE_PROFILE_ID = "circlestone-labs/anima-base-v1.0"
RESOURCE_ADAPTER_ID = "sd-scripts.anima_train_network"
CONFIG_SHA256 = {
    "configs/qwen3_06b/config.json": "19a7f4d1b0d23df7c0ce5d1b8d77b1698678910e17f2b2fa6dd430ac4931c3c7",
    "configs/qwen3_06b/merges.txt": "b8e04af9c950e393054010dcef499c3f005e803e9b2da1a09b96a26c33266eb1",
    "configs/qwen3_06b/tokenizer_config.json": "7992a7924330571ac9b97d58e39d4a4993ccdb865335034cec29cf2c482fd460",
    "configs/qwen3_06b/tokenizer.json": "47ec9be242d3ef39b9c97ac0a3f06c1752f061b234e295bc0a2842067a3fe4f9",
    "configs/qwen3_06b/vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    "configs/t5_old/config.json": "f59550f9095104a1d970031bf7ae640b92866fbb19cac7bd0e1ce4d09d32d776",
    "configs/t5_old/spiece.model": "d60acb128cf7b7f2536e8f38a5b18a05535c9e14c7a355904270e15b0945ea86",
    "configs/t5_old/tokenizer.json": "d2acde0d8d71dd30a711834b07781b9c89feaac33fd332f60507699282740066",
}
ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "tools/training_runtime_sources.json"
WORKER = Path(__file__).with_name("training_anima_worker.py")
OUTPUT_NAME = "anima-lora.safetensors"
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
DEFAULT_QUALIFICATION = ROOT / "tools/training_anima_qualification.json"
DEFAULT_NATIVE_EVIDENCE = ROOT / "tools/training_anima_native_evidence.json"
QUALIFICATION_CODE = {
    "adapter": Path(__file__),
    "worker": WORKER,
    "process": Path(__file__).with_name("training_process.py"),
    "result": Path(__file__).with_name("training_result.py"),
    "runtime": Path(__file__).with_name("training_runtime_qualification.py"),
    "inventory": Path(__file__).with_name("training_file_inventory.py"),
    "package": Path(__file__).with_name("training_package.py"),
    "source_io": Path(__file__).with_name("training_source_io.py"),
    "registry": Path(__file__).with_name("model_resource_registry.py"),
    "execution_cache": Path(__file__).with_name("model_execution_cache.py"),
    "cache": Path(__file__).with_name("model_cache.py"),
    "use_leases": Path(__file__).with_name("model_use_leases.py"),
    "cache_capacity": Path(__file__).with_name("model_cache_capacity.py"),
}
SHA = re.compile(r"[0-9a-f]{64}\Z")


class AnimaError(ValueError):
    """Fail closed without echoing selected private paths or captions."""


def _check_cancel(callback):
    if callback is not None and callback():
        raise InterruptedError("Anima preparation was cancelled.")


def _digest(value):
    return sha256(_bytes(value)).hexdigest()


def anima_optimizer_steps(count: int, repeats: int, epochs: int,
                          batch_size: int, accumulation: int) -> tuple[int, int]:
    """The pinned single-process, batch-one trainer flushes accumulation each epoch."""
    if (any(type(value) is not int or value < 1
            for value in (count, repeats, epochs, batch_size, accumulation))
            or batch_size != 1):
        raise AnimaError("Anima epoch accounting requires positive batch-one settings.")
    batches_per_epoch = count * repeats
    steps_per_epoch = (batches_per_epoch + accumulation - 1) // accumulation
    return steps_per_epoch, epochs * steps_per_epoch


@dataclass(frozen=True)
class AnimaSettings:
    max_steps: int = 2
    resolution: int = 256
    rank: int = 4
    seed: int = 42
    learning_rate: float = 2e-5
    timeout_seconds: int = 1200
    batch_size: int = 1
    gradient_accumulation: int = 1
    repeats: int = 1

    def validate(self):
        if (type(self.max_steps) is not int or not 1 <= self.max_steps <= 2000
                or type(self.resolution) is not int or self.resolution not in {256, 384, 512}
                or type(self.rank) is not int or self.rank not in {4, 8}
                or type(self.seed) is not int or not 0 <= self.seed <= 4294967295
                or type(self.learning_rate) is not float or not 0 < self.learning_rate <= 1e-4
                or type(self.timeout_seconds) is not int or not 60 <= self.timeout_seconds <= 28800
                or type(self.batch_size) is not int or self.batch_size != 1
                or type(self.gradient_accumulation) is not int or not 1 <= self.gradient_accumulation <= 8
                or type(self.repeats) is not int or not 1 <= self.repeats <= 16):
            raise AnimaError("Anima settings exceed the bounded DiT-only profile.")

    def validate_trial(self):
        self.validate()
        if not 2 <= self.max_steps <= 20 or self.timeout_seconds > 1200:
            raise AnimaError("Anima qualification trials require 2–20 steps within 20 minutes.")

    def payload(self):
        self.validate()
        return vars(self).copy()


@dataclass(frozen=True)
class AnimaSelection:
    package_path: Path
    source_path: Path
    environment_root: Path
    python_base_root: Path
    environment_inventory: Path
    python_base_inventory: Path
    base_id: str = BASE_ID
    qwen_id: str = QWEN_ID
    vae_id: str = VAE_ID
    resource_bindings: tuple[tuple[str, str], ...] = ()
    execution_id: str = ""
    execution_receipt: str = ""


@dataclass(frozen=True)
class AnimaCacheRow:
    resource_id: str
    source: Path
    destination: Path | None
    status: str
    policy: str
    bytes: int


@dataclass(frozen=True)
class AnimaCacheReview:
    selection: AnimaSelection
    settings: AnimaSettings
    mode: str
    ready: bool
    message: str
    rows: tuple[AnimaCacheRow, ...]
    new_bytes: int
    registry_token: str
    review_id: str
    capacity: object | None = None


@dataclass(frozen=True)
class AnimaResourcePreparation:
    selection: AnimaSelection
    settings: AnimaSettings
    status: str
    message: str
    receipt_path: Path
    operation_id: str = ""
    phase: str = ""
    review_path: Path | None = None


@dataclass(frozen=True)
class AnimaPreview:
    selection: AnimaSelection
    settings: AnimaSettings
    ready: bool
    mode: str
    issues: tuple[str, ...]
    fingerprint: str
    package_fingerprint: str = ""
    manifest_sha256: str = ""
    runtime_fingerprint: str = ""
    resource_paths: tuple[str, str, str] = ()


@dataclass(frozen=True)
class AnimaPrepared:
    preview: AnimaPreview
    plan: TrainingProcessPlan
    contract_path: Path
    preparation_root: Path


@dataclass(frozen=True)
class AnimaVerifiedResult:
    prepared: AnimaPrepared
    outcome: TrainingProcessResult
    output_id: str
    output_path: Path
    journal_tip: str
    worker_receipt_sha256: str
    tensor_count: int
    quality: str = "unmeasured"


def _source_entry(path: Path, expected: str, should_cancel=None):
    selected = ProcessInput.capture(path, should_cancel)
    if selected.sha256 != expected:
        raise AnimaError("The pinned Anima trainer source or tokenizer config changed.")
    return selected


def _source_entries(source: Path, should_cancel=None):
    _source_entry(source / "anima_train_network.py", SOURCE_SCRIPT_SHA256, should_cancel)
    for relative, expected in CONFIG_SHA256.items():
        _check_cancel(should_cancel)
        _source_entry(source / relative, expected, should_cancel)


def _embedded_llm_adapter(path: Path) -> bool:
    """Check the single pinned DiT's safetensors header, without loading weights."""
    _assert_unredirected(path)
    with path.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            return False
        size = struct.unpack("<Q", prefix)[0]
        if not 2 <= size <= 16 * 1024 * 1024:
            return False
        raw = handle.read(size)
    try:
        header = json.loads(raw)
    except (ValueError, UnicodeError):
        return False
    return (type(header) is dict and
        sum(key.startswith("net.llm_adapter.blocks.") for key in header) >= 100)


@dataclass(frozen=True)
class AnimaResourceRequirement:
    slot: str
    role: str
    family: str
    resource_id: str


@dataclass(frozen=True)
class AnimaResourceContract:
    profile_id: str
    adapter_id: str
    adapter_version: str
    source_script_sha256: str
    requirements: tuple[AnimaResourceRequirement, ...]


def anima_resource_contract(*, workflow_kind, profile_id, adapter_id, adapter_version, main_resource_id):
    """Exact resource-only TRAINING contract; no package, runtime or model I/O.

    This does not admit a training run, qualify hardware or describe arbitrary
    Anima checkpoints or generation. The existing execution gates are separate.
    """
    if (workflow_kind, profile_id, adapter_id, adapter_version, main_resource_id) != (
            "training", RESOURCE_PROFILE_ID, RESOURCE_ADAPTER_ID, SOURCE_REVISION, BASE_ID):
        raise AnimaError("This selection has no exact pinned Anima training resource contract.")
    return AnimaResourceContract(RESOURCE_PROFILE_ID, RESOURCE_ADAPTER_ID, SOURCE_REVISION,
        SOURCE_SCRIPT_SHA256, (AnimaResourceRequirement("main", "unet", "Anima", BASE_ID),
            AnimaResourceRequirement("text_encoder", "text_encoder", "Qwen3", QWEN_ID),
            AnimaResourceRequirement("vae", "vae", "Qwen-Image", VAE_ID)))


def verify_anima_resource_structure(contract, main_inspection, *, protected_roots=(), should_cancel=None):
    """Check embedded structure after the caller's fresh exact file verification.

    This structural check alone is not a complete resource-set qualification.
    It neither resolves other locations nor inspects a Dataset/training package.
    """
    from .model_resource_registry import ResourceInspection, _current
    from .model_workspace import _path, _pinned_model_file
    expected = anima_resource_contract(workflow_kind="training", profile_id=RESOURCE_PROFILE_ID,
        adapter_id=RESOURCE_ADAPTER_ID, adapter_version=SOURCE_REVISION, main_resource_id=BASE_ID)
    if contract != expected or not isinstance(main_inspection, ResourceInspection) or main_inspection.resource_id != BASE_ID:
        raise AnimaError("The pinned Anima resource contract or verified base identity changed.")
    _check_cancel(should_cancel)
    path = _path(main_inspection.path, protected_roots)
    with _pinned_model_file(path, protected_roots):
        _current(main_inspection)
        if not _embedded_llm_adapter(path):
            raise AnimaError("The exact Anima base lacks its required embedded LLM adapter structure.")
        _check_cancel(should_cancel)
        _current(main_inspection)


def _qualification_binding(runtime_fingerprint: str) -> dict:
    return {"source_revision": SOURCE_REVISION, "source_script_sha256": SOURCE_SCRIPT_SHA256,
        "base_id": BASE_ID, "qwen_id": QWEN_ID, "vae_id": VAE_ID,
        "runtime_fingerprint": runtime_fingerprint}


def _qualification_code_hashes(should_cancel=None) -> dict[str, str]:
    return {name: ProcessInput.capture(path, should_cancel).sha256
        for name, path in QUALIFICATION_CODE.items()}


class AnimaTrainingService:
    """No ambient resource discovery; all roots, records and jobs are injected."""

    def __init__(self, registry: ModelResourceRegistry, *, process=None, packages=None,
                 protected_roots=(), managed_roots=(), usage_service=None):
        self.registry = registry
        self.process = process or TrainingProcessService()
        self.packages = packages or TrainingPackageService()
        self._resource_transitions = {}
        from .model_execution_cache import ModelExecutionCacheService
        self._execution_cache = ModelExecutionCacheService(registry,
            owner_id=RESOURCE_ADAPTER_ID, owner_version=SOURCE_REVISION,
            protected_roots=protected_roots, managed_roots=managed_roots,
            transition_verifier=self._verify_resource_transition)
        self._cache_reviews = {}
        self._usage_proofs = {}
        if usage_service is None:
            from .model_usage import ModelUsageService
            usage_service = ModelUsageService(registry, admission_verifier=self.admission_facts,
                success_verifier=self.verified_use_attestation, protected_roots=protected_roots)
        self.usage = usage_service

    def _verify_resource_transition(self, execution_id, record, proof):
        """Bounded callback under the cache lock: no registry/runtime/file I/O."""
        issued = self._resource_transitions.get(proof) if isinstance(proof, str) else None
        if issued and issued[:2] == (execution_id, _digest(record)):
            return issued[2]
        return None

    def _update_resources(self, path, record, changes):
        proof = uuid4().hex
        receipt, current = self._execution_cache.read(path)
        if current != record:
            raise AnimaError("Execution ownership changed before its next consumer transition.")
        self._resource_transitions[proof] = (receipt["execution_id"], _digest(record), changes)
        try:
            return self._execution_cache.update(path, record, proof)
        finally:
            self._resource_transitions.pop(proof, None)

    @staticmethod
    def _resource_owner_fingerprint(selection, settings):
        selected = {k: str(v) if isinstance(v, Path) else v for k, v in
            asdict(replace(selection, resource_bindings=(), execution_id="", execution_receipt="")).items()}
        return _digest({"selection": selected, "settings": settings.payload(),
            "owner": RESOURCE_ADAPTER_ID, "version": SOURCE_REVISION})

    def cache_review(self, selection, settings=AnimaSettings(), *, should_cancel=None, progress=None):
        """No-copy exact source/space/policy review; runtime admission is separate."""
        from .model_cache import ModelCacheService
        from .model_companion_readiness import CompanionChoice, CompanionReadinessRequest, CompanionReadinessService
        from .model_storage_policy import load_policy, effective_policy
        settings.validate()
        if (selection.base_id, selection.qwen_id, selection.vae_id) != (BASE_ID, QWEN_ID, VAE_ID):
            raise AnimaError("Only the exact pinned Anima training resource set is admitted.")
        value, token = self.registry.snapshot()
        policy = load_policy(value.get("workspace", {}))
        contract = anima_resource_contract(workflow_kind="training", profile_id=RESOURCE_PROFILE_ID,
            adapter_id=RESOURCE_ADAPTER_ID, adapter_version=SOURCE_REVISION, main_resource_id=BASE_ID)
        choices = []
        for row in contract.requirements:
            locations = value.get("resources", {}).get(row.resource_id, {}).get("locations", [])
            if policy.mode == "tiered":
                locations = [loc for loc in locations if not loc.get("cache_entry_id")
                    and Path(policy.fast_root) not in Path(loc["path"]).parents]
            if not locations:
                raise AnimaError("Locate the exact registered archive original for every Anima input.")
            choices.append(CompanionChoice(row.slot, row.resource_id, locations[0]["path"], row.role, row.family))
        companions = CompanionReadinessService(self.registry,
            protected_roots=self._execution_cache.protected_roots, managed_roots=self._execution_cache.managed_roots)
        report = companions.verify(CompanionReadinessRequest(BASE_ID, "training", RESOURCE_PROFILE_ID,
            RESOURCE_ADAPTER_ID, SOURCE_REVISION, tuple(choices)), expected_registry=token,
            should_cancel=should_cancel, progress=progress)
        requirements = companions.requirements(report)
        if requirements.qualification != "adapter_qualified":
            raise AnimaError("Verify every exact Anima companion before preparation.")
        resolver = companions.resolver(should_cancel=should_cancel, progress=progress)
        cache_preview = None
        rows = []
        if policy.mode == "tiered":
            cache = ModelCacheService(self.registry, requirement_resolver=resolver,
                protected_roots=self._execution_cache.protected_roots, managed_roots=self._execution_cache.managed_roots)
            cache_preview = cache.preview_prepare(requirements, expected_registry=token,
                should_cancel=should_cancel, progress=progress)
            for row in cache_preview.rows:
                applied = effective_policy(policy, resource_id=row.resource.resource_id,
                    role=row.resource.role, family=row.resource.family)
                rows.append(AnimaCacheRow(row.resource.resource_id, row.resource.archive_path,
                    row.destination, row.status, applied.policy, row.inspection.size if row.inspection else 0))
        else:
            for row in requirements.resources:
                rows.append(AnimaCacheRow(row.resource_id, row.archive_path, row.archive_path,
                    "in_place", "single", value["resources"][row.resource_id]["identity"]["size"]))
        ready = all(row.status in {"in_place", "copy", "cached"} for row in rows) and (cache_preview is None or cache_preview.can_prepare)
        review = AnimaCacheReview(selection, settings, policy.mode, ready,
            "Exact resources will be protected through verification and registration." if ready else
            "Resource preparation is held. Review storage policy or use Models > Prepare for Use.",
            tuple(rows), cache_preview.new_bytes if cache_preview else 0, token, uuid4().hex,
            cache_preview.capacity if cache_preview else None)
        if len(self._cache_reviews) >= 32:
            self._cache_reviews.pop(next(iter(self._cache_reviews)))
        self._cache_reviews[review.review_id] = (review, requirements, cache_preview, resolver)
        return review

    def prepare_resources(self, review, receipt_root, *, should_cancel=None, progress=None):
        issued = self._cache_reviews.get(getattr(review, "review_id", ""))
        if not issued or issued[0] != review or not review.ready:
            raise AnimaError("Review the exact Anima resource preparation first.")
        if self.registry.snapshot()[1] != review.registry_token:
            raise AnimaError("Model settings changed; review preparation again.")
        _, requirements, cache_preview, resolver = issued
        selected = review.selection
        if Path(receipt_root) != selected.environment_inventory.parent:
            raise AnimaError("Keep the resource receipt with this exact Anima selection.")
        context = {"selection": {k: str(getattr(selected, k)) for k in (
            "package_path", "source_path", "environment_root", "python_base_root",
            "environment_inventory", "python_base_inventory")}, "settings": review.settings.payload()}
        self._execution_cache.prepare(receipt_root=receipt_root,
            owner_fingerprint=self._resource_owner_fingerprint(selected, review.settings), owner_context=context,
            requirements=requirements, expected_registry=review.registry_token,
            cache_preview=cache_preview, requirement_resolver=resolver,
            should_cancel=should_cancel, progress=progress)
        del self._cache_reviews[review.review_id]
        return self.open_resources(Path(receipt_root) / "anima-resources.json")

    def open_resources(self, receipt_path, *, should_cancel=None):
        _check_cancel(should_cancel)
        receipt, record = self._execution_cache.read(receipt_path)
        try:
            context = receipt["owner_context"]
            selection = AnimaSelection(**{k: Path(v) for k, v in context["selection"].items()},
                base_id=BASE_ID, qwen_id=QWEN_ID, vae_id=VAE_ID)
            settings = AnimaSettings(**context["settings"])
            settings.validate()
            if (self._resource_owner_fingerprint(selection, settings) != receipt["owner_fingerprint"]
                    or Path(receipt_path).parent != selection.environment_inventory.parent):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise AnimaError("The retained Anima resource selection changed.") from None
        status = self._execution_cache.status(receipt_path)
        if record:
            selection = replace(selection, resource_bindings=status.bindings,
                execution_id=status.execution_id, execution_receipt=str(receipt_path))
        review_path = None
        if record and record.get("review_path"):
            from .model_workspace import _path
            review_path = _path(record["review_path"], self._execution_cache.protected_roots)
            retained, _, _ = self.open_review(review_path)
            if (retained.preview.selection != selection or retained.preview.settings != settings
                    or retained.plan.fingerprint != record.get("plan_fingerprint")):
                raise AnimaError("The retained run differs from its resource preparation owner.")
        return AnimaResourcePreparation(selection, settings, status.status, status.message,
            Path(receipt_path), status.operation_id, record.get("phase", "") if record else "",
            review_path)

    def recover_resources(self, receipt_path, *, should_cancel=None, progress=None):
        self._execution_cache.recover(receipt_path, should_cancel=should_cancel, progress=progress)
        return self.open_resources(receipt_path, should_cancel=should_cancel)

    def _resource_record(self, selection, settings):
        receipt, record = self._execution_cache.read(selection.execution_receipt)
        if (record is None or record.get("state") != "active"
                or receipt["execution_id"] != selection.execution_id
                or record["owner_fingerprint"] != self._resource_owner_fingerprint(selection, settings)
                or tuple(tuple(v) for v in record["bindings"]) != selection.resource_bindings):
            raise AnimaError("This execution preparation is changed, discarded, or unavailable.")
        if record["phase"] in {"prepared", "bound"}:
            from .model_cache import _policy_digest
            from .model_storage_policy import load_policy
            value, _ = self.registry.snapshot()
            from .model_use_leases import cache_state
            if record["operation_id"] and cache_state(value)["operations"].get(record["operation_id"], {}).get("phase") != "complete":
                raise AnimaError("Finish recovery of this exact resource preparation before use.")
            if ((receipt["mode"] == "tiered" and record["policy_digest"] != _policy_digest(value))
                    or receipt["mode"] != load_policy(value.get("workspace", {})).mode):
                raise AnimaError("Storage policy changed; review the exact resource preparation.")
        return record

    def cache_discard(self, selected, *, outcome=None):
        """Close all input consumers explicitly; process uncertainty keeps pins."""
        from .model_workspace import _path
        from .model_use_leases import now
        prepared = selected if isinstance(selected, AnimaPrepared) else None
        selection = prepared.preview.selection if prepared else selected if isinstance(selected, AnimaSelection) else None
        path = Path(selection.execution_receipt) if selection and selection.execution_receipt else Path(selected) if selection is None else None
        if path is None:
            return "not_created"
        path = _path(path, self._execution_cache.protected_roots)
        if not path.exists():
            from .model_use_leases import cache_state
            records = cache_state(self.registry.snapshot()[0])["leases"].values()
            if any(row.get("kind") == "execution_preparation" and row.get("state") != "released"
                    and str(row.get("receipt_path", "")).casefold() == str(path).casefold() for row in records):
                return "uncertain"
            return "not_created"
        receipt, record = self._execution_cache.read(path)
        if record is None:
            return self._execution_cache.abandon_unreserved(path)
        if record["state"] == "released":
            return "released"
        if self._execution_cache.status(path).status == "recovery_required":
            return "recovery_required"
        if record["phase"] in {"submitted", "uncertain"}:
            if prepared is None and record.get("review_path"):
                prepared, outcome, _ = self.open_review(record["review_path"])
            if prepared is None or not self._stopped_proof(prepared, outcome):
                return "uncertain"
        self._update_resources(path, record,
            {"state": "released", "phase": "discarded", "released_at": now()})
        return "released"

    @staticmethod
    def _stopped_proof(prepared, outcome):
        if (not isinstance(outcome, TrainingProcessResult) or not outcome.tree_stopped
                or outcome.destination != prepared.plan.destination):
            return False
        rows, tip = TrainingRunJournal(prepared.plan.destination).read()
        return bool(rows and tip == outcome.journal_tip
            and _digest(rows[0]["details"].get("plan")) == _digest(TrainingProcessService._receipt(prepared.plan))
            and rows[-1]["state"] in {"exited_unverified", "failed", "cancelled", "interrupted"}
            and rows[-1]["details"].get("tree_stopped") is True)

    def admission_facts(self, execution_id):
        from .model_usage import UsageAdmissionFacts
        from .model_use_leases import cache_state
        record = cache_state(self.registry.snapshot()[0])["leases"].get(execution_id)
        if (not record or record.get("kind") != "execution_preparation"
                or record.get("owner_id") != RESOURCE_ADAPTER_ID or record.get("owner_version") != SOURCE_REVISION
                or record.get("state") != "active" or not record.get("plan_fingerprint")):
            return None
        receipt, retained = self._execution_cache.read(record["receipt_path"])
        if record != retained:
            return None
        return UsageAdmissionFacts(RESOURCE_ADAPTER_ID, SOURCE_REVISION, execution_id,
            tuple(record["resource_ids"]), record["required_set_digest"])

    def verified_use_attestation(self, proof):
        return self._usage_proofs.get(proof) if isinstance(proof, str) else None

    @staticmethod
    def prepare_selection(package_path, request: dict, receipt_root, *, should_cancel=None) -> AnimaSelection:
        """Capture Setup-selected inventories off-thread; never ask for JSON paths."""
        if type(request) is not dict or not {"source_root", "environment_root",
                "python_base_root", "python", "output_root"} <= request.keys():
            raise AnimaError("Choose a saved or explicit local trainer setup.")
        values = {key: Path(request[key]) for key in ("source_root", "environment_root",
            "python_base_root", "python", "output_root")}
        if (any(not path.is_absolute() for path in values.values())
                or values["python"] != values["environment_root"] / "Scripts/python.exe"):
            raise AnimaError("The selected Anima interpreter does not match its exact environment.")
        package_path = Path(package_path)
        receipt_root = Path(receipt_root)
        if (not package_path.is_absolute() or not receipt_root.is_absolute()
                or receipt_root.parent != values["output_root"]
                or not receipt_root.name.startswith(".anima-selection-")):
            raise AnimaError("The Anima preparation evidence must stay in the selected output root.")
        _assert_unredirected(receipt_root)
        receipt_root.mkdir(exist_ok=False)
        inventory = SoftwareInventoryService()
        manifest_paths = []
        for key in ("environment_root", "python_base_root"):
            _check_cancel(should_cancel)
            selected_root = values[key]
            manifest = receipt_root / (key.replace("_root", "") + "-inventory.json")
            captured = inventory.capture(selected_root, should_cancel=should_cancel)
            inventory.save(captured, manifest)
            inventory.verify(selected_root, manifest, should_cancel=should_cancel)
            manifest_paths.append(manifest)
        return AnimaSelection(package_path, values["source_root"], values["environment_root"],
            values["python_base_root"], *manifest_paths)

    def _preview(self, selection: AnimaSelection, settings: AnimaSettings,
                 *, mode: str, qualification_path=None, should_cancel=None) -> AnimaPreview:
        settings.validate_trial() if mode == "trial" else settings.validate()
        if not isinstance(selection, AnimaSelection):
            raise AnimaError("Select an exact Anima package and resource set.")
        issues = []
        if (selection.base_id, selection.qwen_id, selection.vae_id) != (BASE_ID, QWEN_ID, VAE_ID):
            issues.append("pinned_resource_identity_changed")
        paths = (Path(selection.package_path).absolute(), Path(selection.source_path).absolute(),
            Path(selection.environment_root).absolute(), Path(selection.python_base_root).absolute(),
            Path(selection.environment_inventory).absolute(), Path(selection.python_base_inventory).absolute())
        if any(str(path) != str(original) or not path.is_absolute()
               for path, original in zip(paths, (selection.package_path, selection.source_path,
                    selection.environment_root, selection.python_base_root,
                    selection.environment_inventory, selection.python_base_inventory))):
            issues.append("noncanonical_selection_path")
        package = self.packages.verify(selection.package_path, should_cancel=should_cancel)
        if not package.valid:
            issues.append("package_unavailable_or_changed")
        else:
            config = package.manifest.get("training_configuration", {})
            target = config.get("target", {})
            image = config.get("image_preparation", {})
            if (str(target.get("model_family", "")).casefold() != "anima"
                    or str(target.get("model_version", "")).casefold() not in {"base v1.0", "base-1.0", "1.0"}
                    or target.get("base_model_id") != BASE_ID
                    or config.get("expected_output", {}).get("kind") != "lora"
                    or image.get("target_resolution") != settings.resolution):
                issues.append("package_anima_base_profile_mismatch")
            weighting = config.get("weighting", {})
            optimization = config.get("optimization_request", {})
            count = package.manifest.get("counts", {}).get("training")
            if (type(weighting) is not dict or type(optimization) is not dict
                    or type(count) is not int or count < 1):
                issues.append("package_training_settings_unavailable")
            elif (weighting.get("repeats") != settings.repeats
                    or optimization.get("batch_size") != settings.batch_size
                    or optimization.get("gradient_accumulation") != settings.gradient_accumulation
                    or optimization.get("seed") != settings.seed
                    or optimization.get("learning_rate") != settings.learning_rate):
                issues.append("package_training_settings_mismatch")
            elif type(weighting.get("epochs")) is not int or weighting["epochs"] < 1:
                issues.append("package_training_settings_unavailable")
            elif mode == "normal" and settings.max_steps != anima_optimizer_steps(
                    count, settings.repeats, weighting["epochs"],
                    settings.batch_size, settings.gradient_accumulation)[1]:
                issues.append("package_optimizer_steps_mismatch")
        runtime = None
        try:
            runtime = TrainingRuntimeQualificationService().inspect(selection.source_path,
                selection.environment_root, profile="kohya-uv-45088f04",
                should_cancel=should_cancel)
            if not runtime.probe_ready:
                issues.append("trainer_runtime_prerequisites_unavailable")
            _source_entries(Path(selection.source_path), should_cancel)
            for root, inventory in ((selection.environment_root, selection.environment_inventory),
                                    (selection.python_base_root, selection.python_base_inventory)):
                _check_cancel(should_cancel)
                SoftwareInventoryService().verify(root, inventory, should_cancel=should_cancel)
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError):
            issues.append("trainer_source_or_runtime_changed")
        resolved = []
        bindings = {}
        if selection.execution_id or selection.execution_receipt or selection.resource_bindings:
            try:
                self._resource_record(selection, settings)
                bindings = dict(selection.resource_bindings)
                if (mode != "normal" or len(selection.resource_bindings) != 3
                        or set(bindings) != {BASE_ID, QWEN_ID, VAE_ID}):
                    raise AnimaError("Invalid exact execution binding.")
            except (OSError, RuntimeError, ValueError):
                issues.append("anima_resource_preparation_changed")
                bindings = {BASE_ID: "", QWEN_ID: "", VAE_ID: ""}
        resource_roles = ((selection.base_id, "unet"), (selection.qwen_id, "text_encoder"),
                          (selection.vae_id, "vae"))
        guarded_paths = None
        boundaries = (*self._execution_cache.protected_roots, *self._execution_cache.managed_roots)
        if boundaries:
            from .model_workspace import _path
            guarded_paths = {}
            try:
                snapshot, _ = self.registry.snapshot()
                # Check the entire set before the first model read. Resolve
                # only these frozen paths, never a newly registered fallback.
                for resource_id, _ in resource_roles:
                    candidates = ([bindings[resource_id]] if bindings else
                        [row["path"] for row in snapshot.get("resources", {}).get(resource_id, {}).get("locations", [])])
                    guarded_paths[resource_id] = tuple(_path(path, boundaries) for path in candidates)
            except (KeyError, TypeError, OSError, RuntimeError, ValueError):
                guarded_paths = {}
                issues.append("selected_anima_resource_boundary_held")
        for resource_id, role in resource_roles:
            _check_cancel(should_cancel)
            try:
                if bindings and not bindings.get(resource_id):
                    raise AnimaError("Exact execution path is unavailable.")
                if guarded_paths is None:
                    value = self.registry.resolve(resource_id, role=role, should_cancel=should_cancel,
                        **({"location_path": Path(bindings[resource_id])} if bindings else {}))
                else:
                    value = None
                    for path in guarded_paths.get(resource_id, ()):
                        value = self.registry.resolve(resource_id, role=role, location_path=path,
                            should_cancel=should_cancel)
                        if value.status == "identity_verified" and value.inspection is not None:
                            break
            except (OSError, RuntimeError, ValueError):
                value = None
            if value is None or value.status != "identity_verified" or value.inspection is None:
                issues.append("selected_anima_resource_unavailable")
            else:
                resolved.append(str(value.inspection.path))
        if len(resolved) == 3:
            try:
                if not _embedded_llm_adapter(Path(resolved[0])):
                    issues.append("embedded_llm_adapter_unverified")
            except (OSError, ValueError):
                issues.append("embedded_llm_adapter_unverified")
        manifest_sha = ""
        if package.valid:
            manifest_sha = ProcessInput.capture(Path(selection.package_path) / MANIFEST_NAME,
                should_cancel).sha256
        binding = {**_qualification_binding(runtime.fingerprint if runtime else ""),
            "package_fingerprint": package.package_fingerprint if package.valid else "",
            "manifest_sha256": manifest_sha, "settings": settings.payload()}
        qualification_sha = ""
        if mode == "normal":
            if qualification_path is None:
                issues.append("anima_runtime_not_qualified")
            else:
                try:
                    qualification_sha = self._read_qualification(Path(qualification_path),
                        _qualification_binding(runtime.fingerprint if runtime else ""),
                        should_cancel=should_cancel)
                except (OSError, RuntimeError, ValueError):
                    issues.append("anima_runtime_not_qualified")
        elif mode != "trial":
            raise AnimaError("Unsupported Anima admission mode.")
        binding["qualification_sha256"] = qualification_sha
        if selection.execution_id:
            binding["execution_resources"] = {"execution_id": selection.execution_id,
                "receipt": selection.execution_receipt, "paths": list(selection.resource_bindings)}
        return AnimaPreview(selection, settings, not issues, mode, tuple(dict.fromkeys(issues)),
            _digest(binding), package.package_fingerprint if package.valid else "",
            manifest_sha, runtime.fingerprint if runtime else "", tuple(resolved) if len(resolved) == 3 else ())

    def preview(self, selection, settings=AnimaSettings(), *, qualification_path=None, should_cancel=None):
        return self._preview(selection, settings, mode="normal",
            qualification_path=qualification_path or DEFAULT_QUALIFICATION,
            should_cancel=should_cancel)

    def preview_trial(self, selection, settings=AnimaSettings(), *, should_cancel=None):
        """Only an explicitly invoked generated-data qualification workflow uses this mode."""
        return self._preview(selection, settings, mode="trial", should_cancel=should_cancel)

    def prepare(self, preview: AnimaPreview, root, name, *, qualification_path=None,
                should_cancel=None):
        if preview.mode != "normal":
            raise AnimaError("A trial review is not ordinary training readiness.")
        from .model_storage_policy import load_policy
        if (load_policy(self.registry.snapshot()[0].get("workspace", {})).mode == "tiered"
                and not preview.selection.execution_id):
            raise AnimaError("Prepare the exact Fast resource bindings before freezing a tiered run.")
        fresh = self.preview(preview.selection, preview.settings,
            qualification_path=qualification_path, should_cancel=should_cancel)
        return self._prepare(preview, fresh, root, name, should_cancel,
            qualification_path=qualification_path)

    def prepare_trial(self, preview: AnimaPreview, root, name, *, should_cancel=None):
        if preview.mode != "trial":
            raise AnimaError("Only an exact trial review can prepare a qualification trial.")
        fresh = self.preview_trial(preview.selection, preview.settings,
            should_cancel=should_cancel)
        return self._prepare(preview, fresh, root, name, should_cancel)

    def _prepare(self, preview, fresh, root, name, should_cancel, *, qualification_path=None):
        if not preview.ready or not fresh.ready or fresh.fingerprint != preview.fingerprint:
            raise AnimaError("The Anima selection changed or remains unqualified.")
        root = Path(root).absolute()
        prep = root / ("." + name + ".anima-preparation")
        _assert_unredirected(prep)
        prep.mkdir()  # no clobber; retain partial evidence on failure
        snapshot = prep / "source"
        snapshot.mkdir()
        try:
            catalog = json.loads(_read(CATALOG_PATH, limit=1024 * 1024))
            names = tuple(catalog["files"])
            if catalog.get("source_revision") != SOURCE_REVISION or not 1 <= len(names) <= 111:
                raise AnimaError("The pinned Anima source catalog changed.")
            for relative in dict.fromkeys((*names, "anima_train_network.py", *CONFIG_SHA256)):
                _check_cancel(should_cancel)
                source = Path(preview.selection.source_path) / relative
                selected = ProcessInput.capture(source, should_cancel)
                if (relative == "anima_train_network.py" and selected.sha256 != SOURCE_SCRIPT_SHA256
                        or relative in CONFIG_SHA256 and selected.sha256 != CONFIG_SHA256[relative]
                        or relative in catalog["files"] and selected.sha256 not in catalog["files"][relative]):
                    raise AnimaError("The pinned Anima source changed before snapshotting.")
                if selected.state[2] > 8 * 1024 * 1024:
                    raise AnimaError("An Anima source file exceeds the snapshot bound.")
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _assert_unredirected(target)
                with source.open("rb") as reader, target.open("xb") as writer:
                    copied = 0
                    while True:
                        _check_cancel(should_cancel)
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > selected.state[2]:
                            raise AnimaError("An Anima source file changed during snapshotting.")
                        writer.write(chunk)
                    writer.flush(); os.fsync(writer.fileno())
                if ProcessInput.capture(source, should_cancel) != selected or ProcessInput.capture(target, should_cancel).sha256 != selected.sha256:
                    raise AnimaError("An Anima source file changed during snapshotting.")
            source_inventory = SoftwareInventoryService().capture(snapshot, should_cancel=should_cancel)
            source_inventory_path = prep / "source-inventory.json"
            SoftwareInventoryService.save(source_inventory, source_inventory_path)
            package_inventory = SoftwareInventoryService().capture(
                preview.selection.package_path, should_cancel=should_cancel)
            package_inventory_path = prep / "package-inventory.json"
            SoftwareInventoryService.save(package_inventory, package_inventory_path)
            contract = {"schema_version": 1, "kind": "collectigen_anima_dit_lora_trial" if preview.mode == "trial" else "collectigen_anima_dit_lora",
                "source_revision": SOURCE_REVISION, "preview_fingerprint": preview.fingerprint,
                "source": str(snapshot), "site_packages": str(Path(preview.selection.environment_root) / "Lib/site-packages"),
                "package": {"path": str(preview.selection.package_path),
                    "fingerprint": preview.package_fingerprint, "manifest_sha256": preview.manifest_sha256},
                "resources": {"base": {"path": preview.resource_paths[0], "id": BASE_ID},
                    "qwen": {"path": preview.resource_paths[1], "id": QWEN_ID},
                    "vae": {"path": preview.resource_paths[2], "id": VAE_ID}},
                "settings": preview.settings.payload(),
                "output_name": OUTPUT_NAME}
            contract_path = prep / "anima-contract.json"
            with contract_path.open("xb") as handle:
                raw = _bytes(contract)
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            if _read(contract_path, limit=64 * 1024) != raw:
                raise AnimaError("The Anima execution contract failed readback.")
            plan = self.process.prepare(root, name,
                (str(Path(preview.selection.environment_root) / "Scripts/python.exe"),
                 "-I", "-S", "-B", "-X", "utf8", str(WORKER), str(contract_path)),
                inputs=(WORKER, contract_path, *(row["path"] for row in contract["resources"].values()),
                    *(path for name, path in QUALIFICATION_CODE.items() if name != "worker")),
                inventories=((snapshot, source_inventory_path),
                    (preview.selection.environment_root, preview.selection.environment_inventory),
                    (preview.selection.python_base_root, preview.selection.python_base_inventory),
                    (preview.selection.package_path, package_inventory_path)),
                preview_fingerprint=_digest(contract), timeout_seconds=preview.settings.timeout_seconds,
                log_limit=1024 * 1024, should_cancel=should_cancel,
                environment_profile=LOCAL_ENVIRONMENT)
            prepared = AnimaPrepared(preview, plan, contract_path, prep)
            if preview.mode == "normal":
                if preview.selection.execution_id:
                    record = self._resource_record(preview.selection, preview.settings)
                    if record["phase"] != "prepared":
                        raise AnimaError("This resource preparation already owns a frozen run; reopen or discard it.")
                    self._update_resources(preview.selection.execution_receipt, record,
                        {"phase": "bound", "plan_fingerprint": plan.fingerprint,
                         "review_path": str(prep / "anima-review.json")})
                self.retain_review(prepared, qualification_path=qualification_path)
            return prepared
        except BaseException:
            # Retain exact partial preparation for diagnosis; no source is modified.
            raise

    def execute(self, prepared: AnimaPrepared, confirmed_fingerprint, jobs, *,
                qualification_path=None, should_cancel=None):
        if not isinstance(prepared, AnimaPrepared) or not prepared.preview.ready:
            raise AnimaError("No exact Anima preparation is ready.")
        from .model_storage_policy import load_policy
        if (prepared.preview.mode == "normal" and not prepared.preview.selection.execution_id
                and load_policy(self.registry.snapshot()[0].get("workspace", {})).mode == "tiered"):
            raise AnimaError("Prepare the exact Fast resource bindings before launching a tiered run.")
        fresh = (self.preview_trial(prepared.preview.selection, prepared.preview.settings,
                should_cancel=should_cancel) if prepared.preview.mode == "trial" else
            self.preview(prepared.preview.selection, prepared.preview.settings,
                qualification_path=qualification_path, should_cancel=should_cancel))
        if not fresh.ready or fresh.fingerprint != prepared.preview.fingerprint:
            raise AnimaError("The Anima selection changed before execution.")
        contract_raw = _read(prepared.contract_path, limit=64 * 1024)
        if (sha256(contract_raw).hexdigest() != prepared.plan.preview_fingerprint
                or contract_raw != _bytes(json.loads(contract_raw))):
            raise AnimaError("The Anima execution contract changed.")
        selection = prepared.preview.selection
        record = None
        if selection.execution_id:
            record = self._resource_record(selection, prepared.preview.settings)
            if record.get("plan_fingerprint") != prepared.plan.fingerprint or record["phase"] != "bound":
                raise AnimaError("This exact execution is already submitted or requires recovery.")
            if not record.get("usage_attempted"):
                # Persist the no-admission sentinel first. A crash here cannot
                # re-admit an old execution after reset/history compaction.
                record = self._update_resources(selection.execution_receipt, record,
                    {"usage_attempted": True, "usage_admission": None})
                admission = None
                try:
                    facts = self.admission_facts(selection.execution_id)
                    admission = self.usage.admit(facts.owner_id, facts.owner_version, facts.execution_id,
                        facts.resource_ids, facts.required_set_digest) if facts else None
                except (OSError, RuntimeError, ValueError):
                    pass  # Optional usage failure never changes run admission.
                if admission is not None:
                    record = self._update_resources(selection.execution_receipt, record,
                        {"usage_admission": admission.to_dict()})
            record = self._update_resources(selection.execution_receipt, record, {"phase": "submitted"})
        invoked = []
        try:
            outcome = self.process.execute(prepared.plan, confirmed_fingerprint, jobs,
                should_cancel=should_cancel, **({"invocation_observer": lambda: invoked.append(True)} if record else {}))
        except BaseException:
            if record and not invoked:
                self._update_resources(selection.execution_receipt, record, {"phase": "bound"})
            raise
        if record:
            phase = "bound" if outcome.status == "held" and not invoked else "stopped" if self._stopped_proof(prepared, outcome) else "uncertain"
            self._update_resources(selection.execution_receipt, record, {"phase": phase})
        return outcome

    def recover(self, prepared: AnimaPrepared, expected_tip):
        return self.process.recover(prepared.plan.destination, expected_tip)

    def verify_result(self, prepared: AnimaPrepared, outcome: TrainingProcessResult,
                      *, should_cancel=None) -> AnimaVerifiedResult:
        _process_evidence(prepared.plan, outcome)
        _check_cancel(should_cancel)
        contract_raw = _read(prepared.contract_path, limit=64 * 1024)
        if sha256(contract_raw).hexdigest() != prepared.plan.preview_fingerprint:
            raise AnimaError("The Anima execution contract changed.")
        contract = json.loads(contract_raw)
        output = prepared.plan.destination / "work" / "output" / OUTPUT_NAME
        receipt_path = prepared.plan.destination / "work" / "anima-worker-result.json"
        raw = _read(receipt_path, limit=16 * 1024)
        try:
            receipt = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AnimaError("The Anima worker result is invalid.") from None
        if (type(receipt) is not dict or _bytes(receipt) != raw
                or receipt.get("schema_version") != 1 or receipt.get("kind") != "anima_worker_result"
                or receipt.get("contract_sha256") != sha256(contract_raw).hexdigest()
                or receipt.get("output_name") != OUTPUT_NAME
                or receipt.get("max_steps") != prepared.preview.settings.max_steps):
            raise AnimaError("The Anima worker result is unbound.")
        inspection = inspect_model_resource(output, should_cancel=should_cancel)
        if (inspection.size > MAX_OUTPUT_BYTES or inspection.resource_id != receipt.get("output_id")
                or inspection.size != receipt.get("output_bytes")):
            raise AnimaError("The Anima LoRA output changed or exceeds its bound.")
        count = _verify_lora_tensors(output, should_cancel)
        package = self.packages.verify(prepared.preview.selection.package_path,
            should_cancel=should_cancel)
        if (not package.valid or package.package_fingerprint != prepared.preview.package_fingerprint
                or ProcessInput.capture(Path(package.destination) / MANIFEST_NAME,
                    should_cancel).sha256 != prepared.preview.manifest_sha256):
            raise AnimaError("The Anima package changed before result verification.")
        verified = AnimaVerifiedResult(prepared, outcome, inspection.resource_id, output,
            outcome.journal_tip, sha256(raw).hexdigest(), count)
        if prepared.preview.mode == "normal" and prepared.preview.selection.execution_id:
            self._record_verified_use(verified)
        return verified

    def _record_verified_use(self, verified):
        from .model_usage import UsageAdmission, UsageAttestation
        from .model_use_leases import now
        selection = verified.prepared.preview.selection
        record = self._resource_record(selection, verified.prepared.preview.settings)
        if record.get("plan_fingerprint") != verified.prepared.plan.fingerprint:
            raise AnimaError("The verified result belongs to another execution preparation.")
        if not record.get("usage_admission"):
            return
        try:
            admission = UsageAdmission.from_dict(record["usage_admission"])
            if not record.get("verified_completed_at"):
                record = self._update_resources(selection.execution_receipt, record,
                    {"verified_completed_at": now(), "verified_journal_tip": verified.journal_tip})
            if record.get("verified_journal_tip") != verified.journal_tip:
                return
            proof = uuid4().hex
            self._usage_proofs[proof] = UsageAttestation(RESOURCE_ADAPTER_ID, SOURCE_REVISION,
                selection.execution_id, tuple(record["resource_ids"]), record["required_set_digest"],
                record["verified_completed_at"], admission)
            try:
                self.usage.record_success(admission, proof)
            finally:
                self._usage_proofs.pop(proof, None)
        except (OSError, RuntimeError, ValueError):
            pass  # Verified output remains usable when optional history is held.

    @staticmethod
    def retain_review(prepared: AnimaPrepared, *, qualification_path=None) -> Path:
        """Persist the exact normal run before launch for restart/recovery review."""
        if prepared.preview.mode != "normal" or not prepared.preview.ready:
            raise AnimaError("Only an admitted ordinary Anima run can be retained.")
        selected = prepared.preview.selection
        value = {"schema_version": 1, "kind": "collectigen_anima_retained_review",
            "selection": {key: str(getattr(selected, key)) for key in (
                "package_path", "source_path", "environment_root", "python_base_root",
                "environment_inventory", "python_base_inventory")},
            "settings": prepared.preview.settings.payload(),
            "preview": {"fingerprint": prepared.preview.fingerprint,
                "package_fingerprint": prepared.preview.package_fingerprint,
                "manifest_sha256": prepared.preview.manifest_sha256,
                "runtime_fingerprint": prepared.preview.runtime_fingerprint,
                "resource_paths": list(prepared.preview.resource_paths)},
            "qualification_path": str(Path(qualification_path or DEFAULT_QUALIFICATION).absolute()),
            "plan": asdict(prepared.plan), "contract_path": str(prepared.contract_path),
            "preparation_root": str(prepared.preparation_root)}
        if selected.execution_id:
            value["execution_resources"] = {"execution_id": selected.execution_id,
                "execution_receipt": selected.execution_receipt,
                "resource_bindings": list(selected.resource_bindings)}
        raw = _bytes(value)
        if len(raw) > 256 * 1024:
            raise AnimaError("The Anima retained review exceeds its size bound.")
        destination = prepared.preparation_root / "anima-review.json"
        _assert_unredirected(destination)
        with destination.open("xb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        if _read(destination, limit=256 * 1024) != raw:
            raise AnimaError("The Anima retained review failed readback.")
        return destination

    @staticmethod
    def open_review(path) -> tuple[AnimaPrepared, TrainingProcessResult | None, Path]:
        """Restore one exact review; journal state is authoritative after restart."""
        path = Path(path).absolute()
        raw = _read(path, limit=256 * 1024)
        try:
            value = json.loads(raw)
            if (type(value) is not dict or _bytes(value) != raw
                    or value.get("schema_version") != 1
                    or value.get("kind") != "collectigen_anima_retained_review"
                    or path.name != "anima-review.json"):
                raise ValueError()
            selected = value["selection"]
            selection = AnimaSelection(**{key: Path(selected[key]) for key in (
                "package_path", "source_path", "environment_root", "python_base_root",
                "environment_inventory", "python_base_inventory")})
            if "execution_resources" in value:
                retained = value["execution_resources"]
                selection = replace(selection, base_id=BASE_ID, qwen_id=QWEN_ID, vae_id=VAE_ID,
                    execution_id=retained["execution_id"],
                    execution_receipt=retained["execution_receipt"],
                    resource_bindings=tuple(tuple(pair) for pair in retained["resource_bindings"]))
            settings = AnimaSettings(**value["settings"])
            settings.validate()
            preview_value = value["preview"]
            preview = AnimaPreview(selection, settings, True, "normal", (),
                preview_value["fingerprint"], preview_value["package_fingerprint"],
                preview_value["manifest_sha256"], preview_value["runtime_fingerprint"],
                tuple(preview_value["resource_paths"]))
            plan_value = value["plan"]
            plan = TrainingProcessPlan(**{**plan_value,
                "root_identity": tuple(plan_value["root_identity"]),
                "command": tuple(plan_value["command"]),
                "inputs": tuple(ProcessInput(row["path"], tuple(row["state"]), row["sha256"])
                    for row in plan_value["inputs"]),
                "inventories": tuple(ProcessInventory(**row) for row in plan_value["inventories"])})
            plan.validate()
            preparation_root = Path(value["preparation_root"])
            contract_path = Path(value["contract_path"])
            qualification_path = Path(value["qualification_path"])
            if (not qualification_path.is_absolute() or preparation_root != path.parent
                    or preparation_root != Path(plan.root) / ("." + plan.name + ".anima-preparation")
                    or contract_path != preparation_root / "anima-contract.json"
                    or plan.command[6:] != (str(WORKER), str(contract_path))
                    or len(preview.resource_paths) != 3):
                raise ValueError()
            contract_raw = _read(contract_path, limit=64 * 1024)
            contract = json.loads(contract_raw)
            if (sha256(contract_raw).hexdigest() != plan.preview_fingerprint
                    or _bytes(contract) != contract_raw
                    or contract.get("kind") != "collectigen_anima_dit_lora"
                    or contract.get("preview_fingerprint") != preview.fingerprint
                    or contract.get("settings") != settings.payload()
                    or contract.get("package", {}).get("path") != str(selection.package_path)
                    or contract.get("resources", {}).get("base", {}).get("path") != preview.resource_paths[0]
                    or contract.get("resources", {}).get("qwen", {}).get("path") != preview.resource_paths[1]
                    or contract.get("resources", {}).get("vae", {}).get("path") != preview.resource_paths[2]):
                raise ValueError()
            prepared = AnimaPrepared(preview, plan, contract_path, preparation_root)
            if not plan.destination.exists():
                return prepared, None, qualification_path
            rows, tip = TrainingRunJournal(plan.destination).read()
            if not rows or rows[0]["details"].get("plan", {}).get("process_fingerprint") != plan.fingerprint:
                raise ValueError()
            last = rows[-1]
            details = last["details"]
            status = last["state"] if last["state"] in {
                "exited_unverified", "cancelled", "failed", "interrupted"} else "recovery_required"
            outcome = TrainingProcessResult(status, plan.destination,
                str(details.get("message") or "Retained run requires recovery review."),
                details.get("exit_code"), details.get("tree_stopped") is True, tip)
            return prepared, outcome, qualification_path
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            raise AnimaError("The retained Anima review is unavailable, changed, or needs manual recovery.") from None

    def record_trial(self, verified: AnimaVerifiedResult, destination, *, should_cancel=None):
        """Retain one independently rechecked trial; never register its model."""
        if verified.prepared.preview.mode != "trial" or self.verify_result(
                verified.prepared, verified.outcome, should_cancel=should_cancel) != verified:
            raise AnimaError("An Anima qualification trial changed before retention.")
        selected = verified.prepared.preview.selection
        value = {"schema_version": 1, "kind": "collectigen_anima_verified_trial",
            "selection": {key: str(getattr(selected, key)) for key in (
                "package_path", "source_path", "environment_root", "python_base_root",
                "environment_inventory", "python_base_inventory")},
            "settings": verified.prepared.preview.settings.payload(),
            "preview_fingerprint": verified.prepared.preview.fingerprint,
            "plan": asdict(verified.prepared.plan),
            "contract_path": str(verified.prepared.contract_path),
            "preparation_root": str(verified.prepared.preparation_root),
            "outcome": {**asdict(verified.outcome), "destination": str(verified.outcome.destination)},
            "output_id": verified.output_id,
            "worker_receipt_sha256": verified.worker_receipt_sha256,
            "tensor_count": verified.tensor_count}
        raw = _bytes(value)
        if len(raw) > 256 * 1024:
            raise AnimaError("The Anima trial evidence exceeds its size bound.")
        destination = Path(destination).absolute()
        _assert_unredirected(destination)
        with destination.open("xb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        if _read(destination, limit=256 * 1024) != raw:
            raise AnimaError("The Anima trial evidence failed readback.")
        return destination

    def verify_trial(self, path, *, should_cancel=None):
        """Reconstruct and recheck the exact native journal, inputs and output."""
        raw = _read(Path(path).absolute(), limit=256 * 1024)
        try:
            value = json.loads(raw)
            if (type(value) is not dict or _bytes(value) != raw
                    or value.get("schema_version") != 1
                    or value.get("kind") != "collectigen_anima_verified_trial"):
                raise ValueError()
            selected = value["selection"]
            selection = AnimaSelection(**{key: Path(selected[key]) for key in (
                "package_path", "source_path", "environment_root", "python_base_root",
                "environment_inventory", "python_base_inventory")})
            settings = AnimaSettings(**value["settings"])
            preview = self.preview_trial(selection, settings, should_cancel=should_cancel)
            if not preview.ready or preview.fingerprint != value["preview_fingerprint"]:
                raise ValueError()
            plan_value = value["plan"]
            plan = TrainingProcessPlan(**{**plan_value,
                "root_identity": tuple(plan_value["root_identity"]),
                "command": tuple(plan_value["command"]),
                "inputs": tuple(ProcessInput(row["path"], tuple(row["state"]), row["sha256"])
                    for row in plan_value["inputs"]),
                "inventories": tuple(ProcessInventory(**row) for row in plan_value["inventories"])})
            plan.validate()
            outcome_value = value["outcome"]
            outcome = TrainingProcessResult(**{**outcome_value,
                "destination": Path(outcome_value["destination"])})
            prepared = AnimaPrepared(preview, plan, Path(value["contract_path"]),
                Path(value["preparation_root"]))
            verified = self.verify_result(prepared, outcome, should_cancel=should_cancel)
            if (verified.output_id != value["output_id"]
                    or verified.worker_receipt_sha256 != value["worker_receipt_sha256"]
                    or verified.tensor_count != value["tensor_count"]):
                raise ValueError()
            return verified
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            if isinstance(exc, InterruptedError):
                raise
            raise AnimaError("The Anima trial evidence is unavailable or changed.") from None

    def _read_qualification(self, path: Path, expected: dict, *, should_cancel=None) -> str:
        # Only the separately reviewed application artifact can grant ordinary admission.
        # A selected run's receipt is evidence for the curator, never an admission profile.
        if Path(path).absolute() != DEFAULT_QUALIFICATION:
            raise AnimaError("The Anima qualification profile is not the curated application artifact.")
        raw = _read(DEFAULT_QUALIFICATION, limit=32 * 1024)
        try:
            value = json.loads(raw)
            if (type(value) is not dict or set(value) != {"schema_version", "kind",
                    "binding", "native_evidence_sha256", "code_sha256"}
                    or _bytes(value) != raw or value.get("schema_version") != 2
                    or value.get("kind") != "collectigen_anima_base_qualification"
                    or value.get("binding") != expected
                    or type(value.get("native_evidence_sha256")) is not str
                    or not SHA.fullmatch(value["native_evidence_sha256"])
                    or value.get("code_sha256") != _qualification_code_hashes(should_cancel)):
                raise ValueError()
            evidence_raw = _read(DEFAULT_NATIVE_EVIDENCE, limit=32 * 1024)
            if sha256(evidence_raw).hexdigest() != value["native_evidence_sha256"]:
                raise ValueError()
            evidence = json.loads(evidence_raw)
            fields = {"schema_version", "kind", "binding", "code_sha256",
                "trial_sha256", "process_fingerprint", "process_journal_tip",
                "input_verification_sha256", "process_input_sha256", "inventory_sha256",
                "contract_sha256", "package_fingerprint", "manifest_sha256",
                "worker_receipt_sha256", "output_sha256", "tensor_count",
                "max_steps", "run_id", "exit_code", "tree_stopped",
                "finite_changed_dit_only", "quality"}
            hashes = {"trial_sha256", "process_fingerprint", "process_journal_tip",
                "contract_sha256", "package_fingerprint", "manifest_sha256",
                "worker_receipt_sha256", "output_sha256"}
            if (type(evidence) is not dict or set(evidence) != fields
                    or _bytes(evidence) != evidence_raw or evidence["schema_version"] != 1
                    or evidence["kind"] != "collectigen_anima_native_evidence"
                    or evidence["binding"] != expected
                    or evidence["code_sha256"] != value["code_sha256"]
                    or any(type(evidence[key]) is not str or not SHA.fullmatch(evidence[key])
                        for key in hashes)
                    or any(type(evidence[key]) is not list or not evidence[key]
                        or len(evidence[key]) > 32 or any(type(item) is not str
                            or not SHA.fullmatch(item) for item in evidence[key])
                        for key in ("input_verification_sha256", "process_input_sha256",
                            "inventory_sha256"))
                    or len(evidence["input_verification_sha256"]) != 3
                    or type(evidence["tensor_count"]) is not int
                    or not 2 <= evidence["tensor_count"] <= 20000
                    or type(evidence["max_steps"]) is not int
                    or not 2 <= evidence["max_steps"] <= 20
                    or type(evidence["run_id"]) is not str
                    or not re.fullmatch(r"[0-9a-f]{32}", evidence["run_id"])
                    or evidence["exit_code"] != 0 or evidence["tree_stopped"] is not True
                    or evidence["finite_changed_dit_only"] is not True
                    or evidence["quality"] != "unmeasured"):
                raise ValueError()
            return sha256(raw).hexdigest()
        except InterruptedError:
            raise
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            raise AnimaError("The Anima qualification requires one current verified two-step trial.") from None

    def qualify(self, trial_path, destination, *, should_cancel=None) -> Path:
        """Curate a path-free proof after complete current native trial replay."""
        path = Path(trial_path).absolute()
        verified = self.verify_trial(path, should_cancel=should_cancel)
        if (verified.prepared.preview.settings.max_steps < 2
                or verified.outcome.status != "exited_unverified"
                or verified.outcome.exit_code != 0 or verified.outcome.tree_stopped is not True):
            raise AnimaError("Anima qualification needs a stopped successful two-step trial.")
        destination = Path(destination).absolute()
        if destination != DEFAULT_QUALIFICATION:
            raise AnimaError("Publish the Anima qualification only to its curator artifact path.")
        code_hashes = _qualification_code_hashes(should_cancel)
        inputs = {row.path: row.sha256 for row in verified.prepared.plan.inputs}
        if any(inputs.get(str(source)) != code_hashes[name]
               for name, source in QUALIFICATION_CODE.items()):
            raise AnimaError("The trial did not pin the current Anima adapter and native helpers.")
        _rows, tip, input_receipts = _process_evidence(verified.prepared.plan, verified.outcome)
        if tip != verified.journal_tip:
            raise AnimaError("The Anima trial process journal changed.")
        binding = _qualification_binding(verified.prepared.preview.runtime_fingerprint)
        evidence = {"schema_version": 1, "kind": "collectigen_anima_native_evidence",
            "binding": binding, "code_sha256": code_hashes,
            "trial_sha256": sha256(_read(path, limit=256 * 1024)).hexdigest(),
            "process_fingerprint": verified.prepared.plan.fingerprint,
            "process_journal_tip": verified.journal_tip,
            "input_verification_sha256": input_receipts,
            "process_input_sha256": [row.sha256 for row in verified.prepared.plan.inputs],
            "inventory_sha256": [row.manifest_sha256 for row in verified.prepared.plan.inventories],
            "contract_sha256": ProcessInput.capture(verified.prepared.contract_path,
                should_cancel).sha256,
            "package_fingerprint": verified.prepared.preview.package_fingerprint,
            "manifest_sha256": verified.prepared.preview.manifest_sha256,
            "worker_receipt_sha256": verified.worker_receipt_sha256,
            "output_sha256": verified.output_id.removeprefix("sha256:"),
            "tensor_count": verified.tensor_count,
            "max_steps": verified.prepared.preview.settings.max_steps,
            "run_id": verified.prepared.plan.run_id,
            "exit_code": verified.outcome.exit_code,
            "tree_stopped": verified.outcome.tree_stopped,
            "finite_changed_dit_only": True,
            "quality": "unmeasured"}
        evidence_raw = _bytes(evidence)
        raw = _bytes({"schema_version": 2, "kind": "collectigen_anima_base_qualification",
            "binding": binding, "code_sha256": code_hashes,
            "native_evidence_sha256": sha256(evidence_raw).hexdigest()})
        _assert_unredirected(DEFAULT_NATIVE_EVIDENCE)
        _assert_unredirected(destination)
        with DEFAULT_NATIVE_EVIDENCE.open("xb") as handle:
            handle.write(evidence_raw); handle.flush(); os.fsync(handle.fileno())
        if _read(DEFAULT_NATIVE_EVIDENCE, limit=32 * 1024) != evidence_raw:
            raise AnimaError("The Anima native evidence failed readback.")
        with destination.open("xb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        if _read(destination, limit=32 * 1024) != raw:
            raise AnimaError("The Anima qualification receipt failed readback.")
        self._read_qualification(destination, binding, should_cancel=should_cancel)
        return destination

    def register(self, verified: AnimaVerifiedResult, expected_registry,
                 *, qualification_path=None, should_cancel=None):
        if verified.prepared.preview.mode != "normal":
            raise AnimaError("Qualification trial outputs are not ordinary registered results.")
        fresh_admission = self.preview(verified.prepared.preview.selection,
            verified.prepared.preview.settings, qualification_path=qualification_path,
            should_cancel=should_cancel)
        if not fresh_admission.ready or fresh_admission.fingerprint != verified.prepared.preview.fingerprint:
            raise AnimaError("Anima qualification changed before result registration.")
        fresh = self.verify_result(verified.prepared, verified.outcome,
            should_cancel=should_cancel)
        if fresh != verified:
            raise AnimaError("The verified Anima result changed before registration.")
        inspection = inspect_model_resource(verified.output_path, should_cancel=should_cancel)
        return self.registry.register_training_result(inspection,
            package_path=verified.prepared.preview.selection.package_path,
            expected_package_fingerprint=verified.prepared.preview.package_fingerprint,
            expected_registry=expected_registry,
            parent_ids=(BASE_ID, QWEN_ID, VAE_ID), trainer_id="sd-scripts.anima_train_network",
            trainer_version=SOURCE_REVISION,
            settings=verified.prepared.preview.settings.payload(), role="lora", family="Anima",
            should_cancel=should_cancel)


def _verify_lora_tensors(path: Path, should_cancel=None) -> int:
    """Bounded safetensors scan: only finite DiT LoRA and changed up weights."""
    _assert_unredirected(path)
    with path.open("rb") as handle:
        size = path.stat().st_size
        if not 16 <= size <= MAX_OUTPUT_BYTES:
            raise AnimaError("The Anima LoRA output exceeds its supported bound.")
        header_size = struct.unpack("<Q", handle.read(8))[0]
        if not 2 <= header_size <= 16 * 1024 * 1024:
            raise AnimaError("The Anima LoRA header exceeds its supported bound.")
        header = json.loads(handle.read(header_size))
        tensors = {key: value for key, value in header.items() if key != "__metadata__"}
        if not 2 <= len(tensors) <= 20000 or any(not key.startswith("lora_unet_") for key in tensors):
            raise AnimaError("The output is not a DiT-only Anima LoRA.")
        up_changed = False
        for key, row in tensors.items():
            _check_cancel(should_cancel)
            if type(row) is not dict or row.get("dtype") not in {"F32", "F16", "BF16"}:
                raise AnimaError("The Anima LoRA has unsupported tensor values.")
            start, end = row.get("data_offsets", (None, None))
            width = 4 if row["dtype"] == "F32" else 2
            if (type(start) is not int or type(end) is not int or not 0 <= start <= end
                    or (end - start) % width or 8 + header_size + end > size):
                raise AnimaError("The Anima LoRA tensor range is invalid.")
            handle.seek(8 + header_size + start)
            remaining = end - start
            while remaining:
                _check_cancel(should_cancel)
                block = handle.read(min(remaining, 1024 * 1024))
                if len(block) == 0:
                    raise AnimaError("The Anima LoRA tensor was truncated.")
                remaining -= len(block)
                if row["dtype"] == "F32":
                    values = struct.iter_unpack("<I", block)
                    for (bits,) in values:
                        if bits & 0x7f800000 == 0x7f800000:
                            raise AnimaError("The Anima LoRA contains nonfinite values.")
                        if key.endswith("lora_up.weight") and bits & 0x7fffffff:
                            up_changed = True
                else:
                    for (bits,) in struct.iter_unpack("<H", block):
                        exponent = bits & (0x7f80 if row["dtype"] == "BF16" else 0x7c00)
                        if exponent == (0x7f80 if row["dtype"] == "BF16" else 0x7c00):
                            raise AnimaError("The Anima LoRA contains nonfinite values.")
                        if key.endswith("lora_up.weight") and bits & 0x7fff:
                            up_changed = True
        if not up_changed:
            raise AnimaError("The Anima LoRA has no observed changed up weights.")
        return len(tensors)
