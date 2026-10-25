"""Offline unsigned inventory production/package-pair proof; no activation or signing."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import tempfile

from release_contract import safe_zip_members, verify_artifact, verify_release_manifest, ensure_supported_upgrade
from suite_core.installation import ComponentId, _relative
from .component_release_signature import VerifiedComponentRelease, verify_component_release, _json, _version
from .component_staging import (INVENTORY, canonical, require,
                                guard, owned, verify_directory)

from .component_payload_policy import select_payload_policy, payload_relative

ADAPTER_VERSION = "0.1.0-alpha.1"


def produce_inventory(members, *, component_id, software_version, os_name, architecture, runtime_kind="frozen-portable"):
    """Hash supplied actual member bytes into the existing unsigned inventory format."""
    require(type(component_id) is ComponentId, "component_id")
    _version(software_version)
    require(os_name in ("windows", "linux") and architecture in ("x86_64", "aarch64"), "platform")
    policy = select_payload_policy(component_id, os_name, architecture, runtime_kind)
    require(type(members) is tuple and 0 < len(members) <= policy.max_inventory_members, "inventory_members")
    seen = {INVENTORY.casefold()}
    files = []
    total = 0
    for member in members:
        require(type(member) is tuple and len(member) == 2, "inventory_member")
        name, content = member
        parts = payload_relative(name, payload_policy=policy)
        require(len(name) <= policy.max_relative_path_characters, "payload_path_bounds")
        require(parts[0].casefold() not in {"data", "models", "catalogs", "saves", "logs", "backups", ".git", ".venv"}, "mutable_package_path")
        require(name.casefold() not in seen and type(content) is bytes, "duplicate_or_invalid_member")
        seen.add(name.casefold())
        total += len(content)
        require(total <= policy.max_unpacked_bytes, "payload_bounds")
        files.append({"path": name, "size_bytes": len(content), "sha256": sha256(content).hexdigest()})
    safe_zip_members([row["path"] for row in files])
    raw = canonical({"schema_version": 1, "component_id": component_id.value,
        "software_version": software_version, "platform": {"os": os_name, "architecture": architecture},
        "runtime_kind": policy.runtime_kind, "files": sorted(files, key=lambda row: row["path"])})
    require(len(raw) <= policy.max_inventory_json_bytes, "inventory_size")
    if policy.runtime_kind == "interpreter-portable":
        require(total + len(raw) <= policy.max_unpacked_bytes, "payload_bounds")
    return raw


@dataclass(frozen=True)
class PackageInput:
    archive: Path
    manifest_bytes: bytes
    signature_bytes: bytes


@dataclass(frozen=True)
class PairEvidence:
    prior: VerifiedComponentRelease
    target: VerifiedComponentRelease
    prior_member_pins: tuple
    target_member_pins: tuple

    @property
    def production_install_authorized(self):
        return False

    @property
    def native_health_verified(self):
        return False


def _package(package, verified, base, policy):
    path = Path(package.archive)
    guard(path)
    require(path.name == verified.artifact.name, "archive_name_binding")
    require(path.stat().st_size <= policy.max_archive_bytes, "archive_size")
    verify_artifact(path, verified.artifact)
    # Only disposable extraction, never Apps/, active.json, executables or user data.
    with tempfile.TemporaryDirectory(prefix="offline-qualification-", dir=base) as temporary:
        directory = Path(temporary)
        from .component_staging import extract_component_archive
        extract_component_archive(path, directory, payload_policy=policy)
        profile = verify_directory(directory, verified.artifact.portable_manifest_sha256,
                                   verified.component_id, verified.software_version, payload_policy=policy)
        require(profile["platform"] == {"os": verified.os, "architecture": verified.architecture}, "inventory_platform")
        pins = tuple(sorted((row["path"], row["size_bytes"], row["sha256"]) for row in profile["files"]))
        verify_artifact(path, verified.artifact)
    return pins


def qualify_pair(prior, target, *, scratch_root, trust_bytes, component_id, prior_version,
                 target_version, os_name, architecture, runtime_kind="frozen-portable"):
    """Authenticate and inspect two archives; prior package proof is not installed-origin proof.

    Explicit marked synthetic roots only. Temporary members are never executed.
    A forged DTO is not an authority token; all installation remains disabled.
    """
    require(type(prior) is PackageInput and type(target) is PackageInput, "package_inputs")
    expectations = dict(expected_component=component_id, expected_os=os_name, expected_architecture=architecture)
    old = verify_component_release(prior.manifest_bytes, prior.signature_bytes, trust_bytes,
        expected_version=prior_version, expected_channel="beta" if "-" in prior_version else "stable", **expectations)
    new = verify_component_release(target.manifest_bytes, target.signature_bytes, trust_bytes,
        expected_version=target_version, expected_channel="beta" if "-" in target_version else "stable", **expectations)
    upgrade = verify_release_manifest(target.manifest_bytes, target.signature_bytes,
                                     _json(trust_bytes, "trust"), expected_channel=new.channel)
    ensure_supported_upgrade(old.software_version, upgrade)
    require(old.artifact.sha256 != new.artifact.sha256, "distinct_archive_required")
    _, base, _ = owned(scratch_root, component_id)
    policy = select_payload_policy(component_id, os_name, architecture, runtime_kind)
    old_pins = _package(prior, old, base, policy)
    new_pins = _package(target, new, base, policy)
    return PairEvidence(old, new, old_pins, new_pins)
