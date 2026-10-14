"""Source Qt observation and consent states; no executable replacement qualification."""
from concurrent.futures import Future
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QMessageBox
from core.encyclopedia_installation import InstalledEncyclopedia
from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
from suite_core.tests.test_encyclopedia_acquisition_ui import app, executor_scope, wait, click, queue_confirmation
from tests.test_encyclopedia_installation import Fixture, no_external_actions


@pytest.mark.parametrize('selected', [None, SimpleNamespace(version='synthetic-v1')])
def test_unknown_observed_and_failed_refresh_retains_honest_stale_state(tmp_path, monkeypatch, selected):
    application = app(); f = Fixture(tmp_path, monkeypatch)
    class Executor:
        def submit(self, work):
            future = Future(); future.set_exception(OSError('inert observation failure')); return future
    panel = EncyclopediaAcquisitionPanel(f.service, Executor()); panel.show(); application.processEvents()
    try:
        assert panel.version_summary.text() == 'Reader installation has not been checked.'
        click(panel.refresh); panel.poll()
        assert panel.installed is None and 'No reader installed' not in panel.version_summary.text()
        panel.installed = InstalledEncyclopedia(selected, 'healthy' if selected else 'absent', '', '', False)
        panel.render()
        assert ('Installed reader: synthetic-v1' if selected else 'No reader installed.') in panel.version_summary.text()
        click(panel.refresh); panel.poll()
        assert panel.version_summary.text().startswith('Last observed (may be out of date): ')
    finally:
        panel.timer.stop(); panel.close()


def test_successful_refresh_replaces_stale_observation(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch)
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor); panel.show(); application.processEvents()
        try:
            panel.observation_stale = True
            click(panel.refresh); wait(panel)
            assert panel.installed.selected is None and not panel.observation_stale
            assert panel.version_summary.text() == 'No reader installed.'
        finally:
            panel.timer.stop(); panel.close()


def test_standalone_confirmation_authorizes_exact_download_with_compatibility_condition(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    f.service.standalone_compatibility = True
    panel = EncyclopediaAcquisitionPanel(f.service, SimpleNamespace(close_held=False)); panel.show()
    application.processEvents()
    try:
        reviewed = f.reviewed(); panel.installed = reviewed.installed
        seen = []
        def cancel(box):
            seen.append(box.informativeText()); return QMessageBox.No
        monkeypatch.setattr(QMessageBox, 'exec', cancel)
        assert not panel.confirm_installation(reviewed)
        assert 'Compatibility will be checked before installation.' in seen[0]
        assert 'download this exact package and install only if compatible' in seen[0]
        assert reviewed.offer.release.software_version in panel.findChild(QMessageBox).text()
        assert not (f.root / 'Apps').exists()
    finally:
        panel.timer.stop(); panel.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_visible_check_confirm_rejected_package_and_stale_observation(tmp_path, monkeypatch, theme):
    from PySide6.QtWidgets import QWidget
    from qt_app.theme import apply_theme
    application = app(); apply_theme(application, theme)
    f = Fixture(tmp_path, monkeypatch); f.add(); f.service.standalone_compatibility = True
    settings = f.settings.read_bytes()
    canvas = QWidget(); canvas.resize(1920, 1080)
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor, parent=canvas)
        panel.setGeometry(40, 40, 1000, 650); canvas.show(); application.processEvents()
        def capture(state):
            output = os.environ.get('ENCYCLOPEDIA_COMPATIBILITY_CAPTURE')
            if output:
                directory = Path(output); directory.mkdir(parents=True, exist_ok=True)
                assert canvas.grab().save(str(directory / (theme + '-' + state + '.png')))
        try:
            capture('unknown')
            click(panel.refresh); wait(panel)
            assert 'Compatibility will be checked before installation.' in panel.status.text()
            capture('available')
            queue_confirmation(True); click(panel.install); wait(panel)
            assert 'This package was not installed.' in panel.status.text()
            assert panel.offer is None and not (f.root / 'Apps').exists()
            assert f.settings.read_bytes() == settings
            capture('rejected')
            monkeypatch.setattr(f.service, 'observe', lambda: (_ for _ in ()).throw(OSError('inert offline observation')))
            click(panel.refresh); wait(panel)
            assert panel.version_summary.text().startswith('Last observed (may be out of date): ')
            capture('stale')
        finally:
            panel.timer.stop(); panel.close(); canvas.close()
