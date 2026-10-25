"""Disposable source fixtures; recipient creation uses the production API only."""
from dataclasses import replace
import json

import pytest

from core import tag_catalog_connection as connection
from core.category_registry import CategoryRegistry
from core.tag_database import TagDatabase
from suite_core.encyclopedia.publication import StalePublication, read
from suite_core.knowledge import tag_transactions as transactions
from core.settings import AppSettings
from tools.build_tag_database_pack import build_tag_database_pack
from tests.test_tag_database_pack_producer import reviewed_local_input


@pytest.fixture
def selected(tmp_path, reviewed_local_input):
    source, _pack, publication, qualification, evidence = reviewed_local_input
    pack = build_tag_database_pack(source / "manifest.json", source, tmp_path / "dist",
        reviewed_local=True, source_publication=publication, local_qualification=qualification,
        curated_evidence=evidence, inventory_platform={"os": "windows", "architecture": "x86_64"})
    return tmp_path / "ManagerAppSettings.json", pack.parent, tmp_path / "catalog"


def connect(selected):
    review = connection.prepare_connection(*selected, True)
    return connection.confirm_connection(review, review.token).choice


def test_prepare_is_read_only_and_confirm_reopens_native_catalog(selected):
    settings, source, root = selected
    source_before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    review = connection.prepare_connection(*selected, True)
    assert not settings.exists() and not root.exists()
    with pytest.raises(connection.TagConnectionHold, match="token"):
        connection.confirm_connection(review, "wrong")
    result = connection.confirm_connection(review, review.token)
    choice = result.choice
    assert result.source_pin == review.source_pin
    assert not choice.installed and choice.available_release.revision.content_version == "0.1.1"
    assert choice.transactions.host.root == root
    assert not (root / "SYNTHETIC_ROOT.json").exists()
    assert json.loads((root / "data/user/tags.json").read_bytes())["tags"] == []
    assert list((root / "data/defaults").iterdir()) == []
    reopened = connection.open_saved_connection(settings).choice
    assert reopened.native_adapter.catalog_id == choice.native_adapter.catalog_id
    assert reopened.available_release == choice.available_release
    assert source_before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_confirm_reconnect_advances_saved_generation_after_native_correction(selected):
    from core.category_registry import CategoryRegistry
    from core.tag_database import TagDatabase
    from suite_core.encyclopedia.publication import read
    choice=connect(selected);settings,source,root=selected
    previous=read(root/'tag_catalog_snapshot.json')
    categories=CategoryRegistry(root/'data/defaults/categories.json',root/'data/user/categories.json')
    category=categories.add('Local correction');categories.save_user()
    tags=TagDatabase(root/'data/user/tags.json')
    record=tags.add_known_tag('local_correction',category=category.id,properties={'note':'keep'})
    tags.save_verified();before=(root/'data/user/tags.json').read_bytes()
    review=connection.prepare_connection(settings,source,root,False)
    result=connection.confirm_connection(review,review.token)
    current=read(root/'tag_catalog_snapshot.json')
    assert current.generation>previous.generation
    assert current.snapshot.source.revision!=previous.snapshot.source.revision
    assert result.choice.transactions.session.binding.generation==current.generation
    assert result.choice.transactions.session.binding.snapshot_fingerprint==current.snapshot.fingerprint
    assert (root/'data/user/tags.json').read_bytes()==before
    assert TagDatabase(root/'data/user/tags.json').find('local_correction').id==record.id


def test_empty_existing_root_and_unknown_settings_survive_stale_ordinary_save(selected):
    settings, _source, root = selected
    settings.write_text(json.dumps({"future_manager_setting": {"v": 7}}), encoding="utf-8")
    stale = AppSettings.load(settings)
    root.mkdir()
    choice = connect(selected)
    saved = json.loads(settings.read_bytes())
    assert saved["future_manager_setting"] == {"v": 7}
    stale.save(settings)
    assert json.loads(settings.read_bytes())["tag_database_connection"] == saved["tag_database_connection"]
    assert connection.open_saved_connection(settings).choice.native_adapter.catalog_id == choice.native_adapter.catalog_id


