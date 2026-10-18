"""Visible panel component evidence; injected unsigned package/root seams."""
from concurrent.futures import Future
from contextlib import contextmanager
import os
from pathlib import Path
import time
import pytest
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QMessageBox, QStyle, QStyleOptionGroupBox, QToolButton

from core.encyclopedia_installation import EncyclopediaInstallation
from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
from tests.test_encyclopedia_installation import Fixture, no_external_actions
from suite_core.manager.resource_executor import ResourceExecutorOwner


@contextmanager
def executor_scope():
    executor = ResourceExecutorOwner()
    try:
        yield executor
    finally:
        executor.begin_close()
        assert not executor.close_held


def app():
    application = QApplication.instance() or QApplication([])
    if os.name == "nt":
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        assert font_id >= 0
        application.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    application.setFont(QFont("Segoe UI", 10))
    return application


def wait(panel):
    deadline = time.monotonic() + 5
    while panel.close_held and time.monotonic() < deadline:
        # QTest's wait does not release the Python GIL on this Windows build.
        # Yield to the retained Python worker before pumping native Qt events.
        time.sleep(0.002)
        QTest.qWait(20); panel.poll()
    assert not panel.close_held


def click(widget):
    assert widget.isEnabled() and widget.isVisible()
    QTest.mouseClick(widget, Qt.LeftButton, pos=QPoint(10, widget.height() // 2))
    app().processEvents()


_confirmation_timers = []

def queue_confirmation(accepted=True):
    timer = QTimer(); _confirmation_timers.append(timer)
    def answer():
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox): return
        timer.stop()
        QTest.mouseClick(box.button(QMessageBox.Yes if accepted else QMessageBox.No), Qt.LeftButton)
        _confirmation_timers.remove(timer)
    timer.timeout.connect(answer); timer.start(10)
    return timer

def toggle_details(group):
    if isinstance(group, QToolButton):
        QTest.mouseClick(group, Qt.LeftButton); app().processEvents(); return
    option = QStyleOptionGroupBox(); group.initStyleOption(option)
    point = group.style().subControlRect(QStyle.CC_GroupBox, option, QStyle.SC_GroupBoxCheckBox, group).center()
    QTest.mouseClick(group, Qt.LeftButton, pos=point); app().processEvents()

def install_with_confirmation(panel, accepted=True):
    queue_confirmation(accepted); click(panel.install); wait(panel)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_visible_panel_review_confirm_install_save_and_recovery(tmp_path, monkeypatch, theme):
    application = app()
    from qt_app.theme import apply_theme
    apply_theme(application, theme)
    app()
    f = Fixture(tmp_path, monkeypatch); f.add()
    changed = []
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor, connection_changed=lambda: changed.append(True))
        panel.resize(1000, 650); panel.show(); application.processEvents()
        try:
            assert not panel.install.isEnabled()
            click(panel.refresh); wait(panel)
            assert panel.install.isEnabled() and not panel.details.isVisible()
            install_with_confirmation(panel, False)
            assert panel.reviewed is None and not (f.root / "Apps").exists()
            assert "Publisher key:" in panel.details.toPlainText()
            assert panel.offer.release.artifact.sha256 in panel.details.toPlainText()
            captures = os.environ.get("ENCYCLOPEDIA_ACQUISITION_CAPTURE")
            if captures:
                output = Path(captures); output.mkdir(parents=True, exist_ok=True)
                assert panel.grab().save(str(output / (theme + "-collapsed.png")))
                toggle_details(panel.technical)
                assert panel.details.isVisible()
                assert panel.grab().save(str(output / (theme + "-expanded.png")))
                toggle_details(panel.technical)
            install_with_confirmation(panel)
            assert "close it normally to finish setup" in panel.status.text() and changed == [True]
            assert "ordinary restart" in panel.status.text()
            assert f.store.selected(f.store.observe()).version == "0.1.0-alpha.8"
            assert not panel.install.isEnabled() and not panel.retry_save.isEnabled()
            f.service.native_health = lambda selected: False
            panel.render(); click(panel.health_accept); wait(panel)
            assert "close it normally to finish setup" in panel.status.text() and panel.health_accept.isEnabled()
            assert "ordinary restart" in panel.status.text()
            f.service.native_health = lambda selected: selected == f.store.selected(f.store.observe())
            panel.reconcile_launch(); wait(panel)  # existing-owner settlement callback
            assert panel.installed.phase == "healthy" and "setup is complete" in panel.status.text()
            toggle_details(panel.technical)
            click(panel.recovery_confirm); click(panel.recover); wait(panel)
            assert f.store.selected(f.store.observe()) is None and "Recovery settled" in panel.status.text()
            if captures:
                assert panel.grab().save(str(output / (theme + "-recovered.png")))
            assert all(button.geometry().right() < panel.width() for button in panel.findChildren(QPushButton))
        finally:
            panel.timer.stop(); panel.close()


