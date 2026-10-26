import base64
from hashlib import sha256
import json
from pathlib import Path
import stat
import zipfile

import pytest
from suite_core.installation import ComponentId
from suite_core.discovery.component_staging import (
    create_disposable_root, stage_and_activate, finish_or_recover, active_token,
    verify_directory, StagingHold, INVENTORY,
)

COMPONENT = ComponentId("collectigen.suite-manager")
KEY = json.loads((Path(__file__).parent / "fixtures/disposable_rsa_key.json").read_text())
N = int.from_bytes(base64.urlsafe_b64decode(KEY["modulus"] + "=" * (-len(KEY["modulus"]) % 4)), "big")
D = int.from_bytes(base64.b64decode(KEY["private_exponent"]), "big")
TRUST = json.dumps(dict(schema_version=1, keys=[dict(key_id="disposable-test-only", algorithm="rsa-pkcs1v15-sha256",
    status="active", modulus=KEY["modulus"], exponent=65537)])).encode()


def raw(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":")).encode()


def profile(version, contents):
    return raw(dict(schema_version=1, component_id=COMPONENT.value, software_version=version,
        platform=dict(os="windows", architecture="x86_64"), runtime_kind="frozen-portable",
        files=[dict(path=name, size_bytes=len(data), sha256=sha256(data).hexdigest()) for name, data in contents.items()]))


def sign(message):
    length = (N.bit_length() + 7) // 8
    info = bytes.fromhex("3031300d060960864801650304020105000420") + sha256(message).digest()
    encoded = b"\0\1" + b"\xff" * (length - len(info) - 3) + b"\0" + info
    signed = pow(int.from_bytes(encoded, "big"), D, N).to_bytes(length, "big")
    return raw(dict(schema_version=1, key_id="disposable-test-only", algorithm="rsa-pkcs1v15-sha256",
        manifest_sha256=sha256(message).hexdigest(), signature=base64.b64encode(signed).decode()))


def package(folder, *, version="0.1.0-alpha.2", contents=None, extra=None, profile_override=None):
    contents = contents or {"bin/manager.txt": b"INERT NEW CODE; DO NOT EXECUTE"}
    inventory = profile_override or profile(version, contents)
    archive = folder / ("fixture-" + version + ".zip")
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(INVENTORY, inventory)
        for name, data in contents.items():
            z.writestr(name, data)
        if extra:
            for name, data in extra:
                z.writestr(name, data)
    m = raw(dict(schema_version=1, product="collectigen", component_id=COMPONENT.value, version=version,
        channel="beta", published_at="2026-10-06T00:00:00Z", supported_from="0.1.0-alpha.1",
        release_notes="Synthetic package only.", platform=dict(os="windows", architecture="x86_64"),
        artifacts=[dict(name=archive.name, kind="component-package", sha256=sha256(archive.read_bytes()).hexdigest(),
            size_bytes=archive.stat().st_size, portable_manifest_sha256=sha256(inventory).hexdigest())]))
    return archive, m, sign(m)


def setup_root(tmp_path):
    root = create_disposable_root(tmp_path / "synthetic-install")
    old = root / "Apps" / COMPONENT.value / "0.1.0-alpha.1"
    old.mkdir(parents=True)
    (old / "old.txt").write_bytes(b"INERT OLD CODE")
    inventory = profile("0.1.0-alpha.1", {"old.txt": b"INERT OLD CODE"})
    (old / INVENTORY).write_bytes(inventory)
    ref = dict(component_id=COMPONENT.value, version="0.1.0-alpha.1",
        directory=old.relative_to(root).as_posix(), inventory_sha256=sha256(inventory).hexdigest())
    base = root / "State" / COMPONENT.value
    base.mkdir(parents=True)
    (base / "active.json").write_bytes(raw(ref))
    data = root / "Data" / "catalogs"
    data.mkdir(parents=True)
    (data / "own-catalog.txt").write_bytes(b"SYNTHETIC USER OVERRIDE AND IMAGE ASSIGNMENTS")
    return root, ref, old


def data_hash(root):
    return sha256((root / "Data/catalogs/own-catalog.txt").read_bytes()).hexdigest()


def activate(root, before, pkg, **changes):
    archive, manifest, signature = pkg
    kw = dict(component=COMPONENT, version="0.1.0-alpha.2", os_name="windows", architecture="x86_64",
        manifest_bytes=manifest, signature_bytes=signature, trust_bytes=TRUST,
        expected_active_token=active_token(before), operator_approved=True, idle=True)
    kw.update(changes)
    return stage_and_activate(root, archive, **kw)


