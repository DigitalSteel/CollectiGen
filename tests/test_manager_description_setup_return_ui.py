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
from suite_core.generate.window import create_window as create_generate_window
from suite_core.tests.workflow_ui_fixtures import synthetic_module_session, trigger_more
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
    trigger_more(window, 'Create Generation Setup…')
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
    from test_workflow_settings_return_ui import (request_saved_return, manager_for_return,
        description_settings, resumed_generate, wait_restored_review, close_component_window)
    app=application();monkeypatch.setattr("core.swarm_runtime.SwarmRuntimeManager",FixtureRuntime)
    calls=[]
    def missing(*_a,**_k):
        calls.append("explicit suggestion")
        raise ValueError("Generated missing-caption-model fixture")
    monkeypatch.setattr("core.generation_image_suggestions.suggest_image_setup_parts",missing)
    path=tmp_path/"generated-source.png";Image.new("RGB",(20,20),"blue").save(path)
    original_image=path.read_bytes()
    root=tmp_path/'suite'
    session=synthetic_module_session(root, 'generate')
    window=create_generate_window(session);window.resize(1800,980);window.show()
    owner,review=open_image_setup(window,app,path,monkeypatch)
    review.name.setText("Keep this unfinished setup")
    review.part_fields["scene"].setText("Retain this manually reviewed scene")
    goal=review.suggestion_goal.currentData()
    original_request=owner.plan.request.to_dict()
    QTest.mouseClick(review.suggest_button,Qt.LeftButton)
    wait_until(app,lambda:review._suggestion_task is None and review.review_description_setup_button.isVisible())
    assert calls==["explicit suggestion"]
    window._description_test_watchdog.stop()
    config_path=window.state.generation_settings_path
    before=config_path.read_bytes() if config_path.exists() else None
    descriptor,saved,draft_path=request_saved_return(app,session,window,
        review.review_description_setup_button,'description-settings')
    assert descriptor['resume_image_review'] is True
    snapshot=saved.target_context['unfinished_image_setup_review']
    assert snapshot['state']['name']=="Keep this unfinished setup"
    assert snapshot['state']['parts']['scene']['text']=="Retain this manually reviewed scene"
    assert saved.request.to_dict()==original_request
    draft_bytes=draft_path.read_bytes()
    app,manager_session,manager=manager_for_return(root)
    def settings_opened(child):
        assert calls==["explicit suggestion"]
        capture(child,"description-settings-"+outcome)
    result=description_settings(manager,manager_session,outcome=outcome,
        directory=tmp_path/"existing-model-parent",on_open=settings_opened)
    if outcome in {"save","stale_source"}:
        assert result is not None and config_path.exists()
    else:
        assert result is None
        assert (config_path.read_bytes() if config_path.exists() else None)==before
    assert draft_path.read_bytes()==draft_bytes
    manager.close();app.processEvents();assert not manager.isVisible()
    if outcome=="stale_source":Image.new("RGB",(20,20),"red").save(path)
    app,resumed_session,resumed,resumed_owner=resumed_generate(root,descriptor)
    restored=wait_restored_review(app,resumed_owner,"Keep this unfinished setup")
    assert resumed_owner is not owner and restored is not review
    assert resumed_owner._draft_path==draft_path
    assert restored.part_fields["scene"].text()=="Retain this manually reviewed scene"
    assert restored.suggestion_goal.currentData()==goal
    assert resumed_owner.plan.request.to_dict()==original_request
    assert draft_path.read_bytes()==draft_bytes and calls==["explicit suggestion"]
    assert not restored.save_button.isEnabled() and restored.recipe is None
    if outcome in {"save","stale_source"}:
        assert restored.settings.ai_models_directory==str(tmp_path/"existing-model-parent")
        assert restored.settings.joycaption_max_new_tokens==512
    if outcome=="stale_source":
        assert not restored.suggest_button.isEnabled()
        assert "selected image changed" in restored.suggestion_status.text()
    else:
        assert restored.suggest_button.isEnabled()
        assert path.read_bytes()==original_image
    capture(restored,"description-return-"+outcome)
    close_component_window(app,resumed)
    # Restore only the injected synthetic stale source after its recovery assertions.
    if outcome=="stale_source":path.write_bytes(original_image)


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
    app=application();session=synthetic_module_session(tmp_path/'suite', 'generate')
    window=create_generate_window(session)
    window.show()
    deadline=monotonic()+10
    def cancel():
        picker=next((w for w in app.topLevelWidgets() if isinstance(w,GenerationSetupSourceDialog) and w.isVisible()),None)
        if picker is None:
            assert monotonic()<deadline;QTimer.singleShot(20,cancel);return
        QTest.mouseClick(picker.buttons.button(QDialogButtonBox.Cancel),Qt.LeftButton)
    QTimer.singleShot(20,cancel)
    trigger_more(window, 'Create Generation Setup…')
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
