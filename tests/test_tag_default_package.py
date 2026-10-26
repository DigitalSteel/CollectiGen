"""Signed inert data archives and disposable native Foundation fixtures only."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import socket
import stat
import subprocess
import urllib.request
import zipfile

import pytest
from core.tag_default_acquisition import TagDefaultAcquisition, TagDefaultBaseline, TAG_DATABASE_COMPONENT
from core.tag_default_package import prepare_tag_default_package
from suite_core.discovery import component_staging as staging
from suite_core.installation import (InstalledComponent, InstallationId, ComponentKind,
    CapabilityId, InterfaceBinding, LocalInventory)
from suite_core.encyclopedia.publication import encode
from tests.test_component_acquisition import FakeFeed, service
from tests.test_tag_database_pack_producer import companion_input


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("External execution, network or activation denied")
    for owner, name in ((socket, "socket"), (subprocess, "Popen"), (subprocess, "run"),
                        (urllib.request, "urlopen"), (staging, "stage_and_activate_admitted")):
        monkeypatch.setattr(owner, name, deny)


def acquired_fixture(tmp_path, *, mutate=None, platform=None, wrong_source=False):
    from core import knowledge_pack as native
    source, pack, publication, root = companion_input.__wrapped__(tmp_path)
    foundation = native.load_foundation_pack(pack)
    if wrong_source:
        publication = replace(publication, snapshot=replace(publication.snapshot,
            source=replace(publication.snapshot.source, revision="different")))
    payloads = {"TagDatabase.zip": pack.read_bytes(),
        "metadata/TagDatabase.source-snapshot.json": encode(publication), "BUILD_RECEIPT.json": b"{}"}
    inventory = dict(schema_version=1, component_id="TagDatabase", software_version="0.1.2",
        platform=platform or dict(os="windows", architecture="x86_64"), runtime_kind="frozen-portable",
        files=[dict(path=name, size_bytes=len(data), sha256=sha256(data).hexdigest())
            for name, data in payloads.items()])
    inventory_bytes = json.dumps(inventory).encode()
    members = {"TagDatabase/data/BOOTSTRAP_INVENTORY.json": inventory_bytes,
        **{"TagDatabase/data/" + name: data for name, data in payloads.items()},
        "TagDatabase/README.md": b"Synthetic data-only delivery"}
    if mutate:
        mutate(members)
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    payload = stream.getvalue()
    pin = sha256(inventory_bytes).hexdigest()
    feed = FakeFeed()
    feed.add(component="TagDatabase", version="0.1.2", payload=payload, supported_from="0.1.0",
        artifacts=[dict(name="TagDatabase-0.1.2.zip", kind="component-package",
            size_bytes=len(payload), sha256=sha256(payload).hexdigest(), portable_manifest_sha256=pin)])
    stock = service(tmp_path, feed)
    api = TagDefaultAcquisition(acquisition=stock, component_id=TAG_DATABASE_COMPONENT)
    baseline = TagDefaultBaseline("synthetic", "1", foundation.approved_release.pack_id,
        observation_status="verified_empty")
    offer, = api.discover(baseline=baseline, channel="stable").offers
    acquired = api.acquire(offer, baseline=baseline, confirmed=True)
    assert acquired.acquired.artifact.parent.parent == stock.root
    component = InstalledComponent(InstallationId("synthetic-data-cache"), TAG_DATABASE_COMPONENT,
        "0.1.2", ComponentKind.APP, frozenset({CapabilityId("synthetic.tag-defaults")}),
        (InterfaceBinding("snapshot_interface_version", 1),),
        LocalInventory("Apps/TagDatabase/data", "BOOTSTRAP_INVENTORY.json", pin))
    return acquired, stock.trust, component, publication, root


def test_prepare_preserves_native_recipient_and_no_activation(tmp_path):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = prepare_tag_default_package(acquired, tmp_path / "prepared",
        trust_bytes=trust, component=component, acquisition_cache_root=acquired.acquired.artifact.parent.parent)
    assert result.source_directory == result.root / "Apps/TagDatabase/data"
    assert result.source_snapshot == publication.snapshot
    assert result.release.fingerprint == result.foundation.approved_release.release_fingerprint
    assert result.component is component and not result.production_install_authorized
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (result.root / "State").exists()


@pytest.mark.parametrize("missing", ["trust", "component", "signature"])
def test_missing_or_inauthentic_admission_before_extraction(tmp_path, missing):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    if missing == "trust":
        trust = None
    elif missing == "component":
        component = None
    else:
        acquired = replace(acquired, acquired=replace(acquired.acquired,
            offer=replace(acquired.acquired.offer, signature_bytes=b"{}")))
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError):
        prepare_tag_default_package(acquired, destination, trust_bytes=trust, component=component, acquisition_cache_root=acquired.acquired.artifact.parent.parent)
    assert not destination.exists()


@pytest.mark.parametrize("case", ["missing_companion", "extra_file", "extra_root", "changed_payload", "platform", "source"])
def test_inventory_and_independent_foundation_closure(tmp_path, case):
    def mutate(members):
        if case == "missing_companion":
            del members["TagDatabase/data/metadata/TagDatabase.source-snapshot.json"]
        elif case == "extra_file":
            members["TagDatabase/data/untracked.json"] = b"{}"
        elif case == "extra_root":
            members["AnotherComponent/file.txt"] = b"not TagDatabase"
        elif case == "changed_payload":
            members["TagDatabase/data/TagDatabase.zip"] = b"not the inventoried pack"
    acquired, trust, component, publication, root = acquired_fixture(tmp_path, mutate=mutate,
        platform=dict(os="linux", architecture="x86_64") if case == "platform" else None,
        wrong_source=case == "source")
    with pytest.raises((ValueError, OSError)):
        prepare_tag_default_package(acquired, tmp_path / "prepared", trust_bytes=trust, component=component, acquisition_cache_root=acquired.acquired.artifact.parent.parent)


def test_cancel_after_member_retains_owned_partial_output(tmp_path):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    destination = tmp_path / "prepared"
    def cancel():
        return (destination / "Apps/TagDatabase/data/BOOTSTRAP_INVENTORY.json").exists()
    with pytest.raises(InterruptedError):
        prepare_tag_default_package(acquired, destination, cancel, trust_bytes=trust, component=component, acquisition_cache_root=acquired.acquired.artifact.parent.parent)
    assert destination.exists()
    with pytest.raises(ValueError, match="new_disposable"):
        prepare_tag_default_package(acquired, destination, trust_bytes=trust, component=component, acquisition_cache_root=acquired.acquired.artifact.parent.parent)


def test_after_decode_inventory_recheck(tmp_path, monkeypatch):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    import core.tag_default_package as module
    original = module.read
    def changed(path):
        result = original(path)
        path.write_bytes(path.read_bytes() + b" ")
        return result
    monkeypatch.setattr(module, "read", changed)
    with pytest.raises(ValueError):
        module.prepare_tag_default_package(acquired, tmp_path / "prepared",
            trust_bytes=trust, component=component, acquisition_cache_root=acquired.acquired.artifact.parent.parent)


def test_sibling_preparation_in_acquisition_cache_refused_before_writes(tmp_path):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    cache_root = acquired.acquired.artifact.parent.parent
    destination = cache_root / "prepared-sibling"
    with pytest.raises(ValueError, match="disjoint_package_cache"):
        prepare_tag_default_package(acquired, destination, trust_bytes=trust,
            component=component, acquisition_cache_root=cache_root)
    assert not destination.exists()


def test_attempt_directory_cannot_masquerade_as_cache_root(tmp_path):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="cache_marker"):
        prepare_tag_default_package(acquired, destination, trust_bytes=trust,
            component=component, acquisition_cache_root=acquired.acquired.artifact.parent)
    assert not destination.exists()


def test_cancel_during_final_hashing_holds_success_and_retains_output(tmp_path, monkeypatch):
    acquired, trust, component, publication, root = acquired_fixture(tmp_path)
    import core.tag_default_package as module
    original = module.verify_artifact
    cancelled = [False]
    calls = []
    def verify(path, artifact):
        original(path, artifact)
        calls.append(1)
        if len(calls) == 3:  # Pre-extract, before decode, then final verification.
            cancelled[0] = True
    monkeypatch.setattr(module, "verify_artifact", verify)
    destination = tmp_path / "prepared"
    with pytest.raises(InterruptedError):
        module.prepare_tag_default_package(acquired, destination, lambda: cancelled[0],
            trust_bytes=trust, component=component,
            acquisition_cache_root=acquired.acquired.artifact.parent.parent)
    assert len(calls) == 3
    assert (destination / "Apps/TagDatabase/data/TagDatabase.zip").exists()


@pytest.mark.parametrize("case", ["traversal", "symlink", "duplicate", "nonempty"])
def test_stock_extractor_refuses_unsafe_or_nonempty(tmp_path, case):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        if case == "traversal":
            archive.writestr("../outside.txt", b"no")
        elif case == "symlink":
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"elsewhere")
        else:
            archive.writestr("file.txt", b"one")
            if case == "duplicate":
                with pytest.warns(UserWarning):
                    archive.writestr("file.txt", b"two")
    destination = tmp_path / "extraction"
    destination.mkdir()
    if case == "nonempty":
        (destination / "keep.txt").write_bytes(b"keep")
    with pytest.raises(ValueError):
        staging.extract_component_archive(archive_path, destination)
    assert not (tmp_path / "outside.txt").exists()
