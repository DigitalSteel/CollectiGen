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


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("section,topic,required", (
    ("Generate", "generation", ("New or Saved Image", "Review Generation Jobs", "Media Organizer")),
    ("Training", "training", ("Review Training Package", "Review a Dataset Draft", "Review Saved Training Runs")),
))
def test_workflow_help_ribbon_f1_and_close_keep_scope(manager_window, tmp_path, theme, section, topic, required):
    from pathlib import Path
    from PySide6.QtWidgets import QAbstractButton, QTextBrowser
    from qt_app.theme import apply_theme
    from qt_app.help_content import manager_workspace_help
    app, window = manager_window
    from test_manager_comparison_queue import app as configure_ui_fonts
    assert configure_ui_fonts() is app
    apply_theme(app, theme)
    window.resize(1920, 1040)
    item = window.navigation.item(window.page_rows[section])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                    pos=window.navigation.visualItemRect(item).center())
    app.processEvents()
    selected = window.navigation.currentRow()
    control = window._workflow_help_controls[section]
    assert control.help_text == manager_workspace_help(topic)
    assert not control.isVisible()  # The command bar owns the one visible home.
    command, = (c for c in window.commands if c.key == "workflow-help")
    assert command.source is control and command.label == "Help for " + section
    tab = next(i for i in range(window.bar.tabs.count()) if window.bar.tabs.tabText(i) == "Help")
    QTest.mouseClick(window.bar.tabs, Qt.LeftButton, pos=window.bar.tabs.tabRect(tab).center())
    app.processEvents()
    visible = [b for b in window.bar.findChildren(QAbstractButton)
               if b.isVisible() and b.text() == "Help for " + section]
    assert len(visible) == 1
    out = Path(os.environ.get("CG_WORKFLOW_HELP_CAPTURES", str(tmp_path)))
    out.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(out / (section.lower() + "-" + theme + "-entry.png")))
    QTest.mouseClick(visible[0], Qt.LeftButton)
    app.processEvents()
    guide = control._help_dialog
    assert guide is not None and guide.isVisible() and guide.windowTitle() == section + " Help"
    text = "\n".join(b.toPlainText() for b in guide.findChildren(QTextBrowser))
    assert all(word in text for word in required), text
    assert "Dataset Collections" not in text  # No nonexistent all-in-one navigation.
    assert not hasattr(window, "state")  # Help starts no generator, trainer or model work.
    assert window.navigation.currentRow() == selected
    assert guide.grab().save(str(out / (section.lower() + "-" + theme + "-guide.png")))
    assert guide.close()
    app.processEvents()
    window.activateWindow(); window.setFocus(); app.processEvents()
    QTest.keyClick(window, Qt.Key_F1)
    app.processEvents()
    assert control._help_dialog is not None and control._help_dialog.isVisible()
    assert window.navigation.currentRow() == selected and not hasattr(window, "state")
    assert control._help_dialog.close()
    app.processEvents()
    assert window.grab().save(str(out / (section.lower() + "-" + theme + "-return.png")))
    # Other workspaces do not acquire a disabled/no-op Help command.
    other = window.navigation.item(window.page_rows["Dashboard"])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                    pos=window.navigation.visualItemRect(other).center())
    app.processEvents()
    assert not window._workflow_help_shortcut.isEnabled()
    assert not any(c.key == "workflow-help" for c in window.commands)
    # Normal Manager Close settles its own open guide as well.
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                    pos=window.navigation.visualItemRect(item).center())
    app.processEvents(); window.activateWindow(); window.setFocus(); app.processEvents()
    QTest.keyClick(window, Qt.Key_F1); app.processEvents()
    assert control._help_dialog.isVisible()
    assert window.close()
    app.processEvents()
    assert not window.isVisible()
    from shiboken6 import isValid
    remaining = control._help_dialog
    assert remaining is None or not isValid(remaining) or not remaining.isVisible()
