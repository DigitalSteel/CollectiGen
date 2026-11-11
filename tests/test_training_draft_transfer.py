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
