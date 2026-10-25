"""Inert first-install source contracts; no production entry or native launch."""
from dataclasses import replace
from hashlib import sha256
import json
import sys
import sqlite3
from types import SimpleNamespace

import pytest

from core.component_acquisition import ComponentAcquisition
from core.encyclopedia_connection import COMPONENT
from core.encyclopedia_enrollment import EncyclopediaEnrollmentContext, StandaloneEncyclopediaEnrollment
from core.manager_launch_owner import DeploymentOwner, LaunchHold
from suite_core.encyclopedia import bootstrap_entry
from tests.test_encyclopedia_installation import Fixture, no_external_actions
from tests.test_standalone_encyclopedia_update import add_schema_package, synthetic_contract_vocabulary
from tests.test_encyclopedia_connection import Native


@pytest.fixture
def enrollment(tmp_path, monkeypatch):
    synthetic_contract_vocabulary(monkeypatch)
    from core.encyclopedia_update_binding import _data_contract
    from tests.test_standalone_encyclopedia_update import CONTRACT
    monkeypatch.setattr('core.encyclopedia_update_binding.standalone_host_data_contract', lambda:
        _data_contract(json.dumps({'module_id': COMPONENT.value, 'software_version': 'synthetic-host',
            'update_data_contract': CONTRACT}).encode(), 'synthetic-host'))
    f = Fixture(tmp_path, monkeypatch)
    f.database = f.data / 'synthetic-encyclopedia.db'
    with sqlite3.connect(f.database) as database:
        database.execute('CREATE TABLE synthetic_user_corrections (tag TEXT, correction TEXT)')
        database.execute('INSERT INTO synthetic_user_corrections VALUES (?, ?)', ('inert_tag', 'retain bytes'))
    add_schema_package(f, '0.1.0-alpha.8')
    monkeypatch.setattr('core.encyclopedia_update_binding.verify_component_release', f.verify)
    bootstrap = f.root / 'TagEncyclopedia.exe'
    bootstrap.write_bytes(b'INERT STABLE BOOTSTRAP; NEVER EXECUTE')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(bootstrap))
    context = EncyclopediaEnrollmentContext(f.root, f.data, f.settings,
        sha256(bootstrap.read_bytes()).hexdigest(), f.trust, sha256(f.trust).hexdigest())
    cache = f.data / 'Encyclopedia/UpdateCache/first-install'
    cache.mkdir(parents=True)
    acquisition = ComponentAcquisition(cache_root=cache, cache_owner='inert-enrollment-fixture',
        opener=f.opener, host_version='0.2.1-alpha.16', trust_bytes=f.trust,
        trust_sha256=context.trust_sha256, max_artifact_bytes=64 * 1024 * 1024)
    owner = DeploymentOwner(f.root, data_root=f.data).acquire()
    from types import SimpleNamespace
    from tests.test_encyclopedia_entry_control import stock_native_double
    facade = SimpleNamespace(owner=owner, binding=SimpleNamespace(
        bootstrap_program=context.root / 'TagEncyclopedia.exe',
        current=lambda: owner._standalone_encyclopedia_subject.binding.current()))
    owner._fixture_stock_native = stock_native_double(facade, monkeypatch)
    yield f, context, acquisition, owner
    owner.abandon()  # preserve retained evidence; do not claim closure


def enroll(values):
    f, context, acquisition, owner = values
    adapter = StandaloneEncyclopediaEnrollment(context, acquisition, owner)
    owner.begin_standalone_custody(adapter.subject)
    return adapter


def reviewed(adapter):
    offer, = adapter.installation.discover(channel='beta')
    assert offer.operation == 'install' and offer.current_version is None
    return adapter.installation.review(offer)


