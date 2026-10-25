"""Inactive exact stock Manager apply adapter; no executor, lease or IO defaults.

The launcher owner continuously excludes new launches and owns native closure,
durable intent, updater observation and final health. This leaf never coordinates
processes, acquires/releases a lease, downloads, restores or marks apply success.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import stat

from suite_core.discovery.manager_runtime_detached_admission import DetachedAdmissionPlan, PROFILE

COMPONENT = "collectigen.suite-manager"
STOCK_VERSION = "1.2.161"


class StockApplyHold(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise StockApplyHold(reason)


def pin(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "exact_sha256")


def physical(value, *, must_exist=True):
    require(type(value) is str, "exact_path_string")
    require(not value.startswith("\\\\"), "remote_or_device_path")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "absolute_unambiguous_path")
    require(not any("\x00" in p or ":" in p or p.endswith((" ", ".")) for p in path.parts[1:]), "path_alias")
    require(not any(p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)), *(f"LPT{i}" for i in range(1,10))} for p in path.parts[1:]), "path_alias")
    if not must_exist:
        return path  # pure lexical review; owner must qualify root before IO
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400, "link_or_reparse_path")
    require(not must_exist or path.exists(), "missing_path")
    return path


@dataclass(frozen=True)
class StockApplyRequest:
    transaction_id: str
    suite_root: str
    manager_root: str
    packages_dir: str
    update_exe: str
    package: str
    package_size: int
    package_sha256: str
    updater_size: int
    updater_sha256: str
    current_executable_sha256: str
    current_manifest_sha256: str
    current_version: str
    target_version: str
    channel: str
    release_sha256: str
    trust_sha256: str
    inventory_sha256: str
    original_approval_sha256: str
    stale_token: str
    durable_apply_intent_sha256: str
    original_owner_tokens: tuple[str, ...]
    durable_drain_sha256: str
    attempt_identity_sha256: str
    stock_version: str = STOCK_VERSION
    elevation_mode: str = "none"

    @property
    def binding_sha256(self):
        return sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_request(request):
    require(type(request) is StockApplyRequest, "exact_request_type")
    require(type(request.transaction_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request.transaction_id), "transaction_id")
    require(request.stock_version == STOCK_VERSION and request.elevation_mode == "none", "unexpected_stock_or_elevation_mode")
    require(request.channel in ("stable", "beta") and type(request.current_version) is str and type(request.target_version) is str
            and request.current_version and request.target_version
            and request.current_version != request.target_version, "release_context")
    for name in ("package_sha256", "updater_sha256", "current_executable_sha256", "current_manifest_sha256", "release_sha256", "trust_sha256", "inventory_sha256", "original_approval_sha256", "stale_token", "durable_apply_intent_sha256", "durable_drain_sha256", "attempt_identity_sha256"):
        pin(getattr(request, name))
    require(type(request.package_size) is int and 0 < request.package_size <= 512 * 1024 * 1024
            and type(request.updater_size) is int and request.updater_size > 0, "artifact_size")
    require(type(request.original_owner_tokens) is tuple and 1 <= len(request.original_owner_tokens) <= 64
            and len(set(request.original_owner_tokens)) == len(request.original_owner_tokens), "original_owner_closure")
    for token in request.original_owner_tokens:
        pin(token)
    suite = physical(request.suite_root, must_exist=False)
    root = physical(request.manager_root, must_exist=False)
    require(root == suite / "Apps/Manager", "unexpected_suite_or_manager_root")
    packages = physical(request.packages_dir, must_exist=False)
    update = physical(request.update_exe, must_exist=False)
    package = physical(request.package, must_exist=False)
    # Minimal supported stock deployment shape. Custom package roots need their
    # own native locator qualification rather than inferred/foreign paths.
    require(packages == root / "packages" and update == root / "Update.exe", "unexpected_root_mode")
    require(package.parent == packages and package.name.endswith("-full.nupkg"), "exact_full_package_path")
    return root


def fixed_argv(request):
    validate_request(request)
    return (request.update_exe, "apply", "--norestart", "--silent", "--rootDir", request.manager_root,
            "--packageDir", request.packages_dir, "--package", request.package)


def validate_argv(request, argv):
    require(type(argv) is tuple and all(type(arg) is str for arg in argv), "fixed_argument_array")
    require(argv == fixed_argv(request), "unexpected_duplicate_or_unknown_argument")
    return argv


def readback(path, size=None, expected=None):
    path = physical(str(path))
    require(path.is_file(), "regular_payload_file")
    before = path.stat()
    require(size is None or before.st_size == size, "changed_payload_size")
    h = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    after = path.stat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "changed_payload_during_readback")
    require(h.hexdigest() == expected, "changed_payload_pin")


def receipt(value, request, status):
    require(type(value) is dict and value.get("status") == status
            and value.get("transaction_id") == request.transaction_id
            and value.get("binding_sha256") == request.binding_sha256, "unqualified_" + status)
    return value


def held(lease, request, verify_lease):
    require(lease is not None and callable(verify_lease), "held_maintenance_lease_required")
    # Consume the original owner's held descriptor through a stage-2 verifier.
    # No assumed new method on stage-1 DeploymentOwner and no second lease.
    value = receipt(verify_lease(lease, request), request, "maintenance_held")
    require(value.get("continuous_launch_exclusion") is True, "continuous_launch_exclusion_required")
    require(value.get("durable_drain_committed") is True
            and value.get("durable_drain_sha256") == request.durable_drain_sha256,
            "committed_original_drain_required")
    require(value.get("attempt_identity_sha256") == request.attempt_identity_sha256
            and value.get("durable_apply_intent_sha256") == request.durable_apply_intent_sha256
            and value.get("suite_root") == request.suite_root
            and value.get("manager_root") == request.manager_root
            and value.get("package") == request.package
            and value.get("package_sha256") == request.package_sha256
            and value.get("updater_sha256") == request.updater_sha256
            and value.get("current_executable_sha256") == request.current_executable_sha256
            and value.get("current_manifest_sha256") == request.current_manifest_sha256,
            "lease_bound_maintenance_context_required")
    require(value.get("updater_hook_scope_retained") is True, "continuous_updater_hook_scope_required")
    token = value.get("lease_token")
    require(type(token) is str and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token), "original_lease_token")
    return token


def closed(verify_closure, request, lease_token):
    value = receipt(verify_closure(request, lease_token=lease_token), request, "original_owners_closed")
    require(value.get("closed_owner_tokens") == request.original_owner_tokens
            and value.get("unknown_owners") == 0 and type(value.get("unknown_owners")) is int, "unknown_or_unclosed_native_owner")


def queue_stock_apply(request, *, lease, verify_lease, verify_admission, verify_closure, verify_transaction,
                      executor=None, native_review=None, argv=None):
    """Queue evidence only. Native-reviewed injected executor remains mandatory.

    Original Manager cleanup must precede this call. Existing owner durably
    journals intent first and retains the supplied lease after this function.
    """
    root = validate_request(request)
    args = fixed_argv(request) if argv is None else validate_argv(request, argv)
    require(callable(executor) and callable(native_review), "native_execution_disabled")
    require(all(callable(callback) for callback in (verify_lease, verify_admission, verify_closure, verify_transaction)), "mandatory_existing_adapters")
    lease_token = held(lease, request, verify_lease)
    transaction = receipt(verify_transaction(request), request, "original_transaction_current")
    require(transaction.get("durable_apply_intent_sha256") == request.durable_apply_intent_sha256,
            "durable_original_intent_required")
    closed(verify_closure, request, lease_token)
    # Root/transaction ownership must qualify before any input filesystem read.
    require(physical(request.manager_root).is_dir() and physical(request.packages_dir).is_dir()
            and physical(str(root / "current")).is_dir(), "deployment_directories")
    require(physical(request.update_exe).is_file() and physical(request.package).is_file(), "regular_payload_file")
    # Reuse real detached signature/proof admission via owner-supplied adapter.
    # Acquisition happens elsewhere; no downloads or new signature implementation.
    admission = verify_admission(request)
    require(type(admission) is DetachedAdmissionPlan and admission.component_id == COMPONENT
            and admission.version == request.target_version and admission.channel == request.channel
            and admission.inventory_profile == PROFILE and admission.payload_entrypoint == "CollectiGenManager.exe"
            and admission.artifact_sha256 == request.package_sha256 and admission.release_sha256 == request.release_sha256
            and admission.trust_sha256 == request.trust_sha256 and admission.inventory_sha256 == request.inventory_sha256
            and type(admission.payload_mapping) is tuple
            and ("lib/app/CollectiGenManager.exe", "CollectiGenManager.exe") in admission.payload_mapping
            and ("lib/app/sq.version", "sq.version") in admission.payload_mapping,
            "unadmitted_exact_full_package")
    readback(request.update_exe, request.updater_size, request.updater_sha256)
    readback(request.package, request.package_size, request.package_sha256)
    readback(root / "current/CollectiGenManager.exe", expected=request.current_executable_sha256)
    readback(root / "current/sq.version", expected=request.current_manifest_sha256)
    # Existing lease remains owned by caller. Fresh stale/closure/exclusion checks
    # immediately precede invocation; callback evidence cannot replace that owner.
    require(held(lease, request, verify_lease) == lease_token, "changed_maintenance_lease")
    transaction = receipt(verify_transaction(request), request, "original_transaction_current")
    require(transaction.get("durable_apply_intent_sha256") == request.durable_apply_intent_sha256,
            "durable_original_intent_required")
    closed(verify_closure, request, lease_token)
    review = receipt(native_review(request, argv=args), request, "stock_cli_native_reviewed")
    require(review.get("stock_version") == STOCK_VERSION and review.get("hook_mode") == "All"
            and review.get("elevation_mode") == "none" and review.get("root_writable") is True
            and review.get("prerequisites_ready") is True, "native_root_elevation_or_hook_hold")
    result = executor(args, cwd=str(root), shell=False, allow_elevation=False,
                      transaction_id=request.transaction_id, binding_sha256=request.binding_sha256,
                      lease_token=lease_token, attempt_identity_sha256=request.attempt_identity_sha256)
    receipt(result, request, "process_created")
    require(set(result) == {"status", "transaction_id", "binding_sha256", "process_id", "process_identity_sha256"}
            and type(result["process_id"]) is int and result["process_id"] > 0, "queue_receipt_schema")
    pin(result["process_identity_sha256"])
    # Process-created evidence is not apply/exit/health success. Owner observes
    # actual updater termination and code/data readback under continuous exclusion.
    return result
