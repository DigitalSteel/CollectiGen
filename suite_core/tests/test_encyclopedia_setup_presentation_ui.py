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
@pytest.mark.parametrize('viewport', [(1280, 720), (1920, 1080)])
def test_compact_initial_setup_details_and_user_resize_fit_scaled_workarea(app, content_font, monkeypatch, theme, viewport):
    from qt_app.theme import apply_theme
    apply_theme(app, theme)
    adapter, calls, _ = fixture_adapter(); executor = ResourceExecutorOwner()
    dialog = create_original_enrollment_dialog(adapter, executor)
    scale = app.devicePixelRatio()
    area = QRect(0, 0, round(viewport[0] / scale), round(viewport[1] / scale))
    monkeypatch.setattr(dialog, 'screen', lambda: SimpleNamespace(availableGeometry=lambda: area))
    dialog.fit_contents(); dialog.show(); app.processEvents()
    captured = []
    output = os.environ.get('ENCYCLOPEDIA_PRESENTATION_CAPTURE')
    if output:
        output = str(Path(output) / f'{viewport[0]}x{viewport[1]}')
    def check_and_capture(state):
        app.processEvents(); dialog.layout().activate()
        assert dialog.width() <= area.width() - 24 and dialog.height() <= area.height() - 64
        assert dialog.panel.version_summary.isVisible() == (state != 'initial')
        for widget in (dialog.panel.refresh, dialog.close_button):
            assert widget.isVisible()
            assert widget.width() >= widget.fontMetrics().horizontalAdvance(widget.text()) + 24
            point = widget.mapTo(dialog, widget.rect().bottomRight())
            assert 0 <= point.x() < dialog.width() and 0 <= point.y() < dialog.height()
        for widget in dialog.panel.findChildren(QLabel):
            if widget.isVisible():
                if output and widget.height() < widget.minimumSizeHint().height():
                    dest = Path(output); dest.mkdir(parents=True, exist_ok=True)
                    dialog.grab().save(str(dest/(theme+'-'+state+'-geometry-failure.png')))
                assert widget.height() >= widget.minimumSizeHint().height(), (
                    widget.text(), widget.objectName(), widget.height(), widget.minimumSizeHint(),
                    widget.pixmap().isNull())
        if output:
            dest = Path(output); dest.mkdir(parents=True, exist_ok=True)
            image = dialog.grab(); image_path = dest / (theme + '-' + state + '.png')
            assert image.save(str(image_path))
            captured.append(dict(state=state, logical_size=[dialog.width(), dialog.height()],
                pixel_size=[image.width(), image.height()], device_pixel_ratio=scale))
    try:
        assert isinstance(dialog.panel.refresh, QPushButton) and not dialog.panel.refresh.isFlat()
        assert dialog.panel.version_summary.text() == 'Installation status has not been checked yet.'
        assert dialog.panel.refresh.text() == 'Check for app'
        assert dialog.panel.status.text() == 'Choose Check for app to find the reader. Nothing will be installed until you confirm.'
        assert not dialog.panel.notes_toggle.isVisible() and not dialog.panel.technical.isVisible()
        assert not dialog.open_reader.isEnabled() and not calls
        assert not dialog.open_reader.isVisible()
        assert dialog.panel.setup_step.text() == 'Step 1 of 3 · Find the Reader'
        initial_height = dialog.height()
        assert initial_height < 400
        check_and_capture('initial')
        QTest.mouseClick(dialog.panel.refresh, Qt.LeftButton); settle_dialog(app, dialog)
        assert adapter.installation.calls == [('discover', 'stable')]
        assert dialog.panel.offer is adapter.installation.offer
        assert dialog.panel.install.isVisible() and not calls
        assert dialog.panel.setup_step.text() == 'Step 2 of 3 · Review and install'
        assert dialog.panel.notes_toggle.isVisible() and dialog.panel.technical.isVisible()
        check_and_capture('available')
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
        assert panel.setup_step.text() == 'Check the Reader status'
        selected = SimpleNamespace(version='synthetic-v1')
        panel.installed = InstalledEncyclopedia(selected, 'healthy', '', '', False)
        panel.observation_stale = False; panel.offer = adapter.installation.offer; panel.render()
        assert panel.version_summary.text().startswith('Installed version: synthetic-v1\nAvailable version: ')
        assert panel.setup_step.text() == 'Setup complete'
        assert not calls
    finally:
        dialog.close(); settle_dialog(app, dialog)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_first_check_without_reader_keeps_clear_retry_and_close(app, content_font, theme):
    from qt_app.theme import apply_theme
    apply_theme(app, theme)
    adapter, calls, _ = fixture_adapter(); executor = ResourceExecutorOwner()
    def no_offers(*, channel, should_cancel):
        adapter.installation.calls.append(('discover', channel))
        return ()
    adapter.installation.discover = no_offers
    dialog = create_original_enrollment_dialog(adapter, executor)
    dialog.show(); app.processEvents()
    try:
        assert dialog.panel.refresh.text() == 'Check for app'
        assert not dialog.panel.version_summary.isVisible()
        QTest.mouseClick(dialog.panel.refresh, Qt.LeftButton); settle_dialog(app, dialog)
        assert adapter.installation.calls == [('discover', 'stable')]
        assert dialog.panel.status.text() == 'No reader app was found. Choose Check for app to try again later.'
        assert dialog.panel.refresh.isVisible() and dialog.panel.refresh.isEnabled()
        assert not dialog.panel.install.isVisible() and not dialog.open_reader.isVisible()
        assert not dialog.panel.notes_toggle.isVisible()
        assert not calls
        output = os.environ.get('ENCYCLOPEDIA_PRESENTATION_CAPTURE')
        if output:
            dest = Path(output); dest.mkdir(parents=True, exist_ok=True)
            assert dialog.grab().save(str(dest/(theme+'-unavailable.png')))
        QTest.mouseClick(dialog.close_button, Qt.LeftButton); settle_dialog(app, dialog)
        assert dialog.released and calls == ['close']
    finally:
        if not executor.closed:
            dialog.close(); settle_dialog(app, dialog)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_setup_stage_card_retains_delayed_progress_cancel_and_installed_next_action(app, content_font, theme):
    from threading import Event
    from qt_app.theme import apply_theme
    from suite_core.tests.test_encyclopedia_enrollment_ui import install_visible
    apply_theme(app, theme)
    adapter, calls, _ = fixture_adapter()
    service = adapter.installation
    service.entered, service.gate = Event(), Event()
    original_install = service.install
    def install_with_stock_cancellation(review, **kwargs):
        result = original_install(review, **kwargs)
        if kwargs['should_cancel']():
            # Real EncyclopediaInstallation._cancel raises; a false connection
            # result would incorrectly model a publication failure after cancel.
            raise InterruptedError('Synthetic acquisition cancelled before installation')
        return result
    service.install = install_with_stock_cancellation
    executor = ResourceExecutorOwner()
    dialog = create_original_enrollment_dialog(adapter, executor)
    dialog.show(); app.processEvents()
    output = os.environ.get('ENCYCLOPEDIA_PRESENTATION_CAPTURE')
    def capture(name):
        app.processEvents(); dialog.panel.setup_card.layout().activate()
        for label in (dialog.panel.setup_step, dialog.panel.status, dialog.panel.version_summary):
            if label.isVisible() and label.hasHeightForWidth():
                assert label.height() >= label.heightForWidth(label.width()), (
                    name, label.text(), label.height(), label.heightForWidth(label.width()))
        if output:
            dest = Path(output) / 'delayed'; dest.mkdir(parents=True, exist_ok=True)
            assert dialog.grab().save(str(dest / (theme + '-' + name + '.png')))
            (dest / (theme + '-' + name + '-layout.json')).write_text(json.dumps({
                key: {'visible': widget.isVisible(), 'height': widget.height(),
                      'minimum_hint': widget.minimumSizeHint().height(),
                      'retain_hidden': widget.sizePolicy().retainSizeWhenHidden()}
                for key, widget in [('step', dialog.panel.setup_step), ('status', dialog.panel.status),
                    ('activity', dialog.panel.activity), ('elapsed', dialog.panel.elapsed),
                    ('version', dialog.panel.version_summary), ('card', dialog.panel.setup_card)]}, indent=2)+'\n')
    try:
        install_visible(app, dialog)
        assert service.entered.wait(1)
        QTest.qWait(1100); dialog.poll()
        panel = dialog.panel
        assert panel.setup_step.text() == 'Step 2 of 3 · Review and install'
        assert panel.status.parentWidget() is panel.setup_card
        assert panel.activity.parentWidget() is panel.setup_card
        assert panel.activity.isVisible() and panel.activity.minimum() == panel.activity.maximum() == 0
        assert panel.elapsed.isVisible() and 'Elapsed:' in panel.elapsed.text()
        assert panel.stop.isEnabled() and dialog.close_button.isEnabled()
        assert not calls and service.current.selected is None
        capture('installing')
        QTest.mouseClick(panel.stop, Qt.LeftButton)
        service.gate.set(); settle_dialog(app, dialog)
        assert service.current.selected is None and not panel.activity.isVisible()
        assert panel.status.text() == 'Setup did not finish. No reader is installed. Choose Check for app to try again.'
        capture('cancelled')
        install_visible(app, dialog); settle_dialog(app, dialog)
        assert panel.setup_step.text() == 'Step 3 of 3 · Open and finish setup'
        assert dialog.open_reader.isVisible() and dialog.open_reader.isEnabled()
        assert dialog.open_reader.property('primary') is True and not calls
        capture('installed')
    finally:
        service.gate.set()
        dialog.close(); settle_dialog(app, dialog)


