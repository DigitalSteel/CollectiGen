"""Source-visible Organizer acquisition using inert, scoped transaction doubles."""
from threading import Event
from pathlib import Path
from datetime import datetime, timezone
from PySide6.QtCore import QTimer
from tools.qt_test_support import QtTestScope
from tests.test_organizer_installation import OrganizerFixture, no_external_actions
from suite_core.tests.test_encyclopedia_acquisition_ui import click,wait,queue_confirmation
from suite_core.tests.test_encyclopedia_install_progress import spin
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.manager.organizer_acquisition_ui import OrganizerAcquisitionPanel


def test_real_applications_entry_reaches_install_and_saved_completion(tmp_path,monkeypatch):
    from suite_core.manager.window import create_window,unconfigured
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont,QFontDatabase
    from qt_app.theme import apply_theme
    f=OrganizerFixture(tmp_path,monkeypatch);f.add();owner=ResourceExecutorOwner()
    with QtTestScope() as scope:
        # Existing Manager Tag-content source harness registers these installed
        # system fonts for readable offscreen captures; this changes no product.
        font_ids=[];previous_font=scope.app.font()
        for font_path in ('C:/Windows/Fonts/segoeui.ttf','C:/Windows/Fonts/segoeuib.ttf'):
            font_id=QFontDatabase.addApplicationFont(font_path);assert font_id>=0;font_ids.append(font_id)
        scope.app.setFont(QFont('Segoe UI',10))
        apply_theme(QApplication.instance(),'light')
        window=create_window(unconfigured(datetime.now(timezone.utc)),organizer_acquisition=f.service,organizer_executor=owner)
        window.resize(1920,1080);scope.show(window)
        panel=window.organizer_acquisition_panel
        try:
            item=window.navigation.item(window.page_rows['Applications'])
            window.statusBar().showMessage('Use Databases to connect a catalog, review content and recover interrupted work.')
            QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
            scope.settle();assert window.organizer_acquisition_action.isVisible()
            assert 'install or open Media Organizer' in window.statusBar().currentMessage()
            window.statusBar().showMessage('Installation needs attention; keep this window open.')
            window.refresh_section_commands()
            assert window.statusBar().currentMessage()=='Installation needs attention; keep this window open.'
            assert 'Entries marked In development cannot be installed yet.' in window.library_status.text()
            assert any(card.property('componentKey') == 'everday' for card in window.library_body.cards)
            assert 'Organizer' not in window.library_status.text()
            capture=tmp_path/'captures'
            capture.mkdir(parents=True,exist_ok=True)
            assert window.grab().save(str(capture/'applications-start-source.png'))
            click(window.organizer_acquisition_action);wait(panel)
            assert window.organizer_acquisition_dialog.isVisible()
            assert window.organizer_acquisition_dialog.grab().save(str(capture/'review-offer-source.png'))
            queue_confirmation();click(panel.install);wait(panel)
            assert f.store.selected(f.store.observe()) is not None
            assert panel.return_action.isVisible()
            assert window.organizer_acquisition_dialog.grab().save(str(capture/'installed-restart-source.png'))
            click(panel.return_action);assert not window.organizer_acquisition_dialog.isVisible()
            window.library_search.setText('no match');scope.settle()
            assert not window.organizer_workspace.isVisible()
            window.library_search.setText('organizer');scope.settle()
            assert window.organizer_workspace.isVisible()
            assert all(row.key!='media-organizer' for row in window.visible_development_entries)
        finally:
            panel.timer.stop();window.close();owner.begin_close()
            scope.app.setFont(previous_font)
            for font_id in font_ids:QFontDatabase.removeApplicationFont(font_id)


