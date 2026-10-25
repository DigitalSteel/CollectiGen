"""Pure, read-only Servers metadata projection; no collection or storage."""
from datetime import datetime
from suite_core.discovery.ollama_readonly import DiscoveryReport, ReadRoute, ReadState
from suite_core.registry.status_view import ConfiguredConnection


def project_ollama_connection(connection, report, now):
    if type(connection) is not ConfiguredConnection:
        raise ValueError("explicit_configured_connection_required")
    if report is not None and (type(report) is not DiscoveryReport or report.connection != connection):
        raise ValueError("exact_connection_report_required")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("aware_time_required")
    result = {"backend_id": connection.backend_id.value,
              "connection_ref": connection.connection_ref.value,
              "host_ref": connection.host_id.value, "connection_state": "Not observed",
              "ready": None, "busy": None, "version": None, "observed_at": None,
              "installed_count": None, "loaded_count": None,
              "scope": "Configured Ollama metadata connection; physical resource binding unknown."}
    if report is None:
        return result
    version = report.observation(ReadRoute.VERSION)
    freshness = version.freshness_at(now, report.limits).value
    result["observed_at"] = version.observed_at.isoformat() if version.observed_at else None
    current = freshness == "fresh" and version.state is ReadState.AVAILABLE
    result["connection_state"] = ("Connected" if current else
                                  freshness.capitalize() if freshness in ("stale", "future") else
                                  version.state.value.capitalize())
    result["version"] = version.version if current else None
    for route, key in ((ReadRoute.INSTALLED, "installed_count"), (ReadRoute.LOADED, "loaded_count")):
        observation = report.observation(route)
        if observation.state is ReadState.AVAILABLE and observation.freshness_at(now, report.limits).value == "fresh":
            result[key] = len(observation.models) if observation.models is not None else None
    return result


def section_command_keys(section):
    """Supported contextual commands; section navigation stays in the sidebar."""
    return {"Servers": ("refresh-local",),
            "Applications": ("inspect-candidate",)}.get(section, ())


def project_unmatched_jobs(report, jobs=()):
    """Retain observations outside the report's authorized host bindings."""
    from suite_core.inventory_view import InventoryReport
    from suite_core.registry.job_view import JobRow
    if type(report) is not InventoryReport:
        raise ValueError("existing_inventory_report_required")
    if type(jobs) is not tuple or len(jobs) > 128 or any(type(row) is not JobRow for row in jobs):
        raise ValueError("existing_bounded_job_rows_required")
    data = report.to_mapping()
    bindings = {(connection["backend_id"], connection["connection_ref"])
                for host in data["hosts"] for connection in host["connections"]}
    return tuple(row for row in (job.as_ui() for job in jobs)
                 if row["requesting_app"] != data["owner"]
                 or (row["backend_id"], row["connection_ref"]) not in bindings)
