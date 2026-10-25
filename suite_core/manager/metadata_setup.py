"""Concrete Setup/Save/Refresh seam using original settings and accepted provider."""
from dataclasses import dataclass
import hashlib
import json

from core.backend_connection_settings import BackendConnectionSettings, BackendSettingsError
from suite_core.backend_contracts import ApplicationId, BackendId, ConnectionId, HostId, StableId
from suite_core.discovery.ollama_readonly import LocalOllamaTransport, ReadOnlyOllamaClient
from suite_core.discovery.swarm_readonly import EndpointBinding, ReadLimits
from suite_core.registry.status_view import ConfiguredConnection
from suite_core.shared_backend_routing_candidate import ExistingBackend, RoutingError
from suite_core.manager.metadata_binding import bind_existing_metadata


@dataclass(frozen=True, slots=True)
class SetupStatus:
    state: str
    next_action: str
    refresh_available: bool


class MetadataSetup:
    """No AppState/settings defaults; explicit actions fully specify the host.

    IDs are supplied by the existing Manager identity owner. The UI offers an
    endpoint and independent network decision, not backend-ID editing controls.
    """
    def __init__(self, settings_path, *, owner: ApplicationId, backend_id: BackendId,
                 connection_ref: ConnectionId, host_id: HostId,
                 transport_factory=LocalOllamaTransport, limits=ReadLimits()):
        self.settings = BackendConnectionSettings(settings_path)
        self.connection = ConfiguredConnection(owner, backend_id, connection_ref, host_id)
        self.transport_factory, self.limits = transport_factory, limits
        self._observed = None
        self._registration = None
        self._configured_value = None

    def open_setup(self):
        observed = self.settings.observe()  # Explicit Open Setup/Reload only.
        if observed.backend_value != self._configured_value:
            self._registration = None
        self._observed = observed
        self._configured_value = self._observed.backend_value
        return self.status

    @property
    def status(self):
        selection = None if self._observed is None else self._observed.selection
        if selection is None:
            return SetupStatus("not_configured", "Save connection", False)
        return SetupStatus("configured", "Refresh metadata" if selection.endpoint_network_allowed
            else "Review endpoint permission", selection.endpoint_network_allowed)

    def save_connection(self, endpoint, *, endpoint_network_allowed):
        if self._observed is None: raise BackendSettingsError("open_setup_before_save")
        saved = self.settings.save_connection(endpoint,
            endpoint_network_allowed=endpoint_network_allowed, expected=self._observed)
        if saved.backend_value != self._configured_value:
            self._registration = None
        self._observed = saved
        self._configured_value = saved.backend_value
        return self.status

    def cancel_setup(self):
        return self.status  # Drafts live in UI; Cancel makes no persistence call.

    def _current_registrations(self):
        observed = self.settings.observe()
        if observed.backend_value != self._configured_value: return ()
        return () if self._registration is None else (self._registration,)

    def _endpoint_policy(self):
        observed = self.settings.observe()
        return (observed.backend_value == self._configured_value
                and observed.selection is not None and observed.selection.endpoint_network_allowed)

    def selected_binding(self):
        try:
            return self._selected_binding()
        except (RoutingError, BackendSettingsError):
            raise
        except Exception:
            raise RoutingError("metadata_setup_binding_failed") from None

    def _selected_binding(self):
        if self._observed is None or self._observed.selection is None:
            return bind_existing_metadata()  # Honest missing state; no default endpoint.
        if self._registration is None:
            selection = self._observed.selection
            approved = hashlib.sha256(json.dumps(self._configured_value, sort_keys=True).encode()).hexdigest()
            binding = EndpointBinding(self.connection.owner, self.connection.connection_ref,
                StableId("settings." + approved), selection.endpoint)
            transport = self.transport_factory(binding, self.limits)  # Native inert constructor.
            client = ReadOnlyOllamaClient(self.connection, transport)
            self._registration = ExistingBackend(self.connection, client)
        return bind_existing_metadata(self._registration, configured_snapshot=self._current_registrations,
                                      endpoint_policy=self._endpoint_policy)

    def refresh_metadata(self, ticket, *, current_pending, cancelled):
        """Existing Refresh worker calls this; no implicit refresh after Save."""
        binding = self.selected_binding()
        if self._registration is None: raise RoutingError("metadata_configuration_missing")
        return binding.work(ticket, current_pending=current_pending, cancelled=cancelled)

    def consume_metadata(self, completion, *, current_pending, cancelled):
        binding = self.selected_binding()
        if self._registration is None: raise RoutingError("metadata_configuration_missing")
        return binding.consume(completion, current_pending=current_pending, cancelled=cancelled)


def create_collectigen_metadata_setup(*, settings_path=None, transport_factory=LocalOllamaTransport):
    """Use CollectiGen's existing settings path and Manager identity, without I/O."""
    if settings_path is None:
        from core.paths import SETTINGS_PATH
        settings_path = SETTINGS_PATH
    return MetadataSetup(settings_path, owner=ApplicationId("suite-manager"),
        backend_id=BackendId("ollama"), connection_ref=ConnectionId("manager.ollama"),
        host_id=HostId("local"), transport_factory=transport_factory)
