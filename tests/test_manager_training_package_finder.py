"""Manager interrupted-package discovery; synthetic data, no trainer or recovery writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from time import monotonic
import json
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QMessageBox
from core import ImageRecord, LibraryImageIdentity, TagDatabase, TrainingCandidateSelectionService, TrainingGoal, TrainingPermissionDisposition
from core.training_package import TrainingPackageService
from qt_app.theme import apply_theme
from qt_app.training_page import TrainingRecoveryFinderDialog
from suite_core.manager.window import create_window, unconfigured
from test_manager_generator_setup_ui import application, wait_until, capture


def interrupted_package(tmp_path, monkeypatch):
    import core.training_package as package
    images=[]; identities={}
    for number in range(2):
        image=tmp_path/'library'/('sample-%d.png' % number)
        image.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(768+number,896),(number*20,40,80)).save(image)
        record=ImageRecord(image);record.annotation.sdxl_tags=['synthetic','pose_%d' % number]
        record.modified=True;record.save_sidecar(force=True);images.append(image)
        identities[image]=LibraryImageIdentity('synthetic',image.name)
    db=TagDatabase(tmp_path/'tags.json');selection=TrainingCandidateSelectionService(db)
    plan=selection.plan(images,TrainingGoal('Synthetic recovery',kind='character',base_model_id='sha256:'+'a'*64),identities=identities)
    plan=selection.apply_permission_disposition(plan,TrainingPermissionDisposition('proceed','Generated synthetic test scope only.',True))
    service=TrainingPackageService(db);folder=tmp_path/'exports';folder.mkdir()
    prepared=service.preview(plan,folder/'Synthetic Dataset Package')
    with monkeypatch.context() as patch:
        def stopped(*_args,**_kwargs):raise OSError('Synthetic interruption before package promotion')
        patch.setattr(package,'_rename_directory_no_replace',stopped)
        result=service.apply(prepared,prepared.decision.execution())
    assert result.status=='recovery_required',result
    assert result.recovery_receipt.is_file()
    return folder,result.recovery_receipt


def family_bytes(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def manager(tmp_path,theme='dark'):
    app=application();session=NS(state='ready',admitted=NS(layout=NS(data_root=tmp_path/'Suite/Data')))
    window=create_window(unconfigured(datetime.now(timezone.utc)),normal_entry_session=session,theme=theme)
    window.resize(1920,1040);window.show();app.processEvents()
    item=window.navigation.item(window.page_rows['Training'])
    QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
    QTest.mouseClick(window.training_package_recovery_button,Qt.LeftButton)
    app.processEvents();finder=window._training_package_recovery_dialog
    assert type(finder) is TrainingRecoveryFinderDialog and finder.isVisible()
    return app,session,window,finder


def close_manager(app,window):
    finder=window._training_package_recovery_dialog
    if finder is not None:finder.reject()
    if window._training_package_recovery_task is not None:window._training_package_recovery_cancel.set()
    state=getattr(window,'state',None)
    if state is not None:wait_until(app,lambda:not state.jobs.active())
    window.close();app.processEvents()
    if window.isVisible():window.close();app.processEvents()
    assert not window.isVisible()
    if state is not None:state.jobs.shutdown(wait=True)
    window.deleteLater();app.processEvents()


@pytest.mark.parametrize('route',['folder','exact'])
@pytest.mark.parametrize('theme',['dark','light'])
def test_visible_finder_to_original_read_only_review(tmp_path,monkeypatch,route,theme):
    app=application();apply_theme(app,theme)
    folder,receipt=interrupted_package(tmp_path,monkeypatch);before=family_bytes(folder)
    # An unrelated malformed receipt is excluded by the real canonical finder.
    invalid=folder/'.foreign.collectigen-invalid.recovery.json';invalid.write_text('{"not_a_package":true}',encoding='utf-8')
    before=family_bytes(folder)
    app,session,window,finder=manager(tmp_path,theme)
    boxes=[];errors=[];timer=QTimer();deadline=monotonic()+20
    try:
        capture(finder,'training-finder-'+route+'-'+theme+'-empty')
        if route=='folder':
            monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *_a,**_k:str(folder))
            QTest.mouseClick(finder.folder_button,Qt.LeftButton)
            wait_until(app,lambda:window._training_package_recovery_task is None,timeout=15)
            assert finder.items.topLevelItemCount()==1
            assert finder.items.topLevelItem(0).text(0)=='Synthetic Dataset Package'
            assert finder.review_button.isEnabled()
            capture(finder,'training-finder-'+route+'-'+theme+'-choices')
        else:
            monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *_a,**_k:(str(receipt),'Training recovery receipt (*.recovery.json)'))
        def finish_preview():
            try:
                assert monotonic()<deadline,'Original preview did not complete'
                box=window._training_package_recovery_dialog
                if isinstance(box,QMessageBox) and box.isVisible():
                    assert 'Manager did not change any package files.' in box.text()
                    assert 'original package workspace' in box.text()
                    boxes.append(box.text());capture(box,'training-finder-'+route+'-'+theme+'-preview')
                    button=box.button(QMessageBox.Close) or box.button(QMessageBox.Ok)
                    assert button is not None,'Original preview needs an actual observed dismissal action'
                    QTest.mouseClick(button,Qt.LeftButton)
            except BaseException as error:
                errors.append(repr(error));box=window._training_package_recovery_dialog
                if box is not None:box.reject()
        timer.timeout.connect(finish_preview);timer.start(15)
        QTest.mouseClick(finder.review_button if route=='folder' else finder.browse_receipt_button,Qt.LeftButton)
        wait_until(app,lambda:bool(boxes) or bool(errors),timeout=20)
        assert not errors,errors;assert len(boxes)==1
        assert window._training_package_recovery_dialog is None
        assert window._training_package_recovery_task is None
        assert family_bytes(folder)==before
    finally:
        timer.stop();close_manager(app,window)


class Signals(QObject):
    complete=Signal(object)
    failed=Signal(object)
    cancelled=Signal()


@pytest.mark.parametrize('mode',['busy','failed','cancelled','close','revoked','replaced'])
def test_search_failure_late_result_and_close_do_not_open_preview(tmp_path,monkeypatch,mode):
    tasks=[]
    from qt_app import workers
    class Task:
        def __init__(self,*args,**kwargs):self.signals=Signals();self.work=args[2];self.manager=args[0];self.name=args[1];self.job_id="";tasks.append(self)
        def start(self):
            if len(tasks)>1:
                self.signals.complete.emit(())
                return
            if mode=='busy':raise RuntimeError('Synthetic coordinator busy')
            if mode=='failed':self.signals.failed.emit('Synthetic read failure')
    monkeypatch.setattr(workers,'CoordinatedCallTask',Task)
    app,session,window,finder=manager(tmp_path)
    folder=tmp_path/'Synthetic Interrupted Packages';folder.mkdir();marker=folder/'unrelated.txt';marker.write_bytes(b'preserve synthetic marker')
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *_a,**_k:str(folder))
    try:
        QTest.mouseClick(finder.folder_button,Qt.LeftButton);assert len(tasks)==1;task=tasks[0]
        if mode=='cancelled':task.signals.cancelled.emit()
        elif mode=='close':
            assert window.close() is False
            assert window._training_package_recovery_dialog is None
            assert window._training_package_recovery_cancel.is_set()
            # A completion already in flight after Cancel cannot reopen a preview.
            task.signals.complete.emit(())
        elif mode in ['revoked','replaced']:
            if mode=='revoked':session.state='closed'
            else:window.state=NS(**vars(window.state))
            task.signals.complete.emit(())
            assert 'session ended or changed' in finder.status.text()
            assert not finder.folder_button.isEnabled() and not finder.browse_receipt_button.isEnabled()
        app.processEvents();assert window._training_package_recovery_task is None
        assert not [x for x in app.topLevelWidgets() if isinstance(x,QMessageBox) and x.isVisible()]
        assert marker.read_bytes()==b'preserve synthetic marker'
        if mode in ['busy','failed','cancelled']:
            assert finder.isVisible() and finder.folder_button.isEnabled() and finder.browse_receipt_button.isEnabled()
            assert 'retry' in finder.status.text().lower() or 'choose' in finder.status.text().lower()
            capture(finder,'training-finder-'+mode)
            QTest.mouseClick(finder.folder_button,Qt.LeftButton)
            assert len(tasks)==2 and window._training_package_recovery_task is None
            assert 'No interrupted packages found' in finder.status.text()
            capture(finder,'training-finder-'+mode+'-retry-empty')
    finally:close_manager(app,window)


@pytest.mark.parametrize('route',['folder','exact'])
def test_cancelled_picker_keeps_finder_and_chooses_nothing(tmp_path,monkeypatch,route):
    app,session,window,finder=manager(tmp_path);before=finder.status.text()
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *_a,**_k:'')
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *_a,**_k:('',''))
    try:
        QTest.mouseClick(finder.folder_button if route=='folder' else finder.browse_receipt_button,Qt.LeftButton)
        assert window._training_package_recovery_dialog is finder and finder.isVisible()
        assert finder.status.text()==before and window._training_package_recovery_task is None
        assert window._training_package_picker_open is False
    finally:close_manager(app,window)


@pytest.mark.parametrize('ending',['session','manager-close'])
def test_selected_receipt_does_not_reopen_after_session_or_manager_close(tmp_path,monkeypatch,ending):
    from core.training_package import TrainingRecoveryChoice
    app,session,window,finder=manager(tmp_path);receipt=tmp_path/'synthetic.recovery.json';receipt.write_bytes(b'Never read after Close')
    def denied(*_a,**_k):raise AssertionError('Late selection must not inspect a receipt')
    monkeypatch.setattr(TrainingPackageService,'preview_recovery',denied)
    finder.show_choices(tmp_path,(TrainingRecoveryChoice(receipt,'Synthetic package','staged_complete'),))
    def ended(_result):
        if ending=='session':session.state='closed'
        else:window.close()
    finder.finished.connect(ended)
    try:
        QTest.mouseClick(finder.review_button,Qt.LeftButton)
        app.processEvents();QTest.qWait(20);app.processEvents()
        assert window._training_package_recovery_dialog is None and window._training_package_recovery_task is None
        assert receipt.read_bytes()==b'Never read after Close'
    finally:close_manager(app,window)


@pytest.mark.parametrize('theme',['dark','light'])
def test_delayed_search_shared_progress_and_safe_cancel(tmp_path,monkeypatch,theme):
    from threading import Event
    from time import sleep
    app=application();apply_theme(app,theme)
    app,session,window,finder=manager(tmp_path,theme);folder=tmp_path/'Synthetic Slow Package Location';folder.mkdir()
    entered=Event()
    def slow(_folder,*,should_cancel):
        entered.set();deadline=monotonic()+10
        while monotonic()<deadline:
            if should_cancel():raise InterruptedError('Synthetic search cancelled')
            sleep(.01)
        raise AssertionError('Safe Cancel did not reach the original operation')
    monkeypatch.setattr(TrainingPackageService,'recovery_choices',staticmethod(slow))
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *_a,**_k:str(folder))
    try:
        QTest.mouseClick(finder.folder_button,Qt.LeftButton)
        wait_until(app,entered.is_set)
        QTest.qWait(1250);app.processEvents()
        progress=finder._window_job_progress
        assert progress.phase.isVisible() and progress.activity.isVisible() and progress.stop.isEnabled()
        assert 'Synthetic Slow Package Location' in progress.phase.accessibleDescription()
        assert 'Elapsed 1s' in progress.bar.accessibleDescription(),progress.bar.accessibleDescription()
        capture(finder,'training-finder-delayed-'+theme)
        QTest.mouseClick(progress.stop,Qt.LeftButton)
        wait_until(app,lambda:window._training_package_recovery_task is None)
        assert 'stopped' in finder.status.text().lower()
        assert finder.folder_button.isEnabled() and not progress.stop.isVisible()
        assert list(folder.iterdir())==[]
        capture(finder,'training-finder-cancelled-'+theme)
    finally:close_manager(app,window)
