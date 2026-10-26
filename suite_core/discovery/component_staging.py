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

from core.component_release_identity import ComponentReleaseIdentity, require_component_maturity_upgrade
from release_contract import ReleaseContractError, ReleaseArtifact, safe_zip_members, sha256_file, verify_artifact, verify_release_manifest, ensure_supported_upgrade
from suite_core.installation import ComponentId, _relative, _path
from .component_release_signature import VerifiedComponentRelease, verify_component_release, _json, _version, MAX_METADATA_BYTES, inventory_json

from .component_payload_policy import require_payload_policy, payload_relative

ADAPTER_VERSION = "0.1.0-alpha.3"
INVENTORY = "BOOTSTRAP_INVENTORY.json"
MAX_UNPACKED = 256 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024


class StagingHold(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise StagingHold(reason)



def _reference_digest(value):
    require(type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value), "reference_digest")
    return value


def component_reference_from_release(release):
    """Pure reference construction from an independently verified release DTO.

    This validates identity consistency, not signature provenance or admission.
    No production caller is wired here; original journal/owner validation remains
    mandatory. Legacy references retain their exact four-key wire shape.
    """
    require(type(release) is VerifiedComponentRelease, "verified_release_dto_required")
    require(type(release.component_id) is ComponentId, "reference_component")
    require(type(release.schema_version) is int and release.schema_version in (1, 2),
            "reference_release_schema")
    require(type(release.software_version) is str, "reference_version")
    _version(release.software_version)
    require(type(release.os) is str and release.os in ("windows", "linux")
            and type(release.architecture) is str and release.architecture in ("x86_64", "aarch64"),
            "reference_release_platform")
    require(type(release.artifact) is ReleaseArtifact and release.artifact.kind == "component-package",
            "reference_component_artifact")
    inventory = _reference_digest(release.artifact.portable_manifest_sha256)
    digest = _reference_digest(release.manifest_sha256)
    component, version = release.component_id.value, release.software_version
    if release.schema_version == 1:
        require(release.maturity is None, "legacy_reference_has_no_maturity")
        channel = "beta" if "-" in version else "stable"
        directory = f"Apps/{component}/{version}"
        reference = dict(component_id=component, version=version,
                         directory=directory, inventory_sha256=inventory)
    else:
        identity = ComponentReleaseIdentity(version, release.maturity)
        channel = "stable" if identity.maturity == "stable" else "beta"
        directory = f"Apps/{component}/schema2/{version}/{identity.maturity}"
        reference = dict(schema_version=2, release_schema=2, component_id=component,
                         version=version, maturity=identity.maturity, directory=directory,
                         inventory_sha256=inventory, release_sha256=digest)
    require(type(release.channel) is str and release.channel == channel, "reference_release_channel")
    _relative(directory)
    return reference


def validate_component_reference(reference, component, *, release):
    """Bind an exact serialized reference to the supplied verified release DTO.

    Return a fresh canonical mapping; never mutate caller data or inspect a path.
    A fabricated DTO is not a signature or original-host ownership capability.
    """
    require(type(component) is ComponentId, "reference_component")
    expected = component_reference_from_release(release)
    require(release.component_id == component, "reference_component_binding")
    require(type(reference) is dict and set(reference) == set(expected), "reference_schema")
    require(all(type(reference[key]) is type(value) for key, value in expected.items()),
            "reference_field_type")
    require(reference == expected, "reference_signed_identity_binding")
    return expected


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


def payload(root, reference, component, platform=None, *, payload_policy=None, release=None):
    if release is not None:
        validate_component_reference(reference, component, release=release)
    else:
        require(type(reference) is dict and set(reference) == {"component_id", "version", "directory", "inventory_sha256"}, "reference_schema")
        _version(reference["version"])
        require(reference["component_id"] == component.value
                and reference["directory"] == f"Apps/{component.value}/{reference['version']}", "reference_identity")
    _relative(reference["directory"])
    directory = root / reference["directory"]
    guard(directory)
    profile = verify_directory(directory, reference["inventory_sha256"], component, reference["version"], payload_policy=payload_policy)
    if release is not None:
        require(profile["platform"] == {"os": release.os, "architecture": release.architecture}, "signed_inventory_platform")
    if platform is not None:
        require(profile["platform"] == platform, "retained_inventory_platform")
    return directory


