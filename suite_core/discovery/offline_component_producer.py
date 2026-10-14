"""Offline unsigned inventory production/package-pair proof; no activation or signing."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import stat
import tempfile
import zipfile

from release_contract import safe_zip_members, verify_artifact, verify_release_manifest, ensure_supported_upgrade
from suite_core.installation import ComponentId, _relative
from .component_release_signature import VerifiedComponentRelease, verify_component_release, _json, _version, MAX_METADATA_BYTES
from .component_staging import (INVENTORY, MAX_ARCHIVE, MAX_UNPACKED, canonical, require,
                                guard, owned, verify_directory)

ADAPTER_VERSION = "0.1.0-alpha.1"


def produce_inventory(members, *, component_id, software_version, os_name, architecture):
    """Hash supplied actual member bytes into the existing unsigned inventory format."""
    require(type(component_id) is ComponentId, "component_id")
    _version(software_version)
    require(os_name in ("windows", "linux") and architecture in ("x86_64", "aarch64"), "platform")
    require(type(members) is tuple and 0 < len(members) <= 512, "inventory_members")
    seen = {INVENTORY.casefold()}
    files = []
    total = 0
    for member in members:
        require(type(member) is tuple and len(member) == 2, "inventory_member")
        name, content = member
        parts = _relative(name)
        require(parts[0].casefold() not in {"data", "models", "catalogs", "saves", "logs", "backups", ".git", ".venv"}, "mutable_package_path")
        require(name.casefold() not in seen and type(content) is bytes, "duplicate_or_invalid_member")
        seen.add(name.casefold())
        total += len(content)
        require(total <= MAX_UNPACKED, "payload_bounds")
        files.append({"path": name, "size_bytes": len(content), "sha256": sha256(content).hexdigest()})
    safe_zip_members([row["path"] for row in files])
    raw = canonical({"schema_version": 1, "component_id": component_id.value,
        "software_version": software_version, "platform": {"os": os_name, "architecture": architecture},
        "runtime_kind": "frozen-portable", "files": sorted(files, key=lambda row: row["path"])})
    require(len(raw) <= MAX_METADATA_BYTES, "inventory_size")
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


def _package(package, verified, base):
    path = Path(package.archive)
    guard(path)
    require(path.name == verified.artifact.name, "archive_name_binding")
    require(path.stat().st_size <= MAX_ARCHIVE, "archive_size")
    verify_artifact(path, verified.artifact)
    # Only disposable extraction, never Apps/, active.json, executables or user data.
    with tempfile.TemporaryDirectory(prefix="offline-qualification-", dir=base) as temporary:
        directory = Path(temporary)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            require(0 < len(infos) <= 1024 and sum(i.file_size for i in infos) <= MAX_UNPACKED, "archive_bounds")
            safe_zip_members([i.filename for i in infos])
            for info in infos:
                _relative(info.filename[:-1] if info.is_dir() else info.filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                require(mode in (0, stat.S_IFREG, stat.S_IFDIR) and not info.flag_bits & 1, "archive_link_special_or_encrypted")
                destination = directory / info.filename
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                count = 0
                with archive.open(info) as source, destination.open("xb") as output:
                    while chunk := source.read(65536):
                        count += len(chunk)
                        require(count <= info.file_size, "member_bounds")
                        output.write(chunk)
                require(count == info.file_size, "member_size")
        profile = verify_directory(directory, verified.artifact.portable_manifest_sha256,
                                   verified.component_id, verified.software_version)
        require(profile["platform"] == {"os": verified.os, "architecture": verified.architecture}, "inventory_platform")
        pins = tuple(sorted((row["path"], row["size_bytes"], row["sha256"]) for row in profile["files"]))
        verify_artifact(path, verified.artifact)
    return pins


def qualify_pair(prior, target, *, scratch_root, trust_bytes, component_id, prior_version,
                 target_version, os_name, architecture):
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
    old_pins = _package(prior, old, base)
    new_pins = _package(target, new, base)
    return PairEvidence(old, new, old_pins, new_pins)
