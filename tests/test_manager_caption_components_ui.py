"""Visible caption component choices and retained Manager return; synthetic service only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timezone
from threading import Event
from time import sleep, monotonic
from types import SimpleNamespace as NS

from PIL import Image
from PySide6.QtCore import Qt, QTimer, QObject, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton

from core.background_jobs import BackgroundJobManager
from core.settings import AppSettings
from core.generation_image_suggestions import ImageSetupProposal, ImageSetupSuggestionResult
from suite_core.manager.caption_components_ui import CaptionComponentsDialog
from suite_core.manager.description_setup_ui import DescriptionSetupDialog
from suite_core.manager.window import create_window, unconfigured
from test_manager_generator_setup_ui import application, wait_until, capture
from test_manager_description_setup_return_ui import open_image_setup


class CaptionFixture:
    def __init__(self, *, model=False, block=False):
        self.calls = []; self.components = False; self.model = model; self.block = block
        self.active = False; self.cancel_seen = Event(); self.stop = Event()

    @property
    def close_held(self): return self.active

    def status(self, settings, *, cancelled, progress):
        self.calls.append('status')
        if self.block:
            self.active = True
            try:
                while not cancelled(): sleep(.01)
                self.cancel_seen.set()
                while not self.stop.is_set(): sleep(.01)
                raise InterruptedError('Synthetic caption cancellation')
            finally: self.active = False
        return {'ready': self.components and self.model, 'review_token': 'reviewed-fixture', 'runtime_issue': '',
                'missing': [] if self.components and self.model else ['Selected caption model'],
                'components': [{'name': 'NVIDIA GPU', 'kind': 'hardware', 'installed': True, 'detail': 'Synthetic supported hardware'},
                               {'name': 'PyTorch', 'kind': 'package', 'installed': self.components, 'detail': 'Synthetic local status'},
                               {'name': 'Selected caption model', 'kind': 'model', 'installed': self.model, 'detail': ''}]}

    def install_runtime(self, settings, token, *, cancelled, progress):
        assert token == 'reviewed-fixture'; assert not cancelled()
        self.calls.append('install-runtime'); self.components = True
        return {'message': 'Synthetic caption components installed.', 'cleanup_pending': False}

    def download_model(self, settings, token, *, cancelled, progress):
        assert token == 'reviewed-fixture'; assert not cancelled()
        self.calls.append('download-model'); self.components = self.model = True
        return {'message': 'Synthetic selected model installed.', 'cleanup_pending': False}

    def suggest(self, image, settings, *, selected_kinds, cancelled):
        self.calls.append('suggest'); assert not cancelled()
        if not (self.components and self.model): raise RuntimeError('Synthetic caption components are missing.')
        return ImageSetupSuggestionResult((ImageSetupProposal('scene', 'A synthetic blue paper boat.',
                    'Local AI suggestion (uncertain)', 'fixture-caption-model'),), ('Review this optional proposal.',))


def test_downloads_require_visible_review_and_return_without_analysis(tmp_path):
    app=application();jobs=BackgroundJobManager()
    class VisibleOperations(CaptionFixture):
        def __init__(self):
            super().__init__();self.entered={key:Event() for key in ['status','install','model']}
            self.release={key:Event() for key in self.entered}
        def gate(self,key,options):
            self.entered[key].set();options['progress'](0,0,'Synthetic '+key+' activity')
            while not self.release[key].wait(.02):
                if options['cancelled']():raise InterruptedError('Synthetic operation cancelled')
        def status(self,settings,**options):
            self.gate('status',options);return super().status(settings,**options)
        def install_runtime(self,settings,token,**options):
            self.gate('install',options);return super().install_runtime(settings,token,**options)
        def download_model(self,settings,token,**options):
            self.gate('model',options);return super().download_model(settings,token,**options)
    runtime=VisibleOperations();settings=AppSettings(ai_models_directory=str(tmp_path/'model-parent'))
    dialog=CaptionComponentsDialog(runtime,settings,jobs);dialog.show();app.processEvents()
    assert runtime.calls==[] and dialog.check_button.isDefault()
    assert not dialog.install_button.isEnabled() and not dialog.model_button.isEnabled()
    capture(dialog,'caption-components-first-open')
    QTest.mouseClick(dialog.check_button,Qt.LeftButton)
    wait_until(app,lambda:runtime.entered['status'].is_set())
    assert dialog.activity.isVisible();capture(dialog,'caption-components-checking')
    runtime.release['status'].set()
    wait_until(app,lambda:dialog._task is None and dialog._review is not None)
    assert dialog.model_button.property('primary') is True and dialog.model_button.isDefault()
    capture(dialog,'caption-components-missing')
    questions=[];errors=[]
    def confirm(button,capture_name):
        deadline=monotonic()+10
        def choose():
            box=next((w for w in app.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible()),None)
            if box is None:
                if monotonic()>deadline:errors.append('Caption confirmation did not open');return
                QTimer.singleShot(20,choose);return
            try:
                assert box.windowTitle()=='Review Caption Download'
                assert box.defaultButton() is box.button(QMessageBox.No)
                questions.append(box.text());capture(box,capture_name)
                QTest.mouseClick(box.button(QMessageBox.Yes),Qt.LeftButton)
            except Exception as error:errors.append(str(error));box.reject()
        QTimer.singleShot(20,choose);QTest.mouseClick(button,Qt.LeftButton);assert not errors
    confirm(dialog.install_button,'caption-components-confirm-components')
    wait_until(app,lambda:runtime.entered['install'].is_set())
    assert dialog.activity.isVisible() and dialog.close_button.text()=='Cancel and Return'
    capture(dialog,'caption-components-installing');runtime.release['install'].set()
    wait_until(app,lambda:dialog._task is None and runtime.components and dialog._review is not None)
    confirm(dialog.model_button,'caption-components-confirm-model')
    wait_until(app,lambda:runtime.entered['model'].is_set())
    assert dialog.activity.isVisible();capture(dialog,'caption-components-downloading')
    runtime.release['model'].set()
    wait_until(app,lambda:dialog._task is None and runtime.model and dialog._review is not None)
    assert runtime.calls==['status','install-runtime','status','download-model','status']
    assert len(questions)==2 and 'No image analysis' in questions[1]
    assert 'NVIDIA runtime components under separate terms' in questions[0]
    assert 'License:' in questions[1] and 'Terms:' in questions[1]
    assert 'Ready for local image suggestions' in dialog.status.text()
    assert dialog.close_button.property('primary') is True and dialog.close_button.isDefault()
    assert not dialog.activity.isVisible() and not dialog.model_button.isEnabled()
    QTest.mouseClick(dialog.details_button,Qt.LeftButton);capture(dialog,'caption-components-ready-details')
    QTest.mouseClick(dialog.close_button,Qt.LeftButton);assert not dialog.isVisible();jobs.shutdown(wait=True)


def test_close_waits_for_original_cancelled_caption_operation(tmp_path):
    app = application(); runtime = CaptionFixture(block=True); jobs = BackgroundJobManager()
    dialog = CaptionComponentsDialog(runtime, AppSettings(ai_models_directory=str(tmp_path/'models')), jobs)
    dialog.show(); QTest.mouseClick(dialog.check_button, Qt.LeftButton)
    wait_until(app, lambda: runtime.active)
    try:
        dialog.close(); app.processEvents()
        wait_until(app, runtime.cancel_seen.is_set)
        assert dialog.isVisible() and dialog.close_held and dialog.activity.isVisible()
        assert dialog._task is not None
        capture(dialog, 'caption-components-cancelling')
    finally: runtime.stop.set()
    wait_until(app, lambda: dialog._task is None and not dialog.isVisible())
    assert not runtime.close_held; jobs.shutdown(wait=True)


def test_manager_image_review_uses_bound_provider_after_component_return(tmp_path, monkeypatch):
    app = application(); runtime = CaptionFixture(model=True)
    source = tmp_path/'generated-source.png'; Image.new('RGB',(20,20),'blue').save(source)
    original = source.read_bytes()
    session = NS(state='ready', caption_runtime=runtime, admitted=NS(layout=NS(data_root=tmp_path/'suite')))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1800,980); window.show(); window.navigation.setCurrentRow(window.page_rows['Generate'])
    flashes = []
    class VisibleOwnership(QObject):
        def eventFilter(self, widget, event):
            if (event.type() == QEvent.Show and isinstance(widget, QPushButton)
                    and widget.text().startswith('Review Local Description Setup')
                    and widget.parentWidget() is None): flashes.append(widget.text())
            return False
    ownership = VisibleOwnership(app); app.installEventFilter(ownership)
    owner, review = open_image_setup(window, app, source, monkeypatch)
    app.removeEventFilter(ownership)
    assert not flashes
    # The earlier source-picker watchdog rejects this new legitimate modal.
    # This route now owns its caption dialogs through the bounded drive below.
    window._description_test_watchdog.stop()
    review.name.setText('Retained caption setup'); review.part_fields['scene'].setText('Manual scene stays')
    goal = review.suggestion_goal.currentData()
    assert review.review_description_setup_button.isVisible()
    assert runtime.calls == []  # First-time component setup needs no failed analysis.
    monkeypatch.setattr(QMessageBox,'question',lambda *_a,**_k:QMessageBox.Yes)
    errors = []; stage = [0]; deadline = monotonic()+20
    def drive():
        if monotonic()>deadline:
            errors.append('Component return timed out')
            for widget in app.topLevelWidgets():
                if isinstance(widget,(CaptionComponentsDialog,DescriptionSetupDialog)):widget.reject()
            return
        try:
            config = next((x for x in app.topLevelWidgets() if isinstance(x,DescriptionSetupDialog) and x.isVisible()),None)
            components = next((x for x in app.topLevelWidgets() if isinstance(x,CaptionComponentsDialog) and x.isVisible()),None)
            if stage[0] == 0 and config is not None:
                assert config.components_button.isVisible(); stage[0] = 1
                QTest.mouseClick(config.components_button,Qt.LeftButton)
            elif stage[0] == 1 and components is not None:
                stage[0] = 2; QTest.mouseClick(components.check_button,Qt.LeftButton)
            elif stage[0] == 2 and components is not None and components._task is None and components._review is not None:
                stage[0] = 3; QTest.mouseClick(components.install_button,Qt.LeftButton)
            elif stage[0] == 3 and components is not None and components._task is None and components._review is not None and runtime.components:
                capture(components,'manager-caption-components-return'); stage[0] = 4
                QTest.mouseClick(components.close_button,Qt.LeftButton); return
        except Exception as error:
            errors.append(str(error))
            if components is not None:components.reject()
            if config is not None:config.reject()
            return
        QTimer.singleShot(20,drive)
    QTimer.singleShot(20,drive)
    QTest.mouseClick(review.review_description_setup_button,Qt.LeftButton)
    assert not errors and stage[0] == 4
    assert runtime.calls == ['status','install-runtime','status']
    assert review is owner._image_setup_dialog and review.name.text() == 'Retained caption setup'
    assert review.part_fields['scene'].text() == 'Manual scene stays' and review.suggestion_goal.currentData() == goal
    QTest.mouseClick(review.suggest_button,Qt.LeftButton)
    wait_until(app,lambda:review._suggestion_task is None and len(review._suggestion_candidates)==1)
    assert runtime.calls[-1] == 'suggest' and review.part_fields['scene'].text() == 'Manual scene stays'
    assert source.read_bytes() == original
    capture(review,'manager-caption-provider-proposal')
    window._description_test_watchdog.stop(); review.reject()
    wait_until(app,lambda:getattr(owner,'_image_setup_dialog',None) is None)
    window.close()
    if window.isVisible():
        wait_until(app,lambda:not owner.has_active_background_work(),timeout=30); window.close()
    wait_until(app,lambda:not window.isVisible(),timeout=30)


def test_caption_readiness_failure_can_be_reviewed_and_retried(tmp_path):
    app=application();jobs=BackgroundJobManager()
    class FailureOnce(CaptionFixture):
        def __init__(self):
            super().__init__(model=True);self.components=True;self.first=True
        def status(self,settings,**options):
            if self.first:
                self.first=False;self.calls.append('failed-status')
                raise RuntimeError('Synthetic worker unavailable; retained files unchanged.')
            return super().status(settings,**options)
    runtime=FailureOnce();settings=AppSettings(ai_models_directory=str(tmp_path/'unchanged-model-folder'))
    dialog=CaptionComponentsDialog(runtime,settings,jobs);dialog.show()
    QTest.mouseClick(dialog.check_button,Qt.LeftButton)
    wait_until(app,lambda:dialog._task is None and 'Synthetic worker unavailable' in dialog.status.text())
    assert dialog.check_button.property('primary') is True
    assert not dialog.install_button.isEnabled() and not dialog.model_button.isEnabled()
    capture(dialog,'caption-components-failure-retry')
    QTest.mouseClick(dialog.check_button,Qt.LeftButton)
    wait_until(app,lambda:dialog._task is None and dialog._review is not None)
    assert runtime.calls==['failed-status','status']
    assert settings.ai_models_directory==str(tmp_path/'unchanged-model-folder')
    assert dialog.close_button.property('primary') is True
    QTest.mouseClick(dialog.close_button,Qt.LeftButton);assert not dialog.isVisible()
    jobs.shutdown(wait=True)


def test_unsupported_caption_hardware_keeps_downloads_disabled(tmp_path):
    app=application();jobs=BackgroundJobManager()
    class Unsupported(CaptionFixture):
        def status(self,settings,**options):
            value=super().status(settings,**options)
            value['components'][0]['installed']=False
            return value
    runtime=Unsupported();dialog=CaptionComponentsDialog(runtime,AppSettings(ai_models_directory=str(tmp_path/'models')),jobs)
    dialog.show();QTest.mouseClick(dialog.check_button,Qt.LeftButton)
    wait_until(app,lambda:dialog._task is None and dialog._review is not None)
    assert not dialog.install_button.isEnabled() and not dialog.model_button.isEnabled()
    assert 'Nothing was installed' in dialog.status.text()
    capture(dialog,'caption-components-unsupported-hardware')
    QTest.mouseClick(dialog.close_button,Qt.LeftButton);assert runtime.calls==['status'];jobs.shutdown(wait=True)