def verify_directory(directory, expected_hash, component, version, *, payload_policy=None):
    policy = require_payload_policy(payload_policy, component)
    profile_path = directory / INVENTORY
    guard(profile_path)
    require(profile_path.stat().st_size <= policy.max_inventory_json_bytes, "inventory_size")
    raw = profile_path.read_bytes()
    require(sha256(raw).hexdigest() == expected_hash, "inventory_hash")
    profile = inventory_json(raw, max_bytes=policy.max_inventory_json_bytes)
    require(set(profile) == {"schema_version", "component_id", "software_version", "platform", "runtime_kind", "files"}
            and type(profile["schema_version"]) is int and profile["schema_version"] == 1, "inventory_schema")
    require(profile["component_id"] == component.value and profile["software_version"] == version
            and profile["runtime_kind"] == policy.runtime_kind, "inventory_identity")
    require(type(profile["platform"]) is dict and set(profile["platform"]) == {"os", "architecture"}
            and profile["platform"]["os"] in ("windows", "linux")
            and profile["platform"]["architecture"] in ("x86_64", "aarch64"), "inventory_platform")
    require_payload_policy(policy, component, profile["platform"])
    files = profile["files"]
    total = len(raw)
    require(type(files) is list and 0 < len(files) <= policy.max_inventory_members, "inventory_files")
    wanted = {INVENTORY.casefold()}
    for entry in files:
        require(type(entry) is dict and set(entry) == {"path", "size_bytes", "sha256"}, "inventory_file")
        parts = payload_relative(entry["path"], payload_policy=policy)
        require(len(entry["path"]) <= policy.max_relative_path_characters, "payload_path_bounds")
        require(parts[0].casefold() not in {"data", "models", "catalogs", "saves", "logs", "backups", ".git", ".venv"}, "mutable_package_path")
        key = entry["path"].casefold()
        require(key not in wanted, "duplicate_inventory_path")
        wanted.add(key)
        file = directory / entry["path"]
        guard(file)
        require(type(entry["size_bytes"]) is int and entry["size_bytes"] >= 0
                and file.is_file() and file.stat().st_size == entry["size_bytes"], "payload_size")
        total += entry["size_bytes"]
        if policy.runtime_kind == "interpreter-portable":
            require(total <= policy.max_unpacked_bytes, "payload_bounds")
        require(type(entry["sha256"]) is str and sha256_file(file) == entry["sha256"], "payload_hash")
    actual = set()
    for count, path in enumerate(directory.rglob("*"), 1):
        if policy.max_assembly_tree_entries is not None:
            require(count <= policy.max_assembly_tree_entries, "payload_tree_bounds")
            require(len(path.relative_to(directory).as_posix()) <= policy.max_relative_path_characters, "payload_path_bounds")
        guard(path)
        if path.is_file():
            key = path.relative_to(directory).as_posix().casefold()
            require(key not in actual, "case_alias_payload")
            actual.add(key)
    require(actual == wanted, "payload_inventory_closure")
    return profile


def active(root, base, component, platform=None, *, payload_policy=None, release=None):
    path = base / "active.json"
    value = read_json(path) if path.exists() else None
    if value is not None:
        payload(root, value, component, platform, payload_policy=payload_policy, release=release)
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



def _stage_expectations(release_schema, maturity):
    require(type(release_schema) is int and release_schema in (1, 2), "explicit_release_schema")
    require(maturity is None if release_schema == 1 else type(maturity) is str
            and maturity in ("alpha", "beta", "stable"), "explicit_release_maturity")


def _verify_stage_release(manifest_bytes, signature_bytes, trust_bytes, *, component,
                          version, os_name, architecture, release_schema, maturity):
    _stage_expectations(release_schema, maturity)
    channel = ("stable" if maturity == "stable" else "beta") if release_schema == 2 else ("beta" if "-" in version else "stable")
    verified = verify_component_release(manifest_bytes, signature_bytes, trust_bytes,
        expected_component=component, expected_version=version, expected_channel=channel,
        expected_os=os_name, expected_architecture=architecture, expected_maturity=maturity)
    require(verified.schema_version == release_schema, "transaction_release_schema")
    return verified


