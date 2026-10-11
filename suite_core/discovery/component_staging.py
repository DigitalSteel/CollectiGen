"""Component transaction engine with separate synthetic and owner admission.

The fixture entry still requires its disposable marker. An admitted entry needs
the original host's retained root/idle scope; no default production admission,
downloads, process launch, ZIP execution or user-data migration is supplied here.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4
import zipfile

from release_contract import safe_zip_members, sha256_file, verify_artifact, verify_release_manifest, ensure_supported_upgrade
from suite_core.installation import ComponentId, _relative, _path
from .component_release_signature import verify_component_release, _json, _version, MAX_METADATA_BYTES

ADAPTER_VERSION = "0.1.0-alpha.3"
INVENTORY = "BOOTSTRAP_INVENTORY.json"
MAX_UNPACKED = 256 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024


class StagingHold(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise StagingHold(reason)


def guard(path):
    """Reject existing symlink/reparse ancestors and multiply linked files.

    Check-before-use does not defeat hostile concurrent filesystem replacement;
    production needs platform handle-based ownership checks and process locks.
    """
    path = Path(path)
    require(path.is_absolute(), "absolute_path")
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400, "linked_or_reparse_path")
        require(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1, "multiply_linked_file")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def atomic(path, value):
    atomic_bytes(path, canonical(value))


def atomic_bytes(path, raw):
    guard(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_disposable_root(path):
    """Factory refuses nonempty roots. Do not call on any live installation."""
    path = Path(path)
    guard(path)
    require(not path.exists() or (path.is_dir() and not any(path.iterdir())), "empty_disposable_root_required")
    path.mkdir(parents=True, exist_ok=True)
    marker = {"purpose": "SYNTHETIC_TEST_ONLY", "owner": uuid4().hex}
    atomic(path / ".synthetic-component-root.json", marker)
    return path


def owned(root, component):
    root = Path(root)
    guard(root)
    require(type(component) is ComponentId, "component_id")
    _relative(component.value)
    marker_path = root / ".synthetic-component-root.json"
    guard(marker_path)
    marker = _json(marker_path.read_bytes(), "synthetic root")
    require(set(marker) == {"purpose", "owner"} and marker["purpose"] == "SYNTHETIC_TEST_ONLY"
            and type(marker["owner"]) is str and len(marker["owner"]) == 32
            and all(c in "0123456789abcdef" for c in marker["owner"]), "synthetic_root_required")
    base = root / "State" / component.value
    guard(base)
    base.mkdir(parents=True, exist_ok=True)
    return root, base, marker["owner"]


@contextmanager
def lock(base):
    path = base / "exclusive.lock"
    guard(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StagingHold("component_busy_or_stale_lock") from exc
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()  # crash-killed processes leave this for manual handling


def read_metadata_bytes(path):
    guard(path)
    require(path.stat().st_size <= MAX_METADATA_BYTES, "metadata_size")
    return path.read_bytes()


def read_json(path):
    return _json(read_metadata_bytes(path), "component state")


def payload(root, reference, component, platform=None):
    require(type(reference) is dict and set(reference) == {"component_id", "version", "directory", "inventory_sha256"}, "reference_schema")
    _version(reference["version"])
    require(reference["component_id"] == component.value
            and reference["directory"] == f"Apps/{component.value}/{reference['version']}", "reference_identity")
    _relative(reference["directory"])
    directory = root / reference["directory"]
    guard(directory)
    profile = verify_directory(directory, reference["inventory_sha256"], component, reference["version"])
    if platform is not None:
        require(profile["platform"] == platform, "retained_inventory_platform")
    return directory


def verify_directory(directory, expected_hash, component, version):
    profile_path = directory / INVENTORY
    guard(profile_path)
    require(profile_path.stat().st_size <= MAX_METADATA_BYTES, "inventory_size")
    raw = profile_path.read_bytes()
    require(sha256(raw).hexdigest() == expected_hash, "inventory_hash")
    profile = _json(raw, "component inventory")
    require(set(profile) == {"schema_version", "component_id", "software_version", "platform", "runtime_kind", "files"}
            and type(profile["schema_version"]) is int and profile["schema_version"] == 1, "inventory_schema")
    require(profile["component_id"] == component.value and profile["software_version"] == version
            and profile["runtime_kind"] == "frozen-portable", "inventory_identity")
    require(type(profile["platform"]) is dict and set(profile["platform"]) == {"os", "architecture"}
            and profile["platform"]["os"] in ("windows", "linux")
            and profile["platform"]["architecture"] in ("x86_64", "aarch64"), "inventory_platform")
    files = profile["files"]
    require(type(files) is list and 0 < len(files) <= 512, "inventory_files")
    wanted = {INVENTORY.casefold()}
    for entry in files:
        require(type(entry) is dict and set(entry) == {"path", "size_bytes", "sha256"}, "inventory_file")
        parts = _relative(entry["path"])
        require(parts[0].casefold() not in {"data", "models", "catalogs", "saves", "logs", "backups", ".git", ".venv"}, "mutable_package_path")
        key = entry["path"].casefold()
        require(key not in wanted, "duplicate_inventory_path")
        wanted.add(key)
        file = directory / entry["path"]
        guard(file)
        require(type(entry["size_bytes"]) is int and entry["size_bytes"] >= 0
                and file.is_file() and file.stat().st_size == entry["size_bytes"], "payload_size")
        require(type(entry["sha256"]) is str and sha256_file(file) == entry["sha256"], "payload_hash")
    actual = set()
    for path in directory.rglob("*"):
        guard(path)
        if path.is_file():
            key = path.relative_to(directory).as_posix().casefold()
            require(key not in actual, "case_alias_payload")
            actual.add(key)
    require(actual == wanted, "payload_inventory_closure")
    return profile


def active(root, base, component, platform=None):
    path = base / "active.json"
    value = read_json(path) if path.exists() else None
    if value is not None:
        payload(root, value, component, platform)
    return value


def active_token(value):
    return sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class ComponentStageBinding:
    """Trusted host input, never a serialized receipt or a boolean idle claim.

    The host retains native root exclusion and original reader drain ownership
    for this entire scope. check revalidates those same owners at handoff points.
    No source or frozen normal entry creates this capability automatically.
    """
    root: Path
    owner: str
    check: object
    data_root: Path


def component_origin(root, data_root, component):
    """Stable location identity only; never establishes native admission."""
    root, data = _path(Path(root)), _path(Path(data_root))
    require(type(component) is ComponentId and data != root
            and not root.is_relative_to(data)
            and not data.is_relative_to(root / "Apps")
            and not data.is_relative_to(root / "State"), "component_code_data_boundary")
    identity = {"schema": 1, "purpose": "COMPONENT_TRANSACTION_ORIGIN",
                "component_id": component.value, "code_root": os.path.normcase(str(root)),
                "data_root": os.path.normcase(str(data))}
    return {**identity, "owner": sha256(canonical(identity)).hexdigest()[:32]}


def component_stage_owner(root, data_root, component):
    return component_origin(root, data_root, component)["owner"]


def _check_origin(base, expected, *, enroll=False):
    path = base / "component-origin.json"
    guard(path)
    if path.exists():
        # Retain original bytes; schema/future fields drift cannot be adopted.
        require(read_json(path) == expected, "component_origin_changed")
        return
    require(not any((base / name).exists() for name in
                    ("transaction.json", "pending-transaction.json", "active.json")),
            "unenrolled_component_journal_hold")
    if enroll:
        atomic(path, expected)
        require(read_json(path) == expected, "component_origin_readback")


class ComponentStageAdmission:
    def __init__(self, hold):
        require(callable(hold), "original_root_scope_required")
        self._hold = hold

    @contextmanager
    def transaction(self, component, *, enroll=False):
        require(type(component) is ComponentId, "component_id")
        _relative(component.value)
        with self._hold(component) as binding:
            require(type(binding) is ComponentStageBinding and callable(binding.check), "bound_root_scope_required")
            root = Path(binding.root)
            guard(root)
            require(root.is_dir() and not (root / ".synthetic-component-root.json").exists(), "ordinary_owned_root_required")
            require(type(binding.owner) is str and len(binding.owner) == 32
                    and all(c in "0123456789abcdef" for c in binding.owner), "root_owner_identity")
            expected = component_origin(root, binding.data_root, component)
            require(binding.owner == expected["owner"], "stable_component_owner_required")
            binding.check()
            base = root / "State" / component.value
            guard(base)
            base.mkdir(parents=True, exist_ok=True)
            _check_origin(base, expected, enroll=enroll)
            yield root, base, binding.owner, binding.check
            binding.check()


def stage_and_activate(root, archive_path, *, component, version, os_name, architecture,
                       manifest_bytes, signature_bytes, trust_bytes, expected_active_token,
                       operator_approved=False, idle=False, failpoint=lambda phase: None):
    """Exercise real writes only under the synthetic root marker, never execute ZIP code."""
    require(operator_approved is True and idle is True, "explicit_approval_and_idle_required")
    root, base, owner = owned(root, component)
    return _stage_and_activate(root, base, owner, archive_path, component=component, version=version,
        os_name=os_name, architecture=architecture, manifest_bytes=manifest_bytes,
        signature_bytes=signature_bytes, trust_bytes=trust_bytes,
        expected_active_token=expected_active_token, failpoint=failpoint)


def stage_and_activate_admitted(admission, archive_path, *, component, version, os_name,
        architecture, manifest_bytes, signature_bytes, trust_bytes, expected_active_token,
        failpoint=lambda phase: None, preflight=lambda root, base, owner: None):
    require(type(admission) is ComponentStageAdmission, "original_root_admission_required")
    with admission.transaction(component, enroll=True) as (root, base, owner, check):
        preflight(root, base, owner)
        check()
        def handoff(phase):
            check()
            failpoint(phase)
        return _stage_and_activate(root, base, owner, archive_path, component=component,
            version=version, os_name=os_name, architecture=architecture,
            manifest_bytes=manifest_bytes, signature_bytes=signature_bytes, trust_bytes=trust_bytes,
            expected_active_token=expected_active_token, failpoint=handoff)


def _stage_and_activate(root, base, owner, archive_path, *, component, version, os_name,
        architecture, manifest_bytes, signature_bytes, trust_bytes, expected_active_token, failpoint):
    with lock(base):
        journal_path = base / "transaction.json"
        pending_path = base / "pending-transaction.json"
        require(not pending_path.exists(), "recover_pending_transaction_first")
        previous_bytes = None
        if journal_path.exists():
            previous = read_json(journal_path)
            require(previous.get("phase") in ("healthy", "rolled_back", "abandoned"), "recover_pending_transaction_first")
            previous_bytes = journal_path.read_bytes()
        platform = {"os": os_name, "architecture": architecture}
        before = active(root, base, component, platform)
        require(active_token(before) == expected_active_token, "stale_active_reference")
        if previous_bytes is not None:
            validate_journal(previous, component, owner, trust_bytes)
            prior_reference = previous["target"] if previous["phase"] == "healthy" else previous["before"]
            require(prior_reference == before, "prior_current_reference_mismatch")
        _version(version)
        verified = verify_component_release(manifest_bytes, signature_bytes, trust_bytes,
            expected_component=component, expected_version=version,
            expected_channel="beta" if "-" in version else "stable", expected_os=os_name, expected_architecture=architecture)
        if before is not None:
            upgrade = verify_release_manifest(manifest_bytes, signature_bytes,
                _json(trust_bytes, "trust"), expected_channel=verified.channel)
            ensure_supported_upgrade(before["version"], upgrade)
        archive_path = Path(archive_path)
        guard(archive_path)
        require(archive_path.stat().st_size <= MAX_ARCHIVE, "archive_size")
        verify_artifact(archive_path, verified.artifact)
        target = {"component_id": component.value, "version": version,
                  "directory": f"Apps/{component.value}/{version}", "inventory_sha256": verified.artifact.portable_manifest_sha256}
        destination = root / target["directory"]
        guard(destination)
        require(not destination.exists(), "immutable_version_already_exists")
        stage_name = "staging-" + uuid4().hex
        staging = base / stage_name
        guard(staging)
        journal = {"schema": 2, "owner": owner, "component_id": component.value, "phase": "staging",
            "stage": stage_name, "before": before, "target": target, "os": os_name, "architecture": architecture,
            "manifest": base64.b64encode(manifest_bytes).decode(), "signature": base64.b64encode(signature_bytes).decode(),
            "prior_journal": None}
        require(len(canonical(journal)) <= MAX_METADATA_BYTES, "journal_size")
        atomic(pending_path, journal)
        staging.mkdir()
        failpoint("staging_created")
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            require(0 < len(infos) <= 1024 and sum(i.file_size for i in infos) <= MAX_UNPACKED, "archive_bounds")
            safe_zip_members([i.filename for i in infos])
            for info in infos:
                _relative(info.filename[:-1] if info.is_dir() else info.filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                require(mode in (0, stat.S_IFREG, stat.S_IFDIR) and not info.flag_bits & 1, "archive_link_special_or_encrypted")
            for info in infos:
                destination_file = staging / info.filename
                if info.is_dir():
                    destination_file.mkdir(parents=True, exist_ok=True)
                else:
                    destination_file.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination_file.open("xb") as output:
                        shutil.copyfileobj(source, output)
                failpoint("member_extracted")
        profile = verify_directory(staging, target["inventory_sha256"], component, version)
        require(profile["platform"] == {"os": os_name, "architecture": architecture}, "inventory_platform")
        verify_artifact(archive_path, verified.artifact)
        journal["phase"] = "activation_pending"
        atomic(pending_path, journal)
        failpoint("before_version_publish")
        require(active(root, base, component, platform) == before, "active_changed_before_handoff")
        if previous_bytes is not None:
            require(journal_path.exists() and journal_path.read_bytes() == previous_bytes, "current_journal_changed")
            history = base / ("transaction-" + uuid4().hex + ".json")
            atomic_bytes(history, previous_bytes)  # copy exact bytes; current record stays reachable
            journal["prior_journal"] = {"name": history.name, "sha256": sha256(previous_bytes).hexdigest()}
        else:
            require(not journal_path.exists(), "current_journal_changed")
        require(len(canonical(journal)) <= MAX_METADATA_BYTES, "journal_size")
        atomic(pending_path, journal)
        failpoint("prior_journal_preserved")
        atomic(journal_path, journal)  # failure preserves prior current record atomically
        failpoint("new_journal_established")
        pending_path.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)  # same filesystem; absent immutable destination
        failpoint("version_published")
        require(active(root, base, component, platform) == before, "active_changed_before_commit")
        atomic(base / "active.json", target)
        failpoint("active_reference_replaced")
        journal["phase"] = "activated"
        atomic(journal_path, journal)
        require(active(root, base, component) == target, "activation_readback")
        return target


def finish_or_recover(root, *, component, trust_bytes, health_verified=False, rollback_approved=False):
    """Real metadata recovery/readback; never assert native health on its own."""
    root, base, owner = owned(root, component)
    return _finish_or_recover(root, base, owner, component=component, trust_bytes=trust_bytes,
        health_verified=health_verified, rollback_approved=rollback_approved)


def finish_or_recover_admitted(admission, *, component, trust_bytes,
        health_verified=False, rollback_approved=False, preflight=lambda root, base, owner: None):
    require(type(admission) is ComponentStageAdmission, "original_root_admission_required")
    with admission.transaction(component) as (root, base, owner, check):
        preflight(root, base, owner)
        check()
        return _finish_or_recover(root, base, owner, component=component, trust_bytes=trust_bytes,
            health_verified=health_verified, rollback_approved=rollback_approved)


def _finish_or_recover(root, base, owner, *, component, trust_bytes, health_verified, rollback_approved):
    with lock(base):
        current_journal_path = base / "transaction.json"
        pending_path = base / "pending-transaction.json"
        guard(current_journal_path)
        guard(pending_path)
        current_bytes = read_metadata_bytes(current_journal_path) if current_journal_path.exists() else None
        current_record = _json(current_bytes, "component state") if current_bytes is not None else None
        if current_record is not None:
            validate_journal(current_record, component, owner, trust_bytes)
        journal_path = pending_path if pending_path.exists() else current_journal_path
        j = read_json(journal_path) if journal_path == pending_path or current_record is None else current_record
        validate_journal(j, component, owner, trust_bytes)
        pending_only = journal_path == pending_path and current_bytes != pending_path.read_bytes()
        completed_handoff = journal_path == pending_path and not pending_only
        if journal_path == pending_path and not pending_only:
            journal_path = current_journal_path
        current_path = base / "active.json"
        current = read_json(current_path) if current_path.exists() else None
        require(current == j["before"] or current == j["target"], "unexpected_active_reference")
        if pending_only:
            require(current == j["before"], "pending_active_reference_mismatch")
            require(j["phase"] in ("staging", "activation_pending", "abandoned"), "pending_phase")
            require(health_verified is not True, "pending_health_not_permitted")
            if current_record is not None:
                require(current_record["phase"] in ("healthy", "rolled_back", "abandoned"), "pending_current_phase")
                terminal_reference = (current_record["target"] if current_record["phase"] == "healthy"
                                      else current_record["before"])
                require(terminal_reference == current, "pending_current_reference_mismatch")
                prior_journal_bytes(base, current_record, component, owner, trust_bytes)
        prior_bytes = prior_journal_bytes(base, j, component, owner, trust_bytes)
        if completed_handoff:
            pending_path.unlink()  # validated exact current bytes prove metadata handoff occurred
        if j["phase"] == "healthy":
            require(current == j["target"], "healthy_reference_mismatch")
        if j["phase"] in ("rolled_back", "abandoned"):
            require(current == j["before"], "recovered_reference_mismatch")
        if health_verified is True:
            require(j["phase"] == "activated" and current == j["target"], "health_phase")
            payload(root, current, component)
            j["phase"] = "healthy"
        elif j["phase"] == "healthy" and rollback_approved is not True:
            payload(root, current, component)
            return current
        else:
            if j["before"] is not None:
                payload(root, j["before"], component, {"os": j["os"], "architecture": j["architecture"]})
            staging = base / j["stage"]
            guard(staging)
            if staging.exists():
                for file in staging.rglob("*"):
                    guard(file)
                require(staging.resolve().is_relative_to(root.resolve()) and staging != root, "staging_delete_boundary")
                shutil.rmtree(staging)  # exact journal-owned staging only
            if j["before"] is None:
                if current_path.exists():
                    current_path.unlink()
            else:
                atomic(current_path, j["before"])
            j["phase"] = "abandoned" if current == j["before"] else "rolled_back"
        atomic(journal_path, j)
        result = active(root, base, component)
        require(result == (j["target"] if j["phase"] == "healthy" else j["before"]), "recovery_readback")
        if pending_only:
            require((current_journal_path.read_bytes() if current_journal_path.exists() else None) == current_bytes,
                    "current_journal_changed")
            if current_bytes is None:
                atomic(current_journal_path, j)  # retain idempotent first-transaction recovery
            os.replace(pending_path, base / ("abandoned-pending-" + uuid4().hex + ".json"))
        elif j["phase"] in ("abandoned", "rolled_back") and prior_bytes is not None:
            history = base / ("recovered-transaction-" + uuid4().hex + ".json")
            atomic_bytes(history, current_journal_path.read_bytes())
            atomic_bytes(current_journal_path, prior_bytes)  # restore prior API-visible rollback record
        return result


def validate_journal(j, component, owner, trust_bytes):
    keys = {"schema", "owner", "component_id", "phase", "stage", "before", "target", "os", "architecture", "manifest", "signature"}
    require(type(j.get("schema")) is int and ((j["schema"] == 1 and set(j) == keys)
            or (j["schema"] == 2 and set(j) == keys | {"prior_journal"}))
            and j["owner"] == owner and j["component_id"] == component.value, "journal_ownership_schema")
    require(j["phase"] in ("staging", "activation_pending", "activated", "healthy", "rolled_back", "abandoned"), "journal_phase")
    require(type(j["stage"]) is str and len(j["stage"]) == 40 and j["stage"].startswith("staging-")
            and all(c in "0123456789abcdef" for c in j["stage"][8:]), "journal_stage_identity")
    verified = verify_component_release(base64.b64decode(j["manifest"], validate=True),
        base64.b64decode(j["signature"], validate=True), trust_bytes, expected_component=component,
        expected_version=j["target"]["version"], expected_channel="beta" if "-" in j["target"]["version"] else "stable",
        expected_os=j["os"], expected_architecture=j["architecture"])
    require(j["target"] == {"component_id": component.value, "version": verified.software_version,
        "directory": f"Apps/{component.value}/{verified.software_version}",
        "inventory_sha256": verified.artifact.portable_manifest_sha256}, "journal_target_binding")


def prior_journal_bytes(base, journal, component, owner, trust_bytes):
    prior = journal.get("prior_journal")
    if prior is None:
        return None
    require(type(prior) is dict and set(prior) == {"name", "sha256"}, "prior_journal_schema")
    name = prior["name"]
    require(type(name) is str and len(name) == 49 and name.startswith("transaction-") and name.endswith(".json")
            and all(c in "0123456789abcdef" for c in name[12:44]), "prior_journal_path")
    path = base / name
    previous = read_json(path)
    raw = path.read_bytes()
    require(sha256(raw).hexdigest() == prior["sha256"], "prior_journal_hash")
    validate_journal(previous, component, owner, trust_bytes)
    reference = previous["target"] if previous["phase"] == "healthy" else previous["before"]
    require(previous["phase"] in ("healthy", "abandoned", "rolled_back") and reference == journal["before"],
            "prior_journal_reference")
    return raw
