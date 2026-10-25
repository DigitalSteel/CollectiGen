"""Synthetic, non-Qt contract checks using the actual native evidence-query leaf."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from suite_core.media_examples import (
    CatalogMediaExampleProvider, ExampleIdentity, ExampleRequest,
    ExampleStatus, FrozenEvidenceRecord, FrozenEvidenceSnapshot, MediaReference,
)


@pytest.fixture
def query_service():
    # Load just these two pure source leaves under a detached namespace. Never
    # import core/__init__, AppState, native stores, Qt, or any catalog fixture.
    package = "_synthetic_media_query_leaf"
    root = Path(__file__).resolve().parents[2] / "core"
    module = ModuleType(package)
    module.__path__ = [str(root)]
    sys.modules[package] = module
    try:
        for leaf in ("tag_collection", "evidence_query"):
            spec = importlib.util.spec_from_file_location(f"{package}.{leaf}", root / f"{leaf}.py")
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = loaded
            spec.loader.exec_module(loaded)
        yield sys.modules[f"{package}.evidence_query"].EvidenceQueryService()
    finally:
        for name in (package, f"{package}.tag_collection", f"{package}.evidence_query"):
            sys.modules.pop(name, None)


class SyntheticIndex:
    def __init__(self, library_id, rows):
        self.library_id = library_id
        self.rows = [SimpleNamespace(path=f"/synthetic/{library_id}/{path}", tags=tags)
                     for path, tags in rows]
        self.revision = 1
        self.reads = 0

    def evidence_snapshot_token(self):
        return (self.library_id, self.revision)

    def all_records(self):
        self.reads += 1
        return self.rows


@pytest.fixture
def host(query_service):
    indexes = {
        "a": SyntheticIndex("a", [("fox.png", ["RED_FOX"]),
                                    ("vulpes.png", ["Vulpes", "Trees"])]),
        "b": SyntheticIndex("b", [("fox.png", ["red fox"])]),
    }
    state = SimpleNamespace(selection="selected-1", knowledge="knowledge-1", scope=("a", "b"))

    class Dataset:
        @property
        def scope_library_ids(self):
            return state.scope

        def scoped_profile_managers(self):
            return [(SimpleNamespace(stable_id=key), SimpleNamespace(index=indexes[key]))
                    for key in state.scope]

        def identity_for_path(self, path):
            _, _, library_id, relative = str(path).split("/", 3)
            return SimpleNamespace(library_id=library_id, relative_path=relative)

    provider = CatalogMediaExampleProvider(
        Dataset(), query_service,
        read_snapshot=lambda key, index: FrozenEvidenceSnapshot(
            str(index.revision), tuple(FrozenEvidenceRecord(row.path, tuple(row.tags))
                                       for row in index.rows)),
        current_selection=lambda: state.selection,
        current_knowledge_revision=lambda: state.knowledge,
    )
    request = ExampleRequest(state.selection, state.knowledge, ("a", "b"),
                             (ExampleIdentity("fox-id", ("Red Fox", "Vulpes"), "animals"),))
    return provider, request, state, indexes


def test_pure_import_starts_no_application_or_qt():
    code = ("import sys; import suite_core.media_examples; "
            "assert 'core' not in sys.modules; "
            "assert not any(x.startswith(('PySide6', 'qt_app')) for x in sys.modules)")
    subprocess.run([sys.executable, "-B", "-c", code], check=True,
                   cwd=Path(__file__).resolve().parents[2])


def test_success_alias_normalization_identity_and_cache(host):
    provider, request, _, indexes = host
    result = provider.query(request)
    assert result.status == ExampleStatus.SUCCESS
    assert result.references == (MediaReference("a", "fox.png"),
                                 MediaReference("a", "vulpes.png"), MediaReference("b", "fox.png"))
    assert "/synthetic" not in repr(result)
    assert provider.accepts(result)
    assert provider.query(request) == result
    assert [index.reads for index in indexes.values()] == [0, 0]


def test_exact_subset_never_queries_other_library(host):
    provider, request, _, indexes = host
    result = provider.query(replace(request, library_ids=("a",)))
    assert {ref.library_id for ref in result.references} == {"a"}
    assert indexes["b"].reads == 0


def test_native_service_memory_scan_cache_and_revision_invalidation(host):
    provider, request, _, indexes = host
    seen = []

    from suite_core.media_examples import _MemoryIndex
    original = _MemoryIndex.all_records

    def memory_rows(view):
        seen.append(view.token)
        return original(view)

    # Native SQLite entry points must never be reached by this provider.
    def forbidden(*args, **kwargs):
        raise AssertionError("Native index access is outside the pure reader")

    indexes["a"].query_evidence_groups = forbidden
    indexes["a"].evidence_snapshot_token = forbidden
    indexes["a"].all_records = forbidden
    request = replace(request, library_ids=("a",))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_MemoryIndex, "all_records", memory_rows)
        assert len(provider.query(request).references) == 2
        provider.query(request)
        assert len(seen) == 1
        indexes["a"].revision += 1
        provider.query(request)
        assert len(seen) == 2


def test_category_union_supplied_descendants_and_selected_tag_intersection(host):
    provider, request, _, _ = host
    identities = (*request.identities,
                  ExampleIdentity("trees-id", ("Trees",), "plants"),
                  ExampleIdentity("other-id", ("other",), "outside"))
    category = ExampleRequest.for_category(request.selection_token, request.knowledge_revision,
                                          request.library_ids, ("animals", "plants"), identities)
    assert category.identities == identities[:2]
    assert not category.match_all
    assert len(provider.query(category).references) == 3
    selected = replace(request, identities=identities[:2])
    assert provider.query(selected).references == (MediaReference("a", "vulpes.png"),)


@pytest.mark.parametrize("identities", [(), (ExampleIdentity("none", ("missing",)),)])
def test_empty_matches(host, identities):
    provider, request, _, _ = host
    result = provider.query(replace(request, identities=identities))
    assert result.status == ExampleStatus.EMPTY
    assert result.references == ()


@pytest.mark.parametrize("names", [("___",), ("_ _",)])
@pytest.mark.parametrize("include_valid_identity", [False, True])
def test_required_normalized_empty_identity_never_weakens_and(host, names, include_valid_identity):
    provider, request, _, _ = host
    empty = ExampleIdentity("empty-id", names)
    identities = (*request.identities, empty) if include_valid_identity else (empty,)
    result = provider.query(replace(request, identities=identities))
    assert result.status == ExampleStatus.EMPTY
    assert result.references == ()
    assert provider.accepts(result)


@pytest.mark.parametrize("names", [("___", "Vulpes"), ("Vulpes", "___")])
def test_empty_alternative_keeps_valid_canonical_or_alias_and_intersection(host, names):
    provider, request, _, _ = host
    valid = ExampleIdentity("fox-id", names)
    trees = ExampleIdentity("trees-id", ("Trees",))
    result = provider.query(replace(request, identities=(valid, trees)))
    assert result.status == ExampleStatus.SUCCESS
    assert result.references == (MediaReference("a", "vulpes.png"),)
    assert provider.accepts(result)


def test_empty_identity_does_not_prevent_other_category_union_matches(host):
    provider, request, _, _ = host
    result = provider.query(replace(request, match_all=False,
                                   identities=(*request.identities, ExampleIdentity("empty-id", ("___",)))))
    assert result.status == ExampleStatus.SUCCESS
    assert len(result.references) == 3


@pytest.mark.parametrize("missing", ["empty_scope", "index"])
def test_unavailable_is_distinct_from_empty(host, missing):
    provider, request, _, indexes = host
    if missing == "index":
        indexes["a"] = None
    else:
        request = replace(request, library_ids=())
    assert provider.query(request).status == ExampleStatus.UNAVAILABLE


@pytest.mark.parametrize("mutation", ["selection", "knowledge", "scope", "revision", "binding"])
def test_mid_query_changes_reject_all_partial_results(host, mutation):
    provider, request, state, indexes = host
    native_query = provider.evidence_queries.query

    def changed(*args, **kwargs):
        paths = native_query(*args, **kwargs)
        if mutation == "selection": state.selection = "selected-2"
        elif mutation == "knowledge": state.knowledge = "knowledge-2"
        elif mutation == "scope": state.scope = ("a",)
        elif mutation == "revision": indexes["a"].revision += 1
        elif mutation == "binding": indexes["a"] = SyntheticIndex("a", [])
        return paths

    provider.evidence_queries.query = changed
    result = provider.query(request)
    assert result.status == ExampleStatus.STALE
    assert result.references == ()


@pytest.mark.parametrize("mutation", ["selection", "knowledge", "scope", "revision", "binding"])
def test_delivery_rechecks_current_authority(host, mutation):
    provider, request, state, indexes = host
    result = provider.query(request)
    if mutation == "selection": state.selection = "selected-2"
    elif mutation == "knowledge": state.knowledge = "knowledge-2"
    elif mutation == "scope": state.scope = ("a",)
    elif mutation == "revision": indexes["a"].revision += 1
    else: indexes["a"] = SyntheticIndex("a", [])
    assert not provider.accepts(result)


def test_cancel_before_work_and_during_work_returns_no_partial_scope(host):
    provider, request, _, indexes = host
    assert provider.query(request, should_continue=lambda: False).status == ExampleStatus.CANCELLED
    assert indexes["a"].reads == 0
    calls = 0

    def continue_twice():
        nonlocal calls
        calls += 1
        return calls <= 3

    result = provider.query(request, should_continue=continue_twice)
    assert result.status == ExampleStatus.CANCELLED
    assert result.references == ()


def test_query_exception_is_non_private_error(host):
    provider, request, _, _ = host

    def fail(*args, **kwargs):
        raise OSError("PRIVATE synthetic cache path")

    provider.evidence_queries.query = fail
    result = provider.query(request)
    assert result.status == ExampleStatus.ERROR
    assert result.references == ()
    assert "PRIVATE" not in repr(result)


def test_foreign_library_result_is_rejected(host):
    provider, request, _, _ = host
    provider.evidence_queries.query = lambda *args, **kwargs: ("/synthetic/foreign/fox.png",)
    assert provider.query(request).status == ExampleStatus.STALE


def test_unknown_identity_cannot_return_path_as_a_fallback(host):
    provider, request, _, _ = host
    provider.dataset.identity_for_path = lambda path: None
    assert provider.query(request).status == ExampleStatus.STALE


def test_no_longer_included_library_is_not_queried(host):
    provider, request, state, indexes = host
    state.scope = ("a",)
    assert provider.query(request).status == ExampleStatus.STALE
    assert all(index.reads == 0 for index in indexes.values())


def test_changed_alias_request_uses_new_approved_names(host):
    provider, request, state, _ = host
    old = provider.query(request)
    state.knowledge = "knowledge-2"
    assert not provider.accepts(old)
    renamed = replace(request, knowledge_revision=state.knowledge,
                      identities=(ExampleIdentity("fox-id", ("Trees",), "animals"),))
    assert provider.query(renamed).references == (MediaReference("a", "vulpes.png"),)


def test_reused_revision_cannot_hide_changed_cached_rows(host):
    provider, request, _, indexes = host
    result = provider.query(request)
    indexes["a"].rows.clear()
    assert not provider.accepts(result)
    fresh = provider.query(request)
    assert fresh.references == (MediaReference("b", "fox.png"),)


def test_missing_frozen_projection_holds_without_native_reads(host):
    provider, request, _, indexes = host
    provider.read_snapshot = lambda key, index: None
    assert provider.query(request).status == ExampleStatus.UNAVAILABLE
    assert all(index.reads == 0 for index in indexes.values())


def test_delivery_selection_change_during_snapshot_capture_is_rejected(host):
    provider, request, state, _ = host
    result = provider.query(request)
    read = provider.read_snapshot

    def changed(key, index):
        snapshot = read(key, index)
        state.selection = "new-selection"
        return snapshot

    provider.read_snapshot = changed
    assert not provider.accepts(result)


def test_reference_limit_holds_instead_of_silently_truncating(host):
    provider, request, _, _ = host
    provider.max_references = 1
    result = provider.query(request)
    assert result.status == ExampleStatus.UNAVAILABLE
    assert result.references == ()


@pytest.mark.parametrize("path", ["", "/absolute", "../escape", "a/../b", "./a", "a//b", "C:/root", "C:root", "a\\b"])
def test_reference_rejects_unsafe_relative_paths(path):
    with pytest.raises(ValueError):
        MediaReference("a", path)


def test_request_rejects_duplicate_scope_or_identity(host):
    _, request, _, _ = host
    with pytest.raises(ValueError):
        replace(request, library_ids=("a", "a"))
    with pytest.raises(ValueError):
        replace(request, identities=request.identities * 2)
