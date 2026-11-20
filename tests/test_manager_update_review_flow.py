"""Visible review flow with signed fixture metadata and an inert launcher boundary."""
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.manager_update_service import ManagerUpdateService, PreparedManagerUpdate
from qt_app.theme import apply_theme
from suite_core.manager.bootstrap_entry import NormalEntrySession
from suite_core.manager.update_review_ui import ManagerUpdateReviewPanel
from tests.test_component_acquisition import FakeFeed, service


def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        QTest.qWait(15)
    assert predicate()


@pytest.mark.parametrize("theme,outcome", [("light", "apply"), ("dark", "cancel"), ("dark", "failure")])
def test_one_update_confirmation_or_safe_cancellation(tmp_path, monkeypatch, theme, outcome):
    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    assert font_id >= 0
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    apply_theme(app, theme)
    feed = FakeFeed()
    feed.add("collectigen.suite-manager", "0.8.2-alpha.1")
    updater = ManagerUpdateService(service(tmp_path, feed), current_version="0.8.1-alpha.1", channel="beta")
    stage_started = Event()
    release_stage = Event()
    events = []
    cancel_stage = outcome == "cancel"

    def stage(offer, *, should_cancel, progress):
        events.append("stage")
        progress(1, 4, "fixture")
        stage_started.set()
        assert release_stage.wait(5)
        assert should_cancel() is cancel_stage
        if outcome == "failure":
            raise ValueError("Fixture verification failed.")
        admission = SimpleNamespace(version="0.8.2-alpha.1", channel="beta", artifact_sha256="a" * 64,
            release_sha256="b" * 64, trust_sha256="c" * 64, inventory_sha256="d" * 64)
        return PreparedManagerUpdate(offer, tmp_path / "synthetic", b"", admission)

    def revalidate(prepared):
        assert prepared.offer.release.software_version == "0.8.2-alpha.1"
        events.append("revalidate")

    def handoff(action, body, *, profile_sha256):
        assert (action, body["version"], profile_sha256) == ("apply", "0.8.2-alpha.1", "fixture")
        events.append("handoff")
        return {"status": "reviewed", "result": {"action": "close_for_apply"}}

    monkeypatch.setattr(updater, "prepare", stage)
    monkeypatch.setattr(updater, "revalidate", revalidate)
    session = object.__new__(NormalEntrySession)
    session.state = "ready"
    session.update_profile = {"profile_sha256": "fixture"}
    session.request_manager_update = handoff
    panel = ManagerUpdateReviewPanel(updater, normal_entry_session=session)
    panel.close_for_apply.connect(lambda: events.append("close"))
    panel.resize(1100, 580)
    panel.show()
    app.processEvents()

    def capture(name):
        path = os.environ.get("MANAGER_UPDATE_UI_CAPTURES")
        if path:
            out = Path(path)
            out.mkdir(parents=True, exist_ok=True)
            assert panel.grab().save(str(out / f"{theme}-{outcome}-{name}.png"))

    try:
        assert panel.check.isVisible() and panel.stage.isHidden()
        capture("check")
        QTest.mouseClick(panel.check, Qt.LeftButton)
        wait_until(lambda: panel.stage.isVisible())
        assert "0.8.2-alpha.1 is available" in panel.status.text()
        assert "Unknown" not in panel.status.text() and panel.scope.isHidden()
        assert panel.stage.text() == "Update and restart"
        assert panel.stage.width() >= panel.stage.sizeHint().width()
        assert panel.apply.isHidden() and events == []
        capture("review")
        QTest.mouseClick(panel.stage, Qt.LeftButton)
        wait_until(stage_started.is_set)
        QTest.qWait(1100)
        assert panel.progress.isVisible() and panel.elapsed.text().startswith("Elapsed: 1")
        assert panel.cancel.isVisible() and panel.apply.isHidden()
        assert panel.progress.maximum() == 100 and panel.progress.value() == 25
        assert panel.status.text().startswith('Downloading')
        capture("progress")
        if cancel_stage:
            QTest.mouseClick(panel.cancel, Qt.LeftButton)
        release_stage.set()
        wait_until(lambda: not panel.close_held)
        if cancel_stage:
            assert events == ["stage"] and panel.check.isVisible()
            assert "Cancelled" in panel.status.text()
        elif outcome == "failure":
            assert events == ["stage"] and panel.check.isVisible()
            assert "Check again" in panel.status.text()
        else:
            assert events == ["stage", "revalidate", "handoff", "close"]
            assert panel.apply.isHidden() or not panel.apply.isEnabled()
        capture("result")
    finally:
        release_stage.set()
        panel.permit_close()
        panel.close()


def test_dashboard_update_check_opens_named_settings_tab(tmp_path):
    from suite_core.manager.window import create_window, unconfigured

    app = QApplication.instance() or QApplication([])
    feed = FakeFeed()
    updater = ManagerUpdateService(service(tmp_path, feed), current_version="0.8.1-alpha.1", channel="beta")
    window = create_window(unconfigured(datetime.now(timezone.utc)), manager_update_service=updater)
    window.show()
    app.processEvents()
    try:
        notice = window.dashboard.notifications
        assert "Check for Manager updates" in notice.text()
        notice.setFocus()
        QTest.keyClick(notice, Qt.Key_Tab)
        QTest.keyClick(notice, Qt.Key_Return)
        assert window.navigation.currentRow() == window.page_rows["Settings"]
        assert window.settings_tabs.tabText(window.settings_tabs.currentIndex()) == "Updates"
        panel = window.manager_update_panel
        wait_until(lambda: not panel.close_held)
        assert "No newer signed Manager update found" in panel.status.text()
        assert feed.calls
    finally:
        window.close()
        app.processEvents()
