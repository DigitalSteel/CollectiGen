from dataclasses import replace

import pytest

from suite_core.knowledge.tag_lookup import Category, EffectiveTag, Lookup, Snapshot, Source
from suite_core.knowledge.catalog_migration_preview import (
    Assignment, PackRevision, RefinementOffer, preview_catalog_transition,
)


def snapshot(tags=(), *, aliases=(), removed=(), redirects=(), revision="r1", complete="complete"):
    tags = tuple(tags)
    return Snapshot(Source("synthetic", revision, "fixture", ("c",), complete), tags,
                    (Category("c", "Category", "Category"),),
                    tuple(Lookup(t.display, "canonical_name", t.id, (t.id,)) for t in tags) + tuple(aliases),
                    redirects, removed)


def tag(identity, display=None, **kwargs):
    return EffectiveTag(identity, display or identity, "c", **kwargs)


def pack(tags=(), **kwargs):
    return PackRevision("optional-defaults", kwargs.pop("version", "1"), snapshot(tags, **kwargs))


def assignment(identity="a", value="a", occurrence="image/object/1"):
    return Assignment(occurrence, identity, value, "synthetic-evidence-1")


def test_unchanged_id_survives_display_update_and_rollback():
    base, target = pack((tag("a"),)), pack((tag("a", "renamed"),), version="2")
    local = base.snapshot
    original = (assignment(),)
    result = preview_catalog_transition(base, target, local, original)
    assert result.records == target.snapshot.tags
    assert result.reviews[0].reason == "unchanged_identity"
    assert result.assignments == original
    assert result.rollback_snapshot is local
    assert result.changes[0].before == tag("a")
    assert result.base.content_version == "1" and result.target.content_version == "2"


def test_alias_is_specific_review_choice_never_assignment_replacement():
    target = pack((tag("b", "canonical"),), aliases=(Lookup("legacy", "canonical_alias", "b", ("b",)),))
    original = assignment("legacy-id", "legacy")
    result = preview_catalog_transition(pack(), target, snapshot(), (original,))
    review = result.reviews[0]
    assert review.candidate_ids == ("b",)
    assert review.choices == ("keep_original", "skip_review", "review_specific:b")
    assert review.original == original and result.assignments == (original,)


def test_one_to_many_refinement_requires_explicit_specific_choice():
    base = pack((tag("hand"),))
    target = pack((tag("hand"), tag("own"), tag("another")))
    offer = RefinementOffer("hand", ("own", "another"), "native-plan-fixture")
    result = preview_catalog_transition(base, target, base.snapshot, (assignment("hand", "hand"),), offers=(offer,))
    review = result.reviews[0]
    assert review.reason == "specific_refinement_requires_review"
    assert review.candidate_ids == ("another", "own")
    assert review.relationship_refs == ("native-plan-fixture",)
    assert result.assignments[0].tag_id == "hand"


def test_user_overrides_win_while_untouched_fields_update():
    prior = tag("a", aliases=("old",))
    local = snapshot((replace(prior, display="my edit"),))
    target = pack((replace(prior, display="new default", aliases=("new",)),))
    result = preview_catalog_transition(pack((prior,)), target, local)
    assert result.records == (replace(prior, display="my edit", aliases=("new",)),)
    assert result.changes[0].protected_fields == ("display",)


def test_removed_default_retains_record_assignment_and_provenance():
    base = pack((tag("a"),))
    original = assignment()
    result = preview_catalog_transition(base, pack(version="2"), base.snapshot, (original,))
    assert result.records == (tag("a"),)
    assert result.changes[0].reason == "removed_default_retained"
    assert result.changes[0].before == tag("a")
    assert result.assignments == (original,)
    assert result.rollback_snapshot == base.snapshot


def test_declared_redirect_admits_only_native_owned_unchanged_default_and_keeps_assignment():
    prior = tag("old", "same label")
    survivor = tag("new", "same label")
    base = pack((prior,))
    target = pack((survivor,), version="2", removed=("old",), redirects=(("old", "new"),))
    original = assignment("old", "same label")
    held = preview_catalog_transition(base, target, base.snapshot, (original,))
    assert "default_retirement_requires_local_review:old" in held.holds
    result = preview_catalog_transition(base, target, base.snapshot, (original,), retirement_admissions=("old",))
    assert result.records == (survivor,) and not result.holds
    change = next(row for row in result.changes if row.tag_id == "old")
    assert change.reason == "declared_default_retirement_proposed"
    assert change.before == prior
    assert result.assignments == (original,) and result.rollback_snapshot is base.snapshot
    assert result.reviews[0].candidate_ids == ("new",)
    assert result.reviews[0].choices == ("keep_original", "skip_review", "review_specific:new")


