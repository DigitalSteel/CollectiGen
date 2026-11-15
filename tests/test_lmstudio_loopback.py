"""Real stock loopback HTTP fixtures; never contacts LM Studio or loads models."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading
from time import monotonic
from types import SimpleNamespace

import pytest

from core import lmstudio_transport as transport
from core.lmstudio_discovery import DiscoveryError

PAYLOAD = json.dumps({"models": [{"key": "generated/model", "type": "llm",
    "display_name": "Generated Model", "size_bytes": 1024,
    "loaded_instances": [{"id": "generated/instance", "config": {"context_length": 2048}}],
    "capabilities": {"vision": True}}]}).encode()


@pytest.fixture(autouse=True)
def owned_requests_settle(monkeypatch):
    threads, connections, reads = [], [], []
    def worker(*args, **kwargs):
        result = threading.Thread(*args, **kwargs)
        threads.append(result)
        return result
    def timer(*args, **kwargs):
        result = threading.Timer(*args, **kwargs)
        threads.append(result)
        return result
    # Only the adapter's namespace changes; Timer's real Thread base is untouched.
    monkeypatch.setattr(transport, "threading", SimpleNamespace(
        Thread=worker, Timer=timer, Event=threading.Event, Lock=threading.Lock))
    stock_close = transport.http.client.HTTPConnection.close
    def close(connection):
        connections.append(connection)
        return stock_close(connection)
    monkeypatch.setattr(transport.http.client.HTTPConnection, "close", close)
    stock_read = transport.http.client.HTTPResponse.read1
    def read(response, amount):
        reads.append(amount)
        return stock_read(response, amount)
    monkeypatch.setattr(transport.http.client.HTTPResponse, "read1", read)
    yield SimpleNamespace(reads=reads)
    assert all(not thread.is_alive() for thread in threads)
    assert connections and all(connection.sock is None for connection in connections)


@contextmanager
def server(mode="ok", *, ipv6=False, cancel=None):
    calls = []
    released, entered = threading.Event(), threading.Event()
    class FixtureServer(HTTPServer):
        address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        def handle_error(self, request, client_address):
            # The client deliberately closes stalled requests. Never log a body/token.
            pass
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def do_GET(self):
            calls.append({"method": self.command, "path": self.path,
                          "authorization": self.headers.get("Authorization")})
            entered.set()
            active = "ok" if mode == "retry" and len(calls) == 2 else mode
            code = {"redirect": 302, "auth": 401, "retry": 503}.get(active, 200)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            if active == "redirect":
                self.send_header("Location", "/forbidden-follow")
            if active == "chunked":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(PAYLOAD) + (10 if active == "truncated" else 0)))
            self.end_headers()
            try:
                if active == "chunked":
                    for part in (PAYLOAD[:13], PAYLOAD[13:]):
                        self.wfile.write(("%x\r\n" % len(part)).encode() + part + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                elif active in {"cancel", "deadline", "io"}:
                    self.wfile.write(PAYLOAD[:1]); self.wfile.flush()
                    if cancel is not None:
                        released.wait(.1)
                        cancel.set()
                    if active == "deadline":
                        # Progress resets per-read IO waits;only the total request
                        # budget can stop this before the last read's full IO wait.
                        for byte in PAYLOAD[1:4]:
                            released.wait(.12)
                            self.wfile.write(bytes([byte])); self.wfile.flush()
                    released.wait(3)
                elif active == "truncated":
                    self.wfile.write(PAYLOAD[:5])
                elif code == 200:
                    self.wfile.write(PAYLOAD)
                else:
                    self.wfile.write(b"private-generated-error-body")
                self.wfile.flush()
            except (OSError, ValueError):
                pass
            self.close_connection = True
    host = "::1" if ipv6 else "127.0.0.1"
    instance = FixtureServer((host, 0), Handler)
    thread = threading.Thread(target=instance.serve_forever,
        kwargs={"poll_interval": .02}, name="generated-lmstudio-http-fixture")
    thread.start()
    endpoint = "http://[%s]:%d" % (host, instance.server_port) if ipv6 else "http://%s:%d" % (host, instance.server_port)
    try:
        yield endpoint, calls, entered
    finally:
        released.set()
        instance.shutdown()
        instance.server_close()
        thread.join(4)
        assert not thread.is_alive()
        assert instance.socket.fileno() == -1


@pytest.mark.parametrize("ipv6", [False, True])
def test_real_numeric_loopback_success(ipv6):
    with server(ipv6=ipv6) as (endpoint, calls, _):
        result = transport.discover(endpoint)
        assert result.model("generated/model").vision is True
        model, loaded = result.loaded_instance("generated/instance")
        assert model.key != loaded.instance_id and loaded.config.context_length == 2048
        assert calls == [{"method": "GET", "path": "/api/v1/models", "authorization": None}]


def test_real_chunked_response():
    with server("chunked") as (endpoint, calls, _):
        assert len(transport.discover(endpoint).models) == 1
        assert len(calls) == 1


def test_synthetic_invocation_only_bearer():
    with server() as (endpoint, calls, _):
        transport.discover(endpoint, token="synthetic-request-token")
        transport.discover(endpoint)
        assert calls[0]["authorization"] == "Bearer synthetic-request-token"
        assert calls[1]["authorization"] is None
        assert all(row["path"] == "/api/v1/models" for row in calls)


@pytest.mark.parametrize("mode,code", [("redirect", "redirect_refused"),
    ("auth", "authentication_required_or_rejected"), ("truncated", "truncated_response")])
def test_real_http_refusal_has_no_retry_or_raw_body(mode, code):
    with server(mode) as (endpoint, calls, _):
        with pytest.raises(DiscoveryError) as caught:
            transport.discover(endpoint)
        assert str(caught.value) == code
        assert "private-generated-error-body" not in str(caught.value)
        assert len(calls) == 1 and calls[0]["path"] == "/api/v1/models"


def test_cancel_stalled_real_response(owned_requests_settle):
    cancel = threading.Event()
    with server("cancel", cancel=cancel) as (endpoint, calls, entered):
        began = monotonic()
        with pytest.raises(DiscoveryError, match="^discovery_cancelled$"):
            transport.discover(endpoint, cancelled=cancel.is_set,
                limits=transport.DiscoveryLimits(.3, 1, 1.5))
        assert entered.is_set() and monotonic() - began < 1.5
        assert len(owned_requests_settle.reads) >= 2
        assert len(calls) == 1


@pytest.mark.parametrize("mode,limits,code", [
    ("deadline", transport.DiscoveryLimits(.2, .6, .6), "total_budget"),
    ("io", transport.DiscoveryLimits(.2, .2, 1.5), "discovery_io_timeout"),
])
def test_real_stall_deadline_and_io_timeout(mode, limits, code, owned_requests_settle):
    with server(mode) as (endpoint, calls, _):
        began = monotonic()
        with pytest.raises(DiscoveryError) as caught:
            transport.discover(endpoint, limits=limits)
        if mode == "deadline":
            # With a socket wait clamped to the remaining request budget,
            # OS timeout and the total-deadline watcher may race at the boundary.
            assert str(caught.value) in {"discovery_deadline", "discovery_io_timeout"}
            assert len(owned_requests_settle.reads) >= 4
            assert monotonic() - began < .85
        else:
            assert str(caught.value) == code
            assert monotonic() - began < 1.5
        assert len(calls) == 1


def test_retry_requires_a_second_explicit_call():
    with server("retry") as (endpoint, calls, _):
        with pytest.raises(DiscoveryError, match="^http_unavailable$"):
            transport.discover(endpoint)
        assert len(calls) == 1
        assert len(transport.discover(endpoint).models) == 1
        assert len(calls) == 2
