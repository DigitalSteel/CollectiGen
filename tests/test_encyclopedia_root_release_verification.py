"""Synthetic local root integrity; inert PE bytes and existing disposable RSA fixture."""
from hashlib import sha256
import json
from pathlib import Path
import stat
import struct
import zipfile

import pytest
from tools import verify_encyclopedia_root_release as verifier
from suite_core.discovery.offline_component_producer import produce_inventory
from suite_core.tests.test_component_release_signature import raw, trust, signature

VERSION = "0.2.0-alpha.1"
NAME = "TagEncyclopedia-a.0.2.0-windows-x64.zip"


def inert_pe():
    value = bytearray(512); value[:2] = b"MZ"
    struct.pack_into("<I", value, 60, 64)
    value[64:68] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", value, 68, 0x8664, 1, 0, 0, 0, 240, 0x22)
    value[88:90] = b"\x0b\x02"
    return bytes(value)


def packet(tmp_path, *, inventory_change=None, zip_change=None, manifest_change=None, members_change=None):
    members = [("TagEncyclopedia.exe", inert_pe()), ("_internal/python314.dll", b"INERT DLL FIXTURE"),
        ("README.md", b"Synthetic a.0.2.0 root; never execute"), ("LICENSE", b"Synthetic legal")]
    if members_change: members_change(members)
    inventory = produce_inventory(tuple(members), component_id=verifier.COMPONENT,
        software_version=VERSION, os_name="windows", architecture="x86_64")
    if inventory_change:
        value = json.loads(inventory); inventory_change(value); inventory = raw(value)
    archive = tmp_path / NAME
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in members: z.writestr(name, content)
        z.writestr(verifier.INVENTORY, inventory)
        if zip_change: zip_change(z)
    manifest = dict(schema_version=1, product="collectigen", version=VERSION, channel="beta",
        published_at="2026-10-13T00:00:00Z", supported_from=VERSION,
        release_notes="Synthetic root verification fixture only.", artifacts=[dict(name=NAME,
            kind="portable-windows-x64", size_bytes=archive.stat().st_size,
            sha256=sha256(archive.read_bytes()).hexdigest(), portable_manifest_sha256=sha256(inventory).hexdigest())])
    if manifest_change: manifest_change(manifest)
    manifest_raw = raw(manifest)
    # Existing declared disposable unit-test key only; no release signing tool/keys.
    m = tmp_path / "TagEncyclopedia-root-release.json"; m.write_bytes(manifest_raw)
    s = tmp_path / "TagEncyclopedia-root-release.json.sig"; s.write_bytes(raw(signature(manifest_raw)))
    t = tmp_path / "synthetic-public-trust.json"; t.write_bytes(raw(trust()))
    c = tmp_path / (NAME + ".sha256")
    c.write_bytes((sha256(archive.read_bytes()).hexdigest() + "  " + NAME + "\n").encode())
    return dict(archive_path=archive, manifest_path=m, signature_path=s, sidecar_path=c,
        trust_path=t, destination=tmp_path / "verified", expected_version=VERSION,
        expected_channel="beta", expected_basename=NAME, expected_trust_sha256=sha256(t.read_bytes()).hexdigest())


def test_stock_inventory_and_root_integrity_readback(tmp_path):
    args = packet(tmp_path)
    result = verifier.verify_root_release(**args)
    assert result.version == VERSION and result.channel == "beta" and result.basename == NAME
    assert result.key_id == "disposable-test-only" and not result.execution_qualified
    extracted = Path(result.extracted_directory)
    assert (extracted / "TagEncyclopedia.exe").read_bytes() == inert_pe()
    assert result.member_count == 5 and not (extracted / "portable-package.json").exists()
    assert result.inventory_sha256 == sha256((extracted / verifier.INVENTORY).read_bytes()).hexdigest()


@pytest.mark.parametrize("change", [
    lambda m: m.update(component_id="collectigen.tag-encyclopedia"),
    lambda m: m.update(version="0.2.0-alpha.2"),
    lambda m: m.update(channel="stable"),
    lambda m: m["artifacts"][0].update(kind="component-package"),
    lambda m: m["artifacts"][0].update(name="TagEncyclopediaReader-windows-x64.zip"),
    lambda m: m["artifacts"].append(dict(m["artifacts"][0], name="extra.zip", kind="extra")),
    lambda m: m["artifacts"][0].update(portable_manifest_sha256="0" * 64),
])
def test_reader_metadata_or_wrong_root_release_holds_before_output(tmp_path, change):
    args = packet(tmp_path, manifest_change=change)
    with pytest.raises(ValueError): verifier.verify_root_release(**args)
    assert not args["destination"].exists()