@pytest.mark.parametrize("field,value", [("display", "my saved name"), ("aliases", ("saved alias",)),
                                       ("minimum_rating", "explicit")])
def test_edited_retired_donor_is_preserved_even_with_admission(field, value):
    prior = tag("old", "same label")
    local = snapshot((replace(prior, **{field: value}),))
    target = pack((tag("new", "same label"),), removed=("old",), redirects=(("old", "new"),))
    result = preview_catalog_transition(pack((prior,)), target, local, (assignment("old"),), retirement_admissions=("old",))
    assert next(row for row in result.records if row.id == "old") == local.tags[0]
    assert "default_retirement_requires_local_review:old" in result.holds
    assert result.reviews[0].reason == "local_identity_retirement_requires_review"
    assert result.reviews[0].original.tag_id == "old"


def test_removed_without_redirect_and_untracked_donor_do_not_adopt_retirement():
    prior = tag("old", "same label")
    target = pack((tag("new", "same label"),), removed=("old",))
    result = preview_catalog_transition(pack((prior,)), target, snapshot((prior,)), retirement_admissions=("old",))
    assert prior in result.records
    assert "target_retirement_requires_native_review:old" in result.holds
    redirected = pack((tag("new", "same label"),), removed=("old",), redirects=(("old", "new"),))
    unknown = preview_catalog_transition(pack(), redirected, snapshot((prior,)), retirement_admissions=("old",))
    assert prior in unknown.records and "default_retirement_requires_local_review:old" in unknown.holds


def test_local_retirement_and_unrelated_collision_stay_held_with_admissions():
    prior = tag("old", "same label")
    target = pack((tag("new", "same label"), tag("other", "other")),
        removed=("old",), redirects=(("old", "new"),))
    local = snapshot((tag("mine", "other"),), removed=("old",))
    result = preview_catalog_transition(pack((prior,)), target, local, retirement_admissions=("old",))
    assert "catalog_name_collision:other" in result.holds
    assert any(row.reason == "local_retirement_preserved" for row in result.changes)


@pytest.mark.parametrize("redirects", [(("old", "missing"),), (("old", "cycle"), ("cycle", "old"))])
def test_unresolved_redirect_never_retires_a_record_by_admission(redirects):
    prior = tag("old", "same label")
    # Some invalid wire graphs are rejected by TagLookup admission itself.
    try:
        target = pack((tag("new", "same label"),), removed=("old",), redirects=redirects)
        result = preview_catalog_transition(pack((prior,)), target, snapshot((prior,)), retirement_admissions=("old",))
    except ValueError:
        return
    assert prior in result.records
    assert "target_retirement_requires_native_review:old" in result.holds


@pytest.mark.parametrize("admissions", ["old", ["old"], (["old"],), ("old", "old")])
def test_retirement_admission_collection_is_explicit_bounded_and_unique(admissions):
    with pytest.raises(ValueError):
        preview_catalog_transition(pack(), pack(), snapshot(), retirement_admissions=admissions)


@pytest.mark.parametrize("lifecycle", ["redirect", "tombstone"])
def test_proven_shipped_lifecycle_is_not_user_retirement(lifecycle):
    old = tag("prior_owner", "prior label")
    kwargs = {"redirects": (("revived", "prior_owner"),)} if lifecycle == "redirect" else {"removed": ("revived",)}
    base = pack((old,), **kwargs)
    original = assignment("prior_owner")
    local = base.snapshot
    target = pack((old, tag("revived", "new live label")))
    held = preview_catalog_transition(base, target, local, (original,))
    assert "revived" not in {row.id for row in held.records}
    result = preview_catalog_transition(base, target, local, (original,), default_lifecycle_admissions=("revived",))
    assert result.records == tuple(sorted(target.snapshot.tags, key=lambda row: row.id))
    assert result.local is local and result.assignments == (original,)
    assert not result.holds


def test_changed_or_untracked_lifecycle_cannot_claim_shipped_origin():
    base = pack((tag("owner"),), redirects=(("donor", "owner"),))
    local = snapshot((tag("other"),), redirects=(("donor", "other"),))
    with pytest.raises(ValueError, match="prior-source lifecycle"):
        preview_catalog_transition(base, pack((tag("donor"),)), local, default_lifecycle_admissions=("donor",))
    with pytest.raises(ValueError, match="live target"):
        preview_catalog_transition(base, pack(), base.snapshot, default_lifecycle_admissions=("donor",))