def _signed_reference_release(reference, journal, base, component, owner, trust_bytes):
    """Resolve a retained selection through the original bounded signed history."""
    require(journal is not None, "independently_signed_prior_component_required")
    for _ in range(32):
        verified = validate_journal(journal, component, owner, trust_bytes, allow_schema2=True)
        if journal["target"] == reference:
            validate_component_reference(reference, component, release=verified)
            return verified
        previous = prior_journal_bytes(base, journal, component, owner, trust_bytes, allow_schema2=True)
        require(previous is not None, "independently_signed_prior_component_required")
        journal = _json(previous, "prior component transaction")
    raise StagingHold("bounded_component_history_required")


def _transaction_active(root, base, component, owner, trust_bytes, journal, platform, policy, *, allow_schema2):
    path = base / "active.json"
    reference = read_json(path) if path.exists() else None
    verified = (_signed_reference_release(reference, journal, base, component, owner, trust_bytes)
                if allow_schema2 and reference is not None else None)
    return active(root, base, component, platform, payload_policy=policy, release=verified)


def _require_stage_upgrade(before, release, manifest_bytes):
    if before is None:
        return
    try:
        require_component_maturity_upgrade(before["version"],
            ComponentReleaseIdentity(release.software_version, release.maturity),
            current_schema=2 if before.get("schema_version") == 2 else 1,
            current_maturity=before.get("maturity"),
            supported_from=_json(manifest_bytes, "component release")["supported_from"])
    except ReleaseContractError as error:
        raise StagingHold(str(error)) from error


def stage_and_activate(root, archive_path, *, component, version, os_name, architecture,
                       manifest_bytes, signature_bytes, trust_bytes, expected_active_token,
                       operator_approved=False, idle=False, failpoint=lambda phase: None, payload_policy=None, release_schema=1, maturity=None):
    """Exercise real writes only under the synthetic root marker, never execute ZIP code."""
    _stage_expectations(release_schema, maturity)
    if release_schema == 2:
        _verify_stage_release(manifest_bytes, signature_bytes, trust_bytes, component=component, version=version,
            os_name=os_name, architecture=architecture, release_schema=release_schema, maturity=maturity)
    require(operator_approved is True and idle is True, "explicit_approval_and_idle_required")
    root, base, owner = owned(root, component)
    return _stage_and_activate(root, base, owner, archive_path, component=component, version=version,
        os_name=os_name, architecture=architecture, manifest_bytes=manifest_bytes,
        signature_bytes=signature_bytes, trust_bytes=trust_bytes,
        expected_active_token=expected_active_token, failpoint=failpoint, payload_policy=payload_policy,
        release_schema=release_schema, maturity=maturity)


def stage_and_activate_admitted(admission, archive_path, *, component, version, os_name,
        architecture, manifest_bytes, signature_bytes, trust_bytes, expected_active_token,
        failpoint=lambda phase: None, preflight=lambda root, base, owner: None, payload_policy=None, release_schema=1, maturity=None):
    _stage_expectations(release_schema, maturity)
    if release_schema == 2:
        _verify_stage_release(manifest_bytes, signature_bytes, trust_bytes, component=component, version=version,
            os_name=os_name, architecture=architecture, release_schema=release_schema, maturity=maturity)
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
            expected_active_token=expected_active_token, failpoint=handoff, payload_policy=payload_policy,
            release_schema=release_schema, maturity=maturity)


