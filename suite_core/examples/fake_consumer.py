"""Disposable in-process proof. No network, inference, files, or durable queue.

Run from the candidate/repository root: python -m suite_core.examples.fake_consumer
Not a production provider or cross-process resource manager.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

from suite_core.backend_contracts import (
    AdmissionHold, ApplicationId, ApprovedRoute, BackendId, BackendInstance,
    BackendState, CancellationOutcome, CancellationRequest, CapabilityId,
    CapabilitySupport, ConnectionId, CorrelationId, FeatureId, FrozenSettings,
    HostId, JobId, JobState, JobStatus, LeaseId, ModelProfile, ModelProfileId,
    OperationId, PrivatePayloadRef, ProviderCancellation, ResourceGroup,
    ResourceGroupId, ResourceLease, ResourceRequirements, ResourceTelemetry,
    ResultReceipt, StableId, TaskId, TaskRequest, backend_matches_request,
)

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def fixture(app="collectigen", name="one", operation="text", group="gpu-0", host="fake-host"):
    """Synthetic approved profiles; no installed model or endpoint assumptions."""
    owner = ApplicationId(app)
    cap = CapabilityId(operation)
    profile = ModelProfile(ModelProfileId(operation), "synthetic",
                           "fake-" + operation, "revision-1",
                           FrozenSettings.from_mapping({"quantization": "fake"}))
    backend = BackendInstance(BackendId(app + "-" + operation), HostId(host),
        "fake", "1", ConnectionId(app + "-connection"),
        (CapabilitySupport(cap, frozenset({"fake-json"}), True, 4096, 512,
                           True, ProviderCancellation.QUEUE_ONLY),),
        (profile,), frozenset({ResourceGroupId(group)}), BackendState.READY)
    # Canonical immutable bytes remain in the application/provider context.
    # The manager only sees an opaque reference plus its integrity fingerprint.
    payload = FrozenSettings.from_mapping(
        {"model": profile.model_reference, "messages": [{"role": "user", "content": "synthetic"}], "stream": False}
        if operation == "text" else
        {"model": profile.model_reference, "prompt": "synthetic", "width": 768,
         "height": 768, "steps": 30, "cfgscale": 4.5, "seed": -1,
         "sampler": "euler", "scheduler": "normal"})
    options = FrozenSettings.from_mapping({"temperature": 0.4, "num_predict": 128}
        if operation == "text" else {"seed": -1, "steps": 30, "cfgscale": 4.5})
    request = TaskRequest(TaskId(app + "-" + name), owner, cap, profile, options,
        hashlib.sha256(b"synthetic-scope").hexdigest(), ResourceRequirements(100, 100),
        OperationId(name), CorrelationId(name), FeatureId(operation),
        PrivatePayloadRef(owner, StableId(name + "-payload"), payload.fingerprint, "application/json"),
        (ApprovedRoute(owner, FeatureId(operation), backend.id, backend.connection_ref,
                       cap, profile.fingerprint, StableId("synthetic-local"), StableId("fake-approval")),),
        NOW, NOW + timedelta(minutes=5), "fake-json", requires_structured_output=operation == "text",
        context_tokens=1024 if operation == "text" else None, output_tokens=128 if operation == "text" else None)
    return backend, request, payload


class FakeCoordinator:
    """Sequential fixture only; claims are not thread/process/distributed safe."""
    def __init__(self, groups):
        physical = {}
        for group in groups:
            if group.identity_verified:
                identity = (group.host_id, group.device_reference)
                if identity in physical and physical[identity] != group.id:
                    raise ValueError("Conflicting resource aliases for the same verified host/device")
                physical[identity] = group.id
        self.groups = {group.id: group for group in groups}
        self.claims = {}
        self.bindings = {}
        self.targets = {}
        # Synthetic authoritative drain evidence supplied by this fixture's provider.
        self.drained = {}
        self.released = set()
        self.telemetry = {group.id: ResourceTelemetry(group.id, NOW, timedelta(seconds=30),
            1000, 1000, 1000, 1000, 0, 0) for group in groups}

    def reserve(self, request, backend):
        groups = backend.resource_groups
        ready = backend_matches_request(backend, request) and backend.state == BackendState.READY and bool(groups)
        ready = ready and all(group in self.groups and self.groups[group].identity_verified
            and self.groups[group].host_id == backend.host_id and group in self.telemetry
            and self.telemetry[group].verified_idle_at(NOW)
            and self.telemetry[group].verifies_headroom(request.resources, NOW) for group in groups)
        if not ready or any(lease.groups & groups for lease in self.claims.values()):
            return AdmissionHold(request.fingerprint, "unknown-stale-incompatible-or-busy", groups)
        lease = ResourceLease(LeaseId("lease-" + request.id.value), request.id, request.owner_application,
                              request.fingerprint, groups, NOW, request.deadline_at)
        self.claims[lease.id] = lease
        self.targets[lease.id] = (backend.id, backend.connection_ref, request.operation_id, request.correlation_id)
        return lease

    def bind_job(self, lease, status):
        if self.claims.get(lease.id) != lease or not lease.matches_status(status):
            return False
        if self.targets[lease.id] != (status.backend_id, status.connection_ref, status.operation_id, status.correlation_id):
            return False
        if lease.id in self.bindings:
            return self.bindings[lease.id] == status
        self.bindings[lease.id] = status
        return True

    def release(self, lease, terminal_status=None):
        if lease in self.released:
            return True
        if self.claims.get(lease.id) != lease:
            return False
        bound = self.bindings.get(lease.id)
        if bound is not None:
            if terminal_status is None or not terminal_status.terminal:
                return False
            if self.drained.get(terminal_status.id) != terminal_status:
                return False
            if not lease.matches_status(terminal_status) or (
                bound.id, bound.backend_id, bound.connection_ref, bound.operation_id, bound.correlation_id
            ) != (terminal_status.id, terminal_status.backend_id, terminal_status.connection_ref,
                  terminal_status.operation_id, terminal_status.correlation_id):
                return False
        del self.claims[lease.id]
        self.released.add(lease)
        return True


class FakeProvider:
    def __init__(self, backend, coordinator, owner, private_payloads, approved_routes):
        self.backend, self.coordinator, self.owner = backend, coordinator, owner
        self.private_payloads = private_payloads
        self.approved_routes = frozenset(approved_routes)
        self.private_outputs = {}
        self.jobs, self.requests, self.leases, self.receipts = {}, {}, {}, {}

    def submit(self, request, lease):
        if request.owner_application != self.owner or not backend_matches_request(self.backend, request):
            raise ValueError("Unapproved or incompatible request")
        if not any(route in self.approved_routes and route.backend_id == self.backend.id
                   and route.connection_ref == self.backend.connection_ref for route in request.approved_routes):
            raise ValueError("Approval assertion absent from app-owned approvals")
        if not lease.valid_for(request, NOW) or not lease.covers_backend(self.backend):
            raise ValueError("Invalid lease")
        payload = self.private_payloads[request.payload.reference]
        if payload.fingerprint != request.payload.fingerprint:
            raise ValueError("Changed private payload")
        # No implicit replay: caller queries the existing receipt after ambiguity.
        if any(existing.id == request.id for existing in self.requests.values()):
            raise ValueError("Duplicate task; query existing job")
        status = JobStatus(JobId("job-" + request.id.value), request.id, request.owner_application,
            self.backend.id, self.backend.connection_ref, JobState.QUEUED, NOW, "queued",
            request.fingerprint, request.operation_id, request.correlation_id)
        if not self.coordinator.bind_job(lease, status):
            raise ValueError("Lease already bound or foreign")
        self.jobs[status.id], self.requests[status.id], self.leases[status.id] = status, request, lease
        return status

    def status(self, job_id):
        return self.jobs[job_id]

    def start(self, job_id):
        if self.jobs[job_id].state != JobState.QUEUED:
            raise ValueError("Only queued work can start")
        self.jobs[job_id] = replace(self.jobs[job_id], state=JobState.RUNNING, phase="running")

    def cancel(self, request):
        status = self.status(request.job_id)
        if request.owner_application != self.owner or not request.matches(status):
            raise ValueError("Foreign or changed cancellation")
        if status.state != JobState.QUEUED:
            return CancellationOutcome(status, False, False, ProviderCancellation.QUEUE_ONLY)
        status = replace(status, state=JobState.CANCELLED, phase="cancelled")
        self.jobs[status.id] = status
        self.coordinator.drained[status.id] = status
        assert self.coordinator.release(self.leases[status.id], status)
        return CancellationOutcome(status, True, True, ProviderCancellation.QUEUE_ONLY)

    def complete(self, job_id):
        if self.jobs[job_id].state != JobState.RUNNING:
            raise ValueError("Only running work can complete")
        request = self.requests[job_id]
        status = replace(self.jobs[job_id], state=JobState.SUCCEEDED, phase="complete")
        raw_bytes = b'{"response":"synthetic raw output","done":true}'
        raw = PrivatePayloadRef(self.owner, StableId(job_id.value + "-raw"),
                                hashlib.sha256(raw_bytes).hexdigest(), "application/json")
        self.private_outputs[raw.reference] = raw_bytes
        self.jobs[job_id] = status
        self.coordinator.drained[job_id] = status
        self.receipts[job_id] = ResultReceipt(status, request.model_profile.fingerprint, raw,
            (raw,) if request.capability.value == "image" else (), "stop",
            request.model_profile.model_reference, request.model_profile.revision, request.options)
        assert self.coordinator.release(self.leases[job_id], status)

    def result(self, job_id):
        return self.receipts[job_id]

    def fail(self, job_id, drained=False):
        """Known technical failure does not by itself prove execution stopped."""
        if self.jobs[job_id].terminal:
            raise ValueError("Job already terminal")
        status = replace(self.jobs[job_id], state=JobState.FAILED, phase="failed")
        self.jobs[job_id] = status
        if drained:
            self.coordinator.drained[job_id] = status
        return self.coordinator.release(self.leases[job_id], status)


def cancellation(status):
    return CancellationRequest(status.id, status.task_id, status.owner_application,
                               status.backend_id, status.request_fingerprint, status.operation_id)


def app_accepts(receipt, request, current_scope_fingerprint):
    """App-owned stale guard only; semantic/schema acceptance remains separate."""
    return (current_scope_fingerprint == request.scope_fingerprint
            and receipt.status.state == JobState.SUCCEEDED
            and receipt.matches_request(request)
            and receipt.effective_options is not None
            and receipt.actual_model_reference is not None
            and receipt.actual_model_revision is not None)


def run():
    backend, request, payload = fixture()
    coordinator = FakeCoordinator((ResourceGroup(ResourceGroupId("gpu-0"), HostId("fake-host"),
                                                 StableId("synthetic-device"), True),))
    provider = FakeProvider(backend, coordinator, request.owner_application,
                            {request.payload.reference: payload}, request.approved_routes)
    lease = coordinator.reserve(request, backend)
    status = provider.submit(request, lease)
    provider.start(status.id)
    assert not provider.cancel(cancellation(status)).acknowledged
    provider.complete(status.id)
    assert provider.result(status.id).matches_request(request)
    assert not coordinator.claims
    return "Synthetic completion matches exact request; running queue-only cancellation refused; resource released."


if __name__ == "__main__":
    print(run())
