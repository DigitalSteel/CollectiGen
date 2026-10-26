"""Named compatibility contracts with synthetic IDs; no production claims."""
from copy import deepcopy
import json

import pytest

from core import encyclopedia_update_binding as binding
from tests.test_standalone_encyclopedia_update import (CONTRACT, standalone, admitted_standalone, add_schema_package,
    synthetic_contract_vocabulary)
from tests.test_encyclopedia_installation import no_external_actions


@pytest.fixture(autouse=True)
def vocabulary(monkeypatch):
    synthetic_contract_vocabulary(monkeypatch)


def parse(contract):
    return binding._data_contract(json.dumps({'module_id': 'collectigen.tag-encyclopedia',
        'software_version': '0.1.0-alpha.8', 'update_data_contract': contract}).encode(), '0.1.0-alpha.8')


def test_schema2_names_participants_and_snapshot_interfaces():
    parsed = parse(CONTRACT)
    assert set(dict(parsed.read_contracts)) == binding.DATA_PARTICIPANTS
    assert parsed.snapshot_interfaces == (1, 2) and parsed.write_contracts == ()


def test_flat_schema1_category_tag_counterexample_holds():
    with pytest.raises(ValueError, match='signed_named'):
        parse({'schema': 1, 'database_schema_versions': [7, 12], 'snapshot_interface_versions': [1, 2]})


@pytest.mark.parametrize('change', ['missing_participant', 'unknown_participant', 'unknown_id',
    'cross_participant_id', 'empty_ids', 'duplicate_ids', 'non_string_id', 'list_participants',
    'schema_bool', 'migration', 'mode', 'settings_missing', 'settings_unknown', 'unknown_field'])
def test_unknown_missing_or_malformed_named_declarations_hold(change):
    contract = deepcopy(CONTRACT)
    reads = contract['read_contracts']
    if change == 'missing_participant': reads.pop('category-store')
    elif change == 'unknown_participant': contract['required_read_contracts']['global-database'] = ['synthetic/global/r1']
    elif change == 'unknown_id': reads['tag-store'] = ['unqualified/production-format']
    elif change == 'cross_participant_id': reads['tag-store'] = reads['category-store']
    elif change == 'empty_ids': reads['tag-store'] = []
    elif change == 'duplicate_ids': reads['tag-store'] *= 2
    elif change == 'non_string_id': reads['tag-store'] = [11]
    elif change == 'list_participants': contract['read_contracts'] = [11, 7]
    elif change == 'schema_bool': contract['schema'] = True
    elif change == 'migration': contract['migration'] = 'on_startup'
    elif change == 'mode': contract['mode'] = 'catalog_rewrite'
    elif change == 'settings_missing': contract.pop('connection_settings_contract')
    elif change == 'settings_unknown': contract['connection_settings_contract'] = 'opaque/global-schema-1'
    else: contract['register_my_ids'] = reads
    with pytest.raises(ValueError): parse(contract)


@pytest.mark.parametrize('interfaces', [[], [True], [0], [1, 1], [65536]])
def test_unknown_missing_or_duplicate_snapshot_interfaces_hold(interfaces):
    with pytest.raises(ValueError, match='snapshot_interfaces'):
        parse({**CONTRACT, 'snapshot_interface_versions': interfaces})


def test_target_read_and_interface_superset_is_compatible():
    prior = {**CONTRACT, 'snapshot_interface_versions': [1], 'required_snapshot_interface_versions': [1]}
    target = deepcopy(CONTRACT)
    target['read_contracts']['tag-store'].append('synthetic/tag-store/r2')
    binding.require_contract_read_coverage(parse(prior), parse(target))
    binding.require_contract_read_coverage(parse(target), parse(CONTRACT))


def test_missing_prior_read_obligation_holds_even_with_other_capabilities():
    target = deepcopy(CONTRACT)
    target['read_contracts']['category-store'] = ['synthetic/category-store/r2']
    with pytest.raises(ValueError, match='schema_change_held'):
        binding.require_contract_read_coverage(parse(CONTRACT), parse(target))


def test_optional_catalog_writes_do_not_gate_code_only_update():
    prior = {**CONTRACT, 'write_contracts': {'tag-store': ['synthetic/tag-store/w1']}}
    binding.require_contract_read_coverage(parse(prior), parse(CONTRACT))
    assert parse({**CONTRACT, 'write_contracts': {'tag-store': ['unqualified/write-schema']}}).write_contracts


def test_unqualified_production_vocabulary_stays_held(monkeypatch):
    monkeypatch.setattr(binding, '_READ_CONTRACT_IDS', {p: frozenset() for p in binding.DATA_PARTICIPANTS})
    with pytest.raises(ValueError, match='qualified_named'):
        parse(CONTRACT)


def test_package_consumer_accepts_named_read_superset_without_optional_writes(admitted_standalone):
    f, boot, _ = admitted_standalone
    target = deepcopy(CONTRACT)
    target['read_contracts']['tag-store'].append('synthetic/tag-store/r2')
    add_schema_package(f, '0.1.0-alpha.9', target)
    offer = boot.update_check.check().offer
    acquired = boot.acquisition.acquire(offer)
    before = boot.binding.current()
    binding.require_target_compatibility(before.selected, acquired)
    assert boot.binding.current() == before and boot.owner.children == {}


def test_target_artifact_hash_change_cannot_supply_contract(admitted_standalone):
    f, boot, _ = admitted_standalone
    add_schema_package(f, '0.1.0-alpha.9')
    acquired = boot.acquisition.acquire(boot.update_check.check().offer)
    acquired.artifact.write_bytes(acquired.artifact.read_bytes() + b'INERT TAMPER')
    before = boot.binding.current()
    with pytest.raises(ValueError):
        binding.require_target_compatibility(before.selected, acquired)
    assert boot.binding.current() == before


