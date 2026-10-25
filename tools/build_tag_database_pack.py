"""Build a qualified local Tag Database candidate using the native Pack codec.

Synthetic wire fixtures and explicitly reviewed local curated candidates remain
separate. Neither mode grants publication clearance, a signature or installation.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import knowledge_pack as native
from suite_core.encyclopedia import publication as source_codec
from suite_core.knowledge.native_tag_catalog_host import bind_foundation_source_release

SOURCE_COMPANION_MEMBER = "metadata/TagDatabase.source-snapshot.json"
CURATED_EVIDENCE_MEMBER = "metadata/TagDatabase.curated-evidence.json"
LOCAL_QUALIFICATION_MEMBER = "metadata/TagDatabase.local-qualification.json"
LOCAL_CHECKS = {"curated_records", "fresh_apply", "selected_recovery", "repeat_noop",
                "independent_source_binding", "fresh_publication"}


def load_local_json(path, *, expected_sha256, maximum):
    """Read only explicitly selected, hash-bound producer evidence."""
    if (type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise ValueError("Exact local evidence SHA-256 required")
    path = Path(path).absolute()
    native._check_export_path(path)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Local producer evidence changed or exceeds its bound")
    return raw, native._foundation_json(raw)


def _local_candidate_inputs(decoded, canonical_pack_sha256, qualification, curated_evidence, inventory_platform):
    # This is a bounded readiness ledger, not acquisition or signature authority.
    # Native wire/release/source binding and immutable component hashes remain
    # their existing owners. The caller owns actual qualification execution.
    keys = {"contract", "pack_sha256", "source_snapshot_fingerprint", "release_fingerprint",
            "source_inventory", "source_evidence_sha256", "checks", "full_execution_qualified"}
    if (type(qualification) is not dict or set(qualification) != keys
            or qualification["contract"] != "tag_database_local_candidate_qualification/1"
            or type(qualification["checks"]) is not dict
            or set(qualification["checks"]) != LOCAL_CHECKS
            or any(value is not True for value in qualification["checks"].values())
            or qualification["full_execution_qualified"] is not False):
        raise ValueError("Completed bounded local qualification required")
    expected_inventory = [list(row) for row in decoded.proposal.source_inventory]
    if (qualification["pack_sha256"] != canonical_pack_sha256
            or qualification["source_snapshot_fingerprint"] != decoded.approved_release.snapshot_fingerprint
            or qualification["release_fingerprint"] != decoded.approved_release.release_fingerprint
            or qualification["source_inventory"] != expected_inventory):
        raise ValueError("Local qualification differs from exact Foundation wire/source")
    if type(curated_evidence) is not bytes or len(curated_evidence) > native.MAX_FOUNDATION_BYTES:
        raise ValueError("Bounded reviewed curated source evidence required")
    if sha256(curated_evidence).hexdigest() != qualification["source_evidence_sha256"]:
        raise ValueError("Curated source evidence differs from local qualification")
    evidence = native._foundation_json(curated_evidence)
    evidence_keys = {"schema_version", "classification", "publication_clearance", "source_receipt_sha256",
                     "projection_policy_sha256", "restoration_proposal_sha256", "documents",
                     "materialized_source_inventory", "shadowed_default_policy_rows"}
    if (type(evidence) is not dict or set(evidence) != evidence_keys
            or type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1
            or evidence["classification"] != "reviewed_local_curated_source_evidence"
            or evidence["publication_clearance"] is not False
            or evidence["materialized_source_inventory"] != expected_inventory):
        raise ValueError("Exact reviewed curated evidence contract required")
    for key in ("source_receipt_sha256", "projection_policy_sha256", "restoration_proposal_sha256"):
        value = evidence[key]
        if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Explicit reviewed source evidence fingerprints required")
    from tools.tag_public_projection_review import SNAPSHOT_FILES, TAG_FILES, review_document
    documents = evidence["documents"]
    if type(documents) is not dict or set(documents) != SNAPSHOT_FILES - {"data/user/default_knowledge_state.json"}:
        raise ValueError("Exact seventeen public curated documents required")
    # Reclassify evidence AND emitted defaults. A preserved unknown native field
    # cannot accidentally become public merely because a portable DTO omits it.
    payloads = [(name, value) for name, value in documents.items()]
    payloads += [(name, native._foundation_json(raw)) for name, raw in decoded.proposal.payloads]
    for name, payload in payloads:
        family = "tags" if name in TAG_FILES else Path(name).stem
        summary = review_document(payload, family).summary()
        if (summary["core_holds"] or any(summary["scope_reviews"].get(key) for key in (
                "citation_privacy_review", "sensitive_text_review", "retain_private_recovery_evidence"))
                or summary["classifications"].get("exclude_observation_or_operator_history")):
            raise ValueError("Unresolved public curated field review")
    from suite_core.installation import _version
    _version(decoded.approved_release.content_version)
    if (type(inventory_platform) is not dict or set(inventory_platform) != {"os", "architecture"}
            or inventory_platform["os"] not in {"windows", "linux"}
            or inventory_platform["architecture"] not in {"x86_64", "aarch64"}):
        raise ValueError("Explicit qualified component transport platform required")


def _write_inventory(directory, decoded, inventory_platform):
    from suite_core.discovery.component_staging import verify_directory, INVENTORY
    from suite_core.installation import ComponentId
    files = []
    for path in sorted(directory.rglob("*")):
        native._check_export_path(path)
        if path.is_file():
            files.append({"path": path.relative_to(directory).as_posix(), "size_bytes": path.stat().st_size,
                          "sha256": sha256(path.read_bytes()).hexdigest()})
    profile = {"schema_version": 1, "component_id": "TagDatabase",
               "software_version": decoded.approved_release.content_version,
               "platform": dict(inventory_platform), "runtime_kind": "frozen-portable", "files": files}
    raw = (json.dumps(profile, sort_keys=True, indent=2) + "\n").encode()
    with (directory / INVENTORY).open("xb") as stream:
        stream.write(raw)
    verify_directory(directory, sha256(raw).hexdigest(), ComponentId("TagDatabase"), decoded.approved_release.content_version)



def load_source_publication(path, *, expected_sha256):
    """Admit hash-bound local producer input with the existing source codec."""
    if (type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)):
        raise ValueError("Exact source publication SHA-256 required")
    path = Path(path).absolute()
    native._check_export_path(path)
    with path.open("rb") as handle:
        raw = handle.read(source_codec.MAX_BYTES + 1)
    if len(raw) > source_codec.MAX_BYTES:
        raise ValueError("Source publication exceeds codec byte bound")
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Source publication differs from producer evidence")
    return source_codec.decode(raw)


def load_projection_inputs(original_defaults, evidence_archive, *, authority):
    """Explicit private validation inputs only; never enumerate/copy to output."""
    from core.tag_public_projection_evidence import FILES, MEMBERS, MAX_BYTES
    original_defaults, evidence_archive = Path(original_defaults).absolute(), Path(evidence_archive).absolute()
    native._check_export_path(evidence_archive)
    originals = {}
    for name in sorted(FILES):
        path = original_defaults / Path(name).name
        native._check_export_path(path)
        with path.open('rb') as stream: originals[name] = stream.read(MAX_BYTES + 1)
    members = {}
    with zipfile.ZipFile(evidence_archive) as archive:
        entries = archive.infolist()
        if len(entries) > 128 or any(sum(x.filename == name for x in entries) != 1 for name in MEMBERS):
            raise ValueError('Exact bounded projection evidence members required')
        for name in sorted(MEMBERS):
            info = archive.getinfo(name)
            if info.file_size > MAX_BYTES or info.flag_bits & 1:
                raise ValueError('Bounded unencrypted projection evidence required')
            with archive.open(name) as stream: members[name] = stream.read(MAX_BYTES + 1)
    return originals, members


def _public_projection_candidate(decoded, canonical_sha, source_publication, qualification,
                                 originals, evidence, authority, platform):
    from core.tag_public_projection_evidence import ProjectionAuthority, validate_projection
    from tools.tag_public_projection_review import TAG_FILES, review_document_v2
    if type(authority) is not ProjectionAuthority:
        raise ValueError('Separate reviewed projection authority required')
    if (canonical_sha != authority.pack_sha256
            or source_publication is None
            or sha256(source_codec.encode(source_publication)).hexdigest() != authority.source_sha256
            or decoded.approved_release.release_fingerprint != authority.release_fingerprint
            or decoded.approved_release.snapshot_fingerprint != authority.snapshot_fingerprint):
        raise ValueError('Exact projection wire/source authority required')
    report = validate_projection(originals, dict(decoded.proposal.payloads), evidence, authority=authority)
    if (type(qualification) is not dict or set(qualification) != {'contract', 'scope', 'pack_sha256',
            'source_snapshot_fingerprint', 'release_fingerprint', 'checks', 'full_execution_qualified'}
            or qualification['contract'] != 'tag_database_public_projection_qualification/1'
            or qualification['scope'] != 'fresh_install_only'
            or qualification['pack_sha256'] != canonical_sha
            or qualification['source_snapshot_fingerprint'] != authority.snapshot_fingerprint
            or qualification['release_fingerprint'] != authority.release_fingerprint
            or type(qualification['checks']) is not dict or set(qualification['checks']) != LOCAL_CHECKS
            or any(value is not True for value in qualification['checks'].values())
            or qualification['full_execution_qualified'] is not False):
        raise ValueError('Completed exact fresh-only projection qualification required')
    if (type(platform) is not dict or set(platform) != {'os', 'architecture'}
            or platform['os'] not in {'windows', 'linux'} or platform['architecture'] not in {'x86_64', 'aarch64'}):
        raise ValueError('Explicit qualified component transport platform required')
    for name, raw in decoded.proposal.payloads:
        summary = review_document_v2(native._foundation_json(raw), 'tags' if name in TAG_FILES else Path(name).stem).summary()
        if (summary['core_holds'] or any(summary['scope_reviews'].get(key) for key in (
                'citation_privacy_review', 'sensitive_text_review', 'retain_private_recovery_evidence'))
                or summary['classifications'].get('exclude_observation_or_operator_history')):
            raise ValueError('Unresolved public projection field review')
    return report


def _companion_bytes(decoded, source_publication):
    if type(source_publication) is not source_codec.Publication:
        raise TypeError("Exact source Publication required")
    raw = source_codec.encode(source_publication)
    checked = source_codec.decode(raw)
    if checked != source_publication:
        raise ValueError("Source publication codec round-trip differs")
    bind_foundation_source_release(decoded, checked.snapshot)
    return raw


def build_tag_database_pack(manifest_path, input_root, output_root, *, synthetic=False,
                            source_publication=None, reviewed_local=False, local_qualification=None,
                            curated_evidence=None, inventory_platform=None, public_projection=False,
                            projection_inputs=None, projection_evidence=None, projection_authority=None):
    """Input is the same schema-10 manifest plus its exact named default files."""
    if sum(value is True for value in (synthetic, reviewed_local, public_projection)) != 1:
        raise ValueError("Explicit synthetic or reviewed local qualification classification required")
    if synthetic is True and reviewed_local is True:
        raise ValueError("Choose exactly one local candidate mode")
    if public_projection is not True and any(value is not None for value in (
            projection_inputs, projection_evidence, projection_authority)):
        raise ValueError('Projection evidence requires explicit versioned mode')
    if (reviewed_local is True or public_projection is True) and source_publication is None:
        raise ValueError("Independent reviewed source companion required for local candidate")
    manifest_path, input_root = Path(manifest_path).absolute(), Path(input_root).absolute()
    native._check_export_path(manifest_path)
    with manifest_path.open("rb") as handle:
        raw = handle.read(native.MAX_FOUNDATION_MANIFEST_BYTES + 1)
    if len(raw) > native.MAX_FOUNDATION_MANIFEST_BYTES:
        raise ValueError("Foundation manifest exceeds its byte bound")
    manifest = native._foundation_json(raw)
    files = manifest.get("files")
    if type(files) is not list or not 0 < len(files) < native.MAX_PACK_FILES:
        raise ValueError("Explicit bounded Foundation file list required")
    # Reconstruct a disposable ZIP, then let the one native codec validate mode,
    # release, inventory, membership, depths and bounds. Never enumerate input_root.
    with tempfile.TemporaryDirectory(prefix="tag-database-preflight-") as temporary:
        candidate = Path(temporary) / "input.zip"
        total = 0
        with zipfile.ZipFile(candidate, "x", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", raw)
            for name in files:
                native._foundation_relative(name)
                path = input_root / name
                native._check_export_path(path)
                with path.open("rb") as handle:
                    payload = handle.read(native.MAX_PACK_UNCOMPRESSED_BYTES + 1)
                total += len(payload)
                if len(payload) > native.MAX_PACK_UNCOMPRESSED_BYTES or total > native.MAX_FOUNDATION_BYTES:
                    raise ValueError("Foundation inputs exceed their byte bound")
                archive.writestr(name, payload)
        decoded = native.load_foundation_pack(candidate)
        public_report = None
        if public_projection is True:
            canonical = native.export_foundation_pack(Path(temporary) / 'canonical.zip',
                proposal=decoded.proposal, approved_release=decoded.approved_release,
                display_version=decoded.display_version, content_rating_boundary=decoded.content_rating_boundary)
            public_report = _public_projection_candidate(decoded, sha256(canonical.read_bytes()).hexdigest(),
                source_publication, local_qualification, projection_inputs, projection_evidence,
                projection_authority, inventory_platform)
        if reviewed_local is True:
            canonical = native.export_foundation_pack(Path(temporary) / "canonical.zip",
                proposal=decoded.proposal, approved_release=decoded.approved_release,
                display_version=decoded.display_version, content_rating_boundary=decoded.content_rating_boundary)
            _local_candidate_inputs(decoded, sha256(canonical.read_bytes()).hexdigest(),
                                    local_qualification, curated_evidence, inventory_platform)
    # Finish source admission before any output directory exists. Old synthetic
    # wire-only fixtures may omit the sidecar; this grants no real-candidate path.
    companion_raw = None if source_publication is None else _companion_bytes(decoded, source_publication)
    component_root = Path(output_root).absolute() / "TagDatabase" / decoded.display_version
    native._check_export_path(component_root)
    if component_root.exists():
        raise FileExistsError("Tag Database candidate destination already exists")
    destination = component_root / "data"
    native._check_export_path(destination)
    if destination.exists():
        raise FileExistsError("Tag Database candidate destination already exists")
    destination.mkdir(parents=True)
    output = native.export_foundation_pack(destination / "TagDatabase.zip", proposal=decoded.proposal,
        approved_release=decoded.approved_release, display_version=decoded.display_version,
        content_rating_boundary=decoded.content_rating_boundary)
    receipt = {"schema_version": 1, "classification": (
        "reviewed_public_projection_candidate" if public_projection is True else
        "reviewed_local_candidate" if reviewed_local is True else "synthetic_qualification_only"),
        "publication_clearance": False, "display_version": decoded.display_version,
        "legacy_content_revision": decoded.approved_release.content_version,
        "pack_sha256": sha256(output.read_bytes()).hexdigest(),
        "release_fingerprint": decoded.approved_release.release_fingerprint,
        "source_inventory": decoded.proposal.source_inventory}
    receipt_raw = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    receipt_path = destination / "BUILD_RECEIPT.json"
    with receipt_path.open("xb") as handle:
        handle.write(receipt_raw)
    if receipt_path.read_bytes() != receipt_raw or native.load_foundation_pack(output) != decoded:
        raise RuntimeError("Tag Database candidate read-back failed")
    if companion_raw is not None:
        # The data directory is the admitted immutable content payload root.
        # Synthetic historical layouts retain their old outer metadata location.
        companion_root = destination if reviewed_local is True or public_projection is True else component_root
        companion = companion_root / SOURCE_COMPANION_MEMBER
        native._check_export_path(companion)
        companion.parent.mkdir()
        with companion.open("xb") as handle:
            handle.write(companion_raw)
        if companion.read_bytes() != companion_raw:
            raise RuntimeError("Source companion byte read-back failed")
        checked = source_codec.read(companion)
        if checked != source_publication:
            raise RuntimeError("Source companion DTO read-back failed")
        bind_foundation_source_release(native.load_foundation_pack(output), checked.snapshot)
        # The existing component inventory owner binds this exact member's bytes.
        # BUILD_RECEIPT intentionally does not become source acquisition authority.
    if public_projection is True:
        # Only the explicit core-generated public summary is emitted. Original
        # documents, omissions ledger and internal reviews remain private inputs.
        report_path = destination / 'metadata/TagDatabase.public-projection.json'
        with report_path.open('xb') as stream:
            stream.write((json.dumps(public_report, sort_keys=True, indent=2)+'\n').encode())
        _write_inventory(destination, decoded, inventory_platform)
    if reviewed_local is True:
        evidence_path = destination / CURATED_EVIDENCE_MEMBER
        qualification_path = destination / LOCAL_QUALIFICATION_MEMBER
        with evidence_path.open("xb") as stream:
            stream.write(curated_evidence)
        qualification_raw = (json.dumps(local_qualification, sort_keys=True, indent=2) + "\n").encode()
        with qualification_path.open("xb") as stream:
            stream.write(qualification_raw)
        if evidence_path.read_bytes() != curated_evidence or qualification_path.read_bytes() != qualification_raw:
            raise RuntimeError("Local candidate evidence read-back failed")
        _write_inventory(destination, decoded, inventory_platform)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--reviewed-local", action="store_true")
    parser.add_argument('--public-projection', action='store_true')
    parser.add_argument('--projection-original-defaults', type=Path)
    parser.add_argument('--projection-evidence-archive', type=Path)
    parser.add_argument('--projection-authority', type=Path)
    parser.add_argument('--projection-authority-sha256')
    parser.add_argument("--local-qualification", type=Path)
    parser.add_argument("--local-qualification-sha256")
    parser.add_argument("--curated-evidence", type=Path)
    parser.add_argument("--curated-evidence-sha256")
    parser.add_argument("--inventory-os", choices=("windows", "linux"))
    parser.add_argument("--inventory-architecture", choices=("x86_64", "aarch64"))
    parser.add_argument("--source-publication", type=Path)
    parser.add_argument("--source-publication-sha256")
    args = parser.parse_args(argv)
    try:
        if (args.source_publication is None) != (args.source_publication_sha256 is None):
            raise ValueError("Source publication path and evidence SHA-256 must be supplied together")
        source = None if args.source_publication is None else load_source_publication(
            args.source_publication, expected_sha256=args.source_publication_sha256)
        qualification = evidence = platform = authority = originals = members = None
        if not args.public_projection and any((args.projection_original_defaults,
                args.projection_evidence_archive, args.projection_authority, args.projection_authority_sha256)):
            raise ValueError('Projection evidence requires explicit versioned mode')
        if args.public_projection:
            from core.tag_public_projection_evidence import ProjectionAuthority
            if not all((args.projection_original_defaults, args.projection_evidence_archive,
                        args.projection_authority, args.projection_authority_sha256,
                        args.local_qualification, args.local_qualification_sha256,
                        args.inventory_os, args.inventory_architecture)):
                raise ValueError('Separately pinned projection authority, private inputs and qualification required')
            _raw, document = load_local_json(args.projection_authority,
                expected_sha256=args.projection_authority_sha256, maximum=native.MAX_FOUNDATION_MANIFEST_BYTES)
            authority = ProjectionAuthority.from_document(document)
            originals, members = load_projection_inputs(args.projection_original_defaults,
                args.projection_evidence_archive, authority=authority)
            _raw, qualification = load_local_json(args.local_qualification,
                expected_sha256=args.local_qualification_sha256, maximum=native.MAX_FOUNDATION_MANIFEST_BYTES)
            platform = {'os': args.inventory_os, 'architecture': args.inventory_architecture}
            if args.curated_evidence or args.curated_evidence_sha256:
                raise ValueError('Legacy curated evidence cannot authorize public projection')
        elif args.reviewed_local:
            if not all((args.local_qualification, args.local_qualification_sha256, args.curated_evidence,
                        args.curated_evidence_sha256, args.inventory_os, args.inventory_architecture)):
                raise ValueError("Reviewed local mode requires exact qualification, curated evidence and transport platform")
            _raw, qualification = load_local_json(args.local_qualification,
                expected_sha256=args.local_qualification_sha256, maximum=native.MAX_FOUNDATION_MANIFEST_BYTES)
            evidence, _checked = load_local_json(args.curated_evidence,
                expected_sha256=args.curated_evidence_sha256, maximum=native.MAX_FOUNDATION_BYTES)
            platform = {"os": args.inventory_os, "architecture": args.inventory_architecture}
        elif any((args.local_qualification, args.local_qualification_sha256, args.curated_evidence,
                  args.curated_evidence_sha256, args.inventory_os, args.inventory_architecture)):
            raise ValueError("Local qualification inputs require reviewed local mode")
        output = build_tag_database_pack(args.manifest, args.input_root, args.output_root,
            synthetic=args.synthetic, source_publication=source, reviewed_local=args.reviewed_local,
            local_qualification=qualification, curated_evidence=evidence, inventory_platform=platform,
            public_projection=args.public_projection, projection_inputs=originals,
            projection_evidence=members, projection_authority=authority)
    except (OSError, TypeError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(2, f"Tag Database candidate held: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
