"""Visible Manager Settings workflow using generated settings only; no native actors."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from core.manager_appearance_settings import ManagerAppearanceSettings
from suite_core.manager.appearance_ui import AppearancePanel, restore_appearance
from suite_core.manager.window import create_window, unconfigured


@pytest.fixture
def app():
    from test_manager_generator_setup_ui import application
    value = application()  # Existing readable-font/Fusion offscreen setup.
    yield value
    restore_appearance(value, None)


def click(control, app):
    QTest.mouseClick(control, Qt.LeftButton, pos=QPoint(8, control.height()//2)); app.processEvents()


def choose_theme(panel, key, app):
    panel.theme.setFocus(); QTest.keyClick(panel.theme, Qt.Key_Home)
    for _ in range(panel.theme.findData(key)): QTest.keyClick(panel.theme, Qt.Key_Down)
    app.processEvents()


def settings_home(window, app):
    window.show(); app.processEvents()
    item = window.navigation.item(window.page_rows["Settings"])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                    pos=window.navigation.visualItemRect(item).center())
    app.processEvents()
    scroll = window.appearance_panel.parentWidget().parentWidget().parentWidget()
    assert isinstance(scroll, QScrollArea)
    scroll.ensureWidgetVisible(window.appearance_panel); app.processEvents()
    assert window.appearance_panel.isVisible()


def test_visible_settings_save_and_restart_restores_all_choices(tmp_path, app):
    path = tmp_path / "settings.json"; path.write_text('{"future":{"preserved":true}}')
    store = ManagerAppearanceSettings(path)
    snapshot, error, theme, palette, tooltips = restore_appearance(app, store)
    window = create_window(unconfigured(datetime.now(timezone.utc)), appearance_store=store,
        appearance_snapshot=snapshot, theme=theme, color_blind_mode=palette, show_tooltips=tooltips)
    window.resize(1500, 960)
    try:
        settings_home(window, app); panel = window.appearance_panel
        choose_theme(panel, "midnight", app); click(panel.palette, app); click(panel.tooltips, app)
        assert panel.save_button.isEnabled() and not path.read_text().count("midnight")
        click(panel.save_button, app)
        assert "saved and applied" in panel.status.text()
        assert not panel.save_button.isEnabled()
        payload = json.loads(path.read_bytes())
        assert payload == {"future": {"preserved": True}, "ui_theme": "midnight", "color_blind_mode": True, "show_tooltips": False}
    finally: window.close(); app.processEvents()
    fresh, error, theme, palette, tooltips = restore_appearance(app, ManagerAppearanceSettings(path))
    assert error is None and (theme, palette, tooltips) == ("midnight", True, False)
    reopened = create_window(unconfigured(datetime.now(timezone.utc)), appearance_store=store,
        appearance_snapshot=fresh, theme=theme, color_blind_mode=palette, show_tooltips=tooltips)
    reopened.resize(1500, 960)
    try:
        settings_home(reopened, app)
        assert reopened.appearance_panel.theme.currentData() == "midnight"
        assert reopened.appearance_panel.palette.isChecked() and not reopened.appearance_panel.tooltips.isChecked()
    finally: reopened.close(); app.processEvents()


def test_stale_save_explains_reload_and_keeps_external_bytes(tmp_path, app):
    path = tmp_path / "settings.json"; store = ManagerAppearanceSettings(path)
    panel = AppearancePanel(store, store.observe()); panel.show(); app.processEvents()
    try:
        choose_theme(panel, "dark", app)
        choose_theme(panel, "system", app)
        assert not panel.save_button.isEnabled() and "match the saved" in panel.status.text()
        choose_theme(panel, "dark", app)
        path.write_text('{"ui_theme":"light","future":123}'); raw = path.read_bytes()
        click(panel.save_button, app)
        assert path.read_bytes() == raw and "Reload saved choices" in panel.status.text()
        assert panel.theme.currentData() == "system" and not panel.save_button.isEnabled()
        click(panel.reload_button, app)
        assert panel.theme.currentData() == "light" and "reloaded and applied" in panel.status.text()
        click(panel.palette, app); click(panel.save_button, app)
        assert json.loads(path.read_bytes())["future"] == 123
    finally: panel.close()


def test_invalid_startup_never_replaces_settings_and_can_reload_after_repair(tmp_path, app):
    path = tmp_path / "settings.json"; path.write_bytes(b"broken")
    store = ManagerAppearanceSettings(path)
    snapshot, error, *_ = restore_appearance(app, store)
    panel = AppearancePanel(store, snapshot, error=error); panel.show(); app.processEvents()
    try:
        assert error and not panel.theme.isEnabled() and not panel.save_button.isEnabled()
        assert path.read_bytes() == b"broken" and panel.reload_button.isEnabled()
        path.write_text('{"ui_theme":"dark"}'); click(panel.reload_button, app)
        assert panel.theme.isEnabled() and panel.theme.currentData() == "dark"
    finally: panel.close()


def test_no_store_has_no_persistence_actions(app):
    panel = AppearancePanel()
    assert not panel.theme.isEnabled() and not panel.save_button.isEnabled() and not panel.reload_button.isEnabled()
    panel.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_manager_appearance_1080p_visible_state(tmp_path, app, theme):
    store = ManagerAppearanceSettings(tmp_path / "settings.json")
    snapshot = store.save({"ui_theme": theme}, expected=store.observe())
    restore_appearance(app, store)
    window = create_window(unconfigured(datetime.now(timezone.utc)), appearance_store=store, appearance_snapshot=snapshot, theme=theme)
    window.resize(1500, 960)
    try:
        settings_home(window, app); panel = window.appearance_panel
        assert window.rect().contains(panel.mapTo(window, panel.save_button.geometry().center()))
        assert panel.theme.width() >= 180 and panel.status.height() >= panel.status.fontMetrics().height()
        out = Path(os.environ.get("CG_APPEARANCE_CAPTURE_DIR", str(tmp_path)))
        out.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(out / ("manager-settings-"+theme+".png")))
    finally: window.close(); app.processEvents()