def read_active(root):
    return json.loads((root / "State" / COMPONENT.value / "active.json").read_bytes())


def test_real_extract_activate_health_rollback_readback_and_data_preserved(tmp_path):
    root, before, old = setup_root(tmp_path)
    saved = data_hash(root)
    target = activate(root, before, package(tmp_path))
    assert read_active(root) == target
    assert (root / target["directory"] / "bin/manager.txt").read_bytes() == b"INERT NEW CODE; DO NOT EXECUTE"
    assert (old / "old.txt").read_bytes() == b"INERT OLD CODE"
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST, health_verified=True) == target
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST) == target
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST, rollback_approved=True) == before
    assert read_active(root) == before and data_hash(root) == saved
    assert (root / target["directory"]).is_dir()  # retained, not overwritten/deleted


@pytest.mark.parametrize("phase", ["staging_created", "member_extracted", "before_version_publish", "version_published", "active_reference_replaced"])
def test_interrupted_filesystem_staging_or_activation_recovers_old_reference(tmp_path, phase):
    root, before, old = setup_root(tmp_path)
    saved = data_hash(root)
    def failpoint(observed):
        if observed == phase:
            raise RuntimeError("INJECTED INTERRUPTION")
    with pytest.raises(RuntimeError, match="INTERRUPTION"):
        activate(root, before, package(tmp_path), failpoint=failpoint)
    result = finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST)
    assert result == before and read_active(root) == before and data_hash(root) == saved
    assert not list((root / "State" / COMPONENT.value).glob("staging-*"))
    assert (old / "old.txt").read_bytes() == b"INERT OLD CODE"
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST) == before


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "C:/drive.txt", "bin\\escape", "bin/../escape", "bin//alias", "CON.txt", "Data/user.txt"])
def test_malicious_archive_paths_or_mutable_payload_rejected_without_escape(tmp_path, name):
    root, before, _ = setup_root(tmp_path)
    saved = data_hash(root)
    pkg = package(tmp_path, contents={name: b"INERT"})
    with pytest.raises(ValueError):
        activate(root, before, pkg)
    assert read_active(root) == before and data_hash(root) == saved
    assert not (tmp_path / "outside.txt").exists()
    finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST)


@pytest.mark.parametrize("extra", [[("BIN/MANAGER.TXT", b"alias")], [("unexpected.txt", b"unlisted")]])
def test_case_alias_or_unlisted_archive_bytes_rejected(tmp_path, extra):
    root, before, _ = setup_root(tmp_path)
    with pytest.raises(ValueError):
        activate(root, before, package(tmp_path, extra=extra))
    assert read_active(root) == before
    finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST)


def test_zip_symlink_entry_rejected(tmp_path):
    root, before, _ = setup_root(tmp_path)
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    pkg = package(tmp_path, extra=[(info, b"../outside")])
    with pytest.raises(StagingHold, match="archive_link"):
        activate(root, before, pkg)
    assert read_active(root) == before


@pytest.mark.parametrize("change", [dict(operator_approved=False), dict(idle=False),
    dict(expected_active_token="0" * 64), dict(trust_bytes=b'{"schema_version":1,"keys":[]}')])
def test_real_mutation_requires_approval_idle_stale_guard_and_signature(tmp_path, change):
    root, before, _ = setup_root(tmp_path)
    with pytest.raises(ValueError):
        activate(root, before, package(tmp_path), **change)
    assert read_active(root) == before
    assert not (root / "Apps" / COMPONENT.value / "0.1.0-alpha.2").exists()


def test_archive_hash_and_payload_inventory_hash_rejected(tmp_path):
    root, before, _ = setup_root(tmp_path)
    pkg = package(tmp_path)
    pkg[0].write_bytes(pkg[0].read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="size"):
        activate(root, before, pkg)
    mismatch = json.loads(profile("0.1.0-alpha.2", {"bin/manager.txt": b"wrong bytes"}))
    pkg = package(tmp_path, profile_override=raw(mismatch))
    with pytest.raises(StagingHold):
        activate(root, before, pkg)
    assert read_active(root) == before