@pytest.mark.parametrize('operation', ['discover', 'review'])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_setup_stage_card_keeps_release_and_review_waits_visible(app, content_font, theme, operation):
    from threading import Event
    from qt_app.theme import apply_theme
    apply_theme(app, theme)
    adapter, calls, _ = fixture_adapter(); service = adapter.installation
    executor = ResourceExecutorOwner()
    dialog = create_original_enrollment_dialog(adapter, executor)
    dialog.show(); app.processEvents()
    entered, gate = Event(), Event()
    original = getattr(service, operation)
    def delayed(*args, **kwargs):
        entered.set(); assert gate.wait(3)
        return original(*args, **kwargs)
    if operation == 'review':
        QTest.mouseClick(dialog.panel.refresh, Qt.LeftButton); settle_dialog(app, dialog)
    setattr(service, operation, delayed)
    try:
        QTest.mouseClick(dialog.panel.refresh if operation == 'discover' else dialog.panel.install, Qt.LeftButton)
        assert entered.wait(1)
        QTest.qWait(250); dialog.poll()
        assert dialog.panel.activity.isVisible() and dialog.panel.elapsed.isVisible()
        assert dialog.panel.activity.minimum() == dialog.panel.activity.maximum() == 0
        assert dialog.panel.stop.isEnabled() and dialog.close_button.isEnabled()
        assert ('Step 1' if operation == 'discover' else 'Step 2') in dialog.panel.setup_step.text()
        output = os.environ.get('ENCYCLOPEDIA_PRESENTATION_CAPTURE')
        if output:
            dest = Path(output) / 'delayed'; dest.mkdir(parents=True, exist_ok=True)
            assert dialog.grab().save(str(dest / (theme + '-' + operation + '.png')))
        QTest.mouseClick(dialog.panel.stop, Qt.LeftButton)
        gate.set(); settle_dialog(app, dialog)
        assert not calls and service.current.selected is None
        assert not any(row[0] == 'install' for row in service.calls)
    finally:
        gate.set(); dialog.close(); settle_dialog(app, dialog)
