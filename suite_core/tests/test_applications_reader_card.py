from datetime import datetime, timezone
from types import SimpleNamespace
import os
from pathlib import Path
import pytest

from PySide6.QtWidgets import QApplication, QPushButton
from suite_core.manager.window import create_window, unconfigured
from tests.test_encyclopedia_connection import package


def test_reader_search_status_and_saved_connection_dispatch(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = create_window(unconfigured(datetime.now(timezone.utc)),
        reader_settings_path=tmp_path / 'settings.json')
    window.show()
    window.navigation.setCurrentRow(window.page_rows['Applications'])
    def card():
        app.processEvents()
        return next((c for c in window.library_body.cards
            if c.property('componentKey') == 'encyclopedia-connection'), None)
    try:
        assert card() is not None
        window.library_search.setText('unrelated application')
        assert card() is None
        window.library_search.setText('encyclopedia')
        assert card() is not None
        for status in ('Installed', 'Available', 'Updates', 'In development'):
            window.library_filter.setCurrentText(status)
            assert card() is None
        window.library_filter.setCurrentText('All entries')
        primary = card().findChild(QPushButton, 'readerPrimaryAction')
        assert primary.text() == 'Connect existing'
        primary.click(); assert window.reader_dialog.isVisible()
        selected = package(tmp_path / 'reader')
        panel = window.reader_panel
        panel.directory.setText(str(selected.directory))
        panel.review.click(); panel.save.click()
        window.render_library()
        assert card().findChild(QPushButton, 'readerPrimaryAction').text() == 'Launch'
        window.library_filter.setCurrentText('Installed')
        assert card() is None  # Saved external connection is not managed installation.
    finally:
        window.reader_panel.timer.stop(); window.close(); app.processEvents()


def test_dashboard_reader_route_and_catalog_do_not_duplicate_or_infer_installation():
    from suite_core.manager.resource_dashboard import create_dashboard
    app = QApplication.instance() or QApplication([])
    report = unconfigured(datetime.now(timezone.utc)).report
    absent = create_dashboard(report)
    present = create_dashboard(report, reader_available=True)
    supplied = SimpleNamespace(title='Tag Encyclopedia Reader',
        component_id=SimpleNamespace(value='collectigen.tag-encyclopedia'))
    catalog = create_dashboard(report, reader_available=True, supported_apps=(supplied,))
    try:
        assert 'Reader' not in absent.applications_summary.text()
        assert present.applications_summary.text() == 'Tag Encyclopedia Reader · Browse in Applications'
        assert catalog.applications_summary.text() == 'Tag Encyclopedia Reader · Catalog entry'
    finally:
        for widget in (absent, present, catalog):
            widget.close(); widget.deleteLater()
        app.processEvents()


def test_acquisition_state_does_not_infer_update_or_install_after_clear(tmp_path, monkeypatch):
    from tests.test_encyclopedia_installation import Fixture
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    app = QApplication.instance() or QApplication([])
    fixture = Fixture(tmp_path, monkeypatch)
    executor = ResourceExecutorOwner()
    window = create_window(unconfigured(datetime.now(timezone.utc)),
        reader_settings_path=fixture.settings, encyclopedia_acquisition=fixture.service,
        encyclopedia_executor=executor)
    try:
        assert window.reader_card_state()[4] == 'Find reader'
        panel = window.acquisition_panel
        panel.offer = SimpleNamespace(release=SimpleNamespace(software_version='0.1.0-alpha.99'))
        assert window.reader_card_state()[4] == 'Install'
        panel.offer = None
        calls = []
        monkeypatch.setattr(panel, 'refresh_releases', lambda: calls.append('find'))
        window.render_library()
        card = next(c for c in window.library_body.cards
            if c.property('componentKey') == 'encyclopedia-connection')
        card.findChild(QPushButton, 'readerPrimaryAction').click()
        assert window.acquisition_dialog.isVisible() and calls == ['find']
        window.acquisition_dialog.hide()
        panel.installed = SimpleNamespace(selected=None)
        assert not window.reader_card_state()[0]
        selected = package(tmp_path / 'reader')
        panel.installed = SimpleNamespace(selected=selected)
        panel.offer = SimpleNamespace(release=SimpleNamespace(software_version=selected.version))
        assert not window.reader_card_state()[2]
        panel.offer.release.software_version = '0.1.0-alpha.99'
        assert window.reader_card_state()[4] == 'Update'
        panel.closing = True
        assert window.reader_card_state()[4] == 'View progress'
    finally:
        window.acquisition_panel.timer.stop(); window.reader_panel.timer.stop()
        executor.begin_close(); window.close(); app.processEvents()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_reader_card_source_capture(tmp_path, monkeypatch, theme):
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QToolButton
    from qt_app.theme import apply_theme
    from tests.test_encyclopedia_installation import Fixture
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id >= 0
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    apply_theme(app, theme)
    fixture = Fixture(tmp_path, monkeypatch); fixture.add()
    executor = ResourceExecutorOwner()
    window = create_window(unconfigured(datetime.now(timezone.utc)), theme=theme,
        reader_settings_path=fixture.settings, encyclopedia_acquisition=fixture.service,
        encyclopedia_executor=executor)
    try:
        window.resize(1920, 1080); window.show()
        app.processEvents()
        assert window.dashboard.applications_summary.text().count('Tag Encyclopedia Reader') == 1
        assert 'Installed' not in window.dashboard.applications_summary.text()
        captures = os.environ.get('READER_CARD_CAPTURE')
        if captures:
            output = Path(captures); output.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(output / (theme + '-dashboard.png')))
        window.navigation.setCurrentRow(window.page_rows['Applications'])
        window.library_search.setText('encyclopedia'); app.processEvents()
        assert 'installation tools supplied' in window.statusBar().currentMessage()
        card = next(c for c in window.library_body.cards if c.property('componentKey') == 'encyclopedia-connection')
        assert card.findChild(QPushButton, 'readerPrimaryAction').text() == 'Find reader'
        assert 'Choose Find reader' in window.reader_card_state()[6]
        if captures:
            assert window.grab().save(str(output / (theme + '-find-reader.png')))
        from suite_core.tests.test_encyclopedia_acquisition_ui import wait
        card.findChild(QPushButton, 'readerPrimaryAction').click()
        assert window.acquisition_dialog.isVisible()
        wait(window.acquisition_panel)
        assert window.acquisition_panel.offer is not None
        window.acquisition_dialog.hide(); window.render_library(); app.processEvents()
        card = next(c for c in window.library_body.cards
            if c.property('componentKey') == 'encyclopedia-connection')
        assert card.findChild(QPushButton, 'readerPrimaryAction').text() == 'Install'
        if captures:
            assert window.grab().save(str(output / (theme + '-install-offered.png')))
        card.findChild(QToolButton).click(); app.processEvents()
        if captures:
            assert window.grab().save(str(output / (theme + '-options.png')))
        window.navigation.setCurrentRow(window.page_rows['Dependencies']); app.processEvents()
        if captures:
            assert window.grab().save(str(output / (theme + '-dependencies.png')))
    finally:
        window.acquisition_panel.timer.stop(); window.reader_panel.timer.stop()
        executor.begin_close(); window.close(); app.processEvents()


