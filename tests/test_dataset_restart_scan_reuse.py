"""Restart contracts through real page, dependency and disposable index objects."""

from pathlib import Path
from time import perf_counter, sleep
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from core import AppSettings, AppState, TagDatabase
from core.automatic_ai_analysis import AutomaticAIAnalysisResult
from core.dataset_index import DatasetIndex
from core.image_authority_initialization import ImageAuthorityInitializationResult
from core.image_workflow_state import ImageWorkflowStateService
from qt_app.dataset_page import DatasetPage


def _wait(app, predicate):
    deadline = perf_counter() + 10
    while not predicate() and perf_counter() < deadline:
        app.processEvents()
        sleep(.01)
    assert predicate()


def _bound_dataset(state, library, images, cache_dir):
    """Bind a synthetic library through the same federation used by DatasetPage."""
    profile = state.settings.select_library_root(library)
    dataset = state.dataset
    dataset.configure(state.settings.library_registry(),
                      preferred_library_id=profile.stable_id,
                      scope_library_ids=(profile.stable_id,))
    manager = dataset.manager(profile.stable_id)
    manager.root = library
    manager.images = list(images)
    manager.index = DatasetIndex(library, cache_dir)
    dataset.set_library_availability(profile.stable_id, "current")
    return dataset, manager