def test_future_optional_capabilities_are_preserved_without_establishing_coverage():
    target = deepcopy(CONTRACT)
    target['read_contracts']['future-store'] = ['future/reader/r9']
    target['read_contracts']['tag-store'].append('future/tag/r9')
    target['write_contracts'] = {'future-store': ['future/writer/w9']}
    target['snapshot_interface_versions'].append(3)
    parsed = parse(target)
    binding.require_contract_read_coverage(parse(CONTRACT), parsed)
    assert dict(parsed.read_contracts)['future-store'] == ('future/reader/r9',)
    assert parsed.snapshot_interfaces == (1, 2, 3)
    target['read_contracts']['tag-store'] = ['future/tag/r9']
    with pytest.raises(ValueError, match='schema_change_held'):
        parse(target)


def test_unknown_required_contract_cannot_be_established_by_optional_capability():
    target = deepcopy(CONTRACT)
    target['read_contracts']['tag-store'].append('future/tag/r9')
    target['required_read_contracts']['tag-store'] = ['future/tag/r9']
    with pytest.raises(ValueError, match='qualified_named'):
        parse(target)
    target = deepcopy(CONTRACT)
    target['snapshot_interface_versions'].append(3)
    target['required_snapshot_interface_versions'] = [3]
    with pytest.raises(ValueError, match='known_required_snapshot'):
        parse(target)


def test_new_required_obligation_holds_before_publication(admitted_standalone):
    f, boot, _ = admitted_standalone
    target = deepcopy(CONTRACT)
    target['read_contracts']['tag-store'].append('synthetic/tag-store/r2')
    target['required_read_contracts']['tag-store'].append('synthetic/tag-store/r2')
    add_schema_package(f, '0.1.0-alpha.9', target)
    request = boot.confirm_update(boot.update_check.check().offer, confirmed=True)
    before = boot.binding.current()
    with pytest.raises(ValueError, match='schema_change_held'):
        boot.apply_confirmed(request)
    assert boot.binding.current() == before


@pytest.mark.parametrize('kind', ['reads', 'interfaces'])
def test_chained_requirement_reduction_cannot_begin(tmp_path, monkeypatch, kind):
    from tests.test_encyclopedia_installation import Fixture
    from core.encyclopedia_connection import COMPONENT
    f = Fixture(tmp_path, monkeypatch)
    a, b, c = deepcopy(CONTRACT), deepcopy(CONTRACT), deepcopy(CONTRACT)
    if kind == 'reads':
        a['read_contracts']['tag-store'].append('synthetic/tag-store/r2')
        a['required_read_contracts']['tag-store'].append('synthetic/tag-store/r2')
        b['read_contracts']['tag-store'].append('synthetic/tag-store/r2')
    else:
        b['required_snapshot_interface_versions'] = [1]
        c['required_snapshot_interface_versions'] = c['snapshot_interface_versions'] = [1]
    add_schema_package(f, '0.1.0-alpha.8', a)
    installed = f.install().installed
    f.service.native_health = lambda _: True  # inert initial-fixture health, not native proof
    f.service.confirm_health()
    before = (f.root / 'State' / COMPONENT.value / 'active.json').read_bytes()
    data = f.sentinel.read_bytes()
    # Old mutual read coverage alone would allow A->B, and B->C.
    binding.require_contract_read_coverage(parse(a), parse(b))
    binding.require_contract_read_coverage(parse(b), parse(a))
    binding.require_contract_read_coverage(parse(b), parse(c))
    binding.require_code_only_obligation_equality(parse(b), parse(c))
    add_schema_package(f, '0.1.0-alpha.9', b)
    offer, = f.service.discover(channel='beta')
    acquired = f.acquisition.acquire(offer)
    with pytest.raises(ValueError, match='schema_change_held'):
        binding.require_target_compatibility(installed.selected, acquired)
    assert (f.root / 'State' / COMPONENT.value / 'active.json').read_bytes() == before
    assert f.sentinel.read_bytes() == data


@pytest.mark.parametrize('failure', ['stage', 'launch'])
def test_additive_capabilities_recover_original_after_interruption(admitted_standalone, monkeypatch, failure):
    from tests.test_standalone_encyclopedia_update import launch_close
    f, boot, actors = admitted_standalone
    old = launch_close(boot, actors)
    target = deepcopy(CONTRACT)
    target['read_contracts']['tag-store'].append('synthetic/tag-store/r2')
    target['snapshot_interface_versions'].append(3)
    target['write_contracts'] = {'future-store': ['future/writer/w9']}
    add_schema_package(f, '0.1.0-alpha.9', target)
    request = boot.confirm_update(boot.update_check.check().offer, confirmed=True)
    before_data = f.sentinel.read_bytes()
    assert dict(request.data_obligations.required_read_contracts)['tag-store'] == ('synthetic/tag-store/r1',)
    if failure == 'stage':
        import core.encyclopedia_installation as installation
        original = installation.stage_and_activate_admitted
        def interrupted(*args, **kwargs):
            previous = kwargs['failpoint']
            def failpoint(phase):
                previous(phase)
                if phase == 'version_published':
                    raise OSError('inert interruption after version publication')
            kwargs['failpoint'] = failpoint
            return original(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(installation, 'stage_and_activate_admitted', interrupted)
            with pytest.raises(OSError): boot.apply_confirmed(request)
    else:
        boot.apply_confirmed(request)
        launch_close(boot, actors, code=17)
    assert boot.request is request
    recovered = boot.recover_update(request, confirmed=True)
    assert recovered.installed.selected == old and f.sentinel.read_bytes() == before_data
    launch_close(boot, actors)
    boot.finish_recovery_health(request)
    assert boot.request is None
