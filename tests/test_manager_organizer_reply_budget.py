"""Retained helper timing regression; no native launch or authority is created."""
from threading import RLock
from types import SimpleNamespace

import pytest

from core.manager_launch_owner import LaunchHold
from core.ollama_owned_windows import BorrowedProcess
from suite_core.manager import bootstrap_entry as entry


def pending(monkeypatch, kind, role, elapsed):
    clock = [elapsed]
    monkeypatch.setattr(entry.time, "monotonic", lambda: clock[0])
    attempt = SimpleNamespace(identity="original")
    rpc = dict(kind="helper-reserve" if kind == "reserve" else "helper-register",
               send_entered=True, broken=False, began_monotonic=0, receipt=None)
    slot = dict(attempt=attempt, phase=kind + "_pending", rpc=rpc,
                reservation=None if kind == "reserve" else "reservation")
    adapter = entry.EntryHelperAdapter.__new__(entry.EntryHelperAdapter)
    adapter.role_base = role
    adapter.accounting = None
    adapter.session = SimpleNamespace(wire=SimpleNamespace(
        lock=RLock(), channel=SimpleNamespace(read_fault=None), received_pending=None))
    adapter.attempts = {attempt.identity: slot}
    adapter._original_attempt = lambda value: None
    adapter._pre_birth = lambda value: None
    adapter._slot = lambda reservation: slot
    job = SimpleNamespace(thread=True)
    class Child:
        _handle = 7
        pid = 9
        def poll(self):
            return None
    child = Child()
    child.job = job
    adapter.native = SimpleNamespace(job=job, logs={child: None}, _helper_create_entered=kind == "transfer")
    if kind == "transfer":
        slot.update(copy=object(), witness=object(), borrowed=BorrowedProcess(7, 9))
    reads = []
    def receive(value, request_kind, payload, expected, *, read_timeout):
        assert value is slot and value["rpc"] is rpc
        assert request_kind == rpc["kind"] and payload == {}
        reads.append(read_timeout)
        return {"original_reply": True}
    def apply(value, frame):
        assert value is slot and frame == {"original_reply": True}
        value.update(rpc=None, phase="reserved" if kind == "reserve" else "registered",
                     reservation="reservation")
    adapter._wire_rpc = receive
    adapter._wire_apply = apply
    wait = (lambda **kw: adapter.wait_for_original_reserve(attempt, **kw)) if kind == "reserve" else (
        lambda **kw: adapter.wait_for_original_transfer("reservation", **kw))
    return SimpleNamespace(adapter=adapter, slot=slot, rpc=rpc, reads=reads,
                           clock=clock, wait=wait, receive=receive)


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_organizer_retains_original_reply_after_large_package_review(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 61)
    assert case.wait() in ("reservation", True)
    assert case.reads == [30]
    assert case.slot["rpc"] is None


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
@pytest.mark.parametrize("role", ["owned-encyclopedia", "owned-ollama", "owned-llamacpp"])
def test_other_helpers_keep_original_thirty_second_deadline(monkeypatch, kind, role):
    case = pending(monkeypatch, kind, role, 31)
    with pytest.raises(entry.EntryHold, match="retained_helper_reply_deadline"):
        case.wait()
    assert case.reads == [] and case.slot["rpc"] is case.rpc


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_idle_poll_never_restarts_organizer_deadline_or_request(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 60)
    calls = []
    def receive(*args, **kwargs):
        calls.append(kwargs["read_timeout"])
        if len(calls) == 1:
            case.clock[0] += 10
            raise LaunchHold("control_channel_idle_timeout")
        return case.receive(*args, **kwargs)
    case.adapter._wire_rpc = receive
    assert case.wait() in ("reservation", True)
    assert calls == [30, 30]


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_organizer_deadline_retains_unsettled_original_attempt(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 121)
    with pytest.raises(entry.EntryHold, match="retained_helper_reply_deadline"):
        case.wait()
    assert case.reads == [] and case.slot["rpc"] is case.rpc


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_explicit_smaller_budget_and_cancellation_still_apply(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 31)
    with pytest.raises(entry.EntryHold, match="retained_helper_reply_deadline"):
        case.wait(timeout=30)
    with pytest.raises(entry.EntryHold, match="retained_helper_reply_cancelled"):
        case.wait(cancelled=lambda: True)
    assert case.reads == [] and case.slot["rpc"] is case.rpc


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_non_idle_channel_fault_is_not_retried(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 61)
    def fail(*args, **kwargs):
        raise LaunchHold("control_channel_partial_frame")
    case.adapter._wire_rpc = fail
    with pytest.raises(LaunchHold, match="control_channel_partial_frame"):
        case.wait()
    assert case.slot["rpc"] is case.rpc


@pytest.mark.parametrize("timeout", [True, -1, float("nan"), float("inf"), 121])
def test_organizer_budget_remains_finite_and_bounded(monkeypatch, timeout):
    case = pending(monkeypatch, "reserve", "owned-organizer", 0)
    with pytest.raises(entry.EntryHold, match="bounded_retained_reply_wait_required"):
        case.wait(timeout=timeout)
    assert case.reads == [] and case.slot["rpc"] is case.rpc


def test_reader_cannot_request_organizer_budget(monkeypatch):
    case = pending(monkeypatch, "reserve", "owned-encyclopedia", 0)
    with pytest.raises(entry.EntryHold, match="bounded_retained_reply_wait_required"):
        case.wait(timeout=120)
    assert case.reads == []


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_organizer_wait_uses_real_frame_read_with_original_channel_bound(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 61)
    wire = entry._NormalWire.__new__(entry._NormalWire)
    wire.timeout = 1
    wire.incoming = 0
    wire.received_pending = None
    wire.message_kinds = entry.MESSAGE_KINDS
    wire.proof = SimpleNamespace(binding_sha256="a" * 64)
    waits = []
    payload = dict(protocol=entry.PROTOCOL, sequence=1,
                   kind="helper-grant" if kind == "reserve" else "helper-registered",
                   binding_sha256=wire.proof.binding_sha256,
                   payload={"original_reply": True})
    raw = entry._wire_bytes(payload)
    def read_frame(*, timeout):
        assert 0 < timeout <= 30
        waits.append(timeout)
        return len(raw).to_bytes(4, "little") + raw
    wire.channel = SimpleNamespace(read_frame=read_frame)
    def receive(value, request_kind, extra, expected, *, read_timeout):
        assert value is case.slot and value["rpc"] is case.rpc and extra == {}
        case.reads.append(read_timeout)
        return wire.receive(timeout=read_timeout)["payload"]
    case.adapter._wire_rpc = receive
    assert case.wait() in ("reservation", True)
    assert case.reads == [30] and waits == [1]
    assert wire.incoming == 1


@pytest.mark.parametrize("kind", ["reserve", "transfer"])
def test_organizer_idle_read_stops_at_original_overall_deadline(monkeypatch, kind):
    case = pending(monkeypatch, kind, "owned-organizer", 61)
    def receive(value, request_kind, extra, expected, *, read_timeout):
        assert value is case.slot and value["rpc"] is case.rpc and extra == {}
        case.reads.append(read_timeout)
        case.clock[0] = 121
        raise LaunchHold("control_channel_idle_timeout")
    case.adapter._wire_rpc = receive
    with pytest.raises(entry.EntryHold, match="retained_helper_reply_deadline"):
        case.wait()
    assert case.reads == [30] and case.slot["rpc"] is case.rpc
