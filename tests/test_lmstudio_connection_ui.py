"""Synthetic source UI only; no main-window/native/server qualification."""
import json
import os
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import AppSettings
from core.lmstudio_connection_settings import KEY, LmStudioConnectionSettings
from qt_app.lmstudio_connection_dialog import LmStudioConnectionDialog
from qt_app.theme import build_stylesheet, _light_palette, _dark_palette


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    assert QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf") >= 0
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    return app


@pytest.fixture(autouse=True)
def clean_owned_dialogs(app):
    from tools.qt_test_support import QtTestScope
    app._connection_test_dialogs = []
    with QtTestScope(app):
        yield
        for dialog in app._connection_test_dialogs:
            settled(dialog)
            dialog._base = dialog._values()
            dialog.reject()
    app._connection_test_dialogs = []


def settled(dialog):
    for _ in range(400):
        QTest.qWait(10)
        if dialog._task is None:
            return
    pytest.fail("owned preference task did not settle")


def open_dialog(tmp_path, app, payload=None, service=None):
    target = tmp_path / "settings.json"
    if payload is not None:
        target.write_bytes((json.dumps(payload) + "\n").encode())
    dialog = LmStudioConnectionDialog(service or LmStudioConnectionSettings(target))
    app._connection_test_dialogs.append(dialog)
    dialog.show()
    settled(dialog)
    return target, dialog


def type_value(control, text):
    control.setFocus()
    control.selectAll()
    QTest.keyClicks(control, text)


def edit(dialog, key="synthetic-model"):
    type_value(dialog.endpoint, "http://127.0.0.1:1234")
    type_value(dialog.model_key, key)


def choose_message(text):
    def choose():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            capture(box, "close-" + text.replace(" ", "-").lower())
            button = next(b for b in box.buttons() if b.text() == text)
            QTest.mouseClick(button, Qt.LeftButton)
    QTimer.singleShot(50, choose)


def capture(widget, name):
    destination = os.environ.get("COLLECTIGEN_UI_EVIDENCE")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(path / (name + ".png")))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_save_unrelated_save_reopen_forget_reopen(app, tmp_path, theme, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network request"))
    app.setPalette(_light_palette() if theme == "light" else _dark_palette())
    app.setStyleSheet(build_stylesheet(theme))
    target, dialog = open_dialog(tmp_path, app, {"ui_theme": "dark", "future": {"retained": True}})
    stale = AppSettings.load(target)
    assert not dialog.save_button.isEnabled() and not dialog.forget_button.isEnabled()
    capture(dialog, theme + "-empty")
    edit(dialog)
    assert dialog.save_button.isEnabled()
    capture(dialog, theme + "-dirty")
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    settled(dialog)
    assert "saved" in dialog.status.text() and not dialog.save_button.isEnabled()
    capture(dialog, theme + "-saved")
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)
    assert not dialog.isVisible()
    stale.ui_theme = "light"
    stale.save(target)
    target, reopened = open_dialog(tmp_path, app)
    assert reopened.model_key.text() == "synthetic-model"
    QTest.mouseClick(reopened.forget_button, Qt.LeftButton)
    settled(reopened)
    assert "forgotten" in reopened.status.text()
    assert not reopened.endpoint.text() and KEY not in json.loads(target.read_bytes())
    QTest.mouseClick(reopened.close_button, Qt.LeftButton)
    target, final = open_dialog(tmp_path, app)
    assert final._snapshot.selection is None
    assert json.loads(target.read_bytes())["future"] == {"retained": True}
    QTest.mouseClick(final.close_button, Qt.LeftButton)


def test_invalid_draft_and_dirty_escape_keep_then_discard(app, tmp_path):
    target, dialog = open_dialog(tmp_path, app)
    type_value(dialog.endpoint, "http://example.com:1234")
    assert not dialog.save_button.isEnabled()
    QTest.qWait(30)
    bounds = dialog.status.fontMetrics().boundingRect(dialog.status.contentsRect(), Qt.TextWordWrap, dialog.status.text())
    assert bounds.height() <= dialog.status.contentsRect().height()
    capture(dialog, "invalid-draft")
    choose_message("Keep Editing")
    QTest.keyClick(dialog, Qt.Key_Escape)
    assert dialog.isVisible() and dialog.endpoint.text() == "http://example.com:1234"
    choose_message("Discard Changes")
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)
    assert not dialog.isVisible() and not target.exists()


def test_stale_refusal_preserves_draft_until_explicit_reload(app, tmp_path):
    target, dialog = open_dialog(tmp_path, app, {"ui_theme": "dark"})
    edit(dialog)
    target.write_bytes(b'{"ui_theme":"light","foreign":true}\n')
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    settled(dialog)
    assert dialog._held and dialog.model_key.text() == "synthetic-model"
    assert not dialog.save_button.isEnabled()
    capture(dialog, "stale-reload-required")
    choose_message("Discard Changes")
    QTest.mouseClick(dialog.reload_button, Qt.LeftButton)
    settled(dialog)
    assert not dialog._held and not dialog.endpoint.text()
    assert json.loads(target.read_bytes())["foreign"] is True
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)


@pytest.mark.parametrize("payload", [{KEY: {"schema_version": 99}}, {KEY: None}])
def test_malformed_or_future_settings_never_cleared(app, tmp_path, payload):
    target, dialog = open_dialog(tmp_path, app, payload)
    before = target.read_bytes()
    assert dialog._held and not dialog.endpoint.isEnabled()
    assert not dialog.forget_button.isEnabled()
    capture(dialog, "unsupported-schema")
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)
    assert target.read_bytes() == before


