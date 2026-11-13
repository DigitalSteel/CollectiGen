"""Same-actor startup-record retries. Synthetic seams; no native actor or release claim."""
from contextlib import nullcontext
from copy import deepcopy
import json
from threading import RLock
from types import SimpleNamespace as NS
import pytest
from core.manager_self_update_recovery import ManagerRecovery, RecoveryHold
from core.manager_stock_hook_provider import WindowsStockHookProvider
from core.manager_launch_owner import LaunchHold
from suite_core.manager.launcher_main import PublicLaunch


class Process:
    def __init__(self): self.code = None
    def poll(self): return self.code


def launch_fixture():
    launch = PublicLaunch.__new__(PublicLaunch)
    process = Process(); creates = []
    owner = NS(dispatch_lock=RLock(), children={}, record={"owner": "original", "children": {},
        "phase": "replacement_hold", "maintenance": {"replacement_dispatch": "not_started"}})
    def create(request, argv, **kwargs):
        creates.append((request, argv)); owner.children["replacement"] = process
        owner.record["children"]["replacement"] = {"pid": 456, "created": "synthetic-original"}
        owner.record["phase"] = "replacement_wait_health"
        owner.record["maintenance"]["replacement_dispatch"] = "created"
        return process
    owner.spawn_replacement = create
    calls = []
    def starting(proof):
        calls.append(deepcopy(proof))
        if len(calls) == 1: raise PermissionError("synthetic startup record IO")
    launch.owner = launch.update_owner = owner
    launch.update_request = NS(attempt_identity_sha256="attempt")
    launch.update_recovery = NS(starting=starting)
    launch._update_owned = lambda: None
    launch._update_target_start_pending = None
    launch._update_start_record_failure = None
    launch.error = None; launch.state = "update_replacement_ready"
    return NS(launch=launch, owner=owner, process=process, creates=creates, calls=calls)


def start_held(f):
    with pytest.raises(PermissionError):
        f.launch.start_update_target(("synthetic.exe",), admit_entry=lambda: None, create_process=lambda: None)
    assert f.launch.state == "update_target_start_record_held"


def test_record_retry_uses_original_registered_process_and_proof_once():
    f = launch_fixture(); start_held(f)
    original_error = f.launch.error
    result = f.launch.retry_update_target_start_record(explicitly_requested=True)
    assert result is f.process and len(f.creates) == 1 and len(f.calls) == 2
    assert f.calls[0] == f.calls[1] and f.launch.state == "update_target_health_pending"
    assert f.launch._update_start_record_failure is original_error
    with pytest.raises(ValueError): f.launch.retry_update_target_start_record(explicitly_requested=True)
    assert len(f.creates) == 1 and len(f.calls) == 2


@pytest.mark.parametrize("change", ["owner", "proof", "child", "exited", "phase", "dispatch"])
def test_changed_or_exited_actor_is_not_retried(change):
    f = launch_fixture(); start_held(f)
    if change == "owner": f.owner.record["owner"] = "different"
    elif change == "proof": f.owner.record["children"]["replacement"]["pid"] += 1
    elif change == "child": f.owner.children["replacement"] = Process()
    elif change == "exited": f.process.code = 2
    elif change == "phase": f.owner.record["phase"] = "replacement_normal"
    else: f.owner.record["maintenance"]["replacement_dispatch"] = "reserved"
    with pytest.raises(ValueError): f.launch.retry_update_target_start_record(explicitly_requested=True)
    assert f.launch.state == "update_held" and len(f.calls) == 1 and len(f.creates) == 1


def test_no_explicit_retry_or_uncertain_creation_gets_no_continuation():
    f = launch_fixture(); start_held(f)
    with pytest.raises(ValueError): f.launch.retry_update_target_start_record(explicitly_requested=False)
    assert len(f.calls) == 1
    f = launch_fixture()
    def uncertain(*args, **kwargs): raise PermissionError("creation uncertain")
    f.owner.spawn_replacement = uncertain
    with pytest.raises(PermissionError): f.launch.start_update_target((), admit_entry=None, create_process=None)
    assert f.launch.state == "update_held" and f.launch._update_target_start_pending is None


def test_repeated_record_io_failure_retains_same_original_actor():
    f = launch_fixture(); start_held(f)
    def unavailable(proof): raise PermissionError("still unavailable")
    f.launch.update_recovery.starting = unavailable
    with pytest.raises(PermissionError): f.launch.retry_update_target_start_record(explicitly_requested=True)
    assert f.launch.state == "update_target_start_record_held" and len(f.creates) == 1
    f.launch.update_recovery.starting = lambda proof: None
    assert f.launch.retry_update_target_start_record(explicitly_requested=True) is f.process