def extract_component_archive(archive_path, destination, *, member_extracted=lambda: None, payload_policy=None):
    """Stock bounded exclusive ZIP extraction, with no activation or cleanup.

    Caller owns the guarded empty destination and partial output after failure.
    Callback runs after each member, retaining original staging failpoint timing.
    """
    policy = require_payload_policy(payload_policy)
    archive_path, destination = Path(archive_path), Path(destination)
    guard(archive_path)
    guard(destination)
    require(destination.is_dir() and not any(destination.iterdir()), "empty_extraction_destination_required")
    require(archive_path.stat().st_size <= policy.max_archive_bytes, "archive_size")
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        require(0 < len(infos) <= policy.max_zip_entries and sum(i.file_size for i in infos) <= policy.max_unpacked_bytes, "archive_bounds")
        safe_zip_members([i.filename for i in infos])
        tree_entries = set()
        for info in infos:
            parts = payload_relative(info.filename[:-1] if info.is_dir() else info.filename, payload_policy=policy)
            if policy.max_assembly_tree_entries is not None:
                # Count every file and unique implied parent before any mkdir/open.
                # Case-folded shared parents occupy one filesystem traversal slot.
                for depth in range(1, len(parts) + 1):
                    tree_entries.add("/".join(parts[:depth]).casefold())
                require(len(tree_entries) <= policy.max_assembly_tree_entries, "archive_tree_bounds")
            require(len(info.filename.rstrip("/")) <= policy.max_relative_path_characters, "payload_path_bounds")
            require(not policy.zip_regular_files_only or not info.is_dir(), "archive_regular_files_required")
            mode = stat.S_IFMT(info.external_attr >> 16)
            require(mode in ((0, stat.S_IFREG) if policy.zip_regular_files_only else (0, stat.S_IFREG, stat.S_IFDIR)) and not info.flag_bits & 1, "archive_link_special_or_encrypted")
        for info in infos:
            destination_file = destination / info.filename
            guard(destination_file)
            if info.is_dir():
                destination_file.mkdir(parents=True, exist_ok=True)
            else:
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination_file.open("xb") as output:
                    count = 0
                    while chunk := source.read(65536):
                        count += len(chunk)
                        require(count <= info.file_size, "member_bounds")
                        output.write(chunk)
                    require(count == info.file_size, "member_size")
            member_extracted()


def _stage_and_activate(root, base, owner, archive_path, *, component, version, os_name,
        architecture, manifest_bytes, signature_bytes, trust_bytes, expected_active_token, failpoint, payload_policy=None, release_schema=1, maturity=None):
    policy = require_payload_policy(payload_policy, component, {"os": os_name, "architecture": architecture})
    with lock(base):
        journal_path = base / "transaction.json"
        pending_path = base / "pending-transaction.json"
        require(not pending_path.exists(), "recover_pending_transaction_first")
        previous_bytes = None
        previous = None
        if journal_path.exists():
            previous = read_json(journal_path)
            require(previous.get("phase") in ("healthy", "rolled_back", "abandoned"), "recover_pending_transaction_first")
            previous_bytes = journal_path.read_bytes()
        platform = {"os": os_name, "architecture": architecture}
        before = _transaction_active(root, base, component, owner, trust_bytes, previous, platform, policy, allow_schema2=release_schema == 2)
        require(active_token(before) == expected_active_token, "stale_active_reference")
        if previous_bytes is not None:
            validate_journal(previous, component, owner, trust_bytes, allow_schema2=release_schema == 2)
            prior_reference = previous["target"] if previous["phase"] == "healthy" else previous["before"]
            require(prior_reference == before, "prior_current_reference_mismatch")
        _version(version)
        verified = _verify_stage_release(manifest_bytes, signature_bytes, trust_bytes,
            component=component, version=version, os_name=os_name, architecture=architecture,
            release_schema=release_schema, maturity=maturity)
        if release_schema == 2:
            _require_stage_upgrade(before, verified, manifest_bytes)
        elif before is not None:
            upgrade = verify_release_manifest(manifest_bytes, signature_bytes,
                _json(trust_bytes, "trust"), expected_channel=verified.channel)
            ensure_supported_upgrade(before["version"], upgrade)
        archive_path = Path(archive_path)
        guard(archive_path)
        require(archive_path.stat().st_size <= policy.max_archive_bytes, "archive_size")
        verify_artifact(archive_path, verified.artifact)
        target = {"component_id": component.value, "version": version,
                  "directory": f"Apps/{component.value}/{version}", "inventory_sha256": verified.artifact.portable_manifest_sha256}
        if release_schema == 2:
            target = component_reference_from_release(verified)
        destination = root / target["directory"]
        guard(destination)
        reuse = destination.exists()
        if reuse:
            require(release_schema == 1, "immutable_version_already_exists")
            require(previous_bytes is not None and before != target,
                    "immutable_version_already_exists")
            profile = verify_directory(destination, target["inventory_sha256"], component, version, payload_policy=policy)
            require(profile["platform"] == platform, "inventory_platform")
        stage_name = "staging-" + uuid4().hex
        staging = base / stage_name
        guard(staging)
        journal = {"schema": 3 if release_schema == 2 else 2, "owner": owner, "component_id": component.value, "phase": "staging",
            "stage": stage_name, "before": before, "target": target, "os": os_name, "architecture": architecture,
            "manifest": base64.b64encode(manifest_bytes).decode(), "signature": base64.b64encode(signature_bytes).decode(),
            "prior_journal": None}
        require(len(canonical(journal)) <= MAX_METADATA_BYTES, "journal_size")
        atomic(pending_path, journal)
        if not reuse:
            staging.mkdir()
        failpoint("staging_created")
        if not reuse:
            extract_component_archive(archive_path, staging,
                member_extracted=lambda: failpoint("member_extracted"), payload_policy=policy)
        profile = verify_directory(destination if reuse else staging, target["inventory_sha256"], component, version, payload_policy=policy)
        require(profile["platform"] == {"os": os_name, "architecture": architecture}, "inventory_platform")
        verify_artifact(archive_path, verified.artifact)
        journal["phase"] = "activation_pending"
        atomic(pending_path, journal)
        failpoint("before_version_publish")
        require(_transaction_active(root, base, component, owner, trust_bytes, previous, platform, policy, allow_schema2=release_schema == 2) == before, "active_changed_before_handoff")
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
        if reuse:
            # Never overwrite retained code. Recheck exact signed bytes after
            # journal publication and immediately before switching authority.
            profile = verify_directory(destination, target["inventory_sha256"], component, version, payload_policy=policy)
            require(profile["platform"] == platform, "inventory_platform")
        else:
            os.replace(staging, destination)  # same filesystem; absent immutable destination
        failpoint("version_published")
        require(_transaction_active(root, base, component, owner, trust_bytes, previous, platform, policy, allow_schema2=release_schema == 2) == before, "active_changed_before_commit")
        atomic(base / "active.json", target)
        failpoint("active_reference_replaced")
        journal["phase"] = "activated"
        atomic(journal_path, journal)
        require(active(root, base, component, payload_policy=policy, release=verified if release_schema == 2 else None) == target, "activation_readback")
        return target