def test_wrong_journal_owner_and_unexpected_active_ref_hold(tmp_path):
    root, before, _ = setup_root(tmp_path)
    activate(root, before, package(tmp_path))
    journal = root / "State" / COMPONENT.value / "transaction.json"
    original = json.loads(journal.read_bytes())
    changed = dict(original, owner="0" * 32)
    journal.write_bytes(raw(changed))
    with pytest.raises(StagingHold, match="ownership"):
        finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST)
    journal.write_bytes(raw(original))
    (journal.parent / "active.json").write_bytes(raw(dict(before, version="0.9.0")))
    with pytest.raises(StagingHold, match="unexpected_active"):
        finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST)


def test_prior_code_corruption_blocks_rollback(tmp_path):
    root, before, old = setup_root(tmp_path)
    target = activate(root, before, package(tmp_path))
    (old / "old.txt").write_bytes(b"CORRUPTED OLD")
    with pytest.raises(StagingHold):
        finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST)
    assert read_active(root) == target


def test_busy_lock_and_existing_version_are_not_overwritten(tmp_path):
    root, before, _ = setup_root(tmp_path)
    lock = root / "State" / COMPONENT.value / "exclusive.lock"
    lock.write_bytes(b"another owner")
    with pytest.raises(StagingHold, match="busy"):
        activate(root, before, package(tmp_path))
    lock.unlink()
    destination = root / "Apps" / COMPONENT.value / "0.1.0-alpha.2"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"OTHER OWNED CONTENT")
    with pytest.raises(StagingHold, match="already_exists"):
        activate(root, before, package(tmp_path))
    assert (destination / "keep.txt").read_bytes() == b"OTHER OWNED CONTENT"


def test_hardlink_and_unmarked_or_nonempty_roots_rejected(tmp_path):
    root, before, old = setup_root(tmp_path)
    import os
    os.link(old / "old.txt", tmp_path / "other-link.txt")
    with pytest.raises(StagingHold, match="multiply_linked"):
        activate(root, before, package(tmp_path))
    with pytest.raises(StagingHold, match="empty_disposable"):
        create_disposable_root(root)
    other = tmp_path / "unmarked"
    other.mkdir()
    with pytest.raises(FileNotFoundError):
        activate(other, before, package(tmp_path))


def test_authenticated_supported_from_excludes_existing_installation_before_writes(tmp_path):
    root, before, _ = setup_root(tmp_path)
    archive, message, _ = package(tmp_path)
    m = json.loads(message)
    m["supported_from"] = "0.1.0-alpha.2"
    message = raw(m)
    with pytest.raises(ValueError, match="supports automatic upgrades"):
        activate(root, before, (archive, message, sign(message)))
    assert read_active(root) == before
    assert not (root / "State" / COMPONENT.value / "transaction.json").exists()


def test_archive_contained_python_is_extracted_as_bytes_never_executed(tmp_path, monkeypatch):
    import subprocess
    import os
    def deny(*a, **kw):
        raise AssertionError("archive-contained code must never run")
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(os, "system", deny)
    root, before, _ = setup_root(tmp_path)
    code = b'raise RuntimeError("ARCHIVE_CODE_MUST_NOT_RUN")\n'
    target = activate(root, before, package(tmp_path, contents={"runme.py": code}))
    assert (root / target["directory"] / "runme.py").read_bytes() == code
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST) == before


def test_atomic_reference_replace_failure_leaves_old_active_and_recovers(tmp_path, monkeypatch):
    from suite_core.discovery import component_staging as module
    root, before, _ = setup_root(tmp_path)
    saved = data_hash(root)
    replace = module.os.replace
    def fail(source, target):
        if Path(target).name == "active.json":
            raise OSError("INJECTED ATOMIC REPLACE FAILURE")
        return replace(source, target)
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError, match="ATOMIC"):
        activate(root, before, package(tmp_path))
    assert read_active(root) == before
    monkeypatch.setattr(module.os, "replace", replace)
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST) == before
    assert data_hash(root) == saved


def test_retained_platform_mismatch_blocks_replacement(tmp_path):
    root, before, old = setup_root(tmp_path)
    profile_data = json.loads((old / INVENTORY).read_bytes())
    profile_data["platform"]["os"] = "linux"
    changed = raw(profile_data)
    (old / INVENTORY).write_bytes(changed)
    before["inventory_sha256"] = sha256(changed).hexdigest()
    (root / "State" / COMPONENT.value / "active.json").write_bytes(raw(before))
    with pytest.raises(StagingHold, match="retained_inventory_platform"):
        activate(root, before, package(tmp_path))
    assert read_active(root) == before