@pytest.mark.parametrize("drift", ["root", "settings", "source", "token_fields"])
def test_stale_review_holds_before_initialization(selected, drift):
    settings, source, root = selected
    review = connection.prepare_connection(*selected, True)
    if drift == "root":
        root.mkdir()
        (root / "keep.txt").write_text("keep")
    elif drift == "settings":
        settings.write_text('{"other":true}')
    elif drift == "source":
        (source / "TagDatabase.zip").write_bytes(b"changed")
    else:
        review = replace(review, catalog_root=root.parent / "other")
    with pytest.raises((ValueError, RuntimeError)):
        connection.confirm_connection(review, review.token)
    assert not (root / "data").exists()
    if drift == "token_fields":
        assert not review.catalog_root.exists()


@pytest.mark.parametrize("drift", ["identity", "source"])
def test_saved_pins_hold_on_changed_source_or_identity(selected, drift):
    settings, source, root = selected
    connect(selected)
    if drift == "source":
        (source / "TagDatabase.zip").write_bytes(b"changed")
    else:
        from suite_core.catalog_identity_candidate import decode_identity, encode_identity
        from uuid import uuid4
        path = root / "data/user/catalog_identity.json"
        identity = decode_identity(path.read_bytes())
        path.write_bytes(encode_identity(replace(identity, incarnation_id=str(uuid4()))))
    before = settings.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        connection.open_saved_connection(settings)
    assert settings.read_bytes() == before


def test_existing_review_pins_default_membership(selected):
    settings, source, root = selected
    connect(selected)
    review = connection.prepare_connection(settings, source, root, False)
    (root / "data/defaults/future.json").write_text('{}')
    with pytest.raises(connection.TagConnectionHold, match="changed"):
        connection.confirm_connection(review, review.token)


def test_settings_failure_retains_enrolled_catalog_for_explicit_reconnect(selected, monkeypatch):
    settings, source, root = selected
    review = connection.prepare_connection(*selected, True)
    original = AppSettings._save_locked
    def fail(*args, **kwargs):
        raise OSError("injected settings durability failure")
    monkeypatch.setattr(AppSettings, "_save_locked", fail)
    with pytest.raises(connection.TagConnectionHold, match="Catalog data is retained"):
        connection.confirm_connection(review, review.token)
    assert (root / "data/user/catalog_identity.json").is_file()
    assert (root / "tag_catalog_snapshot.json").is_file()
    assert not settings.exists()
    monkeypatch.setattr(AppSettings, "_save_locked", original)
    reconnect = connection.prepare_connection(settings, source, root, False)
    connection.confirm_connection(reconnect, reconnect.token)
    assert connection.open_saved_connection(settings).choice.native_adapter.root == root


def test_legacy_nonempty_root_is_held_without_migration(selected):
    settings, source, root = selected
    (root / "data/user").mkdir(parents=True)
    path = root / "data/user/tags.json"
    path.write_text('{"tags":[],"unknown":"keep"}')
    before = path.read_bytes()
    for create in (True, False):
        with pytest.raises((ValueError, RuntimeError)):
            connection.prepare_connection(settings, source, root, create)
    assert path.read_bytes() == before and not settings.exists()


def preview(choice):
    from suite_core.database_actions import DatabaseAction
    return choice.transactions.preview(DatabaseAction.INSTALL_DEFAULT,
        base_release=choice.base_release, available_release=choice.available_release,
        pack=choice.pack, installed=choice.installed)