def finish_or_recover(root, *, component, trust_bytes, health_verified=False, rollback_approved=False, payload_policy=None, allow_schema2=False):
    """Real metadata recovery/readback; never assert native health on its own."""
    require(type(allow_schema2) is bool, "explicit_schema2_recovery_mode")
    root, base, owner = owned(root, component)
    return _finish_or_recover(root, base, owner, component=component, trust_bytes=trust_bytes,
        health_verified=health_verified, rollback_approved=rollback_approved, payload_policy=payload_policy, allow_schema2=allow_schema2)


def finish_or_recover_admitted(admission, *, component, trust_bytes,
        health_verified=False, rollback_approved=False, preflight=lambda root, base, owner: None, payload_policy=None, allow_schema2=False):
    require(type(allow_schema2) is bool, "explicit_schema2_recovery_mode")
    require(type(admission) is ComponentStageAdmission, "original_root_admission_required")
    with admission.transaction(component) as (root, base, owner, check):
        preflight(root, base, owner)
        check()
        return _finish_or_recover(root, base, owner, component=component, trust_bytes=trust_bytes,
            health_verified=health_verified, rollback_approved=rollback_approved, payload_policy=payload_policy, allow_schema2=allow_schema2)


def _finish_or_recover(root, base, owner, *, component, trust_bytes, health_verified, rollback_approved, payload_policy=None, allow_schema2=False):
    policy = require_payload_policy(payload_policy, component)
    with lock(base):
        current_journal_path = base / "transaction.json"
        pending_path = base / "pending-transaction.json"
        guard(current_journal_path)
        guard(pending_path)
        current_bytes = read_metadata_bytes(current_journal_path) if current_journal_path.exists() else None
        current_record = _json(current_bytes, "component state") if current_bytes is not None else None
        if current_record is not None:
            validate_journal(current_record, component, owner, trust_bytes, allow_schema2=allow_schema2)
        journal_path = pending_path if pending_path.exists() else current_journal_path
        j = read_json(journal_path) if journal_path == pending_path or current_record is None else current_record
        target_release = validate_journal(j, component, owner, trust_bytes, allow_schema2=allow_schema2)
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
                prior_journal_bytes(base, current_record, component, owner, trust_bytes, allow_schema2=allow_schema2)
        prior_bytes = prior_journal_bytes(base, j, component, owner, trust_bytes, allow_schema2=allow_schema2)
        before_release = None
        if allow_schema2 and j["before"] is not None:
            origin = j if j.get("prior_journal") is not None else (current_record if pending_only else None)
            before_release = _signed_reference_release(j["before"], origin, base, component, owner, trust_bytes)
        current_release = (target_release if current == j["target"] else before_release) if allow_schema2 else None
        if completed_handoff:
            pending_path.unlink()  # validated exact current bytes prove metadata handoff occurred
        if j["phase"] == "healthy":
            require(current == j["target"], "healthy_reference_mismatch")
        if j["phase"] in ("rolled_back", "abandoned"):
            require(current == j["before"], "recovered_reference_mismatch")
        if health_verified is True:
            require(j["phase"] == "activated" and current == j["target"], "health_phase")
            payload(root, current, component, payload_policy=policy, release=current_release)
            j["phase"] = "healthy"
        elif j["phase"] == "healthy" and rollback_approved is not True:
            payload(root, current, component, payload_policy=policy, release=current_release)
            return current
        else:
            if j["before"] is not None:
                payload(root, j["before"], component, {"os": j["os"], "architecture": j["architecture"]}, payload_policy=policy, release=before_release)
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
        result = active(root, base, component, payload_policy=policy,
            release=(target_release if j["phase"] == "healthy" else before_release) if allow_schema2 else None)
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


