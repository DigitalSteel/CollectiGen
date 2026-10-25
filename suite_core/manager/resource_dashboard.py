"""Presentation of existing detached inventory/job projections; no collectors."""
from suite_core.inventory_view import InventoryReport
from suite_core.registry.job_view import JobRow


def memory_metric(free, total, freshness):
    if freshness != "fresh":
        return {"text": f"Unknown current usage ({freshness} observation)", "percent": None}
    if type(free) is not int or type(total) is not int or total <= 0 or not 0 <= free <= total:
        return {"text": "Unknown — complete free/total measurement not supplied", "percent": None}
    used = total - free
    gib = 1024 ** 3
    return {"text": f"{used / gib:.1f} GiB used / {total / gib:.1f} GiB total · {free / gib:.1f} GiB observed free",
        "percent": round(used * 100 / total)}


def server_views(report, jobs=()):
    if type(report) is not InventoryReport:
        raise ValueError("existing_inventory_report_required")
    if type(jobs) is not tuple or len(jobs) > 128 or any(type(job) is not JobRow for job in jobs):
        raise ValueError("existing_bounded_job_rows_required")
    data = report.to_mapping(); job_data = [job.as_ui() for job in jobs]
    result = []
    for host in data["hosts"]:
        connections = {(row["backend_id"], row["connection_ref"]) for row in host["connections"]}
        bound_jobs = [row for row in job_data if (row["backend_id"], row["connection_ref"]) in connections
            and row["requesting_app"] == data["owner"]]
        resources = []
        # Existing HostStatus/compose_inventory emits each physical group once;
        # connection rows reference it. Never sum channels/groups into capacity.
        for row in host["resources"]:
            resources.append({**row,
                "ram": memory_metric(row["free_ram_bytes"], row["total_ram_bytes"], row["freshness"]),
                "vram": memory_metric(row["free_vram_bytes"], row["total_vram_bytes"], row["freshness"])})
        models = [row for row in data["models"] if (row["backend_id"], row["connection_ref"]) in connections]
        result.append({"host_id": host["host_id"], "resources": resources, "jobs": bound_jobs,
            "models": models, "connections": host["connections"], "viewed_at": data["viewed_at"]})
    return tuple(result)


def create_dashboard(report, jobs=(), *, synthetic=False, open_servers=None,
                     supported_apps=(), development_apps=(), reader_available=False):
    """Concise supplied observations; no collectors, zero telemetry or launch claims."""
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame
    from PySide6.QtCore import Qt
    from .ui_cards import card_style, workspace_layout, workspace_heading, ResponsiveCardGrid, details_panel
    hosts = server_views(report, jobs)
    from core.manager_server_inventory import project_unmatched_jobs
    unmatched = project_unmatched_jobs(report, jobs)
    scroll = QScrollArea(); scroll.setWidgetResizable(True)
    body = QWidget(); layout = workspace_layout(body); scroll.setWidget(body)
    layout.addWidget(workspace_heading("Dashboard"))
    intro = QLabel("Choose an application, or check your computer and connections in Servers.")
    intro.setWordWrap(True); layout.addWidget(intro)
    if synthetic:
        layout.addWidget(QLabel("Synthetic preview — fabricated observations"))
    grid = ResponsiveCardGrid(minimum_card_width=300, maximum_columns=2)
    cards = []
    def card(title, value, detail):
        frame = QFrame(); frame.setObjectName("visualCard"); card_style(frame, compact=True)
        pane = QVBoxLayout(frame); pane.setContentsMargins(14, 14, 14, 14); pane.setSpacing(8)
        heading = QLabel(title); heading.setObjectName("cardTitle"); pane.addWidget(heading)
        summary = QLabel(value); summary.setWordWrap(True); summary.setTextFormat(Qt.PlainText)
        pane.addWidget(summary); pane.addStretch()
        pane.addWidget(details_panel("Details", detail)); cards.append(frame)
        return summary
    app_lines = [item.title + " · Catalog entry" for item in supported_apps]
    supplied_keys = {item.component_id.value for item in supported_apps}
    if reader_available and "collectigen.tag-encyclopedia" not in supplied_keys:
        app_lines.append("Tag Encyclopedia Reader · Browse in Applications")
    app_lines += [item.title + " · In development" for item in development_apps if item.key not in supplied_keys]
    scroll.applications_summary = card("Applications", "\n".join(app_lines) or "No application catalog supplied",
         "Catalog entries describe applications. They do not prove installation, running state or readiness. "
         "Use Applications to filter the supplied catalog and inspect an individual entry.")
    connections = {(row["backend_id"], row["connection_ref"]) for host in hosts for row in host["connections"]}
    scroll.fleet_summary = card("Connections", f"{len(connections)} recorded connections" if connections else "No connection observations",
         f"{len(hosts)} supplied host records. Readiness and activity remain unknown without current scoped evidence. "
         "Connection configuration and metadata refresh are separate actions in Servers.")
    scroll.backend_summary = QLabel("Local Ollama: connection unknown")
    scroll.backend_summary.setWordWrap(True)
    scroll.fleet_summary.parentWidget().layout().insertWidget(2, scroll.backend_summary)
    scroll.local_summary = card("This computer", "Not measured",
         "Refresh this computer in Servers to request measurements. Nothing is sampled on navigation. "
         "Expired or unavailable readings remain unknown; memory observations do not guarantee model fit.")
    card("Jobs", f"{len(jobs)} supplied job observations" if jobs else "No job observations",
         f"{len(unmatched)} held or unmatched observations retained in Servers. "
         "Supplied job records are not assumed to be current work or assigned to an unverified host.")
    grid.set_cards(cards); layout.addWidget(grid); layout.addStretch()
    return scroll
