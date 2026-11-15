"""One native description POST. No retry, provider activation or model authority.

Only a future qualified consumer may admit actual server/JIT consequences.
Closing the client does not prove that server generation or GPU work stopped.
"""
from dataclasses import dataclass
import http.client
import math
import socket
import threading
import time

from core.lmstudio_description import (
    DescriptionError, MAX_RESPONSE_BYTES, ROUTE, encode_description, decode_description,
)
from core.lmstudio_discovery import DiscoveryError
from core.lmstudio_transport import _endpoint, _framing

SAFE_CODES = frozenset("""
invalid_limits invalid_request invalid_token invalid_endpoint explicit_port_required
invalid_model_key invalid_instance_id invalid_prompt invalid_image_bytes
invalid_image_mime invalid_temperature invalid_output_limit request_too_large
invalid_request_correlation invalid_response_bytes invalid_response
unexpected_stateful_response invalid_response_instance instance_mismatch
invalid_output invalid_output_item unsupported_output_item invalid_output_text
description_too_large empty_description invalid_statistics reconnect_refused
transport_unavailable redirect_refused authentication_required_or_rejected
unsupported_route http_unavailable duplicate_response_header invalid_response_header
non_json_response encoded_response_refused ambiguous_response_framing response_byte_limit
truncated_response description_cancelled description_deadline description_io_timeout
transport_cleanup_failed
""".split())


class DescriptionTransportError(DescriptionError):
    def __init__(self, code, submission_state, *, cleanup_failed=False):
        self.code = code if code in SAFE_CODES else "transport_unavailable"
        self.submission_state = submission_state
        self.cleanup_failed = bool(cleanup_failed)
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise DescriptionError(code)


@dataclass(frozen=True, slots=True)
class DescriptionLimits:
    connect_seconds: float = 2
    io_seconds: float = 5
    total_seconds: float = 120
    response_bytes: int = MAX_RESPONSE_BYTES

    def __post_init__(self):
        for value in (self.connect_seconds, self.io_seconds, self.total_seconds):
            _require(type(value) in (int, float) and 0 < value <= 300
                     and math.isfinite(value), "invalid_limits")
        _require(max(self.connect_seconds, self.io_seconds) <= self.total_seconds
                 and type(self.response_bytes) is int
                 and 0 < self.response_bytes <= MAX_RESPONSE_BYTES, "invalid_limits")


class _SingleConnection(http.client.HTTPConnection):
    """Guard the public connect method, including implicit stock reconnects."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connect_attempted = False

    def connect(self):
        _require(not self._connect_attempted, "reconnect_refused")
        self._connect_attempted = True
        return super().connect()


def describe(endpoint, *, model_key, expected_instance_id, prompt, image_bytes,
             image_mime, temperature, max_output_tokens, token=None,
             cancelled=lambda: False, limits=DescriptionLimits(),
             connection_factory=None):
    """Construct internally, send once, decode and retire only owned client work.

    The factory is an injected source-test seam, not a user-configurable provider.
    Errors after the POST attempt begins always have outcome_unknown disposition.
    Cancellation callbacks must be bounded and thread-safe; exceptions mean stop.
    """
    started = time.monotonic()
    submission = "not_sent"
    error_code = None
    cleanup_failed = False
    stopped, expired, drain = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    sockets = []
    connection = response = timer = watcher = duplicate = request_socket = None
    request = result = data = None
    headers = {}
    deadline = None

    def cancellation_requested():
        try:
            return bool(cancelled())
        except Exception:
            return True

    def stop_code():
        if stopped.is_set() or cancellation_requested():
            return "description_cancelled"
        if expired.is_set() or (deadline is not None and time.monotonic() >= deadline):
            return "description_deadline"
        return None

    def check():
        code = stop_code()
        _require(code is None, code)

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
        _require(type(limits) is DescriptionLimits and callable(cancelled), "invalid_request")
        deadline = started + limits.total_seconds
        check()
        parts = _endpoint(endpoint)
        _require(token is None or (type(token) is str and 0 < len(token) <= 4096
                 and all(33 <= ord(c) <= 126 for c in token)), "invalid_token")
        request = encode_description(
            model_key=model_key, expected_instance_id=expected_instance_id, prompt=prompt,
            image_bytes=image_bytes, image_mime=image_mime, temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        check()
        factory = _SingleConnection if connection_factory is None else connection_factory
        _require(callable(factory), "invalid_request")
        connection = factory(parts.hostname, parts.port,
                             timeout=min(limits.connect_seconds, remaining()))
        connection.set_debuglevel(0)
        timer = threading.Timer(remaining(), expire)
        watcher = threading.Thread(target=watch, name="lmstudio-description-cancel", daemon=True)
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
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Content-Length": str(len(request.body)), "Connection": "close"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        check()
        submission = "outcome_unknown"
        connection.request("POST", ROUTE, body=request.body, headers=headers)
        check()
        response = connection.getresponse()
        check()
        status = response.status
        _require(not 300 <= status < 400, "redirect_refused")
        _require(status not in (401, 403), "authentication_required_or_rejected")
        _require(status not in (404, 405), "unsupported_route")
        _require(status == 200, "http_unavailable")
        length = _framing(response, limits.response_bytes)
        data = bytearray()
        while length is None or len(data) < length:
            request_socket.settimeout(min(limits.io_seconds, remaining()))
            chunk = response.read1(min(65536, limits.response_bytes + 1 - len(data)))
            check()
            if not chunk:
                break
            data.extend(chunk)
            _require(len(data) <= limits.response_bytes, "response_byte_limit")
        _require(length is None or len(data) == length, "truncated_response")
        check()
        result = decode_description(bytes(data), request)
        check()
    except (DescriptionError, DiscoveryError) as caught:
        candidate = str(caught)
        error_code = candidate if candidate in SAFE_CODES else "transport_unavailable"
    except Exception as caught:
        error_code = stop_code() or (
            "description_io_timeout" if isinstance(caught, (TimeoutError, socket.timeout))
            else "transport_unavailable")
    finally:
        drain.set()
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                cleanup_failed = True
        for worker in (watcher, timer):
            if worker is not None and worker.ident is not None:
                try:
                    worker.join()
                except Exception:
                    cleanup_failed = True
        with lock:
            sockets.clear()
        for owned in (response, connection, duplicate, request_socket):
            if owned is not None:
                try:
                    owned.close()
                except Exception:
                    cleanup_failed = True
        headers.clear()
        token = request = data = response = connection = duplicate = request_socket = None
        prompt = image_bytes = endpoint = chunk = candidate = owned = None
    error_code = error_code or stop_code()
    if error_code is not None or cleanup_failed:
        result = None
        raise DescriptionTransportError(error_code or "transport_cleanup_failed", submission,
                                        cleanup_failed=cleanup_failed)
    return result
