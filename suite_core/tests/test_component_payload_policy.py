"""Synthetic finite-policy admission; no installed data or runtime execution."""
from dataclasses import replace
from hashlib import sha256
import json
import stat
import zipfile
import pytest
from suite_core.installation import ComponentId
from suite_core.discovery.component_payload_policy import (
    select_payload_policy, require_payload_policy, payload_relative, FROZEN_PAYLOAD_POLICY,
    ORGANIZER_INTERPRETER_PAYLOAD_POLICY as POLICY)
from suite_core.discovery.component_release_signature import inventory_json, _json, MAX_METADATA_BYTES
from suite_core.discovery.offline_component_producer import produce_inventory
from suite_core.discovery.component_staging import (
    INVENTORY, verify_directory, extract_component_archive, create_disposable_root,
    stage_and_activate, finish_or_recover, active_token)
from suite_core.tests.test_component_staging import raw, sign, TRUST
COMPONENT = ComponentId("collectigen.media-organizer")
VERSION = "0.1.0-alpha.2"

def inventory(members, **kw):
    return produce_inventory(members, component_id=COMPONENT, software_version=VERSION,
        os_name="windows", architecture="x86_64", runtime_kind="interpreter-portable", **kw)

def write_tree(folder, members, contents=None):
    folder.mkdir()
    for name, data in members:
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    contents = contents or inventory(members)
    (folder / INVENTORY).write_bytes(contents)
    return sha256(contents).hexdigest()

@pytest.mark.parametrize("component,os_name,arch,kind", [
    ("collectigen.other", "windows", "x86_64", "interpreter-portable"),
    (COMPONENT.value, "linux", "x86_64", "interpreter-portable"),
    (COMPONENT.value, "windows", "aarch64", "interpreter-portable"),
    (COMPONENT.value, "windows", "x86_64", "unknown")])
def test_only_exact_trusted_expectations_select_enlarged_policy(component, os_name, arch, kind):
    with pytest.raises(ValueError):
        select_payload_policy(ComponentId(component), os_name, arch, kind)

def test_fixed_values_and_unforgeable_policy():
    assert select_payload_policy(COMPONENT, "windows", "x86_64") is FROZEN_PAYLOAD_POLICY
    assert select_payload_policy(COMPONENT, "windows", "x86_64", "interpreter-portable") is POLICY
    assert (POLICY.max_inventory_members, POLICY.max_zip_entries, POLICY.max_inventory_json_bytes,
        POLICY.max_assembly_tree_entries, POLICY.max_relative_path_characters) == (5120, 5121, 1048576, 6144, 512)
    assert POLICY.max_archive_bytes == POLICY.max_unpacked_bytes == 402653184
    assert MAX_METADATA_BYTES == 131072
    with pytest.raises(ValueError):
        require_payload_policy(replace(POLICY, max_inventory_members=99999))

@pytest.mark.parametrize("count", [5120, 5121])
def test_inventory_member_boundary(count):
    members = tuple((f"files/f{i}.txt", b"") for i in range(count))
    if count == 5120:
        assert len(json.loads(inventory(members))["files"]) == count
    else:
        with pytest.raises(ValueError, match="inventory_members"):
            inventory(members)

@pytest.mark.parametrize("length", [512, 513])
def test_relative_path_boundary(length):
    name = "/".join(["a" * 100] * 4 + ["b" * (length - 404)])
    assert len(name) == length
    if length == 512:
        inventory(((name, b""),))
    else:
        with pytest.raises(ValueError, match="path_bounds"):
            inventory(((name, b""),))

@pytest.mark.parametrize("size", [1048576, 1048577])
def test_inventory_json_byte_boundary_preserves_metadata_bound(size):
    content = b'{"x":"' + b'a' * (size - 8) + b'"}'
    assert len(content) == size
    with pytest.raises(ValueError):
        _json(content, "trust")
    if size == 1048576:
        assert len(inventory_json(content, max_bytes=1048576)["x"]) == size - 8
    else:
        with pytest.raises(ValueError, match="size"):
            inventory_json(content, max_bytes=1048576)

@pytest.mark.parametrize("content", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
    b'{"a":"\xff"}', b'{"a":' + b'[' * 17 + b'0' + b']' * 17 + b'}'])
