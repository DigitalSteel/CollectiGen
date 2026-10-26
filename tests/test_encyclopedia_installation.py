"""Unsigned inert integration fixtures, injected admission/crypto, real stock IO.

No signature is generated. Verifier injection tests orchestration/transaction
contracts, not authentication or production native path ownership.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import socket
import subprocess
from types import SimpleNamespace
from urllib.parse import quote
import zipfile

import pytest
from core import component_acquisition as acquisition_module
from core.component_acquisition import ComponentAcquisition
from core.encyclopedia_connection import COMPONENT, ReaderConnectionStore, KEY
from core.encyclopedia_installation import EncyclopediaInstallation
from suite_core.discovery import component_staging as staging
from suite_core.discovery.component_release_signature import VerifiedComponentRelease
from suite_core.discovery.offline_component_producer import produce_inventory
from release_contract import ReleaseArtifact


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("External/native/signing action is not part of this fixture")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", deny)


class Response(BytesIO):
    status = 200
    def getcode(self):
        return self.status


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "ordinary-inert-root"; self.root.mkdir()
        self.data = self.root / "Data"; self.data.mkdir()
        self.sentinel = self.data / "synthetic-user.json"
        self.sentinel.write_bytes(b'{"unknown_future":[1,2]}\n')
        self.settings = self.data / "manager.json"
        self.settings.write_text('{"future_field":{"keep":true}}')
        self.store = ReaderConnectionStore(self.settings)
        self.registry, self.resources, self.rows, self.calls = {}, {}, [], []
        self.held = False; self.busy = False; self.checks = 0
        self.trust = b'{"inert_contract_fixture":true}'
        self.cache = tmp_path / "cache"; self.cache.mkdir()
        self.acquisition = ComponentAcquisition(cache_root=self.cache, cache_owner="isolated-test",
            opener=self.opener, host_version="0.2.1-alpha.16", trust_bytes=self.trust,
            trust_sha256=sha256(self.trust).hexdigest(), max_artifact_bytes=staging.MAX_ARCHIVE)
        self.admission = staging.ComponentStageAdmission(self.hold)
        self.service = EncyclopediaInstallation(self.acquisition, self.store, admission=self.admission)
        monkeypatch.setattr(acquisition_module, "verify_component_release", self.verify)
        monkeypatch.setattr(staging, "verify_component_release", self.verify)
        def upgrade(message, signature, trust, **kw):
            row = json.loads(message)
            return SimpleNamespace(version=row["version"], supported_from="0.1.0-alpha.1")
        monkeypatch.setattr(acquisition_module, "verify_release_manifest", upgrade)
        monkeypatch.setattr(staging, "verify_release_manifest", upgrade)

    def check(self):
        assert self.held
        self.checks += 1
        if self.busy:
            raise RuntimeError("original reader or root maintenance busy")

    @contextmanager
    def hold(self, component):
        assert not self.held
        self.held = True
        try:
            self.check()
            yield staging.ComponentStageBinding(self.root,
                staging.component_stage_owner(self.root, self.data, component), self.check, self.data)
        finally:
            self.held = False

    def verify(self, message, signature, trust, **expected):
        assert signature == b"UNSIGNED INERT CONTRACT" and trust == self.trust
        verified = self.registry[message]
        assert verified.component_id == expected["expected_component"]
        assert verified.software_version == expected["expected_version"]
        return verified

    def add(self, version="0.1.0-alpha.8", contents=b"INERT NOT EXECUTABLE"):
        inventory = produce_inventory((("TagEncyclopedia/TagEncyclopedia.exe", contents),),
            component_id=COMPONENT, software_version=version, os_name="windows", architecture="x86_64")
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(staging.INVENTORY, inventory)
            archive.writestr("TagEncyclopedia/TagEncyclopedia.exe", contents)
        raw = output.getvalue(); name = "inert-" + version + ".zip"
        artifact = ReleaseArtifact(name, "component-package", sha256(raw).hexdigest(), len(raw), sha256(inventory).hexdigest())
        message = json.dumps({"version": version, "release_notes": "INERT contract, not publisher evidence"}).encode()
        self.registry[message] = VerifiedComponentRelease(COMPONENT, version, "beta", "windows", "x86_64", artifact,
            "UNSIGNED-FIXTURE", sha256(message).hexdigest())
        tag = COMPONENT.value + "/v" + version
        base = "https://github.com/DigitalSteel/CollectiGen/releases/download/" + quote(tag, safe="") + "/"
        for filename, data in (("collectigen-release.json", message), ("collectigen-release.json.sig", b"UNSIGNED INERT CONTRACT"), (name, raw)):
            self.resources[base + filename] = data
        self.rows.append(dict(tag_name=tag, draft=False, prerelease=True, assets=[dict(name=n,
            size=len(self.resources[base+n]), browser_download_url=base+n) for n in
            ("collectigen-release.json", "collectigen-release.json.sig", name)]))

    def opener(self, request, timeout):
        self.calls.append((request.full_url, timeout, request.get_header("Range")))
        if "api.github.com" in request.full_url:
            return Response(json.dumps(self.rows).encode())
        return Response(self.resources[request.full_url])

    def reviewed(self):
        offer, = self.service.discover(channel="beta")
        return self.service.review(offer)

    def install(self):
        return self.service.install(self.reviewed(), confirmed=True)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    result = Fixture(tmp_path, monkeypatch); result.add()
    return result


def test_first_install_publication_and_restart_stable_origin(fixture):
    f = fixture; before = f.sentinel.read_bytes()
    result = f.install()
    assert result.connection_saved and result.installed.phase == "activated"
    assert f.store.selected(f.store.observe()) == result.installed.selected
    assert json.loads(f.settings.read_text())["future_field"] == {"keep": True}
    origin = f.root / "State" / COMPONENT.value / "component-origin.json"
    original = origin.read_bytes()
    restarted = EncyclopediaInstallation(f.acquisition, f.store, admission=staging.ComponentStageAdmission(f.hold))
    assert restarted.observe() == result.installed
    assert origin.read_bytes() == original and f.sentinel.read_bytes() == before
    assert not (f.root / ".synthetic-component-root.json").exists()


def test_failed_connection_publication_retry_does_not_download_or_stage_again(fixture, monkeypatch):
    f = fixture; original = f.store.save
    monkeypatch.setattr(f.store, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("injected publication failure")))
    result = f.install(); calls = list(f.calls)
    assert not result.connection_saved and "Retry save" in result.reason
    journal = (f.root / "State" / COMPONENT.value / "transaction.json").read_bytes()
    monkeypatch.setattr(f.store, "save", original)
    retried=f.service.retry_connection()
    assert retried.connection_saved and not retried.installed.connection_pending
    assert f.calls == calls
    assert (f.root / "State" / COMPONENT.value / "transaction.json").read_bytes() == journal


def test_update_requires_native_health_and_preserves_previous_code_and_data(fixture):
    f = fixture; first = f.install()
    f.add("0.1.0-alpha.9", b"INERT SECOND CODE")
    with pytest.raises(ValueError, match="Finish launch"):
        f.reviewed()
    with pytest.raises(ValueError, match="native"):
        f.service.confirm_health()
    f.service.native_health = lambda selected: selected == first.installed.selected
    f.service.confirm_health()
    second = f.install()
    assert second.installed.selected.version == "0.1.0-alpha.9"
    assert second.installed.recovery_available
    assert second.installed.recovery_version == first.installed.selected.version
    assert first.installed.selected.program.read_bytes() == b"INERT NOT EXECUTABLE"
    restored = f.service.recover(confirmed=True)
    assert restored.connection_saved and restored.installed.selected == first.installed.selected
    assert f.store.selected(f.store.observe()) == first.installed.selected
    assert f.sentinel.read_bytes() == b'{"unknown_future":[1,2]}\n'


def test_first_install_recovery_clears_only_reader_setting(fixture):
    f = fixture; f.install()
    result = f.service.recover(confirmed=True)
    assert result.installed.selected is None and result.connection_saved
    assert KEY not in json.loads(f.settings.read_text())
    assert json.loads(f.settings.read_text())["future_field"] == {"keep": True}


def test_recovery_preview_is_bound_to_unchanged_authenticated_installation(fixture):
    f = fixture
    first = f.install()
    reviewed = f.service.observe()
    assert reviewed.recovery_available and reviewed.recovery_version is None
    f.service.native_health = lambda selected: selected == first.installed.selected
    f.service.confirm_health()
    before = f.settings.read_bytes()
    with pytest.raises(ValueError, match='Installation changed'):
        f.service.recover(confirmed=True, expected_installed=reviewed)
    assert f.settings.read_bytes() == before
    assert f.service.observe().selected == first.installed.selected
    current = f.service.observe()
    result = f.service.recover(confirmed=True, expected_installed=current)
    assert result.installed.selected is None and not result.installed.recovery_available


def test_recovery_preview_uses_pending_update_journal_not_previous_install(fixture):
    f = fixture
    first = f.install()
    f.service.native_health = lambda selected: selected == first.installed.selected
    f.service.confirm_health()
    f.add('0.1.0-alpha.9', b'INERT SECOND CODE')
    reviewed = f.reviewed()
    acquired = f.acquisition.acquire(reviewed.offer)
    offer = acquired.offer
    def stop(phase):
        if phase == 'staging_created':
            raise RuntimeError('synthetic interruption')
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        staging.stage_and_activate_admitted(f.admission, acquired.artifact, component=COMPONENT,
            version=offer.release.software_version, os_name='windows', architecture='x86_64',
            manifest_bytes=offer.manifest_bytes, signature_bytes=offer.signature_bytes,
            trust_bytes=f.trust, expected_active_token=reviewed.installed.active_sha256, failpoint=stop)
    preview = f.service.observe()
    assert preview.pending and preview.recovery_available
    assert preview.recovery_version == first.installed.selected.version
    assert preview.recovery_journal_sha256 != preview.journal_sha256
    result = f.service.recover(confirmed=True, expected_installed=preview)
    assert result.installed.selected == first.installed.selected and not result.installed.pending


def test_cancel_transfer_cleanup_then_fresh_explicit_retry(fixture):
    f = fixture
    f.rows.clear(); f.registry.clear(); f.add(contents=b"INERT" * (512 * 1024))
    reviewed = f.reviewed(); cancelled = [False]
    with pytest.raises(InterruptedError):
        f.service.install(reviewed, confirmed=True, should_cancel=lambda: cancelled[0],
            progress=lambda *args: cancelled.__setitem__(0, True))
    assert not list(f.cache.glob("attempt-*"))
    assert not (f.root / "Apps").exists() and KEY not in json.loads(f.settings.read_text())
    assert f.install().connection_saved
    assert all(row[2] is None for row in f.calls)


def test_stale_settings_and_busy_native_owner_refuse_before_transfer(fixture):
    f = fixture; reviewed = f.reviewed(); before = len(f.calls)
    f.settings.write_text('{"independent_change":true}')
    with pytest.raises(ValueError, match="Settings changed"):
        f.service.install(reviewed, confirmed=True)
    assert len(f.calls) == before and not (f.root / "Apps").exists()
    f.busy = True
    with pytest.raises(RuntimeError, match="busy"):
        f.service.observe()


def test_changed_origin_and_unenrolled_journal_never_adopted(fixture):
    f = fixture; f.install()
    base = f.root / "State" / COMPONENT.value
    origin = base / "component-origin.json"; before = origin.read_bytes()
    origin.write_bytes(before.replace(b"COMPONENT_TRANSACTION_ORIGIN", b"ALTERED_TRANSACTION_ORIGIN"))
    with pytest.raises(ValueError, match="origin_changed"):
        f.service.observe()
    origin.unlink()
    with pytest.raises(ValueError, match="unenrolled"):
        f.service.observe()


def test_missing_admission_and_old_fixture_entry_cannot_install_ordinary_root(fixture):
    f = fixture
    held = EncyclopediaInstallation(f.acquisition, f.store)
    assert not held.install_available
    with pytest.raises(ValueError, match="qualified"):
        held.observe()
    with pytest.raises(FileNotFoundError):
        staging.owned(f.root, COMPONENT)


@pytest.mark.parametrize("phase", ["staging_created", "member_extracted", "version_published", "active_reference_replaced"])
def test_same_engine_interruptions_recover_with_stable_origin(fixture, phase):
    f = fixture; reviewed = f.reviewed()
    acquired = f.acquisition.acquire(reviewed.offer); offer = acquired.offer
    def stop(observed):
        if observed == phase:
            raise RuntimeError("injected crash boundary")
    with pytest.raises(RuntimeError):
        staging.stage_and_activate_admitted(f.admission, acquired.artifact, component=COMPONENT,
            version=offer.release.software_version, os_name="windows", architecture="x86_64",
            manifest_bytes=offer.manifest_bytes, signature_bytes=offer.signature_bytes,
            trust_bytes=f.trust, expected_active_token=reviewed.installed.active_sha256, failpoint=stop)
    result = f.service.recover(confirmed=True)
    assert result.installed.selected is None
    assert f.sentinel.read_bytes() == b'{"unknown_future":[1,2]}\n'


def test_no_fixture_signature_bypass_in_canonical_verifier(fixture, monkeypatch):
    import suite_core.discovery.component_release_signature as signatures
    f = fixture; reviewed = f.reviewed(); acquired = f.acquisition.acquire(reviewed.offer)
    monkeypatch.setattr(staging, "verify_component_release", signatures.verify_component_release)
    with pytest.raises(ValueError):
        staging.stage_and_activate_admitted(f.admission, acquired.artifact, component=COMPONENT,
            version=reviewed.offer.release.software_version, os_name="windows", architecture="x86_64",
            manifest_bytes=reviewed.offer.manifest_bytes, signature_bytes=reviewed.offer.signature_bytes,
            trust_bytes=f.trust, expected_active_token=reviewed.installed.active_sha256)
    assert not (f.root / "Apps").exists()


def test_failed_recovery_clear_retries_publication_after_restart_without_rollback_again(fixture, monkeypatch):
    f = fixture; f.install()
    original = f.store.clear
    monkeypatch.setattr(f.store, "clear", lambda **kw: (_ for _ in ()).throw(OSError("injected clear failure")))
    result = f.service.recover(confirmed=True)
    assert not result.connection_saved and result.installed.selected is None
    journal = (f.root / "State" / COMPONENT.value / "transaction.json").read_bytes()
    calls = list(f.calls)
    monkeypatch.setattr(f.store, "clear", original)
    restarted = EncyclopediaInstallation(f.acquisition, f.store, admission=f.admission)
    assert restarted.retry_connection().connection_saved
    assert f.store.selected(f.store.observe()) is None and f.calls == calls
    assert (f.root / "State" / COMPONENT.value / "transaction.json").read_bytes() == journal


@pytest.mark.parametrize("phase,settled", [("member_extracted", False), ("version_published", True)])
def test_cancel_during_staging_retains_recovery_and_after_publish_settles(fixture, monkeypatch, phase, settled):
    import core.encyclopedia_installation as service_module
    f = fixture; reviewed = f.reviewed(); cancelled = [False]
    original = service_module.stage_and_activate_admitted
    def activate(*args, **kwargs):
        callback = kwargs["failpoint"]
        def handoff(observed):
            if observed == phase:
                cancelled[0] = True
            callback(observed)
        kwargs["failpoint"] = handoff
        return original(*args, **kwargs)
    monkeypatch.setattr(service_module, "stage_and_activate_admitted", activate)
    if settled:
        assert f.service.install(reviewed, confirmed=True, should_cancel=lambda: cancelled[0]).connection_saved
    else:
        with pytest.raises(InterruptedError):
            f.service.install(reviewed, confirmed=True, should_cancel=lambda: cancelled[0])
        assert f.service.observe().pending
        assert f.service.recover(confirmed=True).installed.selected is None
    assert f.sentinel.read_bytes() == b'{"unknown_future":[1,2]}\n'


def test_existing_synthetic_entry_retains_marker_and_same_engine_semantics(fixture, tmp_path):
    f = fixture; reviewed = f.reviewed(); acquired = f.acquisition.acquire(reviewed.offer)
    root = staging.create_disposable_root(tmp_path / "legacy-disposable")
    offer = acquired.offer
    target = staging.stage_and_activate(root, acquired.artifact, component=COMPONENT,
        version=offer.release.software_version, os_name="windows", architecture="x86_64",
        manifest_bytes=offer.manifest_bytes, signature_bytes=offer.signature_bytes, trust_bytes=f.trust,
        expected_active_token=staging.active_token(None), operator_approved=True, idle=True)
    assert staging.finish_or_recover(root, component=COMPONENT, trust_bytes=f.trust, health_verified=True) == target
    assert staging.finish_or_recover(root, component=COMPONENT, trust_bytes=f.trust, rollback_approved=True) is None
    assert (root / ".synthetic-component-root.json").exists()


def test_data_root_identity_drift_refuses_recovery_even_with_same_code_root(fixture):
    f = fixture; f.install()
    f.data = f.root / "other-synthetic-data"; f.data.mkdir()
    with pytest.raises(ValueError, match="origin_changed"):
        f.service.observe()


def _stock_discovery_caller(tmp_path, monkeypatch, feed):
    from tests.test_component_acquisition import service
    acquisition = service(tmp_path, feed, max_artifact_bytes=staging.MAX_ARCHIVE)
    caller = EncyclopediaInstallation(acquisition, ReaderConnectionStore(tmp_path / 'synthetic-settings.json'))
    # Only installed observation is doubled: real stock discovery, signature and platform checks execute.
    monkeypatch.setattr(caller, 'observe', lambda: SimpleNamespace(selected=None, pending=False))
    return caller


def test_reader_discovery_scopes_before_unrelated_linux_platform_admission(tmp_path, monkeypatch):
    from tests.test_component_acquisition import FakeFeed
    feed = FakeFeed(); feed.add()
    feed.add(component='collectigen.unrelated', platform=dict(os='linux', architecture='x86_64'))
    caller = _stock_discovery_caller(tmp_path, monkeypatch, feed)
    offer, = caller.discover(channel='beta')
    assert offer.release.component_id == COMPONENT and offer.release.os == 'windows'
    assert not any('collectigen.unrelated' in url for url, *_rest in feed.calls)
    assert not caller.store.settings.path.exists()


@pytest.mark.parametrize('fault', ['platform', 'signature'])
def test_reader_scoped_discovery_still_rejects_invalid_target(tmp_path, monkeypatch, fault):
    from tests.test_component_acquisition import FakeFeed
    feed = FakeFeed()
    base, _name = feed.add(**({'platform':dict(os='linux', architecture='x86_64')} if fault == 'platform' else {}))
    feed.add(component='collectigen.unrelated')
    if fault == 'signature': feed.resources[base + 'collectigen-release.json.sig'] = b'{}'
    caller = _stock_discovery_caller(tmp_path, monkeypatch, feed)
    with pytest.raises(ValueError): caller.discover(channel='beta')
    assert not any('collectigen.unrelated' in url for url, *_rest in feed.calls)
    assert not caller.store.settings.path.exists()


@pytest.mark.parametrize("interruption", [None, "before_version_publish", "new_journal_established", "version_published", "active_reference_replaced"])
def test_repeat_update_after_rollback_reuses_exact_code_and_remains_recoverable(fixture, monkeypatch, interruption):
    f = fixture
    first = f.install()
    f.service.native_health = lambda selected: True  # injected native boundary, not process proof
    f.service.confirm_health()
    f.add("0.1.0-alpha.9", b"INERT SECOND CODE")
    second = f.install()
    f.service.recover(confirmed=True)
    directory = second.installed.selected.directory
    retained = {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.rglob("*") if p.is_file()}
    settings_before = f.settings.read_bytes()
    original = staging._stage_and_activate
    if interruption is not None:
        def interrupted(*args, **kwargs):
            failpoint = kwargs["failpoint"]
            def stop(phase):
                failpoint(phase)
                if phase == interruption:
                    raise RuntimeError("synthetic retained-code interruption")
            return original(*args, **dict(kwargs, failpoint=stop))
        monkeypatch.setattr(staging, "_stage_and_activate", interrupted)
        with pytest.raises(RuntimeError, match="retained-code interruption"):
            f.install()
        monkeypatch.setattr(staging, "_stage_and_activate", original)
        assert f.settings.read_bytes() == settings_before
    else:
        repeated = f.install()
        assert repeated.installed.selected == second.installed.selected
        f.service.confirm_health()
    result = f.service.recover(confirmed=True)
    assert result.connection_saved and result.installed.selected == first.installed.selected
    assert {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.rglob("*") if p.is_file()} == retained
    assert f.sentinel.read_bytes() == b'{"unknown_future":[1,2]}\n'


@pytest.mark.parametrize("fault", ["changed", "extra"])
def test_repeat_update_refuses_changed_retained_package_before_metadata_writes(fixture, fault):
    f = fixture
    f.install(); f.service.native_health = lambda selected: True; f.service.confirm_health()
    f.add("0.1.0-alpha.9", b"INERT SECOND CODE")
    second = f.install(); f.service.recover(confirmed=True)
    directory = second.installed.selected.directory
    path = second.installed.selected.program if fault == "changed" else directory / "unexpected.txt"
    path.write_bytes(b"CHANGED INERT CODE")
    base = f.root / "State" / COMPONENT.value
    before = {p.name: p.read_bytes() for p in base.iterdir() if p.is_file()}
    settings = f.settings.read_bytes()
    with pytest.raises(staging.StagingHold, match="payload_"):
        f.install()
    assert {p.name: p.read_bytes() for p in base.iterdir() if p.is_file()} == before
    assert f.settings.read_bytes() == settings and path.read_bytes() == b"CHANGED INERT CODE"


def test_first_install_undo_allows_explicit_reinstall_without_overwriting_retained_code(fixture):
    f = fixture
    first = f.install()
    directory = first.installed.selected.directory
    retained = {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.rglob("*") if p.is_file()}
    undone = f.service.recover(confirmed=True)
    assert undone.installed.selected is None
    repeated = f.install()
    assert repeated.connection_saved and repeated.installed.selected == first.installed.selected
    assert {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.rglob("*") if p.is_file()} == retained
    assert f.sentinel.read_bytes() == b'{"unknown_future":[1,2]}\n'


def stage_identity_fixture(f,monkeypatch,stage="alpha",version="0.1.0"):
    from dataclasses import replace
    f.add(version)
    row=f.rows[-1];assets={a['name']:a for a in row['assets']}
    original=f.resources[assets['collectigen-release.json']['browser_download_url']]
    value=dict(version=version,supported_from="0.0.0",maturity=stage)
    message=json.dumps(value).encode();release=replace(f.registry[original],schema_version=2,maturity=stage,
        channel="stable" if stage=="stable" else "beta",manifest_sha256=sha256(message).hexdigest())
    f.registry[message]=release
    archive=next(a for a in row['assets'] if a['name'].endswith('.zip'))
    path=f.cache/("identity-"+stage+".zip");path.write_bytes(f.resources[archive['browser_download_url']])
    f.service=EncyclopediaInstallation(f.acquisition,f.store,admission=f.admission,schema2_identity=True)
    # Existing injected verifier/typed original scope: this is not signing/native evidence.
    with f.admission.transaction(COMPONENT) as (root,base,owner,check):
        active=staging.read_json(base/'active.json') if (base/'active.json').exists() else None
    target=staging.stage_and_activate_admitted(f.admission,path,component=COMPONENT,version=version,
        os_name="windows",architecture="x86_64",manifest_bytes=message,signature_bytes=b"UNSIGNED INERT CONTRACT",
        trust_bytes=f.trust,expected_active_token=staging.active_token(active),release_schema=2,maturity=stage)
    return target


def test_identity_host_observe_save_reopen_health_and_prior_recovery(fixture,monkeypatch):
    f=fixture;data=f.sentinel.read_bytes();target=stage_identity_fixture(f,monkeypatch)
    before=f.store.observe();result=f.service._publish_connection(expected=before)
    assert result.connection_saved and result.installed.selected.maturity=="alpha"
    assert f.store.selected(f.store.observe()).release_sha256==target['release_sha256']
    f.service.native_health=lambda selected: True  # fixture-only outcome
    f.service.confirm_health()
    prior=f.store.selected(f.store.observe())
    stage_identity_fixture(f,monkeypatch,"beta")
    result=f.service._publish_connection(expected=f.store.observe())
    assert result.installed.selected.version==prior.version and result.installed.selected!=prior
    restored=f.service.recover(confirmed=True)
    assert restored.connection_saved and f.store.selected(f.store.observe())==prior
    assert f.service.observe().selected==prior and f.sentinel.read_bytes()==data


def test_identity_host_publication_retry_saves_only_and_refuses_forged_stage(fixture,monkeypatch):
    from dataclasses import replace
    f=fixture;stage_identity_fixture(f,monkeypatch);save=f.store.save
    monkeypatch.setattr(f.store,'save',lambda *a,**kw:(_ for _ in ()).throw(OSError('INJECTED SAVE')))
    held=f.service._publish_connection(expected=f.store.observe());assert not held.connection_saved
    monkeypatch.setattr(f.store,'save',save);calls=list(f.calls)
    result=f.service.retry_connection();assert result.connection_saved and f.calls==calls
    snapshot=f.store.observe();selected=f.store.selected(snapshot)
    f.store.save(replace(selected,maturity='stable'),expected=snapshot)
    # Signed installation observation does not adopt the saved claim.
    assert f.service.observe().selected.maturity=='alpha'
    held=f.service.reconcile_launch();assert not held.connection_saved and "identity differs" in held.reason
    from core.encyclopedia_update_binding import _installed
    with pytest.raises(ValueError,match="original_saved_component_selection"):
        _installed(f.root,f.data,f.settings,f.trust,schema2_identity=True)


def test_identity_host_default_mode_refuses_new_reference(fixture,monkeypatch):
    f=fixture;stage_identity_fixture(f,monkeypatch)
    old=EncyclopediaInstallation(f.acquisition,f.store,admission=f.admission)
    with pytest.raises(ValueError):old.observe()


def test_full_signed_snapshot_reopens_exact_stage_and_refuses_changed_digest(fixture,monkeypatch):
    from core.encyclopedia_update_binding import _installed
    from dataclasses import replace
    f=fixture;target=stage_identity_fixture(f,monkeypatch)
    f.service._publish_connection(expected=f.store.observe());f.service.native_health=lambda _:True;f.service.confirm_health()
    current=_installed(f.root,f.data,f.settings,f.trust,schema2_identity=True)
    assert current.selected.release_schema==2 and current.selected.maturity=='alpha'
    assert current.release_sha256==current.selected.release_sha256==target['release_sha256']
    assert _installed(f.root,f.data,f.settings,f.trust,schema2_identity=True)==current
    f.store.save(replace(current.selected,release_sha256='d'*64),expected=f.store.observe())
    with pytest.raises(ValueError,match='original_saved_component_selection'):
        _installed(f.root,f.data,f.settings,f.trust,schema2_identity=True)


@pytest.mark.parametrize('recovering',[False,True])
def test_full_identity_publication_request_and_retry_bind_exact_target_component_only(fixture,monkeypatch,recovering):
    from types import SimpleNamespace
    from core.encyclopedia_update_binding import _installed
    from suite_core.encyclopedia.bootstrap_entry import StandaloneEncyclopediaBootstrap
    f=fixture;stage_identity_fixture(f,monkeypatch)
    f.service._publish_connection(expected=f.store.observe());f.service.native_health=lambda _:True;f.service.confirm_health()
    prior=_installed(f.root,f.data,f.settings,f.trust,schema2_identity=True)
    target=stage_identity_fixture(f,monkeypatch,'beta')
    f.service._publish_connection(expected=f.store.observe())
    journal=json.loads((f.root/'State'/COMPONENT.value/'transaction.json').read_bytes())
    message=__import__('base64').b64decode(journal['manifest']);release=f.registry[message]
    offer=SimpleNamespace(release=release)
    request=SimpleNamespace(current=prior,offer=offer,identity={'fixture':'original-request'})
    if recovering:f.service.recover(confirmed=True)
    # This exercises publication identity/state only. The owner hook is a fixture;
    # existing original-owner regressions separately cover actual custody logic.
    owner=SimpleNamespace(record={'standalone_update':{'identity':request.identity,'phase':'recovering' if recovering else 'applying'}},
        _standalone_encyclopedia_owned=lambda subject:None,_standalone_encyclopedia_update=request)
    boot=object.__new__(StandaloneEncyclopediaBootstrap);boot.owner=owner;boot.subject=object();boot.request=request
    boot.binding=SimpleNamespace(root=f.root,trust_bytes=f.trust,schema2_identity=True);boot._installation=f.service
    with f.admission.transaction(COMPONENT) as (root,base,original_owner,check):
        boot._verify_publication_request(request,root,base,original_owner)
    snapshot=f.store.observe();selected=f.store.selected(snapshot)
    from dataclasses import replace
    f.store.save(replace(selected,maturity='stable'),expected=snapshot)
    with f.admission.transaction(COMPONENT) as (root,base,original_owner,check):
        with pytest.raises(ValueError,match='publication_readback'):
            boot._verify_publication_request(request,root,base,original_owner)
