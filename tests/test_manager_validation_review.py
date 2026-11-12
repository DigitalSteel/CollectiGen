"""Manager Validation report scope and saved review; generated receipts, no GPU jobs."""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace as NS
import time

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget
from core import training_validation as validation
from core.training_process import (ProcessInput, TrainingProcessPlan, TrainingProcessResult,
    TrainingProcessService, TrainingRunJournal, _bytes, _directory)
from core.training_retained_history import upsert_retained_review
from core.training_run_index import TrainingRunIndex
from qt_app.training_validation_dialog import TrainingValidationDialog
from qt_app.training_workflow_controller import TrainingWorkflowController
from suite_core.manager.window import create_window, unconfigured
from test_manager_comparison_queue import app, click, settle, close_clean, capture


def saved_measurement(training_root):
    root = training_root / "validation"
    root.mkdir(parents=True)
    executable = root / "generated-interpreter.exe"
    executable.write_bytes(b"Generated receipt input; never executed")
    request = {"qualification": "product", "cases": [{"stable_id": "generated-image"}]}
    name = "heldout-generated"
    request_path = root / (name + ".validation-request.json")
    request_path.write_bytes(_bytes(request))
    request_input = ProcessInput.capture(request_path)
    plan = TrainingProcessPlan("1" * 32, str(root), _directory(root), name,
        (str(executable), "-I", "-S", "-B", "-X", "utf8", str(root / "generated-worker.py"), str(request_path)),
        (ProcessInput.capture(executable), request_input), validation._digest(request))
    (plan.destination / "events").mkdir(parents=True)
    (plan.destination / "work").mkdir()
    journal = TrainingRunJournal(plan.destination)
    reserved = TrainingProcessService._receipt(plan)
    tip = journal.append("reserved", {"plan": reserved, "fingerprint": validation._digest(reserved)}, "")
    for state, details in (("starting", {}), ("running", {}),
                           ("exited_unverified", {"tree_stopped": True, "exit_code": 0})):
        tip = journal.append(state, details, tip)
    # Generated saved-report data tests review only, not native qualification.
    report = {"schema_version": 1, "kind": "sdxl_heldout_validation_result",
        "profile": validation.PROFILE, "request_sha256": request_input.sha256,
        "process_fingerprint": plan.fingerprint, "metric": "epsilon_denoising_mse",
        "qualification": "product", "timesteps": list(validation.TIMESTEPS),
        "values": [{"stable_id": "generated-image", "mse_by_timestep": [1.0, 3.0]}],
        "mean_mse": 2.0, "elapsed_seconds": 1.0, "peak_cuda_memory_bytes": 1024}
    (plan.destination / "work" / validation.RESULT_NAME).write_bytes(_bytes(report))
    receipt = root / "saved-review.json"
    payload = {"schema_version": 1, "kind": "sdxl_heldout_validation_review",
        "plan": asdict(plan), "request_path": str(request_path),
        "request_sha256": request_input.sha256, "candidate": False}
    receipt.write_bytes(_bytes(payload))
    prepared = validation.restore_prepared(receipt, sha256(_bytes(payload)).hexdigest())
    outcome = validation.TrainingValidationOutcome("complete", "Generated measurement",
        TrainingProcessResult("exited_unverified", plan.destination, "", 0, True, tip), report)
    index = TrainingRunIndex(training_root / "training-runs.json")
    upsert_retained_review(index, "validation", {"prepared": prepared, "outcome": outcome, "registered": False})
    return report


