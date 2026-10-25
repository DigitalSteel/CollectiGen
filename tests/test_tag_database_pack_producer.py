"""Synthetic Foundation wire, native consumer and local data candidate tests."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import zipfile

import pytest

from core import knowledge_pack as native
from suite_core.knowledge.pack_container import validate_manifest
from suite_core.knowledge.pack_preview import inspect_pack_preview
from tools.build_tag_database_pack import build_tag_database_pack
from tests.test_knowledge_pack_records import _foundation_fixture, _foundation_proposal_edit, _read, _write


def prepared(tmp_path):
    _native, root, _carrier, _plan, release, proposal, _before = _foundation_fixture(tmp_path)
    pack = native.export_foundation_pack(tmp_path / "Foundation.zip", proposal=proposal,
        approved_release=release, display_version="a.0.1.1")
    return root, pack, release, proposal


def rewrite(pack, output, mutate=None, extra=None, replace_payload=None):
    with zipfile.ZipFile(pack) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if mutate:
        manifest = json.loads(members["manifest.json"])
        mutate(manifest)
        members["manifest.json"] = json.dumps(manifest).encode()
    if replace_payload:
        members.update(replace_payload)
    if extra:
        members.update(extra)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return output


def catalog_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in (root / "data").rglob("*.json")}


def test_roundtrip_is_exact_deterministic_and_keeps_legacy_revision(tmp_path):
    _root, pack, release, proposal = prepared(tmp_path)
    second = native.export_foundation_pack(tmp_path / "second.zip", proposal=proposal,
        approved_release=release, display_version="a.0.1.1")
    assert pack.read_bytes() == second.read_bytes()
    reordered = native.export_foundation_pack(tmp_path / "reordered.zip",
        proposal=replace(proposal, payloads=tuple(reversed(proposal.payloads))),
        approved_release=release, display_version="a.0.1.1")
    assert pack.read_bytes() == reordered.read_bytes()
    decoded = native.load_foundation_pack(pack)
    assert decoded.proposal == proposal
    assert decoded.approved_release == release
    assert decoded.display_version == "a.0.1.1"
    assert decoded.approved_release.content_version == "synthetic-1"
    with zipfile.ZipFile(pack) as archive:
        assert set(archive.namelist()) == {"manifest.json", *dict(proposal.payloads)}
        for name, raw in proposal.payloads:
            assert archive.read(name) == raw
    with pytest.raises(FileExistsError):
        native.export_foundation_pack(pack, proposal=proposal, approved_release=release, display_version="a.0.1.1")


@pytest.mark.parametrize("defect", ["schema", "mode", "unknown_authority", "missing_inventory",
    "membership", "release", "display", "boundary", "source_hash", "source_bytes",
    "extra_user", "missing_file", "duplicate_file", "duplicate_json", "depth", "nonfinite"])
def test_loader_rejects_malformed_or_mismatched_inputs_without_recipient_write(tmp_path, defect):
    root, pack, _release, _proposal = prepared(tmp_path)
    original = catalog_bytes(root)
    output = tmp_path / "bad.zip"
    mutations = {
        "schema": lambda m: m.update(schema_version=9),
        "mode": lambda m: m["foundation"].update(version=2),
        "unknown_authority": lambda m: m.update(future_authority=True),
        "missing_inventory": lambda m: m["foundation"].update(source_inventory=[]),
        "membership": lambda m: m["files"].pop(),
        "release": lambda m: m["foundation"]["approved_release"].update(content_version="other"),
        "display": lambda m: m["foundation"].update(display_version="a.00.1.1"),
        "boundary": lambda m: m.update(content_rating_boundary="unknown"),
        "source_hash": lambda m: m["foundation"]["source_inventory"][0].__setitem__(1, "0" * 64),
    }
    replacements = {
        "source_bytes": b'{"changed":true}',
        "duplicate_json": b'{"schema_version":1,"schema_version":2}',
        "depth": b'{"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}',
        "nonfinite": b'{"x":NaN}',
    }
    target = "data/defaults/categories.json"
    rewrite(pack, output, mutate=mutations.get(defect),
        extra={"data/user/tags.json": b'{}'} if defect == "extra_user" else None,
        replace_payload={target: replacements[defect]} if defect in replacements else None)
    if defect == "missing_file":
        with zipfile.ZipFile(output) as archive:
            members = [(n, archive.read(n)) for n in archive.namelist() if n != target]
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in members:
                archive.writestr(name, raw)
    if defect == "duplicate_file":
        with zipfile.ZipFile(output, "a") as archive:
            with pytest.warns(UserWarning):
                archive.writestr(target, b'{}')
    with pytest.raises((ValueError, RuntimeError)):
        native.load_foundation_pack(output)
    assert catalog_bytes(root) == original


def test_wire_bounds_are_explicit_and_do_not_expand_ordinary_pack_limit(tmp_path, monkeypatch):
    _root, pack, _release, _proposal = prepared(tmp_path)
    with zipfile.ZipFile(pack) as archive:
        total = sum(row.file_size for row in archive.infolist())
        largest = max(row.file_size for row in archive.infolist())
    monkeypatch.setattr(native, "MAX_FOUNDATION_BYTES", total)
    monkeypatch.setattr(native, "MAX_PACK_UNCOMPRESSED_BYTES", largest)
    assert native.load_foundation_pack(pack)
    monkeypatch.setattr(native, "MAX_PACK_UNCOMPRESSED_BYTES", largest - 1)
    with pytest.raises(ValueError, match="participant"):
        native.load_foundation_pack(pack)
    monkeypatch.setattr(native, "MAX_PACK_UNCOMPRESSED_BYTES", total)
    monkeypatch.setattr(native, "MAX_FOUNDATION_BYTES", total - native.MAX_FOUNDATION_MANIFEST_BYTES - 1)
    with pytest.raises(ValueError, match="large"):
        native.load_foundation_pack(pack)


def test_ordinary_and_partial_readers_reject_foundation_mode_instead_of_empty_patch(tmp_path):
    _root, pack, _release, _proposal = prepared(tmp_path)
    with zipfile.ZipFile(pack) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    with pytest.raises(ValueError, match="Foundation"):
        validate_manifest(manifest)
    with pytest.raises(ValueError, match="Foundation"):
        inspect_pack_preview(pack, source_reference="synthetic")


@pytest.mark.parametrize("fresh", [False, True])
def test_native_wire_apply_preserves_user_bytes_and_defaults_remain_defaults(tmp_path, fresh):
    root, pack, release, proposal = prepared(tmp_path)
    if fresh:
        # A truly initialized blank recipient; source graph is closed for this fixture.
        for filename, key in (("tags.json", "tags"), ("categories.json", "categories"),
            ("characters.json", "characters"), ("series.json", "series"), ("outfits.json", "outfits"),
            ("patterns.json", "patterns"), ("relationships.json", "relationships")):
            _write(root / "data/user" / filename, {"schema_version": 11, key: []})
        _write(root / "data/user/default_knowledge_state.json", {"schema_version": 6, "records": {}})
        # Remove recipient-only override source references from the synthetic source.
        proposal = _foundation_proposal_edit(proposal, "relationships.json",
            lambda v: v.update(relationships=[r for r in v["relationships"] if r["id"] != "retained_override"]))
        # Source-byte binding remains explicit; regenerate the immutable wire.
        pack = native.export_foundation_pack(tmp_path / "blank.zip", proposal=proposal,
            approved_release=release, display_version="a.0.1.1")
    before = catalog_bytes(root)
    with pytest.raises(ValueError, match="explicitly approved"):
        native.plan_knowledge_pack_import(root, pack)
    plan = native.plan_knowledge_pack_import(root, pack, approved_default_release=release)
    assert plan.schema_version == 10 and plan.foundation_proposal == proposal
    assert plan.can_apply, plan.issues
    result = native.import_knowledge_pack(root, pack, plan=plan)
    assert result["foundation"]["participants"] == len(proposal.payloads)
    for name, raw in proposal.payloads:
        assert (root / name).read_bytes() == raw
    for name, raw in before.items():
        if name.parts[:2] == ("data", "user") and name.name != "default_knowledge_state.json":
            assert (root / name).read_bytes() == raw
    assert "fresh_default" not in {r["id"] for r in _read(root / "data/user/tags.json")["tags"]}
    committed = catalog_bytes(root)
    repeat = native.plan_knowledge_pack_import(root, pack, approved_default_release=release)
    assert native.import_knowledge_pack(root, pack, plan=repeat)["backup"]["path"] == ""
    assert catalog_bytes(root) == committed


@pytest.mark.parametrize("drift", ["recipient", "wire", "descriptor"])
def test_stale_wire_recipient_or_descriptor_holds_before_backup(tmp_path, drift):
    root, pack, release, _proposal = prepared(tmp_path)
    plan = native.plan_knowledge_pack_import(root, pack, approved_default_release=release)
    if drift == "recipient":
        path = root / "data/user/tags.json"
        path.write_bytes(path.read_bytes() + b' ')
    elif drift == "wire":
        pack.write_bytes(pack.read_bytes() + b' ')
    else:
        # Predecessor is recipient transition state, not immutable wire metadata.
        release = replace(release, previous_release_fingerprint="0" * 64)
        with pytest.raises(RuntimeError, match="baseline"):
            native.plan_knowledge_pack_import(root, pack, approved_default_release=release)
        return
    original = catalog_bytes(root)
    with pytest.raises(RuntimeError, match="changed"):
        native.import_knowledge_pack(root, pack, plan=plan)
    assert catalog_bytes(root) == original
    assert not (root / "backups/knowledge-pack-import").exists()


def test_native_endpoint_integration_admits_new_restored_and_mapped_relations(tmp_path):
    _native, root, carrier, _plan, release, proposal, _before = _foundation_fixture(tmp_path)
    tracking = _read(root / "data/user/default_knowledge_state.json")
    tracking["tag_lifecycle"] = {"removed_tag_ids": ["blue"], "merged_tag_ids": {}}
    tracking["records"]["tags"]["fresh_default"] = {"local_id": "local_only_synthetic"}
    _write(root / "data/user/default_knowledge_state.json", tracking)
    proposal = _foundation_proposal_edit(proposal, "promoted_tags.json", lambda v: v.update(removed_tag_ids=["blue"]))
    proposal = _foundation_proposal_edit(proposal, "relationships.json", lambda v: v["relationships"].extend([
        {"id": "new-restored", "kind": "identity_alias", "source": "synthetic", "endpoints": [
            {"kind": "alias_text", "id": "restored spelling"}, {"kind": "tag", "id": "blue"}]},
        {"id": "new-mapped", "kind": "identity_alias", "source": "synthetic", "endpoints": [
            {"kind": "alias_text", "id": "mapped spelling"}, {"kind": "tag", "id": "fresh_default"}]}]))
    before = catalog_bytes(root)
    plan = native.plan_knowledge_pack_import(root, carrier, approved_default_release=release, foundation_proposal=proposal)
    assert plan.can_apply
    native.import_knowledge_pack(root, carrier, plan=plan)
    assert (root / "data/user/tags.json").read_bytes() == before[Path("data/user/tags.json")]


def test_direct_native_apply_requalifies_dependency_graph_of_supplied_plan(tmp_path):
    _native, root, carrier, plan, _release, proposal, _before = _foundation_fixture(tmp_path)
    proposal = _foundation_proposal_edit(proposal, "relationships.json", lambda v: v["relationships"].append({
        "id": "unexpected-missing", "kind": "identity_alias", "source": "synthetic", "endpoints": [
            {"kind": "alias_text", "id": "missing spelling"}, {"kind": "tag", "id": "not-present"}]}))
    before = catalog_bytes(root)
    with pytest.raises(ValueError, match="effective dependencies"):
        native.import_knowledge_pack(root, carrier, plan=replace(plan, foundation_proposal=proposal))
    assert catalog_bytes(root) == before
    assert not (root / "backups/knowledge-pack-import").exists()


def test_builder_uses_single_display_data_layout_and_requires_synthetic_scope(tmp_path):
    _root, pack, _release, _proposal = prepared(tmp_path)
    source = tmp_path / "input"
    with zipfile.ZipFile(pack) as archive:
        # Test input is produced by our own synthetic codec, not supplied executable material.
        archive.extractall(source)
    output_root = tmp_path / "dist"
    with pytest.raises(ValueError, match="classification"):
        build_tag_database_pack(source / "manifest.json", source, output_root)
    assert not output_root.exists()
    output = build_tag_database_pack(source / "manifest.json", source, output_root, synthetic=True)
    assert output.relative_to(output_root).as_posix() == "TagDatabase/a.0.1.1/data/TagDatabase.zip"
    receipt = json.loads(output.with_name("BUILD_RECEIPT.json").read_bytes())
    assert receipt["legacy_content_revision"] == "synthetic-1" and receipt["publication_clearance"] is False
    assert receipt["pack_sha256"] == sha256(output.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        build_tag_database_pack(source / "manifest.json", source, output_root, synthetic=True)


def test_builder_bad_source_hash_produces_no_output_tree(tmp_path):
    _root, pack, _release, _proposal = prepared(tmp_path)
    source = tmp_path / "input"
    with zipfile.ZipFile(pack) as archive:
        archive.extractall(source)
    (source / "data/defaults/categories.json").write_bytes(b'{}')
    output_root = tmp_path / "dist"
    with pytest.raises(ValueError, match="inventory"):
        build_tag_database_pack(source / "manifest.json", source, output_root, synthetic=True)
    assert not output_root.exists()


def test_wire_interruption_restart_uses_existing_selected_recovery(tmp_path, monkeypatch):
    root, pack, release, _proposal = prepared(tmp_path)
    before = catalog_bytes(root)
    plan = native.plan_knowledge_pack_import(root, pack, approved_default_release=release)
    stock = native._atomic_bytes
    fired = []
    class Interruption(BaseException):
        pass
    def write_then_interrupt(path, raw):
        stock(path, raw)
        if Path(path).parent == root / "data/defaults" and not fired:
            fired.append(Path(path))
            raise Interruption()
    monkeypatch.setattr(native, "_atomic_bytes", write_then_interrupt)
    with pytest.raises(Interruption):
        native.import_knowledge_pack(root, pack, plan=plan)
    assert fired and catalog_bytes(root) != before
    receipt, = (root / "backups/knowledge-pack-import").glob("*/recovery.json")
    monkeypatch.setattr(native, "_atomic_bytes", stock)
    native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)
    assert catalog_bytes(root) == before
    assert json.loads(receipt.read_bytes())["status"] == "restored"


def test_supplied_wire_plan_cannot_substitute_other_valid_default_bytes(tmp_path):
    root, pack, release, proposal = prepared(tmp_path)
    plan = native.plan_knowledge_pack_import(root, pack, approved_default_release=release)
    substituted = _foundation_proposal_edit(proposal, "categories.json", lambda v: v.update(future="other valid bytes"))
    before = catalog_bytes(root)
    with pytest.raises(ValueError, match="exact wire input"):
        native.import_knowledge_pack(root, pack, plan=replace(plan, foundation_proposal=substituted))
    assert catalog_bytes(root) == before
    assert not (root / "backups/knowledge-pack-import").exists()


@pytest.mark.parametrize("mode", ["ordinary", "foundation"])
def test_public_dispatch_guards_manifest_depth_before_json_probe(tmp_path, mode):
    _native, root, carrier, _plan, release, _proposal, _before = _foundation_fixture(tmp_path)
    prefix = b'{"format":"tagmanager_knowledge_pack","schema_version":' + (b'10' if mode == "foundation" else b'9')
    raw = prefix + b',"nested":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}'
    with zipfile.ZipFile(carrier, "w") as archive:
        archive.writestr("manifest.json", raw)
    before = catalog_bytes(root)
    with pytest.raises(ValueError, match="nesting"):
        native.plan_knowledge_pack_import(root, carrier, approved_default_release=release)
    assert catalog_bytes(root) == before
    assert not (root / "backups/knowledge-pack-import").exists()


def test_source_release_binder_requires_exact_independently_supplied_snapshot(tmp_path):
    from dataclasses import replace
    from suite_core.knowledge.native_tag_catalog_host import bind_foundation_source_release
    root, service, wire, archive_a, archive_b, a, b, empty, proposal = _wire_service_fixture(tmp_path)
    decoded = native.load_foundation_pack(archive_a)
    before = decoded.proposal.payloads, decoded.proposal.source_inventory
    bound = bind_foundation_source_release(decoded, a.revision.snapshot)
    assert bound == a and bound.fingerprint == decoded.approved_release.release_fingerprint
    recipient_predecessor = replace(decoded.approved_release, previous_release_fingerprint="1" * 64)
    assert bind_foundation_source_release(replace(decoded, approved_release=recipient_predecessor),
                                          a.revision.snapshot) == a
    assert (decoded.proposal.payloads, decoded.proposal.source_inventory) == before
    with pytest.raises(ValueError, match="Snapshot.*fingerprint"):
        bind_foundation_source_release(decoded, b.revision.snapshot)
    with pytest.raises(TypeError):
        bind_foundation_source_release(decoded, {})
    with pytest.raises(TypeError):
        bind_foundation_source_release(replace(decoded, proposal=object()), a.revision.snapshot)
    with pytest.raises(TypeError):
        bind_foundation_source_release(replace(decoded, approved_release=object()), a.revision.snapshot)
    recipient = replace(a.revision.snapshot, source=replace(a.revision.snapshot.source, kind="effective_native"))
    with pytest.raises(ValueError, match="reviewed_portable"):
        bind_foundation_source_release(decoded, recipient)
    changed = replace(a.revision.snapshot, source=replace(a.revision.snapshot.source, reference="changed source"))
    with pytest.raises(ValueError, match="Snapshot.*fingerprint"):
        bind_foundation_source_release(decoded, changed)


def _wire_service_fixture(tmp_path):
    from suite_core.catalog_identity_candidate import decode_identity
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    from suite_core.knowledge.tag_transactions import TagCatalogTransactions
    from suite_core.knowledge import Source, Snapshot, EffectiveTag, Category, Lookup
    from suite_core.knowledge.catalog_migration_preview import PackRevision
    from suite_core.database_actions import CatalogRelease
    from suite_core.selected_catalog import SelectedCatalogSession
    from suite_core.encyclopedia.publication import publish, read
    from core.default_catalog_release import ApprovedDefaultPackRelease
    _native, root, carrier, _plan, approval, proposal_b, _before = _foundation_fixture(tmp_path)
    proposal_b = _foundation_proposal_edit(proposal_b, "relationships.json", lambda v:
        v.update(relationships=[row for row in v["relationships"] if row["id"] != "retained_override"]))
    rows = tuple(sorted((p.relative_to(root).as_posix(), p.read_bytes()) for p in (root / "data/defaults").glob("*.json")))
    proposal_a = native.FoundationProposal(1, "synthetic wire prior", tuple((n, sha256(b).hexdigest()) for n, b in rows), rows)
    def release(version, proposal):
        draft = native.plan_knowledge_pack_import(root, carrier, approved_default_release=approval, foundation_proposal=proposal)
        tags = tuple(EffectiveTag(*row) for row in draft.foundation_default_tags)
        categories = tuple(Category(*row) for row in draft.foundation_default_categories)
        snapshot = Snapshot(Source("synthetic wire defaults", version, version, ("colors",), kind="reviewed_portable"),
            tags, categories, tuple(Lookup(t.display, "canonical_name", t.id, (t.id,)) for t in tags))
        return CatalogRelease(PackRevision("defaultTagDatabase", version, snapshot), "2026-10-10",
                              ("Synthetic wire release",), "synthetic wire source")
    a, b = release("wire-A", proposal_a), release("wire-B", proposal_b)
    def wire(version, proposal, release):
        metadata = ApprovedDefaultPackRelease(release.revision.pack_id, release.revision.content_version,
            release.released_on, release.whats_new, release.source_ref, release.revision.snapshot.fingerprint,
            release.fingerprint, None)  # Not a recipient's live transition predecessor.
        return native.export_foundation_pack(tmp_path / (version + ".zip"), proposal=proposal,
            approved_release=metadata, display_version="a.0.1.1" if version == "wire-A" else "a.0.1.2")
    wire_a, wire_b = wire("wire-A", proposal_a, a), wire("wire-B", proposal_b, b)
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    def adapter(proposal):
        return NativeTagCatalogAdapter(root, native=native, catalog_id=identity.publication_catalog_id,
            source_reference="synthetic selected wire recipient", foundation_proposal=proposal)
    host = adapter(native.load_foundation_pack(wire_a).proposal)
    destination = root / "encyclopedia.json"
    publish(host.capture(1), destination, expected_token=None, revision=host.revision, guard=host.guard())
    session = SelectedCatalogSession(read(destination))
    def service(pack):
        decoded = native.load_foundation_pack(pack)
        value = TagCatalogTransactions(adapter(decoded.proposal).host(), session, destination=destination)
        value.reload()
        return value
    empty = replace(a, revision=replace(a.revision, snapshot=replace(a.revision.snapshot, tags=(), lookups=())))
    return root, service, wire, wire_a, wire_b, a, b, empty, proposal_b


@pytest.mark.parametrize("route", ["A_repeat_A", "A_B_repeat_B", "same_release_source_change"])
def test_actual_service_adapter_wire_transitions_and_repeat_source_guards(tmp_path, route):
    from suite_core.database_actions import DatabaseAction
    root, factory, build_wire, wire_a, wire_b, a, b, empty, proposal_b = _wire_service_fixture(tmp_path)
    service = factory(wire_a)
    review = service.preview(DatabaseAction.MERGE_UPDATE, base_release=empty, available_release=a,
                             pack=wire_a, installed=False)
    assert review.can_apply, (review.holds, review.native.issues)
    assert service.apply(review, confirmed_token=review.token).publication is not None
    current, pack, release = service, wire_a, a
    if route != "A_repeat_A":
        current = factory(wire_b)
        review = current.preview(DatabaseAction.MERGE_UPDATE, base_release=a, available_release=b,
                                 pack=wire_b, installed=True)
        assert review.can_apply, (review.holds, review.native.issues)
        assert current.apply(review, confirmed_token=review.token).publication is not None
        pack, release = wire_b, b
    before = catalog_bytes(root)
    receipts = {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")}
    if route == "same_release_source_change":
        changed = _foundation_proposal_edit(proposal_b, "categories.json", lambda v: v.update(future="changed source"))
        changed_wire = build_wire("wire-B-changed", changed, b)
        changed_service = factory(changed_wire)
        with pytest.raises(RuntimeError, match="source|inventory"):
            changed_service.preview(DatabaseAction.MERGE_UPDATE, base_release=b, available_release=b,
                                    pack=changed_wire, installed=True)
    else:
        review = current.preview(DatabaseAction.MERGE_UPDATE, base_release=release, available_release=release,
                                 pack=pack, installed=True)
        assert review.can_apply, (review.holds, review.native.issues)
        assert current.apply(review, confirmed_token=review.token).native_result["backup"]["path"] == ""
    assert catalog_bytes(root) == before
    assert {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")} == receipts


@pytest.fixture
def companion_input(tmp_path):
    from suite_core.encyclopedia.publication import Publication
    root, service, wire, archive_a, archive_b, a, b, empty, proposal = _wire_service_fixture(tmp_path)
    source = tmp_path / "companion-input"
    with zipfile.ZipFile(archive_a) as archive:
        archive.extractall(source)
    publication = Publication("synthetic-independent-source", 7, a.revision.snapshot)
    return source, archive_a, publication, root


def test_builder_companion_uses_metadata_stock_codec_and_exact_native_binding(tmp_path, companion_input):
    from suite_core.encyclopedia import publication as codec
    from suite_core.knowledge.native_tag_catalog_host import bind_foundation_source_release
    source, archive, publication, root = companion_input
    before = catalog_bytes(root)
    output = build_tag_database_pack(source / "manifest.json", source, tmp_path / "dist",
        synthetic=True, source_publication=publication)
    companion = output.parent.parent / "metadata/TagDatabase.source-snapshot.json"
    assert companion.read_bytes() == codec.encode(publication)
    decoded = codec.read(companion)
    assert decoded == publication
    assert bind_foundation_source_release(native.load_foundation_pack(output), decoded.snapshot).revision.snapshot == publication.snapshot
    with zipfile.ZipFile(output) as left, zipfile.ZipFile(archive) as right:
        assert set(left.namelist()) == set(right.namelist())
        assert all(left.read(name) == right.read(name) for name in left.namelist())
    assert catalog_bytes(root) == before
    assert not (output.parent / "TagDatabase.source-snapshot.json").exists()


@pytest.mark.parametrize("change", ["recipient", "snapshot", "type"])
def test_builder_bad_companion_holds_before_any_output(tmp_path, companion_input, change):
    source, archive, publication, root = companion_input
    if change == "recipient":
        publication = replace(publication, snapshot=replace(publication.snapshot,
            source=replace(publication.snapshot.source, kind="effective_native")))
    elif change == "snapshot":
        publication = replace(publication, snapshot=replace(publication.snapshot,
            source=replace(publication.snapshot.source, reference="different source")))
    else: publication = {"snapshot": publication.snapshot}
    output = tmp_path / "dist"
    with pytest.raises((TypeError, ValueError)):
        build_tag_database_pack(source / "manifest.json", source, output,
            synthetic=True, source_publication=publication)
    assert not output.exists()


def test_source_publication_loader_binds_exact_local_producer_bytes(tmp_path, companion_input):
    from suite_core.encyclopedia import publication as codec
    from tools.build_tag_database_pack import load_source_publication
    source, archive, publication, root = companion_input
    path = tmp_path / "source-publication.json"
    raw = codec.encode(publication)
    path.write_bytes(raw)
    assert load_source_publication(path, expected_sha256=sha256(raw).hexdigest()) == publication
    path.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="producer evidence"):
        load_source_publication(path, expected_sha256=sha256(raw).hexdigest())
    path.write_bytes(b'{"interface_version":1,"interface_version":1}')
    with pytest.raises(ValueError):
        load_source_publication(path, expected_sha256=sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="SHA-256"):
        load_source_publication(path, expected_sha256="not a digest")


def test_builder_companion_readback_failure_is_reported(tmp_path, companion_input, monkeypatch):
    from tools import build_tag_database_pack as producer
    source, archive, publication, root = companion_input
    def failed_read(_path):
        raise ValueError("synthetic durable companion read-back failure")
    monkeypatch.setattr(producer.source_codec, "read", failed_read)
    with pytest.raises(ValueError, match="read-back failure"):
        producer.build_tag_database_pack(source / "manifest.json", source, tmp_path / "dist",
            synthetic=True, source_publication=publication)
    # Failed candidate is retained for inspection; no publication/install claimed.
    assert (tmp_path / "dist/TagDatabase/a.0.1.1/metadata/TagDatabase.source-snapshot.json").is_file()


@pytest.fixture
def reviewed_local_input(tmp_path):
    from suite_core.knowledge import Source, Snapshot, EffectiveTag, Category, Lookup
    from suite_core.knowledge.catalog_migration_preview import PackRevision
    from suite_core.database_actions import CatalogRelease
    from suite_core.encyclopedia.publication import Publication
    from core.default_catalog_release import ApprovedDefaultPackRelease
    from tools.build_tag_database_pack import LOCAL_CHECKS
    source=tmp_path/"public-input"
    documents={}
    for layer in ("defaults","user"):
        for family in ("categories","characters","series","outfits","relationships"):
            documents[f"data/{layer}/{family}.json"]={"schema_version":1,family:[]}
    for filename in ("foundation_tags","community_tags","promoted_tags"):
        documents[f"data/defaults/{filename}.json"]={"schema_version":11,"tags":[],"merge_rules":[]}
    documents["data/user/tags.json"]={"schema_version":11,"tags":[],"merge_rules":[]}
    documents["data/defaults/foundation_tags.json"]["tags"]=[{"id":"live","display":"live","category":"cat"}]
    documents["data/defaults/categories.json"]["categories"]=[{"id":"cat","name":"Cat"}]
    documents["data/defaults/concepts.json"]={"schema_version":1,"concepts":[]}
    documents["data/defaults/modifiers.json"]={"schema_version":1,"groups":{}}
    documents["data/defaults/tag_aliases.json"]={"schema_version":1,"tags":[]}
    payloads=tuple(sorted((n,json.dumps(v).encode()) for n,v in documents.items() if n.startswith("data/defaults/")))
    inventory=tuple((n,sha256(raw).hexdigest()) for n,raw in payloads)
    proposal=native.FoundationProposal(1,"synthetic reviewed local fixture",inventory,payloads)
    snapshot=Snapshot(Source("independent synthetic source","r1","a1",("cat",),kind="reviewed_portable"),
        (EffectiveTag("live","live","cat",()),),(Category("cat","Cat","Cat"),),
        (Lookup("live","canonical_name","live",("live",)),))
    release=CatalogRelease(PackRevision("defaultTagDatabase","0.1.1",snapshot),"2026-10-11",("Synthetic local fixture",),"Synthetic source")
    approved=ApprovedDefaultPackRelease("defaultTagDatabase","0.1.1",release.released_on,release.whats_new,release.source_ref,snapshot.fingerprint,release.fingerprint)
    pack=native.export_foundation_pack(tmp_path/"public.zip",proposal=proposal,approved_release=approved,display_version="a.0.1.1")
    for n,raw in payloads:
        p=source/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    with zipfile.ZipFile(pack) as archive:(source/"manifest.json").write_bytes(archive.read("manifest.json"))
    evidence=json.dumps({"schema_version":1,"classification":"reviewed_local_curated_source_evidence","publication_clearance":False,
        "source_receipt_sha256":"1"*64,"projection_policy_sha256":"2"*64,"restoration_proposal_sha256":"3"*64,
        "documents":documents,"materialized_source_inventory":inventory,"shadowed_default_policy_rows":[]}).encode()
    qualification={"contract":"tag_database_local_candidate_qualification/1","pack_sha256":sha256(pack.read_bytes()).hexdigest(),
        "source_snapshot_fingerprint":snapshot.fingerprint,"release_fingerprint":release.fingerprint,"source_inventory":[list(x) for x in inventory],
        "source_evidence_sha256":sha256(evidence).hexdigest(),"checks":{key:True for key in LOCAL_CHECKS},"full_execution_qualified":False}
    return source,pack,Publication("independent-synthetic-authoring",1,snapshot),qualification,evidence


def test_reviewed_local_candidate_has_admitted_data_root_and_bound_source(tmp_path,reviewed_local_input):
    from suite_core.discovery.component_staging import verify_directory,INVENTORY
    from suite_core.installation import ComponentId
    from suite_core.encyclopedia.publication import read
    source,pack,publication,qualification,evidence=reviewed_local_input
    output=build_tag_database_pack(source/"manifest.json",source,tmp_path/"dist",reviewed_local=True,
        source_publication=publication,local_qualification=qualification,curated_evidence=evidence,
        inventory_platform={"os":"windows","architecture":"x86_64"})
    assert output==tmp_path/"dist/TagDatabase/a.0.1.1/data/TagDatabase.zip"
    assert output.read_bytes()==pack.read_bytes()
    companion=output.parent/"metadata/TagDatabase.source-snapshot.json"
    assert read(companion)==publication
    assert (output.parent/"metadata/TagDatabase.curated-evidence.json").read_bytes()==evidence
    profile=verify_directory(output.parent,sha256((output.parent/INVENTORY).read_bytes()).hexdigest(),ComponentId("TagDatabase"),"0.1.1")
    assert {r["path"] for r in profile["files"]}=={"TagDatabase.zip","BUILD_RECEIPT.json",
        "metadata/TagDatabase.source-snapshot.json","metadata/TagDatabase.curated-evidence.json","metadata/TagDatabase.local-qualification.json"}
    receipt=json.loads((output.parent/"BUILD_RECEIPT.json").read_bytes())
    assert receipt["classification"]=="reviewed_local_candidate" and not receipt["publication_clearance"]


@pytest.mark.parametrize("defect",["missing_source","incomplete","full_claim","pack","inventory","snapshot","release","evidence","private","unknown","platform","mixed_modes"])
def test_reviewed_local_gate_holds_before_output(tmp_path,reviewed_local_input,defect):
    import copy
    source,pack,publication,qualification,evidence=reviewed_local_input;qualification=copy.deepcopy(qualification)
    platform={"os":"windows","architecture":"x86_64"};synthetic=False
    if defect=="missing_source":publication=None
    elif defect=="incomplete":qualification["checks"]["selected_recovery"]=False
    elif defect=="full_claim":qualification["full_execution_qualified"]=True
    elif defect=="inventory":qualification["source_inventory"].pop()
    elif defect in {"pack","snapshot","release"}:
        qualification[{"pack":"pack_sha256","snapshot":"source_snapshot_fingerprint","release":"release_fingerprint"}[defect]]="0"*64
    elif defect=="evidence":evidence+=b" "
    elif defect in {"private","unknown"}:
        v=json.loads(evidence);v["documents"]["data/defaults/foundation_tags.json"]["tags"][0]["seen_count" if defect=="private" else "unknown_core_fact"]=5
        evidence=json.dumps(v).encode();qualification["source_evidence_sha256"]=sha256(evidence).hexdigest()
    elif defect=="platform":platform={"os":"any","architecture":"data"}
    elif defect=="mixed_modes":synthetic=True
    output=tmp_path/"dist"
    with pytest.raises((ValueError,TypeError)):
        build_tag_database_pack(source/"manifest.json",source,output,synthetic=synthetic,reviewed_local=True,
            source_publication=publication,local_qualification=qualification,curated_evidence=evidence,inventory_platform=platform)
    assert not output.exists()
# New route cannot borrow legacy self-asserted evidence or create partial output.
def test_public_projection_route_needs_separate_authority_before_output(tmp_path, reviewed_local_input):
    source, _pack, publication, qualification, evidence = reviewed_local_input
    destination = tmp_path/'public-projection-held'
    with pytest.raises(ValueError, match='Separate reviewed projection authority'):
        build_tag_database_pack(source/'manifest.json', source, destination, public_projection=True,
            source_publication=publication, local_qualification=qualification,
            projection_inputs={}, projection_evidence={}, inventory_platform={'os':'windows','architecture':'x86_64'})
    assert not destination.exists()