def test_enlarged_parser_keeps_strict_structure(content):
    with pytest.raises(ValueError):
        inventory_json(content, max_bytes=1048576)

def test_directory_never_self_promotes_and_revalidates_bytes(tmp_path):
    members = tuple((f"files/f{i}.txt", b"inert") for i in range(700))
    tree = tmp_path / "tree"
    digest = write_tree(tree, members)
    with pytest.raises(ValueError):
        verify_directory(tree, digest, COMPONENT, VERSION)
    assert len(verify_directory(tree, digest, COMPONENT, VERSION, payload_policy=POLICY)["files"]) == 700
    (tree / members[0][0]).write_bytes(b"other")
    with pytest.raises(ValueError, match="payload_hash"):
        verify_directory(tree, digest, COMPONENT, VERSION, payload_policy=POLICY)

@pytest.mark.parametrize("count", [5121, 5122])
def test_zip_entry_boundary(tmp_path, count):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for i in range(count):
            z.writestr(f"f{i}.txt", b"")
    destination = tmp_path / "out"
    destination.mkdir()
    if count == 5121:
        extract_component_archive(archive, destination, payload_policy=POLICY)
        assert len(list(destination.iterdir())) == count
    else:
        with pytest.raises(ValueError, match="archive_bounds"):
            extract_component_archive(archive, destination, payload_policy=POLICY)
        assert not list(destination.iterdir())

@pytest.mark.parametrize("name,mode", [("folder/", stat.S_IFDIR), ("link", stat.S_IFLNK), ("pipe", stat.S_IFIFO)])
def test_interpreter_refuses_nonregular_zip_entries_before_writes(tmp_path, name, mode):
    archive = tmp_path / "test.zip"
    info = zipfile.ZipInfo(name)
    info.external_attr = (mode | 0o700) << 16
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(info, b"")
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ValueError):
        extract_component_archive(archive, destination, payload_policy=POLICY)
    assert not list(destination.iterdir())

@pytest.mark.parametrize("interruption", [None, "member_extracted", "version_published"])
def test_authenticated_stage_health_and_rollback_preserve_external_sentinel(tmp_path, interruption):
    root = create_disposable_root(tmp_path / "synthetic")
    sentinel = root / "Data" / "profile.txt"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"synthetic user authority")
    members = (("Runtime/python/python.txt", b"inert runtime"),)
    contents = inventory(members)
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(INVENTORY, contents)
        for name, data in members:
            z.writestr(name, data)
    manifest = raw(dict(schema_version=1, product="collectigen", component_id=COMPONENT.value,
        version=VERSION, channel="beta", published_at="2026-10-13T00:00:00Z", supported_from="0.1.0-alpha.1",
        release_notes="Synthetic only", platform=dict(os="windows", architecture="x86_64"),
        artifacts=[dict(name=archive.name, kind="component-package", size_bytes=archive.stat().st_size,
        sha256=sha256(archive.read_bytes()).hexdigest(), portable_manifest_sha256=sha256(contents).hexdigest())]))
    options = dict(component=COMPONENT, version=VERSION, os_name="windows", architecture="x86_64",
        manifest_bytes=manifest, signature_bytes=sign(manifest), trust_bytes=TRUST,
        expected_active_token=active_token(None), operator_approved=True, idle=True, payload_policy=POLICY)
    if interruption:
        def interrupt(phase):
            if phase == interruption:
                raise RuntimeError("synthetic interruption")
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            stage_and_activate(root, archive, failpoint=interrupt, **options)
        assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST,
            payload_policy=POLICY) is None
        assert sentinel.read_bytes() == b"synthetic user authority"
        return
    reference = stage_and_activate(root, archive, **options)
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST,
        health_verified=True, payload_policy=POLICY) == reference
    with pytest.raises(ValueError, match="stale_active_reference"):
        stage_and_activate(root, archive, **options)
    with pytest.raises(ValueError):
        stage_and_activate(root, archive, **{**options, "expected_active_token": active_token(reference)})
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST,
        rollback_approved=True, payload_policy=POLICY) is None
    assert sentinel.read_bytes() == b"synthetic user authority"

