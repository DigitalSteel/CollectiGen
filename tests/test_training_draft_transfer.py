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


@pytest.mark.parametrize("name", ["automation_rules.json", "conflicts.json"])
def test_caption_authority_tracks_absent_optional_default_rules(tmp_path, name):
    from core.training_candidate_selection import capture_dataset_caption_authority
    defaults=tmp_path/"defaults";user=tmp_path/"user"
    defaults.mkdir();user.mkdir()
    for filename in ("categories.json","community_tags.json","concepts.json","foundation_tags.json",
                     "modifiers.json","promoted_tags.json","relationships.json","tag_aliases.json"):
        (defaults/filename).write_text("{}",encoding="utf-8")
    for filename in ("default_knowledge_state.json","tags.json"):
        (user/filename).write_text("{}",encoding="utf-8")
    before={str(p):p.read_bytes() for p in tmp_path.rglob("*.json")}
    current=capture_dataset_caption_authority(tmp_path)
    assert current() and before=={str(p):p.read_bytes() for p in tmp_path.rglob("*.json")}
    rule=defaults/name;rule.write_text("{}",encoding="utf-8")
    assert not current()
    present=capture_dataset_caption_authority(tmp_path)
    assert present()
    rule.unlink()
    assert not present()
    (defaults/"categories.json").unlink()
    with pytest.raises(FileNotFoundError):capture_dataset_caption_authority(tmp_path)


def test_missing_flat_conflict_store_retains_reviewed_typed_constraints(tmp_path):
    from core.conflict_rules import ConflictRuleEngine
    from core.knowledge_relationships import KnowledgeRelationship,SCOPED_CONSTRAINT
    relation=KnowledgeRelationship("typed-conflict",SCOPED_CONSTRAINT,(),label="Typed constraint")
    evaluator=NS(registry=NS(relationships=[relation]))
    path=tmp_path/"conflicts.json"
    engine=ConflictRuleEngine.load(path,relationship_evaluator=evaluator)
    assert engine.relationship_evaluator is evaluator
    assert [rule.relationship_id for rule in engine.rules]==[relation.id]
    assert not path.exists()
    path.write_text("{broken",encoding="utf-8")
    with pytest.raises(ValueError):ConflictRuleEngine.load(path,relationship_evaluator=evaluator)


def test_tag_bound_conflicts_load_typed_constraints_without_flat_file(tmp_path,monkeypatch):
    from core.conflict_rules import ConflictRuleEngine
    from core.knowledge_relationships import KnowledgeRelationship,SCOPED_CONSTRAINT
    from core import relationship_evaluator
    tags=NS();relation=KnowledgeRelationship("bound-constraint",SCOPED_CONSTRAINT,(),label="Existing constraint")
    evaluator=NS(registry=NS(relationships=[relation]));seen=[]
    def active(knowledge):
        seen.append(knowledge)
        return evaluator
    monkeypatch.setattr(relationship_evaluator,"active_relationship_evaluator",active)
    path=tmp_path/"missing-conflicts.json"
    engine=ConflictRuleEngine.load(path,tags=tags)
    assert seen==[tags] and engine.relationship_evaluator is evaluator
    assert [r.relationship_id for r in engine.rules]==[relation.id]
    assert not path.exists()


def test_unbound_missing_conflicts_keeps_legacy_empty_engine(tmp_path,monkeypatch):
    from core.conflict_rules import ConflictRuleEngine
    from core import relationship_evaluator
    def unexpected(*args):raise AssertionError("Unbound missing file must not discover process authority")
    monkeypatch.setattr(relationship_evaluator,"active_relationship_evaluator",unexpected)
    engine=ConflictRuleEngine.load(tmp_path/"missing.json")
    assert engine.rules==[] and engine.relationship_evaluator is None