@pytest.mark.parametrize("change", [
    lambda i: i.update(component_id="collectigen.suite-manager"),
    lambda i: i.update(software_version="0.2.0-alpha.2"),
    lambda i: i.update(schema_version=2),
    lambda i: i.update(platform={"os": "linux", "architecture": "x86_64"}),
    lambda i: i.update(platform={"os": "windows", "architecture": "aarch64"}),
    lambda i: i.update(runtime_kind="interpreter-portable"),
    lambda i: i["files"][0].update(sha256="0" * 64),
    lambda i: i["files"][0].update(size_bytes=True),
    lambda i: i["files"].append(dict(i["files"][0], path=i["files"][0]["path"].swapcase())),
])
def test_inventory_mismatch_holds_before_output(tmp_path, change):
    args = packet(tmp_path, inventory_change=change)
    with pytest.raises(ValueError): verifier.verify_root_release(**args)
    assert not args["destination"].exists()


def special(z):
    info = zipfile.ZipInfo("special-node"); info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16; z.writestr(info, b"synthetic-target")


@pytest.mark.parametrize("change", [
    lambda z: z.writestr("extra.txt", b"extra"),
    lambda z: z.writestr("empty-extra/", b""),
    lambda z: z.writestr("../escaped", b"escape"),
    lambda z: z.writestr("C:/escaped", b"escape"),
    lambda z: z.writestr("_internal//alias", b"alias"),
    lambda z: z.writestr("tagencyclopedia.EXE", inert_pe()),
    lambda z: z.writestr("Apps/Reader/file", b"mutable"),
    lambda z: z.writestr("Data/synthetic.json", b"{}"),
    lambda z: z.writestr("State/synthetic.json", b"{}"), special,
])
def test_unsafe_extra_alias_mutable_and_special_members_hold_before_extraction(tmp_path, change):
    args = packet(tmp_path, zip_change=change)
    with pytest.raises(ValueError): verifier.verify_root_release(**args)
    assert not args["destination"].exists() and not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("mutation", ["sidecar", "trust", "signature", "archive", "metadata_name", "existing_destination"])
def test_exact_input_binding_and_crypto_failures(tmp_path, mutation):
    args = packet(tmp_path)
    if mutation == "sidecar": args["sidecar_path"].write_bytes(b"0" * 64 + b"  wrong.zip\n")
    elif mutation == "trust": args["expected_trust_sha256"] = "0" * 64
    elif mutation == "signature": args["signature_path"].write_bytes(b"{}")
    elif mutation == "archive":
        with args["archive_path"].open("ab") as stream: stream.write(b"drift")
    elif mutation == "metadata_name":
        p = args["manifest_path"].with_name("collectigen-release.json")
        args["manifest_path"].rename(p); args["manifest_path"] = p
    else: args["destination"].mkdir()
    with pytest.raises(ValueError): verifier.verify_root_release(**args)
    if mutation != "existing_destination": assert not args["destination"].exists()


@pytest.mark.parametrize("mutation", ["reader", "no_dependency", "wrong_machine"])
def test_actual_original_root_layout_and_machine_are_required(tmp_path, mutation):
    def change(rows):
        if mutation == "reader": rows[0] = ("TagEncyclopediaReader.exe", rows[0][1])
        elif mutation == "no_dependency": rows[1] = ("wrong-layout.dll", rows[1][1])
        else:
            value = bytearray(rows[0][1]); value[68:70] = b"\x4c\x01"; rows[0] = (rows[0][0], bytes(value))
    args = packet(tmp_path, members_change=change)
    with pytest.raises(ValueError): verifier.verify_root_release(**args)
    assert not args["destination"].exists()


def test_failure_report_and_partial_extraction_evidence_retained(tmp_path, monkeypatch):
    args = packet(tmp_path)
    original = verifier.extract_component_archive
    def failure(*a, **k):
        original(*a, **k)
        raise OSError("Synthetic readback interruption")
    monkeypatch.setattr(verifier, "extract_component_archive", failure)
    report = tmp_path / "retained-failure.json"
    argv = []
    mapping = {"archive": "archive_path", "manifest": "manifest_path", "signature": "signature_path",
        "sidecar": "sidecar_path", "trust": "trust_path", "destination": "destination",
        "expected-version": "expected_version", "expected-channel": "expected_channel",
        "expected-basename": "expected_basename", "expected-trust-sha256": "expected_trust_sha256"}
    for flag, key in mapping.items(): argv.extend(("--" + flag, str(args[key])))
    argv.extend(("--report", str(report)))
    assert verifier.main(argv) == 1
    value = json.loads(report.read_bytes())
    assert value["status"] == "ROOT_VERIFICATION_HELD" and "Synthetic" in value["reason"]
    assert not value["execution_qualified"] and args["destination"].is_dir()
    assert (args["destination"] / verifier.INVENTORY).exists()