def test_save_failure_visible_retry_does_not_reapply(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    original = f.store.save
    monkeypatch.setattr(f.store, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("injected save failure")))
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor)
        panel.resize(1000,650); panel.show(); application.processEvents()
        try:
            click(panel.refresh); wait(panel); install_with_confirmation(panel)
            assert panel.retry_save.isEnabled() and "Retry Save" in panel.status.text()
            calls = list(f.calls)
            monkeypatch.setattr(f.store, "save", original)
            click(panel.retry_save); wait(panel)
            assert f.calls == calls and not panel.retry_save.isEnabled()
        finally:
            panel.timer.stop(); panel.close()


def test_cancel_and_close_hold_keep_original_worker_until_result_reconciliation(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    future = Future(); future.set_running_or_notify_cancel()
    class Executor:
        def submit(self, work):
            self.work = work
            return future
    executor = Executor(); panel = EncyclopediaAcquisitionPanel(f.service, executor)
    panel.resize(1000,650); panel.show(); application.processEvents()
    try:
        click(panel.refresh); assert panel.close_held
        panel.reconcile_launch(); assert panel.reconcile_queued
        click(panel.stop); assert panel.cancel.is_set() and panel.close_held
        assert not panel.reconcile_queued
        assert not panel.close_ready() and not future.cancelled()
        future.set_exception(InterruptedError("fixture cancellation settled"))
        assert panel.close_held  # retain even completed future until UI reconciles
        panel.poll()
        assert not panel.close_held and panel.close_ready()
        panel.reconcile_launch(); assert not panel.close_held and not panel.reconcile_queued
        panel.resume_after_cancelled_close(); assert panel.refresh.isEnabled()
    finally:
        panel.timer.stop(); panel.close()


def test_missing_native_admission_is_visible_and_never_enables_install(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch)
    held = EncyclopediaInstallation(f.acquisition, f.store)
    class Executor:
        def submit(self, work):
            raise AssertionError("Unavailable admission cannot dispatch")
    panel = EncyclopediaAcquisitionPanel(held, Executor())
    panel.show(); application.processEvents()
    try:
        assert "Installation is unavailable" in panel.status.text()
        assert not panel.refresh.isEnabled() and not panel.install.isEnabled()
        assert panel.close_ready() and not f.calls
    finally:
        panel.timer.stop(); panel.close()


@pytest.mark.parametrize("diagnostic", ["release_listing_bound", "exact_version"])
def test_refresh_failure_names_enabled_retry_and_preserves_installed_state(tmp_path, monkeypatch, diagnostic):
    application = app()
    f = Fixture(tmp_path, monkeypatch)
    installed = f.service.observe()
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    def held_refresh(**_options):
        raise ValueError(diagnostic)
    monkeypatch.setattr(f.service, "discover", held_refresh)
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor)
        panel.installed = installed
        panel.resize(1920, 1080)
        panel.show()
        application.processEvents()
        try:
            click(panel.refresh)
            wait(panel)
            assert panel.installed == installed and f.service.observe() == installed
            assert panel.offer is None and panel.reviewed is None
            assert panel.refresh.isEnabled()
            assert not panel.install.isEnabled()
            status = panel.status.text()
            assert "could not finish" in status and "unchanged" in status
            assert "Check Again" in status and "Review again" not in status
            assert "No newer" not in status and "up to date" not in status.lower()
            assert diagnostic in panel.details.toPlainText()
            assert panel.status.isVisible() and not panel.details.isVisible()
            toggle_details(panel.technical)
            assert panel.details.isVisible()
            captures = os.environ.get("ENCYCLOPEDIA_ACQUISITION_CAPTURE")
            if captures:
                output = Path(captures)
                output.mkdir(parents=True, exist_ok=True)
                assert panel.grab().save(str(output / ("refresh-hold-" + diagnostic + ".png")))
            assert {path: path.read_bytes() for path in before} == before
        finally:
            panel.timer.stop()
            panel.close()