def test_native_user_record_keeps_whole_row_while_generic_merge_keeps_changed_fields():
    prior = tag("stored", "original")
    target_row = replace(prior, minimum_rating="general")
    local_row = replace(prior, display="saved name")
    local = snapshot((local_row,))
    generic = preview_catalog_transition(pack((prior,)), pack((target_row,)), local)
    assert generic.records[0].minimum_rating == "general"
    result = preview_catalog_transition(pack((prior,)), pack((target_row,)), local, native_user_record_ids=("stored",))
    assert result.records == (local_row,)
    assert result.changes[0].reason == "native_user_record_preserved"
    assert "minimum_rating" in result.changes[0].protected_fields
    assert result.local is local


@pytest.mark.parametrize("parameter", ["default_lifecycle_admissions", "native_user_record_ids"])
@pytest.mark.parametrize("value", ["owner", ["owner"], ("owner", "owner"), (["owner"],)])
def test_new_native_ownership_collections_fail_closed(parameter, value):
    with pytest.raises(ValueError):
        preview_catalog_transition(pack((tag("owner"),)), pack((tag("owner"),)), snapshot((tag("owner"),)),
                                  **{parameter: value})


def test_native_user_record_ownership_cannot_add_absent_record():
    with pytest.raises(ValueError):
        preview_catalog_transition(pack(), pack(), snapshot(), native_user_record_ids=("absent",))


@pytest.mark.parametrize("mode,ids", [("blank", ()), ("own", ("local",)),
                                     ("default", ("default",)), ("merged", ("default", "local"))])
def test_catalog_modes_preserve_image_assignments(mode, ids):
    local = snapshot((tag("local"),))
    original = assignment("local", "local")
    result = preview_catalog_transition(pack(), pack((tag("default"),)), local, (original,), mode=mode)
    assert tuple(t.id for t in result.records) == ids
    assert result.assignments == (original,)
    assert result.rollback_snapshot == local
    if mode == "default":
        assert "default_would_exclude_local_record:local" in result.holds


def test_default_only_adoption_holds_for_edits_and_retirements():
    base = pack((tag("a"), tag("b")))
    local = snapshot((tag("a", "edit"),), removed=("b",))
    result = preview_catalog_transition(base, base, local, mode="default")
    assert result.records == (tag("a", "edit"),)
    assert result.holds == ("default_conflicts_with_local_retirement:b", "default_conflicts_with_user_override:a")


def test_missing_local_prior_default_is_not_resurrected():
    base = pack((tag("a"),))
    result = preview_catalog_transition(base, base, snapshot())
    assert result.records == () and result.holds == ("missing_local_baseline:a",)


def test_untracked_same_id_collision_holds_preserving_local():
    result = preview_catalog_transition(pack(), pack((tag("a"),)), snapshot((tag("a", "edit"),)))
    assert result.records == (tag("a", "edit"),)
    assert result.holds == ("untracked_identity_collision:a",)


def test_ambiguous_alias_lists_specific_targets():
    target = pack((tag("a"), tag("b")), aliases=(Lookup("legacy", "ambiguous", owner_ids=("b", "a")),))
    result = preview_catalog_transition(pack(), target, snapshot(), (assignment("old", "legacy"),))
    assert result.reviews[0].candidate_ids == ("a", "b")
    assert result.reviews[0].reason == "ambiguous_identity_requires_review"


def test_retirement_precedes_alias_and_never_resurrects_redirect_donor():
    local = snapshot((tag("b"),), removed=("old",), redirects=(("donor", "b"),))
    target = pack((tag("old"), tag("donor"), tag("b")))
    result = preview_catalog_transition(pack(), target, local, (assignment("old", "b"),))
    assert tuple(t.id for t in result.records) == ("b",)
    assert result.reviews[0].candidate_ids == ()
    assert result.reviews[0].reason == "local_retirement_retained"


def test_preview_determinism_and_stale_snapshot_detection():
    base = pack((tag("a"),))
    first = preview_catalog_transition(base, base, base.snapshot, (assignment(),))
    second = preview_catalog_transition(base, base, base.snapshot, (assignment(),))
    assert first == second and first.matches_catalog_inputs(base, base, base.snapshot)
    assert not first.matches_catalog_inputs(base, base, replace(base.snapshot, source=replace(base.snapshot.source, revision="new")))
    assert not first.matches_catalog_inputs(base, replace(base, content_version="older"), base.snapshot)


def test_cross_catalog_name_collision_holds_without_merging_ids():
    local = snapshot((tag("user", "shared"),))
    target = pack((tag("default", "shared"),))
    result = preview_catalog_transition(pack(), target, local)
    assert tuple(t.id for t in result.records) == ("default", "user")
    assert result.holds == ("catalog_name_collision:shared",)


