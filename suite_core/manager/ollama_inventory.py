"""Presentation of accepted, independently dated Ollama observations."""
from dataclasses import dataclass,field
from suite_core.discovery.ollama_readonly import DiscoveryReport,ReadOnlyOllamaClient,ReadRoute
from suite_core.discovery.swarm_readonly import EndpointBinding,ReadLimits
from suite_core.registry.status_view import ConfiguredConnection

@dataclass(frozen=True)
class OllamaScope:
    connection: ConfiguredConnection
    binding: EndpointBinding=field(repr=False)
    limits: ReadLimits
    def __post_init__(self):
        if (type(self.connection) is not ConfiguredConnection or type(self.binding) is not EndpointBinding
            or type(self.limits) is not ReadLimits or self.connection.owner!=self.binding.owner
            or self.connection.connection_ref!=self.binding.connection_ref):raise ValueError('ollama_scope_required')
    def accepts(self,report):
        return (type(report) is DiscoveryReport and report.connection==self.connection
            and report.binding==self.binding and report.limits==self.limits)
    def client_matches(self,client):
        return (type(client) is ReadOnlyOllamaClient and client.connection==self.connection
            and client._binding==self.binding and client._limits==self.limits)

def route_text(report,route,now):
    observation=report.observation(route)
    freshness=observation.freshness_at(now,report.limits).value
    count=('unknown' if observation.models is None else str(len(observation.models)))
    return (f'{observation.state.value}; {freshness}; models {count}; '
        f'last success {observation.observed_at.isoformat() if observation.observed_at else "unknown"}; '
        f'last attempt {observation.attempted_at.isoformat()}; error {observation.failure_code or "none"}')

def summary(scope,report,now,*,pending=False,error=None):
    if scope is None:return 'Ollama connection: not supplied. Version, health and model inventories unknown.'
    connection=scope.connection
    text=f'Backend {connection.backend_id.value}; connection {connection.connection_ref.value}; host {connection.host_id.value}. '
    if report is None:text+='Version and metadata health unknown; installed and loaded inventories not observed.'
    else:
        version=report.observation(ReadRoute.VERSION)
        text+=f'Version {version.version or "unknown"}; metadata health {report.health_at(now)}. '
        text+=f'Version observation: {version.state.value}; {version.freshness_at(now,report.limits).value}; '
        text+=f'last success {version.observed_at.isoformat() if version.observed_at else "unknown"}; '
        text+=f'last attempt {version.attempted_at.isoformat()}; error {version.failure_code or "none"}.'
    if pending:text+=' Refresh pending; previous observations retain their original dates.'
    if error:text+=f' Refresh unavailable: {error}. Retained observations are not refreshed.'
    return text
