"""Inert host binding for the Manager's existing explicit Ollama Refresh.

The configuration owner supplies an already-owned registration and current
in-memory snapshot. No settings reader, endpoint default or session acquisition.
"""
from dataclasses import dataclass, field

from suite_core.discovery.ollama_readonly import ReadOnlyOllamaClient
from suite_core.manager.ollama_inventory import OllamaScope
from suite_core.shared_backend_routing_candidate import (
    ExistingBackend, MetadataCompletion, PendingMetadataOperation, RoutingError,
    SharedBackendResolver, admit_metadata_completion, run_metadata_operation,
)


@dataclass(frozen=True, slots=True)
class MissingMetadataConfiguration:
    code: str = "metadata_configuration_missing"
    setup_seam: str = ("The connection owner must supply a reviewed Ollama registration, "
        "its current in-memory snapshot and explicit endpoint policy. No saved Ollama "
        "connection reader is wired into the standalone Manager.")


@dataclass(frozen=True, slots=True)
class ManagerMetadataBinding:
    registration: ExistingBackend = field(repr=False)
    configured_snapshot: object = field(repr=False)
    endpoint_policy: object = field(repr=False)

    def __post_init__(self):
        if (type(self.registration) is not ExistingBackend
                or type(self.registration.client) is not ReadOnlyOllamaClient
                or not callable(self.configured_snapshot) or not callable(self.endpoint_policy)):
            raise RoutingError("manager_metadata_binding_required")

    @property
    def scope(self):
        registration = self.registration
        return OllamaScope(registration.connection, registration._binding, registration._limits)

    def pending(self, ticket):
        return PendingMetadataOperation(ticket, self.registration.connection.owner,
                                        self.registration.connection.backend_id)

    def _snapshot(self):
        registrations = self.configured_snapshot()
        if type(registrations) is tuple:
            for row in registrations:
                if (type(row) is ExistingBackend
                        and row.connection.owner == self.registration.connection.owner
                        and row.connection.backend_id == self.registration.connection.backend_id):
                    if (row.connection != self.registration.connection
                            or row.client is not self.registration.client
                            or row._binding != self.registration._binding
                            or row._limits != self.registration._limits):
                        raise RoutingError("manager_metadata_configuration_changed")
        return registrations

    def _invoke(self, operation, *args, **kwargs):
        try:
            policy = self.endpoint_policy()
            return operation(SharedBackendResolver(self._snapshot), *args,
                             endpoint_network_allowed=policy, **kwargs)
        except RoutingError:
            raise
        except Exception:
            raise RoutingError("manager_metadata_host_failed") from None

    def work(self, ticket, *, current_pending, cancelled):
        return self._invoke(run_metadata_operation, ticket,
            self.registration.connection.owner, self.registration.connection.backend_id,
            current_pending=current_pending, cancelled=cancelled)

    def consume(self, completion, *, current_pending, cancelled):
        if type(completion) is not MetadataCompletion:
            raise RoutingError("metadata_completion_required")
        summary = self._invoke(admit_metadata_completion, completion,
            current_pending=current_pending, cancelled=cancelled)
        if not self.scope.accepts(completion._report):
            raise RoutingError("metadata_scope_mismatch")
        return completion._report, summary


def bind_existing_metadata(registration=None, *, configured_snapshot=None, endpoint_policy=None):
    """No known registration is an honest missing result, never a default URL."""
    if registration is None:
        return MissingMetadataConfiguration()
    return ManagerMetadataBinding(registration, configured_snapshot, endpoint_policy)
