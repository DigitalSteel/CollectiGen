"""Standalone Help adapters over the canonical Qt dialogs; source evidence only."""
from pathlib import Path
import os
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget, QMenu, QMessageBox, QDialogButtonBox
from suite_core.media_organizer import entry
from qt_app.main_window import TagManagerWindow


class HelpShell(QWidget):
    about_dialog = TagManagerWindow.about_dialog
    show_about = TagManagerWindow.show_about
    show_release_notes = TagManagerWindow.show_release_notes
    def __init__(self, **kwargs):
        super().__init__()
        self.startup_cancelled = True
    def _build_menu(self):
        menu = QMenu(self)
        for label, callback in (("Plan Image Generation\u2026", lambda: None),
                                ("About CollectiGen", self.show_about),
                                ("What's New in CollectiGen v0.102.3-alpha.1", self.show_release_notes),
                                ("Exit", self.close)):
            action = QAction(label, self); action.triggered.connect(callback); menu.addAction(action)
        self._application_menus = (menu,)


@pytest.fixture
def window(qt_test_scope):
    if os.environ.get("COLLECTIGEN_ORGANIZER_FIX_CAPTURE_DIR"):
        from PySide6.QtGui import QFont, QFontDatabase
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        assert QFontDatabase.addApplicationFont(str(font)) >= 0
        qt_test_scope.app.setFont(QFont("Segoe UI", 10))
    cls = entry.make_window_type(HelpShell, current_application=lambda: qt_test_scope.app)
    widget = cls(); widget._build_menu(); qt_test_scope.show(widget)
    return widget


def test_about_uses_component_identity_and_supported_capabilities(window):
    box = window.about_dialog()
    assert box.windowTitle() == "About Media Organizer"
    assert f"Media Organizer {entry.DISPLAY_VERSION}" in box.text()
    assert "Dataset planning" in box.text() and "training-data export" in box.text()
    assert "Generation and Training execution are unavailable" in box.text()
    assert "Local SDXL 1.0 LoRA training (supported recipe)" not in box.text()
    assert "AGPL-3.0-or-later" in box.text() and "CC-BY-SA-4.0" in box.text()
    original = TagManagerWindow.about_dialog(window)
    assert "CollectiGen" in original.text() and "Local SDXL 1.0 LoRA training" in original.text()
    capture = os.environ.get("COLLECTIGEN_ORGANIZER_FIX_CAPTURE_DIR")
    if capture:
        directory = Path(capture); directory.mkdir(parents=True, exist_ok=True)
        box.show(); QTest.qWait(50); box.grab().save(str(directory / "about.png"))
    box.close(); original.close()


def test_notes_use_packaged_component_resource_without_shared_history(window, monkeypatch):
    import core.release_notes
    monkeypatch.setattr(core.release_notes, "load_release_history", lambda *a, **k: pytest.fail("borrowed shared history"))
    dialog = window.release_notes_dialog()
    expected = Path(entry.__file__).with_name("WHATS_NEW.md").read_text(encoding="utf-8-sig").strip()
    assert dialog.entries[0].version == entry.APP_VERSION
    assert dialog.entries[0].released_at == ""
    assert dialog.entries[0].notes == expected
    assert dialog.windowTitle() == f"What's New in Media Organizer {entry.DISPLAY_VERSION}"
    assert dialog.release_chooser.itemText(0) == f"Media Organizer {entry.DISPLAY_VERSION}"
    assert "Media Organizer" in dialog.viewer.toPlainText()
    capture = os.environ.get("COLLECTIGEN_ORGANIZER_FIX_CAPTURE_DIR")
    if capture:
        dialog.resize(900, 700); dialog.show(); QTest.qWait(50)
        dialog.grab().save(str(Path(capture) / "whats-new.png"))
    dialog.close()


@pytest.mark.parametrize("content", [None, " \n"])
def test_missing_or_empty_own_notes_refuse_without_full_app_fallback(window, monkeypatch, tmp_path, content):
    monkeypatch.setattr(entry, "__file__", str(tmp_path / "entry.py"))
    if content is not None: (tmp_path / "WHATS_NEW.md").write_text(content)
    with pytest.raises((OSError, ValueError)): window.release_notes_dialog()


def test_existing_help_actions_dispatch_once_and_qtest_closes_stock_dialogs(window, qt_test_scope):
    actions = window._application_menus[0].actions()
    assert not actions[0].isVisible()  # real Unicode ellipsis, unlike the old ASCII-only filter
    assert actions[1].text() == "About Media Organizer"
    assert actions[2].text() == f"What's New in Media Organizer {entry.DISPLAY_VERSION}"
    assert actions[3].text() == "Exit" and actions[3].isVisible()
    observed = []
    def close_about():
        box = qt_test_scope.app.activeModalWidget(); observed.append(box.windowTitle())
        QTest.mouseClick(box.button(QMessageBox.Ok), Qt.LeftButton)
    QTimer.singleShot(50, close_about); actions[1].trigger()
    def close_notes():
        dialog = qt_test_scope.app.activeModalWidget(); observed.append(dialog.windowTitle())
        button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close)
        QTest.mouseClick(button, Qt.LeftButton)
    QTimer.singleShot(50, close_notes); actions[2].trigger()
    assert observed == ["About Media Organizer", f"What's New in Media Organizer {entry.DISPLAY_VERSION}"]
