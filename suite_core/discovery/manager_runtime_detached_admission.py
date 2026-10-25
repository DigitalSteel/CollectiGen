"""Inactive r4: exact payload entry versus stock generated launcher roles.

No extraction, proof-file installation, signing, SDK, feed, locks or executors.
Existing r2 embedded admission is separate and unchanged; no fallback between it
and this profile. Proof production is unsigned data generation, not admission.
"""
from dataclasses import dataclass
from pathlib import Path
import stat
import zipfile

from .manager_runtime_admission_support import MANAGER, PREFIX, AdmissionPlan, _metadata
from release_contract import (safe_zip_members, sha256_file,
                              verify_release_manifest, ensure_supported_upgrade)
from suite_core.discovery.component_release_signature import (
    verify_component_release, verify_component_artifact, _json,
)
from suite_core.discovery.component_staging import (
    canonical, guard, require, MAX_ARCHIVE, MAX_UNPACKED, MAX_METADATA_BYTES,
)
from suite_core.discovery.offline_component_producer import produce_inventory
from hashlib import sha256

PROFILE = "manager-windows-full-nupkg-detached-v1"
PAYLOAD_ENTRY = "CollectiGenManager.exe"
PACKAGE_STUB = "collectigen.suite-manager_ExecutionStub.exe"
PORTABLE_LAUNCHER = "collectigen.suite-manager.exe"


@dataclass(frozen=True)
class DetachedAdmissionPlan(AdmissionPlan):
    inventory_profile: str = PROFILE

    @property
    def payload_entrypoint(self):
        return PAYLOAD_ENTRY

    @property
    def packaged_execution_stub(self):
        return PACKAGE_STUB

    @property
    def portable_launcher(self):
        return PORTABLE_LAUNCHER

    @property
    def detached_proof_is_payload(self):
        return False


def produce_detached_inventory(archive, *, inventory_profile, expected_version,
                               expected_channel, expected_component=MANAGER,
                               expected_os="windows", expected_architecture="x86_64"):
    """Read final archive; return unsigned canonical proof bytes, never write them.

    Members are read sequentially in bounded chunks. Each actual payload member
    is buffered individually for the unchanged stock byte-tuple inventory
    producer, then discarded. Never buffer/extract the whole archive or execute
    its members. Per-member memory is bounded by MAX_UNPACKED; scale qualification
    remains separate. The stock helper retains path/hash/schema rules unchanged.
    """
    require(inventory_profile == PROFILE, "explicit_detached_profile_required")
    require(expected_component == MANAGER and expected_os == "windows"
            and expected_architecture == "x86_64", "candidate_windows_manager_only")
    require(expected_channel in ("beta", "stable"), "channel_expectation")
    archive = Path(archive)
    guard(archive)
    require(archive.name.endswith("-full.nupkg"), "full_nupkg_required")
    require(archive.stat().st_size <= MAX_ARCHIVE, "archive_size")
    before = sha256_file(archive)
    before_size = archive.stat().st_size
    wrappers = {"[Content_Types].xml", "_rels/.rels", MANAGER.value + ".nuspec"}
    files = []
    profile = None
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        require(0 < len(infos) <= 515, "archive_member_count")
        normalized = safe_zip_members(i.filename for i in infos)
        folded = {p.as_posix().casefold() for p in normalized}
        require(all(parent.as_posix().casefold() not in folded
                    for path in normalized for parent in path.parents
                    if parent.as_posix() != "."), "file_parent_alias")
        require(sum(i.file_size for i in infos) <= MAX_UNPACKED, "unpacked_size")
        actual_wrappers = {i.filename for i in infos if not i.filename.startswith(PREFIX)}
        require(actual_wrappers == wrappers, "wrapper_closure")
        metadata = {}
        for info in infos:
            mode = info.external_attr >> 16
            require(not info.is_dir() and not info.flag_bits & 1
                    and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                    and not info.external_attr & 0x400, "archive_link_special_or_encrypted")
            if not info.filename.startswith(PREFIX):
                require(info.file_size <= MAX_METADATA_BYTES, "wrapper_size")
                continue
            relative = info.filename[len(PREFIX):]
            require(relative != "", "payload_path")
            if relative == "sq.version":
                require(info.file_size <= MAX_METADATA_BYTES, "sq_version_size")
            member = bytearray()
            with z.open(info) as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    member.extend(block)
                    require(len(member) <= info.file_size, "member_bounds")
            require(len(member) == info.file_size, "member_size")
            # Actual bytes, not fabricated placeholder hashes. Stock producer
            # also refuses embedded BOOTSTRAP_INVENTORY, aliases/mutable paths.
            one = produce_inventory(((relative, bytes(member)),),
                component_id=MANAGER, software_version=expected_version,
                os_name="windows", architecture="x86_64")
            profile = _json(one, "member inventory")
            files.extend(profile["files"])
            if relative == "sq.version":
                require(len(member) <= MAX_METADATA_BYTES, "sq_version_size")
                metadata[relative] = bytes(member)
        require(0 < len(files) <= 512, "payload_count")
        names = {row["path"] for row in files}
        require({PAYLOAD_ENTRY, PACKAGE_STUB,
                 "Squirrel.exe", "sq.version"} <= names, "required_generated_payload")
        nuspec = z.read(MANAGER.value + ".nuspec")
        require(metadata["sq.version"] == nuspec, "sq_version_binding")
        fields = _metadata(nuspec)  # exact accepted r2 encoding/DTD policy
        for key, value in {"id": MANAGER.value, "version": expected_version,
                           "channel": expected_channel + "-win-x64", "os": "win",
                           "rid": "win-x64", "machineArchitecture": "x64",
                           "mainExe": PAYLOAD_ENTRY}.items():
            require(fields.get(key) == value, "nuspec_" + key + "_binding")
    guard(archive)
    require(sha256_file(archive) == before and archive.stat().st_size == before_size,
            "changed_archive_during_proof")
    profile["files"] = sorted(files, key=lambda row: row["path"])
    profile["inventory_profile"] = PROFILE
    # Pair pin covers wrapper bytes/ZIP encoding too; proof is external so no
    # self-reference or repacking of stock output is introduced.
    profile["archive_sha256"] = before
    profile["archive_size_bytes"] = before_size
    result = canonical(profile)
    require(len(result) <= MAX_METADATA_BYTES, "inventory_size")
    return result