# Pure future-reference contract; no schema2 transaction entry is enabled.
def reference_release(*, schema=2, maturity="alpha", version="0.1.0"):
    from suite_core.discovery.component_release_signature import verify_component_release
    channel = ("stable" if maturity == "stable" else "beta") if schema == 2 else ("beta" if "-" in version else "stable")
    manifest = dict(schema_version=schema, product="collectigen", component_id=COMPONENT.value,
        version=version, channel=channel, published_at="2026-10-26T00:00:00Z",
        supported_from="0.0.0", release_notes="Contained reference identity only.",
        platform=dict(os="windows", architecture="x86_64"), artifacts=[dict(name="inert.zip",
        kind="component-package", sha256="a"*64, size_bytes=12, portable_manifest_sha256="b"*64)])
    if schema == 2:
        manifest["maturity"] = maturity
    message = raw(manifest)
    return verify_component_release(message, sign(message), TRUST, expected_component=COMPONENT,
        expected_version=version, expected_channel=channel, expected_os="windows",
        expected_architecture="x86_64", expected_maturity=maturity if schema == 2 else None)


@pytest.fixture
def pure_reference_guards(monkeypatch):
    import suite_core.discovery.component_staging as staging
    def forbidden(*args, **kwargs):
        pytest.fail("Pure reference helper attempted an external effect")
    for name in ("guard", "read_json", "atomic", "atomic_bytes", "verify_directory"):
        monkeypatch.setattr(staging, name, forbidden)
    monkeypatch.setattr(staging, "Path", forbidden)


@pytest.mark.parametrize("maturity", ["alpha", "beta", "stable"])
def test_pure_stage_reference_signed_roundtrip(pure_reference_guards, maturity):
    from suite_core.discovery.component_staging import component_reference_from_release, validate_component_reference, canonical
    release = reference_release(maturity=maturity)
    reference = component_reference_from_release(release)
    assert reference == dict(schema_version=2, release_schema=2, component_id=COMPONENT.value,
        version="0.1.0", maturity=maturity, directory=f"Apps/{COMPONENT.value}/schema2/0.1.0/{maturity}",
        inventory_sha256="b"*64, release_sha256=release.manifest_sha256)
    returned = validate_component_reference(json.loads(canonical(reference)), COMPONENT, release=release)
    assert returned == reference and returned is not reference
    returned["version"] = "9.0.0"
    assert reference["version"] == "0.1.0"


@pytest.mark.parametrize("version", ["0.1.0-alpha.1", "0.1.0-beta.2", "0.1.0"])
def test_pure_legacy_reference_bytes_unchanged(pure_reference_guards, version):
    from suite_core.discovery.component_staging import component_reference_from_release, validate_component_reference, canonical
    release = reference_release(schema=1, version=version)
    expected = dict(component_id=COMPONENT.value, version=version,
                    directory=f"Apps/{COMPONENT.value}/{version}", inventory_sha256="b"*64)
    assert canonical(component_reference_from_release(release)) == raw(expected)
    assert validate_component_reference(expected, COMPONENT, release=release) == expected


def test_pure_stage_destinations_distinct_from_each_other_and_legacy(pure_reference_guards):
    from suite_core.discovery.component_staging import component_reference_from_release
    refs = [component_reference_from_release(reference_release(maturity=m)) for m in ("alpha", "beta", "stable")]
    refs.append(component_reference_from_release(reference_release(schema=1)))
    assert len({r["directory"] for r in refs}) == 4


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 3), ("release_schema", True), ("release_schema", 1),
    ("component_id", "collectigen.other"), ("version", "0.2.0"), ("version", 1),
    ("maturity", "beta"), ("maturity", None), ("maturity", "Alpha"),
    ("inventory_sha256", "c"*64), ("inventory_sha256", "B"*64),
    ("release_sha256", "c"*64), ("release_sha256", "A"*64),
    ("directory", "../escape"), ("directory", "C:/escape"), ("directory", "Apps/../escape"),
    ("directory", "Apps/alias"), ("directory", f"Apps/{COMPONENT.value}/schema2/0.1.0/beta"),
    ("directory", f"Apps/{COMPONENT.value}/schema2/0.1.0/alpha/"),
])
def test_pure_reference_rejects_tamper_without_mutation(pure_reference_guards, field, value):
    from suite_core.discovery.component_staging import component_reference_from_release, validate_component_reference
    release = reference_release(); ref = component_reference_from_release(release); ref[field] = value
    before = dict(ref)
    with pytest.raises(ValueError): validate_component_reference(ref, COMPONENT, release=release)
    assert ref == before