def test_manager_history_opens_same_saved_measurement_twice_without_process_work(tmp_path, monkeypatch):
    application = app()
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    manager = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    manager.resize(1500, 960); manager.show()
    state = manager.generation_state()
    training_root = state.generation_data_root / "training"
    report = saved_measurement(training_root)
    before = {p: p.read_bytes() for p in training_root.rglob("*") if p.is_file()}
    def no_process(*args, **kwargs):
        pytest.fail("Saved measurement review must not execute, recover, or read live evaluator inputs")
    monkeypatch.setattr(validation, "_current_inputs", no_process)
    monkeypatch.setattr(validation.TrainingValidationService, "recover", no_process)
    monkeypatch.setattr(validation.TrainingValidationService, "execute", no_process)
    item = manager.navigation.item(manager.page_rows["Training"])
    QTest.mouseClick(manager.navigation.viewport(), Qt.LeftButton,
                    pos=manager.navigation.visualItemRect(item).center())
    seen = []; requested = 0; errors = []; deadline = time.monotonic() + 35
    def drive():
        nonlocal requested
        try:
            assert time.monotonic() < deadline, "History/report UI did not settle"
            history = getattr(manager, "_training_history_dialog", None)
            if history is None:
                return
            views = [w for w in application.topLevelWidgets()
                     if isinstance(w, TrainingValidationDialog) and w.isVisible()]
            if views:
                view = views[0]
                if any(previous is view for previous in seen):
                    return
                seen.append(view)
                assert view._report == report
                assert view.chart.values == (1.0, 3.0)
                assert view.table.rowCount() == 1
                assert view.table.item(0, 0).text() == "generated-image"
                assert "not image quality" in view.scope.text()
                assert "hash verified" in view.status.text()
                for button in (view.check_button, view.prepare_button, view.start_button,
                               view.cancel_button, view.recover_button):
                    assert not button.isVisible() and not button.isEnabled()
                capture(view, "manager-saved-validation-" + str(len(seen)))
                close = view.findChild(QDialogButtonBox).button(QDialogButtonBox.Close)
                QTimer.singleShot(0, lambda b=close: click(b))
            elif len(seen) == 2:
                QTimer.singleShot(0, lambda: QTest.keyClick(history, Qt.Key_Escape))
            elif requested == len(seen) and history.runs.topLevelItemCount() == 1 and history._task is None:
                row = history.runs.topLevelItem(0)
                QTest.mouseClick(history.runs.viewport(), Qt.LeftButton,
                                pos=history.runs.visualItemRect(row).center())
                if history.open_button.isEnabled():
                    requested += 1
                    QTimer.singleShot(0, lambda: click(history.open_button))
        except BaseException as exc:
            errors.append(repr(exc))
            for widget in application.topLevelWidgets():
                if widget.isVisible() and hasattr(widget, "reject"):
                    QTimer.singleShot(0, widget.reject)
    timer = QTimer(); timer.setInterval(15); timer.timeout.connect(drive); timer.start()
    try:
        click(manager.training_history_button)
    finally:
        timer.stop()
    assert not errors, errors
    assert len(seen) == 2
    assert {p: p.read_bytes() for p in training_root.rglob("*") if p.is_file()} == before
    manager.close(); application.processEvents()


@pytest.mark.parametrize("manager_host", [True, False])
def test_validation_result_route_uses_owning_report_folder(tmp_path, manager_host):
    application = app()
    if manager_host:
        session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
        parent = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
        state = parent.generation_state()
        from core.model_resource_registry import ModelResourceRegistry
        controller = parent.sdxl_preparation_controller(ModelResourceRegistry(tmp_path / "models.json"))
        expected = state.generation_data_root / "training" / "validation"
    else:
        from core.paths import USER_DATA_DIR
        parent = QWidget()
        controller = TrainingWorkflowController(NS(), parent)
        expected = USER_DATA_DIR / "training-validation"
    # Prepare only the result pointer; this check does not run or verify training.
    source = QWidget(parent); source.plan = None; source.contract = {"review": {"images": {"roles": {"validation": 1}}}}
    controller.dialogs.add(source)
    view = controller._validation(source, None)
    assert view.evaluation_root == expected.absolute()
    assert not view.start_button.isEnabled()
    view.reject(); controller.dialogs.discard(source); source.close(); parent.close(); application.processEvents()


@pytest.mark.parametrize("measurement", [[0.5, 1.0], [float("nan"), 1.0]])
def test_packaged_validation_worker_preserves_finite_measurement_contract(measurement):
    from core.training_validation_worker import evaluate_values
    request = {"cases": [{"stable_id": "generated"}], "timesteps": [250, 750]}
    if measurement[0] != measurement[0]:
        with pytest.raises(ValueError, match="validation_native_nonfinite"):
            evaluate_values(request, lambda case: measurement)
    else:
        assert evaluate_values(request, lambda case: measurement) == [
            {"stable_id": "generated", "mse_by_timestep": measurement}]


