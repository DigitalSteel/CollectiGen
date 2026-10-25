"""Inert doubles over stock transaction IO; no native or publisher qualification."""
from hashlib import sha256
from io import BytesIO
import json
from urllib.parse import quote
import zipfile

import pytest
from tests.test_encyclopedia_installation import Fixture, no_external_actions
from core import organizer_connection
from core.organizer_connection import COMPONENT, POLICY
from core.organizer_connection_settings import OrganizerConnectionStore
from core.organizer_installation import OrganizerInstallation
from release_contract import ReleaseArtifact
from suite_core.discovery import component_staging as staging
from suite_core.discovery.component_release_signature import VerifiedComponentRelease
from suite_core.discovery.offline_component_producer import produce_inventory


class OrganizerFixture(Fixture):
    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.store = OrganizerConnectionStore(self.settings)
        self.acquisition.maximum = POLICY.max_archive_bytes
        self.service = OrganizerInstallation(self.acquisition, self.store, admission=self.admission)
        monkeypatch.setattr(organizer_connection, 'verify_component_release', self.verify)

    def add(self, version='0.4.6-alpha.1', contents=b'INERT NOT EXECUTABLE'):
        files = tuple((name, contents) for name in sorted(organizer_connection.REQUIRED_ENTRIES))
        inventory = produce_inventory(files, component_id=COMPONENT, software_version=version,
                                      os_name='windows', architecture='x86_64', runtime_kind='interpreter-portable')
        output = BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr(staging.INVENTORY, inventory)
            for name, raw in files: archive.writestr(name, raw)
        raw = output.getvalue(); name = 'inert-' + version + '.zip'
        artifact = ReleaseArtifact(name, 'component-package', sha256(raw).hexdigest(), len(raw), sha256(inventory).hexdigest())
        message = json.dumps({'version':version, 'release_notes':'INERT contract fixture'}).encode()
        self.registry[message] = VerifiedComponentRelease(COMPONENT, version, 'beta', 'windows', 'x86_64',
                                                         artifact, 'UNSIGNED-FIXTURE', sha256(message).hexdigest())
        base = 'https://github.com/DigitalSteel/CollectiGen/releases/download/' + quote(COMPONENT.value+'/v'+version, safe='') + '/'
        for filename, data in (('collectigen-release.json',message), ('collectigen-release.json.sig',b'UNSIGNED INERT CONTRACT'), (name,raw)):
            self.resources[base+filename]=data
        self.rows.append(dict(tag_name=COMPONENT.value+'/v'+version, draft=False, prerelease=True,
            assets=[dict(name=n,size=len(self.resources[base+n]),browser_download_url=base+n)
                    for n in ('collectigen-release.json','collectigen-release.json.sig',name)]))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    result=OrganizerFixture(tmp_path,monkeypatch);result.add();return result


def test_install_preserves_existing_schema_and_unknown_settings(fixture):
    f=fixture;before=f.sentinel.read_bytes();result=f.install()
    assert result.connection_saved and result.installed.phase=='activated'
    selected=f.store.selected(f.store.observe())
    assert selected.version=='0.4.6-alpha.1'
    assert selected.manifest_sha256==next(iter(f.registry.values())).manifest_sha256
    assert selected.binding_revision and not selected.execution_authorized
    assert json.loads(f.settings.read_text())['future_field']=={'keep':True}
    assert f.sentinel.read_bytes()==before
    assert f.service.observe()==result.installed


def test_inventory_and_public_connection_never_grant_launch_or_health(fixture):
    f=fixture;f.install()
    assert not f.service.launch_available
    with pytest.raises(ValueError,match='native'):f.service.launch(confirmed=True)
    with pytest.raises(ValueError,match='native'):f.service.confirm_health()
    with pytest.raises(ValueError,match='explicit'):f.service.launch()


def test_failed_publication_retries_without_restaging(fixture,monkeypatch):
    f=fixture;save=f.store.save
    monkeypatch.setattr(f.store,'save',lambda *a,**k: (_ for _ in ()).throw(OSError('inert failure')))
    result=f.install();assert not result.connection_saved
    calls=list(f.calls);journal=(f.root/'State'/COMPONENT.value/'transaction.json').read_bytes()
    monkeypatch.setattr(f.store,'save',save)
    retried=f.service.retry_connection()
    assert retried.connection_saved and not retried.installed.connection_pending
    assert f.calls==calls
    assert (f.root/'State'/COMPONENT.value/'transaction.json').read_bytes()==journal


def test_first_install_recovery_clears_only_organizer(fixture):
    f=fixture;f.install();before=f.sentinel.read_bytes()
    result=f.service.recover(confirmed=True)
    assert result.connection_saved and f.store.selected(f.store.observe()) is None
    assert json.loads(f.settings.read_text())['future_field']=={'keep':True}
    assert f.sentinel.read_bytes()==before


def test_missing_stage_admission_holds_install(fixture):
    f=fixture;service=OrganizerInstallation(f.acquisition,f.store)
    assert not service.install_available
    with pytest.raises(ValueError,match='admission'):service.observe()


def test_signed_evidence_cannot_be_read_outside_original_scope(fixture):
    f=fixture;selected=f.install().installed.selected
    with pytest.raises(ValueError,match='scope'):f.service.signed_selection(selected)


def test_update_and_recovery_follow_signed_prior_journal_and_rotate_revision(fixture,monkeypatch):
    from core.encyclopedia_connection import OwnedEncyclopedia
    from core.organizer_native_owner import OwnedOrganizer
    from core.manager_launch_owner import RootOrganizerSelection
    from suite_core.manager.bootstrap_entry import NormalEntrySession
    f=fixture;f.install();first=f.store.selected(f.store.observe());before=f.sentinel.read_bytes()
    session=object.__new__(NormalEntrySession);session.state='ready';session.helper_outstanding=None
    startup=RootOrganizerSelection(f.root,f.data,f.settings,first,b'INERT',b'INERT',f.trust)
    session.organizer_selection=startup
    reader=OwnedEncyclopedia(normal_entry_session=session);owner=OwnedOrganizer(session,reader)
    monkeypatch.setattr(RootOrganizerSelection,'validate',lambda self:self.selected)
    owner._health=startup # explicit original-native health boundary double
    f.service=OrganizerInstallation(f.acquisition,f.store,admission=f.admission,native_owner=owner)
    assert f.service.observe().phase=='activated' and f.service.launch_available
    assert f.service.confirm_health().phase=='healthy'
    f.add('0.4.7-alpha.1',b'INERT SECOND CODE');second=f.install()
    assert second.installed.selected.version=='0.4.7-alpha.1'
    assert not f.service.launch_available # old original offer cannot open new code
    with pytest.raises(ValueError,match='Restart'):f.service.launch(confirmed=True)
    recovered=f.service.recover(confirmed=True)
    saved=f.store.selected(f.store.observe())
    assert recovered.connection_saved and saved.directory==first.directory
    assert saved.manifest_sha256==first.manifest_sha256
    assert saved.binding_revision!=first.binding_revision and not f.service.launch_available
    assert f.sentinel.read_bytes()==before