def test_connected_import_restart_selected_recovery_and_repeat_noop(selected, monkeypatch):
    settings, _source, root = selected
    choice = connect(selected)
    before = {p.relative_to(root): p.read_bytes() for p in (root / "data").rglob("*.json")}
    review = preview(choice)
    assert review.can_apply, review.holds
    atomic = connection.native._atomic_json
    def interrupt(path, value):
        if path.name == "recovery.json" and value.get("status") == "complete":
            raise SystemExit("all participants and release tracking written")
        return atomic(path, value)
    monkeypatch.setattr(connection.native, "_atomic_json", interrupt)
    with pytest.raises(SystemExit):
        choice.transactions.apply(review, confirmed_token=review.token)
    monkeypatch.setattr(connection.native, "_atomic_json", atomic)
    reopened = connection.open_saved_connection(settings).choice
    assert reopened.observation_status == "recovery_pending"
    assert len(reopened.recovery_receipts) == len(reopened.receipt_stamps) == 1
    reopened.transactions.recover(reopened.recovery_receipts[0])
    assert before == {p.relative_to(root): p.read_bytes() for p in (root / "data").rglob("*.json")}
    reopened = connection.open_saved_connection(settings).choice
    review = preview(reopened)
    assert review.can_apply, review.holds
    reopened.transactions.apply(review, confirmed_token=review.token)
    installed = connection.open_saved_connection(settings).choice
    assert installed.installed and installed.base_release == installed.available_release
    assert json.loads((root / "data/user/tags.json").read_bytes())["tags"] == []
    receipts = tuple((root / "backups/knowledge-pack-import").glob("*/recovery.json"))
    repeat = installed.native_adapter.plan_default_release(installed.pack, installed.base_release,
        installed.available_release, True)
    result = installed.native_adapter.apply(installed.pack, repeat)
    assert result["backup"]["path"] == ""
    assert receipts == tuple((root / "backups/knowledge-pack-import").glob("*/recovery.json"))


def test_future_connection_schema_holds_before_creation(selected):
    settings, _source, root = selected
    settings.write_text('{"tag_database_connection":{"schema_version":2,"opaque":true}}')
    before = settings.read_bytes()
    with pytest.raises(connection.TagConnectionHold, match="schema"):
        connection.prepare_connection(*selected, True)
    assert settings.read_bytes() == before and not root.exists()


def test_profile_program_overlap_and_redirected_root_hold(selected, tmp_path):
    from pathlib import Path
    from core.paths import PROGRAM_ROOT
    settings, source, root = selected
    with pytest.raises((ValueError, RuntimeError), match="separate"):
        connection.prepare_connection(settings, source, Path(PROGRAM_ROOT), True)
    other = tmp_path / "redirect-target"
    other.mkdir()
    try:
        root.symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("Windows developer symlink permission unavailable")
    with pytest.raises(RuntimeError, match="redirect"):
        connection.prepare_connection(*selected, True)
    assert list(other.iterdir()) == [] and not settings.exists()


def test_settings_race_after_initialization_retains_data_and_other_owner(selected, monkeypatch):
    settings, _source, root = selected
    review = connection.prepare_connection(*selected, True)
    initialize = connection._initialize
    def race(*args):
        identity = initialize(*args)
        settings.write_text('{"other_owner":{"keep":true}}')
        return identity
    monkeypatch.setattr(connection, "_initialize", race)
    with pytest.raises(connection.TagConnectionHold, match="Catalog data is retained"):
        connection.confirm_connection(review, review.token)
    assert json.loads(settings.read_bytes()) == {"other_owner": {"keep": True}}
    assert (root / "data/user/catalog_identity.json").is_file()


def test_interruption_after_catalog_publication_requires_explicit_reconnect(selected, monkeypatch):
    settings, source, root = selected
    review = connection.prepare_connection(*selected, True)
    choose = connection._choice
    def interrupt(*args, **kwargs):
        raise SystemExit("catalog published before connection persistence")
    monkeypatch.setattr(connection, "_choice", interrupt)
    with pytest.raises(SystemExit):
        connection.confirm_connection(review, review.token)
    assert not settings.exists() and (root / "data/user/catalog_identity.json").is_file()
    monkeypatch.setattr(connection, "_choice", choose)
    resumed = connection.prepare_connection(settings, source, root, False)
    result = connection.confirm_connection(resumed, resumed.token)
    assert connection.open_saved_connection(settings).catalog_identity == result.catalog_identity