@pytest.mark.parametrize("change", ["none", "one_sidecar", "policy", "ingest", "legacy_v2", "expanded_rules"])
def test_dataset_classification_restores_into_a_fresh_page(tmp_path, monkeypatch, change):
    from dataclasses import asdict
    from core import conflict_policy, workflow_semantic_proof
    from core import image_workflow_state, automation_repository
    from core.automation_rules import AutomationEngine
    from core.conflict_rules import ConflictRule, ConflictRuleEngine
    # Public source has no private default rules. Keep this cache-format
    # transition explicit and synthetic rather than relying on the host rules.
    engine = AutomationEngine({}, conflicts=ConflictRuleEngine([
        ConflictRule("synthetic-cache-rule", ["synthetic-left", "synthetic-right"]),
    ]))
    monkeypatch.setattr(workflow_semantic_proof, "active_automation_engine", lambda _knowledge: engine)
    monkeypatch.setattr(image_workflow_state, "active_automation_engine", lambda _knowledge: engine)
    monkeypatch.setattr(automation_repository, "active_automation_engine", lambda _knowledge: engine)
    app = QApplication.instance() or QApplication([])
    library = tmp_path / "library"
    library.mkdir()
    images = [library / f"synthetic-{number}.png" for number in range(2)]
    for image in images:
        image.write_bytes(b"synthetic image; pixel decoding is outside this check")
        image.with_suffix(".tags.json").write_text('{"schema_version":6}', encoding="utf-8")
    cache = tmp_path / "cache"
    assessed = []
    original_assess = ImageWorkflowStateService.assess

    def assess(service, image, *args, **kwargs):
        assessed.append(Path(image))
        return original_assess(service, image, *args, **kwargs)

    monkeypatch.setattr(ImageWorkflowStateService, "assess", assess)
    # Conflict preparation is independent of the classification worker and
    # its fingerprint; avoid unrelated Qt timing while exercising real restore.
    monkeypatch.setattr(DatasetPage, "_start_conflict_preparation", lambda self: None)
    contexts = []
    serializer = conflict_policy.conflict_rule_cache_payload
    def cache_payload(rule, *, structured=False, **options):
        if change == "expanded_rules" and session == 0:
            return asdict(rule) if structured else dict(vars(rule))
        return serializer(rule, structured=structured, **options)
    monkeypatch.setattr(conflict_policy, "conflict_rule_cache_payload", cache_payload)
    monkeypatch.setattr(workflow_semantic_proof, "conflict_rule_cache_payload", cache_payload)
    for session in range(3 if change == "expanded_rules" else 2):
        settings = AppSettings()
        if session and change == "policy":
            settings.ignored_plain_language_sentences = ["Synthetic excluded sentence."]
        state = AppState(settings=settings, knowledge=TagDatabase(tmp_path / "tags.json"))
        dataset, manager = _bound_dataset(state, library, images, cache)
        if not session:
            for image in images:
                manager.index.put(image, [])
        else:
            if change == "one_sidecar":
                images[0].with_suffix(".tags.json").write_text(
                    '{"schema_version":6, "synthetic_change":true}', encoding="utf-8",
                )
            manager.validate_cached()
            manager.analyze()
        page = DatasetPage(state, defer_expensive_initialization=True)
        scan = Mock(wraps=page._start_workflow_filter_scan)
        monkeypatch.setattr(page, "_start_workflow_filter_scan", scan)
        assessed.clear()
        try:
            page.set_dataset(dataset)
            _wait(app, lambda: page._dataset_search_results_ready())
            contexts.append(page._readiness_context_fingerprint)
            assert set(page._readiness_cache) == set(images)
            expected = images if not session else (
                images[:1] if change == "one_sidecar" else []
            )
            assert assessed == expected
            # The intervening expanded format needs one background receipt
            # validation, but it must perform zero image assessments and save
            # the compatible result so the next restart needs no status job.
            assert scan.call_count == bool(expected or change == "policy"
                                           or (change == "expanded_rules" and session == 1))
            saved_context=(page.workflow_states.context_fingerprint(cache_version=2)
                           if session and change=="legacy_v2" else contexts[-1])
            assert set(manager.index.cached_workflow_states(
                context_fingerprint=saved_context,
                context_validator=page.workflow_states.semantic_validator(),
            )) == set(images)
            if not session and change == "legacy_v2":
                legacy_context=page.workflow_states.context_fingerprint(cache_version=2)
                manager.index.record_workflow_states(
                    {path:{**value[1].to_cache_dict(),"version":2}
                     for path,value in page._readiness_cache.items()},
                    context_fingerprint=legacy_context,
                )
            if not session and change == "ingest":
                from types import SimpleNamespace
                from qt_app.ingest_page import IngestPage
                context = IngestPage._workflow_context_fingerprint(SimpleNamespace(
                    workflow_states=page.workflow_states, settings=settings,
                ))
                assert context != contexts[-1]
                manager.index.record_workflow_states(
                    {path: {**value[1].to_cache_dict(), "automatic_preparation_complete": True}
                     for path, value in page._readiness_cache.items()},
                    context_fingerprint=context, cache_scope="ingest",
                )
        finally:
            page.request_background_shutdown()
            state.jobs.shutdown(wait=True)
            page.pool.waitForDone(5000)
            page.close()
            app.processEvents()
    assert (contexts[0] == contexts[1]) == (change not in {"policy", "expanded_rules"})
    if change == "expanded_rules":
        assert contexts[1] == contexts[2]


