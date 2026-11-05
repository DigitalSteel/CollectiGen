"""Explicit loopback model-list GET only; no saved connection or inference.

The caller owns the worker for this synchronous operation. Cancellation closes
only this request and never establishes that a server/model has stopped.
"""
from dataclasses import dataclass
import http.client
import math
import socket
import threading
import time
from urllib.parse import urlsplit

from core.generation_transport import local_endpoint
from core.lmstudio_discovery import DiscoveryError, MAX_BYTES, parse_models

ROUTE = "/api/v1/models"


def _require(value, code):
    if not value:
        raise DiscoveryError(code)


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    connect_seconds: float = 2
    io_seconds: float = 2
    total_seconds: float = 10
    response_bytes: int = MAX_BYTES

    def __post_init__(self):
        for value in (self.connect_seconds, self.io_seconds, self.total_seconds):
            _require(type(value) in (int, float) and math.isfinite(value)
                     and 0 < value <= 15, "invalid_limits")
        _require(max(self.connect_seconds, self.io_seconds) <= self.total_seconds
                 and type(self.response_bytes) is int
                 and 0 < self.response_bytes <= MAX_BYTES, "invalid_limits")


def _endpoint(value):
    _require(type(value) is str and 0 < len(value) <= 2048
             and not any(ord(c) <= 32 or ord(c) >= 127 for c in value),
             "invalid_endpoint")
    try:
        parts = urlsplit(value)
        _require(parts.port is not None and 1 <= parts.port <= 65535,
                 "explicit_port_required")
        # Refuse even empty query/fragment/user-info delimiters.
        _require(not any(c in value for c in "?#@"), "invalid_endpoint")
        return urlsplit(local_endpoint(value))
    except DiscoveryError:
        raise
    except (ValueError, UnicodeError):
        raise DiscoveryError("invalid_endpoint") from None


def _framing(response, maximum):
    fields = {}
    for name, value in response.getheaders():
        key = name.lower()
        if key in {"content-type", "content-encoding", "content-length", "transfer-encoding"}:
            _require(key not in fields, "duplicate_response_header")
            _require(type(value) is str and not any(ord(c) < 32 or ord(c) >= 127 for c in value),
                     "invalid_response_header")
            fields[key] = value.strip()
    _require(fields.get("content-type", "").split(";", 1)[0].strip().lower()
             == "application/json", "non_json_response")
    _require(fields.get("content-encoding", "identity").lower() == "identity",
             "encoded_response_refused")
    transfer = fields.get("transfer-encoding", "").lower()
    length = fields.get("content-length")
    _require(transfer in ("", "chunked") and not (transfer and length is not None),
             "ambiguous_response_framing")
    if length is not None:
        _require(0 < len(length) <= 10 and length.isascii() and length.isdigit()
                 and int(length) <= maximum, "response_byte_limit")
        return int(length)
    return None


def discover(endpoint, *, token=None, cancelled=lambda: False,
             limits=DiscoveryLimits(), connection_factory=None):
    """Return one model-list observation, or a content-free DiscoveryError.

    The optional token lives in this invocation only. The cancellation callback
    must be bounded/thread-safe; exceptions mean stop. The optional factory is
    solely a source-test seam. Construction of a client/server is not implicit.
    """
    parts = _endpoint(endpoint)
    _require(type(limits) is DiscoveryLimits and callable(cancelled), "invalid_request")
    _require(token is None or (type(token) is str and 0 < len(token) <= 4096
             and all(33 <= ord(c) <= 126 for c in token)), "invalid_token")
    factory = http.client.HTTPConnection if connection_factory is None else connection_factory
    _require(callable(factory), "invalid_request")
    stopped, expired, drain = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    sockets = []
    connection = response = timer = watcher = duplicate = None
    deadline = time.monotonic() + limits.total_seconds

    def cancellation_requested():
        try:
            return bool(cancelled())
        except Exception:
            return True

    def check():
        _require(not stopped.is_set() and not cancellation_requested(), "discovery_cancelled")
        _require(not expired.is_set() and time.monotonic() < deadline, "discovery_deadline")

    def remaining():
        check()
        return max(.001, deadline - time.monotonic())

    def abort():
        with lock:
            for owned in sockets:
                try:
                    owned.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def expire():
        expired.set()
        abort()

    def watch():
        while not drain.wait(.02):
            if cancellation_requested():
                stopped.set()
                abort()
                return

    try:
        check()
        connection = factory(parts.hostname, parts.port,
                             timeout=min(limits.connect_seconds, remaining()))
        timer = threading.Timer(remaining(), expire)
        watcher = threading.Thread(target=watch, name="lmstudio-discovery-cancel", daemon=True)
        timer.start()
        watcher.start()
        check()
        connection.connect()
        check()
        request_socket = connection.sock
        _require(request_socket is not None, "transport_unavailable")
        duplicate = request_socket.dup()
        with lock:
            sockets.extend((request_socket, duplicate))
        request_socket.settimeout(min(limits.io_seconds, remaining()))
        headers = {"Accept": "application/json", "Connection": "close"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        check()
        connection.request("GET", ROUTE, body=None, headers=headers)
        response = connection.getresponse()
        check()
        status = response.status
        _require(not 300 <= status < 400, "redirect_refused")
        _require(status not in (401, 403), "authentication_required_or_rejected")
        _require(status not in (404, 405), "unsupported_route")
        _require(status == 200, "http_unavailable")
        length = _framing(response, limits.response_bytes)
        data = bytearray()
        while True:
            if length is not None and len(data) == length:
                break
            request_socket.settimeout(min(limits.io_seconds, remaining()))
            chunk = response.read1(min(65536, limits.response_bytes + 1 - len(data)))
            check()
            if not chunk:
                break
            data.extend(chunk)
            _require(len(data) <= limits.response_bytes, "response_byte_limit")
        _require(length is None or len(data) == length, "truncated_response")
        check()
        result = parse_models(bytes(data))
        check()
    except DiscoveryError:
        raise
    except Exception as exc:
        check()
        code = "discovery_io_timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "transport_unavailable"
        raise DiscoveryError(code) from None
    finally:
        drain.set()
        cleanup_failed = False
        if timer is not None:
            timer.cancel()
        # No accepted operation returns while its cancellation/deadline worker
        # still holds request references. Native OS responsiveness is separate.
        for worker in (watcher, timer):
            if worker is not None and worker.ident is not None:
                worker.join()
        with lock:
            sockets.clear()
        for owned in (response, connection, duplicate):
            if owned is not None:
                try:
                    owned.close()
                except Exception:
                    cleanup_failed = True
        _require(not cleanup_failed, "transport_cleanup_failed")
    check()
    return result