@pytest.mark.parametrize("choice", ["open", "cancel", "ended_session"])
def test_manager_history_imports_unlisted_validation(tmp_path, monkeypatch, choice):
    """Normal History entry, generated stopped receipt; no evaluator is launched."""
    from PySide6.QtWidgets import QFileDialog, QPushButton
    from core.training_validation import TrainingValidationService
    application = app()
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path)))
    manager = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    manager.resize(1500, 960); manager.show()
    state = manager.generation_state()
    training_root = state.generation_data_root / "training"
    report = saved_measurement(training_root)
    index_path = training_root / "training-runs.json"
    index_path.unlink()  # Generated receipt is deliberately not listed in History.
    receipt = training_root / "validation" / "saved-review.json"
    source_before = {p: p.read_bytes() for p in receipt.parent.rglob("*") if p.is_file()}
    def no_launch(*args, **kwargs):
        pytest.fail("Importing a retained review must not launch an evaluator")
    monkeypatch.setattr(TrainingValidationService, "execute", no_launch)
    # This route fixture contains no model/package inputs. The unchanged input
    # verifier has its own service tests; retain real journal/report readback.
    def generated_inputs_only(request):
        assert request == {"qualification": "product", "cases": [{"stable_id": "generated-image"}]}
    monkeypatch.setattr(validation, "_current_inputs", generated_inputs_only)
    picks = []
    def choose(*args, **kwargs):
        picks.append(True)
        if choice == "ended_session":
            session.state = "closed"
        return ("" if choice == "cancel" else str(receipt), "")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    item = manager.navigation.item(manager.page_rows["Training"])
    QTest.mouseClick(manager.navigation.viewport(), Qt.LeftButton,
                    pos=manager.navigation.visualItemRect(item).center())
    errors = []; clicked = []; deadline = time.monotonic() + 25
    def drive():
        try:
            assert time.monotonic() < deadline, "Unlisted review entry did not settle"
            history = getattr(manager, "_training_history_dialog", None)
            if history is None or clicked or history._task is not None:
                return
            assert history.runs.topLevelItemCount() == 0
            button = next(b for b in history.findChildren(QPushButton)
                          if b.text().startswith("Import Validation Review"))
            clicked.append(True)
            capture(history, "manager-unlisted-validation-entry")
            click(button)
        except BaseException as exc:
            errors.append(repr(exc))
            history = getattr(manager, "_training_history_dialog", None)
            if history is not None: history.reject()
    timer = QTimer(); timer.setInterval(20); timer.timeout.connect(drive); timer.start()
    try:
        click(manager.training_history_button)
    finally:
        timer.stop()
    assert not errors, errors
    assert picks == [True]
    controller = manager._sdxl_preparation_controller
    if choice == "open":
        view = next(v for v in controller.open_reviews() if isinstance(v, TrainingValidationDialog))
        settle(lambda: view._task is None, seconds=60)
        assert view._report is not None, view.status.text()
        settle(lambda: controller._retained_task is None, seconds=60)
        assert index_path.exists(), controller._retained_error
        assert view._report == report
        assert view.chart.values == (1.0, 3.0)
        rows, revision = TrainingRunIndex(index_path).snapshot()
        assert len(rows) == 1 and rows[0].kind == "validation"
        assert rows[0].review_path == str(receipt)
        assert "measurement" in view.status.text().lower()
        capture(view, "manager-unlisted-validation-imported")
        click(view.findChild(QDialogButtonBox).button(QDialogButtonBox.Close))
        settle(lambda: not controller.open_reviews())
    else:
        assert not controller.open_reviews()
        assert not index_path.exists()
    after = {p: p.read_bytes() for p in receipt.parent.rglob("*") if p.is_file()}
    assert {p: after[p] for p in source_before} == source_before
    extras = set(after) - set(source_before)
    assert extras <= {receipt.parent / ".trainer-owner.lock"}
    assert not extras or after[receipt.parent / ".trainer-owner.lock"] == b"0"
    session.state = "ready"
    controller.request_background_shutdown()
    settle(lambda: not controller.has_active_background_work())
    manager.close(); application.processEvents()
