"""Named Manager task homes; synthetic UI, no model or worker execution."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from suite_core.manager.window import create_window, unconfigured


@pytest.fixture
def manager_window(tmp_path):
    app = QApplication.instance() or QApplication([])
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1500, 960)
    window.show()
    app.processEvents()
    yield app, window
    window.close()
    app.processEvents()


def test_visible_workflow_navigation_keeps_one_panel_and_retained_routes(manager_window):
    app, window = manager_window
    # Existing component/setup route identities are preserved.
    for index, name in enumerate(("Dashboard", "Servers", "Applications", "Databases", "Dependencies", "Settings")):
        assert window.page_rows[name] == index
    assert window.generate_page.isAncestorOf(window.generation_panel)
    assert window.training_page.isAncestorOf(window.training_panel)
    assert not window.applications_page.isAncestorOf(window.generation_panel)
    assert not window.applications_page.isAncestorOf(window.training_panel)
    for name, panel, other in (("Generate", window.generation_panel, window.training_panel),
                                ("Training", window.training_panel, window.generation_panel)):
        item = window.navigation.item(window.page_rows[name])
        QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                        pos=window.navigation.visualItemRect(item).center())
        app.processEvents()
        assert window.stack.currentIndex() == window.page_rows[name]
        assert panel.isVisible() and panel.isEnabled()
        assert not other.isVisible()
    assert not hasattr(window, "state")  # Navigation itself does not start jobs or create a runtime.


def test_unconfigured_task_homes_do_not_enable_runtime(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = create_window(unconfigured(datetime.now(timezone.utc)))
    try:
        for name, panel in (("Generate", window.generation_panel), ("Training", window.training_panel)):
            window.navigation.setCurrentRow(window.page_rows[name])
            app.processEvents()
            assert not panel.isEnabled()
        assert not hasattr(window, "state")
    finally:
        window.close()
        app.processEvents()


def test_retained_drafts_switch_without_replacing_original_owners(manager_window):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QDialog
    app, window = manager_window

    class Draft(QDialog):
        workspace_closed = Signal()
        draft_picker_open = False
        def has_active_background_work(self): return False
        def closeEvent(self, event):
            self.workspace_closed.emit()
            event.accept()

    first, second, video = Draft(), Draft(), Draft()
    page = window.generation_page
    page.add_workspace(first, label="New Image")
    page.add_workspace(second, label="New Image")
    assert page.draft_choice.itemText(0) != page.draft_choice.itemText(1)
    page.add_workspace(video, label="Retained video", media_type="video", task="video")
    assert page.retained_workspaces() == (first, second, video)
    page.show_workspace(first)
    assert page.active_workspace() is first
    assert window._generation_dialog is first
    assert window._video_generation_dialog is video
    page.show_workspace(video)
    assert page.active_workspace() is video
    assert page.retained_workspaces() == (first, second, video)
    assert not page.begin_button.isVisible()  # Manager keeps its original launch controls.
    assert not hasattr(window, "state")


def test_close_cannot_drop_an_inactive_busy_draft(manager_window):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QDialog
    app, window = manager_window
    closed = []

    class Draft(QDialog):
        workspace_closed = Signal()
        draft_picker_open = False
        def __init__(self, name, busy=False):
            super().__init__()
            self.name, self.busy = name, busy
        def has_active_background_work(self): return self.busy
        def closeEvent(self, event):
            closed.append(self.name)
            self.workspace_closed.emit()
            event.accept()

    busy, visible = Draft("original", busy=True), Draft("newer")
    window.generation_page.add_workspace(busy, label="Original running request")
    window.generation_page.add_workspace(visible, label="Newer idle request")
    assert window.generation_page.active_workspace() is visible
    assert not window.close()
    assert window.isVisible() and not closed
    assert window.generation_page.active_workspace() is busy
    assert window.generation_page.retained_workspaces() == (busy, visible)
    busy.busy = False
    assert window.close()
    assert closed == ["original", "newer"]


def test_training_open_review_selection_retains_exact_controller_owner(manager_window):
    from PySide6.QtWidgets import QDialog
    app, window = manager_window
    first, second = QDialog(window), QDialog(window)
    first.setWindowTitle("Retained SDXL review")
    second.setWindowTitle("Retained comparison")
    calls = []
    class Reviews:
        def __init__(self, *dialogs): self.dialogs = list(dialogs)
        def open_reviews(self): return tuple(self.dialogs)
        def show_review(self, dialog):
            if dialog not in self.dialogs: return False
            calls.append(dialog)
            return True
        def has_active_background_work(self): return False
    preparation, comparison = Reviews(first), Reviews(second)
    window._sdxl_preparation_controller = preparation
    window._training_comparison_controller = comparison
    window.refresh_open_training_reviews()
    assert window.training_open_reviews.count() == 2
    window.training_open_reviews.setCurrentIndex(1)
    window.refresh_open_training_reviews()
    assert window.training_open_reviews.currentData() == (comparison, second)
    window.return_to_open_training_review()
    assert calls == [second]
    assert window.navigation.currentRow() == window.page_rows["Training"]
    # A review that finishes while selected is not reopened or duplicated.
    comparison.dialogs.clear()
    window.return_to_open_training_review()
    assert calls == [second]
    assert window.training_open_reviews.count() == 1
    preparation.dialogs.clear()
    window.refresh_open_training_reviews()
    assert not window.training_open_review_button.isEnabled()
    window._sdxl_preparation_controller = None
    window._training_comparison_controller = None
    first.close(); second.close()