@pytest.mark.parametrize("surface", ["dataset", "ingest"])
@pytest.mark.parametrize("stop", ["cancel", "failure"])
def test_status_workers_checkpoint_before_completion(tmp_path, monkeypatch, surface, stop):
    from types import SimpleNamespace
    from core import ImageWorkflowState
    from core.workflow_state_cache import WorkflowStateCheckpoint
    from qt_app import dataset_page, ingest_page

    app = QApplication.instance() or QApplication([])
    images = [tmp_path / f"synthetic-{number}.png" for number in range(90)]
    index = DatasetIndex(tmp_path, tmp_path / "cache")
    for image in images:
        image.write_bytes(b"synthetic")
        index.put(image, [])
    ready = ImageWorkflowState("reviewed", "green", "Complete", "Ready", 100, True)
    tasks = []

    class Task:
        def __init__(self, *args, **kwargs):
            self.callable = args[-1]
            self.signals = SimpleNamespace(progress=Mock(), complete=Mock(), failed=Mock(), cancelled=Mock())
        def start(self):
            tasks.append(self)

    common = dict(
        images=images, state=SimpleNamespace(
            knowledge=None, jobs=Mock(shutting_down=False),
            begin_long_operation=lambda *args, **kwargs: (True, ""),
            update_long_operation=Mock(),
        ),
        settings=SimpleNamespace(ignored_plain_language_sentences=[], ingest_directory=str(tmp_path)),
        _readiness_context_fingerprint="synthetic-context",
    )
    if surface == "dataset":
        monkeypatch.setattr(dataset_page, "CancelableProgressCallTask", Task)
        monkeypatch.setattr(ImageWorkflowStateService, "assess", lambda *args: ready)
        page = SimpleNamespace(
            **common, _filter_state_task=None, _filter_generation=0, characters=None, categories=None,
            dataset=SimpleNamespace(root=tmp_path, images=images, index=index),
            header=Mock(), cancel_projection_button=Mock(), pool=SimpleNamespace(start=tasks.append),
            _workflow_persistence_targets=lambda paths: {path:index for path in paths},
            _workflow_signatures=DatasetPage._workflow_signatures,
            _workflow_filter_complete=Mock(), _workflow_filter_failed=Mock(),
        )
        DatasetPage._start_workflow_filter_scan(page, images)
        page.state.update_long_operation.assert_called_once_with(
            "dataset", 0, len(images), "Preparing image-status checks",
        )
    else:
        monkeypatch.setattr(ingest_page, "CoordinatedCallTask", Task)
        from PySide6.QtWidgets import QProgressBar
        page = SimpleNamespace(
            **common, _state_scan_task=None, _state_scan_generation=0,
            _workflow_index=index, state_progress=QProgressBar(),
            _compute_state_projection=lambda path: ("ready", ready),
            _state_scan_complete=Mock(), _state_scan_failed=Mock(), _state_scan_cancelled=Mock(),
        )
        ingest_page.IngestPage._start_state_scan(page, images)

    processed = 0
    def progress(current, total, name):
        nonlocal processed
        # Cache validation has its own progress; cancellation below targets
        # durable image work, where a completed batch must survive restart.
        if name == "Checking saved image status":
            return
        processed = current
        if current >= WorkflowStateCheckpoint.BATCH_SIZE:
            # This connection represents a different process while the scan is
            # still active; committed batches must already be reusable.
            restarted = DatasetIndex(tmp_path, tmp_path / "cache")
            assert len(restarted.cached_workflow_states(
                context_fingerprint="synthetic-context", cache_scope=surface,
            )) >= WorkflowStateCheckpoint.BATCH_SIZE
        if current >= 65 and stop == "failure":
            raise RuntimeError("synthetic interrupted worker")

    def run():
        if surface == "dataset":
            return tasks[0].callable(progress, lambda: processed >= 65)
        return tasks[0].callable(SimpleNamespace(
            checkpoint=lambda: processed < 65, update=progress,
        ))

    if stop == "failure":
        with pytest.raises(RuntimeError, match="synthetic interrupted worker"):
            run()
    elif surface == "ingest":
        with pytest.raises(InterruptedError):
            run()
    else:
        assert run()[2] is True
    restored = DatasetIndex(tmp_path, tmp_path / "cache").cached_workflow_states(
        context_fingerprint="synthetic-context", cache_scope=surface,
    )
    assert set(restored) == set(images[:processed])
    assert processed < len(images)
    app.processEvents()


def test_checkpoint_rejects_changed_files_and_keeps_memory_on_cache_failure(tmp_path):
    from core.workflow_state_cache import WorkflowStateCheckpoint
    import sqlite3
    image = tmp_path / "synthetic.png"
    image.write_bytes(b"before")
    index = DatasetIndex(tmp_path, tmp_path / "cache")
    checkpoint = WorkflowStateCheckpoint(index=index, context_fingerprint="context")
    checkpoint.add(image, {"version": 2}, index.fingerprint(image))
    image.write_bytes(b"changed")
    checkpoint.flush()
    assert checkpoint.recorded == set()
    assert not checkpoint.cache_warning
    index.record_workflow_states = Mock(side_effect=sqlite3.OperationalError("synthetic cache failure"))
    checkpoint.add(image, {"version": 2}, index.fingerprint(image))
    checkpoint.flush()
    assert checkpoint.recorded == {image}
    assert checkpoint.cache_warning