def test_first_install_uses_stock_origin_journal_and_preserves_data(enrollment):
    f, context, acquisition, owner = enrollment
    adapter = enroll(enrollment)
    base = f.root / 'State' / COMPONENT.value
    assert not (base / 'component-origin.json').exists()
    before = f.sentinel.read_bytes()
    result = adapter.installation.install(reviewed(adapter), confirmed=True)
    assert result.connection_saved and result.installed.phase == 'activated'
    assert (base / 'component-origin.json').exists() and (base / 'transaction.json').exists()
    binding = adapter.enrolled_binding()
    assert binding.initial.selected == result.installed.selected and binding.initial.phase == 'activated'
    assert f.sentinel.read_bytes() == before
    assert json.loads(f.settings.read_text())['future_field'] == {'keep': True}
    assert owner.fd is not None and owner.children == {} and owner.native_transport is None
    assert owner._creation_entered and not owner._standalone_reader_creation_entered
    assert owner._standalone_root_custody is not None and 'component_stage' not in owner.record
    assert bootstrap_entry.PROFILE is None and bootstrap_entry.from_compiled_profile() is None
    with pytest.raises(ValueError, match='Qualified native'):
        adapter.installation.confirm_health()
    with pytest.raises(LaunchHold, match='enrollment_excludes'):
        owner.arm_standalone_encyclopedia(binding)


def test_enrollment_does_not_authorize_creation_or_manager_commands(enrollment):
    adapter = enroll(enrollment)
    owner = adapter.owner
    with pytest.raises(LaunchHold, match='enrollment_excludes'):
        owner.spawn('manager', ('NEVER-EXECUTE.exe',))
    with pytest.raises(LaunchHold, match='enrollment_excludes'):
        owner.arm_model_free_normal(('NEVER-EXECUTE.exe', '--code-root'))
    assert owner._creation_entered and not owner._standalone_reader_creation_entered
    assert owner.native_transport is None


def test_first_install_requires_explicit_current_review(enrollment):
    adapter = enroll(enrollment)
    with pytest.raises(ValueError, match='explicit_current'):
        adapter.installation.install(reviewed(adapter))
    assert adapter.owner.fd is not None
    assert not (adapter.context.root / 'State' / COMPONENT.value / 'transaction.json').exists()


def test_forged_precreation_subject_and_other_component_refuse(enrollment):
    adapter = enroll(enrollment)
    forged = replace(adapter.subject)
    with pytest.raises(LaunchHold, match='original_precreation'):
        with adapter.owner.standalone_enrollment_stage_hold(forged, COMPONENT):
            pytest.fail('forged subject admitted')
    with pytest.raises(LaunchHold, match='exact_idle'):
        with adapter.owner.standalone_enrollment_stage_hold(adapter.subject, None):
            pytest.fail('foreign component admitted')


@pytest.mark.parametrize('state', ['creation', 'transport', 'pending', 'reservation'])
def test_first_install_rejects_entered_or_uncertain_owner(enrollment, state):
    f, context, acquisition, owner = enrollment
    if state == 'creation':
        owner._creation_entered = True
    elif state == 'transport':
        owner.native_transport = object()
    elif state == 'pending':
        owner.pending.append({'role': 'unknown', 'process': object()})
    else:
        owner.record['reservations']['unknown'] = {'role': 'unknown'}
        owner._persist()
    with pytest.raises(LaunchHold):
        enroll(enrollment)
    assert owner._standalone_enrollment_subject is None
    assert owner.record['phase'] == 'launch_hold'


def test_first_install_rejects_unmanaged_payload_and_existing_ledger(enrollment):
    f, context, acquisition, owner = enrollment
    path = f.root / 'Apps' / COMPONENT.value
    path.mkdir(parents=True)
    with pytest.raises(LaunchHold, match='absent_first_install'):
        enroll(enrollment)
    assert owner.record['phase'] == 'launch_hold'


def test_original_lease_excludes_second_manager_owner(enrollment):
    f, context, acquisition, owner = enrollment
    enroll(enrollment)
    other = DeploymentOwner(f.root, data_root=f.data)
    with pytest.raises(LaunchHold, match='already_open'):
        other.acquire()
    assert owner.fd is not None and owner.marker.read_bytes() == owner.raw


def test_bootstrap_and_settings_stale_guards_hold_before_install(enrollment):
    f, context, acquisition, owner = enrollment
    adapter = enroll(enrollment)
    review = reviewed(adapter)
    f.settings.write_text('{"future_field":{"user_correction":true}}')
    with pytest.raises(ValueError, match='Settings changed'):
        adapter.installation.install(review, confirmed=True)
    assert not (f.root / 'State' / COMPONENT.value / 'transaction.json').exists()
    (f.root / 'TagEncyclopedia.exe').write_bytes(b'CHANGED INERT BOOTSTRAP')
    with pytest.raises(ValueError, match='bootstrap_changed'):
        adapter.installation.observe()