def admit_detached(archive, *, inventory_profile, detached_inventory_bytes,
                   manifest_bytes, signature_bytes, trust_bytes,
                   expected_trust_sha256, expected_inventory_sha256,
                   expected_version, expected_channel, current_version,
                   expected_component=MANAGER, expected_os="windows",
                   expected_architecture="x86_64"):
    """Exact signed pair admission only. No profile default or embedded fallback."""
    require(inventory_profile == PROFILE, "explicit_detached_profile_required")
    require(sha256(trust_bytes).hexdigest() == expected_trust_sha256,
            "changed_trust_evidence")
    verified = verify_component_release(manifest_bytes, signature_bytes, trust_bytes,
        expected_component=expected_component, expected_version=expected_version,
        expected_channel=expected_channel, expected_os=expected_os,
        expected_architecture=expected_architecture)
    release = verify_release_manifest(manifest_bytes, signature_bytes,
        _json(trust_bytes, "trust"), expected_channel=expected_channel)
    ensure_supported_upgrade(current_version, release)
    _json(detached_inventory_bytes, "detached inventory")  # strict bounded JSON
    require(sha256(detached_inventory_bytes).hexdigest() == expected_inventory_sha256
            == verified.artifact.portable_manifest_sha256, "detached_inventory_pin")
    archive = Path(archive)
    guard(archive)
    require(archive.name == verified.artifact.name, "archive_name_binding")
    verify_component_artifact(archive, verified)
    observed = produce_detached_inventory(archive, inventory_profile=inventory_profile,
        expected_component=expected_component, expected_version=expected_version,
        expected_channel=expected_channel, expected_os=expected_os,
        expected_architecture=expected_architecture)
    require(detached_inventory_bytes == observed, "exact_detached_proof_equality")
    guard(archive)
    verify_component_artifact(archive, verified)
    rows = _json(observed, "observed inventory")["files"]
    return DetachedAdmissionPlan(MANAGER.value, expected_version, expected_channel,
        verified.artifact.sha256, verified.manifest_sha256, expected_trust_sha256,
        expected_inventory_sha256, tuple((PREFIX + row["path"], row["path"]) for row in rows))