def test_observation_counts_and_json_formatting_do_not_invalidate_status(tmp_path):
    import json
    payload = {"schema_version": 1, "tags": [{
        "id": "synthetic-tag", "display": "solo", "category": "subject",
        "seen_count": 1, "first_seen": "before", "last_seen": "before",
        "future_meaning": {"version": 1},
    }]}
    path = tmp_path / "tags.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = ImageWorkflowStateService(TagDatabase(path)).context_fingerprint()
    payload["tags"][0].update(seen_count=99, first_seen="earlier", last_seen="later")
    path.write_text(json.dumps(payload, indent=4, sort_keys=True), encoding="utf-8")
    assert ImageWorkflowStateService(TagDatabase(path)).context_fingerprint() == first
    # Unknown fields remain dependencies, as do actual category/ordering facts.
    for key, value in (("category", "changed"), ("future_meaning", {"version": 2})):
        changed = json.loads(json.dumps(payload))
        changed["tags"][0][key] = value
        path.write_text(json.dumps(changed), encoding="utf-8")
        assert ImageWorkflowStateService(TagDatabase(path)).context_fingerprint() != first


def test_workflow_context_tracks_default_character_relationships(tmp_path, monkeypatch):
    import json
    from core import CharacterAnnotation, CharacterDatabase, ImageRecord
    from core.knowledge_relationships import CHARACTER_SOURCE, KnowledgeRelationship, RelationshipEndpoint

    user = tmp_path / "user"
    defaults = tmp_path / "defaults"
    user.mkdir(); defaults.mkdir()
    for filename, version, key, records in (
        ("series.json", CharacterDatabase.SERIES_SCHEMA_VERSION, "series", [
            {"id": "old", "name": "Old Work"}, {"id": "new", "name": "New Work"},
        ]),
        ("characters.json", CharacterDatabase.CHARACTERS_SCHEMA_VERSION, "characters", [
            {"id": "hero", "name": "Hero"},
        ]),
        ("outfits.json", CharacterDatabase.OUTFITS_SCHEMA_VERSION, "outfits", []),
    ):
        (user / filename).write_text(json.dumps({"schema_version": version, key: records}), encoding="utf-8")

    def write_source(target):
        relationship = KnowledgeRelationship(
            id="default-source", kind=CHARACTER_SOURCE,
            endpoints=(RelationshipEndpoint("character", "hero"), RelationshipEndpoint("character_source", target)),
            source="synthetic", layer="default",
        )
        (defaults / "relationships.json").write_text(json.dumps({
            "schema_version": 1, "relationships": [relationship.to_dict()],
        }), encoding="utf-8")

    write_source("old")
    characters = CharacterDatabase(user)
    image = tmp_path / "synthetic.png"
    image.write_bytes(b"synthetic image")
    record = ImageRecord(image)
    record.annotation.sdxl_tags = ["general", "1girl", "solo"]
    record.annotation.global_tags = {"rating": ["general"], "subject_count": ["1girl", "solo"]}
    record.annotation.plain_language = ["A synthetic portrait."]
    record.annotation.review_status = "reviewed"
    record.annotation.characters = [CharacterAnnotation(slot=1, role="person", character_id="hero")]
    record.modified = True
    record.save_sidecar(force=True)
    record.export_caption(character_db=characters, force=True)
    service = ImageWorkflowStateService(characters=characters)
    before = service.context_fingerprint()
    assert service.assess(image).training_export_health_state == "current"
    assert ImageWorkflowStateService(characters=CharacterDatabase(user)).context_fingerprint() == before
    write_source("new")
    changed = ImageWorkflowStateService(characters=CharacterDatabase(user))
    assert changed.context_fingerprint() != before
    assert changed.assess(image).training_export_health_state == "stale"


