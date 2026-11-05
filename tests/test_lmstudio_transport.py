"""Injected source-only socket/HTTP cases; never contacts a server."""
from dataclasses import FrozenInstanceError
import json
import socket
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from core import lmstudio_transport as transport
from core.lmstudio_discovery import DiscoveryError
from core.lmstudio_transport import DiscoveryLimits, discover

PAYLOAD = json.dumps({"models": [{"key": "synthetic/model", "type": "llm",
    "display_name": "Example", "size_bytes": 1, "loaded_instances": []}]}).encode()


@pytest.fixture(autouse=True)
def owned_threads_settle(monkeypatch):
    original_thread, original_timer = threading.Thread, threading.Timer
    workers = []
    def thread(*args, **kwargs):
        value = original_thread(*args, **kwargs)
        if kwargs.get("name") == "lmstudio-discovery-cancel":
            workers.append(value)
        return value
    def timer(*args, **kwargs):
        value = original_timer(*args, **kwargs)
        workers.append(value)
        return value
    # Keep stdlib Timer's Thread base untouched; instrument only the client seam.
    monkeypatch.setattr(transport, "threading", SimpleNamespace(
        Thread=thread, Timer=timer, Event=threading.Event, Lock=threading.Lock))
    yield
    assert all(not worker.is_alive() for worker in workers)


class FakeSocket:
    def __init__(self, signal=None):
        self.signal = signal or threading.Event()
        self.closed = False
        self.aborted = False
        self.timeouts = []
        self.duplicate = None

    def dup(self):
        self.duplicate = FakeSocket(self.signal)
        return self.duplicate

    def settimeout(self, value):
        assert not self.closed
        self.timeouts.append(value)

    def shutdown(self, how):
        assert how == socket.SHUT_RDWR
        self.aborted = True
        self.signal.set()

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, *, status=200, body=PAYLOAD, headers=None, phase=None,
                 action=lambda phase: None, chunks=None, bad_close=None):
        self.status, self.body, self.phase = status, body, phase
        self.headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))] if headers is None else headers
        self.action, self.bad_close = action, bad_close
        self.sock = FakeSocket()
        self.requests, self.reads = [], 0
        self.connected = self.closed = self.response_closed = False
        self.chunks = list(chunks) if chunks is not None else [body]

    def at(self, phase):
        self.action(phase)
        if self.phase == phase:
            raise OSError("synthetic-secret-must-not-echo")

    def connect(self):
        self.at("connect")
        self.connected = True

    def request(self, method, route, *, body, headers):
        self.at("request")
        self.requests.append((method, route, body, dict(headers)))

    def getresponse(self):
        self.at("headers")
        return FakeResponse(self)

    def close(self):
        self.closed = True
        self.sock.close()
        if self.bad_close == "connection":
            raise OSError("synthetic-secret-must-not-echo")


class FakeResponse:
    def __init__(self, owner):
        self.owner = owner
        self.status = owner.status

    def getheaders(self):
        self.owner.at("framing")
        return self.owner.headers

    def read1(self, amount):
        self.owner.at("body")
        self.owner.reads += 1
        if not self.owner.chunks:
            return b""
        chunk = self.owner.chunks.pop(0)
        self.owner.chunks.insert(0, chunk[amount:]) if len(chunk) > amount else None
        return chunk[:amount]

    def close(self):
        self.owner.response_closed = True
        if self.owner.bad_close == "response":
            raise OSError("synthetic-secret-must-not-echo")


def factory(connection, seen=None):
    def create(host, port, *, timeout):
        if seen is not None:
            seen.append((host, port, timeout))
        return connection
    return create


def assert_drained(connection):
    assert connection.closed
    if connection.sock.duplicate is not None:
        assert connection.sock.duplicate.closed
    assert not any(t.name == "lmstudio-discovery-cancel" for t in threading.enumerate())


def test_one_exact_get_and_immutable_metadata_without_default_selection():
    connection = FakeConnection()
    seen = []
    value = discover("http://localhost:1234", connection_factory=factory(connection, seen))
    assert value.models[0].key == "synthetic/model"
    assert seen[0][:2] == ("127.0.0.1", 1234) and 0 < seen[0][2] <= 2
    assert connection.requests == [("GET", "/api/v1/models", None,
        {"Accept": "application/json", "Connection": "close"})]
    assert connection.reads == 1  # Known-length EOF does not reuse a retired socket.
    assert connection.response_closed
    with pytest.raises(FrozenInstanceError):
        value.models = ()
    assert_drained(connection)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:80/", "http://[::1]:1234"])
def test_literal_loopback(endpoint):
    connection = FakeConnection()
    seen = []
    discover(endpoint, connection_factory=factory(connection, seen))
    assert seen[0][0] in ("127.0.0.1", "::1")
    assert_drained(connection)


