"""Manager host routing and durable Training Experiment annotations."""
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from core.background_jobs import BackgroundJobManager
from core.training_run_index import TrainingRunIndex,TrainingRunRecord,ScopedTrainingHistory
from suite_core.manager.window import create_window,unconfigured
from test_training_draft_transfer import app,wait,capture


def test_manager_visible_experiments_save_reopen_and_saved_draft_route(tmp_path,monkeypatch):
    application=app()
    session=NS(state="ready",admitted=NS(layout=NS(data_root=tmp_path)))
    window=create_window(unconfigured(datetime.now(timezone.utc)),normal_entry_session=session)
    window.resize(1500,960);window.show()
    state=window.generation_state();root=state.generation_data_root/"training";root.mkdir(parents=True,exist_ok=True)
    index=TrainingRunIndex(root/"training-runs.json")
    record=TrainingRunRecord("a"*32,"execution",str(root/"runs/review.json"),"b"*64,"c"*64,str(root/"runs/run"),
        str(state.generation_data_root/"model_resources.json"),"d"*64,phase="completed")
    index.register(record,index.snapshot()[1])
    try:
        item=window.navigation.item(window.page_rows["Training"])
        QTest.mouseClick(window.navigation.viewport(),Qt.LeftButton,pos=window.navigation.visualItemRect(item).center())
        capture(window,"manager-experiments-entry.png",tmp_path)
        QTest.mouseClick(window.training_experiments_button,Qt.LeftButton)
        controller=window._sdxl_preparation_controller
        dialog=next(iter(controller.open_reviews()))
        wait(lambda:dialog._task is None and dialog.runs.topLevelItemCount()==1)
        row=dialog.runs.topLevelItem(0)
        QTest.mouseClick(dialog.runs.viewport(),Qt.LeftButton,pos=dialog.runs.visualItemRect(row).center())
        dialog.name_edit.setText("Hero experiment");dialog.notes_edit.setPlainText("Prefer the second caption draft.")
        dialog.favorite.setFocus();QTest.keyClick(dialog.favorite,Qt.Key_Space)
        QTest.mouseClick(dialog.save_button,Qt.LeftButton)
        wait(lambda:dialog._task is None and index.snapshot()[0][0].annotation.name=="Hero experiment")
        capture(dialog,"manager-experiments-saved-notes.png",tmp_path)
        assert index.snapshot()[0][0].annotation.favorite
        assert "second caption" in index.snapshot()[0][0].annotation.notes
        QTest.mouseClick(dialog.saved_drafts_button,Qt.LeftButton)
        from qt_app.training_draft_transfer_dialog import TrainingDraftTransferDialog
        transfer=next(d for d in controller.open_reviews() if isinstance(d,TrainingDraftTransferDialog))
        wait(lambda:transfer._task is None);transfer.reject();dialog.reject()
        application.processEvents()
        QTest.mouseClick(window.training_experiments_button,Qt.LeftButton)
        reopened=next(iter(controller.open_reviews()))
        wait(lambda:reopened._task is None and reopened.runs.topLevelItemCount()==1)
        assert reopened.runs.topLevelItem(0).text(0)=="Hero experiment"
        reopened.reject()
    finally:
        controller=getattr(window,"_sdxl_preparation_controller",None)
        if controller is not None:
            controller.request_background_shutdown();wait(lambda:not controller.has_active_background_work())
        window.close();application.processEvents()


def test_scoped_retained_compare_and_clone_real_packages(tmp_path):
    import hashlib
    from core import TrainingPermissionDisposition,TrainingCandidateSelectionService,TagDatabase
    from core.training_package import TrainingPackageService,MANIFEST_NAME
    from core.training_dataset_preparation import binding as package_binding
    from core.training_process import ProcessInput,TrainingProcessPlan,_bytes,_directory
    from core.training_execution import retain_review
    from core.training_experiments import TrainingExperiments
    from core.training_plan_drafts import TrainingPlanDraftStore
    from qt_app.training_experiments_dialog import TrainingExperimentsDialog
    from test_training_draft_transfer import candidate
    application=app();jobs=BackgroundJobManager()
    from dataclasses import replace
    from core import LibraryImageIdentity
    plan,image=candidate(tmp_path)
    selection_service=TrainingCandidateSelectionService(TagDatabase(tmp_path/"tags.json"))
    plan=selection_service.plan((image,),replace(plan.goal,base_model_id="sha256:"+"a"*64),
        identities={image:LibraryImageIdentity("personal",image.name)})
    plan=selection_service.apply_permission_disposition(
        plan,TrainingPermissionDisposition("proceed","Reviewed generated test images.",True))
    packages=TrainingPackageService(selection_service.knowledge);parent=tmp_path/"packages";parent.mkdir()
    destination=parent/"frozen"
    preview=packages.preview(plan,destination);assert preview.ready,preview.issues
    created=packages.apply(preview,preview.decision.execution())
    assert created.status=="created",created.issues
    verified=packages.verify(destination);assert verified.valid,verified.issues
    selection={"root":str(destination),"package_fingerprint":verified.package_fingerprint,
        "manifest_sha256":hashlib.sha256((destination/MANIFEST_NAME).read_bytes()).hexdigest(),
        "recipe":{"rank":16,"alpha":16,"output_precision":"fp16","gradient_checkpointing":False,"schema_version":1}}
    contract={"resources":{"registry_revision":"a"*64},"package":package_binding(selection),
        "result_binding":{"package_path":str(destination),"package_fingerprint":verified.package_fingerprint,
            "manifest_sha256":selection["manifest_sha256"]}}
    executable=tmp_path/"generated-python.exe";executable.write_bytes(b"synthetic nonexecutable")
    index=TrainingRunIndex(tmp_path/"training-runs.json")
    for number in (1,2):
        process=TrainingProcessPlan(f"{number:032x}",str(tmp_path),_directory(tmp_path),"run-"+str(number),
            (str(executable),"-I"),(ProcessInput.capture(executable),),hashlib.sha256(_bytes(contract)).hexdigest())
        review_path,review_sha=retain_review(process,contract)
        index.register(TrainingRunRecord(process.run_id,"execution",str(review_path),review_sha,
            process.fingerprint,str(process.destination),str(tmp_path/"registry.json"),"a"*64),index.snapshot()[1])
    scoped=ScopedTrainingHistory(tmp_path)
    dialog=TrainingExperimentsDialog(scoped,jobs,tmp_path/"drafts.json",saved_drafts_available=True)
    clones=[];dialog.draftCreated.connect(clones.append)
    try:
        dialog.show();wait(lambda:dialog._task is None and dialog.runs.topLevelItemCount()==2)
        for i in range(2):dialog.runs.topLevelItem(i).setSelected(True)
        QTest.mouseClick(dialog.compare_button,Qt.LeftButton)
        wait(lambda:dialog._task is None)
        assert dialog.status.text()=="Comparison complete.",dialog.status.text()
        capture(dialog,"manager-experiments-comparison.png",tmp_path)
        dialog.runs.clearSelection();dialog.runs.topLevelItem(0).setSelected(True)
        dialog.clone_name.setText("Editable next experiment")
        QTest.mouseClick(dialog.clone_button,Qt.LeftButton)
        wait(lambda:dialog._task is None)
        assert len(clones)==1,dialog.status.text()
        assert TrainingPlanDraftStore(tmp_path/"drafts.json").all_drafts()[0]==clones[0]
        assert clones[0].plan_manifest==plan.to_manifest_dict()
        assert len(index.snapshot()[0])==2
    finally:dialog.reject();jobs.shutdown();application.processEvents()
