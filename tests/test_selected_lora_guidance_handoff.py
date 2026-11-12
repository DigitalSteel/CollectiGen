"""Synthetic role fidelity and atomic failure; no model files or generator."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel
from core.generation_planning import GenerationPlan
from core.training_configuration import TrainingConfiguration
from qt_app.generation_dialog import GenerationDialog
from qt_app.model_workspace_dialog import ModelWorkspaceDialog

ID = "sha256:" + "b" * 64

@pytest.mark.parametrize("retained_lora_base", [False, True])
@pytest.mark.parametrize("roles", [("checkpoint",), ("lora",), ("checkpoint", "lora")])
def test_selected_role_is_not_reinterpreted(roles, retained_lora_base):
    app = QApplication.instance() or QApplication([])
    captured = []
    class Field:
        def findData(self, value): return -1
        def setText(self, value): pass
    class Review:
        def __init__(self, *a, **kw):
            captured.append(kw)
            self.goal_kind = self.target = self.goal = Field()
        def exec(self): return QDialog.Rejected
    owner = NS(guidance_configuration=TrainingConfiguration(base_model_id=ID) if retained_lora_base else None, registry=None, jobs=None,
        workspace=NS(protected_roots=()), select_plan=False, image_count=0,
        guidance_goal_kind="concept", guidance_target_id="", guidance_goal="Synthetic goal", status=QLabel(),
        generation_handoff="retained")
    snapshot={"resources":{ID:{"declarations":[{"role":role,"family":"SDXL"} for role in roles]}}}
    with patch("qt_app.model_workflow_guidance_dialog.ModelWorkflowGuidanceDialog", Review):
        ModelWorkspaceDialog._guidance_ready(owner,(None,None,snapshot,()),ID,workflow_kind="generation")
    assert owner.generation_handoff == "retained"
    if len(roles) != 1:
        assert not captured and "one declared" in owner.status.text()
    elif roles == ("checkpoint",):
        assert captured[0]["current_configuration"].base_model_id == ID
        assert not captured[0]["selected_generation_resources"]
    else:
        assert captured[0]["current_configuration"] is None or captured[0]["current_configuration"].base_model_id == ""
        assert captured[0]["selected_generation_resources"] == ((ID,"lora"),)

@pytest.mark.parametrize("mode", ["success", "wrong_role", "stale", "unsupported_role"])
def test_apply_lora_is_role_aware_and_atomic(mode):
    app = QApplication.instance() or QApplication([])
    snapshot={"resources":{ID:{"declarations":[{"role":"checkpoint" if mode == "wrong_role" else "lora","family":"SDXL"}],"locations":[],"lineage":[],"user":{"display_name":"Synthetic LoRA"}}}}
    count=[0]
    def read():
        count[0]+=1
        return snapshot, "changed" if mode == "stale" and count[0] > 2 else "r"
    target=NS(plan=GenerationPlan.create(None,goal="Original"),registry=NS(snapshot=read),status=QLabel(),
        local_model_name=NS(clear=lambda:None),local_model_path=NS(clear=lambda:None),_generation=0,
        _refresh_from_plan=lambda message:None)
    before=target.plan
    request=NS(workflow_kind="generation",base_model_id="",resource_bindings=(),goal="Reviewed goal")
    guidance=NS(request=request,companion_requirements=(),source_urls=(),record_id="synthetic")
    config=NS(request_changes=lambda:{},recipe_id="synthetic",recipe_version=1,target_model_family="SDXL",target_model_version="1.0")
    selected=((ID,"checkpoint" if mode == "unsupported_role" else "lora"),)
    result=GenerationDialog.apply_model_guidance(target,guidance,config,expected_registry_revision="r",selected_resources=selected)
    assert result == (mode == "success")
    if result:
        assert target.plan.request.model_hash == ""
        assert [(item.id,item.role) for item in target.plan.request.resources] == [(ID,"lora")]
        assert target.plan.goal == "Reviewed goal" and before.goal == "Original"
    else:
        assert target.plan is before and target._generation == 0

def test_shared_setup_host_preserves_selected_lora(monkeypatch):
    from qt_app.pages import SetupPage
    from qt_app import generation_workflow
    from core import model_resource_registry
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(model_resource_registry,"ModelResourceRegistry",lambda *a,**k:NS(snapshot=lambda:({},"r")))
    chosen=((ID,"lora"),)
    guidance,configuration=object(),object()
    dialog=NS(generation_handoff=(guidance,configuration,"r",NS(catalog_write_token=lambda:"t"),"t",chosen),_generation_handoff_ready=True)
    calls=[]
    target=NS(status=QLabel(),apply_model_guidance=lambda *a,**k:calls.append((a,k)) or True)
    owner=NS(confirm_leave=lambda:True,_retain_models_generation_handoff=lambda *a:pytest.fail("Unexpected retention"))
    monkeypatch.setattr(generation_workflow,"open_generation_workspace",lambda parent,**k:k["on_opened"](target))
    SetupPage._continue_models_generation_handoff(owner,dialog)
    assert calls==[((guidance,configuration),{"expected_registry_revision":"r","selected_resources":chosen})]
    assert dialog.generation_handoff is None and not dialog._generation_handoff_ready