@pytest.mark.parametrize("schema", [1, 2])
def test_pure_reference_exact_keys_and_schema_separation(pure_reference_guards, schema):
    from suite_core.discovery.component_staging import component_reference_from_release, validate_component_reference
    release = reference_release(schema=schema); ref = component_reference_from_release(release)
    for key in ref:
        changed = dict(ref); changed.pop(key)
        with pytest.raises(ValueError): validate_component_reference(changed, COMPONENT, release=release)
    for key, value in [("unknown", None), ("maturity", "alpha"), ("schema_version", 2)]:
        if key not in ref:
            with pytest.raises(ValueError): validate_component_reference(dict(ref, **{key:value}), COMPONENT, release=release)
    opposite = component_reference_from_release(reference_release(schema=3-schema))
    with pytest.raises(ValueError): validate_component_reference(opposite, COMPONENT, release=release)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 3), ("component_id", "collectigen.suite-manager"),
    ("software_version", "../bad"), ("software_version", "01.1.0"), ("software_version", "0.1.0-alpha.1"),
    ("maturity", "Alpha"), ("maturity", None), ("maturity", True),
    ("manifest_sha256", "A"*64), ("manifest_sha256", "bad"), ("manifest_sha256", None),
    ("channel", "stable"), ("os", "other"), ("architecture", "other"), ("artifact", None),
])
def test_pure_reference_rejects_inconsistent_dto(pure_reference_guards, field, value):
    from dataclasses import replace
    from suite_core.discovery.component_staging import component_reference_from_release
    with pytest.raises(ValueError): component_reference_from_release(replace(reference_release(), **{field:value}))


@pytest.mark.parametrize("field,value", [("portable_manifest_sha256", "A"*64), ("portable_manifest_sha256", ""), ("kind", "other")])
def test_pure_reference_rejects_bad_artifact_identity(pure_reference_guards, field, value):
    from dataclasses import replace
    from suite_core.discovery.component_staging import component_reference_from_release
    release = reference_release()
    with pytest.raises(ValueError): component_reference_from_release(replace(release, artifact=replace(release.artifact, **{field:value})))


def test_pure_reference_requires_exact_typed_inputs(pure_reference_guards):
    from suite_core.discovery.component_staging import component_reference_from_release, validate_component_reference
    release = reference_release(); ref = component_reference_from_release(release)
    for value in (None, {}, release.__dict__):
        with pytest.raises(ValueError): component_reference_from_release(value)
    with pytest.raises(ValueError): validate_component_reference(ref, COMPONENT.value, release=release)
    with pytest.raises(ValueError): validate_component_reference(ref, ComponentId("collectigen.other"), release=release)
    for value in (None, [], list(ref.items())):
        with pytest.raises(ValueError): validate_component_reference(value, COMPONENT, release=release)


def test_legacy_staging_still_refuses_new_reference(tmp_path):
    from suite_core.discovery.component_staging import payload, component_reference_from_release
    ref = component_reference_from_release(reference_release())
    with pytest.raises(ValueError, match="reference_schema"):
        payload(tmp_path, ref, COMPONENT)
    assert list(tmp_path.iterdir()) == []


# Explicit new-mode transactions use inert packages and original staging only.
def stage_fixture_package(folder, stage="alpha", version="0.1.0", schema=2):
    directory = folder / ("package-" + version + "-" + stage)
    directory.mkdir(exist_ok=True)
    archive, message, _ = package(directory, version=version)
    value = json.loads(message)
    value.update(schema_version=schema, channel=("stable" if stage == "stable" else "beta"), supported_from="0.0.0")
    if schema == 2:
        value["maturity"] = stage
    message = raw(value)
    return archive, message, sign(message)


def empty_stage_root(tmp_path):
    root = create_disposable_root(tmp_path / "signed-stage-root")
    data = root / "Data/catalogs"; data.mkdir(parents=True)
    (data / "own-catalog.txt").write_bytes(b"SYNTHETIC DATA MUST SURVIVE")
    return root


