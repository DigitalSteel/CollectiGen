"""Generated comparison queues through the real service; no models or network."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import base64
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace as NS

import pytest
from PIL import Image
from PySide6.QtCore import QObject, Signal, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from core.annotation import GenerationResource
from core.generation_execution import digest
from core.generation_experiments import build_experiment, save_experiment, load_experiment
from core.generation_planning import GenerationPlan, save_plan, load_plan
from core.model_resource_registry import ModelResourceRegistry
from qt_app.generation_dialog import GenerationDialog
from qt_app import generation_experiment_dialog as groups


class Signals(QObject):
    complete = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class ImmediateTask:
    def __init__(self, jobs, name, call, **kwargs):
        self.call = call
        self.signals = Signals()
        self.job_id = "controlled-io"
    def start(self):
        try:
            self.signals.complete.emit(self.call(NS(cancel_requested=False, checkpoint=lambda: True)))
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class Jobs:
    def __init__(self, delayed=False):
        self.delayed = delayed
        self.pending = []
        self.all = {}
    def submit_observed(self, name, worker, listener, **kwargs):
        assert kwargs["resource"] == "gpu"
        job = NS(id="controlled-" + str(len(self.all)), state="running", cancel_requested=False,
                 update=lambda *a: None, result=None, error="")
        self.all[job.id] = job
        self.pending.append((worker, listener, job))
        if not self.delayed:
            self.finish()
        return job, None
    def finish(self):
        worker, listener, job = self.pending.pop(0)
        try:
            job.result = worker(job)
            job.state = "completed"
        except Exception as exc:
            job.error = str(exc)
            job.state = "failed"
        listener(job)
    def cancel(self, job_id):
        self.all[job_id].cancel_requested = True


class Adapter:
    """Reuse the normal Swarm response contract with generated 64px pixels."""
    def __init__(self, after_first=None):
        self.transport = self
        self.calls = 0
        self.after_first = after_first
    def prepare(self, plan, binding, registry, source_current=None):
        value = {"plan": plan.to_dict(), "request": plan.request.to_dict(),
                 "payload": {"prompt": plan.request.positive_prompt, "negativeprompt": "",
                     "model": "fixture.safetensors", "width": 64, "height": 64,
                     "seed": plan.request.seed, "steps": 2, "cfgscale": 1.0,
                     "sampler": "euler", "scheduler": "normal"},
                 "binding": deepcopy(binding), "capabilities": {"version": "inert-v1"}}
        value["fingerprint"] = digest(value)
        return value
    def revalidate(self, prepared):
        return {"session_id": "generated-test-session"}
    def events(self, payload, **kwargs):
        self.calls += 1
        out = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(out, format="PNG")
        params = {k: v for k, v in payload.items() if k not in {"session_id", "extra_metadata"}}
        yield {"image": "data:image/png;base64," + base64.b64encode(out.getvalue()).decode(),
               "metadata": json.dumps({"sui_image_params": params,
                   "sui_extra_data": payload["extra_metadata"],
                   "sui_models": [{"param": "model", "name": params["model"], "hash": "0x" + "b" * 64}]}),
               "batch_index": "0"}
        yield {"discard_indices": []}
        if self.calls == 1 and self.after_first:
            self.after_first()


def app():
    value = QApplication.instance() or QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf"):
        assert QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name)) >= 0
    value.setFont(QFont("Segoe UI", 10))
    value.setStyle("Fusion")
    return value


def plan():
    base = GenerationResource(id="sha256:" + "a" * 64, role="checkpoint",
                              name="fixture.safetensors", hashes={"sha256": "a" * 64})
    return GenerationPlan.create(None).revise({"seed": 7, "width": 64, "height": 64,
        "steps": 2, "cfg_scale": 1.0, "sampler": "euler", "scheduler": "normal",
        "model": "fixture.safetensors", "resources": [base.to_dict()],
        "safety_policy": {"policy_version": 1, "state": "allowed", "reviewed": True}})


def click(button):
    assert button.isEnabled()
    if button.isCheckable():
        button.setFocus()
        QTest.keyClick(button, Qt.Key_Space)
    else:
        QTest.mouseClick(button, Qt.LeftButton)
    QApplication.processEvents()


def settle(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert predicate()


def close_clean(widget):
    def choose_discard():
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox):
            button = modal.button(QMessageBox.Discard) or modal.button(QMessageBox.Yes)
            if button:
                QTest.mouseClick(button, Qt.LeftButton)
                return
        if widget.isVisible():
            QTimer.singleShot(20, choose_discard)
    QTimer.singleShot(20, choose_discard)
    QTest.keyClick(widget, Qt.Key_Escape)
    settle(lambda: not widget.isVisible())


def capture(widget, name):
    root = os.environ.get("COLLECTIGEN_COMPARISON_CAPTURE_DIR")
    if root:
        p = Path(root); p.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(p / (name + ".png")))


@pytest.mark.parametrize("mode", ["complete", "changed", "cancel", "ordinary"])
def test_checked_manager_queue_uses_real_service_and_retains_pending(tmp_path, monkeypatch, mode):
    application = app()
    monkeypatch.setattr(groups, "CoordinatedCallTask", ImmediateTask)
    selected = {"connection": ("http://127.0.0.1:7801", "fixture-v1"), "active": True}
    jobs = Jobs(delayed=mode == "cancel")
    owner = GenerationDialog(jobs, registry=ModelResourceRegistry(tmp_path / "models.json"),
        storage_root=tmp_path / "generation", runtime_allowed=mode == "ordinary", offline_alternatives=True,
        checked_connection=selected["connection"], checked_connection_provider=lambda: selected["connection"],
        checked_image_submission=True, file_selection_allowed=lambda: selected["active"])
    draft = owner.storage_root / "drafts/fixture.json"
    draft.parent.mkdir(parents=True, exist_ok=True)
    pin = save_plan(draft, plan())
    owner._apply_loaded_plan(load_plan(draft), draft, pin)
    owner.binding = {"resource_id": "sha256:" + "a" * 64, "tensor_sha256": "b" * 64}
    owner.adapter = Adapter(after_first=(lambda: selected.update(connection=None)) if mode == "changed" else None)
    owner.show()
    owner.open_experiment()
    group = owner._experiment_dialog
    assert not group.planning_only and owner.runtime_allowed is (mode == "ordinary")
    value = build_experiment(owner.plan, count=2, draft_first=False)
    path = owner.storage_root / "experiments" / (value["id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    pin = save_experiment(path, value)
    assert group.open_exact_group(path, pin, value["id"])
    click(group.safety_review)
    click(group.review)
    assert len(group.prepared) == 2, group.status.text()
    click(group.ack)
    capture(group, "checked-queue-review-" + mode)
    click(group.start)
    if mode == "cancel":
        assert group._job is not None
        click(group.cancel)
        jobs.finish()
    settle(lambda: not group.busy())
    states = [r["state"] for r in group.experiment["candidates"]]
    assert states == (["verified", "verified"] if mode in {"complete", "ordinary"} else
                      ["verified", "pending"] if mode == "changed" else ["cancelled", "pending"]), (states, group.status.text())
    assert owner.adapter.calls == (2 if mode in {"complete", "ordinary"} else 1 if mode == "changed" else 0)
    saved, _ = load_experiment(path)
    assert [r["state"] for r in saved["candidates"]] == states
    if mode == "changed":
        assert "connection" in group.status.text().lower()
    capture(group, "checked-queue-result-" + mode)
    close_clean(group); close_clean(owner); application.processEvents()


@pytest.mark.parametrize("existing_draft", [False, True])
def test_manager_visible_comparison_opens_saved_batch_and_returns(tmp_path, monkeypatch, existing_draft):
    application = app()
    from core.training_evaluation import create_evaluation
    from qt_app import training_evaluation_dialog as evaluation
    from qt_app import training_workflow_controller as workflow
    from suite_core.manager.window import create_window, unconfigured
    monkeypatch.setattr(evaluation, "CoordinatedCallTask", ImmediateTask)
    monkeypatch.setattr(workflow, "CoordinatedCallTask", ImmediateTask)
    monkeypatch.setattr(groups, "CoordinatedCallTask", ImmediateTask)
    saved = tmp_path / "fixed-comparison.json"
    save_plan(saved, create_evaluation(plan(), prompts=["generated blue cube"], seeds=[7, 8]))
    before = saved.read_bytes()
    monkeypatch.setattr(evaluation.QFileDialog, "getOpenFileName", lambda *a, **k: (str(saved), ""))
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    manager = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    manager.checked_image_connection = lambda: ("http://127.0.0.1:7801", "fixture-v1")
    manager.resize(1500, 960); manager.show()
    item = manager.navigation.item(manager.page_rows["Applications"])
    QTest.mouseClick(manager.navigation.viewport(), Qt.LeftButton,
                     pos=manager.navigation.visualItemRect(item).center())
    click(manager.training_comparison_button)
    controller = manager._training_comparison_controller
    review, = tuple(controller.dialogs)
    assert not review.batch_review_only
    click(review.open_saved)
    assert review.session is not None, review.status.text()
    capture(review, "manager-fixed-comparison-" + str(existing_draft))
    if existing_draft:
        from qt_app.generation_workflow import open_generation_workspace
        open_generation_workspace(manager, initial_plan=plan())
        settle(lambda: getattr(manager, "_generation_dialog", None) is not None)
        previous = manager._generation_dialog
        previous_digest = previous.plan.digest
        click(review.queue_missing)
        assert manager._generation_dialog is previous
        assert previous.plan.digest == previous_digest
        assert "close the open generate draft" in review.status.text().casefold()
        capture(review, "manager-preserved-open-draft")
        close_clean(previous)
    click(review.queue_missing)
    settle(lambda: getattr(manager, "_generation_dialog", None) is not None)
    generation = manager._generation_dialog
    queue = generation._experiment_dialog
    settle(lambda: queue.experiment is not None)
    assert not queue.planning_only
    assert len(queue.experiment["candidates"]) == 2
    assert not queue.start.isEnabled()
    assert generation.training_comparison_return.isVisible()
    capture(queue, "manager-saved-comparison-queue-" + str(existing_draft))
    click(queue.readiness_button)
    assert not queue.isVisible() and generation.isVisible()
    assert generation.tabs.currentIndex() == generation._readiness_tab_index
    assert generation.alternatives_return_button.isVisible()
    capture(generation, "manager-queue-readiness-" + str(existing_draft))
    click(generation.alternatives_return_button)
    assert generation._experiment_dialog is queue and queue.isVisible()
    close_clean(queue)
    click(generation.training_comparison_return_button)
    assert review.isVisible()
    assert saved.read_bytes() == before
    close_clean(generation); close_clean(review); manager.close(); application.processEvents()
