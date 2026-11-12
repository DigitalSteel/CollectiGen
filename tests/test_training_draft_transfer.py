"""Synthetic Training draft transfer, editable save/reopen and Close coverage."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
import time
import pytest
from PIL import Image
from core import ImageRecord, LibraryImageIdentity, TagDatabase, TrainingCandidateSelectionService, TrainingGoal
from core.training_plan_drafts import TrainingPlanDraftStore
from core.training_draft_transfer import export_training_draft, import_training_draft


def candidate(tmp_path):
    image=tmp_path/"library"/"hero.png"; image.parent.mkdir(parents=True)
    Image.new("RGB",(768,896),(30,60,90)).save(image)
    record=ImageRecord(image); record.annotation.sdxl_tags=["hero","solo"]
    record.modified=True; record.save_sidecar(force=True)
    service=TrainingCandidateSelectionService(TagDatabase(tmp_path/"tags.json"))
    plan=service.plan((image,),TrainingGoal("Teach hero",kind="character"),
        identities={image:LibraryImageIdentity("personal",image.name)})
    return plan,image


def test_transfer_fresh_import_preserves_source_and_edits_own_copy(tmp_path):
    plan,image=candidate(tmp_path)
    original=TrainingPlanDraftStore(tmp_path/"manager.json").save_plan(plan,"Original").draft
    before=image.read_bytes(); source=tmp_path/"draft.json"
    export_training_draft(original,source); source_before=source.read_bytes()
    store=TrainingPlanDraftStore(tmp_path/"dataset.json")
    imported=import_training_draft(source,store)
    assert imported.draft_id!=original.draft_id and imported.plan_manifest==original.plan_manifest
    saved=store.save_plan(plan,"Edited copy",draft_id=imported.draft_id,expected_fingerprint=imported.fingerprint).draft
    assert TrainingPlanDraftStore(store.path).all_drafts()[0].fingerprint==saved.fingerprint
    assert TrainingPlanDraftStore(tmp_path/"manager.json").all_drafts()[0]==original
    assert source.read_bytes()==source_before and image.read_bytes()==before
    with pytest.raises(FileExistsError): export_training_draft(original,source)
    assert source.read_bytes()==source_before


def test_transfer_cancel_before_publication(tmp_path):
    plan,_=candidate(tmp_path)
    draft=TrainingPlanDraftStore(tmp_path/"manager.json").save_plan(plan,"Original").draft
    destination=tmp_path/"cancelled.json"
    with pytest.raises(InterruptedError): export_training_draft(draft,destination,should_cancel=lambda: True)
    assert not destination.exists()
    source=export_training_draft(draft,tmp_path/"draft.json")
    store=TrainingPlanDraftStore(tmp_path/"dataset.json")
    with pytest.raises(InterruptedError): import_training_draft(source,store,should_cancel=lambda: True)
    assert not store.all_drafts()


def app():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont,QFontDatabase
    value=QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    value.setFont(QFont("Segoe UI",10)); value.setStyle("Fusion")
    return value


def wait(predicate):
    from PySide6.QtTest import QTest
    end=time.monotonic()+10
    from PySide6.QtWidgets import QApplication
    while not predicate() and time.monotonic()<end:
        QApplication.processEvents(); time.sleep(.005)
    assert predicate()


def capture(widget,name,tmp_path):
    target=Path(os.environ.get("COLLECTIGEN_DRAFT_CAPTURES",str(tmp_path/"captures")))
    target.mkdir(parents=True,exist_ok=True)
    assert widget.grab().save(str(target/name))


def test_visible_export_import_edit_save_reopen(tmp_path,monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog,QInputDialog,QDialog
    from core.background_jobs import BackgroundJobManager
    from qt_app.training_draft_transfer_dialog import TrainingDraftTransferDialog
    from qt_app.training_candidate_plan_dialog import TrainingCandidatePlanDialog
    application=app(); jobs=BackgroundJobManager()
    plan,image=candidate(tmp_path)
    manager=TrainingPlanDraftStore(tmp_path/"manager.json")
    original=manager.save_plan(plan,"Cloned experiment").draft
    destination=tmp_path/"transfer.json"
    monkeypatch.setattr(QFileDialog,"getSaveFileName",lambda *a,**k:(str(destination),""))
    monkeypatch.setattr(QFileDialog,"getOpenFileName",lambda *a,**k:(str(destination),""))
    exporter=TrainingDraftTransferDialog(jobs,manager.path,selected=original)
    importer=TrainingDraftTransferDialog(jobs,tmp_path/"dataset.json",importing=True)
    editor=None
    try:
        exporter.show();wait(lambda:exporter._task is None and exporter.transfer_button.isEnabled())
        capture(exporter,"manager-draft-export.png",tmp_path)
        QTest.mouseClick(exporter.transfer_button,Qt.LeftButton)
        wait(lambda:exporter._task is None and destination.exists())
        assert "exported" in exporter.status.text()
        exporter.reject();assert not exporter.isVisible()
        importer.show();capture(importer,"organizer-draft-import.png",tmp_path)
        QTest.mouseClick(importer.transfer_button,Qt.LeftButton)
        wait(lambda:importer.imported_draft is not None and importer._task is None)
        assert importer.result()==QDialog.Accepted
        imported=importer.imported_draft
        assert imported.draft_id!=original.draft_id
        store=TrainingPlanDraftStore(tmp_path/"dataset.json")
        editor=TrainingCandidatePlanDialog(plan,draft_store=store,draft=imported)
        editor.show();application.processEvents()
        assert editor.save_draft_button.isEnabled()
        capture(editor,"organizer-editable-draft.png",tmp_path)
        monkeypatch.setattr(QInputDialog,"getText",lambda *a,**k:("Edited experiment",True))
        QTest.mouseClick(editor.save_draft_button,Qt.LeftButton)
        assert TrainingPlanDraftStore(store.path).all_drafts()[0].name=="Edited experiment"
        assert TrainingPlanDraftStore(manager.path).all_drafts()[0]==original
        capture(editor,"organizer-draft-saved.png",tmp_path)
    finally:
        exporter.reject();importer.reject()
        if editor is not None:editor.reject()
        jobs.shutdown();application.processEvents()


def test_transfer_close_drains_owned_task(tmp_path,monkeypatch):
    from PySide6.QtCore import QObject,Signal
    from qt_app import training_draft_transfer_dialog as module
    application=app(); cancellations=[]
    class Signals(QObject):
        complete=Signal(object); failed=Signal(str); cancelled=Signal()
    class Delayed:
        def __init__(self,*a,**k):self.signals=Signals();self.job_id="owned"
        def start(self):pass
    monkeypatch.setattr(module,"CoordinatedCallTask",Delayed)
    dialog=module.TrainingDraftTransferDialog(NS(cancel=cancellations.append),tmp_path/"drafts.json",importing=True)
    dialog.show();dialog._launch("import",lambda job:None);task=dialog._task
    dialog.close();application.processEvents()
    assert dialog.isVisible() and dialog._closing and cancellations==["owned"]
    task.signals.cancelled.emit();application.processEvents()
    assert dialog._task is None and not dialog.isVisible()


def test_dataset_resume_and_reviewed_collection_keep_imported_settings(tmp_path,monkeypatch):
    from qt_app.dataset_page import DatasetPage
    plan,image=candidate(tmp_path)
    draft=TrainingPlanDraftStore(tmp_path/"dataset.json").save_plan(plan,"Imported").draft
    calls=[]
    dataset=NS(path_for_identity=lambda identity:image)
    host=NS(dataset=dataset,_training_candidate_task=None,
        _start_training_candidate_plan=lambda *a,**k:calls.append((a,k)))
    monkeypatch.setattr(DatasetPage,"_storage_scope_ready",lambda *a,**k:True)
    DatasetPage.resume_training_dataset_draft(host,draft=draft)
    assert calls[0][0][0]==(image,) and calls[0][1]["draft"]==draft
    assert calls[0][1]["configuration"]==draft.configuration()
    group={"id":"a"*32,"name":"Current collection","description":"Reviewed", "members":[{"library_id":"personal","relative_path":image.name}],"rules":None}
    def start(*a,**k):
        calls.append((a,k));host._training_candidate_task=object()
    host._start_training_candidate_plan=start
    assert DatasetPage.plan_concept_training(host,group,draft=draft)
    assert calls[-1][1]["draft"]==draft and calls[-1][1]["concept_group"]==group
    assert calls[-1][1]["overrides"]==draft.overrides()
    assert calls[-1][1]["permission_disposition"]==draft.permission_disposition()


def test_import_readiness_does_not_start_early_and_stops_refresh_on_close(tmp_path,monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog
    from core.background_jobs import BackgroundJobManager
    from qt_app.training_draft_transfer_dialog import TrainingDraftTransferDialog
    application=app();jobs=BackgroundJobManager();ready=[False]
    plan,_=candidate(tmp_path)
    original=TrainingPlanDraftStore(tmp_path/"original.json").save_plan(plan,"Saved experiment").draft
    source=export_training_draft(original,tmp_path/"transfer.json")
    monkeypatch.setattr(QFileDialog,"getOpenFileName",lambda *a,**k:(str(source),""))
    dialog=TrainingDraftTransferDialog(jobs,tmp_path/"destination.json",importing=True,can_transfer=lambda:ready[0])
    try:
        dialog.show();application.processEvents()
        assert not dialog.transfer_button.isEnabled() and "current Dataset task" in dialog.status.text()
        capture(dialog,"import-waiting-for-dataset.png",tmp_path)
        QTest.mouseClick(dialog.transfer_button,Qt.LeftButton)
        assert dialog._task is None and not Path(tmp_path/"destination.json").exists()
        ready[0]=True;wait(lambda:dialog.transfer_button.isEnabled())
        capture(dialog,"import-ready.png",tmp_path)
        QTest.mouseClick(dialog.transfer_button,Qt.LeftButton)
        wait(lambda:dialog.imported_draft is not None)
        assert dialog.imported_draft.draft_id!=original.draft_id
        assert not dialog._availability_timer.isActive()
    finally:dialog.reject();jobs.shutdown();application.processEvents()

@pytest.mark.parametrize("empty", [False, True])
def test_saved_dataset_picker_offers_only_selected_export(tmp_path, empty):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from qt_app.training_candidate_plan_dialog import TrainingPlanDraftPickerDialog
    application=app();plan,_=candidate(tmp_path)
    draft=TrainingPlanDraftStore(tmp_path/"dataset.json").save_plan(plan,"Saved Dataset plan").draft
    exported=[]
    picker=TrainingPlanDraftPickerDialog(() if empty else (draft,),export_draft=exported.append)
    plain=TrainingPlanDraftPickerDialog((draft,))
    try:
        picker.show();application.processEvents()
        assert picker.export_button.isVisible() and picker.export_button.isEnabled() is (not empty)
        if not empty:
            QTest.mouseClick(picker.export_button,Qt.LeftButton)
            assert exported==[draft] and picker.isVisible() and picker.result()==0
            capture(picker,"dataset-draft-export-picker.png",tmp_path)
        plain.show();application.processEvents();assert not plain.export_button.isVisible()
    finally:
        picker.reject();plain.reject();application.processEvents()


def test_manager_target_export_cancel_retry_and_readback(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog
    from core.background_jobs import BackgroundJobManager
    from core.training_plan_drafts import TrainingPlanDraft
    from qt_app.training_draft_transfer_dialog import TrainingDraftTransferDialog
    application=app();jobs=BackgroundJobManager();plan,image=candidate(tmp_path)
    store=TrainingPlanDraftStore(tmp_path/"dataset.json");draft=store.save_plan(plan,"Saved Dataset plan").draft
    before=store.path.read_bytes();pixels=image.read_bytes();existing=tmp_path/"existing.json";existing.write_text("retain this file",encoding="utf-8");destination=tmp_path/"manager-review.json"
    choices=iter(["",str(existing),str(destination)])
    monkeypatch.setattr(QFileDialog,"getSaveFileName",lambda *a,**k:(next(choices),""))
    dialog=TrainingDraftTransferDialog(jobs,store.path,selected=draft,destination_host="manager")
    try:
        dialog.show();wait(lambda:dialog._task is None and dialog.transfer_button.isEnabled())
        assert "Suite Manager" in dialog.windowTitle() and "Suite Manager" in dialog.transfer_button.text()
        capture(dialog,"dataset-export-for-manager.png",tmp_path)
        QTest.mouseClick(dialog.transfer_button,Qt.LeftButton)
        assert dialog._task is None and not destination.exists() and dialog.isVisible()
        QTest.mouseClick(dialog.transfer_button,Qt.LeftButton);wait(lambda:dialog._task is None)
        assert existing.read_text(encoding="utf-8")=="retain this file" and not destination.exists()
        assert dialog.isVisible() and dialog.transfer_button.isEnabled()
        QTest.mouseClick(dialog.transfer_button,Qt.LeftButton);wait(lambda:dialog._task is None and destination.exists())
        readback=TrainingPlanDraft.read_transfer(destination)
        assert readback.fingerprint==draft.fingerprint and readback.plan_manifest==draft.plan_manifest
        assert "Review a Dataset Draft" in dialog.status.text()
        capture(dialog,"dataset-exported-for-manager.png",tmp_path)
        assert store.path.read_bytes()==before and image.read_bytes()==pixels
    finally:
        dialog.reject();wait(lambda:dialog._task is None);jobs.shutdown(wait=True);application.processEvents()


def test_dataset_export_reuses_original_transfer_owner_and_shutdown_gate(tmp_path,monkeypatch):
    from qt_app.dataset_page import DatasetPage
    from qt_app import training_draft_transfer_dialog as module
    application=app();seen=[];finished=NS(connect=lambda callback:None)
    class Transfer:
        def __init__(self,*args,**kwargs):seen.append((args,kwargs));self.finished=finished
        def exec(self):return 0
        def deleteLater(self):pass
    monkeypatch.setattr(module,"TrainingDraftTransferDialog",Transfer)
    jobs=NS(shutting_down=False)
    page=NS(state=NS(jobs=jobs),training_plan_drafts=NS(path=tmp_path/"drafts.json"),_notify_background_work_finished=lambda:None)
    selected=object();DatasetPage.export_training_draft_for_manager(page,selected)
    assert page._training_draft_transfer_dialog is None
    assert seen[0][1]["destination_host"]=="manager" and seen[0][1]["selected"] is selected
    assert seen[0][1]["can_transfer"]()
    jobs.shutting_down=True;assert not seen[0][1]["can_transfer"]()
    DatasetPage.export_training_draft_for_manager(page,selected);assert len(seen)==1


@pytest.mark.parametrize("context", ["", "b" * 64])
def test_assessment_context_survives_saved_draft_transfer(tmp_path, context):
    from core.training_plan_drafts import TrainingPlanDraft
    plan,image=candidate(tmp_path)
    plan=replace(plan,assessment_context_fingerprint=context)
    manifest=plan.to_manifest_dict()
    assert manifest.get("assessment_context_fingerprint", "")==context
    if not context:
        assert "assessment_context_fingerprint" not in manifest
    source=TrainingPlanDraftStore(tmp_path/"source.json").save_plan(plan,"Context-bound plan").draft
    assert source.plan_manifest.get("assessment_context_fingerprint", "")==context
    pixels=image.read_bytes();stored=(tmp_path/"source.json").read_bytes()
    path=export_training_draft(source,tmp_path/"transfer.json")
    readback=TrainingPlanDraft.read_transfer(path)
    imported=import_training_draft(path,TrainingPlanDraftStore(tmp_path/"destination.json"))
    assert readback.plan_manifest==manifest and imported.plan_manifest==manifest
    assert readback.fingerprint==source.fingerprint
    assert image.read_bytes()==pixels and (tmp_path/"source.json").read_bytes()==stored


def test_assessment_context_is_part_of_manifest_integrity(tmp_path):
    from copy import deepcopy
    from core.training_plan_drafts import TrainingPlanDraftError
    plan,_=candidate(tmp_path)
    one=replace(plan,assessment_context_fingerprint="a"*64).to_manifest_dict()
    two=replace(plan,assessment_context_fingerprint="b"*64).to_manifest_dict()
    assert one["selection_scope"]==two["selection_scope"]
    assert one["plan_fingerprint"]!=two["plan_fingerprint"]
    changed=deepcopy(one);changed["assessment_context_fingerprint"]="b"*64
    with pytest.raises(TrainingPlanDraftError,match="fingerprint"):
        TrainingPlanDraftStore(tmp_path/"tampered.json").save_manifest(changed,"Tampered")
