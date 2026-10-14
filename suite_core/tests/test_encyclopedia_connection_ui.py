import json
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from suite_core.manager.encyclopedia_connection_ui import EncyclopediaConnectionPanel
from suite_core.tests.test_encyclopedia_acquisition_ui import queue_confirmation, toggle_details
from suite_core.manager.window import create_window, unconfigured
from datetime import datetime, timezone
from tests.test_encyclopedia_connection import package, Native
from core.encyclopedia_connection import OwnedEncyclopedia, KEY


def test_visible_review_save_launch_close_and_saved_return(tmp_path):
    app=QApplication.instance() or QApplication([])
    selected=package(tmp_path/'package');native=Native();owner=OwnedEncyclopedia(factory=lambda:native)
    path=tmp_path/'settings.json'
    window=create_window(unconfigured(datetime.now(timezone.utc)),reader_settings_path=path,reader_owner=owner)
    window.show();app.processEvents()
    index=window.page_rows['Applications']
    item=window.navigation.item(index)
    QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
    app.processEvents()
    from PySide6.QtWidgets import QPushButton
    button=window.findChild(QPushButton,'readerPrimaryAction')
    assert button is not None and button.isVisible()
    QTest.mouseClick(button,Qt.LeftButton);app.processEvents()
    panel=window.reader_panel
    QTest.keyClicks(panel.directory,str(selected.directory))
    QTest.mouseClick(panel.review,Qt.LeftButton);assert panel.save.isEnabled()
    assert not path.exists()
    QTest.mouseClick(panel.save,Qt.LeftButton)
    assert json.loads(path.read_text())[KEY]['version']==selected.version
    QTest.mouseClick(panel.launch,Qt.LeftButton)
    assert owner.close_held
    window.close();app.processEvents();assert window.isVisible() and window.close_policy.pending
    native.code=0;native.members=0
    QTest.qWait(400);app.processEvents()
    assert not owner.close_held and not window.isVisible()
    other=EncyclopediaConnectionPanel(path)
    assert other.selected==selected and other.launch.isEnabled()
    other.timer.stop();other.close()


def test_portable_connection_saves_but_launch_explains_root_hold(tmp_path):
    app=QApplication.instance() or QApplication([])
    selected=package(tmp_path/'package')
    panel=EncyclopediaConnectionPanel(tmp_path/'settings.json',normal_entry_session=object())
    panel.directory.setText(str(selected.directory));QTest.mouseClick(panel.review,Qt.LeftButton)
    QTest.mouseClick(panel.save,Qt.LeftButton)
    assert panel.selected==selected and not panel.launch.isEnabled()
    assert 'root admission' in panel.launch.toolTip()
    panel.timer.stop();panel.close();app.processEvents()


def test_failed_entry_reuses_original_reader_and_visible_stop(tmp_path):
    from PySide6.QtCore import QTimer
    from suite_core.manager.__main__ import EntryLifetimeOwner
    from suite_core.manager.owned_package_entry import HeldManagerEntry
    app=QApplication.instance() or QApplication([])
    selected=package(tmp_path/'package');native=Native();reader=OwnedEncyclopedia(factory=lambda:native)
    window=create_window(unconfigured(datetime.now(timezone.utc)),reader_settings_path=tmp_path/'settings.json',reader_owner=reader)
    entry=EntryLifetimeOwner();entry.reader_owner=reader;entry.window=window;entry.app=app
    window.show();window.open_reader_connection();app.processEvents()
    panel=window.reader_panel;panel.directory.setText(str(selected.directory))
    QTest.mouseClick(panel.review,Qt.LeftButton);QTest.mouseClick(panel.save,Qt.LeftButton)
    QTest.mouseClick(panel.launch,Qt.LeftButton)
    error=RuntimeError('synthetic entry failure');entry.preserve_failure(error)
    assert error.manager_entry_owner is entry and entry.reader_owner is reader and reader.child is native
    QTimer.singleShot(100,lambda:QTest.mouseClick(panel.stop,Qt.LeftButton))
    assert HeldManagerEntry(error,None).run_recovery()==1
    assert entry.closed and native.closed and not reader.close_held