@pytest.mark.parametrize("count", [6144, 6145])
def test_directory_traversal_boundary(tmp_path, count):
    tree = tmp_path / "tree"
    digest = write_tree(tree, (("file.txt", b"inert"),))
    # Inventory plus payload occupy two traversal slots; empty directories count.
    for i in range(count - 2):
        (tree / f"d{i}").mkdir()
    if count == 6144:
        verify_directory(tree, digest, COMPONENT, VERSION, payload_policy=POLICY)
    else:
        with pytest.raises(ValueError, match="payload_tree_bounds"):
            verify_directory(tree, digest, COMPONENT, VERSION, payload_policy=POLICY)

@pytest.mark.parametrize("extra", [0, 1])
def test_archive_byte_boundary_without_large_allocation(tmp_path, monkeypatch, extra):
    from types import SimpleNamespace
    import suite_core.discovery.component_staging as staging
    archive = tmp_path / "fixture.zip"
    archive.write_bytes(b"inert")
    destination = tmp_path / "out"
    destination.mkdir()
    original_stat = type(archive).stat
    def measured_stat(path, *args, **kw):
        if path == archive and not kw.get("follow_symlinks") is False:
            return SimpleNamespace(st_size=POLICY.max_archive_bytes + extra)
        return original_stat(path, *args, **kw)
    monkeypatch.setattr(type(archive), "stat", measured_stat)
    class ReachedZipInspection(Exception):
        pass
    def zip_probe(*args):
        raise ReachedZipInspection
    monkeypatch.setattr(staging.zipfile, "ZipFile", zip_probe)
    expected = ValueError if extra else ReachedZipInspection
    with pytest.raises(expected):
        extract_component_archive(archive, destination, payload_policy=POLICY)
    assert not list(destination.iterdir())

@pytest.mark.parametrize("extra", [0, 1])
def test_unpacked_byte_boundary_before_extraction(tmp_path, monkeypatch, extra):
    from types import SimpleNamespace
    import suite_core.discovery.component_staging as staging
    archive = tmp_path / "fixture.zip"
    archive.write_bytes(b"inert")
    destination = tmp_path / "out"
    destination.mkdir()
    class ReachedPathInspection(Exception):
        pass
    class ZipProbe:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def infolist(self):
            return [SimpleNamespace(filename="file.txt", file_size=POLICY.max_unpacked_bytes + extra)]
    monkeypatch.setattr(staging.zipfile, "ZipFile", lambda *a: ZipProbe())
    def paths_probe(*args):
        raise ReachedPathInspection
    monkeypatch.setattr(staging, "safe_zip_members", paths_probe)
    expected = ValueError if extra else ReachedPathInspection
    with pytest.raises(expected):
        extract_component_archive(archive, destination, payload_policy=POLICY)
    assert not list(destination.iterdir())

@pytest.mark.parametrize("extra", [0, 1])
def test_zip_implied_parent_tree_boundary_before_member_writes(tmp_path, extra):
    # 383 independent 15-parent branches: 6128 entries. Shared 14-parent
    # branch plus two sibling files: 16 entries, not 30 duplicated parents.
    # Only 385 regular ZIP entries produce exactly 6144 filesystem entries.
    names = ["/".join([f"b{i}"] + ["d"] * 14 + ["file.txt"]) for i in range(383)]
    shared = "/".join(["shared"] + ["d"] * 13)
    names.extend([shared + "/first.txt", shared + "/second.txt"])
    if extra:
        names.append("one-over.txt")
    archive = tmp_path / "tree-boundary.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name in names:
            z.writestr(name, b"inert")
    destination = tmp_path / "out"
    destination.mkdir()
    extracted = []
    if extra:
        with pytest.raises(ValueError, match="archive_tree_bounds"):
            extract_component_archive(archive, destination, payload_policy=POLICY,
                member_extracted=lambda: extracted.append(True))
        assert not list(destination.iterdir())
        assert not extracted
    else:
        extract_component_archive(archive, destination, payload_policy=POLICY,
            member_extracted=lambda: extracted.append(True))
        assert len(extracted) == 385
        assert sum(1 for _ in destination.rglob("*")) == 6144