@pytest.mark.parametrize("endpoint", [None, "", "http://localhost", "https://localhost:1234",
    "http://example.test:1234", "http://127.0.0.2:1234", "http://localhost:0",
    "http://localhost:65536", "http://user@localhost:1234", "http://localhost:1234/v1",
    "http://localhost:1234?", "http://localhost:1234#", "http://localhost:1234\n",
    "http://local\u202ehost:1234", " http://localhost:1234", "file:///tmp"])
def test_refused_endpoint_never_constructs_connection(endpoint):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid URI reached connection factory")
    with pytest.raises(DiscoveryError):
        discover(endpoint, connection_factory=forbidden)


@pytest.mark.parametrize("token", ["", "x y", "x\nHeader:value", "\x7f", "\u00e9", 1, "x" * 4097])
def test_refused_token_never_connects(token):
    connection = FakeConnection()
    with pytest.raises(DiscoveryError, match="^invalid_token$"):
        discover("http://localhost:1234", token=token, connection_factory=factory(connection))
    assert not connection.connected


def test_invocation_only_bearer_and_sanitized_failures():
    token = "synthetic-secret-must-not-echo"
    connection = FakeConnection(status=401)
    try:
        discover("http://localhost:1234", token=token, connection_factory=factory(connection))
    except DiscoveryError as exc:
        assert str(exc) == "authentication_required_or_rejected"
        assert token not in str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    else:
        pytest.fail("unauthorized response accepted")
    assert connection.requests[0][3]["Authorization"] == "Bearer " + token
    assert connection.reads == 0
    assert_drained(connection)
    other = FakeConnection()
    discover("http://localhost:1234", connection_factory=factory(other))
    assert "Authorization" not in other.requests[0][3]
    assert_drained(other)


@pytest.mark.parametrize("status,code", [(301, "redirect_refused"), (307, "redirect_refused"),
    (403, "authentication_required_or_rejected"), (404, "unsupported_route"),
    (405, "unsupported_route"), (500, "http_unavailable"), (204, "http_unavailable")])
def test_status_refusal_has_no_body_followup_or_retry(status, code):
    connection = FakeConnection(status=status)
    with pytest.raises(DiscoveryError, match=f"^{code}$"):
        discover("http://localhost:1234", connection_factory=factory(connection))
    assert len(connection.requests) == 1 and connection.reads == 0
    assert_drained(connection)


@pytest.mark.parametrize("extra", [[("Content-Type", "application/json")],
    [("Content-Length", "1"), ("content-length", "1")],
    [("Transfer-Encoding", "chunked"), ("Content-Length", "1")],
    [("Transfer-Encoding", "gzip")], [("Content-Encoding", "gzip")],
    [("Content-Length", "-1")], [("Content-Length", "99999999999")],
    [("Content-Length", "x")], [("Content-Encoding", "identity\r\nsecret")]])
def test_invalid_framing_never_reads(extra):
    connection = FakeConnection(headers=[("Content-Type", "application/json")] + extra)
    with pytest.raises(DiscoveryError):
        discover("http://localhost:1234", connection_factory=factory(connection))
    assert connection.reads == 0
    assert_drained(connection)


@pytest.mark.parametrize("headers", [[], [("Content-Type", "text/plain")],
    [("Content-Type", "application/json"), ("Content-Length", "1000")]])
def test_content_or_truncation_refused(headers):
    connection = FakeConnection(headers=headers)
    with pytest.raises(DiscoveryError):
        discover("http://localhost:1234", connection_factory=factory(connection))
    assert_drained(connection)


def test_chunked_incremental_success_and_streamed_cap():
    connection = FakeConnection(headers=[("Content-Type", "application/json; charset=utf-8"),
        ("Transfer-Encoding", "chunked")], chunks=[PAYLOAD[:10], PAYLOAD[10:]])
    assert discover("http://localhost:1234", connection_factory=factory(connection)).models
    assert connection.reads == 3
    assert_drained(connection)
    large = FakeConnection(body=b"x" * 20, headers=[("Content-Type", "application/json")])
    with pytest.raises(DiscoveryError, match="^response_byte_limit$"):
        discover("http://localhost:1234", limits=DiscoveryLimits(response_bytes=10),
                 connection_factory=factory(large))
    assert_drained(large)


@pytest.mark.parametrize("values", [dict(total_seconds=True), dict(io_seconds=float("nan")),
    dict(connect_seconds=0), dict(total_seconds=16), dict(io_seconds=11),
    dict(response_bytes=True), dict(response_bytes=0), dict(response_bytes=2**23)])
def test_invalid_limits(values):
    with pytest.raises(DiscoveryError, match="^invalid_limits$"):
        DiscoveryLimits(**values)