def test_restart_miss_summary_contains_counts_without_image_data(tmp_path):
    from core import ImageWorkflowState
    images = [tmp_path / f"synthetic-{number}.png" for number in range(3)]
    index = DatasetIndex(tmp_path, tmp_path / "cache")
    ready = ImageWorkflowState("reviewed", "green", "Complete", "Ready", 100, True)
    for image in images:
        image.write_bytes(b"synthetic")
        image.with_suffix(".tags.json").write_text('{"schema_version":6}', encoding="utf-8")
        index.put(image, [])
    index.record_workflow_states({images[0]: ready.to_cache_dict()}, context_fingerprint="current")
    index.record_workflow_states({images[1]: ready.to_cache_dict()}, context_fingerprint="old")
    assert set(index.cached_workflow_states(context_fingerprint="current")) == {images[0]}
    assert index.workflow_cache_summary == {
        "restored": 1, "context_changed": 1, "missing": 1, "file_changed": 0,
    }
    index.connect = Mock(side_effect=OSError("synthetic unavailable cache"))
    assert index.cached_workflow_states(context_fingerprint="current") == {}
    assert index.workflow_cache_summary == {"unavailable": 1}


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_ingest_restored_status_is_not_automatic_preparation(tmp_path, monkeypatch, prepared, cancelled, failed):
    from types import MethodType, SimpleNamespace
    from PySide6.QtWidgets import QProgressBar
    from core import ImageWorkflowState
    from qt_app import ingest_page
    app = QApplication.instance() or QApplication([])
    image = tmp_path / "synthetic.png"
    image.write_bytes(b"synthetic")
    index = DatasetIndex(tmp_path, tmp_path / "cache")
    ready = ImageWorkflowState("reviewed", "green", "Complete", "Ready", 100, True)
    seen = {}

    class Initializer:
        def __init__(self, **kwargs):
            seen["trusted"] = kwargs["trusted_existing"]
        def initialize_many(self, paths, **kwargs):
            for path in paths:
                if path not in seen["trusted"]:
                    kwargs["on_cache_complete"](path, object(), {})
            return ImageAuthorityInitializationResult(reused=1)

    class Automatic:
        def __init__(self, *args, **kwargs): pass
        def analyze_many(self, paths, **kwargs):
            seen["ai_paths"] = paths
            return AutomaticAIAnalysisResult(errors=["synthetic"] if failed else [],
                                             cancelled=cancelled)

    monkeypatch.setattr(ingest_page, "ImageAuthorityInitializationService", Initializer)
    monkeypatch.setattr(ingest_page, "AutomaticAIAnalysisService", Automatic)
    def run_batch(operation, label, paths, **kwargs):
        seen["result"] = operation(paths, lambda *args:None, lambda:False)
    page = SimpleNamespace(
        settings=SimpleNamespace(auto_import_source_evidence=False, ingest_directory=str(tmp_path)),
        state=SimpleNamespace(knowledge=None, ai=Mock(), ai_work_lease=None, moves=SimpleNamespace(last_available=lambda:None)),
        state_progress=QProgressBar(), refresh_button=Mock(), selected=set(), render_page=Mock(),
        workflow_states=SimpleNamespace(assess_record=lambda *args, **kwargs: ready),
        _run_batch=run_batch,
    )
    page._start_managed_ingest = MethodType(ingest_page.IngestPage._start_managed_ingest, page)
    # These are the independent display and completion fields produced by the
    # inventory reader. A generic readiness row supplies only the former.
    ingest_page.IngestPage._scan_complete(page, tmp_path, [image], automatic=True, persisted={
        "index": index, "states": {image: "saved display state"}, "record_paths": {image},
        "trusted_paths": {image} if prepared else set(),
    }, context_fingerprint="context")
    assert seen["trusted"] == ({image} if prepared else set())
    assert seen["ai_paths"] == ([] if prepared else [image])
    assert set(seen["result"]["automatic_prepared_paths"]) == (
        {image} if prepared or not (cancelled or failed) else set()
    )
    if not prepared:
        payload = index.validated_workflow_states([image], context_fingerprint="context", cache_scope="ingest")[image]
        assert payload["automatic_preparation_complete"] is (not cancelled and not failed)
    app.processEvents()