def test_acquisition_only_failed_entry_retains_visible_original_worker(tmp_path,monkeypatch):
    from PySide6.QtCore import QTimer
    from suite_core.manager.__main__ import EntryLifetimeOwner
    from suite_core.manager.owned_package_entry import HeldManagerEntry
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from tests.test_encyclopedia_installation import Fixture
    app=QApplication.instance() or QApplication([])
    fixture=Fixture(tmp_path,monkeypatch)
    class RetainedThread:
        alive=True
        def start(self):pass
        def is_alive(self):return self.alive
    thread=RetainedThread();executor=ResourceExecutorOwner(thread_factory=lambda **kwargs:thread)
    reader=OwnedEncyclopedia()
    window=create_window(unconfigured(datetime.now(timezone.utc)),reader_settings_path=fixture.settings,
        reader_owner=reader,encyclopedia_acquisition=fixture.service,encyclopedia_executor=executor)
    entry=EntryLifetimeOwner();entry.reader_owner=reader;entry.window=window;entry.app=app
    entry.acquisition_panel=panel=window.acquisition_panel;entry.acquisition_executor=executor
    window.show();window.open_reader_acquisition();app.processEvents()
    QTest.mouseClick(panel.refresh,Qt.LeftButton)
    assert panel.close_held and not reader.close_held
    original_future=panel.future
    window.close();app.processEvents()
    assert window.isVisible() and window.close_policy.pending and panel.closing
    QTest.mouseClick(window.cancel_close_button,Qt.LeftButton);app.processEvents()
    assert not window.close_policy.pending and not panel.closing and panel.future is original_future
    error=RuntimeError('acquisition-only entry failure');entry.preserve_failure(error)
    assert error.manager_entry_owner is entry and entry.acquisition_panel is panel
    checks=[]
    def settle():
        assert window.acquisition_dialog.isVisible() and panel.cancel.is_set()
        assert entry.acquisition_executor is executor and entry.acquisition_panel is panel
        panel.future.set_exception(InterruptedError('original worker settled'))
        panel.poll()
        assert not entry.poll_cleanup() and executor.close_held
        checks.append('completed future does not erase live original thread')
        thread.alive=False
    QTimer.singleShot(100,settle)
    assert HeldManagerEntry(error,None).run_recovery()==1
    assert checks and entry.closed and executor.closed and not window.isVisible()
    panel.timer.stop();window.reader_panel.timer.stop()


