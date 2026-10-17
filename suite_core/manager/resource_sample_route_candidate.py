"""Inactive candidate: one host-owned sample Future, polled by the UI owner."""
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Event, get_ident
from time import monotonic
from uuid import uuid4
import math

from suite_core.backend_contracts import ApplicationId, HostId, ResourceGroup, ResourceTelemetry
from suite_core.inventory_view import DetectionState, HardwareObservation
from suite_core.local_resources import LocalResourceSnapshot, ResourceReading


@dataclass(frozen=True)
class SampleTicket:
    route_id: str
    sequence: int
    generation: int


@dataclass(frozen=True)
class SampleCompletion:
    ticket: SampleTicket
    snapshot: LocalResourceSnapshot | None
    error: str | None = None


def _sample(sampler, ticket, cancelled, ui_owner):
    if get_ident() == ui_owner:
        return SampleCompletion(ticket, None, "Background sampling unavailable: executor ran on the UI owner.")
    if cancelled.is_set():
        return SampleCompletion(ticket, None)
    try:
        snapshot = sampler()
        return SampleCompletion(ticket, None if cancelled.is_set() else snapshot)
    except Exception:
        return SampleCompletion(ticket, None, "Local measurement failed. Refresh to try again.")


def _valid_snapshot(snapshot, owner, host):
    if (type(snapshot) is not LocalResourceSnapshot or type(snapshot.readings) is not tuple
            or not 1 <= len(snapshot.readings) <= 18 or snapshot.polling_enabled is not False):
        return False
    groups = set()
    physical_devices = set()
    observations = set()
    for reading in snapshot.readings:
        if type(reading) is not ResourceReading or type(reading.hardware) is not HardwareObservation:
            return False
        if (type(reading.reason) is not str or not 1 <= len(reading.reason) <= 160
                or any(ord(c) < 32 or ord(c) == 127 for c in reading.reason)):
            return False
        hardware = reading.hardware
        if hardware.owner != owner or hardware.host_id != host or hardware.observation_ref in observations:
            return False
        observations.add(hardware.observation_ref)
        if reading.utilization_percent is not None and (type(reading.utilization_percent) not in (int, float)
                or not math.isfinite(reading.utilization_percent) or not 0 <= reading.utilization_percent <= 100):
            return False
        for value in (reading.used_memory_bytes, reading.unaccounted_memory_bytes):
            if value is not None and (type(value) is not int or value < 0 or value > 2**63 - 1):
                return False
        if hardware.kind == "gpu" and (hardware.state is DetectionState.OBSERVED
                or reading.group is not None or reading.telemetry is not None
                or reading.utilization_percent is not None or reading.used_memory_bytes is not None
                or reading.unaccounted_memory_bytes is not None):
            if type(reading.group) is not ResourceGroup or not reading.group.identity_verified:
                return False
        if reading.group is not None:
            group = reading.group
            if (type(group) is not ResourceGroup or group.host_id != host or group.id in groups
                    or hardware.resource_group != group.id):
                return False
            groups.add(group.id)
            if group.device_reference is not None:
                physical = (group.host_id, group.device_reference)
                if physical in physical_devices:
                    return False
                physical_devices.add(physical)
        elif hardware.resource_group is not None:
            return False
        if reading.telemetry is not None and (type(reading.telemetry) is not ResourceTelemetry
                or reading.group is None or reading.telemetry.group_id != reading.group.id
                or reading.telemetry.observed_at != hardware.observed_at
                or reading.telemetry.valid_for != hardware.valid_for):
            return False
        total = hardware.total_memory_bytes
        free = None
        if reading.telemetry is not None:
            telemetry_total = (reading.telemetry.total_vram_bytes if hardware.kind == "gpu"
                               else reading.telemetry.total_ram_bytes)
            free = (reading.telemetry.free_vram_bytes if hardware.kind == "gpu"
                    else reading.telemetry.free_ram_bytes)
            if telemetry_total != total:
                return False
        if total is not None:
            parts = (free, reading.used_memory_bytes, reading.unaccounted_memory_bytes)
            known = tuple(part for part in parts if part is not None)
            if sum(known) > total:
                return False
            if all(part is not None for part in parts):
                if sum(known) != total:
                    return False
    return True


