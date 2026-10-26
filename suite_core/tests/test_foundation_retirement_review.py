"""Native plan-fingerprinted user ownership, never portable-field equality alone."""
from types import SimpleNamespace
import json

import pytest
from core import knowledge_pack as native
from tests.test_knowledge_pack_records import _foundation_fixture
from suite_core.tests.test_catalog_migration_preview import pack, tag, snapshot
from suite_core.knowledge.catalog_migration_preview import preview_catalog_transition
from suite_core.knowledge.tag_transactions import _native_default_retirement_admissions, _native_catalog_ownership


def prepared(tmp_path):
    _native, root, carrier, _plan, release, proposal, _before = _foundation_fixture(tmp_path)
    plan = native.plan_knowledge_pack_import(root, carrier, approved_default_release=release, foundation_proposal=proposal)
    prior = tag("blue", "blue")
    preview = preview_catalog_transition(pack((prior,)),
        pack((tag("new", "blue"),), removed=("blue",), redirects=(("blue", "new"),)), snapshot((prior,)))
    return root, carrier, release, proposal, plan, preview


def test_native_stored_donor_even_equal_portable_row_is_never_default_only(tmp_path):
    root, _carrier, _release, _proposal, plan, preview = prepared(tmp_path)
    # Fixture contains a blue user record with raw evidence/properties outside DTO.
    assert any(row['id'] == 'blue' for row in json.loads((root/'data/user/tags.json').read_bytes())['tags'])
    assert _native_default_retirement_admissions(SimpleNamespace(root=root), plan, preview) == ()


def test_native_absence_admission_is_bound_to_original_plan_fingerprints(tmp_path):
    root, _carrier, _release, _proposal, plan, _preview = prepared(tmp_path)
    # A detached caller-supplied default-only identity, absent from both native
    # user participants. No persisted identity is removed to set up this test.
    prior = tag('default_only', 'same label')
    preview = preview_catalog_transition(pack((prior,)),
        pack((tag('new', 'same label'),), removed=('default_only',), redirects=(('default_only','new'),)), snapshot((prior,)))
    assert _native_default_retirement_admissions(SimpleNamespace(root=root), plan, preview) == ('default_only',)
    path = root/'data/user/tags.json'
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(RuntimeError, match='ownership changed'):
        _native_default_retirement_admissions(SimpleNamespace(root=root), plan, preview)


def test_native_relationship_reference_preserves_donor_intent(tmp_path):
    root, _carrier, _release, _proposal, plan, _preview = prepared(tmp_path)
    # Existing valid native relation references local_only_synthetic. Check that
    # the relationship authority protects it independently of portable equality.
    prior=tag('local_only_synthetic', 'same label')
    preview=preview_catalog_transition(pack((prior,)),pack((tag('new','same label'),),
        removed=('local_only_synthetic',), redirects=(('local_only_synthetic','new'),)),snapshot((prior,)))
    assert _native_default_retirement_admissions(SimpleNamespace(root=root),plan,preview)==()


