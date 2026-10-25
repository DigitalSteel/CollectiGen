"""Explicit local Ollama metadata reads; no inference or lifecycle operations.

Reuse suite connection identities, endpoint/limit validation and strict decoder.
Construction is inert. Only discover/read contact the caller-selected endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import http.client
import socket
import ssl
import threading
import time
from typing import Protocol
from urllib.parse import urlsplit

from ..adapters.swarm_translation import BoundaryError
from ..backend_contracts import ConnectionId, FrozenSettings
from ..registry.status_view import ConfiguredConnection, Freshness, _freshness
from .swarm_readonly import DiscoveryError, EndpointBinding, ReadLimits, _inventory_object, _require
from .ollama_version import MODULE_VERSION, OLLAMA_DISCOVERY_VERSION

MAX_MODELS = 4096


class ReadRoute(str, Enum):
    VERSION = "/api/version"
    INSTALLED = "/api/tags"
    LOADED = "/api/ps"


class ReadState(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


def _time(value):
    _require(type(value) is datetime and value.utcoffset() is not None, "aware_timestamp_required")


def _text(value):
    return type(value) is str and 0 < len(value) <= 1024 and not any(ord(c) < 32 for c in value)


@dataclass(frozen=True, slots=True)
class ModelRecord:
    connection_ref: ConnectionId
    name: str
    digest: str | None
    size_bytes: int | None
    size_vram_bytes: int | None
    context_length: int | None
    modified_at: str | None
    expires_at: str | None
    reported: FrozenSettings = field(repr=False)

    @property
    def identity(self):
        """Provider identity, never a verified local weight/profile identity."""
        return self.connection_ref, self.name, self.digest


@dataclass(frozen=True, slots=True)
class Observation:
    route: ReadRoute
    state: ReadState
    attempted_at: datetime
    observed_at: datetime | None = None
    models: tuple[ModelRecord, ...] | None = None
    version: str | None = None
    failure_code: str | None = None
    raw_response: bytes | None = field(default=None, repr=False)

    @property
    def response_sha256(self):
        return None if self.raw_response is None else hashlib.sha256(self.raw_response).hexdigest()

    def freshness_at(self, now, limits: ReadLimits):
        _time(now)
        return _freshness(self.observed_at, limits.valid_for, now)


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    connection: ConfiguredConnection
    binding: EndpointBinding = field(repr=False)
    limits: ReadLimits
    observations: tuple[Observation, ...]
    adapter_version: str = MODULE_VERSION
    interface_version: int = OLLAMA_DISCOVERY_VERSION

    def observation(self, route: ReadRoute):
        _require(type(route) is ReadRoute, "read_route_required")
        return next(item for item in self.observations if item.route is route)

    def health_at(self, now):
        """Metadata reachability only; never execution readiness or idle capacity."""
        version = self.observation(ReadRoute.VERSION)
        freshness = version.freshness_at(now, self.limits)
        if freshness in (Freshness.STALE, Freshness.FUTURE):
            return freshness.value
        return version.state.value


class ReadTransport(Protocol):
    binding: EndpointBinding
    limits: ReadLimits

    def read(self, route: ReadRoute) -> bytes: ...


class LocalOllamaTransport:
    """Fixed GET routes using the existing bounded local HTTP transport pattern.

    Numeric loopback, no DNS/proxy/redirect/auth, deadline watchdog, byte cap.
    Each read is bounded by ReadLimits.total_seconds; no retries or polling.
    """
    def __init__(self, binding: EndpointBinding, limits: ReadLimits = ReadLimits()):
        _require(type(binding) is EndpointBinding and type(limits) is ReadLimits, "invalid_transport")
        self._binding, self._limits = binding, limits

    @property
    def binding(self): return self._binding

    @property
    def limits(self): return self._limits

    def read(self, route: ReadRoute) -> bytes:
        _require(type(route) is ReadRoute, "read_route_required")
        parts = urlsplit(self.binding.endpoint)
        limits = self.limits
        deadline = time.monotonic() + limits.total_seconds
        expired = threading.Event()
        connection = response = timer = None

        def remaining():
            seconds = deadline - time.monotonic()
            _require(seconds > 0 and not expired.is_set(), "total_timeout")
            return seconds

        try:
            context = ssl.create_default_context() if parts.scheme == "https" else None
            connection = http.client.HTTPConnection(parts.hostname, parts.port,
                timeout=min(limits.connect_seconds, remaining()))
            connection.connect()
            sock = connection.sock

            def abort():
                expired.set()
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass

            timer = threading.Timer(remaining(), abort)
            timer.daemon = True
            timer.start()
            if context is not None:
                sock = context.wrap_socket(sock, server_hostname=parts.hostname, do_handshake_on_connect=False)
                connection.sock = sock
                sock.settimeout(min(limits.connect_seconds, remaining()))
                sock.do_handshake()
            sock.settimeout(min(limits.io_seconds, remaining()))
            connection.request("GET", route.value, headers={"Accept": "application/json", "Connection": "close"})
            response = connection.getresponse()
            _require(not 300 <= response.status < 400, "redirect_refused")
            _require(response.status not in (401, 403), "approval_rejected")
            _require(response.status not in (404, 405), "unsupported_route")
            _require(response.status == 200, "http_unavailable")
            _require(response.getheader("Content-Encoding", "identity").lower() == "identity", "encoded_response_refused")
            _require(response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json", "non_json_response")
            length = response.getheader("Content-Length")
            transfer = response.getheader("Transfer-Encoding", "").lower()
            _require(transfer in ("", "chunked") and not (transfer and length is not None), "ambiguous_response_framing")
            if length is not None:
                _require(len(length) <= 10 and length.isascii() and length.isdigit()
                         and int(length) <= limits.response_bytes, "response_byte_limit")
            raw = response.read(limits.response_bytes + 1)
            remaining()
            _require(len(raw) <= limits.response_bytes, "response_byte_limit")
            if length is not None: _require(len(raw) == int(length), "truncated_response")
            return raw
        except DiscoveryError:
            raise
        except socket.timeout:
            raise DiscoveryError("total_timeout" if expired.is_set() or time.monotonic() >= deadline else "io_timeout") from None
        except (OSError, http.client.HTTPException, ValueError, RuntimeError):
            raise DiscoveryError("total_timeout" if expired.is_set() or time.monotonic() >= deadline else "transport_unavailable") from None
        finally:
            if timer is not None: timer.cancel()
            if response is not None: response.close()
            if connection is not None: connection.close()


def _models(value, connection_ref):
    rows = value.get("models")
    _require(type(rows) is list and len(rows) <= MAX_MODELS, "unknown_model_schema")
    records = []
    names = set()
    for row in rows:
        _require(type(row) is dict and _text(row.get("name")), "unknown_model_identity")
        name = row["name"]
        _require(name not in names, "duplicate_model_identity")
        names.add(name)
        _require("model" not in row or row["model"] == name, "conflicting_model_identity")
        for key in ("digest", "modified_at", "expires_at"):
            _require(row.get(key) is None or _text(row[key]), "invalid_model_text")
        for key in ("size", "size_vram", "context_length"):
            _require(row.get(key) is None or type(row[key]) is int and 0 <= row[key] <= 2**63 - 1, "invalid_model_measurement")
        _require("details" not in row or type(row["details"]) is dict, "invalid_model_details")
        records.append(ModelRecord(connection_ref, name, row.get("digest"), row.get("size"),
            row.get("size_vram"), row.get("context_length"), row.get("modified_at"), row.get("expires_at"),
            FrozenSettings.from_mapping(row)))
    return tuple(sorted(records, key=lambda item: item.name))


@dataclass(frozen=True, slots=True, init=False)
class ReadOnlyOllamaClient:
    connection: ConfiguredConnection
    transport: ReadTransport = field(repr=False)
    clock: object = field(repr=False)
    _binding: EndpointBinding = field(repr=False)
    _limits: ReadLimits

    def __init__(self, connection: ConfiguredConnection, transport: ReadTransport,
                 *, clock=lambda: datetime.now(timezone.utc)):
        binding, limits = transport.binding, transport.limits
        _require(type(connection) is ConfiguredConnection and type(binding) is EndpointBinding
                 and type(limits) is ReadLimits, "invalid_client")
        _require(connection.owner == binding.owner
                 and connection.connection_ref == binding.connection_ref, "connection_binding_mismatch")
        for name, value in (("connection", connection), ("transport", transport), ("clock", clock),
                            ("_binding", binding), ("_limits", limits)):
            object.__setattr__(self, name, value)
        self._check_transport()

    def _check_transport(self):
        _require(self.transport.binding == self._binding and self.transport.limits == self._limits,
                 "transport_binding_changed")

    def _held_report(self, previous, attempted):
        observations = tuple(
            Observation(route, ReadState.ERROR, attempted, failure_code="transport_binding_changed")
            if previous is None else replace(previous.observation(route), state=ReadState.ERROR,
                attempted_at=attempted, failure_code="transport_binding_changed")
            for route in ReadRoute)
        return DiscoveryReport(self.connection, self._binding, self._limits, observations)

    def discover(self, previous: DiscoveryReport | None = None) -> DiscoveryReport:
        if previous is not None:
            _require(type(previous) is DiscoveryReport and previous.connection == self.connection
                     and previous.binding == self._binding and previous.limits == self._limits,
                     "previous_report_binding_mismatch")
        attempted = self.clock()
        _time(attempted)
        try:
            self._check_transport()
            return self._discover(previous)
        except DiscoveryError as error:
            if error.code != "transport_binding_changed": raise
            # Discard this entire refresh: only already-bound prior evidence survives.
            return self._held_report(previous, attempted)

    def _discover(self, previous):
        observations = []
        for route in ReadRoute:
            attempted = self.clock()
            _time(attempted)
            self._check_transport()
            old = None if previous is None else previous.observation(route)
            try:
                raw = self.transport.read(route)
                self._check_transport()
                value = _inventory_object(raw, self._limits.response_bytes)
                _require(not value.get("error"), "provider_read_error")
                if route is ReadRoute.VERSION:
                    _require(_text(value.get("version")), "unknown_version_schema")
                    models, version = None, value["version"]
                else:
                    models, version = _models(value, self.connection.connection_ref), None
                observed = self.clock()
                _time(observed)
                self._check_transport()
                _require(observed >= attempted and (old is None or old.observed_at is None
                         or observed >= old.observed_at), "clock_rollback")
                observation = Observation(route, ReadState.AVAILABLE, attempted, observed, models, version,
                                          raw_response=raw)
            except (DiscoveryError, BoundaryError) as error:
                self._check_transport()
                if error.code == "transport_binding_changed": raise
                code = error.code
                state = (ReadState.UNAVAILABLE if code in {"transport_unavailable", "io_timeout", "total_timeout", "http_unavailable"}
                         else ReadState.UNSUPPORTED if code == "unsupported_route" else ReadState.ERROR)
                observation = (Observation(route, state, attempted, failure_code=code) if old is None
                               else replace(old, state=state, attempted_at=attempted, failure_code=code))
            observations.append(observation)
        self._check_transport()
        return DiscoveryReport(self.connection, self._binding, self._limits, tuple(observations))
