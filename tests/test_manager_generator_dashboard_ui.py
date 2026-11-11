"""Manager Dashboard generator entry; generated records, no live runtime I/O."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox
from core.swarm_runtime import SwarmSnapshot
from suite_core.manager.window import create_window, unconfigured
from test_manager_generator_setup_ui import application, wait_until


def capture(window, name):
    output = os.environ.get("COLLECTIGEN_DASHBOARD_CAPTURES")
    if output:
        # Allow the stock card's queued layout request to settle after text changes.
        QTest.qWait(60)
        card = getattr(window, "generator_card", None)
        if card is not None:
            assert card.detail_label.height() >= card.detail_label.heightForWidth(card.detail_label.width())
            assert card.action_button.width() >= card.action_button.sizeHint().width()
        Path(output).mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(Path(output) / (name + ".png")))


def fixture_window(tmp_path, monkeypatch, *, admitted=True):
    import core.swarm_runtime as runtime
    value = [SwarmSnapshot("not_configured", "Not connected", "Connect or install local SwarmUI.", False)]
    reads = []
    class Runtime:
        def __init__(self, path): self.path = path
        def snapshot(self):
            reads.append(self.path)
            return value[0]
    monkeypatch.setattr(runtime, "SwarmRuntimeManager", Runtime)
    session = NS(state="ready" if admitted else "revoked", admitted=NS(layout=NS(data_root=tmp_path)))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1600, 950); window.show()
    return window, session, value, reads


def test_dashboard_saved_states_and_existing_action_dispatch(tmp_path, monkeypatch):
    app = application()
    window, session, value, reads = fixture_window(tmp_path, monkeypatch)
    calls = []
    window.review_generator_connection = lambda: calls.append("setup")
    window.open_generate_plan = lambda: calls.append("plan")
    window.review_generation_jobs = lambda: calls.append("work")
    cases = [("not_configured", False, "setup"), ("offline", True, "setup"),
             ("starting", True, "setup"), ("needs_attention", True, "setup"),
             ("incompatible", True, "setup"), ("ready", True, "plan"),
             ("busy", True, "work"), ("update_available", True, "plan")]
    for state, configured, action in cases:
        value[0] = SwarmSnapshot(state, state.replace("_", " ").title(),
            "Generated saved runtime state; no service contacted.", configured,
            runtime_version="fixture-1" if configured else "", last_health_check="2026-11-11 14:00 UTC",
            active_jobs=2 if state == "busy" else 0,
            resource_summary="GPU memory: fixture observation")
        window.refresh_generator_card(); app.processEvents()
        card = window.generator_card
        assert card.presentation.action_enabled
        assert card.presentation.status.startswith(value[0].headline)
        if configured:
            assert "Saved observation" in card.detail_label.text()
            assert "fixture-1" in card.detail_label.text()
        QTest.mouseClick(card.action_button, Qt.LeftButton)
        assert calls[-1] == action
        if state in {"not_configured", "ready", "busy", "offline"}:
            capture(window, "dashboard-" + state)
    assert all(path.parent == tmp_path / "Manager" for path in reads)
    # Dispatch rechecks the current saved state, rather than trusting the card's old label.
    value[0] = replace(value[0], state="offline")
    QTest.mouseClick(window.generator_card.action_button, Qt.LeftButton)
    assert calls[-1] == "setup"
    count = len(reads)
    session.state = "revoked"
    window.refresh_generator_card()
    assert len(reads) == count
    assert not window.generator_card.action_button.isEnabled()
    capture(window, "dashboard-session-unavailable")
    window.close(); app.processEvents()


def test_dashboard_opens_real_setup_and_refreshes_saved_state_on_return(tmp_path, monkeypatch):
    app = application()
    window, session, value, reads = fixture_window(tmp_path, monkeypatch)
    errors = []
    def close_setup():
        dialog = window._generation_setup_dialog
        if dialog is None:
            QTimer.singleShot(10, close_setup); return
        try:
            assert dialog.review.task_nav.count() == 4
            capture(dialog, "dashboard-opened-setup")
            value[0] = SwarmSnapshot("offline", "Offline", "Saved local installation; start it explicitly in Setup.", True)
            buttons = dialog.findChild(QDialogButtonBox)
            QTest.mouseClick(buttons.button(QDialogButtonBox.Close), Qt.LeftButton)
        except Exception as exc:
            errors.append(exc); dialog.reject()
    QTimer.singleShot(10, close_setup)
    QTest.mouseClick(window.generator_card.action_button, Qt.LeftButton)
    assert not errors, errors
    assert window._generation_setup_dialog is None
    assert window.generator_card.presentation.status == "Offline (saved)"
    assert window.navigation.currentRow() == window.page_rows["Dashboard"]
    capture(window, "dashboard-after-setup")
    window.close(); app.processEvents()


def test_unavailable_session_does_not_read_generator_record(tmp_path, monkeypatch):
    app = application()
    window, session, value, reads = fixture_window(tmp_path, monkeypatch, admitted=False)
    assert reads == []
    assert not window.generator_card.action_button.isEnabled()
    QTest.mouseClick(window.generator_card.action_button, Qt.LeftButton)
    assert reads == []
    window.close(); app.processEvents()