def test_dataset_explains_a_cache_miss_during_classification(tmp_path, monkeypatch):
    import json
    from threading import Event
    from PySide6.QtGui import QImage, QColor, QFontDatabase
    from core import ImageWorkflowState
    from qt_app.theme import apply_theme
    from tools.test_evidence import build_test_evidence
    from version import VERSION

    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    apply_theme(app, "dark", False)
    library = tmp_path / "library"
    library.mkdir()
    image = library / "synthetic.png"
    pixels = QImage(80, 80, QImage.Format_RGB32)
    pixels.fill(QColor("#205072"))
    assert pixels.save(str(image))
    image.with_suffix(".tags.json").write_text('{"schema_version":6}', encoding="utf-8")
    release = Event()
    def assess(*args, **kwargs):
        assert release.wait(10)
        return ImageWorkflowState("reviewed", "green", "Complete", "Ready", 100, True)
    monkeypatch.setattr(ImageWorkflowStateService, "assess", assess)
    monkeypatch.setattr(DatasetPage, "_start_conflict_preparation", lambda self: None)
    state = AppState(settings=AppSettings(), knowledge=TagDatabase(tmp_path / "tags.json"))
    dataset, manager = _bound_dataset(state, library, [image], tmp_path / "cache")
    manager.index.put(image, [])
    page = DatasetPage(state, defer_expensive_initialization=True)
    try:
        page.resize(1700, 1040); page.show()
        page.set_dataset(dataset)
        _wait(app, lambda: page._filter_state_task is not None)
        activity_binding = next(
            binding for binding in page._status_footer_bindings
            if page.search_activity_status in binding.widgets
        )
        assert page.search_activity_status.parentWidget() is activity_binding.row
        assert activity_binding.footer.window() is page
        # Supporting messages now live under the shared footer disclosure.
        activity_binding.footer.details_button.setChecked(True)
        app.processEvents()
        assert page.search_activity_status.isVisible()
        assert "do not have saved status yet" in page.search_activity_status.text()
        page._filter_state_task.signals.progress.emit(0, 1, "synthetic.png")
        app.processEvents()
        assert "do not have saved status yet" in page.header.status.text()
        page.update_responsive_layout(); app.processEvents()
        capture = tmp_path / "dataset-cache-reason-1080p.png"
        assert page.grab().save(str(capture))
        report = tmp_path / "dataset-cache-visual.json"
        report.write_text(json.dumps({
            "evidence": build_test_evidence(Path(__file__).resolve().parents[1], VERSION, Path(__file__)),
            "captures": [str(capture)], "scope": "Synthetic Dataset cache reason at 1700 x 1040",
        }, indent=2), encoding="utf-8")
        print(f"Dataset cache visual evidence: {report}")
        release.set()
        _wait(app, lambda: page._dataset_search_results_ready())
    finally:
        release.set(); page.request_background_shutdown()
        state.jobs.shutdown(wait=True)
        page.pool.waitForDone(5000)
        page.close(); app.processEvents()


