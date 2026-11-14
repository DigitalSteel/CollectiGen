"""Generated Manager entry, catalog prompt choice, save/reopen and refusal."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from copy import deepcopy
from pathlib import Path
from time import monotonic

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from core.generation_prompt_catalog import PromptCatalog, catalog_draft, catalog_from_plan
from core.generation_planning import GenerationPlan, save_plan, load_plan_with_digest
from qt_app.generation_saved_work_dialog import SavedGenerationWorkDialog
from qt_app.generation_workflow import open_generation_workspace
from suite_core.manager.window import create_window, unconfigured
from test_manager_generator_setup_ui import application, wait_until, capture, FixtureRuntime
from test_generation_prompt_catalog import payload


def test_manager_visible_catalog_import_preserves_other_draft_and_reopens_choices(tmp_path, monkeypatch):
    app = application()
    monkeypatch.setattr("core.swarm_runtime.SwarmRuntimeManager", FixtureRuntime)
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path / "suite")))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1800, 980); window.show()
    window.generation_state()
    open_generation_workspace(window, force_new=True)
    wait_until(app, lambda: getattr(window, "_generation_dialog", None) is not None)
    original = window._generation_dialog
    wait_until(app, lambda: not original._pending)
    original.positive.setPlainText("keep the first image request")
    wait_until(app, lambda: original._draft_path.exists() and not original._pending and
               load_plan_with_digest(original._draft_path)[0].request.positive_prompt == "keep the first image request", timeout=30)
    original_bytes = original._draft_path.read_bytes()
    source = tmp_path / "exported-prompt-catalog.json"
    save_plan(source, catalog_draft(PromptCatalog.from_payload(payload())))
    source_bytes = source.read_bytes()
    monkeypatch.setattr("qt_app.generation_saved_work_dialog.QFileDialog.getOpenFileName", lambda *_a, **_k: (str(source), ""))
    errors = []
    deadline = monotonic() + 25
    def import_catalog():
        picker = next((w for w in app.topLevelWidgets() if isinstance(w, SavedGenerationWorkDialog) and w.isVisible()), None)
        if picker is None:
            assert monotonic() < deadline, "Prompt catalog picker did not open"
            QTimer.singleShot(20, import_catalog); return
        if picker._task is not None or not picker.import_button.isEnabled():
            if monotonic() >= deadline:
                errors.append(AssertionError(picker.status.text())); picker.reject(); return
            QTimer.singleShot(20, import_catalog); return
        try:
            assert picker.windowTitle() == "Use Organizer Prompt Catalog"
            assert not picker.create_setup_button.isVisible()
            capture(picker, "catalog-import-ready")
            QTest.mouseClick(picker.import_button, Qt.LeftButton)
            def open_imported():
                if picker._task is not None or not picker.open_button.isEnabled():
                    if monotonic() >= deadline:
                        errors.append(AssertionError(picker.status.text())); picker.reject(); return
                    QTimer.singleShot(20, open_imported); return
                QTest.mouseClick(picker.open_button, Qt.LeftButton)
            QTimer.singleShot(20, open_imported)
        except Exception as error:
            errors.append(error); picker.reject()
    QTimer.singleShot(20, import_catalog)
    window.navigation.setCurrentRow(window.page_rows["Generate"])
    QTest.mouseClick(window.prompt_catalog_button, Qt.LeftButton)
    assert not errors
    wait_until(app, lambda: window._generation_dialog is not original)
    draft = window._generation_dialog
    wait_until(app, lambda: not draft._pending)
    assert catalog_from_plan(draft.plan) is not None
    assert original._draft_path.read_bytes() == original_bytes
    assert source.read_bytes() == source_bytes
    draft.prompt_tag_search.setText("azure")
    wait_until(app, lambda: draft.prompt_tag_grid.count() == 1)
    chip = draft.prompt_tag_grid.itemAt(0).widget()
    QTest.mouseClick(chip, Qt.LeftButton)
    assert "blue dress" in draft.positive.toPlainText()
    draft.tabs.setCurrentIndex(2)
    assert draft.advanced_tasks.currentIndex() == 0
    assert draft.wearer_choice.isVisible() and draft.outfit_scope_choice.isVisible()
    before = draft.plan.request.positive_prompt
    draft.wearer_choice.setCurrentIndex(draft.wearer_choice.findData("char-one"))
    for group, expected in [("associated", "own"), ("other", "other"), ("generic", "generic")]:
        draft.outfit_scope_choice.setCurrentIndex(draft.outfit_scope_choice.findData(group))
        assert expected in [draft.fragment_tree.topLevelItem(i).data(0, Qt.UserRole)["id"] for i in range(draft.fragment_tree.topLevelItemCount())]
        assert draft.plan.request.positive_prompt == before
    capture(window, "catalog-outfit-scopes")
    wait_until(app, lambda: not draft._pending and draft._draft_path.exists() and
               catalog_from_plan(load_plan_with_digest(draft._draft_path)[0]) is not None, timeout=30)
    path = draft._draft_path
    saved, token = load_plan_with_digest(path)
    draft._apply_loaded_plan(GenerationPlan.create(None), tmp_path / "plain.json", "generated-token")
    assert draft.prompt_knowledge is None and not draft.outfits
    draft._apply_loaded_plan(saved, path, token)
    assert draft.prompt_knowledge.tags["tag-blue"].display == "blue dress"
    assert len(draft.characters) == 2 and len(draft.outfits) == 3
    assert source.read_bytes() == source_bytes
    window.close()
    wait_until(app, lambda: not window.isVisible(), timeout=30)
    assert not hasattr(window, "state"), "Normal Manager Close must release its own generation state"


def test_malformed_catalog_load_keeps_current_draft_and_native_choices(tmp_path):
    app = application()
    from core.background_jobs import BackgroundJobManager
    from qt_app.generation_dialog import GenerationDialog
    from core.generation_planning import GenerationPlanValidationError
    jobs = BackgroundJobManager()
    native = NS(tags={})
    dialog = GenerationDialog(jobs, storage_root=tmp_path / "generation", prompt_knowledge=native, runtime_allowed=False)
    before = dialog.plan.digest
    invalid = payload(); invalid["tags"].append(deepcopy(invalid["tags"][0]))
    bad = GenerationPlan.create(None).revise({"target_context": {"organizer_prompt_catalog": invalid}})
    with pytest.raises(GenerationPlanValidationError):
        dialog._apply_loaded_plan(bad, tmp_path / "invalid.json", "generated-token")
    assert dialog.plan.digest == before and dialog.prompt_knowledge is native
    dialog.close(); wait_until(app, lambda: not dialog._pending)
    jobs.shutdown(wait=True)
