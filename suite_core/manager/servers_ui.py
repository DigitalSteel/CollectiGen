"""Compact read-only Servers presentation over existing observations and actions."""
from core.manager_server_inventory import project_ollama_connection, project_unmatched_jobs
from .resource_presentation_candidate import project_local_resources
from .resource_dashboard import server_views
from time import monotonic


def create_servers_panel(report, jobs=(), *, route=None, now, synthetic=False,
                         configure=None, metadata=None, open_metadata=None):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QProgressBar, QToolButton
    from .ui_cards import ResponsiveCardGrid, card_style, compact_action, disclosure_panel, workspace_layout, workspace_heading

    def label(text, parent):
        item = QLabel(text); item.setWordWrap(True); item.setTextFormat(Qt.PlainText)
        parent.addWidget(item); return item

    class ServersPanel(QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("serversInventory")
            body = workspace_layout(self); body.addWidget(workspace_heading("Servers"))
            label("Check this computer and your backend connections.", body)
            if synthetic:
                label("SYNTHETIC DEMO — supplied fabricated observations; local measurement is disabled.", body)
            controls = QHBoxLayout()
            self.refresh = compact_action(QPushButton("Refresh this computer"))
            self.cancel = compact_action(QPushButton("Cancel local refresh"))
            self.refresh.setAccessibleName("Refresh local resources on this computer")
            self.refresh.clicked.connect(self.request); self.cancel.clicked.connect(self.stop)
            controls.addWidget(self.refresh); controls.addWidget(self.cancel); controls.addStretch()
            body.addLayout(controls)
            self.status = label("", body)
            from qt_app.components import IndeterminateActivityBar
            self.activity = IndeterminateActivityBar(self)
            self.activity.setAccessibleName('Local resource refresh progress')
            body.addWidget(self.activity)
            self.elapsed = label('', body)
            self._measurement_started = None
            self.grid = ResponsiveCardGrid(minimum_card_width=330, maximum_columns=3)
            body.addWidget(self.grid); body.addStretch()
            self._signature = None; self.update_values()

        def request(self):
            if route is not None and not synthetic:
                route.request()
            self.update_values()

        def stop(self):
            if route is not None and not synthetic:
                route.cancel()
            self.update_values()

        def card(self, identity, title, status):
            card = QFrame(); card.setObjectName("visualCard"); card_style(card, compact=True)
            card.setProperty("serverIdentity", identity); card.setAccessibleName(title)
            pane = QVBoxLayout(card); pane.setContentsMargins(14, 14, 14, 14); pane.setSpacing(8)
            heading = label(title, pane); heading.setObjectName("cardTitle")
            badge = label(status, pane); badge.setObjectName("statusBadge")
            return card, pane

        def render_metric(self, pane, name, value):
            label(name + ": " + value["text"], pane)
            if value["percent"] is not None:
                bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(value["percent"])
                bar.setTextVisible(False); bar.setFixedHeight(8)
                bar.setAccessibleName(name + ": " + value["text"]); pane.addWidget(bar)

        def details(self, pane, identity, lines, opened):
            target = QWidget(); contents = QVBoxLayout(target); contents.setContentsMargins(0, 0, 0, 0)
            for text in lines:
                if len(text) > 1500:
                    from PySide6.QtWidgets import QTextBrowser
                    evidence = QTextBrowser(); evidence.setPlainText(text)
                    evidence.setMinimumHeight(min(220, evidence.fontMetrics().lineSpacing() * 12
                        + 2 * evidence.frameWidth() + 2 * int(evidence.document().documentMargin())))
                    evidence.setMaximumHeight(220); contents.addWidget(evidence)
                else:
                    label(text, contents)
            disclosure = disclosure_panel("Details", target)
            toggle = disclosure.findChild(QToolButton); toggle.setAccessibleName("Details for " + identity)
            toggle.setChecked(opened); pane.addStretch(); pane.addWidget(disclosure)

        def update_values(self):
            local = () if route is None or synthetic else project_local_resources(route, now())
            hosts = server_views(report, jobs)
            unmatched = project_unmatched_jobs(report, jobs)
            scope, observation = metadata() if callable(metadata) and not synthetic else (None, None)
            accepted = observation if scope is not None and scope.accepts(observation) else None
            backend = project_ollama_connection(scope.connection, accepted, now()) if scope is not None else None
            can_request = route is not None and not synthetic and route.can_request and not route.executor.close_held
            pending = route is not None and not synthetic and route.pending and not route.closed
            cancellable = pending and route.state != 'canceled'
            self.refresh.setEnabled(can_request); self.cancel.setEnabled(cancellable)
            self.cancel.setVisible(cancellable)
            if pending and self._measurement_started is None:
                self._measurement_started = monotonic()
            self.activity.set_active(pending, 'Refreshing this computer')
            self.activity.setVisible(pending)
            self.elapsed.setVisible(pending)
            if pending:
                seconds = max(0, int(monotonic() - self._measurement_started))
                self.elapsed.setText(f'{seconds // 60}m {seconds % 60:02d}s elapsed. Waiting for the original refresh to finish.')
            else:
                self._measurement_started = None
            message = ("Local measurement unavailable for this view." if route is None or synthetic else
                       f"{route.state.capitalize()}: {route.reason}".replace("leaving Dashboard", "leaving Servers"))
            if route is not None and not synthetic and route.reason == 'Refresh local resources to measure this host.':
                message = 'Not measured. Choose Refresh this computer to check CPU, RAM and GPU use.'
            self.status.setText(message)
            signature = (local, hosts, unmatched, backend, callable(configure), callable(open_metadata))
            if signature == self._signature:
                return
            self._signature = signature
            expanded = {card.property("serverIdentity"): any(t.isChecked() for t in card.findChildren(QToolButton))
                        for card in self.grid.cards}
            cards = []
            if not synthetic:
                card, pane = self.card("local-machine", "This computer", "Not measured" if not local else "Recorded measurements")
                if not local:
                    label("Choose Refresh this computer to check CPU, RAM and GPU use.", pane)
                for row in local:
                    label(row.label + " — " + row.state, pane)
                    metrics = (("CPU use", row.utilization),) if row.kind == "cpu" else (
                        (("VRAM", row.memory), ("GPU use", row.utilization)) if row.kind == "gpu" else (("RAM", row.memory),))
                    for name, metric in metrics:
                        self.render_metric(pane, name, metric)
                lines = ["Scope: this computer. Each physical GPU and host RAM reading is shown once.",
                         "Temperature unavailable; this provider does not report it."]
                lines += [f"{row.label}: {row.state}\nObserved: {row.observed_at}\nSource: {row.source}\n{row.detail}" for row in local]
                self.details(pane, "this computer", lines, expanded.get("local-machine", False)); cards.append(card)
            for host in hosts:
                identity = "supplied-host:" + host["host_id"]
                card, pane = self.card(identity, "Supplied host " + host["host_id"], "Detached observations; no physical link to this computer is inferred.")
                label(f"{len(host['connections'])} configured connections; readiness and current activity unknown.", pane)
                for connection in host["connections"]:
                    label(f"Backend {connection['backend_id']} / connection {connection['connection_ref']}: "
                          f"{connection['metadata_state'] or 'not observed'}; {connection['freshness']}", pane)
                lines = ["Physical resource groups are referenced once; observations are not summed into capacity."]
                lines += [f"Group {row['group_id']}: {row['freshness']} / identity "
                          f"{'verified' if row['identity_verified'] else 'unverified'}\n"
                          f"Observed: {row['observed_at'] or 'unknown'}; source: {row['source_ref'] or 'unknown'}"
                          for row in host["resources"]]
                lines += ["Hardware metrics remain in their supplied scope; no alias merge or combined memory total."]
                lines += [f"Job: {row['job_id'] or 'unknown identity'} — {row['current_status']} / {row['freshness']}" for row in host["jobs"]]
                lines += ["Model observations: " + str(len(host["models"])) + "; exact model and estimate evidence retained below."]
                import json
                lines.append(json.dumps({"connections": host["connections"], "resources": host["resources"],
                                         "jobs": host["jobs"], "models": host["models"]}, ensure_ascii=False, indent=2))
                self.details(pane, identity, lines, expanded.get(identity, False)); cards.append(card)
            for index, job in enumerate(unmatched):
                identity = "unmatched-job:" + str(index)
                title = "Held / unmatched job " + (job.get("job_id") or job.get("task_id") or "unknown identity")
                card, pane = self.card(identity, title, "No authorized host binding; not current server work.")
                label(f"Requesting app: {job['requesting_app']}\n"
                      f"State: {job.get('current_status') or 'unknown'}; retained: {job.get('last_observed_status') or 'unknown'}\n"
                      f"Observed: {job.get('observed_at') or 'unknown'}\n"
                      f"Source: {job.get('source_ref') or 'not supplied'}; freshness: {job.get('freshness') or 'unknown'}", pane)
                import json
                self.details(pane, title, ["No machine or physical resource assignment is inferred.",
                    json.dumps(job, ensure_ascii=False, indent=2)], expanded.get(identity, False))
                cards.append(card)
            card, pane = self.card("ollama-connection", "Local Ollama metadata", "Not connected" if backend is None else backend["connection_state"])
            if backend is None:
                label("Connect your existing Ollama to check model information.", pane)
            else:
                for key, title in (("installed_count", "Installed models"), ("loaded_count", "Loaded models")):
                    label(title + ": " + ("unknown" if backend[key] is None else str(backend[key])), pane)
            actions = QHBoxLayout()
            configure_button = compact_action(QPushButton("Connect existing Ollama"))
            configure_button.setEnabled(callable(configure) and not synthetic)
            configure_button.setToolTip("Opens the existing local Ollama connection editor; Save does not refresh metadata." if callable(configure) and not synthetic else "Local Ollama configuration unavailable for this view.")
            if callable(configure): configure_button.clicked.connect(configure)
            actions.addWidget(configure_button)
            inspect = compact_action(QPushButton("View Ollama in Applications")); inspect.setEnabled(callable(open_metadata) and not synthetic)
            if callable(open_metadata): inspect.clicked.connect(open_metadata)
            # Separate rows keep long action labels readable in a narrow card.
            if not configure_button.isEnabled(): configure_button.hide()
            if not inspect.isEnabled(): inspect.hide()
            pane.addStretch(); pane.addLayout(actions); pane.addWidget(inspect, 0, Qt.AlignLeft)
            lines = ["Configuration is metadata-only. Save and Refresh are separate decisions.",
                     "Loaded model memory is not physical VRAM capacity; a loaded model is not proof of busy or ready."]
            if backend is not None:
                lines += [f"Backend: {backend['backend_id']}\nConnection: {backend['connection_ref']}\nConfigured host reference: {backend['host_ref']}",
                          f"Version: {backend['version'] or 'unknown'}\nObserved: {backend['observed_at'] or 'unknown'}", backend["scope"]]
            self.details(pane, "local Ollama metadata", lines, expanded.get("ollama-connection", False)); cards.append(card)
            self.grid.set_cards(cards)

    return ServersPanel()