@pytest.mark.parametrize("operation", ["read", "save", "forget"])
def test_delayed_stock_worker_keeps_close_and_repeat_submit_held(app, tmp_path, operation):
    target = tmp_path / "settings.json"
    store = LmStudioConnectionSettings(target)
    if operation == "forget":
        store.save_selection("http://127.0.0.1:1234", expected=store.observe())
    gate = Event()
    started = Event()
    calls = []
    class Delayed:
        def observe(self):
            if operation == "read":
                started.set(); gate.wait(5)
            return store.observe()
        def save_selection(self, *a, **k):
            calls.append("save"); started.set(); gate.wait(5)
            return store.save_selection(*a, **k)
        def forget_selection(self, **k):
            calls.append("forget"); started.set(); gate.wait(5)
            return store.forget_selection(**k)
    dialog = LmStudioConnectionDialog(Delayed())
    app._connection_test_dialogs.append(dialog)
    dialog.show()
    try:
        if operation != "read":
            settled(dialog)
            if operation == "save":
                edit(dialog); QTest.mouseClick(dialog.save_button, Qt.LeftButton)
            else:
                QTest.mouseClick(dialog.forget_button, Qt.LeftButton)
        for _ in range(100):
            QTest.qWait(10)
            if started.is_set(): break
        assert started.is_set() and not dialog._task.is_finished()
        assert not dialog.close_button.isEnabled()
        dialog.close()
        QTest.keyClick(dialog, Qt.Key_Escape)
        assert dialog.isVisible() and dialog._task is not None
        assert not dialog.save_button.isEnabled() and not dialog.forget_button.isEnabled()
        QTest.qWait(60)
        assert "Elapsed" in dialog.status.text()
        assert "close after" in dialog.status.text()
        capture(dialog, "busy-" + operation)
    finally:
        gate.set()
        settled(dialog)
    assert len(calls) == int(operation != "read")
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)


def test_safe_failure_does_not_display_filesystem_details(app, tmp_path):
    class Unavailable:
        def observe(self): raise OSError("synthetic-private-detail")
    _, dialog = open_dialog(tmp_path, app, service=Unavailable())
    assert "synthetic-private-detail" not in dialog.status.text()
    assert "could not" in dialog.status.text()
    capture(dialog, "read-unavailable")
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)


def test_write_readback_restore_is_visible_as_failure(app, tmp_path, monkeypatch):
    target, dialog = open_dialog(tmp_path, app, {"ui_theme": "dark"})
    before = target.read_bytes()
    edit(dialog)
    read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k: "{}" if p == target else read(p, *a, **k))
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    settled(dialog)
    assert dialog._held and target.read_bytes() == before
    assert "saved." not in dialog.status.text()
    capture(dialog, "write-restored-failure")
    choose_message("Discard Changes")
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_original_setup_visible_entry_preserves_local_ai_draft(app, tmp_path, theme):
    app.setPalette(_light_palette() if theme == "light" else _dark_palette())
    app.setStyleSheet(build_stylesheet(theme))
    from types import SimpleNamespace
    from qt_app.pages import SetupPage
    settings = AppSettings._from_payload({"ui_theme": "light", "ai_models_directory": str(tmp_path / "models")})
    fake = SimpleNamespace(wd_model_dir_for=lambda _key: tmp_path / "models",
        capability_profile="organizer-image", components=lambda **_k: [],
        component_pack_available=lambda: False, recovered_optional_components=False,
        optional_cleanup_pending=False, recovered_removals=(), recovered_installs=())
    page = SetupPage(settings, defer_expensive_initialization=True,
        dependency_service_factory=lambda _settings: fake,
        connection_settings_path=tmp_path / "settings.json")
    page.resize(1920, 1080)
    page.show()
    QTest.qWait(50)
    QTest.mouseClick(page.workflow_default_buttons["analysis"], Qt.LeftButton)
    bar = page.ai_sections.tabBar()
    index = next(i for i in range(bar.count()) if bar.tabText(i) == "LM Studio Connection")
    QTest.mouseClick(bar, Qt.LeftButton, pos=bar.tabRect(index).center())
    assert page.lm_connection_button.isVisible()
    original_model = page.plain_language_model.currentData()
    original_prompt = page.joy_prompt.toPlainText()
    capture(page, "setup-original-connection-entry-" + theme)
    errors = []
    def use_dialog():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, LmStudioConnectionDialog)
            settled(dialog)
            edit(dialog)
            QTest.mouseClick(dialog.save_button, Qt.LeftButton)
            settled(dialog)
            assert "saved" in dialog.status.text()
            QTest.mouseClick(dialog.close_button, Qt.LeftButton)
        except BaseException as exc:
            errors.append(exc)
            settled(dialog)
            dialog._base = dialog._values()
            dialog.reject()
    QTimer.singleShot(50, use_dialog)
    QTest.mouseClick(page.lm_connection_button, Qt.LeftButton)
    assert not errors, errors
    assert page.plain_language_model.currentData() == original_model
    assert page.joy_prompt.toPlainText() == original_prompt
    assert LmStudioConnectionSettings(tmp_path / "settings.json").observe().selection is not None
    page.close()


@pytest.mark.parametrize("key", ["", "synthetic/" + "x" * 120])
def test_optional_and_long_exact_key_stays_editable(app, tmp_path, key):
    target, dialog = open_dialog(tmp_path, app)
    edit(dialog, key)
    assert dialog.save_button.isEnabled()
    capture(dialog, "optional-key" if not key else "long-key")
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    settled(dialog)
    assert dialog._snapshot.selection.preferred_model_key == (key or None)
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)
