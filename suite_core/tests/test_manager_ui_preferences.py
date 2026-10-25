"""Portable UI context survives ordinary Close without changing app settings."""
from datetime import datetime, timezone
import pytest

from PySide6.QtCore import QSettings, QByteArray
from PySide6.QtWidgets import QApplication

from suite_core.manager.ui_preferences import ManagerUiPreferences
from suite_core.manager.window import create_window, unconfigured


def make_window():
    return create_window(unconfigured(datetime.now(timezone.utc)))


def test_ordinary_close_and_new_manager_restore_page_and_size_preserve_unknown_ini(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'settings.ini'
    path.write_text('[Future]\nretain=unknown\n', encoding='utf-8')
    first = make_window()
    first.ui_preferences = ManagerUiPreferences(path)
    first.ui_preferences.restore(first)
    first.show(); first.resize(720, 600)
    first.navigation.setCurrentRow(first.page_rows['Applications'])
    app.processEvents()
    assert first.close()
    app.processEvents()
    second = make_window()
    try:
        ManagerUiPreferences(path).restore(second)
        assert second.navigation.currentRow() == second.page_rows['Applications']
        # Fit the 800x800 offscreen screen; native acceptance checks 1080p.
        assert second.size() == first.size()
        assert QSettings(str(path), QSettings.IniFormat).value('Future/retain') == 'unknown'
        assert not (tmp_path / 'settings.json').exists()
        assert second.repository_inspection_button.isHidden()
        assert second.direct_model_button.isHidden()
    finally:
        second.close(); app.processEvents()


def test_unknown_page_and_untrusted_geometry_keep_fresh_defaults(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'settings.ini'
    settings = QSettings(str(path), QSettings.IniFormat)
    settings.setValue('ManagerWindow/page', 'Unknown future page')
    settings.setValue('ManagerWindow/geometry', QByteArray(b'x' * 5000))
    settings.sync()
    window = make_window()
    try:
        ManagerUiPreferences(path).restore(window)
        assert window.navigation.currentRow() == window.page_rows['Dashboard']
        assert window.width() == 1200 and window.height() == 780
    finally:
        window.close(); app.processEvents()


def test_context_guidance_clears_on_navigation_without_hiding_real_errors():
    app = QApplication.instance() or QApplication([])
    window = make_window()
    try:
        window.statusBar().showMessage('Use Databases to connect a catalog, review content and recover interrupted work.')
        window.navigation.setCurrentRow(window.page_rows['Servers'])
        assert window.statusBar().currentMessage() == ''
        window.statusBar().showMessage('Synthetic save failed; review the retained work.')
        window.navigation.setCurrentRow(window.page_rows['Dashboard'])
        assert window.statusBar().currentMessage() == 'Synthetic save failed; review the retained work.'
    finally:
        window.close(); app.processEvents()


def test_malformed_ini_is_retained_without_blocking_default_window(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'settings.ini'
    original = b'[unclosed section\nretain=unknown\n'
    path.write_bytes(original)
    window = make_window()
    try:
        preferences = ManagerUiPreferences(path)
        preferences.restore(window)
        assert window.navigation.currentRow() == window.page_rows['Dashboard']
        with pytest.raises(OSError, match='existing INI retained'):
            preferences.save(window)
        assert path.read_bytes() == original
    finally:
        window.close(); app.processEvents()
