"""Inert signed stock transport; no network, native catalog or Apply."""
from dataclasses import replace
import socket
import subprocess
import urllib.request

import pytest
from core.tag_default_acquisition import TagDefaultAcquisition, TagDefaultBaseline, TAG_DATABASE_COMPONENT
from core.application_update import UpdateServiceError
from tests.test_component_acquisition import FakeFeed, service, PAYLOAD


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("External runtime/network denied")
    for owner, name in ((socket, "socket"), (subprocess, "Popen"),
                        (subprocess, "run"), (urllib.request, "urlopen")):
        monkeypatch.setattr(owner, name, deny)


def baseline(**changes):
    values = dict(catalog_id="synthetic", catalog_revision="revision-1", pack_id="foundation",
        approved_release_fingerprint="a" * 64, content_version="0.1.1",
        distribution_version="0.1.1", observation_status="known")
    values.update(changes)
    return TagDefaultBaseline(**values)


def prepared(tmp_path):
    feed = FakeFeed()
    feed.add(component="TagDatabase", version="0.1.2", supported_from="0.1.0")
    stock = service(tmp_path, feed)
    return TagDefaultAcquisition(acquisition=stock, component_id=TAG_DATABASE_COMPONENT), feed


def test_tag_discovery_ignores_unrelated_platform_component(tmp_path):
    api, feed = prepared(tmp_path)
    feed.add(component="collectigen.unrelated", version="0.1.2", platform={"os": "linux", "architecture": "x86_64"})
    seen = api.discover(baseline=baseline(), channel="stable")
    assert seen.status == "update_available", seen.reason
    assert len(seen.offers) == 1 and seen.offers[0].release.component_id == TAG_DATABASE_COMPONENT
    assert not any("collectigen.unrelated" in call for call in feed.calls)


def test_signed_stock_download_does_not_apply(tmp_path):
    api, feed = prepared(tmp_path)
    seen = api.discover(baseline=baseline(), channel="stable")
    assert seen.status == "update_available"
    result = api.acquire(seen.offers[0], baseline=baseline(), confirmed=True)
    assert result.acquired.artifact.read_bytes() == PAYLOAD
    assert result.production_install_authorized is False
    assert not (api.acquisition.root / "data").exists()
    with pytest.raises(ValueError):
        api.acquire(seen.offers[0], baseline=baseline(), confirmed=True)


def test_empty_newer_offer_result_does_not_certify_current(tmp_path):
    api, feed = prepared(tmp_path)
    current = baseline(content_version="0.1.2", distribution_version="0.1.2")
    result = api.discover(baseline=current, channel="stable")
    assert result.status == "unable_to_check" and "current-release" in result.reason
    feed.failure = lambda url: True
    assert api.discover(baseline=current, channel="stable").status == "unable_to_check"


@pytest.mark.parametrize("state", ["unknown", "recovery_pending"])
def test_unknown_never_uses_missing_version_as_blank(tmp_path, state):
    api, feed = prepared(tmp_path)
    value = TagDefaultBaseline("synthetic", "1", "foundation", observation_status=state)
    assert api.discover(baseline=value, channel="stable").status == "unable_to_check"
    assert not feed.calls


def test_empty_requires_evidence_and_admission(tmp_path):
    with pytest.raises(ValueError):
        TagDefaultBaseline("synthetic", "1", "foundation", observation_status="known")
    blank = TagDefaultBaseline("synthetic", "1", "foundation", observation_status="verified_empty")
    assert TagDefaultAcquisition().discover(baseline=blank, channel="stable").status == "unable_to_check"
    api, feed = prepared(tmp_path)
    assert api.discover(baseline=blank, channel="stable").offers[0].operation == "install"


def test_original_offer_confirmation_and_baseline(tmp_path):
    api, feed = prepared(tmp_path)
    seen = api.discover(baseline=baseline(), channel="stable")
    for offer, value, confirmed in ((replace(seen.offers[0]), baseline(), True),
            (seen.offers[0], baseline(catalog_revision="2"), True),
            (seen.offers[0], baseline(), False)):
        with pytest.raises(ValueError):
            api.acquire(offer, baseline=value, confirmed=confirmed)


def test_changed_signed_metadata_or_payload_holds(tmp_path):
    api, feed = prepared(tmp_path)
    seen = api.discover(baseline=baseline(), channel="stable")
    feed.resources[seen.offers[0].artifact_source.url] = b"tampered payload"
    with pytest.raises(UpdateServiceError):
        api.acquire(seen.offers[0], baseline=baseline(), confirmed=True)


def test_signature_failure_is_unavailable(tmp_path):
    api, feed = prepared(tmp_path)
    key = next(key for key in feed.resources if key.endswith(".sig"))
    feed.resources[key] = b"{}"
    assert api.discover(baseline=baseline(), channel="stable").status == "unable_to_check"


def test_cancel_and_wrong_component_hold(tmp_path):
    api, feed = prepared(tmp_path)
    with pytest.raises(InterruptedError):
        api.discover(baseline=baseline(), channel="stable", should_cancel=lambda: True)
    with pytest.raises(ValueError):
        TagDefaultAcquisition(component_id="another-component")
