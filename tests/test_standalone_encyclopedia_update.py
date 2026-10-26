"""Synthetic original-owner/unsigned verifier fixtures, never native qualification.

All process actors and crypto are injected. Real existing lease, immutable stage,
journal, signed-context checks and canonical settings IO use disposable roots.
"""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import sys
import zipfile

import pytest

from core.component_acquisition import ComponentAcquisition
from core.encyclopedia_connection import COMPONENT
from core.encyclopedia_update_binding import admit_standalone_binding
from core.manager_launch_owner import DeploymentOwner, LaunchHold
from release_contract import ReleaseArtifact
from suite_core.discovery import component_staging as staging
from suite_core.discovery.offline_component_producer import produce_inventory
from suite_core.encyclopedia.bootstrap_entry import StandaloneEncyclopediaBootstrap, from_compiled_profile
from tests.test_encyclopedia_installation import Fixture, no_external_actions
from tests.test_encyclopedia_root_admission import original

from core.encyclopedia_update_binding import DATA_PARTICIPANTS, CONNECTION_SETTINGS_CONTRACT

CONTRACT = {'schema': 2, 'mode': 'code_only', 'migration': 'none',
    'read_contracts': {p: ['synthetic/' + p + '/r1'] for p in DATA_PARTICIPANTS},
    'required_read_contracts': {p: ['synthetic/' + p + '/r1'] for p in DATA_PARTICIPANTS},
    'required_snapshot_interface_versions': [1, 2],
    'snapshot_interface_versions': [1, 2], 'connection_settings_contract': CONNECTION_SETTINGS_CONTRACT}


def synthetic_contract_vocabulary(monkeypatch):
    """Unsigned fixture IDs only. No production declaration or codec promise."""
    monkeypatch.setattr('core.encyclopedia_update_binding._READ_CONTRACT_IDS',
        {p: frozenset(('synthetic/' + p + '/r1', 'synthetic/' + p + '/r2')) for p in DATA_PARTICIPANTS})
    monkeypatch.setattr('core.encyclopedia_update_binding._WRITE_CONTRACT_IDS',
        {p: frozenset(('synthetic/' + p + '/w1',)) for p in DATA_PARTICIPANTS})


def add_schema_package(f, version, contract=CONTRACT):
    f.add(version)
    row = f.rows[-1]
    assets = {a['name']: a for a in row['assets']}
    package = next(a for a in row['assets'] if a['name'].endswith('.zip'))
    module = json.dumps({'module_id': COMPONENT.value, 'software_version': version,
                         'update_data_contract': contract}).encode()
    members = (('TagEncyclopedia/TagEncyclopedia.exe', b'INERT NOT EXECUTABLE'),
               ('TagEncyclopedia/MODULE_MANIFEST.json', module))
    inventory = produce_inventory(members, component_id=COMPONENT, software_version=version,
                                  os_name='windows', architecture='x86_64')
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(staging.INVENTORY, inventory)
        for name, raw in members:
            archive.writestr(name, raw)
    payload = output.getvalue()
    f.resources[package['browser_download_url']] = payload
    package['size'] = len(payload)
    message = f.resources[assets['collectigen-release.json']['browser_download_url']]
    f.registry[message] = replace(f.registry[message], artifact=ReleaseArtifact(package['name'],
        'component-package', sha256(payload).hexdigest(), len(payload), sha256(inventory).hexdigest()))


