"""Synthetic offline Video authoring and existing typed-draft handoff."""
import os,time
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication,QWidget,QFileDialog
from core.background_jobs import BackgroundJobManager
from core.model_resource_registry import ModelResourceRegistry
from core.settings import AppSettings
from core.library_profiles import LibraryProfile,LibraryProfileRegistry,LibraryRole
from core.video_generation_draft import load_video_draft
from qt_app import video_generation_dialog as module
from qt_app import video_generation_workflow as workflow


def application():return QApplication.instance() or QApplication([])

def wait(predicate):
    deadline=time.monotonic()+20
    while not predicate() and time.monotonic()<deadline:
        application().processEvents();time.sleep(.005)
    assert predicate()

def settings(root):
    value=AppSettings();profile=LibraryProfile.create("Synthetic Video",root,LibraryRole.NETWORK_SHARED)
    value.set_library_registry(LibraryProfileRegistry([profile]),active_library_id=profile.stable_id)
    return value


def test_offline_constructor_and_online_default_have_separate_capabilities(tmp_path,monkeypatch):
    app=application();calls=[];jobs=BackgroundJobManager()
    monkeypatch.setattr(module,"VideoExecutionService",lambda *a,**k:calls.append("execution") or NS())
    monkeypatch.setattr(module.VideoGenerationDialog,"_apply_local_setup",lambda self:calls.append("setup"))
    offline=module.VideoGenerationDialog(jobs,ModelResourceRegistry(tmp_path/"models.json"),settings(tmp_path),tmp_path/"drafts",runtime_allowed=False)
    try:
        assert calls==[] and offline.service is None
        assert not offline.section_bar.isTabVisible(2)
        assert not offline.start_button.isEnabled() and not offline.submit_button.isEnabled()
        assert offline.save_button.isEnabled() and not offline._setup_dirty()
        assert offline.windowTitle()=="Plan Video" and offline.review.isHidden() and offline.review_loss.isHidden()
        online=module.VideoGenerationDialog(jobs,ModelResourceRegistry(tmp_path/"other-models.json"),settings(tmp_path),tmp_path/"other-drafts")
        assert online.runtime_allowed and calls==["execution","setup"]
        online.close_workspace()
    finally:offline.close_workspace();jobs.shutdown();app.processEvents()


@pytest.mark.parametrize("action",["preview_setup","prepare_model_storage","recover_model_storage","start_runtime","prepare_video","submit_video","recover_job","preview_video","finish_video","send_to_ingest","stop_runtime","open_dependency_pack","save_local_setup","reload_local_setup","show_local_setup"])
def test_offline_public_runtime_actions_do_not_start_work(tmp_path,action):
    app=application();jobs=BackgroundJobManager();dialog=module.VideoGenerationDialog(jobs,ModelResourceRegistry(tmp_path/"models.json"),settings(tmp_path),tmp_path/"drafts",runtime_allowed=False)
    try:
        getattr(dialog,action)()
        assert dialog._task is None and dialog.service is None
        assert "Suite Manager" in dialog.status.text()
        assert not (tmp_path/"drafts/jobs").exists()
    finally:dialog.close_workspace();jobs.shutdown();app.processEvents()


def test_selected_still_offline_draft_roundtrip_uses_original_workflow(tmp_path,monkeypatch):
    app=application();jobs=BackgroundJobManager();config=settings(tmp_path);image=tmp_path/"synthetic.png";Image.new("RGB",(832,480),(30,70,100)).save(image);before=image.read_bytes()
    host=QWidget();host.state=NS(jobs=jobs,settings=config,video_generation_runtime_allowed=False,dependency_service_factory=object(),admit_video_runtime=lambda **k:pytest.fail("Offline entry must not admit runtime"))
    monkeypatch.setattr(workflow,"MODEL_RESOURCES_PATH",tmp_path/"models.json");monkeypatch.setattr(workflow,"USER_DATA_DIR",tmp_path/"profile")
    target=tmp_path/"video.json";monkeypatch.setattr(QFileDialog,"getSaveFileName",lambda *a,**k:(str(target),""))
    dialog=workflow.open_video_generation_workspace(host,source_path=image)
    try:
        assert dialog is not None and not dialog.runtime_allowed
        wait(lambda:dialog._task is None)
        assert dialog._source_ref is not None and dialog.task_choice.currentData()=="i2v"
        dialog.positive.setPlainText("A slow pan across the synthetic rectangle")
        source_ref=dialog._source_ref
        assert dialog.save_draft()
        wait(lambda:dialog._task is None)
        saved=load_video_draft(target)
        other=module.VideoGenerationDialog(jobs,ModelResourceRegistry(tmp_path/"other-models.json"),config,tmp_path/"other",runtime_allowed=False)
        try:
            other._restore_draft(saved)
            assert other.positive.toPlainText()==dialog.positive.toPlainText()
            assert other._draft_request().to_dict()==saved.to_dict()
            assert source_ref.sha256==dialog._source_ref.sha256 and image.read_bytes()==before
            other._confirm_draft_loss_choice=lambda:"discard";other.close_workspace()
        finally:other.hide()
        assert not (tmp_path/"profile/video_generation/jobs").exists()
    finally:
        dialog._confirm_draft_loss_choice=lambda:"discard";dialog.close_workspace();host.close();jobs.shutdown();app.processEvents()


def test_offline_cancel_and_failed_source_keep_existing_draft(tmp_path,monkeypatch):
    app=application();jobs=BackgroundJobManager();config=settings(tmp_path)
    image=tmp_path/"source.png";Image.new("RGB",(832,480),(60,100,30)).save(image);pixels=image.read_bytes()
    dialog=module.VideoGenerationDialog(jobs,ModelResourceRegistry(tmp_path/"models.json"),config,tmp_path/"drafts",runtime_allowed=False)
    try:
        dialog.attach_source(image);wait(lambda:dialog._task is None)
        assert dialog._source_ref is not None
        reference=dialog._source_ref
        monkeypatch.setattr(QFileDialog,"getOpenFileName",lambda *a,**k:("",""))
        dialog.choose_source()
        assert dialog._source_ref==reference and dialog._task is None
        dialog.attach_source(tmp_path/"missing.png");wait(lambda:dialog._task is None)
        assert dialog._source_ref==reference and image.read_bytes()==pixels
        monkeypatch.setattr(QFileDialog,"getSaveFileName",lambda *a,**k:("",""))
        assert not dialog.save_draft()
        assert dialog._source_ref==reference and dialog._task is None
    finally:
        dialog._confirm_draft_loss_choice=lambda:"discard";dialog.close_workspace();jobs.shutdown();app.processEvents()