def test_actual_settings_readback_failure_restores_exact_settings_and_keeps_catalog(selected, monkeypatch):
    from pathlib import Path
    settings, _source, root = selected
    settings.write_text('{"unknown":{"keep":[1,2]}}')
    before = settings.read_bytes()
    review = connection.prepare_connection(*selected, True)
    read_text = Path.read_text
    fired = False
    def fail(path, *args, **kwargs):
        nonlocal fired
        if path == settings and not fired:
            fired = True
            raise OSError("injected active-settings readback failure")
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(connection.TagConnectionHold, match="Catalog data is retained"):
        connection.confirm_connection(review, review.token)
    assert fired and settings.read_bytes() == before
    assert (root / "data/user/catalog_identity.json").is_file()
    assert any(p.read_bytes() == before for p in (settings.parent / "settings-history").glob("*.json"))


@pytest.mark.parametrize("fields", [{}, {"source_directory": 42}])
def test_incomplete_saved_connection_is_an_actionable_readonly_hold(selected, fields):
    settings, _source, root = selected
    settings.write_text(json.dumps({"tag_database_connection": {"schema_version": 1,
        "component": "TagDatabase", **fields}}))
    before = settings.read_bytes()
    with pytest.raises(connection.TagConnectionHold, match="choose and review"):
        connection.open_saved_connection(settings)
    assert settings.read_bytes() == before and not root.exists()


def test_source_replacement_after_fresh_validation_keeps_original_review_pin(selected, monkeypatch):
    settings, source, root = selected
    review = connection.prepare_connection(*selected, True)
    prepare = connection.prepare_connection
    inventory = source / "BOOTSTRAP_INVENTORY.json"
    replacement = inventory.read_bytes() + b"\n"
    def replace_after_fresh(*args, **kwargs):
        fresh = prepare(*args, **kwargs)
        assert fresh == review
        # Another valid immutable inventory observation, not malformed input.
        inventory.write_bytes(replacement)
        return fresh
    monkeypatch.setattr(connection, "prepare_connection", replace_after_fresh)
    with pytest.raises(connection.TagConnectionHold, match="Source changed after review"):
        connection.confirm_connection(review, review.token)
    assert connection._source(source)[2] != review.source_pin
    assert inventory.read_bytes() == replacement
    assert not settings.exists() and not root.exists()
    assert not settings.with_name(settings.name + ".lock").exists()


@pytest.mark.parametrize("member", ["ManagerAppSettings.json", "metadata/TagDatabase.local-qualification.json"])
def test_settings_inside_source_hold_before_reads_or_initialization(selected, monkeypatch, member):
    _settings, source, root = selected
    settings = source / member
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    root.mkdir()
    def forbidden(*args, **kwargs):
        pytest.fail("Overlap must hold before settings reads or catalog initialization")
    monkeypatch.setattr(connection, "_settings", forbidden)
    monkeypatch.setattr(connection, "_initialize", forbidden)
    with pytest.raises(connection.TagConnectionHold, match="separate"):
        connection.prepare_connection(settings, source, root, True)
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert list(root.iterdir()) == []


def _reload_corrected(selected):
    connect(selected)
    settings, _source, root = selected
    destination = root / "tag_catalog_snapshot.json"
    previous = read(destination)
    categories = CategoryRegistry(root / "data/defaults/categories.json",
                                  root / "data/user/categories.json")
    category = categories.add("Synthetic reload correction")
    categories.save_user()
    tags = TagDatabase(root / "data/user/tags.json")
    tags.add_known_tag("synthetic_reload_correction", category=category.id,
                       properties={"note": "preserve"})
    tags.save_verified()
    protected = {path: path.read_bytes() for path in
                 (root / "data/user/tags.json", root / "data/user/categories.json")}
    service = connection.open_saved_connection(settings).choice.transactions
    return service, destination, previous, protected