def test_publication_retry_preserves_activated_code_and_new_user_settings(enrollment, monkeypatch):
    f, context, acquisition, owner = enrollment
    adapter = enroll(enrollment)
    store = adapter.installation.store
    save = store.save
    monkeypatch.setattr(store, 'save', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('inert save failure')))
    result = adapter.installation.install(reviewed(adapter), confirmed=True)
    assert not result.connection_saved and result.installed.phase == 'activated'
    base = f.root / 'State' / COMPONENT.value
    journal = (base / 'transaction.json').read_bytes()
    f.settings.write_text('{"future_field":{"user_correction":true}}')
    monkeypatch.setattr(store, 'save', save)
    assert adapter.installation.retry_connection().connection_saved
    assert (base / 'transaction.json').read_bytes() == journal
    assert json.loads(f.settings.read_text())['future_field'] == {'user_correction': True}
    assert adapter.enrolled_binding().initial.phase == 'activated'


def test_first_install_recovery_is_stock_absent_prior_without_data_rollback(enrollment):
    f, context, acquisition, owner = enrollment
    adapter = enroll(enrollment)
    before = f.sentinel.read_bytes()
    installed = adapter.installation.install(reviewed(adapter), confirmed=True)
    recovered = adapter.installation.recover(confirmed=True)
    assert recovered.connection_saved and recovered.installed.selected is None
    assert installed.installed.selected.program.exists() and f.sentinel.read_bytes() == before
    assert owner.fd is not None and owner._creation_entered and not owner._standalone_reader_creation_entered
    with pytest.raises(ValueError, match='signed_current'):
        adapter.enrolled_binding()


def test_existing_component_journal_is_not_first_install_evidence(enrollment):
    f, context, acquisition, owner = enrollment
    base = f.root / 'State' / COMPONENT.value
    base.mkdir(parents=True)
    (base / 'transaction.json').write_bytes(b'INERT UNTRUSTED LEDGER; DO NOT PARSE OR ADOPT')
    with pytest.raises(LaunchHold, match='absent_first_install'):
        enroll(enrollment)
    assert owner._standalone_enrollment_subject is None


def test_stage_loss_retains_exclusion_and_binding_cannot_escape_active_stage(enrollment):
    f, context, acquisition, owner = enrollment
    adapter = enroll(enrollment)
    with pytest.raises(ValueError, match='bootstrap_changed'):
        with owner.standalone_enrollment_stage_hold(adapter.subject, COMPONENT) as scope:
            with pytest.raises(ValueError, match='stage_still_owned'):
                adapter.enrolled_binding()
            (f.root / 'TagEncyclopedia.exe').write_bytes(b'CHANGED INERT BOOTSTRAP')
            scope.check()
    assert 'component_stage' in owner.record and owner.fd is not None
    assert owner.children == {} and owner._creation_entered
    assert not owner._standalone_reader_creation_entered