@pytest.mark.parametrize("mutation", ["parent_alias", "file_parent_collision"])
def test_tree_aliases_and_file_parent_collisions_hold_before_extraction(tmp_path, mutation):
    def change(rows):
        if mutation == "parent_alias":
            rows.extend((("Assets/a.txt", b"a"), ("assets/b.txt", b"b")))
        else:
            rows.extend((("collision", b"file"), ("collision/child.txt", b"child")))
    args = packet(tmp_path, members_change=change)
    with pytest.raises(ValueError): verifier.verify_root_release(**args)
    assert not args["destination"].exists()


def test_root_does_not_weaken_main_application_package_admission(tmp_path):
    from release_contract import verify_portable_package
    args = packet(tmp_path)
    result = verifier.verify_root_release(**args)
    args["sidecar_path"].write_bytes(args["sidecar_path"].read_bytes().replace(b"\n", b"\r\n"))
    args["destination"] = tmp_path / "verified-crlf"
    assert verifier.verify_root_release(**args).version == VERSION
    with pytest.raises(FileNotFoundError):
        verify_portable_package(result.extracted_directory, expected_version=VERSION)


def test_root_archive_bound_precedes_first_artifact_hash(tmp_path, monkeypatch):
    args = packet(tmp_path, manifest_change=lambda m: m["artifacts"][0].update(
        size_bytes=verifier.POLICY.max_archive_bytes + 1))
    monkeypatch.setattr(verifier, "verify_artifact", lambda *a: pytest.fail("oversized artifact reached hashing"))
    with pytest.raises(verifier.RootReleaseHold, match="before_hashing"):
        verifier.verify_root_release(**args)
    assert not args["destination"].exists()


def rebind_disposable_fixture_archive(args):
    """Refresh only existing disposable test envelopes after raw ZIP mutation."""
    archive = args["archive_path"]
    value = json.loads(args["manifest_path"].read_bytes())
    value["artifacts"][0].update(size_bytes=archive.stat().st_size,
        sha256=sha256(archive.read_bytes()).hexdigest())
    content = raw(value)
    args["manifest_path"].write_bytes(content)
    args["signature_path"].write_bytes(raw(signature(content)))
    args["sidecar_path"].write_bytes((sha256(archive.read_bytes()).hexdigest() + "  " + NAME + "\n").encode())


@pytest.mark.parametrize("mutation", ["nul", "backslash"])
def test_original_zip_filename_is_admitted_before_truncation_or_normalization(tmp_path, monkeypatch, mutation):
    canonical = "_internal/python314.dll"
    if mutation == "nul":
        wire_name = canonical + "Xignored"
        def members(rows): rows[1] = (wire_name, rows[1][1])
        def inventory(value):
            for row in value["files"]:
                if row["path"] == wire_name: row["path"] = canonical
        args = packet(tmp_path, members_change=members, inventory_change=inventory)
        original, replacement = wire_name.encode(), canonical.encode() + b"\0ignored"
    else:
        args = packet(tmp_path)
        original, replacement = canonical.encode(), canonical.replace("/", "\\").encode()
    data = args["archive_path"].read_bytes()
    assert len(original) == len(replacement) and data.count(original) == 2
    args["archive_path"].write_bytes(data.replace(original, replacement))
    rebind_disposable_fixture_archive(args)
    with zipfile.ZipFile(args["archive_path"]) as z:
        info = next(i for i in z.infolist() if "python314" in i.orig_filename)
        if mutation == "nul":
            assert info.filename == canonical and "\0" in info.orig_filename
        else:
            assert "\\" in info.orig_filename
    monkeypatch.setattr(verifier, "extract_component_archive", lambda *a, **k: pytest.fail("bad original spelling reached extraction"))
    with pytest.raises(verifier.RootReleaseHold, match="original_spelling"):
        verifier.verify_root_release(**args)
    assert not args["destination"].exists()


def test_directory_member_payload_is_refused_before_output_creation(tmp_path, monkeypatch):
    args = packet(tmp_path, zip_change=lambda z: z.writestr("_internal/", b"x" * 31))
    with zipfile.ZipFile(args["archive_path"]) as z:
        info = z.getinfo("_internal/")
        assert info.is_dir() and info.file_size == 31
    monkeypatch.setattr(verifier, "extract_component_archive", lambda *a, **k: pytest.fail("directory payload reached extraction"))
    with pytest.raises(verifier.RootReleaseHold, match="directory_payload"):
        verifier.verify_root_release(**args)
    assert not args["destination"].exists()
