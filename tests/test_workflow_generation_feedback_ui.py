"""Generated offscreen component states; no desktop, native admission or generator."""
from types import SimpleNamespace

from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest

from suite_core.tests.workflow_ui_fixtures import workflow_window
from test_manager_generator_setup_ui import application, wait_until, capture


def test_readiness_disclosure_and_terminal_cancel_feedback_from_module_entry(tmp_path, monkeypatch):
    application()  # Stock offscreen font setup keeps isolated captures readable.
    from test_manager_generator_setup_ui import FixtureRuntime
    monkeypatch.setattr("core.swarm_runtime.SwarmRuntimeManager", FixtureRuntime)
    app, session, window = workflow_window(tmp_path, 'generate', theme='light')
    window.resize(1920, 1080)
    QTest.mouseClick(window.surface.new, Qt.LeftButton)
    wait_until(app, lambda: window._generation_dialog is not None)
    dialog = window._generation_dialog
    wait_until(app, lambda: not dialog._pending, timeout=30)
    dialog.tabs.setCurrentIndex(dialog._readiness_tab_index)
    dialog.manual_connection.setChecked(False)
    app.processEvents()
    assert not dialog.connection_detail.isVisible()
    QTest.mouseClick(dialog.manual_connection, Qt.LeftButton,
                     pos=QPoint(8, dialog.manual_connection.height() // 2))
    assert dialog.connection_detail.isVisible()
    QTest.mouseClick(dialog.manual_connection, Qt.LeftButton,
                     pos=QPoint(8, dialog.manual_connection.height() // 2))
    assert not dialog.connection_detail.isVisible()
    capture(window, 'generate-readiness-details-collapsed-1080p')
    # Component injection represents the terminal callback. No submitted job,
    # transport, hardware execution or actual cancellation is claimed here.
    receipt = dialog.receipt
    dialog._submitted_job_id = 'generated-terminal-component'
    dialog._submitted_progress = SimpleNamespace(state='cancelled')
    dialog._submission_terminal_seen = True
    dialog._submission_terminal_state = 'cancelled'
    dialog._update_actions()
    assert 'cancelled' in dialog.results_status.text().casefold()
    assert 'can cancel submitted work' not in dialog.results_status.text().casefold()
    assert dialog.receipt is receipt
    assert not dialog.cancel_button.isEnabled()
    dialog.tabs.setCurrentIndex(3)
    app.processEvents()
    capture(window, 'generate-cancelled-outcome-1080p')
    window.close()
    wait_until(app, lambda: not window.isVisible(), timeout=30)
    assert session.local_drain_reported
