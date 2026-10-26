"""Verify one explicitly selected root release; never launch/install/sign it.

Uses the unchanged release envelope, frozen inventory and bounded extractor.
Root integrity is distinct from reader updates and main-app portable metadata.
"""
import argparse
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
import struct
import sys
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from release_contract import verify_release_manifest, verify_artifact, safe_zip_members, SemVer
from suite_core.installation import ComponentId
from suite_core.discovery.component_staging import (
    guard, read_metadata_bytes, extract_component_archive, verify_directory, INVENTORY)
from suite_core.discovery.component_release_signature import _json, inventory_json
from suite_core.discovery.component_payload_policy import FROZEN_PAYLOAD_POLICY, payload_relative

COMPONENT = ComponentId("collectigen.tag-encyclopedia")
EXECUTABLE = "TagEncyclopedia.exe"
POLICY = FROZEN_PAYLOAD_POLICY
_MUTABLE = frozenset({"data", "apps", "state", "models", "catalogs", "saves", "logs",
    "backups", "training", "output", "cache", ".git", ".venv", "__pycache__"})
_READER_METADATA = frozenset({"portable-package.json", "portable_manifest.json", "module_manifest.json",
    "collectigen-release.json", "collectigen-release.json.sig", "qualification-responses.json",
    "tagencyclopediareader.exe"})
_MODEL_SUFFIXES = {".gguf", ".safetensors", ".ckpt", ".pt", ".sqlite", ".sqlite3", ".db"}