class ResourceSampleRoute:
    """Host retains executor lifetime. UI never waits, joins, or shuts it down.

    Cancellation/timeout/navigation revoke admission immediately, but the Future
    must acknowledge completion before retry or close. No automatic sampling.
    """
    def __init__(self, *, owner, host, sampler=None, executor=None, clock=monotonic, timeout_seconds=4):
        if (type(owner) is not ApplicationId or type(host) is not HostId
                or sampler is not None and not callable(sampler)
                or executor is not None and not callable(getattr(executor, "submit", None))
                or not callable(clock) or type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5):
            raise ValueError("explicit_bounded_host_binding_required")
        self.owner, self.host = owner, host
        self.sampler, self.executor, self.clock = sampler, executor, clock
        self.timeout = timeout_seconds
        self.route_id = uuid4().hex
        self._ui_owner = get_ident()
        self._sequence = self._generation = 0
        self._future = self._ticket = self._cancelled = self._started = None
        self._last_mono = None
        self._unconfirmed = False
        self._revoked = False
        self._binding = None
        self.visible = False
        self.closed = False
        self.snapshot = None
        self.state = "disabled" if sampler is None or executor is None else "unknown"
        self.reason = "Local measurements are disabled. Supply an approved host sampler and background executor."

    def _owner(self):
        if get_ident() != self._ui_owner:
            raise RuntimeError("Resource result admission belongs to the UI owner")

    def _now(self):
        value = self.clock()
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or self._last_mono is not None and value < self._last_mono):
            raise ValueError("invalid_resource_clock")
        self._last_mono = value
        return value

    @property
    def pending(self):
        return self._future is not None

    @property
    def close_held(self):
        return self.pending or self._unconfirmed

    @property
    def can_request(self):
        return (self.visible and not self.closed and not self.close_held
                and self.sampler is not None and self.executor is not None)

    def set_visible(self, visible):
        self._owner()
        if type(visible) is not bool:
            raise ValueError("explicit_visibility_required")
        if self.visible and not visible and self.pending:
            self.cancel("Refresh canceled after leaving Dashboard; wait for completion before retry.")
        self.visible = visible
        if self.can_request and self.state == "unknown":
            self.reason = "Refresh local resources to measure this host."

    def bind(self, binding):
        self._owner()
        if self._binding is not None:
            raise ValueError("resource_route_already_bound")
        self._binding = binding

    def request(self):
        self._owner()
        if not self.can_request:
            return False
        try:
            started = self._now()
        except Exception:
            self.state, self.reason = "error", "Sampling clock unavailable. No sample started."
            return False
        self._sequence += 1
        self._ticket = SampleTicket(self.route_id, self._sequence, self._generation)
        self._cancelled = Event()
        self._revoked = False
        self.snapshot = None
        self._started = started
        try:
            future = self.executor.submit(_sample, self.sampler, self._ticket, self._cancelled, self._ui_owner)
        except Exception:
            self._unconfirmed = True
            future = None
        if not isinstance(future, Future):
            self._cancelled.set()
            self._unconfirmed = True
            self.state = "error"
            self.reason = "Sampling completion is unconfirmed. Host supervision is required before retry or close."
            return False
        self._future = future
        self.state = "pending"
        self.reason = "Measuring local resources in the background."
        return True

    def cancel(self, reason="Refresh canceled; wait for sample completion before retry."):
        self._owner()
        self._generation += 1
        self._revoked = True
        self.snapshot = None
        if self._cancelled is not None:
            self._cancelled.set()
        if self._future is not None:
            self._future.cancel()  # only queued work; never interrupts a running process/thread
        self.state, self.reason = "canceled", reason

    def begin_close(self):
        self._owner()
        if not self.closed:
            self.cancel("Local sampling stopped for close. Reopen Manager to refresh again.")
            self.closed = True

    def poll(self):
        self._owner()
        if not self.pending:
            return False
        try:
            now = self._now()
        except Exception:
            self.cancel("Sampling clock failed. Result discarded; await completion.")
            self.state = "error"
        else:
            if not self._revoked and now - self._started >= self.timeout:
                self.cancel("Local measurement timed out. Refresh after sample completion to retry.")
                self.state = "timeout"
        future = self._future
        if not future.done():
            return False
        self._future = None
        if self._revoked or self.closed or not self.visible:
            return False
        try:
            completion = future.result()
        except Exception:
            completion = None
        if (type(completion) is not SampleCompletion or completion.ticket != self._ticket
                or completion.ticket.generation != self._generation):
            self.state, self.reason = "error", "Invalid measurement completion. Refresh to try again."
            return False
        if completion.error is not None or not _valid_snapshot(completion.snapshot, self.owner, self.host):
            self.state, self.reason = "error", "Local measurement unavailable. Refresh to try again."
            return False
        self.snapshot = completion.snapshot
        self.state, self.reason = "received", "Local measurement received. Values expire independently."
        return True
