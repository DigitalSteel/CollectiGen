"""Pure detached local resource projection plus an inactive Qt panel factory."""
from dataclasses import dataclass
from .resource_dashboard import memory_metric


@dataclass(frozen=True)
class ResourceDisplay:
    identity: str
    label: str
    kind: str
    source: str
    observed_at: str
    state: str
    detail: str
    utilization: dict
    memory: dict


def project_local_resources(route, now):
    rows = []
    for reading in (() if route.snapshot is None else route.snapshot.readings):
        hardware = reading.hardware
        values = reading.current_values(now)
        state = values["state"]
        utilization = values["utilization_percent"]
        cpu_free = values.get("available_cpu_time_percent")
        usage = {"percent": None if utilization is None else round(utilization),
                 "text": "Unknown utilization" if utilization is None else f"{utilization:.1f}% busy"}
        if cpu_free is not None:
            usage["text"] += f" / {cpu_free:.1f}% available CPU time"
        memory = memory_metric(values["available_memory_bytes"], values["total_memory_bytes"],
                               "fresh" if state == "measured" else state)
        used = values["used_memory_bytes"]
        total = values["total_memory_bytes"]
        free = values["available_memory_bytes"]
        if state == "measured":
            memory = {"percent": round(100 * used / total) if used is not None and total else None,
                      "text": ("Used memory unknown" if used is None else f"{used / 1024**3:.1f} GiB used")
                              + " / " + ("total unknown" if total is None else f"{total / 1024**3:.1f} GiB total")
                              + "; " + ("available memory unknown" if free is None else f"{free / 1024**3:.1f} GiB observed available")}
            remainder = values["unaccounted_memory_bytes"]
            if remainder:
                memory["text"] += f"; {remainder / 1024**3:.2f} GiB unaccounted (not free)"
        if state != "measured":
            usage = {"percent": None, "text": f"Unknown current utilization ({state})"}
        rows.append(ResourceDisplay(hardware.observation_ref.value, hardware.label, hardware.kind,
                                    hardware.source_ref.value, hardware.observed_at.isoformat(),
                                    state, {
                                        "measured": "Dated measurement; no allocation promise.",
                                        "system_metrics_unavailable": "CPU/RAM measurement unavailable; current values are unknown.",
                                        "cpu_interval_unavailable": "CPU counter interval unavailable; CPU use is unknown.",
                                        "nvidia_tool_missing": "NVIDIA measurement tool unavailable; GPU detection is unknown.",
                                        "gpu_timeout": "GPU measurement timed out; current values are unknown.",
                                        "gpu_query_failed": "GPU measurement error; current values are unknown.",
                                    }.get(reading.reason, "Measurement unavailable."), usage, memory))
    return tuple(rows)


def create_local_panel(route, *, now):
    """Uses existing Manager card spacing/actions; no sampling during construction."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QGridLayout, QProgressBar
    from .ui_cards import card_style, workspace_heading

    class LocalPanel(QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("localResourceCandidate")
            body = QVBoxLayout(self); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(18)
            body.addWidget(workspace_heading("Local host resources"))
            intro = QLabel("Measure CPU, RAM and GPU use on this host. Observed free memory does not guarantee model fit.")
            intro.setWordWrap(True); body.addWidget(intro)
            controls = QHBoxLayout()
            self.refresh = QPushButton("Refresh local resources")
            self.cancel = QPushButton("Cancel refresh")
            self.refresh.clicked.connect(self.request); self.cancel.clicked.connect(self.stop)
            controls.addWidget(self.refresh); controls.addWidget(self.cancel); controls.addStretch()
            body.addLayout(controls)
            self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
            body.addWidget(self.status)
            self.grid = QGridLayout(); self.grid.setSpacing(14); body.addLayout(self.grid)
            self._signature = None
            self.update_values()

        def request(self):
            route.request(); self.update_values()

        def stop(self):
            route.cancel(); self.update_values()

        def update_values(self):
            rows = project_local_resources(route, now())
            signature = (rows, route.state, route.reason, route.can_request, route.pending, route.closed)
            if signature == self._signature:
                return
            self._signature = signature
            self.refresh.setEnabled(route.can_request)
            self.cancel.setEnabled(route.pending and not route.closed)
            self.status.setText(f"{route.state.capitalize()}: {route.reason}")
            while self.grid.count():
                widget = self.grid.takeAt(0).widget()
                if widget is not None:
                    widget.hide(); widget.deleteLater()
            if not rows:
                empty = QLabel("CPU, RAM and GPU measurements: unknown. Use Refresh when enabled.")
                empty.setWordWrap(True); self.grid.addWidget(empty, 0, 0, 1, 3)
            for index, row in enumerate(rows):
                tile = QFrame(); tile.setObjectName("visualCard"); tile.setProperty("resourceIdentity", row.identity)
                card_style(tile)
                area = QVBoxLayout(tile); area.setContentsMargins(18, 16, 18, 16)
                label = QLabel(row.label); label.setWordWrap(True); label.setTextFormat(Qt.PlainText); area.addWidget(label)
                state = QLabel(f"{row.kind.upper()} / {row.state}"); area.addWidget(state)
                metrics = (row.utilization,) if row.kind == "cpu" else ((row.memory, row.utilization) if row.kind == "gpu" else (row.memory,))
                for metric in metrics:
                    caption = QLabel(metric["text"]); caption.setWordWrap(True); caption.setTextFormat(Qt.PlainText)
                    area.addWidget(caption)
                    bar = QProgressBar(); bar.setRange(0, 100); bar.setFixedHeight(8); bar.setTextVisible(False)
                    bar.setEnabled(metric["percent"] is not None); bar.setValue(metric["percent"] or 0); area.addWidget(bar)
                detail = QLabel(f"Observed: {row.observed_at}\nSource: {row.source}")
                detail.setWordWrap(True); detail.setTextFormat(Qt.PlainText); area.addWidget(detail)
                reason = QLabel(row.detail); reason.setWordWrap(True); reason.setTextFormat(Qt.PlainText); area.addWidget(reason)
                self.grid.addWidget(tile, index // 3, index % 3)

    return LocalPanel()
