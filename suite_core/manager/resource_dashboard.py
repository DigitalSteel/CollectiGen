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


def active_module_jobs(jobs, names, now):
    """Report only current, attributable work from existing job observations."""
    from datetime import datetime, timedelta

    states = {"queued": "Waiting", "starting": "Starting", "running": "In progress",
              "cancel_requested": "Stopping"}
    activity = []
    for job in jobs:
        row = job.as_ui()
        name = names.get(row["requesting_app"])
        state = states.get(row["current_status"])
        if not name or not state or not row["progress_is_current"]:
            continue
        observed = datetime.fromisoformat(row["observed_at"])
        if not timedelta(0) <= now - observed <= timedelta(seconds=row["valid_for_seconds"]):
            continue
        done, total = row["completed_units"], row["total_units"]
        count = f" ({done}/{total})" if done is not None and total else ""
        activity.append(f"{name}: {state}{count}")
    return tuple(activity)


def create_dashboard(*, synthetic=False, open_applications, review_update):
    """Show Suite status and link to the separate Applications workspace."""
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame, QPushButton
    from PySide6.QtCore import Qt
    from .ui_cards import card_style, workspace_layout, workspace_heading, compact_action

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    body = QWidget()
    layout = workspace_layout(body)
    scroll.setWidget(body)
    layout.addWidget(workspace_heading("Dashboard"))
    if synthetic:
        layout.addWidget(QLabel("Demo: example status only"))

    def status_card(title):
        card = QFrame()
        card.setObjectName("visualCard")
        card_style(card, compact=True)
        panel = QVBoxLayout(card)
        panel.setContentsMargins(14, 14, 14, 14)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        panel.addWidget(heading)
        status = QLabel()
        status.setWordWrap(True)
        status.setTextFormat(Qt.PlainText)
        panel.addWidget(status)
        layout.addWidget(card)
        return status

    scroll.module_activity = status_card("Active work")
    scroll.notifications = status_card("Notifications")
    scroll.notifications.setTextFormat(Qt.RichText)
    scroll.notifications.setTextInteractionFlags(Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
    scroll.notifications.linkActivated.connect(review_update)
    intro = QLabel("Choose an app in Applications to review an install or open it. Computer and connection status is in Servers.")
    intro.setWordWrap(True)
    layout.addWidget(intro)
    scroll.open_applications = compact_action(QPushButton("Go to Applications"))
    scroll.open_applications.setProperty("primary", True)
    scroll.open_applications.clicked.connect(open_applications)
    layout.addWidget(scroll.open_applications, 0, Qt.AlignLeft)
    layout.addStretch(1)
    return scroll