def test_repeated_startup_binding_preserves_live_work_and_exactly_rechecks_changes(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    library = tmp_path / "library"
    library.mkdir()
    images = [library / f"synthetic-{number}.png" for number in range(3)]
    for image in images:
        image.write_bytes(b"synthetic")
        image.with_suffix(".tags.json").write_text('{"schema_version":6}', encoding="utf-8")
    state = AppState(settings=AppSettings(), knowledge=TagDatabase(tmp_path / "tags.json"))
    dataset, manager = _bound_dataset(state, library, images, tmp_path / "cache")
    for image in images:
        manager.index.put(image, [])
    monkeypatch.setattr(DatasetPage, "_start_conflict_preparation", lambda self: None)
    page = DatasetPage(state, defer_expensive_initialization=True)
    assessed = []
    from core import ImageWorkflowState
    def assess(service, path, *args, **kwargs):
        assessed.append(Path(path))
        return ImageWorkflowState("reviewed", "green", "Complete", "Ready", 100, True)
    monkeypatch.setattr(ImageWorkflowStateService, "assess", assess)
    try:
        page.set_dataset(dataset)
        _wait(app, lambda: page._dataset_search_results_ready())
        saved = dict(page._readiness_cache)
        manager.changed_images.update(images)
        page.set_dataset(dataset)
        assert page._readiness_cache == saved
        manager.changed_images.clear()
        page.batch_selected.add(str(images[1]))
        # A working memory result remains useful even if its optional persisted
        # checkpoint becomes unavailable between startup notifications.
        manager.index.invalidate_workflow_image(images[1])
        active = Mock()
        page._filter_state_task = active
        assessed.clear()
        page.set_dataset(dataset)
        assert page._filter_state_task is active
        active.cancel.assert_not_called()
        assert page._readiness_cache == saved
        assert page._readiness_cache[images[1]] is saved[images[1]]
        assert page.batch_selected == {str(images[1])}
        assert assessed == []
        # Revalidation discovers an external edit before the next binding.
        images[0].with_suffix(".tags.json").write_text(
            '{"schema_version":6,"synthetic_change":true}', encoding="utf-8",
        )
        manager.validate_cached()
        assert images[0] in manager.changed_images
        page.set_dataset(dataset)
        active.cancel.assert_called_once_with()
        _wait(app, lambda: page._dataset_search_results_ready())
        assert assessed == images[:1]
        assert page.batch_selected == {str(images[1])}
    finally:
        page.request_background_shutdown()
        state.jobs.shutdown(wait=True)
        page.pool.waitForDone(5000)
        page.close()
        app.processEvents()


def test_dataset_binding_reuses_its_fresh_readiness_context_for_restore(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    library = tmp_path / "library"
    library.mkdir()
    state = AppState(settings=AppSettings(), knowledge=TagDatabase(tmp_path / "tags.json"))
    dataset, manager = _bound_dataset(state, library, [], tmp_path / "cache")
    monkeypatch.setattr(DatasetPage, "_start_conflict_preparation", lambda self: None)
    page = DatasetPage(state, defer_expensive_initialization=True)
    original_context = page.workflow_states.context_fingerprint
    context = Mock(wraps=original_context)
    monkeypatch.setattr(page.workflow_states, "context_fingerprint", context)
    try:
        page.set_dataset(dataset)
        assert context.call_count == 1
        context.reset_mock()
        page.set_dataset(dataset)
        assert context.call_count == 1
    finally:
        page.request_background_shutdown()
        state.jobs.shutdown(wait=True)
        page.pool.waitForDone(5000)
        page.close()
        app.processEvents()


@pytest.mark.parametrize("legacy",[False,True])
@pytest.mark.parametrize("expanded_defaults",[False,True])
def test_ingest_recheck_reuses_valid_saved_status_and_only_checks_changed_files(tmp_path,monkeypatch,legacy,expanded_defaults):
    from dataclasses import asdict
    from core import conflict_policy, workflow_semantic_proof
    from core import ImageWorkflowState
    from qt_app import ingest_page as module
    expanded = [expanded_defaults]
    serializer = conflict_policy.conflict_rule_cache_payload
    def cache_payload(rule, *, structured=False, **options):
        if expanded[0]:
            return asdict(rule) if structured else dict(vars(rule))
        return serializer(rule, structured=structured, **options)
    monkeypatch.setattr(conflict_policy, "conflict_rule_cache_payload", cache_payload)
    monkeypatch.setattr(workflow_semantic_proof, "conflict_rule_cache_payload", cache_payload)
    app=QApplication.instance() or QApplication([])
    library=tmp_path/"incoming"; library.mkdir()
    images=[library/f"synthetic-{number}.png" for number in range(2)]
    for image in images:
        image.write_bytes(b"synthetic")
        image.with_suffix(".tags.json").write_text('{"schema_version":6}',encoding="utf-8")
    settings=AppSettings(); settings.ingest_directory=str(library)
    state=AppState(settings=settings,knowledge=TagDatabase(tmp_path/"tags.json"))
    monkeypatch.setattr(module.IngestPage,"_start_conflict_preparation",lambda self:None)
    page=module.IngestPage(state,defer_expensive_initialization=True)
    index=DatasetIndex(library,tmp_path/"cache")
    workflow=ImageWorkflowState("reviewed","green","Complete","Ready",90,True,
        has_saved_tags=True,has_saved_descriptions=True,has_pending_tags=False,has_pending_descriptions=False)
    if expanded_defaults:
        workflow=page.workflow_states.assess(images[0])
    context=page._workflow_context_fingerprint(workflow_context=(
        page.workflow_states.context_fingerprint(cache_version=2) if legacy else None))
    index.record_workflow_states({image:{**page._workflow_cache_payload(workflow),"version":2 if legacy else 3,
                                        "automatic_preparation_complete":True} for image in images},
                                context_fingerprint=context,cache_scope="ingest")
    expanded[0] = False
    monkeypatch.setattr(module,"DatasetIndex",lambda _root:index)
    scans=Mock(); monkeypatch.setattr(page,"_start_state_scan",scans)
    try:
        page.scan(full_readiness=True)
        _wait(app,lambda:page._scan_task is None)
        scans.assert_called_once_with([])
        assert set(page._state_cache)==set(images)
        assert page._trusted_workflow_paths==set(images)
        scans.reset_mock()
        images[0].with_suffix(".tags.json").write_text('{"schema_version":6,"changed":true}',encoding="utf-8")
        page.scan(full_readiness=True)
        _wait(app,lambda:page._scan_task is None)
        scans.assert_called_once_with(images[:1])
        assert set(page._state_cache)=={images[1]}
        assert page._trusted_workflow_paths=={images[1]}
    finally:
        page.request_background_shutdown(); state.jobs.shutdown(wait=True)
        page.pool.waitForDone(5000); page.close(); app.processEvents()


@pytest.mark.parametrize("cancelled,warning,unstable,current,cleared",[
    (False,False,(),True,True),(True,False,(),True,False),
    (False,True,(),True,False),(False,False,("changed.png",),True,False),
    (False,False,(),False,False),
])
def test_successful_current_scan_clears_stale_cache_explanation(monkeypatch,cancelled,warning,unstable,current,cleared):
    from types import SimpleNamespace
    task=object()
    page=SimpleNamespace(_filter_state_task=task,_filter_state_automatic=False,
        state=SimpleNamespace(finish_long_operation=Mock()),cancel_projection_button=Mock(),
        _filter_generation=2,_readiness_cache={},_workflow_cache_notice="old explanation",
        header=Mock(),filtered=[],render_page=Mock(),apply_filters=Mock(),
        _notify_background_work_finished=Mock())
    monkeypatch.setattr(DatasetPage,"_invalidate_dataset_search",lambda _:None)
    DatasetPage._workflow_filter_complete(page,task,(2 if current else 1,{},cancelled,warning,unstable))
    assert (page._workflow_cache_notice=="")==cleared


def test_shutdown_notifies_after_detaching_search_without_completion(tmp_path,qt_test_scope,monkeypatch):
    from PySide6.QtTest import QSignalSpy
    monkeypatch.setattr(DatasetPage,"_start_conflict_preparation",lambda self:None)
    state=AppState(settings=AppSettings(),knowledge=TagDatabase(tmp_path/"tags.json"))
    page=DatasetPage(state,defer_expensive_initialization=True)
    class DetachedReader:
        # Coordinated reader cancellation intentionally has no later Qt signal.
        def __init__(self):self.cancel_calls=0
        def cancel(self):self.cancel_calls+=1
    reader=DetachedReader();page._dataset_search_task=reader
    spy=QSignalSpy(page.background_work_finished)
    assert page.has_active_background_work()
    page.request_background_shutdown()
    assert reader.cancel_calls==1 and page._dataset_search_task is None
    assert not page.has_active_background_work()
    qt_test_scope.settle()
    assert spy.count()==1,"Detached search must release a waiting close owner without a nonexistent completion callback"
    assert not state.jobs.active()
    state.shutdown_background_jobs(wait=True)