@pytest.mark.parametrize("status,expected", [("disabled", ()), ("canonical_alias", ("user",))])
def test_user_alias_decisions_precede_incoming_pack(status, expected):
    local_row = Lookup("legacy", status, "user" if expected else "", expected)
    local = snapshot((tag("user"),), aliases=(local_row,))
    target = pack((tag("default"),), aliases=(Lookup("legacy", "canonical_alias", "default", ("default",)),))
    result = preview_catalog_transition(pack(), target, local, (assignment("old", "legacy"),))
    assert result.reviews[0].candidate_ids == expected
    assert result.assignments[0].tag_id == "old"


def test_downgrade_preview_preserves_newer_local_override():
    newer = pack((tag("a", "newer"),), version="2")
    older = pack((tag("a", "older"),), version="1")
    local = snapshot((tag("a", "user"),))
    result = preview_catalog_transition(newer, older, local)
    assert result.records == local.tags and result.rollback_snapshot == local


@pytest.mark.parametrize("bad", ["partial", "out_of_scope", "duplicate_occurrence", "unknown_offer", "overbound"])
def test_invalid_or_incomplete_authority_rejected(bad):
    base = pack((tag("a"),))
    local, rows, offers = base.snapshot, (assignment(),), ()
    if bad == "partial":
        local = replace(local, source=replace(local.source, completeness="partial"))
    elif bad == "out_of_scope":
        local = replace(local, source=replace(local.source, scope=()))
    elif bad == "duplicate_occurrence":
        rows = rows * 2
    elif bad == "overbound":
        rows = rows * 10001
    else:
        offers = (RefinementOffer("a", ("missing",), "evidence"),)
    with pytest.raises(ValueError):
        preview_catalog_transition(base, base, local, rows, offers=offers)


@pytest.mark.parametrize("targets", ["ab", b"ab", bytearray(b"ab"),
    {"a": 1, "b": 2}, None, 7, True, {"a", "b"}, (), [], ("a", "a"),
    ("a", None), ("a", 1), ("",), (" ",), tuple(str(i) for i in range(65))])
def test_targets_reject_shape_types_and_bounds_before_normalization(targets):
    with pytest.raises(ValueError):
        RefinementOffer("old", targets, "host-refinement-fixture")


@pytest.mark.parametrize("factory", [tuple, list])
def test_explicit_targets_keep_specific_choices_and_detach_lists(factory):
    targets = factory(("b", "a"))
    offer = RefinementOffer("old", targets, "host-refinement-fixture")
    if type(targets) is list:
        targets[:] = ["invented"]
    assert offer.target_ids == ("b", "a")
    base = pack((tag("old"),))
    target = pack((tag("old"), tag("a"), tag("b")))
    original = assignment("old", "old")
    result = preview_catalog_transition(base, target, base.snapshot,
                                       [original], offers=[offer])
    assert result.reviews[0].candidate_ids == ("a", "b")
    assert result.reviews[0].choices == ("keep_original", "skip_review",
                                       "review_specific:a", "review_specific:b")
    assert result.reviews[0].relationship_refs == ("host-refinement-fixture",)
    assert result.assignments == (original,)


@pytest.mark.parametrize("boundary", ["assignments", "offers"])
@pytest.mark.parametrize("value", ["", "ab", b"", {}, {"a": 1}, None, 3, set(), ["a"]])
def test_adjacent_preview_collections_reject_malformed_shapes(boundary, value):
    base = pack()
    with pytest.raises(ValueError):
        preview_catalog_transition(base, base, base.snapshot, **{boundary: value})


@pytest.mark.parametrize("boundary", ["target_ids", "assignments", "offers"])
def test_lazy_collection_is_rejected_without_iteration(boundary):
    def forbidden_iteration():
        raise AssertionError("malformed lazy collection was consumed")
        yield "a"
    value = forbidden_iteration()
    with pytest.raises(ValueError, match="explicit list or tuple"):
        if boundary == "target_ids":
            RefinementOffer("old", value, "fixture")
        else:
            base = pack()
            preview_catalog_transition(base, base, base.snapshot, **{boundary: value})


def test_explicit_empty_collections_remain_valid_but_overbound_offers_hold():
    base = pack((tag("old"), tag("a")))
    assert preview_catalog_transition(base, base, base.snapshot, [], offers=[]).assignments == ()
    offer = RefinementOffer("old", ("a",), "fixture")
    with pytest.raises(ValueError, match="offers collection bound"):
        preview_catalog_transition(base, base, base.snapshot, offers=[offer] * 1001)