def test_dependency_route_revision_and_fresh_close_guard(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = create_window(unconfigured(datetime.now(timezone.utc)),
        reader_settings_path=tmp_path / 'settings.json')
    window.show(); window.navigation.setCurrentRow(window.page_rows['Dependencies'])
    app.processEvents()
    pane = window.dependency_status_panel
    try:
        assert pane.snapshot.observations[-1].state.value == 'unknown'
        calls = []
        pane.route_requested.connect(lambda *args: calls.append(args))
        pane._dispatch('reader-connection')
        assert window.reader_dialog.isVisible() and len(calls) == 1
        pane._dispatch('reader-connection'); assert len(calls) == 1
        revision = pane.snapshot.revision
        window.reader_dialog.reject(); app.processEvents()
        assert pane.snapshot.revision != revision
        pane._dispatch('reader-connection'); assert len(calls) == 2
        revision = pane.snapshot.revision
        window.reader_panel.status.setText('Saved selection requires review.')
        window.render_dependency_status()
        assert pane.snapshot.revision != revision
        window.reader_dialog.hide()
        window.close_policy.pending = True
        window.open_dependency_route('reader-connection', 'reader-connection')
        assert not window.reader_dialog.isVisible()
        assert pane.snapshot.observations[-1].state.value == 'busy'
        window.close_policy.pending = False
    finally:
        window.reader_panel.timer.stop(); window.close(); app.processEvents()


def test_backend_dependency_chat_busy_then_idle_and_rejected_retry(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = create_window(unconfigured(datetime.now(timezone.utc)),
        reader_settings_path=tmp_path / 'settings.json')
    original_chat = window.chat_host
    original_flow = window.backend_setup_controller
    try:
        window.backend_setup_controller = SimpleNamespace(activation_pending=False,
            hold=False, message='Current connection needs review.', closed=False)
        window.chat_host = SimpleNamespace(busy=True)
        window.render_dependency_status()
        pane = window.dependency_status_panel
        row = next(r for r in pane.snapshot.observations if r.key == 'backend-connection')
        assert row.state.value == 'busy'
        calls = []
        window.open_backend_setup = lambda: calls.append('opened')
        pane._dispatch('backend-connection'); assert not calls
        window.open_dependency_route('backend-connection', 'backend-connection')
        assert not calls
        window.chat_host.busy = False
        window.render_dependency_status()
        pane._dispatch('backend-connection'); assert calls == ['opened']
        revision = pane.snapshot.revision
        window.dependency_route_settled()
        assert pane.snapshot.revision != revision
        pane._dispatch('backend-connection'); assert calls == ['opened', 'opened']
    finally:
        window.chat_host = original_chat; window.backend_setup_controller = original_flow
        window.reader_panel.timer.stop(); window.close(); app.processEvents()