def install_and_handoff(enrollment, monkeypatch):
    adapter = enroll(enrollment)
    adapter.installation.install(reviewed(adapter), confirmed=True)
    actors = []
    from tests.test_standalone_encyclopedia_update import StockReaderControls
    from suite_core.encyclopedia.control_entry import StandaloneControlActor
    state = adapter.owner._fixture_stock_native[1]
    original_init = StandaloneControlActor.__init__
    def retained_actor(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        actors.append(StockReaderControls(self, state))
    monkeypatch.setattr(StandaloneControlActor, '__init__', retained_actor)
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window', lambda _: True)
    backend = adapter.handoff_reader()
    adapter.owner.enable_standalone_control(backend.subject)
    return adapter, backend, actors


def close_original_reader(backend, actors, *, code=0):
    backend.launch_current()
    assert not backend.poll_reader()
    actor = actors[-1]
    actor.code, actor.members = code, 0
    assert backend.poll_reader() and actor.closed


def test_retained_enrollment_handoff_initializes_control_before_actual_owner_birth(enrollment, monkeypatch):
    from suite_core.encyclopedia import entry_binding, control_entry
    f, context, acquisition, owner = enrollment
    adapter=enroll(enrollment)
    adapter.installation.install(reviewed(adapter),confirmed=True)
    backend=adapter.handoff_reader()
    anchor=owner._standalone_root_custody; lease=owner.fd; generation=owner.record['owner']
    assert owner._standalone_control_factory is None and not owner._standalone_reader_creation_entered
    # Only READY delivery is an inert fixture; factory, owner launch and original
    # control constructor/native identity checks are the production source route.
    from core.manager_launch_owner import WindowsControlChannel
    custody_write = WindowsControlChannel.write_frame
    def original_channel_write(channel, raw, **kwargs):
        if channel.peer_handle == 83:
            return custody_write(channel, raw, **kwargs)
        assert channel.peer_handle == 81
        assert json.loads(raw[4:])['kind'] == 'offer'
    monkeypatch.setattr(WindowsControlChannel, 'write_frame', original_channel_write)
    monkeypatch.setattr(control_entry.EncyclopediaRootControl,'accept_ready',lambda self:setattr(self,'ready',True))
    started,control=entry_binding.start_original_reader(backend)
    assert started is backend and type(control) is control_entry.EncyclopediaRootControl and control.ready
    assert backend.subject.reader.factory is owner._standalone_control_factory
    assert owner._standalone_reader_creation_entered and owner._standalone_control_actor is backend.subject.reader.native
    assert owner._standalone_root_custody is anchor and owner.fd==lease and owner.record['owner']==generation


def test_explicit_beta_profile_survives_stable_first_install_handoff(enrollment, monkeypatch):
    from suite_core.encyclopedia import entry_binding
    f, context, acquisition, owner = enrollment
    add_schema_package(f,'0.1.0')
    f.rows[-1]['prerelease'] = False
    stable_message = next(f.resources[a['browser_download_url']] for a in f.rows[-1]['assets']
        if a['name'] == 'collectigen-release.json')
    f.registry[stable_message] = replace(f.registry[stable_message], channel='stable')
    profile=entry_binding.StandaloneEntryProfile(context.root,context.data_root,context.settings_path,
        context.bootstrap_sha256,context.trust_bytes,context.trust_sha256,'beta',acquisition)
    # Reuse the actual already acquired fixture owner, never manufacture a second
    # root owner or change its API after custody birth.
    monkeypatch.setattr(entry_binding,'DeploymentOwner',lambda *a,**kw:SimpleNamespace(acquire=lambda:owner))
    adapter=entry_binding.create_enrollment_entry(profile)
    assert adapter.channel=='beta'
    offer,=adapter.installation.discover(channel='stable')
    result=adapter.installation.install(adapter.installation.review(offer),confirmed=True)
    assert result.installed.selected.version=='0.1.0'
    backend=adapter.handoff_reader()
    assert backend.update_check.channel=='beta' and backend.owner is owner


def test_handoff_retains_original_owner_lease_then_normal_close_health_and_retirement(enrollment, monkeypatch):
    f, context, acquisition, owner = enrollment
    lease, generation = owner.fd, owner.record['owner']
    database = f.database.read_bytes()
    adapter, backend, actors = install_and_handoff(enrollment, monkeypatch)
    assert backend.owner is owner and backend.subject.lease == lease and owner.fd == lease
    assert owner.record['owner'] == generation and owner.record['phase'] == 'standalone_encyclopedia'
    assert owner._standalone_enrollment_subject is None
    assert adapter.handoff_reader() is backend and actors == []
    assert backend.close_held
    with pytest.raises(LaunchHold, match='standalone_close_waits'):
        backend.close()
    close_original_reader(backend, actors)
    assert backend.finish_initial_health().phase == 'healthy'
    assert f.database.read_bytes() == database
    receipt = backend.close()
    assert owner.fd is None and not owner.marker.exists() and backend.close() == receipt
    assert bootstrap_entry.PROFILE is None and bootstrap_entry.from_compiled_profile() is None


def test_handoff_revokes_old_stage_subject_and_refuses_competing_owner(enrollment, monkeypatch):
    f, context, acquisition, owner = enrollment
    adapter, backend, actors = install_and_handoff(enrollment, monkeypatch)
    with pytest.raises(LaunchHold):
        adapter.installation.observe()
    with pytest.raises(LaunchHold):
        owner.handoff_standalone_enrollment(replace(adapter.subject), backend.binding)
    with pytest.raises(LaunchHold, match='already_open'):
        DeploymentOwner(f.root, data_root=f.data).acquire()
    assert owner.fd == backend.subject.lease and owner.children == {}


def test_failed_save_holds_handoff_then_retry_preserves_database_and_journal(enrollment, monkeypatch):
    f, context, acquisition, owner = enrollment
    adapter = enroll(enrollment)
    database = f.database.read_bytes()
    save = adapter.installation.store.save
    monkeypatch.setattr(adapter.installation.store, 'save', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('inert save failure')))
    assert not adapter.installation.install(reviewed(adapter), confirmed=True).connection_saved
    journal = (f.root / 'State' / COMPONENT.value / 'transaction.json').read_bytes()
    with pytest.raises(ValueError, match='recover_original_component_transaction'):
        adapter.handoff_reader()
    assert owner._standalone_enrollment_subject is adapter.subject and owner._standalone_encyclopedia_subject is None
    f.settings.write_text('{"future_field":{"later_user_edit":true}}')
    monkeypatch.setattr(adapter.installation.store, 'save', save)
    assert adapter.installation.retry_connection().connection_saved
    backend = adapter.handoff_reader()
    assert backend.owner is owner and backend.binding.initial.phase == 'activated'
    assert (f.root / 'State' / COMPONENT.value / 'transaction.json').read_bytes() == journal
    assert f.database.read_bytes() == database
    assert json.loads(f.settings.read_text())['future_field'] == {'later_user_edit': True}


