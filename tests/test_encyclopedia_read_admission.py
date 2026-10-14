"""Candidate source route proof on disposable enrolled native catalogs only."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import threading
from uuid import uuid4

import pytest

from core import encyclopedia_read_admission as admission
from core import tag_catalog_connection as connection
from core.catalog_identity_codec import CatalogIdentity, encode_identity
from core.encyclopedia_catalog import EncyclopediaCatalog, EncyclopediaCatalogView
from test_tag_catalog_connection import selected, connect
from test_tag_database_pack_producer import reviewed_local_input


def evidence():
    # Independent fixture producer pins exact sources, not a completed route receipt.
    repo = Path(__file__).resolve().parent.parent
    return admission.CandidateReadEvidence(tuple((name, sha256((repo / name).read_bytes()).hexdigest())
        for name in admission.SOURCE_PATHS))


def minimal(tmp_path):
    root = tmp_path / "selected"
    (root / "data/user").mkdir(parents=True)
    (root / "data/defaults").mkdir()
    (root / "data/user/catalog_identity.json").write_bytes(
        encode_identity(CatalogIdentity(str(uuid4()), str(uuid4()))))
    return root


def contents(root):
    # Lock bookkeeping is the sole declared filesystem-write exception.
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
        if p.is_file() and p.name != ".knowledge-write.lock"}


@pytest.mark.parametrize("payload", [
    {"schema_version": 6, "retained_default_compatibility": {"records": ["opaque"]}},
    {"schema_version": 6, "required_read_semantics": ["future"]},
    {"schema_version": 99}, ["ambiguous"],
    '{"schema_version":6,"schema_version":6}',
    {"schema_version": 6, "records": {"tags": {"required_read_semantics": ["future"]}}},
    {"schema_version": 6, "records": ["ambiguous carrier"]},
])
def test_unknown_metadata_holds_before_stamp_adapter_or_lock(tmp_path, monkeypatch, payload):
    root = minimal(tmp_path)
    path = root / "data/user/default_knowledge_state.json"
    path.write_text(payload if type(payload) is str else json.dumps(payload), encoding="utf-8")
    before = contents(root)
    monkeypatch.setattr(connection, "_catalog_stamp", lambda *a: pytest.fail("record hashing before semantic admission"))
    import core.encyclopedia_catalog as service_module
    monkeypatch.setattr(service_module, "NativeTagCatalogAdapter", lambda *a, **k: pytest.fail("adapter before semantic admission"))
    with pytest.raises(connection.TagConnectionHold):
        EncyclopediaCatalog(tmp_path / "settings.json").open_catalog(root, read_candidate=evidence())
    assert contents(root) == before and not (root / "data/user/.knowledge-write.lock").exists()


def test_wrong_route_source_and_mutable_evidence_hold_before_stamp(tmp_path, monkeypatch):
    root = minimal(tmp_path)
    monkeypatch.setattr(connection, "_catalog_stamp", lambda *a: pytest.fail("unqualified source read"))
    good = evidence()
    rows = list(good.source_pins); rows[0] = (rows[0][0], "0" * 64)
    for value in (admission.CandidateReadEvidence(tuple(rows)), {"supports_read": True}):
        with pytest.raises(connection.TagConnectionHold):
            EncyclopediaCatalog(tmp_path / "settings.json").open_catalog(root, read_candidate=value)
    with pytest.raises(connection.TagConnectionHold):
        replace(good, entry="other-client")
    with pytest.raises(connection.TagConnectionHold):
        replace(good, source_pins=list(good.source_pins))


@pytest.mark.parametrize("missing", ["data/user", "data/defaults", "header", "root"])
def test_missing_selected_authority_never_initializes(tmp_path, missing):
    root = minimal(tmp_path)
    if missing == "header":
        (root / "data/user/catalog_identity.json").unlink()
    else:
        shutil.rmtree(root if missing == "root" else root / missing)
    before = contents(root) if root.exists() else {}
    with pytest.raises((connection.TagConnectionHold, OSError, ValueError)):
        EncyclopediaCatalog(tmp_path / "settings.json").open_catalog(root, read_candidate=evidence())
    assert (contents(root) if root.exists() else {}) == before
    assert not (root / "data/user/.knowledge-write.lock").exists()


def test_native_capture_refresh_preserves_content_and_detaches_writer(selected, monkeypatch):
    from core.category_registry import CategoryRegistry
    from core.tag_database import TagDatabase
    settings, _source, root = selected
    choice = connect(selected)
    with choice.native_adapter.guard():
        categories = CategoryRegistry(root / "data/defaults/categories.json", root / "data/user/categories.json")
        category = categories.add("Synthetic category"); categories.save_user()
        tags = TagDatabase(root / "data/user/tags.json")
        tag = tags.add_known_tag("Synthetic personal tag", category=category.id)
        tag.notes = "Preserve local note"; tag.extras["opaque_future"] = {"keep": [1, 2]}
        tags.save_verified()
    before = contents(root); settings_before = settings.read_bytes()
    service = EncyclopediaCatalog(settings)
    accepted = service.open_catalog(root)
    original_stamp = connection._catalog_stamp
    seen = []
    original_observe = admission.observe_selected_read
    def observe(*a, **k):
        seen.append("metadata")
        return original_observe(*a, **k)
    def stamp(*a):
        assert seen.count("metadata") >= 2
        return original_stamp(*a)
    monkeypatch.setattr(admission, "observe_selected_read", observe)
    monkeypatch.setattr(connection, "_catalog_stamp", stamp)
    view = service.open_catalog(root, read_candidate=evidence())
    assert view.adapter is None and view.read_only and view.candidate_only
    assert service.view is accepted  # No accepted production state transition.
    assert view.publication.snapshot.tags == accepted.publication.snapshot.tags
    assert view.publication.snapshot.categories == accepted.publication.snapshot.categories
    assert view.publication.snapshot.lookups == accepted.publication.snapshot.lookups
    assert any(t.display == "Synthetic personal tag" for t in view.publication.snapshot.tags)
    assert service.revalidate_read_view(view) is view
    refreshed = service.open_catalog(root, read_candidate=evidence())
    assert refreshed.publication == view.publication
    assert contents(root) == before and settings.read_bytes() == settings_before
    with pytest.raises(connection.TagConnectionHold):
        replace(view, adapter=choice.native_adapter)
    with pytest.raises(connection.TagConnectionHold):
        replace(view, candidate_only=False)
    with pytest.raises(connection.TagConnectionHold):
        replace(view, read_binding={"supports_read": True})
    with pytest.raises(connection.TagConnectionHold):
        service.validate_read_delivery(view)  # Superseded exact result.
    with pytest.raises(InterruptedError):
        service.open_catalog(root, read_candidate=evidence(), should_cancel=lambda: True)
    assert service.view is accepted and service._candidate_view is refreshed


def test_deferred_drift_and_replaced_root_hold_previous_view(selected):
    settings, _source, root = selected; connect(selected)
    service = EncyclopediaCatalog(settings)
    view = service.open_catalog(root, read_candidate=evidence())
    state = root / "data/user/default_knowledge_state.json"
    state.write_text('{"schema_version":6,"required_read_semantics":["future"]}')
    with pytest.raises(connection.TagConnectionHold):
        service.revalidate_read_view(view)
    assert service._candidate_view is view and service.view is None
    state.unlink()
    old = root.with_name(root.name + "-previous"); root.rename(old); shutil.copytree(old, root)
    with pytest.raises(connection.TagConnectionHold):
        service.open_catalog(root, read_candidate=evidence())
    assert service._candidate_view is view


def test_preflight_repeated_inside_existing_exclusion_and_cancel_at_capture(selected, monkeypatch):
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    settings, _source, root = selected; connect(selected)
    service = EncyclopediaCatalog(settings)
    first = service.open_catalog(root, read_candidate=evidence())
    original = NativeTagCatalogAdapter.capture
    cancelled = [False]
    def capture(adapter, generation):
        result = original(adapter, generation)
        cancelled[0] = True
        return result
    monkeypatch.setattr(NativeTagCatalogAdapter, "capture", capture)
    before = contents(root)
    with pytest.raises(InterruptedError):
        service.open_catalog(root, read_candidate=evidence(), should_cancel=lambda: cancelled[0])
    assert service._candidate_view is first and contents(root) == before


def test_existing_writer_exclusion_blocks_capture(selected, monkeypatch):
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    settings, _source, root = selected; connect(selected)
    service = EncyclopediaCatalog(settings)
    preflight = threading.Event(); captured = threading.Event(); finished = threading.Event()
    errors = []
    original_observe = admission.observe_selected_read
    original_capture = NativeTagCatalogAdapter.capture
    def observe(*a, **k):
        result = original_observe(*a, **k); preflight.set(); return result
    def capture(*a, **k):
        captured.set(); return original_capture(*a, **k)
    monkeypatch.setattr(admission, "observe_selected_read", observe)
    monkeypatch.setattr(NativeTagCatalogAdapter, "capture", capture)
    def reader():
        try: service.open_catalog(root, read_candidate=evidence())
        except BaseException as exc: errors.append(exc)
        finally: finished.set()
    with connection.native.RulePairAdmission(root / "data/user").admit():
        worker = threading.Thread(target=reader); worker.start()
        assert preflight.wait(5)
        assert not captured.wait(0.15)
    worker.join(15)
    assert finished.is_set() and not errors and captured.is_set()


def test_pending_native_namespace_holds_before_record_stamp(tmp_path, monkeypatch):
    root = minimal(tmp_path)
    guard = connection.native.RulePairAdmission(root / "data/user")
    guard.pending_root.mkdir()
    (guard.pending_root / "unresolved.json").write_text('{"opaque":"pending evidence"}')
    before = contents(root)
    monkeypatch.setattr(connection, "_catalog_stamp", lambda *a: pytest.fail("pending work reached record reads"))
    with pytest.raises(RuntimeError):
        EncyclopediaCatalog(tmp_path / "settings.json").open_catalog(root, read_candidate=evidence())
    assert contents(root) == before


def test_changed_metadata_between_preflight_and_exclusion_holds_before_stamp(tmp_path, monkeypatch):
    root = minimal(tmp_path)
    original = admission.observe_selected_read
    calls = []
    def observe(*a, **k):
        result = original(*a, **k)
        calls.append(result)
        if len(calls) == 1:
            path = root / "data/user/catalog_identity.json"
            path.write_bytes(encode_identity(replace(result.identity, incarnation_id=str(uuid4()))))
        return result
    monkeypatch.setattr(admission, "observe_selected_read", observe)
    monkeypatch.setattr(connection, "_catalog_stamp", lambda *a: pytest.fail("metadata drift reached record reads"))
    with pytest.raises(connection.TagConnectionHold, match="metadata changed"):
        EncyclopediaCatalog(tmp_path / "settings.json").open_catalog(root, read_candidate=evidence())
    assert len(calls) == 2


def test_actual_installed_foundation_effective_read_preserves_entire_content(selected):
    settings, source, root = selected
    service = EncyclopediaCatalog(settings)
    review = service.review_source(source, root, create_new=True)
    service.confirm_source(review, confirmed_token=review.token)
    preview = service.preview_import()
    applied = service.apply(preview, confirmed_token=preview.token)
    assert applied.publication is not None
    before = contents(root)
    state = json.loads((root / "data/user/default_knowledge_state.json").read_bytes())
    assert state["schema_version"] == 6 and "approved_pack_releases" in state
    ordinary = service.open_catalog(root)
    candidate = service.open_catalog(root, read_candidate=evidence())
    assert candidate.publication.snapshot.tags == ordinary.publication.snapshot.tags
    assert candidate.publication.snapshot.relationships == ordinary.publication.snapshot.relationships
    assert candidate.publication.snapshot.tags
    assert contents(root) == before
    assert service.view is ordinary


@pytest.mark.parametrize("boundary", ["after_preflight", "before_directory_handle", "during_directory_handle"])
@pytest.mark.parametrize("replace_directory", [False, True])
def test_acquisition_directory_race_never_bootstraps_or_writes_replacement(
        tmp_path, monkeypatch, boundary, replace_directory):
    from core import history_versions as history
    root = minimal(tmp_path); user = root / "data/user"
    retained = root / "data/retained-user"
    before = contents(user)
    moved = []
    def move():
        assert user.is_relative_to(tmp_path) and retained.is_relative_to(tmp_path)
        user.rename(retained)
        if replace_directory:
            user.mkdir()
            (user / "synthetic-replacement.txt").write_text("Keep replacement unchanged")
        moved.append(True)
    if boundary == "after_preflight":
        original = admission.observe_selected_read
        def observe(*a, **k):
            result = original(*a, **k)
            if not moved: move()
            return result
        monkeypatch.setattr(admission, "observe_selected_read", observe)
    elif boundary == "before_directory_handle":
        original = history._existing_lock_directory
        def directory(*a, **k):
            if not moved: move()
            return original(*a, **k)
        monkeypatch.setattr(history, "_existing_lock_directory", directory)
    else:
        original = history._existing_directory_pin
        def directory_pin(*a, **k):
            pin = original(*a, **k)
            if not moved: move()
            return pin
        monkeypatch.setattr(history, "_existing_directory_pin", directory_pin)
    monkeypatch.setattr(connection, "_catalog_stamp", lambda *a: pytest.fail("changed directory reached record reads"))
    with pytest.raises((RuntimeError, OSError)):
        EncyclopediaCatalog(tmp_path / "settings.json").open_catalog(root, read_candidate=evidence())
    assert moved and contents(retained) == before
    if replace_directory:
        assert contents(user) == {"synthetic-replacement.txt": b"Keep replacement unchanged"}
        assert list(user.iterdir()) == [user / "synthetic-replacement.txt"]
    else:
        assert not user.exists()


def test_existing_directory_lock_initialization_nested_inheritance_and_legacy_default(tmp_path, monkeypatch):
    from core import history_versions as history
    from core.knowledge_write_admission import KnowledgeWriteAdmission, existing_directory_pin
    user = tmp_path / "existing"; user.mkdir()
    info = user.stat(); pin = info.st_dev, info.st_ino
    original = Path.mkdir
    def no_directory_bootstrap(path, *a, **k):
        if path == user: pytest.fail("nested strict acquisition attempted mkdir")
        return original(path, *a, **k)
    monkeypatch.setattr(Path, "mkdir", no_directory_bootstrap)
    owner = connection.native.RulePairAdmission(user)
    with owner.admit(existing_directory=True, expected_directory=pin):
        with owner.admit():
            with KnowledgeWriteAdmission(user).locked():
                assert existing_directory_pin(user) == pin
    assert (user / ".knowledge-write.lock").read_bytes() == b"\0"
    assert existing_directory_pin(user) is None
    with history.history_lock(user / ".knowledge-write.lock", existing_parent=True):
        pass
    legacy = tmp_path / "legacy-new"
    with connection.native.RulePairAdmission(legacy).admit():
        assert legacy.is_dir()
    assert (legacy / ".knowledge-write.lock").read_bytes() == b"\0"


def test_nested_acquisition_disappearance_cannot_recreate_authority(tmp_path, monkeypatch):
    import os
    from core.knowledge_write_admission import KnowledgeAdmissionError
    user = tmp_path / "existing"; user.mkdir()
    retained = tmp_path / "retained"
    owner = connection.native.RulePairAdmission(user)
    info = user.stat(); pin = info.st_dev, info.st_ino
    with owner.admit(existing_directory=True, expected_directory=pin):
        original = Path.mkdir
        def no_bootstrap(path, *a, **k):
            if path == user: pytest.fail("nested admission attempted directory recreation")
            return original(path, *a, **k)
        monkeypatch.setattr(Path, "mkdir", no_bootstrap)
        if os.name == "nt":
            # Actual directory handle denies rename for the entire lock scope.
            with pytest.raises(OSError): user.rename(retained)
            with owner.admit(): pass
            assert user.is_dir() and not retained.exists()
        else:
            user.rename(retained)
            with pytest.raises(KnowledgeAdmissionError):
                with owner.admit(): pass
            assert not user.exists()


def test_deferred_validation_directory_disappearance_holds_without_bootstrap(selected, monkeypatch):
    settings, _source, root = selected; connect(selected)
    service = EncyclopediaCatalog(settings)
    view = service.open_catalog(root, read_candidate=evidence())
    user = root / "data/user"; retained = root / "data/retained-user"
    before = contents(user)
    original = admission.observe_selected_read
    moved = []
    def observe(*a, **k):
        result = original(*a, **k)
        if not moved:
            assert user.is_relative_to(root) and retained.is_relative_to(root)
            user.rename(retained); moved.append(True)
        return result
    monkeypatch.setattr(admission, "observe_selected_read", observe)
    with pytest.raises(RuntimeError): service.revalidate_read_view(view)
    assert not user.exists() and contents(retained) == before
    assert service._candidate_view is view and service.view is None


def synthetic_symlink(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip("Synthetic symlink creation unavailable: " + str(exc))


@pytest.mark.parametrize("dangling", [False, True])
def test_strict_lock_redirect_existing_or_dangling_target_is_untouched(tmp_path, dangling):
    from core.knowledge_write_admission import KnowledgeWriteAdmission
    user = tmp_path / "user"; user.mkdir()
    target = tmp_path / "synthetic-sentinel"
    if not dangling: target.write_bytes(b"")
    lock = user / ".knowledge-write.lock"
    synthetic_symlink(lock, target)
    for strict in (False, True):
        with pytest.raises((RuntimeError, OSError)):
            with KnowledgeWriteAdmission(user).locked(existing_directory=strict):
                pytest.fail("redirected lock admitted")
    assert target.exists() is not dangling
    if not dangling: assert target.read_bytes() == b""
    assert lock.is_symlink()


@pytest.mark.parametrize("existing_lock", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_strict_lock_redirect_substitution_after_observation_does_not_touch_target(
        tmp_path, monkeypatch, existing_lock, dangling):
    from core import history_versions as history
    from core.knowledge_write_admission import KnowledgeWriteAdmission
    user = tmp_path / "user"; user.mkdir()
    lock = user / ".knowledge-write.lock"
    retained = user / "original-lock"
    target = tmp_path / "synthetic-sentinel"
    if existing_lock: lock.write_bytes(b"")
    if not dangling: target.write_bytes(b"")
    original = history._existing_lock_state
    substituted = []
    def state(path):
        result = original(path)
        if not substituted:
            assert lock.is_relative_to(tmp_path) and retained.is_relative_to(tmp_path)
            if existing_lock: lock.rename(retained)
            synthetic_symlink(lock, target)
            substituted.append(True)
        return result
    monkeypatch.setattr(history, "_existing_lock_state", state)
    with pytest.raises((RuntimeError, OSError)):
        with KnowledgeWriteAdmission(user).locked(existing_directory=True):
            pytest.fail("substituted lock admitted")
    assert substituted and lock.is_symlink() and target.exists() is not dangling
    if not dangling: assert target.read_bytes() == b""
    if existing_lock: assert retained.read_bytes() == b""


def test_strict_lock_regular_substitution_holds_before_initialization(tmp_path, monkeypatch):
    from core import history_versions as history
    from core.knowledge_write_admission import KnowledgeWriteAdmission
    user = tmp_path / "user"; user.mkdir()
    lock = user / ".knowledge-write.lock"; lock.write_bytes(b"")
    retained = user / "original-lock"
    replacement = user / "synthetic-replacement"; replacement.write_bytes(b"")
    original = history._existing_lock_state
    substituted = []
    def state(path):
        result = original(path)
        if not substituted:
            assert path.is_relative_to(tmp_path)
            lock.rename(retained); replacement.rename(lock); substituted.append(True)
        return result
    monkeypatch.setattr(history, "_existing_lock_state", state)
    with pytest.raises(RuntimeError):
        with KnowledgeWriteAdmission(user).locked(existing_directory=True): pass
    assert lock.read_bytes() == retained.read_bytes() == b""


def test_strict_lock_substitution_after_open_cannot_redirect_initialization(tmp_path, monkeypatch):
    import os
    from core import history_versions as history
    from core.knowledge_write_admission import KnowledgeWriteAdmission
    user = tmp_path / "user"; user.mkdir()
    lock = user / ".knowledge-write.lock"; lock.write_bytes(b"")
    retained = user / "original-lock"
    target = tmp_path / "synthetic-sentinel"; target.write_bytes(b"")
    original = history._existing_lock_state
    calls = []
    def state(path):
        calls.append(path)
        if len(calls) == 2:
            assert lock.is_relative_to(tmp_path) and retained.is_relative_to(tmp_path)
            if os.name == "nt":
                with pytest.raises(OSError): lock.rename(retained)
            else:
                lock.rename(retained); synthetic_symlink(lock, target)
        return original(path)
    monkeypatch.setattr(history, "_existing_lock_state", state)
    if os.name == "nt":
        with KnowledgeWriteAdmission(user).locked(existing_directory=True): pass
        assert lock.read_bytes() == b"\0" and not retained.exists()
    else:
        with pytest.raises(RuntimeError):
            with KnowledgeWriteAdmission(user).locked(existing_directory=True): pass
        assert retained.read_bytes() == b""
    assert len(calls) >= 2 and target.read_bytes() == b""
