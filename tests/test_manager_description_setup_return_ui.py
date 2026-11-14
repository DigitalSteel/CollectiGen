"""Visible Manager image setup and saved caption-settings return; fake inference only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from time import monotonic

import pytest
from PIL import Image
from PySide6.QtCore import Qt,QTimer,QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog,QDialogButtonBox,QPushButton,QMessageBox,QScrollArea
from suite_core.manager.window import create_window,unconfigured
from suite_core.manager.description_setup_ui import DescriptionSetupDialog
from qt_app.generation_setup_source_dialog import GenerationSetupSourceDialog
from test_manager_generator_setup_ui import application,wait_until,capture,FixtureRuntime


def open_image_setup(window,app,path,monkeypatch):
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getOpenFileName",lambda *_a,**_k:(str(path),""))
    errors=[]; deadline=monotonic()+15
    watchdog=QTimer(window);watchdog.setInterval(100)
    def unexpected():
        for widget in app.topLevelWidgets():
            if not widget.isVisible() or not isinstance(widget,QDialog):continue
            if isinstance(widget,(GenerationSetupSourceDialog,DescriptionSetupDialog)):continue
            if widget.__class__.__name__=="ImageGenerationSetupDialog":continue
            text=widget.text() if isinstance(widget,QMessageBox) else widget.windowTitle()
            errors.append(f"Unexpected {type(widget).__name__}: {text}")
            print(errors[-1],flush=True);capture(widget,"description-unexpected-dialog")
            widget.reject()
    watchdog.timeout.connect(unexpected);watchdog.start();window._description_test_watchdog=watchdog
    def choose():
        picker=next((w for w in app.topLevelWidgets() if isinstance(w,GenerationSetupSourceDialog) and w.isVisible()),None)
        if picker is None:
            assert monotonic()<deadline
            QTimer.singleShot(20,choose);return
        try:
            assert picker.image.isVisible() and not picker.character.isVisible()
            capture(picker,"description-image-source-choice")
            QTest.mouseClick(picker.image,Qt.LeftButton,pos=QPoint(8,picker.image.height()//2))
            assert picker.source_kind=="image", "The observed image radio must be selected before Continue"
            QTest.mouseClick(picker.buttons.button(QDialogButtonBox.Ok),Qt.LeftButton)
        except Exception as error:errors.append(error);picker.reject()
    QTimer.singleShot(20,choose)
    button=next(b for b in window.findChildren(QPushButton) if b.text()=="Create Generation Setup…")
    QTest.mouseClick(button,Qt.LeftButton)
    assert not errors
    try:wait_until(app,lambda:getattr(window,"_generation_dialog",None) is not None)
    except AssertionError:raise AssertionError(errors or "Image draft did not open") from None
    owner=window._generation_dialog
    wait_until(app,lambda:getattr(owner,"_image_setup_dialog",None) is not None)
    review=owner._image_setup_dialog
    review.task_tabs.setCurrentIndex(1)
    wait_until(app,lambda:review.suggest_button.isEnabled())
    assert not errors
    return owner,review


@pytest.mark.parametrize("outcome",["save","cancel","stale_source","ended_session"])
def test_visible_description_setup_returns_to_exact_review(tmp_path,monkeypatch,outcome):
    app=application();monkeypatch.setattr("core.swarm_runtime.SwarmRuntimeManager",FixtureRuntime)
    calls=[]
    def missing(*_a,**_k):
        calls.append("explicit suggestion")
        raise ValueError("Generated missing-caption-model fixture")
    monkeypatch.setattr("core.generation_image_suggestions.suggest_image_setup_parts",missing)
    path=tmp_path/"generated-source.png";Image.new("RGB",(20,20),"blue").save(path)
    original_image=path.read_bytes()
    session=NS(state="ready",admitted=NS(layout=NS(data_root=tmp_path/"suite")))
    window=create_window(unconfigured(datetime.now(timezone.utc)),normal_entry_session=session)
    window.resize(1800,980);window.show();window.navigation.setCurrentRow(window.page_rows["Generate"])
    owner,review=open_image_setup(window,app,path,monkeypatch)
    review.name.setText("Keep this unfinished setup")
    review.part_fields["scene"].setText("Retain this manually reviewed scene")
    goal=review.suggestion_goal.currentData();original=owner.plan.digest;settings=review.settings
    QTest.mouseClick(review.suggest_button,Qt.LeftButton)
    wait_until(app,lambda:review._suggestion_task is None and review.review_description_setup_button.isVisible())
    assert calls==["explicit suggestion"]
    config_path=window.state.generation_settings_path
    before=config_path.read_bytes() if config_path.exists() else None
    errors=[];deadline=monotonic()+15
    def configure():
        child=next((w for w in app.topLevelWidgets() if isinstance(w,DescriptionSetupDialog) and w.isVisible()),None)
        if child is None:
            assert monotonic()<deadline
            QTimer.singleShot(20,configure);return
        try:
            assert calls==["explicit suggestion"]
            window.close();assert window.isVisible()
            assert "Generation Setup" in window.statusBar().currentMessage()
            capture(child,"description-settings-"+outcome)
            child.directory.setText(str(tmp_path/"existing-model-parent"))
            child.controls["joycaption_max_new_tokens"].setValue(512)
            if outcome=="cancel":QTest.mouseClick(child.cancel_button,Qt.LeftButton);return
            if outcome=="stale_source":Image.new("RGB",(20,20),"red").save(path)
            if outcome=="ended_session":
                session.state="ended"
                QTest.mouseClick(child.save_button,Qt.LeftButton)
                assert child.isVisible() and child.saved_snapshot is None
                QTest.mouseClick(child.cancel_button,Qt.LeftButton);return
            QTest.mouseClick(child.save_button,Qt.LeftButton)
            if child.isVisible():
                errors.append(AssertionError(child.status.text()));child.reject()
        except Exception as error:errors.append(error);child.reject()
    QTimer.singleShot(20,configure)
    QTest.mouseClick(review.review_description_setup_button,Qt.LeftButton)
    assert not errors
    assert review is owner._image_setup_dialog and review.settings is settings
    assert review.name.text()=="Keep this unfinished setup"
    assert review.part_fields["scene"].text()=="Retain this manually reviewed scene"
    assert owner.plan.digest==original and calls==["explicit suggestion"]
    if outcome in {"save","stale_source"}:
        assert settings.ai_models_directory==str(tmp_path/"existing-model-parent")
        assert settings.joycaption_max_new_tokens==512 and config_path.exists()
    else:
        assert (config_path.read_bytes() if config_path.exists() else None)==before
    if outcome=="stale_source":
        assert not review.suggest_button.isEnabled() and not review.save_button.isEnabled()
        assert "selected image changed" in review.suggestion_status.text()
    elif outcome!="ended_session":
        assert review.suggestion_goal.currentData()==goal
        assert "choices retained" in review.suggestion_status.text()
    capture(review,"description-return-"+outcome)
    window._description_test_watchdog.stop()
    # Restore only injected synthetic conditions for fixture teardown; no product recovery or real session is resumed.
    if outcome=="ended_session":session.state="ready"
    if outcome=="stale_source":path.write_bytes(original_image)
    review.reject();wait_until(app,lambda:getattr(owner,"_image_setup_dialog",None) is None)
    window.close()
    if window.isVisible():
        wait_until(app,lambda:not owner.has_active_background_work(),timeout=30)
        window.close()  # Existing Close contract explicitly asks to close again after owned work settles.
    try:wait_until(app,lambda:not window.isVisible(),timeout=30)
    except AssertionError:
        raise AssertionError({"manager":window.statusBar().currentMessage(),"draft":owner.draft_status.text(),
                              "status":owner.status.text(),"pending":owner._pending}) from None


def test_native_source_chooser_default_keeps_lazy_entity_route():
    app=application();calls=[]
    database=NS(characters={},outfits={})
    dialog=GenerationSetupSourceDialog(lambda:calls.append(True) or database)
    dialog.show();assert not dialog.image.isVisible() and calls==[]
    QTest.mouseClick(dialog.character,Qt.LeftButton,pos=QPoint(8,dialog.character.height()//2))
    assert calls==[True] and dialog.source_kind=="character"
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.reject()


def test_original_shell_description_return_reuses_source_recheck_without_analysis(tmp_path):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QWidget,QListWidget
    from core.background_jobs import BackgroundJobManager
    from core.settings import AppSettings
    from core.model_resource_registry import ModelResourceRegistry
    from qt_app.generation_dialog import GenerationDialog
    from qt_app.generation_image_setup_dialog import ImageGenerationSetupDialog
    from qt_app.generation_workflow import _connect_setup_return
    app=application();jobs=BackgroundJobManager();settings=AppSettings(ai_models_directory=str(tmp_path/"models"))
    class Panel(QWidget):
        image_setup_return_requested=Signal()
        def __init__(self):
            super().__init__();self.image_setup_return_panel=NS(action_button=QPushButton("Return"));self.available=False
        def show_section(self,*_args):pass
        def set_image_setup_return_available(self,value):self.available=value
        def confirm_leave(self):return True
    window=QWidget();window.navigation=QListWidget();window.navigation.addItems([str(i) for i in range(9)]);window.navigation.setCurrentRow(7)
    window.setup_page=Panel();window._ensure_page=lambda _row:None
    owner=GenerationDialog(jobs,storage_root=tmp_path/"generation",settings=settings,
        registry=ModelResourceRegistry(tmp_path/"models.json"),runtime_allowed=False)
    identity={"generated":"one"}
    review=ImageGenerationSetupDialog((),(),owner,jobs=jobs,settings=settings,source_identity=identity,
        source_current_provider=lambda:identity,registry=owner.registry)
    review.name.setText("Keep original shell setup")
    _connect_setup_return(window,owner,generator_setup_allowed=False)
    owner.image_setup_requested.emit(review)
    assert window.setup_page.available and callable(window._image_setup_return)
    window.setup_page.image_setup_return_requested.emit()
    assert window._image_setup_return is None and not window.setup_page.available
    assert review.name.text()=="Keep original shell setup"
    assert "choices retained" in review.suggestion_status.text()
    review.reject();owner.close();wait_until(app,lambda:not owner._pending)
    jobs.shutdown(wait=True);window.close();window.setup_page.close()


def test_manager_source_choice_cancel_starts_no_image_work(tmp_path):
    app=application();session=NS(state="ready",admitted=NS(layout=NS(data_root=tmp_path/"suite")))
    window=create_window(unconfigured(datetime.now(timezone.utc)),normal_entry_session=session)
    window.show();window.navigation.setCurrentRow(window.page_rows["Generate"])
    deadline=monotonic()+10
    def cancel():
        picker=next((w for w in app.topLevelWidgets() if isinstance(w,GenerationSetupSourceDialog) and w.isVisible()),None)
        if picker is None:
            assert monotonic()<deadline;QTimer.singleShot(20,cancel);return
        QTest.mouseClick(picker.buttons.button(QDialogButtonBox.Cancel),Qt.LeftButton)
    QTimer.singleShot(20,cancel)
    button=next(b for b in window.findChildren(QPushButton) if b.text()=="Create Generation Setup…")
    QTest.mouseClick(button,Qt.LeftButton)
    assert getattr(window,"_generation_source_task",None) is None and getattr(window,"_generation_dialog",None) is None
    assert not window.state.generation_settings_path.exists()
    window.close();wait_until(app,lambda:not window.isVisible())


def test_advanced_description_controls_are_reachable_in_scrolled_dialog(tmp_path):
    app=application();dialog=DescriptionSetupDialog(tmp_path/"Manager/generation-settings.json")
    dialog.show();capture(dialog,"description-default-controls")
    QTest.mouseClick(dialog.expert_button,Qt.LeftButton)
    assert dialog.expert.isVisible()
    assert dialog.controls["joycaption_max_new_tokens"].isVisible()
    assert all(control.height() >= control.fontMetrics().height() for control in dialog.controls.values())
    assert dialog.save_button.isVisible() and dialog.cancel_button.isVisible()
    capture(dialog,"description-advanced-controls")
    scroll=dialog.findChild(QScrollArea)
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    capture(dialog,"description-advanced-bottom")
    assert dialog.prompt.height() >= 80 and dialog.save_button.isVisible()
    dialog.reject();assert not (tmp_path/"Manager/generation-settings.json").exists()
