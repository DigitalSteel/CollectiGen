"""Inactive capability resolver over host-owned existing metadata clients.

No endpoint/settings/credentials store, readiness assertion or execution routing.
The trusted embedding host supplies task scope and a bounded configured snapshot.
"""
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from typing import Callable

from suite_core.backend_contracts import ApplicationId, BackendId, CapabilityId
from suite_core.discovery.ollama_readonly import ReadOnlyOllamaClient
from suite_core.discovery.ollama_readonly import DiscoveryReport as OllamaReport, ReadRoute as OllamaRoute
from suite_core.discovery.swarm_readonly import (
    DiscoveryError, EndpointBinding, ReadLimits, ReadOnlySwarmClient,
    DiscoveryReport as SwarmReport,
)
from suite_core.adapters.swarm_translation import BoundaryError
from suite_core.registry.status_view import ConfiguredConnection, MAX_CONNECTIONS
from suite_core.manager.async_lifecycle_bridge import WorkTicket


MODULE_ID = "collectigen.shared-backend-routing"
MODULE_VERSION = "0.1.0-alpha.1"


METADATA = CapabilityId("backend.metadata")


class RoutingError(ValueError):
    """Static codes only: never embed config, exception text or credentials."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise RoutingError(code)


@contextmanager
def _private_errors(code):
    """Include property/validation access in the fixed-code error boundary."""
    try:
        yield
    except (RoutingError, DiscoveryError, BoundaryError):
        raise
    except Exception:
        raise RoutingError(code) from None


@dataclass(frozen=True, slots=True)
class ExistingBackend:
    """Private host registration; same client and binding, no endpoint copying.

    ConfiguredConnection.capabilities describes provider declarations; it never
    enables inference here. Only the metadata method implemented below is routed.
    """
    connection: ConfiguredConnection
    client: object = field(repr=False)
    _binding: EndpointBinding = field(init=False, repr=False)
    _limits: ReadLimits = field(init=False, repr=False)

    def __post_init__(self):
        with _private_errors("metadata_client_failed"):
            self._initialize()

    def _initialize(self):
        _require(type(self.connection) is ConfiguredConnection, "configured_connection_required")
        _require(type(self.client) in (ReadOnlyOllamaClient, ReadOnlySwarmClient), "unsupported_backend")
        binding = (self.client._binding if type(self.client) is ReadOnlyOllamaClient
                   else self.client.binding)
        limits = (self.client._limits if type(self.client) is ReadOnlyOllamaClient
                  else self.client.limits)
        _require(type(binding) is EndpointBinding and type(limits) is ReadLimits, "invalid_client_binding")
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_limits", limits)
        self.validate()

    def validate(self):
        with _private_errors("metadata_client_failed"):
            self._validate()

    def _validate(self):
        _require(self.connection.owner == self._binding.owner
                 and self.connection.connection_ref == self._binding.connection_ref,
                 "connection_binding_mismatch")
        if type(self.client) is ReadOnlyOllamaClient:
            _require(self.client.connection == self.connection, "connection_binding_mismatch")
            binding, limits = self.client.transport.binding, self.client.transport.limits
        else:
            binding, limits = self.client.binding, self.client.limits
        _require(binding == self._binding and limits == self._limits, "transport_binding_changed")

    def validate_report(self, report):
        self.validate()
        if type(self.client) is ReadOnlyOllamaClient:
            _require(type(report) is OllamaReport and report.connection == self.connection
                     and report.binding == self._binding and report.limits == self._limits,
                     "metadata_scope_mismatch")
        else:
            _require(type(report) is SwarmReport and report.owner == self.connection.owner
                     and report.connection_ref == self.connection.connection_ref
                     and report.valid_for == self._limits.valid_for
                     and all(item.connection_ref == self.connection.connection_ref for item in report.evidence),
                     "metadata_scope_mismatch")


@dataclass(frozen=True, slots=True)
class _SwarmOperation:
    """Immutable per-operation receiver for the existing native Swarm methods.

    A host client may be rebound concurrently. Every native read uses this
    captured scope, so no later read can contact the new endpoint. Pre/post guards
    reject host-scope changes; final acceptance also checks the native evidence.
    No HTTP implementation or credential acquisition is duplicated here.
    """
    _registration: ExistingBackend = field(repr=False)
    _binding: EndpointBinding = field(repr=False)
    _limits: ReadLimits = field(repr=False)
    _session: str = field(repr=False)

    @property
    def binding(self): return self._binding

    @property
    def limits(self): return self._limits

    def validate(self):
        self._registration.validate()
        _require(self._registration.client._session == self._session, "transport_binding_changed")

    def read(self, route):
        self.validate()
        evidence = ReadOnlySwarmClient.read(self, route)
        self.validate()
        _require(evidence.connection_ref == self._binding.connection_ref, "metadata_scope_mismatch")
        return evidence


@dataclass(frozen=True, slots=True)
class ResolvedMetadata:
    """Task-local handle; reports stay provider-native and app-private."""
    backend_id: BackendId
    capability: CapabilityId
    _registration: ExistingBackend = field(repr=False)
    _session: str | None = field(default=None, repr=False)

    def discover(self, *, previous=None):
        with _private_errors("metadata_client_failed"):
            self._registration.validate()
            client = self._registration.client
            _require(previous is None or type(client) is ReadOnlyOllamaClient,
                     "previous_report_unsupported")
            if type(client) is ReadOnlyOllamaClient:
                report = client.discover(previous=previous)
            else:
                operation = _SwarmOperation(self._registration, self._registration._binding,
                                            self._registration._limits, self._session)
                operation.validate()
                report = ReadOnlySwarmClient.discover(operation)
                operation.validate()
            self._registration.validate_report(report)
            return report


class SharedBackendResolver:
    """Use a trusted host's existing configured clients, with exact selection.

    Snapshot callbacks must return registrations without contacting endpoints.
    Owner is the app whose existing connection authority is being reused. No
    cross-app permission is inferred from sharing a backend ID or endpoint.
    """
    def __init__(self, configured_snapshot: Callable[[], tuple[ExistingBackend, ...]]):
        _require(callable(configured_snapshot), "configured_snapshot_required")
        self._configured_snapshot = configured_snapshot

    def resolve(self, owner: ApplicationId, backend_id: BackendId,
                capability: CapabilityId, *, endpoint_network_allowed: bool) -> ResolvedMetadata:
        _require(type(endpoint_network_allowed) is bool, "explicit_endpoint_network_policy_required")
        _require(endpoint_network_allowed, "endpoint_network_refused")
        _require(type(owner) is ApplicationId and type(backend_id) is BackendId
                 and type(capability) is CapabilityId, "typed_route_required")
        _require(capability == METADATA, "unsupported_capability")
        with _private_errors("configured_snapshot_unavailable"):
            registrations = self._configured_snapshot()
        _require(type(registrations) is tuple and len(registrations) <= MAX_CONNECTIONS
                 and all(type(item) is ExistingBackend for item in registrations),
                 "invalid_configured_snapshot")
        identities = [(item.connection.owner, item.connection.backend_id) for item in registrations]
        references = [(item.connection.owner, item.connection.connection_ref) for item in registrations]
        _require(len(set(identities)) == len(identities) and len(set(references)) == len(references),
                 "ambiguous_configured_backend")
        match = next((item for item in registrations
                      if item.connection.owner == owner and item.connection.backend_id == backend_id), None)
        _require(match is not None, "backend_not_configured")
        match.validate()
        with _private_errors("metadata_client_failed"):
            session = match.client._session if type(match.client) is ReadOnlySwarmClient else None
            return ResolvedMetadata(backend_id, capability, match, session)


@dataclass(frozen=True, slots=True)
class PendingMetadataOperation:
    """Host-owned pending ticket plus its independently intended metadata route."""
    ticket: WorkTicket = field(repr=False)
    owner: ApplicationId
    backend_id: BackendId

    def __post_init__(self):
        _require(type(self.ticket) is WorkTicket and type(self.owner) is ApplicationId
                 and type(self.backend_id) is BackendId, "pending_metadata_operation_required")


@dataclass(frozen=True, slots=True)
class MetadataCompletion:
    """Private worker output; publication requires the separate admission step."""
    _ticket: WorkTicket = field(repr=False)
    _handle: ResolvedMetadata = field(repr=False)
    _report: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class RouteSummary:
    route: str
    state: str
    model_count: int | None
    observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MetadataSummary:
    """Allowlisted publication: no endpoints, sessions, names, bytes or errors."""
    backend_id: BackendId
    request_id: int
    generation: int
    routes: tuple[RouteSummary, ...]


def _current(ticket, current_pending, cancelled, owner, backend_id):
    _require(type(ticket) is WorkTicket and ticket.operation in ("ollama_refresh", "metadata_refresh"),
             "metadata_ticket_required")
    _require(type(ticket.request_id) is int and ticket.request_id > 0
             and type(ticket.generation) is int and ticket.generation >= 0, "metadata_ticket_required")
    active = current_pending()
    stopped = cancelled()
    _require(type(stopped) is bool, "explicit_cancellation_state_required")
    _require(not stopped, "metadata_cancelled")
    _require(type(active) is PendingMetadataOperation and active.ticket == ticket, "stale_metadata_ticket")
    _require(active.owner == owner and active.backend_id == backend_id, "metadata_request_route_mismatch")


def _current_configuration(resolver, handle, endpoint_network_allowed):
    old = handle._registration
    current = resolver.resolve(old.connection.owner, handle.backend_id, METADATA,
                               endpoint_network_allowed=endpoint_network_allowed)
    new = current._registration
    _require(new.connection == old.connection and new.client is old.client
             and new._binding == old._binding and new._limits == old._limits
             and current._session == handle._session,
             "configured_backend_changed")


def run_metadata_operation(resolver, ticket, owner, backend_id, *,
                           endpoint_network_allowed, current_pending, cancelled):
    """One existing worker operation; no threads, queue or scheduler created.

    Resolve fresh on the worker, keep native evidence private and discard work
    when its ticket/configuration changes. The host supplies its existing pending
    ticket, independently intended route and cancellation state. The library's
    cloud-storage flag is irrelevant.
    """
    with _private_errors("metadata_worker_failed"):
        _current(ticket, current_pending, cancelled, owner, backend_id)
        handle = resolver.resolve(owner, backend_id, METADATA,
                                  endpoint_network_allowed=endpoint_network_allowed)
        _require(ticket.operation != "ollama_refresh"
                 or type(handle._registration.client) is ReadOnlyOllamaClient,
                 "metadata_ticket_backend_mismatch")
        _current(ticket, current_pending, cancelled, owner, backend_id)
        report = handle.discover(previous=ticket.inventory_previous)
        _current_configuration(resolver, handle, endpoint_network_allowed)
        _current(ticket, current_pending, cancelled, owner, backend_id)
        return MetadataCompletion(ticket, handle, report)


def admit_metadata_completion(resolver, completion, *, endpoint_network_allowed,
                              current_pending, cancelled):
    """Owning host calls under its existing admission lock, then retires ticket.

    Recheck exact generation/request/bridge/operation and configuration at actual
    consumption, not just worker return. Native evidence remains private in the
    completion; only an allowlisted counts/status summary may cross into display.
    """
    with _private_errors("metadata_worker_failed"):
        _require(type(completion) is MetadataCompletion, "metadata_completion_required")
        ticket, handle, report = completion._ticket, completion._handle, completion._report
        _current(ticket, current_pending, cancelled, handle._registration.connection.owner, handle.backend_id)
        _current_configuration(resolver, handle, endpoint_network_allowed)
        handle._registration.validate_report(report)
        if type(report) is OllamaReport:
            rows = tuple(RouteSummary(route.value, report.observation(route).state.value,
                None if report.observation(route).models is None else len(report.observation(route).models),
                report.observation(route).observed_at) for route in OllamaRoute)
        else:
            rows = (RouteSummary("swarm.metadata", report.state.value,
                None if report.failure_code is not None else len(report.models), report.observed_at),)
        _current(ticket, current_pending, cancelled, handle._registration.connection.owner, handle.backend_id)
        return MetadataSummary(handle.backend_id, ticket.request_id, ticket.generation, rows)
