"""Synthetic observations only: no provider submission or queue execution."""
from dataclasses import replace
from datetime import timedelta
import json

from suite_core.backend_contracts import (
    AdmissionHold, ApplicationId, CancellationOutcome, JobId, JobState, JobStatus,
    ProviderCancellation, ResourceGroup, ResourceTelemetry, StableId,
)
from suite_core.examples.fake_consumer import NOW, fixture
from suite_core.registry.job_view import (
    AvailabilityFact, ExplanationKind, JobViewSource, ProviderControlEvidence, project_queue,
)
from suite_core.registry.status_view import ResourceSnapshot

TTL = timedelta(seconds=30)


def inputs(app="everday", name="one", state=JobState.QUEUED):
    backend, request, _ = fixture(app, name, operation="image")
    request = replace(request, priority=10)
    status = JobStatus(JobId("synthetic-job-"+request.id.value), request.id, request.owner_application,
        backend.id, backend.connection_ref, state, NOW, state.value, request.fingerprint,
        request.operation_id, request.correlation_id)
    control = ProviderControlEvidence(backend, StableId("synthetic-control"), NOW, TTL,
                                      StableId("synthetic-behavior-qualification"))
    return backend, JobViewSource(request, status, status, StableId("synthetic-status"), TTL, control)


def resource_fact(source, backend, kind=ExplanationKind.RESOURCE_IN_USE):
    group = ResourceGroup(next(iter(backend.resource_groups)), backend.host_id, StableId("synthetic-device"), True)
    resource = ResourceSnapshot(ResourceTelemetry(group.id, NOW, TTL,
        6*1024**3, 12*1024**3, None, None, 1, 0), StableId("synthetic-telemetry"))
    return AvailabilityFact(kind, source.submitted, StableId("synthetic-reason"), NOW, TTL,
        backend=backend, resource_group=group, resource=resource,
        hold=AdmissionHold(source.request.fingerprint, "private explanation never projected", backend.resource_groups))


def run():
    backend, first = inputs()
    first = replace(first, status=replace(first.status, completed_units=3, total_units=12),
                    facts=(resource_fact(first, backend),))
    _, second = inputs("collectigen", "two", JobState.CANCELLED)
    second = replace(second, cancellation=CancellationOutcome(second.status, True, True, ProviderCancellation.QUEUE_ONLY),
                     cancellation_source_ref=StableId("synthetic-cancel-result"))
    return {"synthetic_only": True, "display_order": "supplied_input_order_not_scheduling_order",
            "authorized_apps": ["everday", "collectigen"],
            "rows": [row.as_ui() for row in project_queue((first, second),
                     frozenset({ApplicationId("everday"), ApplicationId("collectigen")}), now=NOW)]}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
