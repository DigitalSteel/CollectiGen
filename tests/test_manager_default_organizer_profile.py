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
        assert acquisition._expected_offer is PROFILE.expected_offer
        assert acquisition._expected_offer.component_id == COMPONENT
        assert acquisition._expected_offer.software_version == "0.5.4-alpha.1"
        assert acquisition.manifest_name == "release-manifest.json"
        assert acquisition.signature_name == "release-manifest.json.sig"
        assert not ini.exists() and not ini.with_suffix(".json").exists()
    finally:
        executor.begin_close()


def test_default_organizer_profile_keeps_original_session_requirement():
    with pytest.raises(ValueError, match="original_native_normal_session_required"):
        factory_for_session(object())


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