def test_manager_visible_acquisition_connection_auto_status_and_update(tmp_path,monkeypatch):
    """Actual Applications controls with inert admission/transport/native doubles."""
    import time
    from PySide6.QtWidgets import QPushButton
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from tests.test_encyclopedia_installation import Fixture
    monkeypatch.setattr('core.encyclopedia_connection._visible_reader_window',lambda pid:True)
    app=QApplication.instance() or QApplication([])
    fixture=Fixture(tmp_path,monkeypatch);fixture.add()
    native=Native();reader=OwnedEncyclopedia(factory=lambda:native)
    fixture.service.native_health=reader.native_health
    executor=ResourceExecutorOwner()
    window=create_window(unconfigured(datetime.now(timezone.utc)),reader_settings_path=fixture.settings,
        reader_owner=reader,encyclopedia_acquisition=fixture.service,encyclopedia_executor=executor)
    window.show();app.processEvents()
    item=window.navigation.item(window.page_rows['Applications'])
    QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
    app.processEvents()
    def wait():
        end=time.monotonic()+5
        while window.acquisition_panel.close_held and time.monotonic()<end:
            time.sleep(.003);app.processEvents();window.acquisition_panel.poll()
        assert not window.acquisition_panel.close_held
        window.render_library();app.processEvents()
    def click(button):
        if button.objectName() in ('connectExistingEncyclopedia','acquireEncyclopedia'):
            card=next(card for card in window.library_body.cards if card.property('componentKey')=='encyclopedia-connection')
            button=card.findChild(QPushButton,button.objectName())
        if not button.isVisible():
            from PySide6.QtWidgets import QToolButton
            card=next(card for card in window.library_body.cards if card.property('componentKey')=='encyclopedia-connection')
            toggle=card.findChild(QToolButton)
            if not toggle.isChecked(): QTest.mouseClick(toggle,Qt.LeftButton);app.processEvents()
        assert button.isVisible() and button.isEnabled()
        from PySide6.QtWidgets import QCheckBox,QStyle,QStyleOptionButton
        if isinstance(button,QCheckBox):
            option=QStyleOptionButton();button.initStyleOption(option)
            point=button.style().subElementRect(QStyle.SE_CheckBoxIndicator,option,button).center()
            QTest.mouseClick(button,Qt.LeftButton,pos=point)
        else:
            QTest.mouseClick(button,Qt.LeftButton)
        app.processEvents()
    try:
        click(window.findChild(QPushButton,'acquireEncyclopedia'));wait()
        panel=window.acquisition_panel
        assert not fixture.calls  # opening reconciles only local status
        click(panel.refresh);wait();queue_confirmation();click(panel.install);wait()
        first=fixture.store.selected(fixture.store.observe())
        assert first==window.reader_panel.selected and panel.installed.phase=='activated'
        click(window.findChild(QPushButton,'connectExistingEncyclopedia'))
        click(window.reader_panel.launch)
        window.reader_panel.poll();assert not reader.native_health(first)
        native.code=0;native.members=0
        window.reader_panel.poll();wait()
        assert panel.installed.phase=='healthy' and 'complete' in panel.status.text()
        fixture.add('0.1.0-alpha.9',b'INERT UPDATE CODE')
        click(window.findChild(QPushButton,'acquireEncyclopedia'));wait()
        click(panel.refresh);wait();queue_confirmation();click(panel.install);wait()
        assert fixture.store.selected(fixture.store.observe()).version=='0.1.0-alpha.9'
        assert first.program.read_bytes()==b'INERT NOT EXECUTABLE'
        assert fixture.sentinel.read_bytes()==b'{"unknown_future":[1,2]}\n'
        toggle_details(panel.technical);click(panel.recovery_confirm);click(panel.recover);wait()
        assert fixture.store.selected(fixture.store.observe())==first
    finally:
        panel.timer.stop();window.reader_panel.timer.stop();window.close();app.processEvents()
        assert not executor.close_held
        executor.begin_close()


def test_first_frozen_save_requires_restart_and_never_allocates_reader(tmp_path):
    from types import SimpleNamespace
    app=QApplication.instance() or QApplication([])
    selected=package(tmp_path/'reader');calls=[]
    session=SimpleNamespace(reader_selection=None)
    owner=OwnedEncyclopedia(factory=lambda:calls.append(True),normal_entry_session=session,launch_allowed=False)
    panel=EncyclopediaConnectionPanel(tmp_path/'settings.json',normal_entry_session=session,owner=owner)
    try:
        panel.directory.setText(str(selected.directory));QTest.mouseClick(panel.review,Qt.LeftButton)
        QTest.mouseClick(panel.save,Qt.LeftButton)
        assert panel.selected==selected and not panel.launch.isEnabled()
        assert 'Restart Manager to launch this reader' in panel.status.text()
        panel.launch_reader();assert calls==[] and not owner.close_held
        panel.reload_saved();assert not panel.launch.isEnabled()
        assert 'Restart Manager' in panel.status.text()
    finally:panel.timer.stop();panel.close();app.processEvents()