def stage_fixture(root, before, pkg, *, stage="alpha", version="0.1.0", schema=2, **kw):
    return stage_and_activate(root, pkg[0], component=COMPONENT, version=version,
        os_name="windows", architecture="x86_64", manifest_bytes=pkg[1], signature_bytes=pkg[2],
        trust_bytes=TRUST, expected_active_token=active_token(before), operator_approved=True,
        idle=True, release_schema=schema, maturity=stage if schema == 2 else None, **kw)


def settle_stage(root, **kw):
    return finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST, allow_schema2=True, **kw)


def test_explicit_schema2_same_number_promotions_and_saved_recovery(tmp_path):
    root = empty_stage_root(tmp_path); saved = data_hash(root); before = None; retained = []
    for stage in ("alpha", "beta", "stable"):
        target = stage_fixture(root, before, stage_fixture_package(tmp_path, stage), stage=stage)
        assert target["maturity"] == stage and target["directory"].endswith("/0.1.0/"+stage)
        assert settle_stage(root, health_verified=True) == target
        retained.append(target); before = target
    assert settle_stage(root, rollback_approved=True) == retained[1]
    assert settle_stage(root) == retained[1]
    assert all((root/r["directory"]).exists() for r in retained) and data_hash(root) == saved


def test_explicit_legacy_to_schema2_and_signed_legacy_rollback(tmp_path):
    root = empty_stage_root(tmp_path)
    prior = stage_fixture(root, None, stage_fixture_package(tmp_path, "stable", "0.0.1", 1), stage="stable", version="0.0.1", schema=1)
    assert finish_or_recover(root, component=COMPONENT, trust_bytes=TRUST, health_verified=True) == prior
    prior_bytes = (root/"State"/COMPONENT.value/"transaction.json").read_bytes()
    target = stage_fixture(root, prior, stage_fixture_package(tmp_path))
    assert target["release_schema"] == 2
    assert settle_stage(root, rollback_approved=True) == prior
    assert (root/"State"/COMPONENT.value/"transaction.json").read_bytes() == prior_bytes
    assert settle_stage(root) == prior


@pytest.mark.parametrize("phase", ["staging_created", "member_extracted", "before_version_publish",
    "prior_journal_preserved", "new_journal_established", "version_published", "active_reference_replaced"])
def test_explicit_stage_failure_recovers_independent_signed_prior(tmp_path, phase):
    root = empty_stage_root(tmp_path); saved=data_hash(root)
    before = stage_fixture(root, None, stage_fixture_package(tmp_path))
    settle_stage(root, health_verified=True)
    prior_bytes=(root/"State"/COMPONENT.value/"transaction.json").read_bytes()
    def failpoint(at):
        if at == phase: raise InterruptedError("INJECTED CANCELLATION")
    with pytest.raises(InterruptedError):
        stage_fixture(root, before, stage_fixture_package(tmp_path, "beta"), stage="beta", failpoint=failpoint)
    assert settle_stage(root) == before
    assert settle_stage(root) == before
    assert (root/"State"/COMPONENT.value/"transaction.json").read_bytes() == prior_bytes
    assert data_hash(root) == saved


@pytest.mark.parametrize("schema,maturity", [(True,None),(3,None),(1,"alpha"),(2,None),(2,"Alpha"),(2,True)])
def test_explicit_stage_partial_options_hold_before_writes(tmp_path,schema,maturity):
    root=empty_stage_root(tmp_path);pkg=stage_fixture_package(tmp_path)
    before=sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    with pytest.raises(ValueError):
        stage_and_activate(root,pkg[0],component=COMPONENT,version="0.1.0",os_name="windows",architecture="x86_64",
            manifest_bytes=pkg[1],signature_bytes=pkg[2],trust_bytes=TRUST,expected_active_token=active_token(None),
            operator_approved=True,idle=True,release_schema=schema,maturity=maturity)
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*"))==before


def test_schema2_recovery_and_payload_require_explicit_context(tmp_path):
    from suite_core.discovery.component_staging import payload, validate_journal, owned
    root=empty_stage_root(tmp_path);target=stage_fixture(root,None,stage_fixture_package(tmp_path))
    _,base,owner=owned(root,COMPONENT);journal=json.loads((base/"transaction.json").read_bytes())
    for call in (lambda: finish_or_recover(root,component=COMPONENT,trust_bytes=TRUST),
                 lambda: validate_journal(journal,COMPONENT,owner,TRUST),lambda:payload(root,target,COMPONENT)):
        with pytest.raises(ValueError): call()
    release=validate_journal(journal,COMPONENT,owner,TRUST,allow_schema2=True)
    assert payload(root,target,COMPONENT,release=release)==root/target["directory"]
    for value in (None,1,"yes"):
        with pytest.raises(ValueError): finish_or_recover(root,component=COMPONENT,trust_bytes=TRUST,allow_schema2=value)


