"""Synthetic Organizer planning policy; no generator, model or private data."""
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QFileDialog, QMenu
from core.background_jobs import BackgroundJobManager
from core.generation_execution import SwarmLocalAdapter
from core.generation_planning import load_plan
from core.generation_saved_work import import_selected_saved_work
from qt_app.generation_dialog import GenerationDialog


def wait(predicate, seconds=30):
    end = monotonic() + seconds
    while monotonic() < end:
        value = predicate()
        if value:
            return value
        QTest.qWait(25)
    assert predicate(), "Synthetic workflow did not settle"


def click(widget):
    assert widget.isVisible() and widget.isEnabled(), widget
    QTest.mouseClick(widget, Qt.LeftButton)


def reject_runtime(monkeypatch):
    from qt_app import generation_workflow
    def forbidden(*_args, **_kwargs):
        pytest.fail("Organizer planning attempted generator access")
    monkeypatch.setattr(SwarmLocalAdapter, "__init__", forbidden)
    monkeypatch.setattr(generation_workflow, "_saved_runtime_connection", forbidden)


def test_offline_request_exports_and_manager_imports_without_runtime(monkeypatch, tmp_path, qt_test_scope):
    reject_runtime(monkeypatch)
    jobs = BackgroundJobManager()
    dialog = qt_test_scope.show(GenerationDialog(jobs, storage_root=tmp_path / "Organizer" / "generation",
        runtime_allowed=False), size=(1220, 1000))
    assert not dialog.runtime_allowed
    assert not dialog.check_button.isEnabled()
    assert not dialog.submit_button.isVisible()
    assert all(not dialog.tabs.isTabVisible(i) for i in (3, dialog._readiness_tab_index))
    for name in ("check_connection", "bind_local_model", "prepare_request", "submit_reviewed",
                 "recover_selected", "load_history", "open_experiment"):
        getattr(dialog, name)()
        assert not dialog._pending and dialog.adapter is None
    dialog.positive.setPlainText("A synthetic blue square under soft light")
    dialog.negative.setPlainText("synthetic avoid text")
    QTest.qWait(100)
    click(dialog.more_draft_actions_button)
    exported = tmp_path / "request-for-manager.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(exported), ""))
    click(dialog.export_button)
    wait(lambda: exported.exists() and not dialog._pending)
    original = load_plan(exported)
    imported = Path(import_selected_saved_work(tmp_path / "Manager" / "generation", "drafts", exported))
    assert imported != exported and load_plan(imported).request == original.request
    assert original.request.positive_prompt == "A synthetic blue square under soft light"
    assert original.request.negative_prompt == "synthetic avoid text"
    assert dialog.grab().save(str(tmp_path / "offline-export.png"))
    dialog.close()
    wait(lambda: dialog._closed)
    jobs.shutdown()


def test_default_generation_capability_is_preserved(monkeypatch, tmp_path, qt_test_scope):
    jobs = BackgroundJobManager()
    dialog = qt_test_scope.show(GenerationDialog(jobs, storage_root=tmp_path / "generation"))
    assert dialog.runtime_allowed and dialog.check_button.isEnabled()
    calls = []
    monkeypatch.setattr(dialog, "_run", lambda *a, **k: calls.append(a[0]))
    dialog.check_connection()
    assert calls == ["probe"]
    dialog.close()
    jobs.shutdown()


