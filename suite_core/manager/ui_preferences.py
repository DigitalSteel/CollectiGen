"""Window context in the explicitly admitted portable UI INI, never AppSettings."""
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import QApplication

from core.training_source_io import _assert_unredirected


class ManagerUiPreferences:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.suffix.casefold() != '.ini':
            raise ValueError('explicit_portable_ui_ini_required')
        _assert_unredirected(self.path)
        # Retain the same QSettings owner, including its sticky parse/error
        # status; a fresh object can see Qt's shared parsed cache without it.
        self.settings = QSettings(str(self.path), QSettings.IniFormat)

    def restore(self, window):
        _assert_unredirected(self.path)
        settings = self.settings
        geometry = settings.value('ManagerWindow/geometry')
        if settings.status() != QSettings.NoError:
            return  # A malformed UI INI must neither hide nor block Manager.
        if isinstance(geometry, QByteArray) and 0 < len(geometry) <= 4096:
            window.restoreGeometry(geometry)
            # A removed display must not hide the restored Manager.
            screens = QApplication.screens()
            if screens and not any(screen.availableGeometry().intersects(window.frameGeometry()) for screen in screens):
                window.move(screens[0].availableGeometry().topLeft())
        page = settings.value('ManagerWindow/page', 'Dashboard')
        if isinstance(page, str) and page in window.page_rows:
            window.navigation.setCurrentRow(window.page_rows[page])

    def save(self, window):
        _assert_unredirected(self.path)
        settings = self.settings
        settings.sync()
        settings.value('ManagerWindow/page')  # Force reading before any mutation.
        if settings.status() != QSettings.NoError:
            raise OSError('Manager window preferences could not be read; existing INI retained')
        settings.setValue('ManagerWindow/geometry', window.saveGeometry())
        row = window.navigation.currentRow()
        page = next((name for name, index in window.page_rows.items() if index == row), 'Dashboard')
        settings.setValue('ManagerWindow/page', page)
        settings.sync()
        if settings.status() != QSettings.NoError:
            raise OSError('Manager window preferences could not be saved')