def test_explicit_organizer_install_and_preserved_launch_hold(tmp_path,monkeypatch):
    f=OrganizerFixture(tmp_path,monkeypatch);f.add();owner=ResourceExecutorOwner()
    with QtTestScope() as scope:
        panel=OrganizerAcquisitionPanel(f.service,owner);panel.resize(1100,780);scope.show(panel)
        try:
            assert panel.refresh.text()=='Find Organizer'
            assert 'libraries' in panel.scope_text() and not panel.details.isVisible()
            assert panel.refresh.property('primary') is True
            click(panel.refresh);wait(panel)
            assert panel.install.isEnabled() and 'Organizer' in panel.status.text()
            reviewed_details=[];confirm=panel.confirm_installation
            def inspect_confirmation(reviewed):
                reviewed_details.append(panel.details.toPlainText())
                return confirm(reviewed)
            monkeypatch.setattr(panel,'confirm_installation',inspect_confirmation)
            queue_confirmation();click(panel.install);wait(panel)
            assert reviewed_details[0].startswith('Organizer ')
            assert f.store.selected(f.store.observe()) is not None
            assert panel.open_action.isVisible() and not panel.open_action.isEnabled()
            assert 'Restart Manager' in panel.status.text()
            assert not panel.activity.isVisible()
            assert panel.health_accept.isVisible()
            assert 'Reader' not in panel.details.toPlainText()
            assert 'Restart Manager' in panel.details.toPlainText()
            # Synthetic health callback exercises shared result presentation;
            # it grants no native process or packaged health evidence.
            f.service.native_health=lambda selected: True
            panel.reconcile_launch();wait(panel)
            assert panel.installed.phase=='healthy'
            assert 'Organizer setup is complete' in panel.status.text()
            assert 'Status: Organizer setup is complete' in panel.details.toPlainText()
            assert 'Reader setup is complete' not in panel.details.toPlainText()
        finally:
            panel.timer.stop();panel.close();owner.begin_close()


def test_entry_failure_before_panel_retains_original_organizer_worker():
    from suite_core.manager.__main__ import EntryLifetimeOwner
    from time import monotonic,sleep
    executor=ResourceExecutorOwner();release=Event();entered=Event()
    def work():entered.set();assert release.wait(5)
    future=executor.submit(work);assert entered.wait(2)
    owner=EntryLifetimeOwner();owner.organizer_acquisition_executor=executor
    try:
        assert not owner.poll_cleanup() and owner.organizer_acquisition_executor is executor
        release.set();future.result(timeout=2)
        deadline=monotonic()+3
        while not owner.poll_cleanup() and monotonic()<deadline:sleep(.005)
        assert owner.closed and owner.organizer_acquisition_executor is None
    finally:release.set();executor.begin_close()


def test_organizer_delayed_preparation_has_moving_progress_and_responsive_cancel(tmp_path,monkeypatch):
    f=OrganizerFixture(tmp_path,monkeypatch);f.add();owner=ResourceExecutorOwner()
    entered,release=Event(),Event();original=f.service.install
    def delayed(reviewed,**kwargs):
        notify=kwargs.pop('phase_progress')
        def phase(value):
            notify(value)
            if value=='Preparing reader files':entered.set();assert release.wait(5)
        return original(reviewed,phase_progress=phase,**kwargs)
    monkeypatch.setattr(f.service,'install',delayed)
    with QtTestScope() as scope:
        panel=OrganizerAcquisitionPanel(f.service,owner);scope.show(panel)
        try:
            click(panel.refresh);wait(panel);queue_confirmation();click(panel.install)
            spin(scope,entered.is_set);spin(scope,lambda:'Preparing Organizer files' in panel.status.text())
            assert panel.activity.isVisible() and panel.activity.maximum()==0
            assert panel.elapsed.isVisible() and not panel.refresh.isEnabled()
            ticks=[];timer=QTimer(panel);timer.timeout.connect(lambda:ticks.append(1));timer.start(10)
            spin(scope,lambda:len(ticks)>1);timer.stop()
            click(panel.stop);assert panel.close_held and not panel.stop.isEnabled()
            release.set();wait(panel)
            assert not panel.activity.isVisible() and panel.refresh.isEnabled()
        finally:
            release.set()
            if panel.close_held:wait(panel)
            panel.timer.stop();panel.close();owner.begin_close()
