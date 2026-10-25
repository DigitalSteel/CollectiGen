"""Bounded supplied dependency observations; no discovery, setup or I/O."""
from dataclasses import dataclass
from enum import Enum


class DependencyState(str, Enum):
    UNKNOWN = 'unknown'
    READY = 'ready'
    MISSING = 'missing'
    BLOCKED = 'blocked'
    BUSY = 'busy'
    RESTART = 'restart-required'


class DependencyRoute(str, Enum):
    OWNED_RUNTIME = 'owned-runtime'
    BACKEND_CONNECTION = 'backend-connection'
    READER_CONNECTION = 'reader-connection'


def _text(value, name, maximum, *, empty=False):
    if type(value) is not str or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('Bounded dependency ' + name + ' required')


@dataclass(frozen=True)
class DependencyObservation:
    key: str
    component: str
    state: DependencyState = DependencyState.UNKNOWN
    detail: str = ''
    blocking_reason: str = ''
    observed_at: str = ''
    version: str = ''
    route: DependencyRoute | None = None
    route_available: bool = False

    def __post_init__(self):
        for name, limit in (('key', 128), ('component', 256), ('detail', 2048),
                            ('blocking_reason', 1024), ('observed_at', 128), ('version', 96)):
            _text(getattr(self, name), name, limit, empty=name not in {'key', 'component'})
        if type(self.state) is not DependencyState or (self.route is not None and type(self.route) is not DependencyRoute):
            raise ValueError('Explicit dependency state and supported route required')
        if type(self.route_available) is not bool or self.route_available and self.route is None:
            raise ValueError('Explicit existing route availability required')
        if self.state in {DependencyState.MISSING, DependencyState.BLOCKED, DependencyState.RESTART} and not self.blocking_reason.strip():
            raise ValueError('Visible dependency blocking reason required')


@dataclass(frozen=True)
class DependencyStatusSnapshot:
    revision: str
    observations: tuple[DependencyObservation, ...] = ()

    def __post_init__(self):
        _text(self.revision, 'revision', 128)
        if type(self.observations) is not tuple or len(self.observations) > 32 or any(
                type(row) is not DependencyObservation for row in self.observations):
            raise ValueError('Bounded exact dependency observations required')
        if len({row.key for row in self.observations}) != len(self.observations):
            raise ValueError('Unique dependency keys required')


@dataclass(frozen=True)
class DependencyPresentation:
    status: str
    detail: str
    action_text: str
    action_enabled: bool
    disabled_reason: str


def project_dependency(row):
    if type(row) is not DependencyObservation:
        raise ValueError('Supplied dependency observation required')
    status = {DependencyState.UNKNOWN: 'Unknown', DependencyState.READY: 'Observed ready',
              DependencyState.MISSING: 'Missing', DependencyState.BLOCKED: 'Needs attention',
              DependencyState.BUSY: 'Operation in progress', DependencyState.RESTART: 'Restart required'}[row.state]
    action = {DependencyRoute.OWNED_RUNTIME: 'Review owned status',
              DependencyRoute.BACKEND_CONNECTION: 'Review connection',
              DependencyRoute.READER_CONNECTION: 'Review reader setup'}.get(row.route, 'Review setup')
    enabled = row.route_available and row.state is not DependencyState.BUSY
    reason = ('Wait for the retained operation to finish before opening another setup action.'
              if row.state is DependencyState.BUSY else
              '' if enabled else 'No supported setup workspace is connected for this component.')
    details = [row.detail.strip(), row.blocking_reason.strip()]
    if row.version:
        details.append('Reported version: ' + row.version)
    details.append('Observation: ' + row.observed_at if row.observed_at else 'No dated observation supplied; current readiness is unknown.')
    return DependencyPresentation(status, '\n'.join(x for x in details if x), action, enabled, reason)