@pytest.mark.parametrize("phase", ["connect", "request", "headers", "framing", "body"])
def test_transport_errors_sanitized_and_cleaned(phase):
    connection = FakeConnection(phase=phase)
    try:
        discover("http://localhost:1234", connection_factory=factory(connection))
    except DiscoveryError as exc:
        assert str(exc) == "transport_unavailable"
        assert "synthetic-secret" not in "".join(traceback.format_exception(exc))
    else:
        pytest.fail("transport failure returned metadata")
    assert_drained(connection)


@pytest.mark.parametrize("phase", ["connect", "request", "headers", "body", "framing"])
def test_cancellation_at_boundaries_never_returns_inventory(phase):
    cancel = threading.Event()
    connection = FakeConnection(action=lambda p: cancel.set() if p == phase else None)
    with pytest.raises(DiscoveryError, match="^discovery_cancelled$"):
        discover("http://localhost:1234", cancelled=cancel.is_set,
                 connection_factory=factory(connection))
    if phase == "connect":
        assert not connection.requests
    assert_drained(connection)


def test_preconnect_cancel_and_failing_callback_never_construct():
    def forbidden(*args, **kwargs):
        pytest.fail("cancelled call constructed a connection")
    for callback in (lambda: True, lambda: 1 / 0):
        with pytest.raises(DiscoveryError, match="^discovery_cancelled$"):
            discover("http://localhost:1234", cancelled=callback, connection_factory=forbidden)


def test_late_cleanup_cancellation_does_not_return_stale_inventory():
    cancel = threading.Event()
    connection = FakeConnection()
    original_close = connection.close
    def close():
        original_close()
        cancel.set()
    connection.close = close
    with pytest.raises(DiscoveryError, match="^discovery_cancelled$"):
        discover("http://localhost:1234", cancelled=cancel.is_set,
                 connection_factory=factory(connection))
    assert_drained(connection)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_blocked_phase_cancel_aborts_only_owned_sockets_and_drains(phase):
    cancel = threading.Event()
    connection = FakeConnection()
    def block(p):
        if p == phase:
            cancel.set()
            assert connection.sock.signal.wait(1), "watcher did not abort request"
            raise OSError("interrupted")
    connection.action = block
    with pytest.raises(DiscoveryError, match="^discovery_cancelled$"):
        discover("http://localhost:1234", cancelled=cancel.is_set,
                 connection_factory=factory(connection))
    assert connection.sock.aborted and connection.sock.duplicate.aborted
    assert_drained(connection)


def test_total_deadline_under_continuing_small_reads():
    connection = FakeConnection(headers=[("Content-Type", "application/json")], chunks=[b" "] * 100)
    connection.action = lambda phase: time.sleep(.012) if phase == "body" else None
    limits = DiscoveryLimits(connect_seconds=.05, io_seconds=.05, total_seconds=.06)
    start = time.monotonic()
    with pytest.raises(DiscoveryError, match="^discovery_deadline$"):
        discover("http://localhost:1234", limits=limits, connection_factory=factory(connection))
    assert connection.reads < 100 and time.monotonic() - start < 1
    assert_drained(connection)


def test_io_timeout_and_parser_failure_cleanup():
    def timeout(phase):
        if phase == "body":
            raise socket.timeout("private")
    connection = FakeConnection(action=timeout)
    with pytest.raises(DiscoveryError, match="^discovery_io_timeout$"):
        discover("http://localhost:1234", connection_factory=factory(connection))
    assert_drained(connection)
    broken = FakeConnection(body=b"not-json")
    with pytest.raises(DiscoveryError, match="^invalid_json$"):
        discover("http://localhost:1234", connection_factory=factory(broken))
    assert_drained(broken)


@pytest.mark.parametrize("owner", ["response", "connection"])
def test_cleanup_failure_never_returns_success_and_still_closes_other_owners(owner):
    connection = FakeConnection(bad_close=owner)
    with pytest.raises(DiscoveryError, match="^transport_cleanup_failed$"):
        discover("http://localhost:1234", connection_factory=factory(connection))
    assert connection.response_closed
    assert_drained(connection)


def test_no_real_network_process_or_persistence(monkeypatch):
    import builtins
    import io
    import os
    import subprocess
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail("discovery escaped its injected adapter")
    for obj, attr in [(socket, "socket"), (socket, "create_connection"),
                      (builtins, "open"), (io, "open"), (os, "system"),
                      (subprocess, "Popen"), (urllib.request, "urlopen")]:
        monkeypatch.setattr(obj, attr, forbidden)
    connection = FakeConnection()
    assert discover("http://localhost:1234", connection_factory=factory(connection)).models
    assert_drained(connection)