@pytest.mark.parametrize("stage,version",[("alpha","0.1.0"),("beta","0.1.0"),("stable","0.0.1")])
def test_explicit_equal_downgrade_and_numeric_regression_hold(tmp_path,stage,version):
    root=empty_stage_root(tmp_path);before=stage_fixture(root,None,stage_fixture_package(tmp_path,"beta"),stage="beta")
    settle_stage(root,health_verified=True)
    journal=(root/"State"/COMPONENT.value/"transaction.json").read_bytes()
    with pytest.raises(ValueError): stage_fixture(root,before,stage_fixture_package(tmp_path,stage,version),stage=stage,version=version)
    assert read_active(root)==before and (root/"State"/COMPONENT.value/"transaction.json").read_bytes()==journal


def test_explicit_later_numeric_upgrade_and_occupied_target_refusal(tmp_path):
    root=empty_stage_root(tmp_path);before=stage_fixture(root,None,stage_fixture_package(tmp_path,"stable"),stage="stable")
    settle_stage(root,health_verified=True)
    pkg=stage_fixture_package(tmp_path,"alpha","0.2.0")
    destination=root/"Apps"/COMPONENT.value/"schema2/0.2.0/alpha";destination.mkdir(parents=True)
    (destination/"retained.txt").write_bytes(b"MUST NOT OVERWRITE")
    with pytest.raises(ValueError,match="immutable_version_already_exists"):
        stage_fixture(root,before,pkg,stage="alpha",version="0.2.0")
    assert (destination/"retained.txt").read_bytes()==b"MUST NOT OVERWRITE" and read_active(root)==before


@pytest.mark.parametrize("mutation", ["signature","stage","digest","platform","floor","stale"])
def test_explicit_stage_rejects_changed_signed_inputs(tmp_path,mutation):
    root=empty_stage_root(tmp_path);before=stage_fixture(root,None,stage_fixture_package(tmp_path))
    settle_stage(root,health_verified=True)
    pkg=list(stage_fixture_package(tmp_path,"beta"));value=json.loads(pkg[1]); passed_before=before
    if mutation=="signature": pkg[2]=sign(pkg[1]+b"changed")
    elif mutation=="stage": value["maturity"]="stable";pkg[1]=raw(value);pkg[2]=sign(pkg[1])
    elif mutation=="digest": value["artifacts"][0]["sha256"]="c"*64;pkg[1]=raw(value);pkg[2]=sign(pkg[1])
    elif mutation=="platform": value["platform"]["os"]="linux";pkg[1]=raw(value);pkg[2]=sign(pkg[1])
    elif mutation=="floor": value["supported_from"]="0.1.1";pkg[1]=raw(value);pkg[2]=sign(pkg[1])
    else: passed_before=None
    journal=(root/"State"/COMPONENT.value/"transaction.json").read_bytes()
    with pytest.raises(ValueError): stage_fixture(root,passed_before,pkg,stage="beta")
    assert read_active(root)==before and (root/"State"/COMPONENT.value/"transaction.json").read_bytes()==journal


@pytest.mark.parametrize("tamper",["missing","hash","stage","owner"])
def test_explicit_recovery_refuses_tampered_prior_custody(tmp_path,tamper):
    root=empty_stage_root(tmp_path);before=stage_fixture(root,None,stage_fixture_package(tmp_path))
    settle_stage(root,health_verified=True);target=stage_fixture(root,before,stage_fixture_package(tmp_path,"beta"),stage="beta")
    base=root/"State"/COMPONENT.value;j=json.loads((base/"transaction.json").read_bytes());prior=base/j["prior_journal"]["name"]
    if tamper=="missing": prior.unlink()
    elif tamper=="hash": prior.write_bytes(prior.read_bytes()+b" ")
    else:
        old=json.loads(prior.read_bytes())
        if tamper=="stage": old["target"]["maturity"]="stable"
        else: old["owner"]="f"*32
        prior.write_bytes(raw(old));j["prior_journal"]["sha256"]=sha256(prior.read_bytes()).hexdigest();(base/"transaction.json").write_bytes(raw(j))
    with pytest.raises((ValueError,OSError)): settle_stage(root,rollback_approved=True)
    assert read_active(root)==target


