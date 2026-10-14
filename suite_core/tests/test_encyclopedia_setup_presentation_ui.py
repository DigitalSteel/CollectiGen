"""Source offscreen layout/controls; synthetic work area, not frozen native acceptance."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QLabel, QPushButton
from core.encyclopedia_installation import InstalledEncyclopedia
from suite_core.encyclopedia.root_recovery_ui import create_original_enrollment_dialog
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.tests.test_encyclopedia_enrollment_ui import fixture_adapter, settle_dialog
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font


@pytest.fixture(autouse=True)
def restore_application_theme(app):
    from qt_app import theme, confidence
    palette=QPalette(app.palette()); stylesheet=app.styleSheet()
    high_contrast=app.property('collectigenHighContrast')
    system_palette=theme._system_palette
    status_colors=dict(confidence.STATUS_COLORS)
    yield
    confidence.STATUS_COLORS.clear(); confidence.STATUS_COLORS.update(status_colors)
    theme._system_palette=system_palette
    app.setPalette(palette); app.setStyleSheet(stylesheet)
    app.setProperty('collectigenHighContrast',high_contrast)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_compact_initial_setup_details_and_user_resize_fit_scaled_workarea(app, content_font, monkeypatch, theme):
    from qt_app.theme import apply_theme
    apply_theme(app, theme)
    adapter, calls, _ = fixture_adapter(); executor = ResourceExecutorOwner()
    dialog = create_original_enrollment_dialog(adapter, executor)
    scale = app.devicePixelRatio()
    area = QRect(0, 0, round(1920 / scale), round(1080 / scale))
    monkeypatch.setattr(dialog, 'screen', lambda: SimpleNamespace(availableGeometry=lambda: area))
    dialog.fit_contents(); dialog.show(); app.processEvents()
    captured = []
    output = os.environ.get('ENCYCLOPEDIA_PRESENTATION_CAPTURE')
    def check_and_capture(state):
        app.processEvents(); dialog.layout().activate()
        assert dialog.width() <= area.width() - 24 and dialog.height() <= area.height() - 64
        for widget in (dialog.panel.refresh, dialog.close_button, dialog.panel.version_summary):
            assert widget.isVisible()
            point = widget.mapTo(dialog, widget.rect().bottomRight())
            assert 0 <= point.x() < dialog.width() and 0 <= point.y() < dialog.height()
        for widget in dialog.panel.findChildren(QLabel):
            if widget.isVisible():
                assert widget.height() >= widget.minimumSizeHint().height()
        if output:
            dest = Path(output); dest.mkdir(parents=True, exist_ok=True)
            image = dialog.grab(); image_path = dest / (theme + '-' + state + '.png')
            assert image.save(str(image_path))
            captured.append(dict(state=state, logical_size=[dialog.width(), dialog.height()],
                pixel_size=[image.width(), image.height()], device_pixel_ratio=scale))
    try:
        assert isinstance(dialog.panel.refresh, QPushButton) and not dialog.panel.refresh.isFlat()
        assert dialog.panel.version_summary.text() == 'Installation status has not been checked yet.'
        assert dialog.panel.status.text() == 'Choose Check Again to look for an available version.'
        assert not dialog.open_reader.isEnabled() and not calls
        initial_height = dialog.height()
        assert initial_height < 400
        check_and_capture('initial')
        QTest.mouseClick(dialog.panel.notes_toggle, Qt.LeftButton); app.processEvents()
        QTest.mouseClick(dialog.panel.technical, Qt.LeftButton); app.processEvents()
        assert dialog.height() >= initial_height
        assert dialog.panel.release_notes.isVisible() and dialog.panel.details.isVisible()
        assert dialog.panel.release_notes.height() >= dialog.panel.release_notes.minimumHeight()
        assert dialog.panel.details.height() >= dialog.panel.details.minimumSizeHint().height()
        check_and_capture('details')
        dialog.resize(min(area.width()-24, dialog.width()+60), min(area.height()-64, dialog.height()+40))
        app.processEvents(); check_and_capture('resized')
        QTest.mouseClick(dialog.panel.notes_toggle, Qt.LeftButton)
        QTest.mouseClick(dialog.panel.technical, Qt.LeftButton); app.processEvents()
        assert not dialog.panel.release_notes.isVisible() and not dialog.panel.details.isVisible()
        assert dialog.height() >= initial_height  # do not override user enlargement on collapse
        QTest.mouseClick(dialog.close_button, Qt.LeftButton); settle_dialog(app, dialog)
        assert dialog.released and calls == ['close']
    finally:
        if not executor.closed:
            dialog.close(); settle_dialog(app, dialog)
        if output:
            (Path(output)/(theme+'-geometry.json')).write_text(json.dumps(captured,indent=2)+'\n')


def test_standalone_plain_observations_preserve_unknown_absent_stale_and_installed(app, content_font):
    adapter, calls, _ = fixture_adapter(); executor = ResourceExecutorOwner()
    adapter.installation.standalone_compatibility=True
    dialog = create_original_enrollment_dialog(adapter, executor); dialog.show(); app.processEvents()
    try:
        panel = dialog.panel
        assert 'not been checked' in panel.version_summary.text()
        QTest.mouseClick(panel.refresh,Qt.LeftButton); settle_dialog(app,dialog)
        assert panel.status.text().startswith('Version ')
        assert 'Compatibility will be checked before installation.' in panel.status.text()
        panel.offer=None
        panel.installed = adapter.installation.current; panel.render()
        assert panel.version_summary.text() == 'Tag Encyclopedia is not installed yet.'
        panel.observation_stale = True; panel.render()
        assert panel.version_summary.text().startswith('Last observed (may be out of date): ')
        selected = SimpleNamespace(version='synthetic-v1')
        panel.installed = InstalledEncyclopedia(selected, 'healthy', '', '', False)
        panel.observation_stale = False; panel.offer = adapter.installation.offer; panel.render()
        assert panel.version_summary.text().startswith('Installed version: synthetic-v1\nAvailable version: ')
        assert not calls
    finally:
        dialog.close(); settle_dialog(app, dialog)
