"""Manager result-evaluation wiring using generated registry/prepared outcome fixtures.

These cases exercise visible comparison/Generate controls, not native Training
completion, checkpoint publication or generation execution qualification.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from qt_app import training_evaluation_dialog as evaluation
from core.model_resource_registry import inspect_model_resource
from qt_app.generation_workflow import _generation_model_registry
from suite_core.manager.window import create_window, unconfigured
from test_manager_comparison_queue import app, click, settle, close_clean, capture, ImmediateTask


def registered_fixture(manager, tmp_path):
    registry = _generation_model_registry(manager)
    # Registry identity and a declared role are real; this tiny tensor is not a
    # trained/output-qualified LoRA and is never submitted to a generator.
    model = tmp_path / "generated-evaluation.safetensors"
    header = json.dumps({"fixture.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    model.write_bytes(len(header).to_bytes(8, "little") + header + bytes(4))
    inspected = inspect_model_resource(model)
    resource = registry.register(inspected, role="lora", family="sdxl", expected_registry=registry.snapshot()[1])
    return registry, resource


@pytest.mark.parametrize("origin", ["result", "checkpoint"])
def test_manager_registered_result_comparison_uses_manager_return(tmp_path, monkeypatch, origin):
    application = app()
    monkeypatch.setattr(evaluation, "CoordinatedCallTask", ImmediateTask)
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    manager = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    manager.checked_image_connection = lambda: ("http://127.0.0.1:7801", "generated-test")
    manager.resize(1500, 960); manager.show()
    item = manager.navigation.item(manager.page_rows["Applications"])
    QTest.mouseClick(manager.navigation.viewport(), Qt.LeftButton,
                    pos=manager.navigation.visualItemRect(item).center())
    state = manager.generation_state()
    state.generation_data_root.mkdir(parents=True, exist_ok=True)
    registry, resource = registered_fixture(manager, tmp_path)
    registry_before = registry.path.read_bytes()
    controller = manager.sdxl_preparation_controller(registry)
    selected_run = "generated-result-run"
    contract = {"resources": {"registry_revision": registry.snapshot()[1]}}
    # Prepare an already-completed display state. Native execution is outside
    # this callback fix; the real dialogs and their signal wiring are exercised.
    run = controller._run(NS(plan=NS(run_id=selected_run), contract=contract),
                          NS(), registry, {}, {})
    if origin == "checkpoint":
        controller._export_checkpoint(run, {"step": 2, "epoch": 1})
        source = next(d for d in controller.dialogs if d is not run)
        source.resource_id = resource
        source._update()
    else:
        source = run
        source._state = "completed"
        source._outcome = NS(resource_id=resource, verified=None, history_retry_required=False, process=NS(tree_stopped=True))
        source._update_actions()
        tab = next(i for i in range(source.tabs.count()) if "Recovery" in source.tabs.tabText(i))
        QTest.mouseClick(source.tabs.tabBar(), Qt.LeftButton, pos=source.tabs.tabBar().tabRect(tab).center())
    assert source.evaluation_button.isVisible()
    click(source.evaluation_button)
    settle(lambda: getattr(manager, "_training_comparison_controller", None) is not None)
    review, = tuple(manager._training_comparison_controller.dialogs)
    assert review.run_id == selected_run and review.required_resource == resource
    assert not review.batch_review_only
    generation = manager._generation_dialog
    assert resource in {r.id for r in generation.plan.request.resources if r.role == "lora"}
    before = generation.plan.digest
    click(review.open_generate)
    assert generation.training_comparison_return.isVisible()
    assert not review.isVisible()
    capture(generation, "manager-registered-" + origin + "-return")
    click(generation.training_comparison_return_button)
    settle(review.isVisible)
    assert manager.navigation.currentRow() == manager.page_rows["Applications"]
    assert review.run_id == selected_run and review.required_resource == resource
    assert generation.plan.digest == before
    assert registry.path.read_bytes() == registry_before
    capture(review, "manager-registered-" + origin + "-comparison")
    close_clean(generation); close_clean(review)
    for dialog in tuple(controller.dialogs):
        close_clean(dialog)
    manager.close(); application.processEvents()


def test_training_result_evaluation_keeps_original_host_fallback(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from qt_app.training_workflow_controller import TrainingWorkflowController
    from qt_app import generation_workflow
    application = app()
    parent = QWidget()
    controller = TrainingWorkflowController(NS(), parent)
    source = NS(plan=NS(run_id="original-host-run"))
    # A simple hashable retained pointer is sufficient for this dispatch check.
    class Pointer:
        plan = source.plan
    source = Pointer(); controller.dialogs.add(source)
    calls = []
    monkeypatch.setattr(controller, "evaluation", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(generation_workflow, "open_generation_workspace", lambda *args, **kwargs: kwargs["on_opened"](None))
    controller._evaluate_resource(source, "sha256:" + "a" * 64)
    assert calls == [("original-host-run", "sha256:" + "a" * 64)]
    controller.dialogs.clear(); parent.close(); application.processEvents()


def test_training_result_evaluation_waits_for_its_active_host(monkeypatch):
    from PySide6.QtWidgets import QWidget
    from qt_app.training_workflow_controller import TrainingWorkflowController
    from qt_app import generation_workflow
    application = app(); parent = QWidget()
    controller = TrainingWorkflowController(NS(), parent, session_allowed=lambda: False)
    class Pointer:
        plan = NS(run_id="ended-session-run")
    source = Pointer(); controller.dialogs.add(source)
    calls = []
    monkeypatch.setattr(generation_workflow, "open_generation_workspace", lambda *a, **k: calls.append(k))
    controller._evaluate_resource(source, "sha256:" + "a" * 64)
    assert calls == []
    controller.dialogs.clear(); parent.close(); application.processEvents()
