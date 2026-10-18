"""Pure pre-Qt diagnostics/replacement/recovery contracts; no action adapters."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from release_contract import ReleaseContractError, verify_release_manifest, ensure_supported_upgrade
from runtime_contract import RuntimeCheck
from suite_core.installation import ComponentId, LocalInventory, _relative
from .component_release_signature import verify_component_release, _json, _version, MAX_METADATA_BYTES

ADAPTER_VERSION = "0.1.0-alpha.2"
COMPONENT = ComponentId("collectigen.suite-manager")
QT_PROBES = ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets")


class BootstrapHold(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise BootstrapHold(reason)


def pin(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "exact_sha256")


def rows(value):
    require(type(value) is tuple and len(value) <= 512, "exact_bounded_tuple")
    names = set()
    for row in value:
        require(type(row) is tuple and len(row) == 3, "file_observation")
        try:
            _relative(row[0])
        except ValueError as exc:
            raise BootstrapHold("file_path") from exc
        require(row[0] not in names, "duplicate_file")
        names.add(row[0])
        require((row[1] is None and row[2] is None) or
                (type(row[1]) is int and row[1] >= 0 and type(row[2]) is str), "file_observation")
        if row[2] is not None:
            pin(row[2])
    return {r[0]: (r[1], r[2]) for r in value}


def diagnose(inventory_bytes, *, expected_inventory_sha256, expected_version,
             os, architecture, file_observations, qt_probes):
    """No imports/processes/I/O. Report supplied probe failure even before Qt UI.

    Producer hashes files and runs bounded native-load probes in an external
    process. This function never attempts those probes and is not attestation.
    Missing/untrusted inventory remains held, while diagnostics stay readable.
    """
    issues = []
    _version(expected_version)
    require(os in ("windows", "linux") and architecture in ("x86_64", "aarch64"), "platform")
    observed = rows(file_observations)
    require(type(qt_probes) is tuple and len(qt_probes) == 3
            and all(type(r) is tuple and len(r) == 2 for r in qt_probes)
            and tuple(r[0] for r in qt_probes) == QT_PROBES, "exact_qt_probe_closure")
    for name, state in qt_probes:
        require(type(state) is str and state in ("ok", "failed", "not_run"), "probe_state")
        if state != "ok":
            issues.append(name + " native-load probe " + state + "; startup held")
    if inventory_bytes is None:
        issues.append("Runtime inventory missing; external diagnostic/repair is required")
        return RuntimeCheck(False, tuple(issues))
    if expected_inventory_sha256 is None:
        issues.append("Runtime inventory trust/pin not established")
    else:
        pin(expected_inventory_sha256)
        if (type(inventory_bytes) is not bytes or len(inventory_bytes) > MAX_METADATA_BYTES
                or sha256(inventory_bytes).hexdigest() != expected_inventory_sha256):
            issues.append("Runtime inventory pin mismatch")
    try:
        profile = _json(inventory_bytes, "bootstrap inventory")
        require(set(profile) == {"schema_version", "component_id", "software_version", "platform", "runtime_kind", "files"}
                and type(profile["schema_version"]) is int and profile["schema_version"] == 1, "inventory_schema")
        require(profile["component_id"] == COMPONENT.value and profile["software_version"] == expected_version
                and profile["runtime_kind"] == "frozen-portable", "inventory_identity")
        require(profile["platform"] == {"os": os, "architecture": architecture}, "inventory_platform")
        require(type(profile["files"]) is list and 0 < len(profile["files"]) <= 512, "inventory_files")
        expected_rows = []
        for file in profile["files"]:
            require(type(file) is dict and set(file) == {"path", "size_bytes", "sha256"}, "inventory_file_schema")
            expected_rows.append((file["path"], file["size_bytes"], file["sha256"]))
        expected = rows(tuple(expected_rows))
        require(all(size is not None for size, _ in expected.values()), "inventory_file_pin_required")
        if set(observed) != set(expected):
            issues.append("Runtime file-observation closure differs from inventory")
        for path, wanted in expected.items():
            if observed.get(path) != wanted:
                issues.append("Runtime file missing or mismatched: " + path)
    except (BootstrapHold, ReleaseContractError) as exc:
        issues.append("Invalid runtime inventory: " + str(exc))
        profile = None
    return RuntimeCheck(not issues, tuple(issues), profile)


@dataclass(frozen=True)
class BootstrapState:
    active: LocalInventory
    software_version: str
    artifact_sha256: str
    os: str
    architecture: str
    data_sentinels: tuple  # supplied opaque hashes; never open user data

    def __post_init__(self):
        require(type(self.active) is LocalInventory, "local_inventory")
        _version(self.software_version)
        pin(self.artifact_sha256)
        require(self.os in ("windows", "linux") and self.architecture in ("x86_64", "aarch64"), "platform")
        require(type(self.data_sentinels) is tuple and len(self.data_sentinels) <= 64, "data_sentinels")
        names = set()
        for row in self.data_sentinels:
            require(type(row) is tuple and len(row) == 2 and type(row[0]) is str
                    and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", row[0]) and row[0] not in names, "data_sentinel")
            names.add(row[0])
            pin(row[1])

    @property
    def token(self):
        # Local stale-state token only; not a signing/canonicalization contract.
        value = [self.active.relative_directory, self.active.manifest, self.active.manifest_sha256,
                 self.software_version, self.artifact_sha256, self.os, self.architecture, self.data_sentinels]
        return sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ActionApproval:
    before_token: str
    target_artifact_sha256: str
    explicitly_approved: bool = False  # supplied decision; never authorizes real I/O


@dataclass(frozen=True)
class ReplacementPlan:
    before: BootstrapState
    target: LocalInventory
    target_version: str
    artifact_sha256: str
    signed_manifest_sha256: str

    @property
    def production_install_authorized(self):
        return False

    @property
    def download_authorized(self):
        return False

    @property
    def repair_authorized(self):
        return False


def plan_replacement(before, target, *, manifest_bytes, signature_bytes, trust_bytes,
                     target_version, staged_inventory_bytes, file_observations, qt_probes,
                     observed_artifact_sha256, observed_artifact_size, idle, approval):
    """Review a verified newer replacement; never repair by arbitrary frozen pip.

    Actual downloads/activation remain absent. Revalidate package/file/probe and
    real user/process approval in the eventual adapter; caller DTOs are no powers.
    """
    require(type(before) is BootstrapState and type(target) is LocalInventory, "exact_plan_inputs")
    require(idle is True, "idle_required")
    _version(target_version)
    require(type(approval) is ActionApproval and approval.explicitly_approved is True
            and approval.before_token == before.token, "explicit_fresh_approval_required")
    verified = verify_component_release(manifest_bytes, signature_bytes, trust_bytes,
        expected_component=COMPONENT, expected_version=target_version, expected_channel="beta" if "-" in target_version else "stable",
        expected_os=before.os, expected_architecture=before.architecture)
    upgrade = verify_release_manifest(manifest_bytes, signature_bytes,
        _json(trust_bytes, "trust"), expected_channel=verified.channel)
    ensure_supported_upgrade(before.software_version, upgrade)
    require(verified.artifact.sha256 != before.artifact_sha256, "same_failing_artifact_not_a_repair")
    require(approval.target_artifact_sha256 == verified.artifact.sha256, "approval_artifact_binding")
    require(type(observed_artifact_size) is int and observed_artifact_size == verified.artifact.size_bytes
            and observed_artifact_sha256 == verified.artifact.sha256, "verified_replacement_artifact_required")
    require(type(staged_inventory_bytes) is bytes and 0 < len(staged_inventory_bytes) <= MAX_METADATA_BYTES, "inventory_bytes_required")
    require(target.manifest_sha256 == verified.artifact.portable_manifest_sha256
            and sha256(staged_inventory_bytes).hexdigest() == target.manifest_sha256, "inventory_signature_binding")
    require(target.relative_directory == f"Apps/{COMPONENT.value}/{target_version}", "immutable_target_directory")
    old = _relative(before.active.relative_directory)
    new = _relative(target.relative_directory)
    if before.os == "windows":
        old, new = tuple(p.casefold() for p in old), tuple(p.casefold() for p in new)
    require(not (old[:len(new)] == new or new[:len(old)] == old), "overlapping_code_directories")
    check = diagnose(staged_inventory_bytes, expected_inventory_sha256=target.manifest_sha256,
        expected_version=target_version, os=before.os, architecture=before.architecture,
        file_observations=file_observations, qt_probes=qt_probes)
    require(check.ready, "staged_runtime_not_ready: " + "; ".join(check.issues))
    return ReplacementPlan(before, target, target_version, verified.artifact.sha256, verified.manifest_sha256)


def recovery_reference(plan, *, phase, observed_active, prior_available, healthy, data_sentinels):
    """Pure decision only; no durable journal, ref switch, process stop or cleanup.

    A writer must retain immutable prior code, revalidate physical paths/pins and
    persist/recover a transaction. Never rewrite catalog/data for runtime repair.
    """
    require(type(plan) is ReplacementPlan and type(observed_active) is LocalInventory, "recovery_inputs")
    require(type(phase) is str and phase in ("prepared", "activated", "healthy"), "recovery_phase")
    require(data_sentinels == plan.before.data_sentinels, "data_changed_manual_recovery_required")
    require(observed_active in (plan.before.active, plan.target), "unexpected_active_reference")
    require(type(healthy) is bool and type(prior_available) is bool, "recovery_observations")
    if phase == "prepared":
        require(observed_active == plan.before.active, "prepared_state_mismatch")
        return plan.before.active
    require(observed_active == plan.target, "activation_state_mismatch")
    if phase == "healthy" and healthy:
        return plan.target
    require(prior_available, "prior_code_unavailable_manual_recovery_required")
    return plan.before.active


# Stable public launcher protocol. Existing diagnostics/replacement APIs above
# remain unchanged. This contract does not launch, import Qt, or invoke Velopack.
STABLE_LAUNCH_PROTOCOL = 1
STABLE_BOOTSTRAP_VERSION = "0.1.0"


@dataclass(frozen=True)
class StableLaunchContract:
    protocol: int
    minimum_bootstrap: str
    maximum_bootstrap_exclusive: str
    manager_relative: str = "Apps/Manager/current/CollectiGenManager.exe"


@dataclass(frozen=True)
class StableLaunchPlan:
    deployment_root: str
    bootstrap_root: str
    manager_executable: str
    manager_sha256: str
    owner_protocol: int
    bootstrap_upgrade: str = "explicit_closed_suite_replacement"
    supported_public_entry: str = "CollectiGen.exe"


def stable_launch_plan(root, contract, *, manager_sha256):
    """Pure distribution/compatibility admission before external owner acquire.

    Packaging must provide and authenticate this contract with Manager inventory.
    Direct internal current executables are unsupported user entry points.
    """
    from pathlib import Path
    require(type(contract) is StableLaunchContract, "stable_launch_contract")
    require(type(contract.protocol) is int and contract.protocol == STABLE_LAUNCH_PROTOCOL,
            "bootstrap_protocol_incompatible")
    def numeric(value):
        require(type(value) is str and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value),
                "bootstrap_version_contract")
        return tuple(int(part) for part in value.split("."))
    require(numeric(contract.minimum_bootstrap) <= numeric(STABLE_BOOTSTRAP_VERSION) <
            numeric(contract.maximum_bootstrap_exclusive), "bootstrap_upgrade_required")
    require(contract.manager_relative == "Apps/Manager/current/CollectiGenManager.exe",
            "unsupported_manager_entry")
    pin(manager_sha256)
    base = Path(root)
    require(base.is_absolute(), "absolute_deployment_root_required")
    return StableLaunchPlan(str(base), str(base / "_bootstrap"),
                           str(base / contract.manager_relative), manager_sha256,
                           STABLE_LAUNCH_PROTOCOL)


def verify_stable_launch_file(plan):
    """Bounded, nonredirected file pin; host revalidates at its spawn boundary.

    Returns an argv tuple, never executes. No arguments accepted from the UI.
    File pin alone is not release admission or a promise against malicious races.
    """
    from pathlib import Path
    from core.training_source_io import _assert_unredirected
    require(type(plan) is StableLaunchPlan, "stable_launch_plan")
    pin(plan.manager_sha256)
    base = Path(plan.deployment_root)
    require(base.is_absolute() and plan.owner_protocol == STABLE_LAUNCH_PROTOCOL and
            plan.bootstrap_root == str(base / "_bootstrap") and
            plan.manager_executable == str(base / "Apps/Manager/current/CollectiGenManager.exe"),
            "stable_launch_layout_mismatch")
    path = Path(plan.manager_executable)
    _assert_unredirected(path)
    require(path.is_file() and path.stat().st_size <= 256 * 1024 * 1024,
            "manager_executable_missing_or_oversized")
    digest = sha256()
    with path.open("rb") as stream:
        before = path.stat()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = path.stat()
    _assert_unredirected(path)
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "manager_file_changed")
    require(digest.hexdigest() == plan.manager_sha256, "manager_file_pin_mismatch")
    return (str(path),)


def bind_stable_launch(plan, arguments, *, validate_normal_launch):
    """Reuse canonical complete binding before any channel/process creation.

    Returns the existing FrozenEntryBinding, not native handoff admission.
    The entry owner supplies process-bound inherited transport separately.
    """
    from suite_core.manager.entry_binding import FrozenEntryBinding
    verify_stable_launch_file(plan)
    require(type(arguments) is tuple and all(type(arg) is str for arg in arguments), "complete_argument_tuple")
    require(callable(validate_normal_launch), "entry_owned_complete_validator_required")
    admitted = validate_normal_launch(arguments, executable=plan.manager_executable)
    require(type(admitted) is FrozenEntryBinding and str(admitted.layout.code_root) == plan.deployment_root,
            "foreign_or_incomplete_entry_binding")
    return admitted


def stable_owner_binding(plan, admitted):
    """Immutable input to the one owner; respects installed separate data roots.

    No launcher/pipe/native defaults are provided in this source candidate.
    """
    from suite_core.manager.entry_binding import FrozenEntryBinding
    require(type(admitted) is FrozenEntryBinding and str(admitted.layout.code_root) == plan.deployment_root,
            "canonical_complete_binding_required")
    return {"root": plan.deployment_root, "data_root": str(admitted.layout.data_root),
            "entry_binding": {"mode": admitted.layout.mode.value,
                "settings_path": str(admitted.settings_path),
                "argv_sha256": sha256(json.dumps(admitted.restart_arguments, ensure_ascii=False,separators=(",", ":")).encode()).hexdigest(),
                "manager_sha256": plan.manager_sha256}}
