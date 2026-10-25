"""Foundation review/native service seam with audited synthetic recipients only."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sys

root, fixture, scenario = Path(sys.argv[1]), Path(sys.argv[2]).resolve(), sys.argv[3]
sys.argv = [str(root / "suite_core/tests/native_identity_transaction_fixture.py"), str(root), str(fixture), "enroll"]
ctx = runpy.run_path(sys.argv[0], run_name="bootstrap")
native, helpers = ctx["native"], ctx["helpers"]
from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
from suite_core.knowledge.tag_transactions import TagCatalogTransactions
from suite_core.knowledge import Source, Snapshot, EffectiveTag, Category, Lookup
from suite_core.knowledge.catalog_migration_preview import PackRevision, Assignment
from suite_core.database_actions import CatalogRelease, DatabaseAction
from suite_core.selected_catalog import SelectedCatalogSession
from suite_core.encyclopedia.publication import publish, read

native, selected, pack, draft, approved, proposal_b, before = helpers._foundation_fixture(fixture / "foundation")
conflicting_proposal = proposal_b
proposal_b = helpers._foundation_proposal_edit(proposal_b, "relationships.json", lambda v:
    v.update(relationships=[row for row in v["relationships"] if row["id"] != "retained_override"]))
if scenario == "effective_review_hold": proposal_b = conflicting_proposal
if scenario == "raw_override_review_hold":
    proposal_b = helpers._foundation_proposal_edit(proposal_b, "foundation_tags.json", lambda v:
        next(row for row in v["tags"] if row["id"] == "red").update(display="updated default red"))
identity = ctx["decode_identity"]((selected / "data/user/catalog_identity.json").read_bytes())
if scenario == "effective_review_hold":
    # A real compatibility gap: a default-only prior definition is absent from
    # B without a redirect/removal. Whole stored user ownership does not admit
    # deleting this retained prior row or weaken strict native equality.
    path = selected / "data/defaults/foundation_tags.json"
    payload = helpers._read(path)
    payload["tags"].append({"id": "prior_default_only", "display": "prior default only", "category": "colors"})
    helpers._write(path, payload)
rows = tuple(sorted((p.relative_to(selected).as_posix(), p.read_bytes()) for p in (selected / "data/defaults").glob("*.json")))
proposal_a = native.FoundationProposal(1, "generated prior source", tuple((n, sha256(b).hexdigest()) for n,b in rows), rows)

def release(version, proposal):
    draft = native.plan_knowledge_pack_import(selected, pack, approved_default_release=approved, foundation_proposal=proposal)
    assert draft.can_apply, draft.issues
    tags = tuple(EffectiveTag(*row) for row in draft.foundation_default_tags)
    categories = tuple(Category(*row) for row in draft.foundation_default_categories)
    lookups = tuple(Lookup(t.display, "canonical_name", t.id, (t.id,)) for t in tags)
    snapshot = Snapshot(Source("generated reviewed defaults", version, version, ("colors",), kind="reviewed_portable"), tags, categories, lookups)
    return CatalogRelease(PackRevision("defaultTagDatabase", version, snapshot), "2026-10-10", ("Generated Foundation update",), "generated source approval")

def bind(proposal):
    return NativeTagCatalogAdapter(selected, native=native, catalog_id=identity.publication_catalog_id,
        source_reference="generated selected recipient", foundation_proposal=proposal)

host_a = bind(proposal_a)
initial = host_a.capture(1)
destination = selected / "encyclopedia.json"
publish(initial, destination, expected_token=None, revision=host_a.revision, guard=host_a.guard())
session = SelectedCatalogSession(read(destination))
service = TagCatalogTransactions(host_a.host(), session, destination=destination)
if scenario == "reader_recipe_migration":
    legacy_destination = selected / "legacy-encyclopedia.json"
    authority_revision = initial.snapshot.source.authority_fingerprint
    assert host_a.revision() != authority_revision
    legacy = replace(initial, snapshot=replace(initial.snapshot,
        source=replace(initial.snapshot.source, revision=authority_revision)))
    native_before = {p:p.read_bytes() for p in (selected / "data").rglob("*.json")}
    legacy_token = publish(legacy, legacy_destination, expected_token=None,
        revision=lambda: authority_revision, guard=host_a.guard())
    legacy_session = SelectedCatalogSession(read(legacy_destination))
    migration = TagCatalogTransactions(host_a.host(), legacy_session, destination=legacy_destination)
    migrated = migration.reload()
    assert migrated.generation == legacy.generation + 1 and migrated.token != legacy_token
    assert migrated.snapshot.source.revision == host_a.revision()
    assert migrated.snapshot.source.authority_fingerprint == authority_revision
    assert migrated.snapshot.tags == legacy.snapshot.tags
    assert native_before == {p:p.read_bytes() for p in native_before}
    assert not list((selected / "backups/knowledge-pack-import").glob("*/recovery.json"))
    assert migration.reload() == migrated
    print(json.dumps({"scenario":scenario, "passed":True, "native_files":True}))
    sys.exit(0)

a = release("A", proposal_a)
b = release("B", proposal_b)
empty = replace(a, revision=PackRevision(a.revision.pack_id, "none", replace(a.revision.snapshot, tags=(), lookups=())))
assignments = (Assignment("image:tag", "blue", "original blue evidence", "generated original assignment"),)
r = service.preview(DatabaseAction.MERGE_UPDATE, base_release=empty, available_release=a, pack=pack, installed=False, assignments=assignments)
assert r.can_apply, (r.holds, r.native.issues)
assert r.foundation and all(row.outcome == "unchanged" for row in r.foundation.participants)
assert not r.native.record_patches and r.database.can_apply is False
result = service.apply(r, confirmed_token=r.token)
assert result.publication is not None, result.publication_error
assert r.database.proposal.preview.assignments == assignments
# Retain native user correction, opaque history, tombstones and override relation.
user = selected / "data/user/tags.json"
value = helpers._read(user)
next(t for t in value["tags"] if t["id"] == "blue")["display"] = "personal blue"
helpers._write(user, value)
service.reload()
host_b = bind(proposal_b)
service = TagCatalogTransactions(host_b.host(), session, destination=destination)
service.reload()  # explicitly refresh the newly bound complete participant revision
def preview():
    return service.preview(DatabaseAction.MERGE_UPDATE, base_release=a, available_release=b, pack=pack, installed=True, assignments=assignments)
r = preview()
if scenario == "effective_review_hold":
    assert r.native.can_apply and not r.can_apply and any("effective native view" in h for h in r.holds), (r.holds, r.native.issues)
    receipts = {p:p.read_bytes() for p in (selected / "backups/knowledge-pack-import").glob("*/recovery.json")}
    retained = user.read_bytes(), destination.read_bytes()
    try: service.apply(r, confirmed_token=r.token)
    except RuntimeError: pass
    else: raise AssertionError("Applied a conflicting effective review")
    assert retained == (user.read_bytes(), destination.read_bytes())
    assert {p:p.read_bytes() for p in receipts} == receipts
    print(json.dumps({"scenario":scenario, "passed":True, "native_files":True}))
    sys.exit(0)
assert r.can_apply, (r.holds, r.native.issues)
if scenario == "raw_override_review_hold":
    # Equal-to-old fields in a stored user row still retain whole-record native
    # authority; the new default display must not overwrite red during upgrade.
    stored_red = next(row for row in value["tags"] if row["id"] == "red")
    reviewed_red = next(row for row in r.database.proposal.preview.records if row.id == "red")
    assert reviewed_red.display == stored_red["display"] == "red"
    assert reviewed_red in tuple(EffectiveTag(*row) for row in r.native.foundation_effective_tags)
    user_before = user.read_bytes()
    result = service.apply(r, confirmed_token=r.token)
    assert result.publication is not None and user.read_bytes() == user_before
    assert next(row for row in read(destination).snapshot.tags if row.id == "red").display == "red"
    assert r.database.proposal.preview.assignments == assignments
    print(json.dumps({"scenario":scenario, "passed":True, "native_files":True}))
    sys.exit(0)
assert any(row.outcome == "add_default" for row in r.foundation.participants)
assert any(row.outcome == "replace_default" for row in r.foundation.participants)
assert r.database.protected_edits and r.database.proposal.preview.assignments == assignments
user_before, published_before = user.read_bytes(), destination.read_bytes()

def hold(action, match=""):
    try: action()
    except (RuntimeError, ValueError, TypeError) as error:
        assert not match or match in str(error), str(error)
        return
    raise AssertionError("Expected safe hold")

if scenario == "mismatched_review":
    understated = replace(b, revision=replace(b.revision, snapshot=replace(b.revision.snapshot, tags=a.revision.snapshot.tags, lookups=a.revision.snapshot.lookups)))
    r = service.preview(DatabaseAction.MERGE_UPDATE, base_release=a, available_release=understated, pack=pack, installed=True, assignments=assignments)
    assert not r.can_apply and any("default review surface" in h for h in r.holds), r.holds
    hold(lambda: service.apply(r, confirmed_token=r.token))
    assert user.read_bytes() == user_before and destination.read_bytes() == published_before
elif scenario == "seal":
    source = r.native.foundation_proposal
    raw = source.payloads
    object.__setattr__(source, "payloads", ((raw[0][0], raw[0][1] + b" "), *raw[1:]))
    hold(lambda: service.apply(r, confirmed_token=r.token))
    object.__setattr__(source, "payloads", raw)
    assert user.read_bytes() == user_before and destination.read_bytes() == published_before
elif scenario == "stale":
    newer = preview()
    hold(lambda: service.apply(r, confirmed_token=r.token))
    p = selected / "data/defaults/concepts.json"
    helpers._write(p, {"schema_version": 1, "concepts": [], "external_edit": True})
    hold(lambda: service.apply(newer, confirmed_token=newer.token))
    assert helpers._read(p)["external_edit"] and user.read_bytes() == user_before
elif scenario == "optional_alias_revision":
    legacy = bind(None)
    before_revision = legacy.revision()
    optional = selected / "data/defaults/tag_aliases.json"
    assert not optional.exists()
    optional.write_bytes(b'{"schema_version":1,"aliases":[]}')
    first = legacy.revision()
    assert first != before_revision
    assert legacy.capture(service.generation).snapshot.source.revision == first
    optional.write_bytes(optional.read_bytes() + b" ")
    assert legacy.revision() != first
    optional.unlink()  # exact newly generated fixture participant
    assert legacy.revision() == before_revision
    assert destination.read_bytes() == published_before and user.read_bytes() == user_before
elif scenario == "capture_stale":
    import core.tag_database as tag_module
    original = tag_module.TagDatabase
    changed = selected / "data/defaults/foundation_tags.json"
    saved = changed.read_bytes()
    def raced(*args, **kwargs):
        loaded = original(*args, **kwargs)
        changed.write_bytes(saved + b" ")
        return loaded
    tag_module.TagDatabase = raced
    try: hold(lambda: host_b.capture(service.generation))
    finally:
        tag_module.TagDatabase = original
        changed.write_bytes(saved)
    assert destination.read_bytes() == published_before and user.read_bytes() == user_before
elif scenario == "interrupted":
    atomic = native._atomic_bytes
    def interrupt(path, raw):
        atomic(path, raw)
        if path == selected / "data/defaults/foundation_tags.json":
            raise SystemExit("generated crash after default mutation")
    native._atomic_bytes = interrupt
    try: service.apply(r, confirmed_token=r.token)
    except SystemExit: pass
    else: raise AssertionError("Expected generated crash")
    finally: native._atomic_bytes = atomic
    receipt = next(p for p in (selected / "backups/knowledge-pack-import").glob("*/recovery.json") if helpers._read(p)["status"] == "pending")
    assert destination.read_bytes() == published_before
    restarted = TagCatalogTransactions(bind(proposal_b).host(), session, destination=destination)
    restarted.recover(receipt)
    assert helpers._read(receipt)["status"] == "restored" and user.read_bytes() == user_before
    assert "fresh_default" not in {t.id for t in read(destination).snapshot.tags}
else:
    import suite_core.knowledge.tag_transactions as transactions
    calls = []
    stock_apply = service.host.apply
    def counted(pack, plan):
        calls.append(True)
        return stock_apply(pack, plan)
    service.host = replace(service.host, apply=counted)
    capture_count = []
    committed_capture = service.capture
    committed_revision = service.revision
    if scenario == "inter_capture_drift":
        # Each hypothetical capture is internally coherent, but a second capture
        # returns another Tag set. Reusing the first verified surface is required.
        def would_drift(generation):
            capture_count.append(True)
            captured = committed_capture(generation)
            if len(capture_count) > 1:
                captured = replace(captured, snapshot=replace(captured.snapshot,
                    tags=tuple(t for t in captured.snapshot.tags if t.id != "fresh_default"),
                    lookups=tuple(row for row in captured.snapshot.lookups if row.tag_id != "fresh_default")))
            return captured
        service.capture = would_drift
    drift_path = selected / "data/defaults/foundation_tags.json"
    drift_original = None
    if scenario == "publication_revision_drift":
        drift_calls = []
        def drift_after_first_revision():
            observed = committed_revision()
            drift_calls.append(True)
            if len(drift_calls) == 1:
                payload = helpers._read(drift_path)
                payload["tags"] = [row for row in payload["tags"] if row["id"] != "fresh_default"]
                helpers._write(drift_path, payload)
            return observed
        service.revision = drift_after_first_revision
    publisher = transactions.publish
    def publication_failure(*args, **kwargs): raise OSError("generated publication failure after commit")
    if scenario == "publication_retry": transactions.publish = publication_failure
    if scenario == "publication_revision_drift": drift_original = dict(proposal_b.payloads)["data/defaults/foundation_tags.json"]
    try: result = service.apply(r, confirmed_token=r.token)
    finally: transactions.publish = publisher
    if scenario == "inter_capture_drift":
        assert capture_count == [True] and result.publication is not None, result.publication_error
        assert "fresh_default" in {t.id for t in result.publication.snapshot.tags}
        service.capture = committed_capture
    assert user.read_bytes() == user_before
    assert "fresh_default" not in {t["id"] for t in helpers._read(user)["tags"]}
    assert next(t for t in r.database.proposal.preview.records if t.id == "blue").display == "personal blue"
    receipt_before = {p:p.read_bytes() for p in (selected / "backups/knowledge-pack-import").glob("*/recovery.json")}
    if scenario in {"publication_retry", "publication_revision_drift"}:
        assert result.publication is None and result.publication_error
        if scenario == "publication_retry": assert "generated publication failure" in result.publication_error
        assert service.pending_expected_tags == tuple(r.database.proposal.preview.records)
        if scenario == "publication_revision_drift":
            assert drift_calls and "fresh_default" not in {row["id"] for row in helpers._read(drift_path)["tags"]}
            drift_path.write_bytes(drift_original)
            service.revision = committed_revision
        assert destination.read_bytes() == published_before
        hold(lambda: service.reload(expected_tags=read(destination).snapshot.tags), "exact committed review")
        hold(lambda: service.apply(r, confirmed_token=r.token))
        refreshed = service.reload()
        assert calls == [True] and "fresh_default" in {t.id for t in refreshed.snapshot.tags}
    else:
        assert result.publication is not None, (result.publication_error, set(r.database.proposal.preview.records) ^ set(host_b.capture(service.generation).snapshot.tags))
    assert "fresh_default" in {t.id for t in read(destination).snapshot.tags}
    assert session.resolve("fresh spelling", selection_token="selected").resolution.tag.id == "fresh_default"
    repeated = service.preview(DatabaseAction.MERGE_UPDATE, base_release=b, available_release=b, pack=pack, installed=True, assignments=assignments)
    assert repeated.can_apply, (repeated.holds, repeated.native.issues)
    assert service.apply(repeated, confirmed_token=repeated.token).native_result["backup"]["path"] == ""
    assert {p:p.read_bytes() for p in receipt_before} == receipt_before
print(json.dumps({"scenario":scenario, "passed":True, "native_files":True}))