def test_stale_confirmed_review_requires_a_new_visible_review_and_confirmation(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    reviews = []; original_review = f.service.review; original_unchanged = f.service._unchanged
    def reviewed(offer):
        result = original_review(offer); reviews.append(result); return result
    monkeypatch.setattr(f.service, 'review', reviewed)
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor)
        panel.resize(900,650); panel.show(); application.processEvents()
        try:
            click(panel.refresh); wait(panel)
            monkeypatch.setattr(f.service, '_unchanged', lambda *args: (_ for _ in ()).throw(ValueError('stale_review')))
            install_with_confirmation(panel)
            assert not (f.root/'Apps').exists() and panel.reviewed is None and panel.offer is None
            assert len(reviews) == 1 and 'stale_review' in panel.details.toPlainText()
            monkeypatch.setattr(f.service, '_unchanged', original_unchanged)
            click(panel.refresh); wait(panel); install_with_confirmation(panel, False)
            assert len(reviews) == 2 and reviews[1] is not reviews[0] and not (f.root/'Apps').exists()
            install_with_confirmation(panel)
            assert len(reviews) == 3 and reviews[2] is not reviews[1]
            assert f.store.selected(f.store.observe()).version == '0.1.0-alpha.8'
        finally:
            panel.timer.stop(); panel.close()


def test_visible_update_action_uses_fresh_review_and_preserves_cancel(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor)
        panel.resize(900,650); panel.show(); application.processEvents()
        try:
            click(panel.refresh); wait(panel); install_with_confirmation(panel)
            f.service.native_health = lambda selected: True
            click(panel.health_accept); wait(panel)
            f.add(version='0.1.0-alpha.9')
            click(panel.refresh); wait(panel)
            assert panel.install.text() == 'Update' and panel.install.isVisible()
            install_with_confirmation(panel, False)
            assert f.store.selected(f.store.observe()).version == '0.1.0-alpha.8'
            install_with_confirmation(panel)
            assert f.store.selected(f.store.observe()).version == '0.1.0-alpha.9'
        finally:
            panel.timer.stop(); panel.close()


def test_confirmation_failure_never_installs_or_reports_user_cancellation(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor)
        panel.resize(900,650); panel.show(); application.processEvents()
        try:
            click(panel.refresh); wait(panel)
            monkeypatch.setattr(panel, 'confirm_installation', lambda reviewed: (_ for _ in ()).throw(RuntimeError('synthetic confirmation failure')))
            click(panel.install); wait(panel)
            assert not (f.root/'Apps').exists() and panel.reviewed is None
            assert 'confirmation could not open' in panel.status.text()
            assert 'cancelled' not in panel.status.text() and 'synthetic confirmation failure' in panel.details.toPlainText()
        finally:
            panel.timer.stop(); panel.close()


