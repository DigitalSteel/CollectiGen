"""Connected Manager Models registration/notes/guidance with synthetic files only."""
import hashlib
import json
import os
import time
import pytest
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QLineEdit
from tests.test_manager_models_workspace import manager


@pytest.mark.parametrize("model_role", ["checkpoint", "lora"])
def test_manager_register_notes_reopen_guidance_and_save(tmp_path, monkeypatch, model_role):
    from core.model_resource_registry import ModelResourceRegistry
    from core.generation_planning import load_plan
    from core import model_workflow_guidance as knowledge
    from qt_app.model_workspace_dialog import ModelWorkspaceDialog
    from qt_app.model_workflow_guidance_dialog import ModelWorkflowGuidanceDialog
    from qt_app.generation_dialog import GenerationDialog
    model = tmp_path / "fixtures/synthetic-sdxl.safetensors"
    model.parent.mkdir()
    header = json.dumps({"weight": {"dtype":"F32", "shape":[1], "data_offsets":[0,4]}}).encode()
    original = len(header).to_bytes(8,"little") + header + b"1234"
    model.write_bytes(original)
    identity = "sha256:" + hashlib.sha256(original).hexdigest()
    saved = tmp_path / "Manager/generation/new-image-plan.json"
    saved.parent.mkdir(parents=True)
    machine = knowledge.MachineCapabilityProfile(cpu_threads=8,ram_gib=32,gpu_name="Synthetic GPU",
        gpu_backend="cuda",gpu_vram_gib=16,free_storage_gib=100,evidence="synthetic")
    monkeypatch.setattr(knowledge,"machine_capability_from_setup",lambda *a,**k:machine)
    from core.settings import AppSettings
    profile=tmp_path/"Manager";profile.mkdir(exist_ok=True)
    AppSettings(ai_models_directory=str(model.parent)).save(profile/"generation-settings.json")
    app,window,host = manager(tmp_path)
    native = app.testAttribute(Qt.AA_DontUseNativeDialogs)
    app.setAttribute(Qt.AA_DontUseNativeDialogs,True)
    state = {"step":"initial", "completed":False}
    errors=[]
    captures=os.environ.get("COLLECTIGEN_MODELS_JOURNEY_CAPTURE_DIR")
    output=Path(captures) if captures else None
    if output:output.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+80
    def capture(widget,name):
        if output:
            app.processEvents()
            widget.grab().save(str(output/(name+".png")))
    def click(widget):
        assert widget is not None and widget.isVisible() and widget.isEnabled(),state["step"]
        QTest.mouseClick(widget,Qt.LeftButton)
    def text(widget,value):
        click(widget);QTest.keyClick(widget,Qt.Key_A,Qt.ControlModifier);QTest.keyClicks(widget,value)
    def tab(dialog,label):
        index=next(i for i in range(dialog.tabs.count()) if dialog.tabs.tabText(i)==label)
        bar=dialog.tabs.tabBar();assert bar.isVisible()
        QTest.mouseClick(bar,Qt.LeftButton,pos=bar.tabRect(index).center())
    def group(dialog,label):
        bar=dialog.dialog_commands.bar.tabs
        i=next(i for i in range(bar.count()) if bar.tabData(i)==label)
        QTest.mouseClick(bar,Qt.LeftButton,pos=bar.tabRect(i).center())
    def choose(chooser,filename,button):
        field=chooser.findChild(QLineEdit,"fileNameEdit");assert field is not None
        text(field,str(filename));QTest.keyClick(field,Qt.Key_Tab)
        # Let Qt finish path completion before a later driver tick accepts it.
    def drive():
        try:
            assert time.monotonic()<deadline,state["step"]
            visible=[x for x in QApplication.topLevelWidgets() if x.isVisible()]
            models=next((x for x in visible if isinstance(x,ModelWorkspaceDialog)),None)
            guidance=next((x for x in visible if isinstance(x,ModelWorkflowGuidanceDialog)),None)
            chooser=next((x for x in visible if isinstance(x,QFileDialog)),None)
            step=state["step"]
            if output:
                (output/"STATE.json").write_text(json.dumps({"step":step,"visible":[type(x).__name__ for x in visible]},indent=2))
            if step=="initial" and models is not None and models._task is None and models._registry_loaded:
                models.resize(1500,960);capture(models,"01-models-empty")
                state["step"]="choose-model"
                inspect=next(button for item,button in models.dialog_commands.bar._buttons if item.key=="models.inspect_file")
                QTimer.singleShot(0,lambda:click(inspect))
            elif step=="choose-model" and chooser is not None:
                assert Path(chooser.directory().absolutePath())==model.parent
                state["step"]="open-model";choose(chooser,model.name,QDialogButtonBox.Open)
            elif step=="open-model" and chooser is not None:
                if [Path(x) for x in chooser.selectedFiles()]!=[model]: return
                capture(chooser,"01b-model-chooser")
                state["step"]="declare";click(chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.Open))
            elif step=="declare" and models is not None and models._task is None and models._inspection is not None:
                assert not host.registry_path.exists()
                models.role.setFocus();QTest.keyClick(models.role,Qt.Key_Home)
                for _ in range(models.role.findText(model_role)):QTest.keyClick(models.role,Qt.Key_Down)
                assert models.role.currentText()==model_role
                models.family.setFocus();QTest.keyClick(models.family,Qt.Key_Home)
                for _ in range(models.family.findData("SDXL")):QTest.keyClick(models.family,Qt.Key_Down)
                assert models.family.text()=="SDXL"
                assert models.use_button.text()=="Register Model"
                capture(models,"02-register-exact-file")
                state["step"]="registered";click(models.use_button)
            elif step=="registered" and models is not None and models._task is None and models.models.currentItem() is not None:
                assert models.models.currentItem().data(0,Qt.UserRole)==identity
                resources=ModelResourceRegistry(host.registry_path).snapshot()[0]["resources"]
                assert set(resources)=={identity}
                assert any(d["role"]==model_role and d["family"]=="SDXL" for d in resources[identity]["declarations"])
                tab(models,"Notes")
                text(models.tags,"synthetic, favorite");text(models.notes,"My saved model notes")
                capture(models,"03-model-notes")
                state["step"]="notes-saved";click(models.save_notes_button)
            elif step=="notes-saved" and models is not None and models._task is None:
                record=ModelResourceRegistry(host.registry_path).snapshot()[0]["resources"][identity]
                assert record["user"]["notes"]=="My saved model notes"
                capture(models,"04-notes-saved")
                state["step"]="closed-first";QTest.keyClick(models,Qt.Key_Escape)
            elif step=="reopened" and models is not None and models._task is None and models._registry_loaded:
                if models._active_ribbon_group=="Start":
                    capture(models,"05a-reopened-start")
                    click(models.start_browse_button)
                    return
                capture(models,"05b-reopened-before-selection")
                assert models.browser.cards.isVisible(), {"group":models._active_ribbon_group,"mode":models.browser._mode,"rows":models.models.topLevelItemCount(),"registry":models._registry_loaded,"single":str(models._single_file_review_path),"status":models.status.text()}
                card=models.browser.cards.item(0);assert card is not None
                QTest.mouseClick(models.browser.cards.viewport(),Qt.LeftButton,
                                 pos=models.browser.cards.visualItemRect(card).center())
                tab(models,"Notes")
                assert models.notes.toPlainText()=="My saved model notes"
                capture(models,"05-notes-reopened")
                group(models,"Planning")
                capture(models,"06-planning")
                state["step"]="guidance";state["guidance_started"]=time.monotonic()
                command=next(button for item,button in models.dialog_commands.bar._buttons
                             if item.key=="models.plan_generation")
                if command.isVisible():click(command)
                else:
                    bar=models.dialog_commands.bar;click(bar.overflow)
                    action=next(a for a in bar.overflow_menu.actions() if a.text()=="Recommend for Image Generation…")
                    QTest.mouseClick(bar.overflow_menu,Qt.LeftButton,pos=bar.overflow_menu.actionGeometry(action).center())
            elif step=="guidance" and guidance is None and models is not None and models._task is None:
                assert time.monotonic()-state["guidance_started"]<3, "Guidance did not open: "+models.status.text()
            elif step=="guidance" and guidance is not None:
                assert guidance.workflow.currentData()=="generation"
                guidance.resize(1300,900)
                if model_role == "lora":
                    assert guidance.base_model_id.text() == ""
                    assert guidance.selected_generation_resources_label.isVisible()
                    assert identity in guidance.selected_generation_resources_label.text()
                click(guidance.customize_model_set);assert guidance.base_model_id.isVisible()
                click(guidance.customize_model_set);assert not guidance.base_model_id.isVisible()
                text(guidance.goal,"Create a synthetic portrait")
                guidance.content_scroll.ensureWidgetVisible(guidance.preview_button)
                capture(guidance,"07-guidance-goal")
                state["step"]="reviewed";click(guidance.preview_button)
            elif step=="reviewed" and guidance is not None and guidance.use_button.isEnabled():
                capture(guidance,"08-guidance-reviewed")
                state["step"]="continue";click(guidance.use_button)
            elif step=="continue" and guidance is None and models is not None:
                assert models.generation_handoff is not None
                capture(models,"09-continue-generate")
                state["step"]="new-draft";click(models.continue_generation_button)
            elif step=="new-draft" and getattr(window,"_generation_dialog",None) is not None:
                draft=window._generation_dialog
                assert draft.isVisible() and isinstance(draft,GenerationDialog)
                assert draft.plan.goal=="Create a synthetic portrait"
                assert draft.plan.request.model_hash==(identity.removeprefix("sha256:") if model_role == "checkpoint" else "")
                assert any(item.id == identity and item.role == model_role for item in draft.plan.request.resources)
                assert not window.state.generation_runtime_allowed
                assert not draft._pending
                capture(draft,"10-new-generate-draft")
                state["step"]="save-chooser";QTimer.singleShot(0,lambda:click(draft.save_button))
            elif step=="save-chooser" and chooser is not None:
                assert Path(chooser.directory().absolutePath())==saved.parent
                state["step"]="confirm-save";state["save_started"]=time.monotonic()
                choose(chooser,saved.name,QDialogButtonBox.Save)
            elif step=="confirm-save" and chooser is not None:
                if [Path(x) for x in chooser.selectedFiles()]!=[saved]:
                    assert time.monotonic()-state["save_started"]<3, (chooser.selectedFiles(),chooser.findChild(QLineEdit,"fileNameEdit").text())
                    return
                state["step"]="saved";click(chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.Save))
            elif step=="saved" and saved.is_file() and not window._generation_dialog._pending:
                plan=load_plan(saved)
                assert plan.goal=="Create a synthetic portrait"
                assert plan.request.model_hash==(identity.removeprefix("sha256:") if model_role == "checkpoint" else "")
                assert any(item.id == identity and item.role == model_role for item in plan.request.resources)
                assert plan.target_context["model_guidance"]["family"]=="SDXL"
                capture(window._generation_dialog,"11-generate-saved")
                state["completed"]=True;state["step"]="complete";driver.stop()
                QTest.keyClick(window._generation_dialog,Qt.Key_Escape)
        except BaseException as exc:
            errors.append(f"{state['step']}: {type(exc).__name__}: {exc}")
            driver.stop()
            if host.dialog is not None:
                host.dialog._note_drafts.clear()
                host.dialog._note_identity=""
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget,QDialog) and widget.isVisible():widget.reject()
    driver=QTimer();driver.setInterval(25);driver.timeout.connect(drive)
    watchdog=QTimer();watchdog.setSingleShot(True)
    def timed_out():
        errors.append("watchdog: "+state["step"]);driver.stop()
        from shiboken6 import isValid
        for widget in QApplication.topLevelWidgets():
            if isValid(widget) and isinstance(widget,QDialog) and widget.isVisible():QDialog.reject(widget)
    watchdog.timeout.connect(timed_out)
    item=window.navigation.item(window.page_rows["Applications"])
    QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
    try:
        watchdog.start(85000);driver.start();click(window.local_models_button)
        assert not errors,errors
        assert state["step"]=="closed-first"
        state["step"]="reopened";click(window.local_models_button)
        while not state["completed"] and not errors and time.monotonic()<deadline:
            app.processEvents();time.sleep(.005)
        assert not errors,errors
        assert state["completed"],state
        assert model.read_bytes()==original
        assert host.dialog is None
        host.begin_close()
        end=time.monotonic()+10
        while not host.poll_close() and time.monotonic()<end:app.processEvents();time.sleep(.005)
        assert host.drained
        if output:
            (output/"JOURNEY_RESULT.json").write_text(json.dumps({"completed":True,"model_id":identity,
                "registry":str(host.registry_path),"saved_plan":str(saved),"model_bytes_unchanged":True,
                "notes_reopened":True,"runtime_allowed":False,"native_qualified":False},indent=2))
    finally:
        driver.stop();watchdog.stop();app.setAttribute(Qt.AA_DontUseNativeDialogs,native)
        window.close();app.processEvents()
