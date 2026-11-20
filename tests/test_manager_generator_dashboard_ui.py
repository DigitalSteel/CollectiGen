"""Visible Manager first-run navigation with synthetic component authority."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from core.encyclopedia_connection import OwnedEncyclopedia, ReaderConnectionStore
from core.encyclopedia_installation import EncyclopediaInstallation
from core.organizer_connection_settings import OrganizerConnectionStore
from core.organizer_installation import OrganizerInstallation
from core.workflow_connection_settings import WorkflowConnectionStore
from core.workflow_installation import WorkflowInstallation
from core.workflow_native_owner import WorkflowWorkspaceOwner
from qt_app.theme import apply_theme
from suite_core.discovery.component_staging import ComponentStageAdmission
from suite_core.manager.bootstrap_entry import NormalEntrySession
from suite_core.manager.resource_dashboard import active_module_jobs
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.manager.window import create_window, unconfigured


def capture(window, theme, state):
    output = os.environ.get("COLLECTIGEN_DASHBOARD_CAPTURES")
    if output:
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        QTest.qWait(45)
        assert window.grab().save(str(path / f"{theme}-{state}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_first_run_dashboard_routes_to_explicit_app_setup(tmp_path, theme, monkeypatch):
    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    assert font_id >= 0
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    apply_theme(app, theme)

    session = object.__new__(NormalEntrySession)
    session.state = "ready"
    session.admitted = SimpleNamespace(layout=SimpleNamespace(data_root=tmp_path))
    session.workflow_selections = {}
    reader = OwnedEncyclopedia(normal_entry_session=session, launch_allowed=False)
    owner = WorkflowWorkspaceOwner(session, reader)
    admission = ComponentStageAdmission(lambda _component: pytest.fail("Browsing must not start a transaction."))
    services = []
    for component in ("collectigen.generate", "collectigen.train"):
        service = WorkflowInstallation(component, SimpleNamespace(maximum=1024),
            WorkflowConnectionStore(tmp_path / "manager.json", component),
            admission=admission, native_owner=owner)
        services.append((component, service, ResourceExecutorOwner()))

    settings = tmp_path / "manager.json"
    organizer = OrganizerInstallation(SimpleNamespace(maximum=1024),
        OrganizerConnectionStore(settings), admission=admission)
    reader_installation = EncyclopediaInstallation(SimpleNamespace(maximum=1024),
        ReaderConnectionStore(settings), admission=admission)
    window = create_window(unconfigured(datetime.now(timezone.utc)), theme=theme,
        normal_entry_session=session, workflow_workspace_owner=owner, workflow_services=tuple(services),
        reader_settings_path=settings, reader_owner=reader, encyclopedia_acquisition=reader_installation,
        encyclopedia_executor=ResourceExecutorOwner(), organizer_acquisition=organizer,
        organizer_executor=ResourceExecutorOwner())
    try:
        window.resize(1920, 1080)
        window.show()
        app.processEvents()

        assert window.stack.currentWidget() is window.dashboard_page
        visible = [button.text() for button in window.dashboard.findChildren(QPushButton) if button.isVisible()]
        assert visible == ["Go to Applications"]
        assert window.direct_model_button.isHidden() and window.open_chat.isHidden()
        capture(window, theme, "dashboard")
        window.dashboard.open_applications.setFocus()
        QTest.keyClick(window.dashboard.open_applications, Qt.Key_Space)
        app.processEvents()

        assert window.stack.currentWidget() is window.applications_page
        assert window.library_search.hasFocus()
        assert window.organizer_acquisition_action.isVisible()
        reader_card = next(card for card in window.library_body.cards
                           if card.property("componentKey") == "encyclopedia-connection")
        assert reader_card.findChild(QPushButton, "readerPrimaryAction").isVisible()
        assert window.workflow_applications.rows["collectigen.train"][2].isVisible()
        assert not window.workflow_management.isVisible()
        assert not any("everday" in label.text().casefold() for label in window.applications_page.findChildren(QPushButton))
        status, opening, setup, _group = window.workflow_applications.rows["collectigen.generate"]
        assert "not ready to open" in status.text()
        assert opening.isHidden() and not opening.isEnabled()
        assert setup.isVisible() and setup.isEnabled() and setup.text() == "Get Generate" + chr(0x2026)
        capture(window, theme, "applications")

        QTest.mouseClick(setup, Qt.LeftButton)
        panel = window.workflow_applications.panels["collectigen.generate"]
        dialog = panel.parentWidget()
        assert isinstance(dialog, QDialog) and dialog.isVisible()
        assert panel.refresh.text() == "Find Generate" and panel.refresh.isVisible()
        assert panel.future is None and panel.install.isHidden() and not (tmp_path / "manager.json").exists()
        capture(dialog, theme, "setup-review")
        QTest.keyClick(dialog, Qt.Key_Escape)
        assert not dialog.isVisible()
        assert window.stack.currentWidget() is window.applications_page
        assert owner.child is None
        capture(window, theme, "returned")

        organizer_checks = []
        organizer_panel = window.organizer_acquisition_panel
        monkeypatch.setattr(organizer_panel, "refresh_releases", lambda: organizer_checks.append(True))
        QTest.mouseClick(window.organizer_acquisition_action, Qt.LeftButton)
        assert organizer_checks == [True] and window.organizer_acquisition_dialog.isVisible()
        window.organizer_acquisition_dialog.close()

        panel.offer = SimpleNamespace(current_version="0.1.0-alpha.1",
                                      release=SimpleNamespace(software_version="0.2.0-alpha.1"))
        window.workflow_applications.render()
        assert setup.text() == "Update Generate" + chr(0x2026)
        owner.selection = SimpleNamespace(component_id="collectigen.generate")
        owner.native = object()
        owner.reason = "The workflow has started."
        try:
            window.render_dashboard_status()
            assert "Generate: The workflow has started." in window.dashboard.module_activity.text()
            assert "Review Generate 0.2.0-alpha.1 update" in window.dashboard.notifications.text()
        finally:
            owner.native = owner.selection = None
        try:
            item = window.navigation.item(window.page_rows["Dashboard"])
            QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                             pos=window.navigation.visualItemRect(item).center())
            capture(window, theme, "update-notice")
            notice = window.dashboard.notifications
            notice.setFocus()
            QTest.keyClick(notice, Qt.Key_Tab)
            QTest.keyClick(notice, Qt.Key_Return)
            assert dialog.isVisible()
            assert panel.future is None
            dialog.close()
        finally:
            panel.offer = None
    finally:
        window.close()
        app.processEvents()


def test_dashboard_reports_status_and_unavailable_apps_without_launch_controls():
    app = QApplication.instance() or QApplication([])
    window = create_window(unconfigured(datetime.now(timezone.utc)))
    try:
        window.show()
        app.processEvents()
        assert window.dashboard.module_activity.text() == "No current module work reported."
        assert "No update notices" in window.dashboard.notifications.text()
        QTest.mouseClick(window.dashboard.open_applications, Qt.LeftButton)
        for status, opening, setup, _group in window.workflow_applications.rows.values():
            assert "No installable release" in status.text()
            assert not opening.isVisible() and not setup.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_active_work_uses_current_module_reports_only():
    now = datetime.now(timezone.utc)
    row = {
        "requesting_app": "collectigen.generate", "current_status": "running",
        "progress_is_current": True, "observed_at": now.isoformat(),
        "valid_for_seconds": 10, "completed_units": 2, "total_units": 5,
    }
    observations = (
        row,
        dict(row, requesting_app="unknown"),
        dict(row, observed_at=(now - timedelta(seconds=11)).isoformat()),
        dict(row, progress_is_current=False),
        dict(row, current_status="succeeded"),
    )
    jobs = [SimpleNamespace(as_ui=lambda data=data: data) for data in observations]
    assert active_module_jobs(jobs, {"collectigen.generate": "Generate"}, now) == (
        "Generate: In progress (2/5)",
    )