def test_optional_markdown_notes_block_resources_and_progress_is_host_neutral(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    with executor_scope() as executor:
        panel = EncyclopediaAcquisitionPanel(f.service, executor)
        panel.resize(900,650); panel.show(); application.processEvents()
        try:
            click(panel.refresh); wait(panel)
            assert not panel.release_notes.isVisible() and not panel.details.isVisible()
            panel.release_notes.setMarkdown('## Changes\n\n- Readable notes\n\n![blocked](file:///synthetic-notes-image.png)')
            assert 'Changes' in panel.release_notes.toPlainText() and '##' not in panel.release_notes.toPlainText()
            toggle_details(panel.notes_toggle); assert panel.release_notes.isVisible()
            from PySide6.QtCore import QUrl
            assert panel.release_notes.loadResource(2,QUrl('file:///synthetic-notes-image.png')) is None
            panel.progress.put((1024*1024,2*1024*1024,'raw-private-counter-name'))
            panel.poll()
            assert '50%' in panel.status.text() and '2.0 MB' in panel.status.text()
            assert 'raw-private-counter-name' not in panel.status.text()
        finally:
            panel.timer.stop(); panel.close()


def test_cancel_review_drains_without_confirmation_or_install(tmp_path, monkeypatch):
    application = app(); f = Fixture(tmp_path, monkeypatch); f.add()
    offer,=f.service.discover(channel='beta')
    future=Future();future.set_running_or_notify_cancel()
    class Executor:
        def submit(self,work):self.work=work;return future
    executor=Executor();panel=EncyclopediaAcquisitionPanel(f.service,executor)
    panel.offer=offer;panel.render();panel.resize(900,650);panel.show();application.processEvents()
    monkeypatch.setattr(panel,'confirm_installation',lambda reviewed:pytest.fail('Cancelled review opened confirmation'))
    try:
        click(panel.install);click(panel.stop)
        assert 'Keep this window open' in panel.status.text() and 'Manager' not in panel.status.text()
        future.set_result(executor.work());panel.poll()
        assert panel.reviewed is None and not panel.close_held and not (f.root/'Apps').exists()
        assert 'Review cancelled' in panel.status.text()
    finally:
        panel.timer.stop();panel.close()


def test_empty_first_setup_names_missing_installable_release(tmp_path,monkeypatch):
    application=app();f=Fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(f.service,'discover',lambda **kwargs:())
    with executor_scope() as executor:
        panel=EncyclopediaAcquisitionPanel(f.service,executor)
        panel.resize(900,650);panel.show();application.processEvents()
        try:
            click(panel.refresh);wait(panel)
            assert 'No installable reader' in panel.status.text() and 'No newer' not in panel.status.text()
            assert not panel.install.isVisible() and panel.refresh.isVisible()
        finally:
            panel.timer.stop();panel.close()


@pytest.mark.parametrize('phase,update_visible',[('activated',False),('healthy',True)])
def test_new_offer_cannot_update_until_original_launch_and_close_settle(tmp_path,monkeypatch,phase,update_visible):
    from dataclasses import replace
    application=app();f=Fixture(tmp_path,monkeypatch);f.add()
    with executor_scope() as executor:
        panel=EncyclopediaAcquisitionPanel(f.service,executor)
        panel.resize(900,650);panel.show();application.processEvents()
        try:
            click(panel.refresh);wait(panel);install_with_confirmation(panel)
            panel.installed=replace(panel.installed,phase=phase)
            panel.offer=next(iter(f.acquisition._offers.values()));panel.render()
            assert panel.install.isVisible()==update_visible and panel.install.isEnabled()==update_visible
            if phase=='activated':assert 'close it normally to finish setup' in panel.status.text()
            else:assert panel.install.property('primary') is True
            assert panel.refresh.property('primary') is not True
        finally:
            panel.timer.stop();panel.close()
