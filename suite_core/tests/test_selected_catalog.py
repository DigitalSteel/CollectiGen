"""Synthetic read-only binding qualification; no catalog or application startup."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from suite_core.encyclopedia.publication import Publication, StalePublication
from suite_core.knowledge import Category, EffectiveTag, Lookup, Snapshot, Source
from suite_core.knowledge.catalog_migration_preview import Assignment, PackRevision
from suite_core.selected_catalog import (
    BINDING_INTERFACE_VERSION, SOFTWARE_VERSION, SelectedCatalogSession,
)
from suite_core.tests.test_media_examples import host, query_service


def publication(catalog_id="catalog-a", *, generation=1, revision="r1",
                display="Red Fox", aliases=("Vulpes",), authority="approved-a"):
    tag = EffectiveTag("shared-id", display, "animals", aliases)
    rows = (Lookup(display.lower().replace("_", " "), "canonical_name", tag.id, (tag.id,)),)
    rows += tuple(Lookup(alias.lower(), "canonical_alias", tag.id, (tag.id,)) for alias in aliases)
    return Publication(catalog_id, generation,
                       Snapshot(Source("synthetic approved source", revision, authority, ("animals",)),
                                (tag,), (Category("animals", "Animals", "Animals"),), rows))


def test_pure_import_and_independent_component_version():
    assert SOFTWARE_VERSION == "0.1.0-alpha.1"
    assert BINDING_INTERFACE_VERSION == 1
    code = ("import sys; import suite_core.selected_catalog; "
            "assert 'core' not in sys.modules; "
            "assert not any(x.startswith(('PySide6', 'qt_app')) for x in sys.modules)")
    subprocess.run([sys.executable, "-B", "-c", code], check=True,
                   cwd=Path(__file__).resolve().parents[2])


def test_binding_retains_publication_source_identity_and_fingerprint():
    value = publication()
    session = SelectedCatalogSession(value)
    binding = session.binding
    assert (binding.catalog_id, binding.generation, binding.authority_revision,
            binding.authority_fingerprint, binding.snapshot_fingerprint) == (
                value.catalog_id, value.generation, value.snapshot.source.revision,
                value.snapshot.source.authority_fingerprint, value.snapshot.fingerprint)
    assert session.catalog_token() == binding.token


def test_lookup_alias_and_context_selection():
    session = SelectedCatalogSession(publication())
    result = session.resolve("Vulpes", selection_token="view-1")
    assert result.resolution.tag.id == "shared-id"
    assert session.accepts_lookup(result, current_selection_token="view-1")
    assert not session.accepts_lookup(result, current_selection_token="view-2")


def test_switch_colliding_tag_ids_and_revision_strings_rejects_old_lookup():
    session = SelectedCatalogSession(publication())
    result = session.resolve("shared-id", selection_token="view-1")
    session.select(publication("catalog-b", display="Blue Fox", aliases=()))
    assert not session.accepts_lookup(result, current_selection_token="view-1")
    assert session.resolve("shared-id", selection_token="view-1").resolution.tag.display == "Blue Fox"


def test_aba_and_foreign_session_deliveries_remain_stale():
    first = publication()
    session = SelectedCatalogSession(first)
    result = session.resolve("shared-id", selection_token="view")
    session.select(publication("catalog-b"))
    session.select(first)
    assert not session.accepts_lookup(result, current_selection_token="view")
    peer = SelectedCatalogSession(first)
    assert not peer.accepts_lookup(result, current_selection_token="view")


@pytest.mark.parametrize("change", ["generation", "authority", "fingerprint"])
def test_every_binding_dimension_changes_token(change):
    first = publication()
    session = SelectedCatalogSession(first)
    old = session.catalog_token()
    changes = {"generation": {"generation": 2, "revision": "r2"},
               "authority": {"authority": "approved-b"},
               "fingerprint": {"aliases": ("Fox",)}}
    session.select(publication(**changes[change]))
    assert session.catalog_token() != old


@pytest.mark.parametrize("candidate", [
    publication("catalog-b"),
    publication(generation=2),
    publication(display="Blue Fox", aliases=()),
])
def test_refresh_reuses_successor_guard_and_failure_retains_session(candidate):
    session = SelectedCatalogSession(publication())
    before = session.binding
    with pytest.raises(StalePublication):
        session.refresh(candidate)
    assert session.binding == before


def test_refresh_noop_keeps_deliveries_and_new_revision_invalidates():
    first = publication()
    session = SelectedCatalogSession(first)
    result = session.resolve("shared-id", selection_token="view")
    session.refresh(first)
    assert session.accepts_lookup(result, current_selection_token="view")
    session.refresh(publication(generation=2, revision="r2"))
    assert not session.accepts_lookup(result, current_selection_token="view")


def test_media_request_is_bound_to_lookup_and_switch_rejects_delivery(host):
    provider, _, state, _ = host
    session = SelectedCatalogSession(publication())
    provider.current_knowledge_revision = session.catalog_token
    request = session.example_request(selection_token=state.selection, library_ids=("a", "b"),
                                      tag_ids=("shared-id",))
    assert request.identities[0].names == ("Red Fox", "Vulpes")
    assert request.knowledge_revision == session.catalog_token()
    result = provider.query(request)
    assert len(result.references) == 3
    assert provider.accepts(result)
    session.select(publication("catalog-b"))
    assert not provider.accepts(result)
    assert provider.query(request).status.value == "stale"


def test_media_request_preserves_existing_selection_guard(host):
    provider, _, state, _ = host
    session = SelectedCatalogSession(publication())
    provider.current_knowledge_revision = session.catalog_token
    request = session.example_request(selection_token=state.selection, library_ids=("a",),
                                      tag_ids=("shared-id",))
    result = provider.query(request)
    state.selection = "next-image-or-tag"
    assert not provider.accepts(result)


def test_missing_or_out_of_scope_tag_holds_request():
    session = SelectedCatalogSession(publication())
    with pytest.raises(ValueError):
        session.example_request(selection_token="view", library_ids=("a",), tag_ids=("missing",))


@pytest.mark.parametrize("mode", ["blank", "own", "default", "merged"])
def test_optional_choices_are_existing_proposals_never_activation(mode):
    old = publication()
    new = publication(generation=2, revision="r2", display="Updated Fox", aliases=())
    session = SelectedCatalogSession(old)
    binding = session.binding
    base = PackRevision("optional-default", "1", old.snapshot)
    target = PackRevision("optional-default", "2", new.snapshot)
    assignment = Assignment("occurrence-1", "shared-id", "Red Fox", "synthetic-evidence")
    proposal = session.propose(base, target, mode=mode, assignments=(assignment,))
    assert proposal.preview.mode == mode
    assert proposal.preview.assignments == (assignment,)
    assert proposal.preview.rollback_snapshot == old.snapshot
    assert session.binding == binding
    assert session.resolve("shared-id", selection_token="view").resolution.tag.display == "Red Fox"
    assert session.accepts_proposal(proposal, current_base=base, current_target=target)
    if mode == "blank": assert proposal.preview.records == ()
    elif mode == "own": assert proposal.preview.records == old.snapshot.tags
    else: assert proposal.preview.records[0].display == "Updated Fox"


def test_merged_user_edits_preserved_and_default_only_holds():
    old = publication()
    local = publication(display="My Fox", aliases=())
    new = publication(generation=2, revision="r2", display="Updated Fox", aliases=())
    session = SelectedCatalogSession(local)
    base = PackRevision("optional-default", "1", old.snapshot)
    target = PackRevision("optional-default", "2", new.snapshot)
    assert session.propose(base, target, mode="merged").preview.records[0].display == "My Fox"
    assert session.propose(base, target, mode="default").preview.holds


def test_proposal_delivery_rejects_catalog_switch_or_changed_pack_version():
    value = publication()
    session = SelectedCatalogSession(value)
    base = PackRevision("optional-default", "1", value.snapshot)
    target = PackRevision("optional-default", "2", value.snapshot)
    proposal = session.propose(base, target, mode="own")
    assert not session.accepts_proposal(proposal, current_base=base,
                                       current_target=replace(target, content_version="3"))
    session.select(publication("catalog-b"))
    assert not session.accepts_proposal(proposal, current_base=base, current_target=target)