def test_changed_saved_reader_requires_exact_startup_selection_then_restart(tmp_path):
    from types import SimpleNamespace
    from core.encyclopedia_connection import ReaderConnectionStore
    app=QApplication.instance() or QApplication([])
    first=package(tmp_path/'first');second=package(tmp_path/'second');path=tmp_path/'settings.json'
    store=ReaderConnectionStore(path);store.save(first,expected=store.observe());calls=[]
    session=SimpleNamespace(reader_selection=SimpleNamespace(selected=first))
    owner=OwnedEncyclopedia(factory=lambda:calls.append(True),normal_entry_session=session)
    panel=EncyclopediaConnectionPanel(path,normal_entry_session=session,owner=owner)
    try:
        assert panel.selected==first and panel.launch.isEnabled()
        panel.directory.setText(str(second.directory));QTest.mouseClick(panel.review,Qt.LeftButton)
        QTest.mouseClick(panel.save,Qt.LeftButton)
        assert panel.selected==second and not panel.launch.isEnabled()
        assert 'Restart Manager to launch this reader' in panel.status.text()
        panel.launch_reader();assert calls==[] and not owner.close_held
        panel.reload_saved();assert panel.selected==second and not panel.launch.isEnabled()
        restarted_session=SimpleNamespace(reader_selection=SimpleNamespace(selected=second))
        restarted=EncyclopediaConnectionPanel(path,normal_entry_session=restarted_session,
            owner=OwnedEncyclopedia(normal_entry_session=restarted_session))
        try:
            assert restarted.selected==second and restarted.launch.isEnabled()
            assert 'Restart Manager' not in restarted.status.text()
        finally:restarted.timer.stop();restarted.close()
        panel.directory.setText(str(first.directory));QTest.mouseClick(panel.review,Qt.LeftButton)
        QTest.mouseClick(panel.save,Qt.LeftButton)
        assert panel.selected==first and panel.launch.isEnabled()  # exact original admission restored
    finally:panel.timer.stop();panel.close();app.processEvents()


def test_same_folder_new_inventory_is_not_the_startup_admitted_reader(tmp_path):
    from types import SimpleNamespace
    from core.encyclopedia_connection import ReaderConnectionStore,review_directory
    app=QApplication.instance() or QApplication([])
    first=package(tmp_path/'reader');path=tmp_path/'settings.json';store=ReaderConnectionStore(path)
    store.save(first,expected=store.observe())
    session=SimpleNamespace(reader_selection=SimpleNamespace(selected=first))
    owner=OwnedEncyclopedia(normal_entry_session=session)
    panel=EncyclopediaConnectionPanel(path,normal_entry_session=session,owner=owner)
    try:
        assert panel.launch.isEnabled()
        inventory=first.directory/'BOOTSTRAP_INVENTORY.json';data=json.loads(inventory.read_bytes())
        data['software_version']='0.1.0-alpha.9';inventory.write_text(json.dumps(data),encoding='utf-8')
        changed=review_directory(first.directory);assert changed!=first
        QTest.mouseClick(panel.review,Qt.LeftButton);QTest.mouseClick(panel.save,Qt.LeftButton)
        assert panel.selected==changed and not panel.launch.isEnabled()
        assert 'Restart Manager to launch this reader' in panel.status.text()
    finally:panel.timer.stop();panel.close();app.processEvents()


def test_restart_guidance_readable_light_dark_1080(tmp_path):
    from types import SimpleNamespace
    from PySide6.QtGui import QFont,QFontDatabase
    from qt_app.theme import apply_theme
    app=QApplication.instance() or QApplication([])
    assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')>=0
    app.setFont(QFont('Segoe UI',10));selected=package(tmp_path/'reader')
    session=SimpleNamespace(reader_selection=None)
    panel=EncyclopediaConnectionPanel(tmp_path/'settings.json',normal_entry_session=session)
    panel.resize(1920,1080);panel.show();app.processEvents()
    captures=__import__('pathlib').Path(__file__).resolve().parents[2]/'.tmp/manager-acquisition-readiness-20261011/captures1080'
    captures.mkdir(parents=True,exist_ok=True)
    try:
        panel.directory.setText(str(selected.directory));QTest.mouseClick(panel.review,Qt.LeftButton)
        QTest.mouseClick(panel.save,Qt.LeftButton)
        for theme in ('light','dark'):
            apply_theme(app,theme,False);app.processEvents()
            assert panel.status.isVisible() and 'Restart Manager' in panel.status.text()
            assert not panel.launch.isEnabled() and panel.reload.isEnabled()
            assert panel.grab().save(str(captures/(theme+'-restart.png')))
    finally:panel.timer.stop();panel.close();app.processEvents()