def test_explicit_transition_requires_independent_legacy_origin(tmp_path):
    root,before,_=setup_root(tmp_path);pkg=stage_fixture_package(tmp_path)
    with pytest.raises(ValueError,match="independently_signed_prior"):
        stage_fixture(root,before,pkg)
    assert read_active(root)==before


def test_explicit_stage_later_numeric_success_and_signed_floor(tmp_path):
    root=empty_stage_root(tmp_path);before=stage_fixture(root,None,stage_fixture_package(tmp_path,"stable"),stage="stable")
    settle_stage(root,health_verified=True)
    pkg=list(stage_fixture_package(tmp_path,"alpha","0.2.0"));m=json.loads(pkg[1]);m["supported_from"]="0.1.1"
    pkg[1]=raw(m);pkg[2]=sign(pkg[1])
    with pytest.raises(ValueError,match="supported_component_floor"):
        stage_fixture(root,before,pkg,version="0.2.0")
    target=stage_fixture(root,before,stage_fixture_package(tmp_path,"alpha","0.2.0"),version="0.2.0")
    assert settle_stage(root,health_verified=True)==target
    assert settle_stage(root,rollback_approved=True)==before


@pytest.mark.parametrize("lose_owner",[False,True])
def test_schema2_existing_admission_scope_rechecks_original_owner(tmp_path,lose_owner):
    from contextlib import contextmanager
    from suite_core.discovery.component_staging import (ComponentStageAdmission,ComponentStageBinding,
        component_stage_owner,stage_and_activate_admitted,finish_or_recover_admitted)
    root=tmp_path/"ordinary-fixture-root";root.mkdir();data=tmp_path/"separate-fixture-data";data.mkdir()
    owner=component_stage_owner(root,data,COMPONENT);checks=[]
    def check():
        checks.append(True)
        if lose_owner and len(checks)>3: raise ValueError("ORIGINAL OWNER LOST")
    @contextmanager
    def hold(component):
        assert component==COMPONENT
        yield ComponentStageBinding(root,owner,check,data)
    admission=ComponentStageAdmission(hold);pkg=stage_fixture_package(tmp_path)
    options=dict(component=COMPONENT,version="0.1.0",os_name="windows",architecture="x86_64",
        manifest_bytes=pkg[1],signature_bytes=pkg[2],trust_bytes=TRUST,expected_active_token=active_token(None),
        release_schema=2,maturity="alpha")
    with pytest.raises(ValueError,match="original_root_admission"):
        stage_and_activate_admitted(None,pkg[0],**options)
    if lose_owner:
        with pytest.raises(ValueError,match="ORIGINAL OWNER LOST"):
            stage_and_activate_admitted(admission,pkg[0],**options)
        assert not (root/"State"/COMPONENT.value/"active.json").exists()
    else:
        target=stage_and_activate_admitted(admission,pkg[0],**options)
        assert len(checks)>4
        assert finish_or_recover_admitted(admission,component=COMPONENT,trust_bytes=TRUST,
            health_verified=True,allow_schema2=True)==target


def test_schema2_stage_cannot_regress_to_legacy_entry(tmp_path):
    root=empty_stage_root(tmp_path);before=stage_fixture(root,None,stage_fixture_package(tmp_path))
    settle_stage(root,health_verified=True)
    pkg=stage_fixture_package(tmp_path,"stable","0.2.0",1)
    with pytest.raises(ValueError): stage_fixture(root,before,pkg,stage="stable",version="0.2.0",schema=1)
    assert read_active(root)==before


def test_schema2_reference_provenance_walk_is_bounded(tmp_path):
    from suite_core.discovery.component_staging import _signed_reference_release,owned
    root=empty_stage_root(tmp_path);target=stage_fixture(root,None,stage_fixture_package(tmp_path))
    _,base,owner=owned(root,COMPONENT);j=json.loads((base/"transaction.json").read_bytes())
    j["phase"]="healthy";j["before"]=target
    for index in range(34):
        name="transaction-"+f"{index:032x}"+".json";message=raw(j);(base/name).write_bytes(message)
        j=dict(j,prior_journal={"name":name,"sha256":sha256(message).hexdigest()})
    unknown=dict(target,release_sha256="f"*64)
    with pytest.raises(ValueError,match="bounded_component_history"):
        _signed_reference_release(unknown,j,base,COMPONENT,owner,TRUST)