class RootReleaseHold(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise RootReleaseHold(reason)


def _member(name):
    parts = payload_relative(name, payload_policy=POLICY)
    require(not _MUTABLE.intersection(part.casefold() for part in parts), "mutable_root_payload")
    require(parts[-1].casefold() not in _READER_METADATA, "reader_or_fixture_metadata_in_root")
    require(Path(parts[-1]).suffix.casefold() not in _MODEL_SUFFIXES, "model_or_database_payload")
    require(parts[0] != INVENTORY or len(parts) == 1, "inventory_not_at_root")
    return parts


def _root_pe(stream, size):
    """Bounded static DOS/PE machine check; not loader or startup qualification."""
    header = stream.read(64)
    require(len(header) == 64 and header[:2] == b"MZ", "root_executable_dos_header")
    offset = struct.unpack_from("<I", header, 60)[0]
    require(64 <= offset <= 65536 and offset + 26 <= size, "root_executable_pe_bounds")
    stream.read(offset - 64)
    pe = stream.read(24)
    require(len(pe) == 24 and pe[:6] == b"PE\0\0\x64\x86", "root_executable_windows_x86_64")
    optional, flags = struct.unpack_from("<HH", pe, 20)
    require(flags & 2 and not flags & 0x2000 and optional >= 112
        and offset + 24 + optional <= size and stream.read(2) == b"\x0b\x02", "root_executable_pe64_image")


def _preflight_archive(path, artifact, version):
    require(path.stat().st_size <= POLICY.max_archive_bytes, "root_archive_bounds")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        require(0 < len(infos) <= POLICY.max_zip_entries
            and sum(i.file_size for i in infos) <= POLICY.max_unpacked_bytes, "root_archive_bounds")
        for info in infos:
            require(type(info.orig_filename) is str and "\0" not in info.orig_filename
                and "\\" not in info.orig_filename and info.orig_filename == info.filename,
                "root_archive_original_spelling")
            require(not info.is_dir() or info.file_size == 0, "root_archive_directory_payload")
        safe_zip_members(i.filename for i in infos)
        for info in infos:
            _member(info.filename[:-1] if info.is_dir() else info.filename)
            mode = stat.S_IFMT(info.external_attr >> 16)
            require(mode in ((0, stat.S_IFDIR) if info.is_dir() else (0, stat.S_IFREG))
                and not info.flag_bits & 1, "root_archive_link_special_or_encrypted")
        require(INVENTORY in archive.namelist(), "exact_root_inventory_required")
        info = archive.getinfo(INVENTORY)
        require(not info.is_dir() and 0 < info.file_size <= POLICY.max_inventory_json_bytes,
            "root_inventory_bounds")
        raw = archive.read(info)
        require(sha256(raw).hexdigest() == artifact.portable_manifest_sha256, "root_inventory_signed_pin")
        profile = inventory_json(raw)
        require(set(profile) == {"schema_version", "component_id", "software_version", "platform",
            "runtime_kind", "files"} and type(profile["schema_version"]) is int
            and profile["schema_version"] == 1, "root_inventory_schema")
        require(profile["component_id"] == COMPONENT.value and profile["software_version"] == version
            and profile["runtime_kind"] == "frozen-portable", "root_inventory_identity")
        require(profile["platform"] == {"os": "windows", "architecture": "x86_64"}, "root_inventory_platform")
        rows = profile["files"]
        require(type(rows) is list and 0 < len(rows) <= POLICY.max_inventory_members, "root_inventory_files")
        wanted = {INVENTORY}; directories = set()
        for row in rows:
            require(type(row) is dict and set(row) == {"path", "size_bytes", "sha256"}, "root_inventory_row")
            parts = _member(row["path"])
            require(row["path"] not in wanted and row["path"].casefold() not in {n.casefold() for n in wanted},
                "root_inventory_alias")
            require(type(row["size_bytes"]) is int and row["size_bytes"] >= 0
                and type(row["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "root_inventory_pin")
            wanted.add(row["path"])
            directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
        require(EXECUTABLE in wanted and any(n.startswith("_internal/") for n in wanted), "original_root_layout")
        require(not wanted.intersection(directories), "root_file_directory_collision")
        nodes = wanted | directories
        require(len({n.casefold() for n in nodes}) == len(nodes), "root_parent_directory_alias")
        actual_files = {i.filename for i in infos if not i.is_dir()}
        actual_dirs = {i.filename[:-1] for i in infos if i.is_dir()}
        require(actual_files == wanted and actual_dirs <= directories, "root_archive_inventory_closure")
        for row in rows:
            member = archive.getinfo(row["path"])
            require(member.file_size == row["size_bytes"], "root_member_size")
            digest = sha256()
            with archive.open(member) as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
            require(digest.hexdigest() == row["sha256"], "root_member_hash")
        with archive.open(EXECUTABLE) as stream:
            _root_pe(stream, archive.getinfo(EXECUTABLE).file_size)
    return raw, wanted, directories


@dataclass(frozen=True)
class RootVerification:
    version: str
    channel: str
    basename: str
    key_id: str
    manifest_sha256: str
    signature_sha256: str
    trust_sha256: str
    archive_sha256: str
    sidecar_sha256: str
    inventory_sha256: str
    extracted_directory: str
    member_count: int
    execution_qualified: bool = False


def verify_root_release(archive_path, manifest_path, signature_path, sidecar_path, trust_path,
        destination, *, expected_version, expected_channel, expected_basename, expected_trust_sha256):
    """Explicit stable local inputs and fresh retained output; no network/default trust."""
    SemVer.parse(expected_version)
    require(expected_channel in ("stable", "beta"), "explicit_root_channel")
    require(type(expected_basename) is str and expected_basename.startswith("TagEncyclopedia-")
        and expected_basename.endswith("-windows-x64.zip")
        and len(payload_relative(expected_basename)) == 1, "explicit_root_basename")
    require(type(expected_trust_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", expected_trust_sha256),
        "explicit_public_trust_pin")
    paths = tuple(Path(p).absolute() for p in (archive_path, manifest_path, signature_path, sidecar_path, trust_path))
    archive, manifest, signature, sidecar, trust = paths
    for path in paths: guard(path)
    require(archive.name == expected_basename and sidecar.name == expected_basename + ".sha256", "root_filename_binding")
    require(manifest.name == "TagEncyclopedia-root-release.json"
        and signature.name == "TagEncyclopedia-root-release.json.sig", "explicit_root_metadata_names")
    metadata = tuple(read_metadata_bytes(p) for p in paths[1:])
    manifest_raw, signature_raw, sidecar_raw, trust_raw = metadata
    require(sha256(trust_raw).hexdigest() == expected_trust_sha256, "public_trust_pin")
    parsed = _json(manifest_raw, "root manifest")
    require(set(parsed) == {"schema_version", "product", "version", "channel", "published_at",
        "supported_from", "release_notes", "artifacts"} and type(parsed["schema_version"]) is int,
        "root_release_envelope_not_reader_metadata")
    _json(signature_raw, "root signature")
    require(type(parsed["artifacts"]) is list and len(parsed["artifacts"]) == 1, "single_root_artifact")
    declared = parsed["artifacts"][0]
    require(type(declared) is dict and set(declared) == {"name", "kind", "size_bytes", "sha256",
        "portable_manifest_sha256"} and type(declared["size_bytes"]) is int, "root_artifact_schema")
    release = verify_release_manifest(manifest_raw, signature_raw, _json(trust_raw, "public trust"),
        expected_channel=expected_channel)
    require(release.version == expected_version and len(release.artifacts) == 1, "single_expected_root_release")
    artifact = release.artifact("portable-windows-x64")
    require(artifact.name == expected_basename, "root_artifact_name")
    require(sidecar_raw in ((artifact.sha256 + "  " + expected_basename + "\n").encode(),
        (artifact.sha256 + "  " + expected_basename + "\r\n").encode()), "root_checksum_sidecar")
    require(artifact.size_bytes <= POLICY.max_archive_bytes
        and archive.stat().st_size <= POLICY.max_archive_bytes, "root_archive_bounds_before_hashing")
    verify_artifact(archive, artifact)
    inventory_raw, wanted, directories = _preflight_archive(archive, artifact, expected_version)
    verify_artifact(archive, artifact)
    target = Path(destination).absolute(); guard(target)
    require(not target.exists() and target.parent.is_dir(), "fresh_verification_directory_required")
    require(not any(p.is_relative_to(target) for p in paths), "verification_output_input_overlap")
    target.mkdir()  # All archive membership/hash/layout checks precede extraction.
    extract_component_archive(archive, target, payload_policy=POLICY)
    verify_directory(target, artifact.portable_manifest_sha256, COMPONENT, expected_version, payload_policy=POLICY)
    require((target / INVENTORY).read_bytes() == inventory_raw, "inventory_readback")
    observed_files = set(); observed_dirs = set()
    for item in target.rglob("*"):
        guard(item); name = item.relative_to(target).as_posix()
        (observed_dirs if item.is_dir() else observed_files).add(name)
    require(observed_files == wanted and observed_dirs == directories, "extraction_exact_readback")
    verify_artifact(archive, artifact)
    require(tuple(read_metadata_bytes(p) for p in paths[1:]) == metadata, "root_metadata_changed_during_verification")
    return RootVerification(expected_version, expected_channel, expected_basename, release.key_id,
        release.manifest_sha256, sha256(signature_raw).hexdigest(), expected_trust_sha256,
        artifact.sha256, sha256(sidecar_raw).hexdigest(), sha256(inventory_raw).hexdigest(), str(target), len(wanted))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "manifest", "signature", "sidecar", "trust", "destination", "report",
            "expected-version", "expected-channel", "expected-basename", "expected-trust-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    report = Path(args.report).absolute(); guard(report)
    require(not report.exists() and report.parent.is_dir(), "fresh_verification_report_required")
    try:
        result = verify_root_release(args.archive, args.manifest, args.signature, args.sidecar, args.trust,
            args.destination, expected_version=args.expected_version, expected_channel=args.expected_channel,
            expected_basename=args.expected_basename, expected_trust_sha256=args.expected_trust_sha256)
        output = dict(status="ROOT_INTEGRITY_VERIFIED", **asdict(result)); code = 0
    except Exception as exc:
        output = dict(status="ROOT_VERIFICATION_HELD", reason=str(exc), error_type=type(exc).__name__,
            extracted_directory=str(Path(args.destination).absolute()), execution_qualified=False); code = 1
    with report.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, indent=2); stream.write("\n")
    print(json.dumps(output))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