def test_relationship_only_reference_blocks_default_only_retirement(tmp_path):
    root, carrier, release, proposal, _plan, _preview = prepared(tmp_path)
    tags = json.loads((root/'data/user/tags.json').read_bytes())
    assert 'fresh_default' not in {row['id'] for row in tags['tags']}
    # Stock fixture's three original colors are synchronized user rows. Reuse
    # its already-declared next default as a before-only fixture default too;
    # do not delete a persisted record or bypass saved identity admission.
    default_path = root/'data/defaults/foundation_tags.json'
    defaults = json.loads(default_path.read_bytes())
    incoming = json.loads(dict(proposal.payloads)['data/defaults/foundation_tags.json'])
    defaults['tags'].append(next(row for row in incoming['tags'] if row['id'] == 'fresh_default'))
    default_path.write_text(json.dumps(defaults), encoding='utf-8')
    path = root/'data/user/relationships.json'
    relationships = json.loads(path.read_bytes())
    relationships['relationships'].append({'id': 'relationship_only_default', 'kind': 'identity_alias',
        'endpoints': [{'kind': 'alias_text', 'id': 'saved default wording'}, {'kind': 'tag', 'id': 'fresh_default'}],
        'source': 'user', 'layer': 'user'})
    path.write_text(json.dumps(relationships), encoding='utf-8')
    plan = native.plan_knowledge_pack_import(root, carrier, approved_default_release=release, foundation_proposal=proposal)
    assert plan.can_apply, plan.issues
    prior = tag('fresh_default', 'fresh default')
    preview = preview_catalog_transition(pack((prior,)),
        pack((tag('new', 'fresh default'),), removed=('fresh_default',), redirects=(('fresh_default', 'new'),)), snapshot((prior,)))
    assert _native_default_retirement_admissions(SimpleNamespace(root=root), plan, preview) == ()


@pytest.mark.parametrize('lifecycle', ['redirect', 'tombstone'])
def test_native_default_lifecycle_proof_requires_source_match_and_user_absence(tmp_path, lifecycle):
    root, _carrier, _release, _proposal, plan, _preview = prepared(tmp_path)
    owner = tag('unowned_default', 'unowned default')
    kwargs = {'redirects': (('default_only', owner.id),)} if lifecycle == 'redirect' else {'removed': ('default_only',)}
    base = pack((owner,), **kwargs)
    preview = preview_catalog_transition(base, pack((owner, tag('default_only', 'live default'))), base.snapshot)
    ownership = _native_catalog_ownership(SimpleNamespace(root=root), plan, preview)
    assert ownership.default_lifecycle_admissions == ('default_only',)
    assert ownership.user_record_ids == ()


def test_native_owned_redirect_target_prevents_shipped_origin_override(tmp_path):
    root, _carrier, _release, _proposal, plan, _preview = prepared(tmp_path)
    owner = tag('local_only_synthetic', 'local only synthetic')
    base = pack((owner,), redirects=(('default_only', owner.id),))
    preview = preview_catalog_transition(base, pack((owner, tag('default_only', 'live default'))), base.snapshot)
    ownership = _native_catalog_ownership(SimpleNamespace(root=root), plan, preview)
    assert ownership.default_lifecycle_admissions == ()
    assert ownership.user_record_ids == (owner.id,)


def test_native_stored_tombstone_cannot_be_admitted_as_shipped_lifecycle(tmp_path):
    root, _carrier, _release, _proposal, plan, _preview = prepared(tmp_path)
    base = pack(removed=('historical_tombstone',))
    preview = preview_catalog_transition(base, pack((tag('historical_tombstone', 'historical tombstone'),)), base.snapshot)
    assert _native_catalog_ownership(SimpleNamespace(root=root), plan, preview).default_lifecycle_admissions == ()


def test_native_redirect_to_stored_tombstone_preserves_user_retirement(tmp_path):
    root, _carrier, _release, _proposal, plan, _preview = prepared(tmp_path)
    base = pack(removed=('historical_tombstone',), redirects=(('default_only', 'historical_tombstone'),))
    preview = preview_catalog_transition(base, pack((tag('default_only', 'live default'),)), base.snapshot)
    assert _native_catalog_ownership(SimpleNamespace(root=root), plan, preview).default_lifecycle_admissions == ()


def test_native_whole_record_evidence_and_tracking_staleness(tmp_path):
    root, _carrier, _release, _proposal, plan, preview = prepared(tmp_path)
    assert _native_catalog_ownership(SimpleNamespace(root=root), plan, preview).user_record_ids == ('blue',)
    path = root/'data/user/default_knowledge_state.json'
    path.write_text(json.dumps({'schema_version': 1}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='ownership changed'):
        _native_catalog_ownership(SimpleNamespace(root=root), plan, preview)
