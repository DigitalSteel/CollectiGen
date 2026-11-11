"""Focused Manager Generator Setup routes using generated stores and fake runtime I/O."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from core.swarm_runtime import SwarmRuntimeManager, SwarmSnapshot
from core.settings import AppSettings
from qt_app.swarm_runtime_widget import SwarmRuntimeWidget
from suite_core.manager.window import create_window, unconfigured


def application():
    app = QApplication.instance() or QApplication([])
    # The offscreen plugin does not load the Windows system font database.
    if not getattr(app, "_setup_font_loaded", False):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
        if font.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font))
            families = QFontDatabase.applicationFontFamilies(font_id)
            assert families, "The generated UI capture needs a readable font"
            app.setFont(QFont(families[0], 10))
        app.setStyle("Fusion")
        app._setup_font_loaded = True
    return app


def wait_until(app, predicate, timeout=8):
    end = monotonic() + timeout
    while not predicate():
        assert monotonic() < end, "Generated setup fixture timed out"
        app.processEvents()
        QTest.qWait(10)


def capture(widget, name):
    output = os.environ.get("COLLECTIGEN_SETUP_CAPTURES")
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(Path(output) / (name + ".png")))


@pytest.mark.parametrize("ownership", ["adopted", "managed", "unknown"])
def test_generation_connection_uses_healthy_existing_and_managed(tmp_path, ownership):
    manager = SwarmRuntimeManager(tmp_path / "runtime.json")
    record = dict(ownership=ownership, endpoint="http://127.0.0.1:7801", server_id="fixture",
        last_health_check="2026-11-11", backend_ids=["one"], control_plane_online=True,
        backend_state="ready", compatibility_state="supported")
    manager._load = lambda: (record, "")
    expected = (record["endpoint"], "fixture")
    assert manager.checked_generation_connection() == (expected if ownership in {"adopted", "managed"} else None)
    assert manager.checked_adopted_connection() == (expected if ownership == "adopted" else None)
    for key, value in [("lifecycle_pending", "restart"), ("backend_state", "offline"),
                       ("compatibility_state", "unknown"), ("last_health_check", "")]:
        original = record.get(key)
        record[key] = value
        assert manager.checked_generation_connection() is None
        record[key] = original


class FixtureRuntime:
    def __init__(self, path):
        self.record_path = path
        self.configured = True
    def snapshot(self):
        if not self.configured:
            return SwarmSnapshot("unconfigured", "Not connected", "Choose an existing SwarmUI installation or install one.", False)
        return SwarmSnapshot("ready", "Ready", "Generated local generator; no runtime contacted.",
            True, install_root="C:/Generated-SwarmUI", ownership="managed",
            control_plane_online=True, backend_state="ready", work_status_known=True,
            compatibility_state="supported")
    def checked_generation_connection(self):
        return "http://127.0.0.1:7801", "fixture-server"


@pytest.mark.parametrize("configured", [False, True])
def test_manager_visible_full_setup_scopes_models_import_and_close(tmp_path, monkeypatch, configured):
    app = application()
    import core.swarm_runtime as runtime
    import qt_app.swarm_onboarding_workflow as onboarding
    import qt_app.swarm_model_paths_dialog as paths
    def manager_factory(path):
        manager = FixtureRuntime(path)
        manager.configured = configured
        return manager
    monkeypatch.setattr(runtime, "SwarmRuntimeManager", manager_factory)
    calls = []
    class Paths(QDialog):
        def __init__(self, manager, parent, **kwargs):
            super().__init__(parent); calls.append(("paths", kwargs))
        def load_current(self): pass
    def import_setup(owner, registry, jobs, **kwargs):
        calls.append(("import", dict(registry=registry, jobs=jobs, **kwargs)))
        child = QDialog(owner); child.show(); return child
    monkeypatch.setattr(paths, "SwarmModelPathsDialog", Paths)
    monkeypatch.setattr(onboarding, "open_swarm_onboarding", import_setup)
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1600, 950); window.show()
    item = window.navigation.item(window.page_rows["Applications"])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
        pos=window.navigation.visualItemRect(item).center())
    button = next(b for b in window.findChildren(QPushButton) if b.text().startswith("Generator Setup"))
    errors = []
    def drive():
        dialog = window._generation_setup_dialog
        if dialog is None:
            QTimer.singleShot(10, drive); return
        try:
            panel = dialog.review
            assert not panel.adoption_only
            assert [panel.task_nav.item(i).text() for i in range(panel.task_nav.count())] == [
                "Connect or install", "Run and status", "Models and import", "Maintenance"]
            if not configured:
                assert panel.install_button.isVisible() and panel.install_button.isEnabled()
                assert panel.use_existing_button.isEnabled()
                capture(dialog, "first-time")
                return
            for index, name in enumerate(["connect", "run", "models", "maintenance"]):
                QTest.mouseClick(panel.task_nav.viewport(), Qt.LeftButton,
                    pos=panel.task_nav.visualItemRect(panel.task_nav.item(index)).center())
                app.processEvents()
                assert panel.task_stack.currentIndex() == index
                capture(dialog, name)
                if index == 0: assert panel.install_button.isVisible()
                if index == 2:
                    QTest.mouseClick(panel.model_paths_button, Qt.LeftButton)
                    panel._model_paths_dialog.close()
                    QTest.mouseClick(panel.onboarding_button, Qt.LeftButton)
                    assert panel._onboarding_dialog.isVisible()
            state = window.generation_state()
            assert state.settings is panel.settings
            assert Path(state.settings.ai_models_directory) == tmp_path / "Manager/models"
            for kind, args in calls:
                registry = args["model_registry"] if kind == "paths" else args["registry"]
                assert registry.path == tmp_path / "Manager/model_resources.json"
                assert args["jobs"] is state.jobs
                if kind == "import":
                    assert args["settings_path"] == tmp_path / "Manager/generation-settings.json"
                    assert args["generation_storage_root"] == tmp_path / "Manager/generation"
            assert len(calls) == 2
            state.settings.ai_models_directory = str(tmp_path / "Chosen-models")
            state.settings.save(state.generation_settings_path)
            assert AppSettings.load(state.generation_settings_path).ai_models_directory == str(tmp_path / "Chosen-models")
            assert window.checked_image_connection() == ("http://127.0.0.1:7801", "fixture-server")
        except Exception as exc:
            errors.append(exc)
        finally:
            dialog.reject()
    QTimer.singleShot(10, drive)
    QTest.mouseClick(button, Qt.LeftButton)
    assert not errors, errors
    assert window._generation_setup_dialog is None
    window.close(); app.processEvents()


def test_setup_pending_close_feedback_and_return(tmp_path):
    app = application()
    entered, release = Event(), Event()
    results = []
    panel = SwarmRuntimeWidget(AppSettings(ai_models_directory=str(tmp_path)),
        manager=FixtureRuntime(tmp_path / "runtime.json"))
    panel.resize(1000, 760); panel.show()
    panel.set_request_return_available(True)
    def operation():
        entered.set()
        assert release.wait(6)
        return "done"
    try:
        panel._run("Checking generated connection", operation, results.append)
        wait_until(app, lambda: entered.is_set() and "Elapsed 1s" in panel.state_label.text())
        assert panel.activity.isVisible()
        assert not panel.return_to_request_button.isEnabled()
        assert not panel.request_setup_close(lambda: None)
        capture(panel, "delayed-operation")
    finally:
        release.set()
    wait_until(app, lambda: panel._task is None)
    assert results == ["done"]
    assert not panel.activity.isVisible()
    assert panel.return_to_request_button.isEnabled()
    returned = []
    panel.return_requested.connect(lambda: returned.append(True))
    QTest.mouseClick(panel.return_to_request_button, Qt.LeftButton)
    assert returned == [True]
    assert panel.request_setup_close(lambda: None)
    panel.close()


def test_ended_host_does_not_run_queued_work_or_publish_feedback(tmp_path, monkeypatch):
    app = application()
    allowed = [True]; calls = []; tasks = []
    panel = SwarmRuntimeWidget(None, manager=FixtureRuntime(tmp_path / "runtime.json"),
        action_allowed=lambda: allowed[0])
    panel.pool = NS(start=tasks.append)
    monkeypatch.setattr("qt_app.swarm_runtime_widget.QMessageBox.warning", lambda *a: calls.append("warning"))
    panel._run("Queued check", lambda: calls.append("operation"), lambda _: calls.append("completed"))
    allowed[0] = False
    tasks[0].run(); app.processEvents()
    assert panel._task is None
    panel._run("No longer current", lambda: calls.append("operation"), calls.append)
    assert len(tasks) == 1 and calls == []
    panel.close()


def test_image_plan_visible_setup_return_preserves_saved_draft(tmp_path, monkeypatch):
    app = application()
    import core.swarm_runtime as runtime
    from qt_app.generation_saved_work_dialog import SavedGenerationWorkDialog
    from core.generation_planning import load_plan_with_digest
    monkeypatch.setattr(runtime, "SwarmRuntimeManager", FixtureRuntime)
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1600, 950); window.show()
    item = window.navigation.item(window.page_rows["Applications"])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
        pos=window.navigation.visualItemRect(item).center())
    def create_new():
        picker = next((w for w in app.topLevelWidgets()
            if isinstance(w, SavedGenerationWorkDialog) and w.isVisible()), None)
        if picker is None or not picker.create_setup_button.isEnabled():
            QTimer.singleShot(20, create_new); return
        QTest.mouseClick(picker.create_setup_button, Qt.LeftButton)
    QTimer.singleShot(20, create_new)
    QTest.mouseClick(window.generate_plan_button, Qt.LeftButton)
    wait_until(app, lambda: getattr(window, "_generation_dialog", None) is not None)
    draft = window._generation_dialog
    wait_until(app, lambda: not draft._pending)
    draft.positive.setFocus()
    QTest.keyClicks(draft.positive, "Generated fixture: a blue ceramic cup")
    try:
        wait_until(app, lambda: draft._draft_path.exists() and not draft._pending
            and load_plan_with_digest(draft._draft_path)[0].request.positive_prompt
                == "Generated fixture: a blue ceramic cup", timeout=30)
    except AssertionError:
        capture(draft, "draft-save-failure")
        raise AssertionError((draft.draft_status.text(), draft.status.text(),
            draft.positive.toPlainText(), draft.plan.request.positive_prompt,
            draft._pending, draft._draft_inputs_valid()))
    path = draft._draft_path
    before, token = load_plan_with_digest(path)
    errors = []
    def return_from_setup():
        setup = window._generation_setup_dialog
        if setup is None:
            QTimer.singleShot(20, return_from_setup); return
        try:
            assert setup.review.return_to_request_button.isVisible()
            assert setup.review._generation_storage_root == tmp_path / "Manager/generation"
            capture(setup, "plan-return")
            QTest.mouseClick(setup.review.return_to_request_button, Qt.LeftButton)
        except Exception as exc:
            errors.append(exc); setup.reject()
    QTimer.singleShot(20, return_from_setup)
    QTest.mouseClick(draft.review_connection_button, Qt.LeftButton)
    assert not errors, errors
    assert window._generation_dialog is draft and draft._draft_path == path
    assert draft.positive.toPlainText() == before.request.positive_prompt
    after, _ = load_plan_with_digest(path)
    assert after.request.positive_prompt == before.request.positive_prompt
    capture(draft, "returned-draft")
    draft.close()
    wait_until(app, lambda: not draft.isVisible())
    window.close(); app.processEvents()


def test_setup_failure_remains_retryable(tmp_path, monkeypatch):
    app = application()
    panel = SwarmRuntimeWidget(None, manager=FixtureRuntime(tmp_path / "runtime.json"))
    messages, results = [], []
    monkeypatch.setattr("qt_app.swarm_runtime_widget.QMessageBox.warning", lambda *args: messages.append(args[-1]))
    def unavailable():
        raise RuntimeError("Generated connection unavailable; retry after starting your generator.")
    panel._run("Checking connection", unavailable, results.append)
    wait_until(app, lambda: panel._task is None)
    assert len(messages) == 1 and "unavailable" in messages[0]
    assert panel.health_button.isEnabled() and not panel.activity.isVisible()
    panel._run("Checking again", lambda: "ready", results.append)
    wait_until(app, lambda: panel._task is None)
    assert results == ["ready"]
    panel.close()