@pytest.fixture
def standalone(tmp_path, monkeypatch, request):
    synthetic_contract_vocabulary(monkeypatch)
    f = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr('core.encyclopedia_update_binding.verify_component_release', f.verify)
    add_schema_package(f, '0.1.0-alpha.8')
    f.install()
    f.service.native_health = lambda _: True  # synthetic initial healthy fixture only
    f.service.confirm_health()
    bootstrap_program = f.root / 'TagEncyclopedia.exe'
    bootstrap_program.write_bytes(b'INERT STABLE BOOTSTRAP; NEVER EXECUTE')
    cache = f.data / 'Encyclopedia/UpdateCache/attempt'
    cache.mkdir(parents=True)
    acquisition = ComponentAcquisition(cache_root=cache, cache_owner='synthetic-standalone-attempt',
        opener=f.opener, host_version='0.1.0-alpha.8', trust_bytes=f.trust,
        trust_sha256=sha256(f.trust).hexdigest(), max_artifact_bytes=staging.MAX_ARCHIVE)
    binding = admit_standalone_binding(root=f.root, data_root=f.data, settings_path=f.settings,
        bootstrap_sha256=sha256(bootstrap_program.read_bytes()).hexdigest(),
        trust_bytes=f.trust, trust_sha256=sha256(f.trust).hexdigest(),
        schema2_identity=getattr(request,'param',False))
    owner = DeploymentOwner(f.root, data_root=f.data).acquire()
    actors = []
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window', lambda _: True)
    monkeypatch.setattr(sys, 'executable', str(bootstrap_program))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    boot = StandaloneEncyclopediaBootstrap(binding, acquisition, owner)
    from tests.test_encyclopedia_entry_control import stock_native_double
    from suite_core.encyclopedia.control_entry import StandaloneControlActor
    _, state, api = stock_native_double(boot, monkeypatch)
    # Inject only HTTP delivery behind the real, freshly scoped transport policy.
    # Do not copy the metadata opener or weaken archive/redirect admission.
    from types import SimpleNamespace
    from core.component_release_transport import ComponentReleaseTransport
    exact_factory = ComponentReleaseTransport.for_exact_artifact
    def contained_factory(transport, source):
        scoped = exact_factory(transport, source)
        assert scoped.policy.admit_initial(source.url) == 'asset'
        scoped._opener = SimpleNamespace(open=lambda request, timeout: f.opener(request, timeout))
        return scoped
    monkeypatch.setattr(ComponentReleaseTransport, 'for_exact_artifact', contained_factory)
    owner.enable_standalone_control(boot.subject)
    original_init = StandaloneControlActor.__init__
    def retained_actor(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        actors.append(StockReaderControls(self, state))
    monkeypatch.setattr(StandaloneControlActor, '__init__', retained_actor)
    yield f, boot, actors
    owner.abandon()  # test cleanup leaves original marker/evidence; never claims closure


class StockReaderControls:
    """Test controls for original stock actors, not a runtime reader/provider."""
    def __init__(self, actor, state):
        self.actor, self.state = actor, state

    @property
    def code(self): return self.state['code']

    @code.setter
    def code(self, value): self.state['code'] = value

    @property
    def members(self): return self.state['members']

    @members.setter
    def members(self, value): self.state['members'] = value

    @property
    def drained(self): return self.actor.drained

    @property
    def closed(self): return self.actor.process is not None and self.actor.process._handle is None


@pytest.fixture
def admitted_standalone(standalone):
    """Mutable component tests explicitly start the original root custodian."""
    _, boot, _ = standalone
    boot.owner.begin_standalone_custody(boot.subject)
    return standalone


def launch_close(boot, actors, *, code=0):
    selected = boot.launch_current()
    assert not boot.poll_reader()  # records visible original synthetic birth
    actor = actors[-1]; actor.code = code; actor.members = 0
    assert boot.poll_reader() and actor.closed
    return selected


def confirm_next(f, boot, version='0.1.0-alpha.9', contract=CONTRACT):
    add_schema_package(f, version, contract)
    notice = boot.update_check.check()
    assert notice.status == 'available'
    return boot.confirm_update(notice.offer, confirmed=True)


def test_normal_close_apply_target_health_and_original_lease_retirement(admitted_standalone):
    f, boot, actors = admitted_standalone
    before = f.sentinel.read_bytes()
    old = boot.launch_current(); assert not boot.poll_reader()
    request = confirm_next(f, boot)
    with pytest.raises(LaunchHold, match='close_and_drain'):
        boot.apply_confirmed(request)
    with pytest.raises(LaunchHold):
        boot.close()
    actor = actors[-1]; actor.code = 0; actor.members = 0; actor.drained.clear()
    assert not boot.poll_reader()
    with pytest.raises(LaunchHold):
        boot.apply_confirmed(request)
    actor.drained.set(); assert boot.poll_reader()
    assert boot.owner.fd is not None  # bootstrap survives ordinary reader exit
    result = boot.apply_confirmed(request)
    assert result.installed.phase == 'activated' and result.installed.selected != old
    assert old.program.exists() and f.sentinel.read_bytes() == before
    with pytest.raises(LaunchHold):
        boot.close()
    launch_close(boot, actors)
    current = boot.finish_target_health(request)
    assert current.phase == 'healthy' and f.sentinel.read_bytes() == before
    receipt = boot.close()
    assert boot.owner.fd is None and not boot.owner.marker.exists()
    assert boot.close() == receipt


def test_cancel_before_reader_close_preserves_active_and_settings(admitted_standalone):
    f, boot, actors = admitted_standalone
    selected = boot.launch_current()
    before = boot.binding.current()
    request = confirm_next(f, boot)
    boot.cancel_update(request)
    assert boot.binding.current() == before and boot.subject.reader.selected == selected
    assert actors[-1].code is None and boot.owner.fd is not None
    assert boot.request is None


def test_forged_subject_and_confirmed_request_rejected(admitted_standalone):
    f, boot, _ = admitted_standalone
    with pytest.raises(LaunchHold, match='original_standalone_subject'):
        boot.owner.poll_standalone_encyclopedia(replace(boot.subject))
    request = confirm_next(f, boot)
    with pytest.raises(ValueError, match='exact_original'):
        boot.apply_confirmed(replace(request))
    with pytest.raises(LaunchHold):
        boot.owner.begin_standalone_encyclopedia_update(boot.subject, None)
    assert boot.request is request


def test_same_existing_lease_excludes_second_root_and_manager_operations(admitted_standalone):
    f, boot, _ = admitted_standalone
    other = DeploymentOwner(f.root, data_root=f.data)
    with pytest.raises(LaunchHold):
        other.acquire()
    assert other.fd is None
    with pytest.raises(LaunchHold, match='excludes_manager'):
        boot.owner.stop_helper_grants()
    with pytest.raises(LaunchHold, match='excludes_manager'):
        boot.owner.normal_ready(boot.owner.record['owner'])


def test_changed_baseline_or_bootstrap_holds_original_request(admitted_standalone):
    f, boot, _ = admitted_standalone
    request = confirm_next(f, boot)
    f.settings.write_text('{"independent_correction":true}', encoding='utf-8')
    with pytest.raises(ValueError, match='saved_component_selection'):
        boot.apply_confirmed(request)
    assert boot.request is request and boot.owner.fd is not None


@pytest.mark.parametrize('contract', [None, {**CONTRACT, 'read_contracts': {**CONTRACT['read_contracts'],
    'category-store': ['synthetic/category-store/r2']}},
                                    {**CONTRACT, 'schema': True}])
def test_missing_or_incompatible_signed_schema_holds_before_publication(admitted_standalone, contract):
    f, boot, _ = admitted_standalone
    before = boot.binding.current(); data = f.sentinel.read_bytes()
    request = confirm_next(f, boot, contract=contract)
    with pytest.raises(ValueError):
        boot.apply_confirmed(request)
    assert boot.binding.current() == before and f.sentinel.read_bytes() == data
    assert boot.request is None  # exact baseline proves prepublication refusal


def test_cancelled_acquisition_does_not_roll_back_baseline(admitted_standalone, monkeypatch):
    f, boot, _ = admitted_standalone
    before = boot.binding.current()
    request = confirm_next(f, boot)
    monkeypatch.setattr(boot.acquisition, 'acquire', lambda *args, **kwargs: (_ for _ in ()).throw(InterruptedError('synthetic cancellation')))
    with pytest.raises(InterruptedError):
        boot.apply_confirmed(request)
    assert boot.binding.current() == before and boot.request is None
    assert before.selected.program.exists()


def test_cancel_at_acquisition_return_prevents_review_or_staging(admitted_standalone, monkeypatch):
    f, boot, _ = admitted_standalone
    before = boot.binding.current()
    request = confirm_next(f, boot)
    acquire = boot.acquisition.acquire
    def cancelled(*args, **kwargs):
        result = acquire(*args, **kwargs)
        boot.request_cancel()
        return result
    monkeypatch.setattr(boot.acquisition, 'acquire', cancelled)
    monkeypatch.setattr(boot._installation, 'review', lambda _: pytest.fail('review started after cancellation'))
    with pytest.raises(InterruptedError, match='after acquisition'):
        boot.apply_confirmed(request)
    assert boot.binding.current() == before and boot.request is None


def test_failed_target_restart_requires_explicit_code_recovery_and_prior_health(admitted_standalone):
    f, boot, actors = admitted_standalone
    before_data = f.sentinel.read_bytes()
    old = launch_close(boot, actors)
    request = confirm_next(f, boot)
    boot.apply_confirmed(request)
    launch_close(boot, actors, code=17)
    with pytest.raises(ValueError, match='native_reader_health'):
        boot.finish_target_health(request)
    with pytest.raises(ValueError, match='explicit_original'):
        boot.recover_update(request)
    # Corrections made after B was installed must survive code recovery to A.
    before_data = b'{"corrected_after_target_startup":true,"future_schema":[1,2,3]}\n'
    f.sentinel.write_bytes(before_data)
    settings = json.loads(f.settings.read_text(encoding='utf-8'))
    settings['user_correction_after_target'] = 'preserve'
    f.settings.write_text(json.dumps(settings), encoding='utf-8')
    result = boot.recover_update(request, confirmed=True)
    assert result.installed.selected == old and f.sentinel.read_bytes() == before_data
    assert json.loads(f.settings.read_text(encoding='utf-8'))['user_correction_after_target'] == 'preserve'
    with pytest.raises(LaunchHold, match='health_or_explicit'):
        boot.finish_recovery_health(request)
    assert launch_close(boot, actors) == old
    recovered = boot.finish_recovery_health(request)
    assert recovered.phase == 'healthy' and recovered.selected == old
    assert f.sentinel.read_bytes() == before_data


def test_interrupted_stage_retains_request_and_uses_existing_recovery(admitted_standalone, monkeypatch):
    f, boot, actors = admitted_standalone
    old = launch_close(boot, actors)
    request = confirm_next(f, boot)
    original = staging.stage_and_activate_admitted
    def interrupted(*args, **kwargs):
        failpoint = kwargs['failpoint']
        def checkpoint(phase):
            failpoint(phase)
            if phase == 'member_extracted':
                raise InterruptedError('synthetic extraction interruption')
        kwargs['failpoint'] = checkpoint
        return original(*args, **kwargs)
    monkeypatch.setattr('core.encyclopedia_installation.stage_and_activate_admitted', interrupted)
    with pytest.raises(InterruptedError):
        boot.apply_confirmed(request)
    assert boot.request is request
    assert (f.root / 'State' / COMPONENT.value / 'pending-transaction.json').exists()
    with pytest.raises(LaunchHold):
        boot.close()
    recovered = boot.recover_update(request, confirmed=True)
    assert recovered.installed.selected == old
    launch_close(boot, actors)
    boot.finish_recovery_health(request)
    assert old.program.exists()


def test_lost_original_marker_retains_stage_and_reader_hold(admitted_standalone):
    _, boot, _ = admitted_standalone
    with pytest.raises(LaunchHold, match='lease_or_marker'):
        with boot._hold(COMPONENT):
            boot.owner.marker.write_bytes(b'changed synthetic owner marker')
    assert boot.subject.reader.component_stage_pending
    assert 'component_stage' in boot.owner.record


def test_flat_package_and_inactive_profile_cannot_mint_authority(admitted_standalone):
    f, boot, _ = admitted_standalone
    assert from_compiled_profile() is None
    (f.root / 'State' / COMPONENT.value / 'component-origin.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='enrolled_component_origin'):
        boot.binding.current()


def test_manager_delegate_controls_same_root_without_reinstall_and_offline_browsing(admitted_standalone, original):
    f, boot, actors = admitted_standalone
    manager, route, counts = original
    before = boot.binding.current()
    delegate = boot.delegate_to_manager(route, confirmed=True)
    assert boot.update_observation().owner == 'manager'
    add_schema_package(f, '0.1.0-alpha.9')
    notice = boot.update_check.check()
    assert notice.status == 'available' and notice.route == 'manager'
    with pytest.raises(ValueError, match='original_manager_delegate'):
        boot.confirm_update(notice.offer, confirmed=True)
    assert boot.binding.current() == before
    counts[0] = 0  # synthetic Manager peer unavailable; never releases leaf lease
    assert boot.update_check.check().status == 'held'
    launch_close(boot, actors)  # Manager availability is not local reading authority
    boot.hand_back_to_standalone(delegate, confirmed=True)
    assert boot.update_observation().owner == 'standalone'
    assert boot.binding.current() == before and boot.owner.fd is not None
    assert manager.fd is not None


def test_manager_delegate_rechecked_after_acquisition_before_any_component_write(admitted_standalone, original, monkeypatch):
    f, boot, _ = admitted_standalone
    _, route, counts = original
    delegate = boot.delegate_to_manager(route, confirmed=True)
    add_schema_package(f, '0.1.0-alpha.9')
    notice = boot.update_check.check()
    request = boot.confirm_update(notice.offer, confirmed=True, delegate=delegate)
    before = boot.binding.current()
    acquire = boot.acquisition.acquire
    def lose_manager(*args, **kwargs):
        acquired = acquire(*args, **kwargs)
        counts[0] = 0
        return acquired
    monkeypatch.setattr(boot.acquisition, 'acquire', lose_manager)
    with pytest.raises(LaunchHold, match='original_component_stage_peer'):
        boot.apply_confirmed(request, delegate=delegate)
    assert boot.binding.current() == before and boot.request is None


def test_foreign_native_actor_cannot_supply_drain(admitted_standalone):
    _, boot, _ = admitted_standalone
    boot.launch_current()
    boot.subject.reader.native = object()
    with pytest.raises(LaunchHold, match='original_standalone_native_actor'):
        boot.poll_reader()
    with pytest.raises(LaunchHold):
        with boot._hold(COMPONENT):
            pass
    assert boot.owner.fd is not None


def test_wrong_settlement_route_cannot_retire_target_request(admitted_standalone):
    f, boot, actors = admitted_standalone
    request = confirm_next(f, boot)
    boot.apply_confirmed(request)
    launch_close(boot, actors)
    with pytest.raises(ValueError, match='exact_original_prior'):
        boot.finish_recovery_health(request)
    assert boot.request is request and boot.owner._standalone_encyclopedia_update is request
    boot.finish_target_health(request)


def test_true_flat_package_has_no_baseline_enrollment(admitted_standalone, tmp_path):
    from tests.test_encyclopedia_connection import package
    f, _, _ = admitted_standalone
    flat = tmp_path / 'old-flat-reader'
    package(flat)
    data = flat / 'Data'; data.mkdir()
    settings = data / 'settings.json'; settings.write_text('{}', encoding='utf-8')
    before = tuple(flat.iterdir())
    with pytest.raises(FileNotFoundError):
        admit_standalone_binding(root=flat, data_root=data, settings_path=settings,
            bootstrap_sha256='0' * 64, trust_bytes=f.trust, trust_sha256=sha256(f.trust).hexdigest())
    assert tuple(flat.iterdir()) == before and not (flat / 'State').exists()


def test_reported_target_release_hash_mismatch_same_version_is_held(admitted_standalone, monkeypatch):
    f, boot, actors = admitted_standalone
    request = confirm_next(f, boot)
    boot.apply_confirmed(request)
    actual = boot.binding.current
    monkeypatch.setattr(type(boot.binding), 'current', lambda self: replace(actual(), release_sha256='0' * 64))
    with pytest.raises(LaunchHold, match='cancel_or_settle_original'):
        boot.launch_current()
    assert boot.owner._standalone_encyclopedia_update is request


def retained_component_bytes(f, selected):
    home = f.root / 'State' / COMPONENT.value
    return {name: (home / name).read_bytes() for name in
            ('transaction.json', 'active.json', 'component-origin.json')}, selected.program.read_bytes()


@pytest.mark.parametrize('recovering', [False, True])
def test_original_publication_retry_saves_only_then_allows_launch_health(admitted_standalone, monkeypatch, recovering):
    f, boot, actors = admitted_standalone
    old = launch_close(boot, actors)
    request = confirm_next(f, boot)
    save = boot._installation.store.save
    def fail_save(*args, **kwargs):
        raise PermissionError('synthetic original settings publication failure')
    if recovering:
        boot.apply_confirmed(request)
        launch_close(boot, actors, code=17)
        monkeypatch.setattr(boot._installation.store, 'save', fail_save)
        result = boot.recover_update(request, confirmed=True)
        expected = old
    else:
        monkeypatch.setattr(boot._installation.store, 'save', fail_save)
        result = boot.apply_confirmed(request)
        expected = result.installed.selected
    home = f.root / 'State' / COMPONENT.value
    assert not result.connection_saved and (home / 'connection-pending.json').is_file()
    assert boot.request is request
    with pytest.raises(ValueError, match='recover_original_component'):
        boot.launch_current()
    component_bytes, code = retained_component_bytes(f, expected)
    calls = list(f.calls)
    monkeypatch.setattr(boot._installation.store, 'save', save)
    def deny(*args, **kwargs):
        pytest.fail('save-only retry attempted acquire, discovery, stage or rollback')
    with monkeypatch.context() as save_only:
        save_only.setattr(boot.acquisition, 'acquire', deny)
        save_only.setattr(boot.acquisition, 'discover', deny)
        save_only.setattr('core.encyclopedia_installation.stage_and_activate_admitted', deny)
        save_only.setattr('core.encyclopedia_installation.finish_or_recover_admitted', deny)
        with pytest.raises(ValueError, match='exact_original_publication'):
            boot.retry_connection(replace(request))
        saved = boot.retry_connection(request)
        assert saved.connection_saved and not (home / 'connection-pending.json').exists()
        assert retained_component_bytes(f, expected) == (component_bytes, code)
        assert f.calls == calls
    assert boot.binding.current().selected == expected
    launch_close(boot, actors)
    boot.finish_recovery_health(request) if recovering else boot.finish_target_health(request)
    assert boot.request is None


def test_publication_retry_rejects_changed_original_intent(admitted_standalone, monkeypatch):
    f, boot, _ = admitted_standalone
    request = confirm_next(f, boot)
    monkeypatch.setattr(boot._installation.store, 'save', lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError('synthetic save failure')))
    result = boot.apply_confirmed(request)
    assert not result.connection_saved
    home = f.root / 'State' / COMPONENT.value
    path = home / 'connection-pending.json'
    intent = json.loads(path.read_text(encoding='utf-8'))
    intent['journal_sha256'] = '0' * 64
    path.write_text(json.dumps(intent), encoding='utf-8')
    before = {name: (home / name).read_bytes() for name in ('transaction.json', 'active.json', 'connection-pending.json')}
    with pytest.raises(ValueError, match='original_connection_publication_intent'):
        boot.retry_connection(request)
    assert all((home / name).read_bytes() == raw for name, raw in before.items())
    assert boot.request is request


@pytest.mark.parametrize('stopped', [False, True])
def test_crash_or_stop_cannot_satisfy_confirmed_normal_close(admitted_standalone, monkeypatch, stopped):
    f, boot, actors = admitted_standalone
    boot.launch_current(); assert not boot.poll_reader()
    request = confirm_next(f, boot)
    assert request.normal_close_attempt is not None
    before = boot.binding.current()
    if stopped:
        with pytest.raises(LaunchHold, match='explicit_stop_unqualified'):
            boot.subject.reader.stop_failed_start()
        actors[-1].code = 0  # simulated eventual exit; no termination adapter fabricated
        actors[-1].members = 0
    else:
        actors[-1].code = 17; actors[-1].members = 0
    assert boot.poll_reader()
    acquire = boot.acquisition.acquire
    monkeypatch.setattr(boot.acquisition, 'acquire', lambda *args, **kwargs: pytest.fail('abnormal Close started acquisition'))
    with pytest.raises(LaunchHold, match='original_normal_reader_close'):
        boot.apply_confirmed(request)
    assert boot.binding.current() == before and boot.request is request
    assert boot.owner.record['standalone_encyclopedia']['attempt']['outcome'] == 'abnormal_exit'
    # An explicit cancellation and fresh review of an already physically idle
    # installation is a new user decision, never adoption of the old Close claim.
    boot.cancel_update(request)
    monkeypatch.setattr(boot.acquisition, 'acquire', acquire)
    notice = boot.update_check.check()
    fresh = boot.confirm_update(notice.offer, confirmed=True)
    assert fresh.normal_close_attempt is None
    assert boot.apply_confirmed(fresh).installed.phase == 'activated'


def test_already_idle_update_has_positive_nonrunning_context(admitted_standalone):
    f, boot, _ = admitted_standalone
    request = confirm_next(f, boot)
    assert request.normal_close_attempt is None
    assert boot.owner.record['standalone_encyclopedia']['attempt'] is None
    assert boot.apply_confirmed(request).installed.phase == 'activated'


def test_compiled_factory_forwards_only_fixed_profile_to_stock_constructor(tmp_path, monkeypatch):
    from suite_core.encyclopedia import bootstrap_entry, entry_binding
    from core import encyclopedia_enrollment
    trust = b"synthetic public trust; no signature operation"
    profile = entry_binding.StandaloneEntryProfile(
        tmp_path / "root", tmp_path / "data", tmp_path / "data/settings.json",
        "a" * 64, trust, sha256(trust).hexdigest(), "beta", object(),
    )
    backend = object()
    calls, contexts = [], []
    monkeypatch.setattr(bootstrap_entry, "PROFILE", profile)
    def enrollment_needed(context):
        assert type(context) is encyclopedia_enrollment.EncyclopediaEnrollmentContext
        assert (context.root, context.data_root, context.settings_path) == (
            profile.root, profile.data_root, profile.settings_path,
        )
        assert (context.bootstrap_sha256, context.trust_bytes, context.trust_sha256) == (
            profile.bootstrap_sha256, profile.trust_bytes, profile.trust_sha256,
        )
        contexts.append(context)
        return False  # This forwarding unit selects the existing-install branch.
    monkeypatch.setattr(encyclopedia_enrollment, "compiled_enrollment_needed", enrollment_needed)
    def create(received):
        calls.append(received)
        return backend
    monkeypatch.setattr(entry_binding, "create_root_entry", create)
    assert bootstrap_entry.from_compiled_profile() is backend
    assert calls == [profile] and len(contexts) == 1
    with pytest.raises(ValueError, match="compiled_standalone_profile_arguments_forbidden"):
        bootstrap_entry.from_compiled_profile(object())
    assert calls == [profile] and len(contexts) == 1


def test_compiled_factory_real_validator_refuses_untyped_profile_before_native(monkeypatch):
    from suite_core.encyclopedia import bootstrap_entry, entry_binding
    monkeypatch.setattr(bootstrap_entry, "PROFILE", object())
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("untyped profile reached a native entry constructor")
    monkeypatch.setattr(entry_binding, "create_root_entry", forbidden)
    monkeypatch.setattr(entry_binding, "create_enrollment_entry", forbidden)
    with pytest.raises(ValueError, match="typed_explicit_compiled_profile_required"):
        bootstrap_entry.from_compiled_profile()
    assert calls == []


@pytest.mark.parametrize('standalone',[True],indirect=True)
def test_original_identity_observation_tracks_current_settings_and_pending_request(standalone):
    f,boot,actors=standalone
    first=boot.update_observation();assert first.release_schema==1 and first.maturity is None
    assert boot.installed_offer_context()==first.installed_context()
    settings=json.loads(f.settings.read_bytes());settings['future_observation_field']=7;f.settings.write_text(json.dumps(settings))
    second=boot.update_observation();assert second.identity!=first.identity
    boot.owner.begin_standalone_custody(boot.subject)
    request=confirm_next(f,boot)
    assert boot.update_observation().pending and boot.installed_offer_context().pending
    boot.cancel_update(request)


@pytest.mark.parametrize('standalone',[True],indirect=True)
def test_opted_host_legacy_update_retains_original_native_settlement(standalone):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    request=confirm_next(f,boot);result=boot.apply_confirmed(request)
    assert result.installed.phase=='activated'
    launch_close(boot,actors);assert boot.finish_target_health(request).phase=='healthy'


@pytest.mark.parametrize('schema2', [False, True])
@pytest.mark.parametrize('recovering', [False, True])
@pytest.mark.parametrize('fault', [None, 'stage', 'digest', 'directory', 'release',
                                   'missing', 'foreign', 'waiting', 'retained'])
def test_original_launch_projection_full_identity(admitted_standalone, monkeypatch, schema2, recovering, fault):
    """Injected launch boundary only; no schema2 acquisition/native acceptance."""
    from core.encyclopedia_connection import connection_from_release
    f, boot, actors = admitted_standalone
    request = confirm_next(f, boot)
    if schema2:
        target = replace(request.offer.release, software_version='0.2.0', schema_version=2, maturity='alpha')
        old = replace(request.current.selected, version='0.1.0', release_schema=2,
                      maturity='beta', release_sha256=request.current.release_sha256)
        request = replace(request, offer=replace(request.offer, release=target),
                          current=replace(request.current, selected=old))
    boot.owner._standalone_encyclopedia_update = request
    expected = request.current.selected if recovering else connection_from_release(f.root, request.offer.release)
    selected = expected
    release = request.current.release_sha256 if recovering else request.offer.release.manifest_sha256
    phase = 'recovering' if recovering else 'applying'
    if fault == 'stage':
        if schema2:
            selected = replace(selected, maturity='stable')
        else:
            selected = replace(selected, version='0.3.0-alpha.1')
    elif fault == 'digest':
        selected = replace(selected, inventory_sha256='0' * 64)
    elif fault == 'directory':
        selected = replace(selected, directory=f.root / 'Apps/other-same-number')
    elif fault == 'release':
        release = '0' * 64
    elif fault == 'missing':
        boot.owner._standalone_encyclopedia_update = None
    elif fault == 'foreign':
        boot.owner._standalone_encyclopedia_update = replace(request, owner_generation='foreign')
    elif fault == 'waiting':
        phase = 'waiting_for_close'
    elif fault == 'retained':
        boot.owner._standalone_encyclopedia_attempt = True
    from copy import deepcopy
    record = deepcopy(boot.owner.record)
    record['standalone_update'] = {'identity': request.identity, 'phase': phase}
    boot.owner._persist(record)
    monkeypatch.setattr(type(boot.binding), 'current', lambda self:
                        replace(request.current, selected=selected, release_sha256=release))
    launches = []
    class ReachedOriginalLaunch(Exception): pass
    def stop_before_native(self, selection):
        launches.append(selection)
        raise ReachedOriginalLaunch
    monkeypatch.setattr(type(boot.subject.reader), 'launch', stop_before_native)
    before = boot.owner.record['standalone_encyclopedia']['attempt']
    if fault is None:
        with pytest.raises(ReachedOriginalLaunch):
            boot.launch_current()
        assert launches == [expected]
        assert boot.owner.record['standalone_encyclopedia']['attempt']['selected'] == expected.mapping()
    else:
        reason = 'standalone_reader_or_stage_still_owned' if fault == 'retained' else 'cancel_or_settle_original'
        with pytest.raises(LaunchHold, match=reason):
            boot.launch_current()
        assert launches == [] and boot.owner.record['standalone_encyclopedia']['attempt'] == before
    assert actors == [] and f.sentinel.read_bytes() == b'{"unknown_future":[1,2]}\n'


def confirmed_metadata_request(f, boot, *, delegate=None, prepared=False):
    """Real service/owner orchestration; crypto and all process actors injected."""
    from types import SimpleNamespace
    from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy
    from tests.test_encyclopedia_installation import Response
    if not prepared:
        add_schema_package(f, '0.1.0-alpha.9')
    for i,row in enumerate(f.rows):
        row['id']=100+i
        for j,asset in enumerate(row['assets']):asset['id']=1000+i*10+j
    def opener(request, timeout):
        if request.full_url=='https://api.github.com/repos/DigitalSteel/CollectiGen':
            f.calls.append((request.full_url,timeout,None))
            return Response(json.dumps({'id':4321,'full_name':'DigitalSteel/CollectiGen'}).encode())
        return f.opener(request,timeout)
    transport=ComponentReleaseTransport(ReleaseTransportPolicy(
        schema2_metadata_assets=('collectigen-release.json','collectigen-release.json.sig')))
    transport._opener=SimpleNamespace(open=opener)
    boot.acquisition.transport.opener=transport
    boot.acquisition.schema2_metadata=True
    boot.acquisition._observe_installed=boot.installed_offer_context
    boot.acquisition._repository_id=4321
    notice=boot.update_check.check()
    assert notice.status=='available', notice
    return boot.confirm_update(notice.offer,confirmed=True,delegate=delegate)


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('running',[False,True])
def test_original_metadata_review_keeps_pending_and_state(standalone,running):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    if running:boot.launch_current();assert not boot.poll_reader()
    request=confirmed_metadata_request(f,boot)
    if running:
        actors[-1].code=0;actors[-1].members=0;assert boot.poll_reader()
    boot.owner.begin_standalone_encyclopedia_update(boot.subject,request)
    before=(boot.owner.raw,f.settings.read_bytes(),boot.binding.current(),f.sentinel.read_bytes())
    tree=set(boot.acquisition.root.iterdir());calls=len(f.calls)
    assert boot.revalidate_confirmed_metadata(request) is None
    assert len(f.calls)>calls and all(not url.endswith('.zip') for url,_,_ in f.calls[calls:])
    assert boot.installed_offer_context().pending and boot.request is request
    assert not boot.acquisition._acquired and set(boot.acquisition.root.iterdir())==tree
    assert (boot.owner.raw,f.settings.read_bytes(),boot.binding.current(),f.sentinel.read_bytes())==before
    # Normal notifications and revalidation still reject live pending state.
    assert boot.update_check.check().status=='held'
    with pytest.raises(ValueError,match='installed_context_held_or_changed'):
        boot.acquisition.revalidate_offer(request.offer)


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('fault',['request','phase','snapshot','cancel','delegate','generation',
    'post_snapshot','post_delegate','post_acquisition','post_cancel','subject'])
def test_original_metadata_review_rejects_changed_authority(standalone,monkeypatch,fault):
    from copy import deepcopy
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    request=confirmed_metadata_request(f,boot)
    boot.owner.begin_standalone_encyclopedia_update(boot.subject,request)
    before=len(f.calls);argument=request;delegate=None
    if fault=='request':argument=replace(request)
    elif fault=='phase':
        record=deepcopy(boot.owner.record);record['standalone_update']['phase']='waiting_for_close'
        boot.owner._persist(record)
    elif fault=='snapshot':
        settings=json.loads(f.settings.read_bytes());settings['changed_since_confirmation']=True
        f.settings.write_text(json.dumps(settings))
    elif fault=='cancel':boot.cancel.set()
    elif fault=='delegate':delegate=object()
    elif fault=='generation':
        argument=replace(request,owner_generation='foreign')
        boot.request=boot.owner._standalone_encyclopedia_update=argument
    elif fault=='subject':boot.subject=replace(boot.subject)
    elif fault.startswith('post_'):
        select=boot.acquisition._select_metadata
        def changed(*args):
            result=select(*args)
            if fault=='post_snapshot':
                settings=json.loads(f.settings.read_bytes());settings['changed_during_metadata']=True
                f.settings.write_text(json.dumps(settings))
            elif fault=='post_delegate':boot.delegate=object()
            elif fault=='post_acquisition':boot.acquisition=object()
            elif fault=='post_cancel':boot.cancel.set()
            return result
        monkeypatch.setattr(boot.acquisition,'_select_metadata',changed)
    acquisition=boot.acquisition
    with pytest.raises((ValueError,InterruptedError,LaunchHold)):
        boot.revalidate_confirmed_metadata(argument,delegate=delegate)
    assert not acquisition._offers and not acquisition._acquired
    assert actors==[] and boot.request is not None
    if not fault.startswith('post_'):assert len(f.calls)==before


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('fault',['generation','outcome','selection','phase'])
def test_metadata_checker_keeps_original_close_receipt(standalone,fault):
    from copy import deepcopy
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    boot.launch_current();assert not boot.poll_reader()
    request=confirmed_metadata_request(f,boot)
    actors[-1].code=0;actors[-1].members=0;assert boot.poll_reader()
    boot.owner.begin_standalone_encyclopedia_update(boot.subject,request)
    assert boot.owner.check_standalone_metadata_request(boot.subject,request) is None
    record=deepcopy(boot.owner.record);attempt=record['standalone_encyclopedia']['attempt']
    if fault=='generation':attempt['generation']+=1
    elif fault=='outcome':attempt['outcome']='abnormal_exit'
    elif fault=='selection':attempt['selected']['inventory_sha256']='0'*64
    else:attempt['phase']='running'
    boot.owner._persist(record);before=boot.owner.raw
    with pytest.raises(LaunchHold,match='original_normal_reader_close'):
        boot.owner.check_standalone_metadata_request(boot.subject,request)
    assert boot.owner.raw==before and boot.request is request


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('fault',[None,'before','after'])
def test_original_metadata_review_retains_typed_manager_delegate(standalone,original,monkeypatch,fault):
    f,boot,_=standalone;boot.owner.begin_standalone_custody(boot.subject)
    _,route,counts=original
    delegate=boot.delegate_to_manager(route,confirmed=True)
    request=confirmed_metadata_request(f,boot,delegate=delegate)
    boot.owner.begin_standalone_encyclopedia_update(boot.subject,request)
    before=len(f.calls)
    if fault=='before':counts[0]=0
    elif fault=='after':
        select=boot.acquisition._select_metadata
        def changed(*args):
            result=select(*args);counts[0]=0;return result
        monkeypatch.setattr(boot.acquisition,'_select_metadata',changed)
    if fault is None:
        assert boot.revalidate_confirmed_metadata(request,delegate=delegate) is None
        assert boot.installed_offer_context().pending
    else:
        with pytest.raises((LaunchHold,ValueError)):
            boot.revalidate_confirmed_metadata(request,delegate=delegate)
        assert not boot.acquisition._offers
        if fault=='before':assert len(f.calls)==before
    assert boot.request is request and not boot.acquisition._acquired


@pytest.mark.parametrize('standalone',[True],indirect=True)
def test_fresh_idle_metadata_review_does_not_adopt_prior_abnormal_close(standalone):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    launch_close(boot,actors,code=17)
    request=confirmed_metadata_request(f,boot)
    assert request.normal_close_attempt is None
    boot.owner.begin_standalone_encyclopedia_update(boot.subject,request)
    before=boot.owner.raw
    assert boot.revalidate_confirmed_metadata(request) is None
    assert boot.owner.raw==before
    assert boot.owner.record['standalone_encyclopedia']['attempt']['outcome']=='abnormal_exit'


def schema2_metadata_request(f,boot,stage='alpha',floor='0.0.0'):
    from urllib.parse import quote
    add_schema_package(f,'0.2.0')
    row=f.rows[-1]
    old={a['name']:a['browser_download_url'] for a in row['assets']}
    message=f.resources[old['collectigen-release.json']]
    release=f.registry[message]
    payload=json.loads(message);payload.update(maturity=stage,supported_from=floor)
    raw=json.dumps(payload).encode()
    f.registry[raw]=replace(release,schema_version=2,maturity=stage,manifest_sha256=sha256(raw).hexdigest())
    row['tag_name']=COMPONENT.value+'/releases/schema2/0.2.0/'+stage
    base='https://github.com/DigitalSteel/CollectiGen/releases/download/'+quote(row['tag_name'],safe='')+'/'
    for asset in row['assets']:
        url=base+asset['name']
        f.resources[url]=raw if asset['name']=='collectigen-release.json' else f.resources[old[asset['name']]]
        asset.update(browser_download_url=url,size=len(f.resources[url]))
    return confirmed_metadata_request(f,boot,prepared=True)


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('schema2',[False,True])
@pytest.mark.parametrize('continuing',[False,True])
def test_original_metadata_install_uses_one_begin_and_installer(standalone,monkeypatch,schema2,continuing):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    request=schema2_metadata_request(f,boot) if schema2 else confirmed_metadata_request(f,boot)
    begin=boot.owner.begin_standalone_encyclopedia_update;begins=[]
    def counted(subject,original):
        begins.append(original);return begin(subject,original)
    monkeypatch.setattr(boot.owner,'begin_standalone_encyclopedia_update',counted)
    if continuing:
        counted(boot.subject,request)
        boot.revalidate_confirmed_metadata(request)
    calls=len(f.calls);before=f.sentinel.read_bytes();settings=f.settings.read_bytes()
    opener=f.opener
    def unlocked(request,timeout):
        assert not boot.owner.dispatch_lock._is_owned(), 'root lock held across HTTP'
        return opener(request,timeout)
    monkeypatch.setattr(f,'opener',unlocked)
    def forbidden(*args,**kwargs):pytest.fail('ordinary acquisition/network review called in original handoff')
    monkeypatch.setattr(boot.acquisition,'acquire',forbidden)
    monkeypatch.setattr(boot._installation,'review',forbidden)
    result=boot.apply_confirmed_metadata(request)
    assert result.connection_saved and result.installed.phase=='activated'
    assert begins==[request] and sum(url.endswith('.zip') for url,_,_ in f.calls[calls:])==1
    assert f.sentinel.read_bytes()==before and f.settings.read_bytes()!=settings
    assert boot.owner.record['standalone_update']['identity']==request.identity
    assert 'component_stage' not in boot.owner.record
    if schema2:
        selected=boot.binding.current().selected
        assert (selected.release_schema,selected.maturity,selected.release_sha256)==(2,'alpha',request.offer.release.manifest_sha256)
    launch_close(boot,actors)
    assert boot.finish_target_health(request).phase=='healthy'
    assert boot.close() and boot.owner.fd is None


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('fault',['request','subject','generation','settings','cancel','delegate','reader'])
def test_original_handoff_refuses_before_archive(standalone,monkeypatch,fault):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    if fault=='reader':boot.launch_current();assert not boot.poll_reader()
    request=schema2_metadata_request(f,boot);argument=request;delegate=None
    before=f.sentinel.read_bytes();calls=len(f.calls)
    if fault=='request':argument=replace(request)
    elif fault=='subject':boot.subject=replace(boot.subject)
    elif fault=='generation':
        argument=replace(request,owner_generation='foreign')
        boot.request=boot.owner._standalone_encyclopedia_update=argument
    elif fault=='settings':
        settings=json.loads(f.settings.read_bytes());settings['changed_before_begin']=True
        f.settings.write_text(json.dumps(settings))
    elif fault=='cancel':boot.cancel.set()
    elif fault=='delegate':delegate=object()
    with pytest.raises((ValueError,LaunchHold,InterruptedError)):
        boot.apply_confirmed_metadata(argument,delegate=delegate)
    assert not any(url.endswith('.zip') for url,_,_ in f.calls[calls:])
    assert f.sentinel.read_bytes()==before
    assert len(actors)==(1 if fault=='reader' else 0)


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('fault',['acquired_copy','review_copy','cached_bytes','epoch','cancel','legacy_actor'])
def test_original_handoff_rejects_changed_installer_input(standalone,monkeypatch,fault):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    request=schema2_metadata_request(f,boot);before=boot.binding.current(),f.settings.read_bytes(),f.sentinel.read_bytes()
    install=boot._installation._install_original_acquired
    def changed(reviewed,acquired,**kwargs):
        if fault=='acquired_copy':acquired=replace(acquired)
        elif fault=='review_copy':reviewed=replace(reviewed)
        elif fault=='cached_bytes':acquired.artifact.write_bytes(b'corrupt original cache')
        elif fault=='epoch':boot.acquisition._offer_epoch+=1
        elif fault=='legacy_actor':
            from types import SimpleNamespace
            boot.subject.reader.legacy_handoff=SimpleNamespace(close_held=True)
        else:boot.cancel.set()
        return install(reviewed,acquired,**kwargs)
    monkeypatch.setattr(boot._installation,'_install_original_acquired',changed)
    with pytest.raises((ValueError,LaunchHold,InterruptedError)) as held:
        boot.apply_confirmed_metadata(request)
    if fault=='acquired_copy':assert 'original_acquired_handoff' in str(held.value)
    assert (boot.binding.current(),f.settings.read_bytes(),f.sentinel.read_bytes())==before
    assert boot.request is None and actors==[] and 'component_stage' not in boot.owner.record


@pytest.mark.parametrize('standalone',[True],indirect=True)
def test_original_handoff_cache_hit_is_fresh_and_uses_no_second_transfer(standalone):
    f,boot,_=standalone;boot.owner.begin_standalone_custody(boot.subject)
    request=schema2_metadata_request(f,boot)
    boot.owner.begin_standalone_encyclopedia_update(boot.subject,request)
    cached=boot.acquisition._acquire_original_metadata(request.offer,
        check_current=boot._confirmed_metadata_checker(request,None))
    calls=len(f.calls)
    result=boot.apply_confirmed_metadata(request)
    assert result.connection_saved and len(f.calls)>calls
    assert not any(url.endswith('.zip') for url,_,_ in f.calls[calls:])
    assert boot.acquired.artifact==cached.artifact and boot.acquired is not cached


@pytest.mark.parametrize('standalone',[True],indirect=True)
@pytest.mark.parametrize('recovering',[False,True])
def test_original_handoff_publication_retry_and_recovery(standalone,monkeypatch,recovering):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    old=boot.binding.current().selected;request=schema2_metadata_request(f,boot)
    save=boot._installation.store.save
    def fail(*args,**kwargs):raise PermissionError('synthetic original publication failure')
    if recovering:
        assert boot.apply_confirmed_metadata(request).connection_saved
        launch_close(boot,actors,code=17)
        monkeypatch.setattr(boot._installation.store,'save',fail)
        result=boot.recover_update(request,confirmed=True);expected=old
    else:
        monkeypatch.setattr(boot._installation.store,'save',fail)
        result=boot.apply_confirmed_metadata(request);expected=result.installed.selected
    assert not result.connection_saved and boot.request is request
    bytes_before=retained_component_bytes(f,expected);calls=list(f.calls)
    monkeypatch.setattr(boot._installation.store,'save',save)
    def deny(*args,**kwargs):pytest.fail('publication retry repeated metadata/acquisition')
    monkeypatch.setattr(boot.acquisition,'_acquire_original_metadata',deny)
    monkeypatch.setattr(boot.acquisition,'acquire',deny)
    assert boot.retry_connection(request).connection_saved
    assert f.calls==calls and retained_component_bytes(f,expected)==bytes_before
    launch_close(boot,actors)
    final=boot.finish_recovery_health(request) if recovering else boot.finish_target_health(request)
    assert final.phase in ('healthy','rolled_back') and boot.request is None


@pytest.mark.parametrize('standalone',[True],indirect=True)
def test_original_handoff_same_number_maturity_survives_publication(standalone):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    alpha=schema2_metadata_request(f,boot);assert boot.apply_confirmed_metadata(alpha).connection_saved
    launch_close(boot,actors);boot.finish_target_health(alpha)
    beta=schema2_metadata_request(f,boot,'beta');assert boot.apply_confirmed_metadata(beta).connection_saved
    selected=boot.binding.current().selected
    assert selected.version=='0.2.0' and selected.maturity=='beta'
    assert selected.release_sha256==beta.offer.release.manifest_sha256
    assert boot._installation.store.selected(boot._installation.store.observe())==selected
    launch_close(boot,actors);assert boot.finish_target_health(beta).phase=='healthy'


@pytest.mark.parametrize('standalone',[True],indirect=True)
def test_original_handoff_accepts_legacy_prerelease_at_numeric_floor(standalone):
    f,boot,actors=standalone;boot.owner.begin_standalone_custody(boot.subject)
    request=schema2_metadata_request(f,boot,floor='0.1.0')
    before=f.sentinel.read_bytes()
    result=boot.apply_confirmed_metadata(request)
    assert result.connection_saved and result.installed.selected.release_schema==2
    assert result.installed.selected.maturity=='alpha' and f.sentinel.read_bytes()==before
    launch_close(boot,actors)
    assert boot.finish_target_health(request).phase=='healthy' and boot.request is None
