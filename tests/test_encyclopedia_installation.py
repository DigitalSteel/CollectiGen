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