def _assert_reload_coherent(service, publication):
    assert service.session.binding.generation == publication.generation
    assert service.session.binding.authority_revision == publication.snapshot.source.revision
    assert service.session.binding.snapshot_fingerprint == publication.snapshot.fingerprint


def test_saved_reload_publishes_new_authority_and_preserves_user_bytes(selected):
    service, destination, previous, protected = _reload_corrected(selected)
    # Reopen retains the published baseline until the explicit reload succeeds.
    _assert_reload_coherent(service, previous)
    assert read(destination) == previous
    published = service.reload()
    assert published.generation == previous.generation + 1
    assert published.snapshot.source.revision != previous.snapshot.source.revision
    assert published == read(destination)
    _assert_reload_coherent(service, published)
    binding = service.session.binding
    assert service.reload() == published
    assert service.session.binding == binding  # unchanged reload is idempotent
    assert all(path.read_bytes() == raw for path, raw in protected.items())


@pytest.mark.parametrize("failure", ["persistence", "readback"])
def test_failed_reload_retains_binding_and_can_retry_without_native_replay(selected, monkeypatch, failure):
    service, destination, previous, protected = _reload_corrected(selected)
    binding = service.session.binding
    original_publish, original_read = transactions.publish, transactions.read
    wrote = False

    def publish(*args, **kwargs):
        nonlocal wrote
        if failure == "persistence":
            raise OSError("injected persistence failure")
        result = original_publish(*args, **kwargs)
        wrote = True
        return result

    def readback(*args, **kwargs):
        if wrote:
            raise OSError("injected readback failure")
        return original_read(*args, **kwargs)

    monkeypatch.setattr(transactions, "publish", publish)
    monkeypatch.setattr(transactions, "read", readback)
    with pytest.raises(OSError, match="injected"):
        service.reload()
    assert service.session.binding == binding
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    assert (read(destination) == previous) == (failure == "persistence")
    monkeypatch.setattr(transactions, "publish", original_publish)
    monkeypatch.setattr(transactions, "read", original_read)
    published = service.reload()
    assert published.generation == previous.generation + 1
    _assert_reload_coherent(service, published)
    assert published == read(destination)
    assert all(path.read_bytes() == raw for path, raw in protected.items())


@pytest.mark.parametrize("race", ["native_revision", "publication"])
def test_reload_rejects_concurrent_stale_authority_and_retries_fresh(selected, monkeypatch, race):
    service, destination, previous, protected = _reload_corrected(selected)
    binding = service.session.binding
    original_publish = transactions.publish
    competing = None

    def publish(*args, **kwargs):
        nonlocal competing, protected
        if race == "native_revision":
            tags_path = destination.parent / "data/user/tags.json"
            tags = TagDatabase(tags_path)
            tags.add_known_tag("later_correction", category=tags.find("synthetic_reload_correction").category)
            tags.save_verified()
            protected = {path: path.read_bytes() for path in protected}
        else:
            # Separate legitimate publisher under the same canonical guard.
            other = connection.open_saved_connection(selected[0]).choice.transactions
            monkeypatch.setattr(transactions, "publish", original_publish)
            competing = other.reload()
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(transactions, "publish", publish)
    message = "Native revision changed" if race == "native_revision" else "Publication changed"
    with pytest.raises(StalePublication, match=message):
        service.reload()
    assert service.session.binding == binding
    assert read(destination) == (previous if competing is None else competing)
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    monkeypatch.setattr(transactions, "publish", original_publish)
    published = service.reload()
    _assert_reload_coherent(service, published)
    assert published == read(destination)
    assert published.generation == previous.generation + 1
    assert all(path.read_bytes() == raw for path, raw in protected.items())