@pytest.mark.parametrize("committed", [False, True])
def test_start_record_reconciles_atomic_io_failure_without_second_event(tmp_path, monkeypatch, committed):
    from core import manager_self_update_recovery as module
    recovery = ManagerRecovery.__new__(ManagerRecovery)
    recovery.journal_path = tmp_path / "transaction.json"
    journal = {"phase": "launch_ready", "binding": {"target": {"version": "synthetic"}}, "events": []}
    recovery.journal_path.write_text(json.dumps(journal))
    recovery.owned = nullcontext
    recovery.load = lambda: json.loads(recovery.journal_path.read_bytes())
    gates = []; recovery.gate = lambda name, j: gates.append(name)
    codes = []; recovery.current_code = lambda target: codes.append(target)
    data = []; recovery.check_data = lambda j, side: data.append(side)
    atomic = module.atomic; failures = [True]
    def interrupted(path, raw, **kwargs):
        if failures[0]:
            failures[0] = False
            if committed: atomic(path, raw, **kwargs)
            raise PermissionError("synthetic IO after write" if committed else "synthetic IO before write")
        return atomic(path, raw, **kwargs)
    monkeypatch.setattr(module, "atomic", interrupted)
    proof = {"owner_token": "original", "original_process": {"pid": 456}}
    with pytest.raises(PermissionError): recovery.starting(proof)
    recovery.starting(proof)
    assert recovery.load()["phase"] == "awaiting_health"
    assert len(recovery.load()["events"]) == 1 and len(gates) == 2 and len(codes) == 4
    assert data == ["expected_b", "expected_b"]
    before = recovery.journal_path.read_bytes()
    with pytest.raises(RecoveryHold): recovery.starting({"owner_token": "different"})
    assert recovery.journal_path.read_bytes() == before


def provider_fixture():
    provider = WindowsStockHookProvider.__new__(WindowsStockHookProvider)
    process = Process(); provider.owner = NS(children={"replacement": process})
    provider.transport = NS(includes=lambda candidate: candidate is process)
    provider._original = lambda: None
    provider.state = "successor_start_record_held"
    provider.selection = NS(revalidate=lambda: provider.selection, admitted=object(), executable_sha256="exe")
    member = NS(process=process, state="suspended", channel=object())
    provider.successor_member = member
    launch = NS(owner=provider.owner, state="update_target_start_record_held")
    calls = []
    def record(**kwargs): calls.append(kwargs); launch.state = "update_target_health_pending"; return process
    launch.retry_update_target_start_record = record
    return NS(provider=provider, process=process, member=member, launch=launch, calls=calls)


def test_provider_retries_record_then_creates_normal_route_and_resumes_once(monkeypatch):
    from suite_core.manager import bootstrap_entry
    f = provider_fixture(); order = []
    def route(*args, **kwargs): order.append("route"); return NS()
    def resume(): order.append("resume"); f.member.state = "running"
    f.member.resume_registered = resume
    monkeypatch.setattr(bootstrap_entry, "NormalOwnerRoute", route)
    assert f.provider.retry_start_successor(f.launch, explicitly_requested=True) is f.process
    assert order == ["route", "resume"] and len(f.calls) == 1 and f.provider.state == "successor_running"
    with pytest.raises(LaunchHold): f.provider.retry_start_successor(f.launch, explicitly_requested=True)
    assert order == ["route", "resume"] and len(f.calls) == 1


@pytest.mark.parametrize("change", ["member", "job", "exited", "explicit"])
def test_provider_rejects_uncertain_resume_or_changed_job_before_recording(change):
    f = provider_fixture()
    if change == "member": f.member.state = "resume_uncertain"
    elif change == "job": f.provider.transport.includes = lambda candidate: False
    elif change == "exited": f.process.code = 2
    with pytest.raises(LaunchHold): f.provider.retry_start_successor(f.launch, explicitly_requested=change != "explicit")
    assert not f.calls


def test_supervision_preserves_existing_nonzero_startup_repair_handoff():
    f = provider_fixture(); f.process.code = 2; f.launch.state = "update_target_health_pending"
    def failed(): raise EOFError("synthetic target exited")
    f.provider.successor_route = NS(state="held", handle_next=failed)
    def repair(): f.launch.state = "update_repair_available"
    f.launch.update_target_failed = repair
    assert f.provider.supervise_successor(f.launch) is f.launch
    assert f.launch.state == "update_repair_available" and isinstance(f.launch.error, EOFError)


