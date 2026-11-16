"""Default Manager composition must retain its reviewed Organizer download route."""
from types import SimpleNamespace
import pytest
from core.encyclopedia_connection import OwnedEncyclopedia
from core.organizer_connection import COMPONENT
from suite_core.manager.bootstrap_entry import NormalEntrySession
from suite_core.manager.organizer_acquisition_binding import factory_for_session
from suite_core.manager.compiled_organizer_config import PROFILE


def test_default_organizer_factory_uses_published_offer_without_fetch_or_launch(tmp_path, monkeypatch):
    import urllib.request
    def denied(*args, **kwargs):
        raise AssertionError("Constructing the installer must not fetch or launch")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
    session = object.__new__(NormalEntrySession)
    session.state = "offered"
    ini = tmp_path / "Data/Manager/settings.ini"
    session.admitted = SimpleNamespace(settings_path=ini, layout=SimpleNamespace(data_root=tmp_path / "Data"))
    reader = OwnedEncyclopedia(normal_entry_session=session)
    create = factory_for_session(session)
    assert callable(create)
    service, executor = create(reader, ini.with_suffix(".json"))
    try:
        acquisition = service.acquisition
        # The compiled offer names metadata/trust, not an old release pin.
        assert acquisition._expected_offer is None
        from core.component_release_transport import ComponentReleaseTransport
        assert type(acquisition.transport.opener) is ComponentReleaseTransport
        assert acquisition.manifest_name == "release-manifest.json"
        assert acquisition.signature_name == "release-manifest.json.sig"
        assert not ini.exists() and not ini.with_suffix(".json").exists()
    finally:
        executor.begin_close()


def test_default_organizer_profile_keeps_original_session_requirement():
    with pytest.raises(ValueError, match="original_native_normal_session_required"):
        factory_for_session(object())


@pytest.mark.parametrize('change', [None, 'mapping', 'trust', 'channel', 'missing'])
def test_sealed_organizer_profile_is_bound_to_original_update_session(tmp_path, monkeypatch, change):
    from suite_core.manager import compiled_organizer_config
    from suite_core.manager.organizer_acquisition_binding import CompiledOrganizerQualificationProfile
    from core.manager_update_host import SealedLocalQualificationOpener
    from suite_core.discovery.component_staging import ComponentStageAdmission
    profile = CompiledOrganizerQualificationProfile(PROFILE, 'b'*64)
    monkeypatch.setattr(compiled_organizer_config, 'PROFILE', profile)
    session = object.__new__(NormalEntrySession)
    session.state = 'offered'
    session.admitted = SimpleNamespace(settings_path=tmp_path/'Data/Manager/settings.ini',
        layout=SimpleNamespace(code_root=tmp_path, data_root=tmp_path/'Data'))
    session.update_profile = dict(transport_id=SealedLocalQualificationOpener('b'*64).transport_id,
                                 trust_sha256=PROFILE.trust_sha256, channel='beta')
    if change == 'mapping': session.update_profile['transport_id'] = SealedLocalQualificationOpener('c'*64).transport_id
    elif change == 'trust': session.update_profile['trust_sha256'] = '0'*64
    elif change == 'channel': session.update_profile['channel'] = 'stable'
    elif change == 'missing': session.update_profile = None
    if change is not None:
        with pytest.raises(ValueError, match='original_sealed_organizer_profile_binding_required'):
            factory_for_session(session)
        assert not (tmp_path/'Data').exists()
        return
    reader = OwnedEncyclopedia(normal_entry_session=session)
    service, executor = factory_for_session(session)(reader, session.admitted.settings_path.with_suffix('.json'))
    try:
        opener = service.acquisition.transport.opener
        assert type(opener) is SealedLocalQualificationOpener
        assert opener.root == tmp_path and opener.mapping_sha256 == 'b'*64
        assert opener.component_ids == (COMPONENT.value,)
        assert service.acquisition._expected_offer is None  # prior and target are discovered, not pinned as one.
        assert type(service.admission) is ComponentStageAdmission
        assert service.native_owner.normal_entry_session is session and service.native_owner.reader_owner is reader
        assert service.native_health.__self__ is service
        assert not session.admitted.settings_path.with_suffix('.json').exists()
    finally:
        executor.begin_close()


@pytest.mark.parametrize('value', [None, '', 'x'*64, 'b'*63, b'b'*64])
def test_sealed_organizer_profile_requires_typed_exact_map(value):
    from suite_core.manager.organizer_acquisition_binding import CompiledOrganizerQualificationProfile
    with pytest.raises(ValueError, match='typed_sealed_organizer_profile_required'):
        CompiledOrganizerQualificationProfile(PROFILE, value)


def test_sealed_organizer_profile_refuses_foreign_base():
    from suite_core.manager.organizer_acquisition_binding import CompiledOrganizerQualificationProfile
    with pytest.raises(ValueError,match='typed_sealed_organizer_profile_required'):
        CompiledOrganizerQualificationProfile(object(),'b'*64)


@pytest.mark.parametrize('frozen', [False, True])
def test_organizer_child_environment_excludes_only_frozen_bundle_paths(monkeypatch, frozen):
    import core.organizer_native_owner as owner
    bundle = r'C:\Disposable Suite\Apps\Manager\current\_internal'
    entries = [bundle, '"' + bundle.upper() + r'\PySide6"', bundle + '-other',
               r'C:\Windows\System32', r'C:\User Tools', '']
    monkeypatch.setattr(owner.sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(owner.sys, '_MEIPASS', bundle, raising=False)
    monkeypatch.setenv('PATH', ';'.join(entries))
    monkeypatch.setenv('QT_PLUGIN_PATH', 'host-plugin')
    monkeypatch.setenv('PYTHONHOME', 'host-python')
    monkeypatch.setenv('COLLECTIGEN_TEST_VALUE', 'preserved')
    before = dict(owner.os.environ)
    child = owner._organizer_environment()
    assert 'QT_PLUGIN_PATH' not in child and 'PYTHONHOME' not in child
    assert child['PATH'].split(';') == (entries[2:] if frozen else entries)
    assert child['COLLECTIGEN_TEST_VALUE'] == 'preserved'
    assert dict(owner.os.environ) == before
