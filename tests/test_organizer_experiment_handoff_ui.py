"""Real Organizer entry -> import -> Dataset edit/save/reopen with generated images."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize("mode",["ordinary","collection"])
def test_organizer_visible_import_edit_reopen(tmp_path,mode):
    env=dict(os.environ,PYTHONIOENCODING="utf-8",PYTHONDONTWRITEBYTECODE="1",QT_QPA_PLATFORM="offscreen",TAG_MANAGER_UI_TEST="1",
        TAG_MANAGER_UI_TEST_USER_DATA_DIR=str(tmp_path/"user"),TAG_MANAGER_UI_TEST_CACHE_DIR=str(tmp_path/"cache"))
    with (tmp_path/"child.log").open("w",encoding="utf-8") as log:
        result=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--child",str(tmp_path),mode],
            cwd=Path(__file__).resolve().parents[1],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=90)
    assert result.returncode==0,(tmp_path/"child.log").read_text(encoding="utf-8")[-10000:]


def run(root,mode):
    import hashlib,json,time
    from PIL import Image
    from PySide6.QtCore import Qt,QTimer
    from PySide6.QtGui import QFont,QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication,QAbstractButton,QFileDialog,QInputDialog,QMessageBox,QDialog,QLabel
    from core import AppSettings,ImageRecord,TagDatabase,LibraryImageIdentity,TrainingCandidateSelectionService,TrainingGoal
    from core.library_profiles import LibraryProfile,LibraryProfileRegistry,LibraryRole
    from core.training_plan_drafts import TrainingPlanDraftStore
    from core.training_draft_transfer import export_training_draft
    from core.concept_groups import ConceptGroupStore,group_fingerprint
    from core.paths import SETTINGS_PATH,TRAINING_PLAN_DRAFTS_PATH
    from qt_app.main_window import TagManagerWindow
    from suite_core.media_organizer.entry import make_window_type
    from suite_core.media_organizer.organizer_workflow_corrections import with_workflow_corrections
    from qt_app.training_draft_transfer_dialog import TrainingDraftTransferDialog
    from qt_app.training_candidate_plan_dialog import TrainingCandidatePlanDialog,TrainingCandidateOverrideDialog,TrainingPlanDraftPickerDialog
    from qt_app.concept_group_dialog import ConceptGroupDialog
    from tools.qt_test_support import QtTestScope
    root=Path(root);(root/"user").mkdir(parents=True,exist_ok=True)
    image=root/"generated-library"/"hero.png";image.parent.mkdir()
    Image.new("RGB",(768,896),(30,60,90)).save(image)
    record=ImageRecord(image);record.annotation.sdxl_tags=["hero","solo"]
    record.modified=True;record.save_sidecar(force=True)
    image_before=image.read_bytes()
    profile=LibraryProfile.create("Generated review images",image.parent,LibraryRole.NETWORK_SHARED,stable_id="personal")
    settings=AppSettings();settings.set_library_registry(LibraryProfileRegistry([profile]),active_library_id=profile.stable_id)
    settings.library_scope_ids=[profile.stable_id];settings.restore_last_dataset=False
    settings.check_dependencies_on_startup=False;settings.check_updates_on_startup=False
    settings.suggest_wd_for_missing_tags=False;settings.suggest_joycaption_for_missing_descriptions=False
    settings.save(SETTINGS_PATH)
    source_label="Explicit Dataset selection"
    if mode=="collection":
        groups=ConceptGroupStore(root/"user/concept_groups.json")
        group=groups.create("Hero study",expected_revision=groups.snapshot()[1])
        group=groups.add_members(group["id"],[{"library_id":"personal","relative_path":"hero.png",
            "image_sha256":hashlib.sha256(image_before).hexdigest()}],expected_revision=groups.snapshot()[1])
        source_label=f"Collection {group['name']} [{group['id']}] snapshot {group_fingerprint(group)}"
    plan=TrainingCandidateSelectionService(TagDatabase(root/"fixture-tags.json")).plan((image,),
        TrainingGoal("Teach hero",kind="character"),identities={image:LibraryImageIdentity("personal",image.name)},source_label=source_label)
    original_store=TrainingPlanDraftStore(root/"original-manager-drafts.json")
    original=original_store.save_plan(plan,"Editable experiment").draft
    source=export_training_draft(original,root/"transfer.json");source_before=source.read_bytes()
    QFileDialog.getOpenFileName=lambda *a,**k:(str(source),"")
    QInputDialog.getText=lambda *a,**k:("Edited experiment",True)
    errors=[];seen=set();editor_ids=[];saved_ids=[];stages={"editor":0};page=None
    app=QApplication.instance() or QApplication([])
    for f in ("segoeui.ttf","segoeuib.ttf"):QFontDatabase.addApplicationFont("C:/Windows/Fonts/"+f)
    app.setFont(QFont("Segoe UI",10));app.setStyle("Fusion")
    capture_root=Path(os.environ.get("COLLECTIGEN_EXPERIMENT_JOURNEY_CAPTURES",str(root/"captures")))/mode
    capture_root.mkdir(parents=True,exist_ok=True)
    def capture(widget,name):
        assert widget.grab().save(str(capture_root/(name+".png")))
    def wait(predicate,seconds=25):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:
            app.processEvents();time.sleep(.005)
        if not predicate():
            import faulthandler
            faulthandler.dump_traceback()
            print("DIAGNOSTIC",[(type(w).__name__,w.windowTitle(),getattr(getattr(w,"status",None),"text",lambda:"")()) for w in app.topLevelWidgets() if w.isVisible()],flush=True)
            try:
                print("JOBS",window.state.jobs.diagnostic_snapshot(),flush=True)
                print("PAGE",page._shutdown_requested,page.header.status.text(),bool(page._training_candidate_task),bool(page._training_candidate_plan),getattr(page,"_training_candidate_draft",None) is not None,flush=True)
            except NameError:pass
        assert predicate(),"Timed out: "+str(sorted(seen))+" "+str(errors)
    deadline=time.monotonic()+65
    def activate(button):
        QTimer.singleShot(0,lambda b=button:QTest.mouseClick(b,Qt.LeftButton))
    def drive():
        if time.monotonic()>deadline:
            errors.append("UI journey deadline exceeded")
            for w in app.topLevelWidgets():
                if w.isVisible() and hasattr(w,"reject"):w.reject()
            return
        try:
            modal=app.activeModalWidget()
            if modal is not None and isinstance(modal,QDialog) and not isinstance(modal,(TrainingDraftTransferDialog,TrainingCandidatePlanDialog,TrainingCandidateOverrideDialog,TrainingPlanDraftPickerDialog,ConceptGroupDialog)):
                capture(modal,"unexpected-modal")
                errors.append("Unhandled modal: "+type(modal).__name__+": "+" | ".join(x.text() for x in modal.findChildren(QLabel))[:2000])
                print(errors[-1],flush=True);modal.reject();return
            for w in app.topLevelWidgets():
                if not w.isVisible():continue
                if isinstance(w,QMessageBox):
                    raise AssertionError(w.windowTitle()+": "+w.text())
                if (isinstance(w,TrainingDraftTransferDialog) and "import" not in seen
                        and w.transfer_button.isEnabled()):
                    seen.add("import");capture(w,"02-import")
                    activate(w.transfer_button)
                elif isinstance(w,TrainingPlanDraftPickerDialog) and "picker" not in seen:
                    seen.add("picker");capture(w,"07-saved-draft-picker")
                    activate(w.open_button)
                elif isinstance(w,ConceptGroupDialog) and w._pending is None:
                    key="collection-reopen" if stages["editor"]==4 else "collection"
                    if key not in seen:
                        if w.groups.currentItem() is None and w.groups.count():
                            item=w.groups.item(0)
                            QTest.mouseClick(w.groups.viewport(),Qt.LeftButton,pos=w.groups.visualItemRect(item).center())
                        if w.prepare_button.isEnabled():
                            assert any("saved goal and training settings" in label.text() for label in w.findChildren(QLabel))
                            seen.add(key);capture(w,"03-reviewed-"+key)
                            activate(w.prepare_button)
                elif isinstance(w,TrainingCandidateOverrideDialog) and "override" not in seen:
                    seen.add("override")
                    w.role.setFocus();QTest.keyClick(w.role,Qt.Key_Down)
                    w.reason.setFocus();QTest.keyClicks(w.reason,"Reserve for validation")
                    capture(w,"05-advanced-override")
                    activate(w.save_button)
                elif isinstance(w,TrainingCandidatePlanDialog):
                    if stages["editor"]==0:
                        stages["editor"]=1;seen.add("editor");editor_ids.append(w.draft.draft_id)
                        capture(w,"04-editable-draft")
                        activate(w.edit_override_button)
                    elif stages["editor"]==1 and "override" in seen and not any(isinstance(x,TrainingCandidateOverrideDialog) and x.isVisible() for x in app.topLevelWidgets()):
                        stages["editor"]=2
                        assert w.plan.candidates[0].role=="validation"
                        activate(w.save_draft_button)
                    elif stages["editor"]==2 and w._draft_save_task is None and w.draft.name=="Edited experiment":
                        stages["editor"]=3;seen.add("saved");saved_ids.append(w.draft.draft_id)
                        capture(w,"06-saved-edit");w.reject()
                    elif stages["editor"]==4:
                        stages["editor"]=5
                        assert w.draft.draft_id==saved_ids[0]
                        assert w.plan.candidates[0].role=="validation"
                        seen.add("reopened");capture(w,"07-reopened-edit");w.reject()
        except BaseException as exc:
            errors.append(repr(exc))
            for w in app.topLevelWidgets():
                if w.isVisible() and hasattr(w,"reject"):w.reject()
    with QtTestScope() as scope:
        window_type=with_workflow_corrections(make_window_type(TagManagerWindow,current_application=QApplication.instance))
        window=scope.show(window_type(settings=settings,genuinely_new_profile=False),size=(1600,960))
        timer=scope.own_timer(QTimer());timer.setInterval(10);timer.timeout.connect(drive);timer.start()
        refresh=next(b for b in window.findChildren(QAbstractButton) if b.isVisible() and b.text().replace("&","")=="Refresh Libraries")
        QTest.mouseClick(refresh,Qt.LeftButton)
        wait(lambda:window._scan_thread is None and getattr(window.state,"dataset",None) is not None)
        item=next(window.navigation.item(i) for i in range(window.navigation.count()) if window.navigation.item(i).text()=="Dataset")
        QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
        wait(lambda:hasattr(window,"dataset_page") and window.dataset_page.dataset is not None)
        page=window.dataset_page;capture(window,"01-dataset-entry")
        wait(lambda:(page.import_training_draft_action.isEnabled() and page._projection_task is None) or bool(errors));assert not errors,errors
        tabs=window.command_bar.tabs
        training_tab=next(i for i in range(tabs.count()) if tabs.tabText(i)=="Dataset Plans")
        QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(training_tab).center())
        def command(label):
            return next((b for b in window.command_bar.row_widget.findChildren(QAbstractButton)
                if b.isVisible() and b.text().startswith(label)),None)
        wait(lambda:command("Import Training Draft") is not None and command("Import Training Draft").isEnabled())
        capture(window,"01-dataset-plans-entry")
        QTest.mouseClick(command("Import Training Draft"),Qt.LeftButton)
        wait(lambda:"saved" in seen or bool(errors));assert not errors,errors
        wait(lambda:not page.has_active_background_work())
        assert not hasattr(window,"training_page")
        assert editor_ids==saved_ids and editor_ids[0]!=original.draft_id
        stages["editor"]=4
        # Reopen through the existing Saved Dataset Drafts command.
        QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(training_tab).center())
        wait(lambda:command("Saved Dataset Drafts") is not None and command("Saved Dataset Drafts").isEnabled())
        QTest.mouseClick(command("Saved Dataset Drafts"),Qt.LeftButton)
        wait(lambda:"reopened" in seen or bool(errors));assert not errors,errors
        timer.stop()
        assert image.read_bytes()==image_before and source.read_bytes()==source_before
        assert TrainingPlanDraftStore(original_store.path).all_drafts()[0]==original
        saved=TrainingPlanDraftStore(TRAINING_PLAN_DRAFTS_PATH).all_drafts()[0]
        assert saved.name=="Edited experiment" and next(iter(saved.overrides().values())).role=="validation"
        (root/"RESULT.json").write_text(json.dumps({"mode":mode,"states":sorted(seen),"source_preserved":True,
            "fresh_destination_id":saved.draft_id,"native_training_constructed":False,"errors":errors},indent=2)+"\n")
        print("PASS",mode,sorted(seen),flush=True)


if __name__=="__main__" and len(sys.argv)==4 and sys.argv[1]=="--child":
    run(sys.argv[2],sys.argv[3])
