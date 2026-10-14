"""Inert exact-package compatibility gates; crypto injected, no native qualification."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest
from core.encyclopedia_connection import COMPONENT
from core.encyclopedia_installation import EncyclopediaInstallation
from core import encyclopedia_update_binding as binding
from release_contract import ReleaseArtifact
from suite_core.discovery.offline_component_producer import produce_inventory
from tests.test_encyclopedia_installation import Fixture, no_external_actions


def package(f, version='0.1.0-alpha.8', change=None):
    f.add(version)
    module = json.loads(Path('suite_core/encyclopedia/MODULE_MANIFEST.json').read_text())
    module['software_version'] = version
    path = binding.MODULE
    if change == 'a018-layout':
        path = 'TagEncyclopedia/_internal/suite_core/encyclopedia/MODULE_MANIFEST.json'
        module.pop('update_data_contract')
    elif change == 'missing-contract': module.pop('update_data_contract')
    elif change == 'malformed-contract': module['update_data_contract']['schema'] = 1
    elif change == 'host-interface':
        module['update_data_contract']['snapshot_interface_versions'] = [1]
        module['update_data_contract']['required_snapshot_interface_versions'] = [1]
    elif change == 'forgotten-obligation':
        module['update_data_contract']['required_snapshot_interface_versions'] = [1]
    raw_module = json.dumps(module).encode()
    members = [('TagEncyclopedia/TagEncyclopedia.exe', b'INERT NOT EXECUTABLE'), (path, raw_module)]
    inventory = produce_inventory(tuple(members), component_id=COMPONENT, software_version=version,
        os_name='windows', architecture='x86_64')
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('BOOTSTRAP_INVENTORY.json', inventory)
        for name, raw in members:
            archive.writestr(name, raw + (b' ' if change == 'module-pin' and name == path else b''))
    data = output.getvalue()
    assets = {a['name']: a for a in f.rows[-1]['assets']}
    asset = next(a for a in assets.values() if a['name'].endswith('.zip'))
    f.resources[asset['browser_download_url']] = data; asset['size'] = len(data)
    message = f.resources[assets['collectigen-release.json']['browser_download_url']]
    f.registry[message] = replace(f.registry[message], artifact=ReleaseArtifact(asset['name'],
        'component-package', sha256(data).hexdigest(), len(data), sha256(inventory).hexdigest()))
    return asset['browser_download_url']


def standalone(f):
    return EncyclopediaInstallation(f.acquisition, f.store, admission=f.admission, standalone_compatibility=True)


@pytest.mark.parametrize('change', ['a018-layout', 'missing-contract', 'malformed-contract', 'host-interface', 'module-pin', 'forgotten-obligation'])
def test_incompatible_exact_package_is_downloaded_only_after_consent_and_never_staged(tmp_path, monkeypatch, change):
    f = Fixture(tmp_path, monkeypatch); url = package(f, change=change); service = standalone(f)
    settings, sentinel = f.settings.read_bytes(), f.sentinel.read_bytes()
    offer, = service.discover(channel='beta'); reviewed = service.review(offer)
    assert not any(call[0] == url for call in f.calls)
    with pytest.raises(ValueError, match='confirmation'):
        service.install(reviewed)
    assert not any(call[0] == url for call in f.calls)
    with pytest.raises(binding.StandaloneReaderCompatibilityHold):
        service.install(reviewed, confirmed=True)
    assert any(call[0] == url for call in f.calls)
    assert not (f.root / 'Apps').exists()
    assert not (f.root / 'State' / COMPONENT.value / 'transaction.json').exists()
    assert f.settings.read_bytes() == settings and f.sentinel.read_bytes() == sentinel
    with pytest.raises(ValueError, match='confirmation'):
        service.install(reviewed, confirmed=True)  # failed exact offer consent is spent


def test_current_contract_installs_even_at_historical_version_without_version_floor(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch); package(f, '0.1.0-alpha.8'); service = standalone(f)
    offer, = service.discover(channel='beta')
    result = service.install(service.review(offer), confirmed=True)
    assert result.connection_saved and result.installed.selected.version == '0.1.0-alpha.8'


def test_generic_manager_default_keeps_existing_legacy_path(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch); f.add()
    assert not f.service.standalone_compatibility
    assert f.install().connection_saved


def test_failed_update_retains_original_selection_and_bytes(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch); package(f); installed = f.install()
    f.service.native_health = lambda _: True; f.service.confirm_health()
    before = f.service.observe(); settings = f.settings.read_bytes()
    package(f, '0.1.0-alpha.9', 'missing-contract'); service = standalone(f)
    offer, = service.discover(channel='beta'); reviewed = service.review(offer)
    with pytest.raises(ValueError, match='signed_named'):
        service.install(reviewed, confirmed=True)
    assert service.observe() == before and f.settings.read_bytes() == settings
    assert installed.installed.selected.program.read_bytes() == b'INERT NOT EXECUTABLE'
    assert not (f.root / 'Apps' / COMPONENT.value / '0.1.0-alpha.9').exists()


def test_foreign_component_offer_cannot_be_reviewed_under_reader_consent(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch); package(f); service = standalone(f)
    offer, = service.discover(channel='beta')
    foreign = replace(offer, release=replace(offer.release, component_id=type(COMPONENT)('collectigen.suite-manager')))
    with pytest.raises(ValueError, match='exact_encyclopedia_offer'):
        service.review(foreign)
    assert not (f.root / 'Apps').exists()


def test_stale_settings_after_acquisition_hold_even_compatible_package(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch); package(f); service = standalone(f)
    offer, = service.discover(channel='beta'); reviewed = service.review(offer)
    acquire = f.acquisition.acquire
    def changed(*args, **kwargs):
        result = acquire(*args, **kwargs)
        f.settings.write_text('{"future_field":{"new":true}}')
        return result
    monkeypatch.setattr(f.acquisition, 'acquire', changed)
    with pytest.raises(ValueError, match='Settings changed'):
        service.install(reviewed, confirmed=True)
    assert not (f.root / 'Apps').exists()