def test_organizer_visible_remix_and_video_guidance(monkeypatch, tmp_path, qt_test_scope):
    from core.app_state import AppState
    from core.edit_service import MoveHistoryService
    from core.image_record import ImageRecord
    from core.library_profiles import LibraryProfile, LibraryProfileRegistry, LibraryRole
    from core.settings import AppSettings
    from core.tag_database import TagDatabase
    from qt_app.main_window import TagManagerWindow
    from qt_app import generation_workflow, video_generation_workflow
    from suite_core.media_organizer.entry import make_window_type
    reject_runtime(monkeypatch)
    monkeypatch.setattr(generation_workflow, "USER_DATA_DIR", tmp_path / "Organizer")
    monkeypatch.setattr(generation_workflow, "MODEL_RESOURCES_PATH", tmp_path / "models.json")
    library = tmp_path / "Synthetic Library"
    library.mkdir()
    image = library / "synthetic.png"
    Image.new("RGB", (128, 128), "steelblue").save(image)
    record = ImageRecord(image)
    record.annotation.sdxl_tags = ["synthetic blue square"]
    record.save()
    sidecar = image.with_suffix(".tags.json")
    before = {p: p.read_bytes() for p in (image, sidecar)}
    profile = LibraryProfile.create("Synthetic Library", library, LibraryRole.CLOUD_SYNCED)
    settings = AppSettings()
    settings.set_library_registry(LibraryProfileRegistry([profile]), active_library_id=profile.stable_id)
    settings.library_scope_ids = [profile.stable_id]
    settings.restore_last_dataset = True
    settings.check_dependencies_on_startup = False
    settings.check_updates_on_startup = False
    settings.gallery_blur_unknown = False
    state = AppState(settings=settings, knowledge=TagDatabase(tmp_path / "tags.json"),
        moves=MoveHistoryService(tmp_path / "history.json", recover=False))
    font = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    assert font >= 0
    qt_test_scope.app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0], 10))
    Organizer = make_window_type(TagManagerWindow, current_application=lambda: qt_test_scope.app)
    window = qt_test_scope.show(Organizer(settings=settings, state=state, genuinely_new_profile=False), size=(1920, 1080))
    assert state.generation_runtime_allowed is False
    assert state.video_generation_runtime_allowed is False
    item = next(window.navigation.item(i) for i in range(window.navigation.count())
                if window.navigation.item(i).text().strip() == "Dataset")
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
        pos=window.navigation.visualItemRect(item).center())
    page = wait(lambda: getattr(window, "dataset_page", None))
    refresh = next(button for button in window.findChildren(QAbstractButton)
        if button.text() == "Refresh Libraries" and button.isVisible())
    click(refresh)
    wait(lambda: window._scan_thread is None and image in page.dataset.images)
    tabs = page.stage_tabs
    index = next(i for i in range(tabs.count()) if tabs.tabData(i) == "partial")
    QTest.mouseClick(tabs, Qt.LeftButton, pos=tabs.tabRect(index).center())
    card = wait(lambda: page.cards.get(str(image)))
    click(card.preview)
    wait(lambda: page.current_path == image and page.current_record is not None)
    wait(lambda: not any(getattr(page, name, None) is not None for name in (
        "_projection_task", "_library_move_task", "_preparation_task", "_batch_task",
        "_organization_transfer_task", "_training_export_task", "_training_candidate_task", "_metadata_backup_task")))
    if not page.inspector_actions.isVisible():
        page.show_current_inspector()
    wait(lambda: page.inspector_actions.isVisible())
    def choose_action(label):
        menu = page.inspector_actions.menu()
        action = next(a for a in menu.actions() if a.text() == label)
        wait(lambda: action.isEnabled())
        timer = qt_test_scope.own_timer(QTimer())
        started = monotonic()
        chosen = []
        def choose():
            if menu.isVisible():
                timer.stop()
                chosen.append(label)
                QTest.mouseClick(menu, Qt.LeftButton, pos=menu.actionGeometry(action).center())
            elif monotonic() - started > 3:
                timer.stop()
                menu.close()
        timer.timeout.connect(choose)
        timer.start(25)
        click(page.inspector_actions)
        wait(lambda: bool(chosen), seconds=5)
    # Library discovery may initialize its own metadata. Pin the source at
    # the actual image-action boundary, after that ordinary work settles.
    assert image.read_bytes() == before[image]
    assert ImageRecord(image).annotation.sdxl_tags == ["synthetic blue square"]
    before = {p: p.read_bytes() for p in (image, sidecar)}
    (tmp_path / "source-before-plan.tags.json").write_bytes(before[sidecar])
    choose_action("Remix This Image\u2026")
    try:
        dialog = wait(lambda: getattr(window, "_generation_dialog", None))
    except AssertionError:
        from PySide6.QtWidgets import QLabel
        print("Current action/task:", page.current_path, getattr(window, "_generation_source_task", None), flush=True)
        print("Visible windows:", [(x.windowTitle(), x.isVisible()) for x in qt_test_scope.app.topLevelWidgets()], flush=True)
        print("Status:", window.statusBar().currentMessage(), flush=True)
        print("Visible notices:", [x.text() for x in window.findChildren(QLabel) if x.isVisible()][-8:], flush=True)
        window.grab().save(str(tmp_path / "remix-failure.png"))
        raise
    assert not dialog.runtime_allowed and dialog.remix_source
    assert not dialog.submit_button.isVisible()
    assert dialog.source_identity
    assert dialog.grab().save(str(tmp_path / "organizer-remix.png"))
    # Unchanged source-bound draft can close without starting any execution.
    dialog.close()
    wait(lambda: getattr(window, "_generation_dialog", None) is None)
    messages = []
    monkeypatch.setattr(video_generation_workflow.QMessageBox, "information",
        lambda _p, title, message: messages.append((title, message)))
    choose_action("Animate This Image\u2026")
    wait(lambda: bool(messages))
    assert "Applications > Plan a Video" in messages[0][1]
    assert getattr(window, "_video_generation_dialog", None) is None
    assert {p: p.read_bytes() for p in before} == before
    assert window.grab().save(str(tmp_path / "organizer-return.png"))
    assert window.close()
    state.jobs.shutdown()


def test_video_default_keeps_its_separate_admission(monkeypatch, qt_test_scope):
    from PySide6.QtWidgets import QWidget
    from qt_app import video_generation_workflow as workflow
    host = qt_test_scope.show(QWidget())
    # Manager image execution has its own checked capability and must not
    # accidentally disable the separate, already-owned video admission.
    host.state = SimpleNamespace(jobs=object(), generation_runtime_allowed=False)
    seen = []
    sentinel = object()
    monkeypatch.setattr(workflow, "_open_prepared_video_generation_workspace",
        lambda *a, **k: (seen.append(k), sentinel)[1])
    assert workflow.open_video_generation_workspace(host) is sentinel
    assert len(seen) == 1
