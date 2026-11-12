"""Synthetic Training guidance component; also runnable under packaged Python."""
import json
import os
from pathlib import Path
import time


def exercise_training_guidance(root, capture_root=None):
    from unittest.mock import patch
    from PySide6.QtCore import Qt,QTimer
    from PySide6.QtGui import QFont,QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication,QDialog
    from core import model_workflow_guidance as knowledge
    from core import paths
    from core.settings import AppSettings
    from core.background_jobs import BackgroundJobManager
    from core import ExportProfileStore
    from qt_app.training_candidate_plan_dialog import TrainingCandidateGoalDialog,TrainingScopeChoice,TrainingScopeKind
    from qt_app.model_workspace_dialog import ModelWorkspaceDialog
    from qt_app.model_workflow_guidance_dialog import ModelWorkflowGuidanceDialog
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([])
    for name in ('segoeui.ttf','segoeuib.ttf'):
        assert QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name))>=0
    app.setFont(QFont('Segoe UI',10));app.setStyle('Fusion')
    jobs=BackgroundJobManager();settings=AppSettings(ai_models_directory=str(root/'models'))
    output=Path(capture_root) if capture_root else None
    if output:output.mkdir(parents=True,exist_ok=True)
    machine=knowledge.MachineCapabilityProfile(cpu_threads=8,ram_gib=32,gpu_name='Synthetic GPU',
        gpu_backend='cuda',gpu_vram_gib=16,free_storage_gib=100,evidence='synthetic')
    state={'step':'models'};errors=[];deadline=time.monotonic()+35
    def click(widget):
        assert widget is not None and widget.isVisible() and widget.isEnabled(),state['step']
        QTest.mouseClick(widget,Qt.LeftButton)
    def capture(widget,name):
        if output:widget.grab().save(str(output/(name+'.png')))
    with patch.object(paths,'MODEL_RESOURCES_PATH',root/'model_resources.json'),patch.object(knowledge,'machine_capability_from_setup',lambda *a,**k:machine):
        goal=TrainingCandidateGoalDialog(3,ExportProfileStore(root/'profiles.json'),'explicit',
            current_profile_id='collectigen.sdxl.tags-only',
            scope_choices=(TrainingScopeChoice(TrainingScopeKind.SELECTED,3,'Three frozen synthetic images'),),
            settings=settings,jobs=jobs)
        goal.goal_text.setText('Teach a synthetic character')
        original_output=goal.output_kind.currentData()
        goal.resize(1000,850);goal.show();app.processEvents();capture(goal,'01-training-goal')
        def drive():
            try:
                assert time.monotonic()<deadline,state['step']
                visible=[x for x in QApplication.topLevelWidgets() if x.isVisible()]
                models=next((x for x in visible if isinstance(x,ModelWorkspaceDialog)),None)
                guide=next((x for x in visible if isinstance(x,ModelWorkflowGuidanceDialog)),None)
                if state['step']=='models' and models is not None and models._task is None and models._registry_loaded:
                    models.resize(1200,850)
                    tabs=models.dialog_commands.bar.tabs;i=next(i for i in range(tabs.count()) if tabs.tabData(i)=='Planning')
                    QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(i).center())
                    button=next(button for item,button in models.dialog_commands.bar._buttons if item.key=='models.plan_guidance')
                    state['step']='guide';state['started']=time.monotonic();QTimer.singleShot(0,lambda:click(button))
                elif state['step']=='guide' and guide is None and models is not None and models._task is None:
                    assert time.monotonic()-state['started']<4,models.status.text()
                elif state['step']=='guide' and guide is not None:
                    assert guide.workflow.currentData()=='training'
                    guide.resize(1300,900);capture(guide,'02-training-guidance')
                    state['step']='preview';QTimer.singleShot(0,lambda:click(guide.preview_button))
                elif state['step']=='preview' and guide is not None and guide.use_button.isEnabled():
                    assert guide.plan.caption_profile is not None
                    capture(guide,'03-training-recommendation');state['step']='returned';click(guide.use_button)
                elif state['step']=='returned' and models is None and guide is None:
                    assert goal._guided_configuration is not None
                    assert goal.selected_profile().target_model_family=='SDXL'
                    assert goal.selected_configuration().output_kind==original_output
                    assert goal.scope_choice.currentData()==TrainingScopeKind.SELECTED
                    assert goal.scope_choices[goal.scope_choice.currentIndex()].count==3
                    assert goal.goal_text.text()=='Teach a synthetic character'
                    assert goal.continue_button.isEnabled()
                    capture(goal,'04-profile-returned');state['step']='complete';driver.stop()
            except BaseException as exc:
                errors.append(f"{state['step']}: {type(exc).__name__}: {exc}");driver.stop()
                for widget in QApplication.topLevelWidgets():
                    if isinstance(widget,QDialog) and widget.isVisible():QDialog.reject(widget)
        driver=QTimer();driver.setInterval(25);driver.timeout.connect(drive)
        try:
            driver.start();click(goal.model_guidance_button)
            end=time.monotonic()+10
            while state['step']!='complete' and not errors and time.monotonic()<end:app.processEvents();time.sleep(.005)
            assert not errors,errors
            assert state['step']=='complete',state
            result={'completed':True,'selected_images':3,'output_kind':original_output,
                'profile_family':goal.selected_profile().target_model_family,'catalog':str(knowledge.BASELINE_PATH),
                'catalog_sha256':__import__('hashlib').sha256(knowledge.BASELINE_PATH.read_bytes()).hexdigest(),
                'service_source':knowledge.__file__,'model_or_trainer_execution':False,'scope':'Training goal/recommendation component; no Dataset save or installed startup claim'}
            if output:(output/'RESULT.json').write_text(json.dumps(result,indent=2))
            return result
        finally:
            driver.stop();goal.close()
            end=time.monotonic()+10
            while not jobs.shutdown(wait=False) and time.monotonic()<end:app.processEvents();time.sleep(.005)
            app.processEvents()


def test_organizer_default_catalog_and_explicit_legacy_path(tmp_path):
    from core.model_workflow_guidance import ModelWorkflowGuidanceService,BASELINE_PATH
    import hashlib
    assert BASELINE_PATH.parent.name=='resources'
    assert hashlib.sha256(BASELINE_PATH.read_bytes()).hexdigest()=='751df980a86705b89cabfda5d3f6c92f1b21ef4c36de3a1cb67722b0fc3fb683'
    catalog=ModelWorkflowGuidanceService().catalog
    legacy=tmp_path/'legacy.json';legacy.write_bytes(BASELINE_PATH.read_bytes())
    assert ModelWorkflowGuidanceService(legacy).catalog==catalog
    assert catalog['catalog_id']=='collectigen.model-workflow-baseline'


def test_organizer_training_guidance_returns_profile(tmp_path):
    exercise_training_guidance(tmp_path,os.environ.get('COLLECTIGEN_TRAINING_GUIDANCE_CAPTURE_DIR'))


if __name__=='__main__':
    import sys
    application=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve()
    os.environ['TAG_MANAGER_UI_TEST']='1'
    os.environ['TAG_MANAGER_UI_TEST_USER_DATA_DIR']=str(root/'user')
    os.environ['TAG_MANAGER_UI_TEST_CACHE_DIR']=str(root/'cache')
    os.environ['QT_QPA_PLATFORM']='offscreen'
    sys.path.insert(0,str(application))
    result=exercise_training_guidance(root,root/'captures')
    assert Path(result['service_source']).resolve().is_relative_to(application)
    print(json.dumps(result))