def test_live_reader_and_incomplete_drain_refuse_initial_health_and_close(enrollment, monkeypatch):
    adapter, backend, actors = install_and_handoff(enrollment, monkeypatch)
    database = enrollment[0].database.read_bytes()
    backend.launch_current(); assert not backend.poll_reader()
    with pytest.raises(LaunchHold, match='idle_original_standalone_component_required'):
        backend.finish_initial_health()
    actor = actors[-1]; actor.code = 0
    with pytest.raises(LaunchHold):
        backend.close()
    actor.members = 0; actor.drained.clear()
    assert not backend.poll_reader() and not actor.closed
    with pytest.raises(LaunchHold, match='idle_original_standalone_component_required'):
        backend.finish_initial_health()
    actor.drained.set(); assert backend.poll_reader()
    backend.finish_initial_health(); backend.close()
    assert enrollment[0].database.read_bytes() == database


@pytest.mark.parametrize('outcome', ['abnormal', 'stopped', 'not_visible'])
def test_unhealthy_original_reader_retains_lease_until_fresh_normal_run(enrollment, monkeypatch, outcome):
    f, context, acquisition, owner = enrollment
    adapter, backend, actors = install_and_handoff(enrollment, monkeypatch)
    database = f.database.read_bytes()
    if outcome == 'not_visible':
        monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window', lambda _: False)
    backend.launch_current(); assert not backend.poll_reader()
    actor = actors[-1]
    if outcome == 'stopped':
        with pytest.raises(LaunchHold, match='explicit_stop_unqualified'):
            backend.subject.reader.stop_failed_start()
        actor.code, actor.members = 0, 0  # inert eventual exit, no termination authority
    else:
        actor.code, actor.members = (17 if outcome == 'abnormal' else 0), 0
    assert backend.poll_reader()
    with pytest.raises(ValueError, match='native_reader_health_held'):
        backend.finish_initial_health()
    with pytest.raises(LaunchHold, match='standalone_close_waits'):
        backend.close()
    assert owner.fd is not None and backend.binding.current().phase == 'activated'
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window', lambda _: True)
    close_original_reader(backend, actors)
    backend.finish_initial_health(); backend.close()
    assert f.database.read_bytes() == database


def test_initial_health_preserves_settings_edits_after_handoff(enrollment, monkeypatch):
    f, context, acquisition, owner = enrollment
    adapter, backend, actors = install_and_handoff(enrollment, monkeypatch)
    current = json.loads(f.settings.read_text()); current['future_field'] = {'after_handoff': True}
    f.settings.write_text(json.dumps(current))
    close_original_reader(backend, actors)
    assert backend.finish_initial_health().phase == 'healthy'
    assert json.loads(f.settings.read_text())['future_field'] == {'after_handoff': True}
    backend.close()