def test_normal_successor_close_keeps_existing_terminal_verifiers():
    f = provider_fixture(); f.provider.successor_route = NS(state="local_accepted")
    calls = []; terminal = object()
    f.provider.finish_successor_close = lambda: calls.append("closed")
    f.provider.verify_terminal = object(); f.provider.settle_terminal_resources = object()
    def retire(**kwargs):
        assert kwargs == dict(verify_terminal=f.provider.verify_terminal, settle_resources=f.provider.settle_terminal_resources)
        calls.append("retired"); return terminal
    f.launch.retire_update = retire
    assert f.provider.supervise_successor(f.launch) is terminal and calls == ["closed", "retired"]


@pytest.mark.parametrize("failed_start", [False, True])
def test_visible_original_review_retry_and_existing_repair_handoff(tmp_path, monkeypatch, failed_start):
    import os
    from pathlib import Path
    from time import monotonic
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog, QPushButton, QLabel
    from test_manager_generator_setup_ui import application
    app = application()
    launch = PublicLaunch.__new__(PublicLaunch)
    launch.theme = "dark" if failed_start else "light"; launch.state = "update_target_start_record_held"
    launch.error = PermissionError("Synthetic startup record unavailable")
    launch.report_paths = []; launch.abort_action_admission = None; launch.route = None
    launch.update_terminal_receipt = None
    owner = NS(native_transport=NS()); launch.owner = owner
    provider = WindowsStockHookProvider.__new__(WindowsStockHookProvider); provider.owner = owner; provider.state = "successor_start_record_held"
    owner.native_transport.stock_hook_provider = provider
    calls = []; receipt = object()
    def finish(held):
        held.state = "update_closed"; held.update_terminal_receipt = receipt; return receipt
    def retry(held):
        calls.append("retry")
        if failed_start:
            held.state = "update_repair_available"; return held
        return finish(held)
    def repair(held): calls.append("repair"); return finish(held)
    provider.run_start_record_retry = retry; provider.run_explicit_repair = repair
    launch.can_close = lambda: launch.state == "update_closed" and not launch.update_worker.close_held
    launch.recheck = lambda: launch.can_close()
    def wait(predicate):
        end = monotonic() + 5
        while not predicate() and monotonic() < end: QTest.qWait(10)
        assert predicate()
    def visible(dialog):
        dialog.show(); app.processEvents()
        retry_button = dialog.findChild(QPushButton, "managerUpdateRetryStartup")
        repair_button = dialog.findChild(QPushButton, "managerUpdateRepair")
        close = dialog.findChild(QPushButton, "startupRecoveryClose")
        status = dialog.findChild(QLabel, "startupRecoveryStatus")
        assert retry_button.isVisible() and retry_button.isEnabled() and not repair_button.isVisible()
        assert retry_button.isDefault()
        assert not close.isEnabled() and "retry updated Manager" in status.text()
        out = Path(os.environ.get("CG_START_RETRY_CAPTURE_DIR", str(tmp_path))); out.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(out / ("startup-record-retry-"+str(failed_start)+".png")))
        QTest.mouseClick(retry_button, Qt.LeftButton)
        assert not retry_button.isEnabled() and not close.isEnabled()
        if failed_start:
            wait(lambda: repair_button.isVisible() and repair_button.isEnabled())
            assert not retry_button.isVisible() and "prior Manager" in status.text()
            assert "still paused" not in dialog.findChild(QLabel, "startupRecoveryOverview").text()
            assert repair_button.isDefault()
            assert calls == ["retry"] and not close.isEnabled()
            assert dialog.grab().save(str(out / "startup-record-repair-handoff.png"))
            QTest.mouseClick(repair_button, Qt.LeftButton)
        wait(close.isEnabled)
        assert calls == (["retry", "repair"] if failed_start else ["retry"])
        assert "verified" in status.text() and not retry_button.isVisible()
        QTest.mouseClick(dialog.findChild(QPushButton, "startupRecoveryCheck"), Qt.LeftButton)
        assert ("Recovery verified" if failed_start else "startup and closure verified") in status.text()
        QTest.mouseClick(close, Qt.LeftButton); assert not dialog.isVisible(); return 0
    monkeypatch.setattr(QDialog, "exec", visible)
    assert launch.present() == 1