@pytest.mark.parametrize("name", [
    "Runtime/python/Lib/site-packages/PySide6/qml/QtQuick/Pdf/+Material/PdfStyle.qml",
    "Runtime/python/Lib/site-packages/PySide6/qml/QtQuick/Dialogs/quickimpl/qml/+Fusion/FileDialog.qml",
    "Runtime/python/Lib/site-packages/PySide6/qml/QtQuick/Controls/Windows/images/checkmark@" + "2x.png",
    "Runtime/python/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/objects-Debug/QmlAssetDownloaderPrivate_resources_1/.qt/rcc/qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj",
    "Runtime/python/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/objects-RelWithDebInfo/QmlAssetDownloaderPrivate_resources_1/.qt/rcc/qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj"])
def test_interpreter_vendor_paths_roundtrip_without_renaming(tmp_path, name):
    assert "/".join(payload_relative(name, payload_policy=POLICY)) == name
    with pytest.raises(ValueError):
        payload_relative(name)
    tree = tmp_path / "tree"
    members = ((name, b"inert vendor bytes"),)
    digest = write_tree(tree, members)
    verify_directory(tree, digest, COMPONENT, VERSION, payload_policy=POLICY)
    archive = tmp_path / "vendor.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(INVENTORY, (tree / INVENTORY).read_bytes())
        z.writestr(name, members[0][1])
    destination = tmp_path / "out"
    destination.mkdir()
    extract_component_archive(archive, destination, payload_policy=POLICY)
    verify_directory(destination, digest, COMPONENT, VERSION, payload_policy=POLICY)
    assert (destination / name).read_bytes() == members[0][1]

@pytest.mark.parametrize("name", [
    "../+Material/file.qml", "Runtime/../file@x", "Runtime/./file@x",
    "/Runtime/file@x", "//server/share/file@x", "C:/Runtime/file@x",
    "Runtime/file@x:stream", "Runtime\\file@x", "Runtime//file@x",
    "Runtime/CON@/../file", "Runtime/CON.txt", "Runtime/AUX", "Runtime/lpt9.qml",
    "Runtime/+Material/file.", "Runtime/+Material/file ", "Runtime/file?@x",
    "Runtime/file*@x", "Runtime/file|@x", "Runtime/file<@x", "Runtime/file\x00@x",
    "Runtime/fileÃ©@x", "Runtime/.hidden@x", "/".join(["d"] * 17),
    "Runtime/" + "a" * 129, "/".join(["a" * 100] * 4 + ["b" * 109])])
def test_interpreter_vendor_allowance_keeps_path_guards(name):
    with pytest.raises(ValueError):
        payload_relative(name, payload_policy=POLICY)

@pytest.mark.parametrize("name", ["Runtime/file#@x", "Runtime/file$@x", "Runtime/file%@x",
    "Runtime/file&@x", "Runtime/file=@x", "Runtime/file(@x)", "Runtime/file,@x", "Runtime/file;@x"])
def test_interpreter_allows_only_measured_extra_characters(name):
    with pytest.raises(ValueError):
        payload_relative(name, payload_policy=POLICY)

@pytest.mark.parametrize("names", [
    ("Runtime/+Material/File.qml", "Runtime/+material/file.qml"),
    ("Runtime/file@" + "x.txt", "Runtime/FILE@" + "X.txt")])
def test_vendor_case_collisions_refuse_producer_and_extraction_before_writes(tmp_path, names):
    with pytest.raises(ValueError):
        inventory(tuple((name, b"inert") for name in names))
    archive = tmp_path / "aliases.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name in names:
            z.writestr(name, b"inert")
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ValueError):
        extract_component_archive(archive, destination, payload_policy=POLICY)
    assert not list(destination.iterdir())

@pytest.mark.parametrize("name", [
    "Runtime/.qt/file.obj", "Runtime/python/Lib/site-packages/PySide6/.qt/file.obj",
    "Runtime/python/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/objects-Other/QmlAssetDownloaderPrivate_resources_1/.qt/file.obj",
    "Runtime/python/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/objects-Debug/QmlAssetDownloaderPrivate_resources_2/.qt/file.obj",
    "Runtime/python/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/objects-Debug/QmlAssetDownloaderPrivate_resources_1/.qt/.other/file.obj"])
def test_exact_qt_vendor_prefix_does_not_admit_general_dot_segments(name):
    with pytest.raises(ValueError):
        payload_relative(name, payload_policy=POLICY)