def validate_journal(j, component, owner, trust_bytes, *, allow_schema2=False):
    require(type(allow_schema2) is bool, "explicit_schema2_recovery_mode")
    require(type(j) is dict, "journal_ownership_schema")
    keys = {"schema", "owner", "component_id", "phase", "stage", "before", "target", "os", "architecture", "manifest", "signature"}
    require(type(j.get("schema")) is int and ((j["schema"] == 1 and set(j) == keys)
            or (j["schema"] in ((2, 3) if allow_schema2 else (2,)) and set(j) == keys | {"prior_journal"}))
            and j["owner"] == owner and j["component_id"] == component.value, "journal_ownership_schema")
    require(j["phase"] in ("staging", "activation_pending", "activated", "healthy", "rolled_back", "abandoned"), "journal_phase")
    require(type(j["stage"]) is str and len(j["stage"]) == 40 and j["stage"].startswith("staging-")
            and all(c in "0123456789abcdef" for c in j["stage"][8:]), "journal_stage_identity")
    require(type(j["target"]) is dict, "journal_target_binding")
    schema = 2 if j["schema"] == 3 else 1
    maturity = j["target"].get("maturity") if schema == 2 else None
    verified = _verify_stage_release(base64.b64decode(j["manifest"], validate=True),
        base64.b64decode(j["signature"], validate=True), trust_bytes, component=component,
        version=j["target"]["version"], release_schema=schema, maturity=maturity,
        os_name=j["os"], architecture=j["architecture"])
    if schema == 2:
        validate_component_reference(j["target"], component, release=verified)
    else:
        require(j["target"] == {"component_id": component.value, "version": verified.software_version,
            "directory": f"Apps/{component.value}/{verified.software_version}",
            "inventory_sha256": verified.artifact.portable_manifest_sha256}, "journal_target_binding")
    return verified



def prior_journal_bytes(base, journal, component, owner, trust_bytes, *, allow_schema2=False):
    require(type(allow_schema2) is bool, "explicit_schema2_recovery_mode")
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
    validate_journal(previous, component, owner, trust_bytes, allow_schema2=allow_schema2)
    reference = previous["target"] if previous["phase"] == "healthy" else previous["before"]
    require(previous["phase"] in ("healthy", "abandoned", "rolled_back") and reference == journal["before"],
            "prior_journal_reference")
    return raw
