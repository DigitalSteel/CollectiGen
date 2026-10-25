"""Fixed Swarm observation routes, exact loopback binding, no session creation.

Only the explicit read/discover methods perform I/O. Policy/session originate in
the trusted host; payloads/responses cannot select another authority or route.
Reuses the qualified translation decoder under exact source pins, not a second
JSON implementation. Not an activated app adapter, global lease or model registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import http.client
import ipaddress
import json
import math
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from ..backend_contracts import ApplicationId, ConnectionId, StableId
from ..adapters.swarm_translation import BoundaryError, PAYLOAD_BYTES, _object, _text

MAX_RESPONSE_BYTES = PAYLOAD_BYTES
MAX_MODELS = 128
MAX_BACKENDS = 64
_IMAGE_FIELDS = frozenset({"prompt", "negativeprompt", "model", "width", "height", "seed",
                           "steps", "cfgscale", "sampler", "scheduler"})


class DiscoveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # No endpoint, session, response or credentials.


def _require(value, code):
    if not value: raise DiscoveryError(code)


class ReadRoute(str, Enum):
    STATUS = "GetCurrentStatus"
    PARAMETERS = "ListT2IParams"
    BACKENDS = "ListBackends"


@dataclass(frozen=True, slots=True)
class EndpointBinding:
    """Trusted existing host configuration assertion, not approval provenance."""
    owner: ApplicationId
    connection_ref: ConnectionId
    approval_ref: StableId
    endpoint: str = field(repr=False)

    def __post_init__(self):
        _require(type(self.owner) is ApplicationId and type(self.connection_ref) is ConnectionId
                 and type(self.approval_ref) is StableId, "invalid_binding")
        _require(_text(self.endpoint) and not any(c.isspace() for c in self.endpoint)
                 and "\\" not in self.endpoint, "invalid_endpoint")
        try:
            parts = urlsplit(self.endpoint)
            _require(parts.scheme in ("http", "https") and parts.username is None and parts.password is None
                     and parts.path in ("", "/") and not parts.query and not parts.fragment
                     and parts.port is not None and 1 <= parts.port <= 65535, "invalid_endpoint")
            # Numeric loopback avoids DNS/proxy/remote contact in this first slice.
            _require(parts.hostname is not None and "%" not in parts.hostname, "invalid_endpoint")
            _require(ipaddress.ip_address(parts.hostname).is_loopback, "loopback_binding_required")
        except DiscoveryError:
            raise
        except (ValueError, TypeError, UnicodeError):
            raise DiscoveryError("invalid_endpoint") from None


@dataclass(frozen=True, slots=True)
class ReadLimits:
    connect_seconds: float = 2.0
    io_seconds: float = 2.0
    total_seconds: float = 5.0
    response_bytes: int = MAX_RESPONSE_BYTES
    valid_for: timedelta = timedelta(seconds=30)

    def __post_init__(self):
        for value, maximum in ((self.connect_seconds, 2), (self.io_seconds, 2), (self.total_seconds, 5)):
            _require(type(value) in (int, float) and 0 < value <= maximum, "invalid_time_limit")
        _require(type(self.response_bytes) is int and 0 < self.response_bytes <= MAX_RESPONSE_BYTES,
                 "invalid_response_limit")
        _require(type(self.valid_for) is timedelta and 0 < self.valid_for.total_seconds() <= 30,
                 "invalid_freshness_limit")


@dataclass(frozen=True, slots=True)
class ReadEvidence:
    connection_ref: ConnectionId
    route: ReadRoute
    observed_at: datetime
    raw_request: bytes = field(repr=False)
    raw_response: bytes = field(repr=False)
    response_sha256: str


class DiscoveryState(str, Enum):
    READY = "ready_metadata"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    HELD = "technical_hold"


@dataclass(frozen=True, slots=True)
class ModelObservation:
    reported_name: str
    revision: str | None = None
    verified_weights: bool = False
    approved_profile: bool = False


@dataclass(frozen=True, slots=True)
class BackendObservation:
    reported_id: str
    reported_type: str
    reported_state: str | None
    enabled: bool | None


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    owner: ApplicationId
    connection_ref: ConnectionId
    state: DiscoveryState
    observed_at: datetime
    valid_for: timedelta
    fields: frozenset[str] = frozenset()
    models: tuple[ModelObservation, ...] = ()
    backends: tuple[BackendObservation, ...] = ()
    evidence: tuple[ReadEvidence, ...] = field(default=(), repr=False)
    failure_code: str | None = None
    server_id: str | None = None
    server_version: str | None = None
    resource_identity_verified: bool = False
    global_activity_known: bool = False
    execution_qualified: bool = False

    def fresh_at(self, now):
        _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
                 "invalid_timestamp")
        return timedelta(0) <= now - self.observed_at <= self.valid_for


MAX_INVENTORY_COLLECTION = 4096
MAX_INVENTORY_NODES = 65536
MAX_INVENTORY_DEPTH = 16


def _inventory_require(value, code):
    if not value: raise BoundaryError(code)


def _inventory_object(raw, max_bytes):
    """Parameter inventory only; other routes retain the frozen small decoder."""
    _inventory_require(type(raw) is bytes and len(raw) <= max_bytes, "byte_limit")
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
        elif byte == 34: quoted = True
        elif byte in (91, 123):
            depth += 1
            _inventory_require(depth <= MAX_INVENTORY_DEPTH, "depth_limit")
        elif byte in (93, 125): depth -= 1
    def pairs(rows):
        _inventory_require(len(rows) <= MAX_INVENTORY_COLLECTION, "collection_limit")
        result = {}
        for key, value in rows:
            _inventory_require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def nonfinite(_): raise BoundaryError("nonfinite_json")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except BoundaryError: raise
    except (UnicodeError, ValueError, RecursionError):
        raise BoundaryError("malformed_json") from None
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        _inventory_require(nodes <= MAX_INVENTORY_NODES, "node_limit")
        if type(item) in (dict, list):
            _inventory_require(depth <= MAX_INVENTORY_DEPTH, "depth_limit")
            _inventory_require(len(item) <= MAX_INVENTORY_COLLECTION, "collection_limit")
            if type(item) is dict:
                try:
                    for key in item: key.encode("utf-8")
                except UnicodeError: raise BoundaryError("invalid_json_unicode") from None
            pending.extend((child, depth + 1) for child in (item.values() if type(item) is dict else item))
        elif type(item) is str:
            try: item.encode("utf-8")
            except UnicodeError: raise BoundaryError("invalid_json_unicode") from None
        elif type(item) is float: _inventory_require(math.isfinite(item), "nonfinite_json")
        else: _inventory_require(item is None or type(item) in (bool, int), "invalid_json_type")
    _inventory_require(type(value) is dict, "json_object_required")
    return value


def _main_model_names(params, rows):
    row = next((row for row in rows if row["id"] == "model"), None)
    _require(row is not None and row.get("type", "model") == "model", "unknown_main_model_parameter")
    values = row.get("values")
    if values is not None:
        _require(type(values) is list and len(values) <= MAX_MODELS and all(_text(name) for name in values),
                 "unknown_or_oversize_model_schema")
        _require(len(set(values)) == len(values), "duplicate_model_name")
    catalog = None
    if "models" in params:
        models = params["models"]
        _require(type(models) is dict and all(_text(key) and type(entries) is list for key, entries in models.items()),
                 "unknown_model_catalog_schema")
        for entries in models.values():
            _require(all(type(pair) is list and len(pair) == 2 and _text(pair[0])
                         and (pair[1] is None or _text(pair[1])) for pair in entries), "unknown_model_catalog_schema")
            _require(len({pair[0] for pair in entries}) == len(entries), "duplicate_model_name")
    if "subtype" in row:
        subtype = row["subtype"]
        _require(subtype == "Stable-Diffusion", "unsupported_main_model_subtype")
        _require(type(params.get("models")) is dict and subtype in params["models"], "missing_main_model_catalog")
        catalog = params["models"][subtype]
        _require(len(catalog) <= MAX_MODELS, "unknown_or_oversize_model_schema")
        names = [pair[0] for pair in catalog]  # Class IDs are metadata, never revision evidence.
        _require(values is None or set(values) == set(names), "contradictory_main_model_inventory")
    else:
        # Explicit legacy values-list mode; never infer/merge an unrelated catalog.
        _require(values is not None and "models" not in params, "unknown_or_oversize_model_schema")
        names = values
    return names


class ReadOnlySwarmClient:
    """No automatic session/credentials, redirects, proxies, retries or fallback."""
    def __init__(self, binding: EndpointBinding, existing_session: str, limits: ReadLimits = ReadLimits()):
        _require(type(binding) is EndpointBinding and type(limits) is ReadLimits, "binding_and_limits_required")
        _require(_text(existing_session), "existing_session_required")
        self._binding, self._limits, self._session = binding, limits, existing_session

    @property
    def binding(self): return self._binding

    @property
    def limits(self): return self._limits

    def read(self, route: ReadRoute) -> ReadEvidence:
        _require(type(route) is ReadRoute, "readonly_route_required")
        payload = {"session_id": self._session}
        if route is ReadRoute.BACKENDS: payload["full_data"] = True
        raw_request = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        parts, limits = urlsplit(self.binding.endpoint), self.limits
        deadline = time.monotonic() + limits.total_seconds
        connection, timer, response, sock = None, None, None, None
        expired = threading.Event()
        def remaining():
            value = deadline - time.monotonic()
            _require(value > 0 and not expired.is_set(), "total_timeout")
            return value
        try:
            # Context creation is local synchronous setup: normalize failures and
            # reject an expired budget before opening a socket.
            context = ssl.create_default_context() if parts.scheme == "https" else None
            connection = http.client.HTTPConnection(parts.hostname, parts.port,
                timeout=min(limits.connect_seconds, remaining()))
            connection.connect()
            sock = connection.sock
            sock.settimeout(min(limits.io_seconds, remaining()))
            # Start the watchdog before TLS. Explicit handshake separation lets
            # it shut down the current socket during handshake and HTTP framing.
            def abort():
                expired.set()
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            timer = threading.Timer(remaining(), abort)
            timer.daemon = True
            timer.start()
            if context is not None:
                sock = context.wrap_socket(sock, server_hostname=parts.hostname,
                    do_handshake_on_connect=False)
                connection.sock = sock
                sock.settimeout(min(limits.connect_seconds, remaining()))
                sock.do_handshake()
            sock.settimeout(min(limits.io_seconds, remaining()))
            connection.request("POST", "/API/" + route.value, raw_request,
                {"Content-Type": "application/json", "Accept": "application/json", "Connection": "close"})
            response = connection.getresponse()
            _require(not 300 <= response.status < 400, "redirect_refused")
            _require(response.status not in (401, 403), "existing_session_or_approval_rejected")
            _require(response.status == 200, "http_unavailable")
            _require(response.getheader("Content-Encoding", "identity").lower() == "identity", "encoded_response_refused")
            _require(response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json",
                     "non_json_response")
            length = response.getheader("Content-Length")
            transfer = response.getheader("Transfer-Encoding", "").lower()
            _require(transfer in ("", "chunked") and not (transfer and length is not None), "ambiguous_response_framing")
            if length is not None:
                _require(len(length) <= 10 and length.isascii() and length.isdigit() and int(length) <= limits.response_bytes,
                         "response_byte_limit")
            raw = response.read(limits.response_bytes + 1)
            _require(not expired.is_set() and time.monotonic() < deadline, "total_timeout")
            _require(len(raw) <= limits.response_bytes, "response_byte_limit")
            if length is not None: _require(len(raw) == int(length), "truncated_response")
            decoder = _inventory_object if route is ReadRoute.PARAMETERS else _object
            value = decoder(raw, limits.response_bytes)
            _require(not value.get("error") and not value.get("error_id"), "provider_read_error")
            _require(not expired.is_set() and time.monotonic() < deadline, "total_timeout")
            return ReadEvidence(self.binding.connection_ref, route, datetime.now(timezone.utc),
                                raw_request, raw, hashlib.sha256(raw).hexdigest())
        except DiscoveryError:
            raise
        except BoundaryError as error:
            raise DiscoveryError("invalid_response_" + error.code) from None
        except socket.timeout:
            raise DiscoveryError("total_timeout" if expired.is_set() or time.monotonic() >= deadline else "io_timeout") from None
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError, RuntimeError):
            code = "total_timeout" if expired.is_set() or time.monotonic() >= deadline else "transport_unavailable"
            raise DiscoveryError(code) from None
        finally:
            if timer is not None: timer.cancel()
            if response is not None: response.close()
            if connection is not None: connection.close()

    def discover(self) -> DiscoveryReport:
        evidence = []
        observed = datetime.now(timezone.utc)
        try:
            for route in ReadRoute:
                evidence.append(self.read(route))
                if route is ReadRoute.PARAMETERS:
                    params = _inventory_object(evidence[-1].raw_response, self.limits.response_bytes)
                    rows = params.get("list")
                    _require(type(rows) is list and all(type(row) is dict and _text(row.get("id")) for row in rows),
                             "unknown_parameter_schema")
                    fields = frozenset(row["id"] for row in rows)
                    _require(len(fields) == len(rows), "duplicate_parameter_id")
                    names = _main_model_names(params, rows)
            inventory = _object(evidence[2].raw_response, self.limits.response_bytes)
            backends = []
            for key, row in inventory.items():
                if type(row) is dict and "type" in row:
                    _require(_text(key) and _text(row["type"]) and
                             (row.get("status") is None or _text(row["status"])) and
                             (row.get("enabled") is None or type(row["enabled"]) is bool), "unknown_backend_schema")
                    backends.append(BackendObservation(key, row["type"], row.get("status"), row.get("enabled")))
            _require(len(backends) <= MAX_BACKENDS, "backend_collection_limit")
            # Names/states are provider-reported metadata, not weights/identity or
            # runtime qualification. No global activity or profile is inferred.
            state = DiscoveryState.UNSUPPORTED if not _IMAGE_FIELDS <= fields else (
                DiscoveryState.UNKNOWN if not backends else DiscoveryState.READY if any(
                    row.enabled is True and row.reported_state in ("running", "idle") for row in backends)
                else DiscoveryState.UNAVAILABLE)
            return DiscoveryReport(self.binding.owner, self.binding.connection_ref, state, observed,
                self.limits.valid_for, fields, tuple(ModelObservation(name) for name in names), tuple(backends), tuple(evidence))
        except BoundaryError as error:
            return DiscoveryReport(self.binding.owner, self.binding.connection_ref, DiscoveryState.HELD,
                observed, self.limits.valid_for, evidence=tuple(evidence), failure_code="invalid_response_" + error.code)
        except DiscoveryError as error:
            return DiscoveryReport(self.binding.owner, self.binding.connection_ref, DiscoveryState.HELD,
                observed, self.limits.valid_for, evidence=tuple(evidence), failure_code=error.code)
