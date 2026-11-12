"""Original framed update RPC across idle polls; synthetic kernel boundary only."""
from queue import Queue, Empty
from threading import RLock, Thread, Event
from types import SimpleNamespace as NS
import pytest
from core.manager_launch_owner import LaunchHold
from suite_core.manager import bootstrap_entry as entry


def pair():
    incoming, outgoing = Queue(), Queue()
    child_id = dict(pid=12, creation_filetime=1001, executable_sha256="a" * 64)
    root_id = dict(pid=13, creation_filetime=1000, executable_sha256="b" * 64)
    class Channel:
        read_fault = None
        def __init__(self, source, target, local, peer):
            self.source, self.target, self.local, self.peer = source, target, local, peer
            self.sent = self.reads = 0
        def local_witness(self): return self.local
        def peer_witness(self): return self.peer
        def write_frame(self, data, *, timeout):
            self.sent += 1; self.target.put(data)
        def read_frame(self, *, timeout):
            self.reads += 1
            try: return self.source.get(timeout=timeout)
            except Empty: raise LaunchHold("control_channel_idle_timeout")
    child = Channel(incoming, outgoing, child_id, root_id)
    root = Channel(outgoing, incoming, root_id, child_id)
    document = dict(entry=child_id, launcher=root_id)
    def wire(channel):
        value = entry._NormalWire.__new__(entry._NormalWire)
        value.channel, value.document = channel, document
        value.proof = NS(binding_sha256="c" * 64)
        value.timeout = .01
        value.incoming = value.outgoing = 0
        value.lock, value.received_pending = RLock(), None
        value.message_kinds = entry.MESSAGE_KINDS
        return value
    session = entry.NormalEntrySession.__new__(entry.NormalEntrySession)
    session.wire, session.helper_outstanding = wire(child), None
    session.state, session.error = "ready", None
    session.readiness_outstanding = {"reply": {"kind": "ready"}}
    route = entry.NormalOwnerRoute.__new__(entry.NormalOwnerRoute)
    route.wire, route.channel = wire(root), root
    route.state, route.owner = "ready", NS(dispatch_lock=RLock())
    route._live_peer = lambda: True
    route.manager_update_profile_sha256 = "e" * 64
    route.manager_update_request_ids = set()
    return session, route, child


@pytest.mark.parametrize("operation,delay,poll", [
    ("configure", .04, .01), ("apply", .04, .01), ("repair", .04, .01),
    ("apply", 1.2, 1),
])
def test_delayed_original_update_reply_is_not_resent(operation, delay, poll):
    session, route, child = pair()
    session.wire.timeout = poll
    calls, errors = [], []
    expected = {"action": "close_for_apply"}
    def review(original, request):
        assert original is route
        calls.append(request)
        Event().wait(delay)
        return dict(status="reviewed", result=expected)
    route.manager_update_handler = review
    def dispatch():
        try: route._handle_next()
        except BaseException as error: errors.append(error)
    worker = Thread(target=dispatch); worker.start()
    try:
        reply = session.request_manager_update(operation, {}, profile_sha256="e" * 64)
    finally: worker.join(4)
    assert not errors and not worker.is_alive()
    assert reply["result"] == expected and len(calls) == 1
    assert child.sent == 1 and child.reads > 1
    assert session.state == "ready" and session.manager_update_outstanding is None
    assert session.readiness_outstanding == {"reply": {"kind": "ready"}}
    assert len(route.manager_update_request_ids) == 1


@pytest.mark.parametrize("fault", ["deadline", "partial", "eof", "peer"])
def test_update_wait_keeps_deadline_and_native_fault_holds(monkeypatch, fault):
    session, route, child = pair()
    monkeypatch.setattr(entry, "MANAGER_UPDATE_REPLY_TIMEOUT", .05)
    if fault in {"partial", "eof"}:
        def read(**kwargs):
            child.reads += 1
            if fault == "partial": child.read_fault = {"bytes_received": 1}
            raise LaunchHold("control_channel_timeout" if fault == "partial" else "control_channel_eof_or_unavailable")
        child.read_frame = read
    elif fault == "peer":
        original = child.read_frame
        def read(**kwargs):
            try: return original(**kwargs)
            finally: child.peer = {**child.peer, "creation_filetime": 999}
        child.read_frame = read
    with pytest.raises((LaunchHold, ValueError)):
        session.request_manager_update("apply", {}, profile_sha256="e" * 64)
    assert session.state == "held" and child.sent == 1
    assert session.manager_update_outstanding["send_entered"]
    if fault in {"partial", "eof"}: assert child.reads == 1
    with pytest.raises(ValueError):
        session.request_manager_update("apply", {}, profile_sha256="e" * 64)
    assert child.sent == 1


def test_wrong_update_reply_correlation_is_retained_and_held():
    session, route, child = pair()
    errors = []
    def corrupt_peer():
        try:
            request = route.wire.receive()["payload"]
            request = {**request, "request_id": "f" * 32, "status": "reviewed", "result": {}}
            request.pop("body")
            route.wire.send("manager-update-reply", request)
        except BaseException as error: errors.append(error)
    worker = Thread(target=corrupt_peer); worker.start()
    try:
        with pytest.raises(ValueError, match="correlated_manager_update_reply_required"):
            session.request_manager_update("apply", {}, profile_sha256="e" * 64)
    finally: worker.join(2)
    assert not errors and not worker.is_alive()
    assert session.state == "held" and child.sent == 1
    assert session.manager_update_outstanding["reply"]["payload"]["request_id"] == "f" * 32
