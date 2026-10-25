from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QStackedWidget,
    QPlainTextEdit, QTextEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.swarm_runtime import DEFAULT_ENDPOINT, SOURCE_UPDATE_BLOCK_REASON, SwarmInspection, SwarmRuntimeManager
from core.swarm_source_inventory import format_source_inventory
from .components import compact_button
from .workers import AsyncCallTask


class SwarmRuntimeWidget(QGroupBox):
    """One Setup surface for adoption, managed preparation and maintenance."""

    status_changed = Signal(object)
    return_requested = Signal()

    def __init__(self, settings, parent=None, *, manager=None):
        super().__init__("Local Generation (SwarmUI)", parent)
        self.settings = settings
        self.manager = manager or SwarmRuntimeManager()
        self.pool = QThreadPool.globalInstance()
        self._task = None
        self._startup_checks_remaining = 0
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._poll_startup)
        self._startup_notice = ""
        self._startup_target = None
        self._request_return_available = False
        self._logs_dialog = None
        self._installation_dialog = None
        self._model_paths_dialog = None
        self._onboarding_dialog = None
        self._configuration_backups_dialog = None
        self._companion_package_dialog = None
        self._companion_configured = False
        self._compatibility_dialog = None
        self._update_review_dialog = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose a task to connect, run, import from, or maintain local SwarmUI. "
            "Your models and outputs remain yours."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.state_label = QLabel()
        self.state_label.setObjectName("sectionTitle")
        self.state_detail = QLabel()
        self.state_detail.setWordWrap(True)
        self.state_detail.setObjectName("mutedLabel")
        layout.addWidget(self.state_label)
        layout.addWidget(self.state_detail)

        self.task_nav = QListWidget()
        self.task_nav.setAccessibleName("Local generation setup tasks")
        self.task_nav.setFixedWidth(190)
        self.task_nav.addItems(("Connect or install", "Run and status", "Models and import", "Maintenance"))
        self.task_stack = QStackedWidget()
        self.task_nav.currentRowChanged.connect(self.task_stack.setCurrentIndex)
        connect_panel = QWidget()
        connect_layout = QVBoxLayout(connect_panel)
        connect_note = QLabel(
            "Use an existing installation or prepare a managed one. Extensions, "
            "credentials, and custom files remain yours."
        )
        connect_note.setWordWrap(True)
        connect_layout.addWidget(connect_note)
        run_panel = QWidget()
        run_layout = QVBoxLayout(run_panel)
        models_panel = QWidget()
        models_layout = QVBoxLayout(models_panel)
        maintenance_panel = QWidget()
        maintenance_layout = QVBoxLayout(maintenance_panel)
        for panel in (connect_panel, run_panel, models_panel, maintenance_panel):
            self.task_stack.addWidget(panel)

        form = QGridLayout()
        self.install_root = QLineEdit()
        self.install_root.setPlaceholderText("Choose the existing SwarmUI folder")
        self.install_root.setAccessibleName("SwarmUI installation folder")
        browse = compact_button(QPushButton("Browse…"))
        browse.clicked.connect(self.choose_existing)
        self.endpoint = QLineEdit(DEFAULT_ENDPOINT)
        self.endpoint.setAccessibleName("SwarmUI loopback endpoint")
        self.endpoint.setToolTip("Only a local HTTP loopback address is accepted.")
        form.addWidget(QLabel("Installation folder"), 0, 0)
        form.addWidget(self.install_root, 0, 1)
        form.addWidget(browse, 0, 2)
        form.addWidget(QLabel("Local endpoint"), 1, 0)
        form.addWidget(self.endpoint, 1, 1, 1, 2)
        connect_layout.addLayout(form)

        first_actions = QHBoxLayout()
        self.use_existing_button = compact_button(QPushButton("Use Existing SwarmUI…"), primary=True)
        self.install_button = compact_button(QPushButton("Install SwarmUI for Me…"))
        self.use_existing_button.clicked.connect(self.review_existing)
        self.install_button.clicked.connect(self.review_managed_install)
        first_actions.addWidget(self.use_existing_button)
        first_actions.addWidget(self.install_button)
        first_actions.addStretch(1)
        connect_layout.addLayout(first_actions)

        manage_actions = QHBoxLayout()
        self.health_button = compact_button(QPushButton("Check Connection"))
        self.start_button = compact_button(QPushButton("Start SwarmUI"))
        self.open_button = compact_button(QPushButton("Open SwarmUI"))
        self.update_check_button = compact_button(QPushButton("Check for Updates"))
        self.update_button = compact_button(QPushButton("Review Update…"))
        self.disconnect_button = compact_button(QPushButton("Disconnect…"))
        self.stop_button = compact_button(QPushButton("Stop SwarmUI…"))
        self.restart_button = compact_button(QPushButton("Restart SwarmUI…"))
        self.logs_button = compact_button(QPushButton("View Local Logs…"))
        self.review_installation_button = compact_button(QPushButton("Review Installation…"))
        self.model_paths_button = compact_button(QPushButton("Model Folders…"))
        self.onboarding_button = compact_button(QPushButton("Import Existing Setup…"))
        self.onboarding_button.clicked.connect(self.review_onboarding)
        self.configuration_backups_button = compact_button(QPushButton("Configuration Backups…"))
        self.companion_package_button = compact_button(QPushButton("Companion Package…"))
        self.companion_package_button.setToolTip("Prepare a detached companion package or verify an existing one. Does not install or activate it.")
        self.model_paths_button.setToolTip("Review, change and restore only SwarmUI model folder settings.")
        self.review_installation_button.setToolTip("Review the saved SwarmUI installation without changing settings.")
        self.health_button.clicked.connect(self.refresh_health)
        self.start_button.clicked.connect(self.start_runtime)
        self.open_button.clicked.connect(self.open_runtime)
        self.update_check_button.clicked.connect(self.check_updates)
        self.update_button.clicked.connect(self.apply_update)
        self.disconnect_button.clicked.connect(self.disconnect)
        self.stop_button.clicked.connect(lambda: self.review_lifecycle("stop"))
        self.restart_button.clicked.connect(lambda: self.review_lifecycle("restart"))
        self.logs_button.clicked.connect(self.view_logs)
        self.review_installation_button.clicked.connect(self.review_installation)
        self.model_paths_button.clicked.connect(self.review_model_paths)
        self.configuration_backups_button.clicked.connect(self.review_configuration_backups)
        self.companion_package_button.clicked.connect(self.review_companion_package)
        installation_actions = QHBoxLayout()
        installation_actions.addWidget(self.review_installation_button)
        installation_actions.addStretch(1)
        connect_layout.addLayout(installation_actions)
        connect_layout.addStretch(1)

        for button in (self.health_button, self.start_button, self.open_button):
            manage_actions.addWidget(button)
        manage_actions.addStretch(1)
        run_layout.addLayout(manage_actions)
        lifecycle = QGroupBox("Runtime controls")
        lifecycle_actions = QHBoxLayout(lifecycle)
        for button in (self.stop_button, self.restart_button, self.logs_button):
            lifecycle_actions.addWidget(button)
        lifecycle_actions.addStretch(1)
        run_layout.addWidget(lifecycle)
        run_layout.addStretch(1)

        model_actions = QHBoxLayout()
        model_actions.addWidget(self.model_paths_button)
        model_actions.addStretch(1)
        models_layout.addLayout(model_actions)
        onboarding_actions = QHBoxLayout()
        onboarding_actions.addWidget(self.onboarding_button)
        onboarding_note = QLabel("Review models, favorites and appearance before importing.")
        onboarding_note.setWordWrap(True)
        onboarding_actions.addWidget(onboarding_note, 1)
        models_layout.addLayout(onboarding_actions)

        backups = QGroupBox("Backups and integration")
        backup_actions = QHBoxLayout(backups)
        for button in (self.configuration_backups_button, self.companion_package_button):
            backup_actions.addWidget(button)
        backup_actions.addStretch(1)
        maintenance_layout.addWidget(backups)
        updates = QGroupBox("Updates and enrollment")
        update_actions = QHBoxLayout(updates)
        for button in (self.update_check_button, self.update_button, self.disconnect_button):
            update_actions.addWidget(button)
        update_actions.addStretch(1)
        maintenance_layout.addWidget(updates)
        maintenance_layout.addStretch(1)

        self.model_note = QLabel()
        self.model_note.setWordWrap(True)
        self.model_note.setObjectName("mutedLabel")
        models_layout.addWidget(self.model_note)
        models_layout.addStretch(1)

        self.request_return_context = QWidget()
        request_return_layout = QHBoxLayout(self.request_return_context)
        request_return_layout.setContentsMargins(0, 0, 0, 0)
        self.request_return_note = QLabel(
            "Your generation plan is still open. Return when you are ready to check "
            "its connection and exact model again."
        )
        self.request_return_note.setWordWrap(True)
        self.request_return_note.setObjectName("mutedLabel")
        self.return_to_request_button = compact_button(
            QPushButton("Return to Generation Plan"), primary=True,
        )
        self.return_to_request_button.setAccessibleName("Return to Generation Plan")
        self.return_to_request_button.setAccessibleDescription(
            "Return to the saved generation plan. This does not check, start, bind, "
            "prepare, or submit anything."
        )
        self.return_to_request_button.clicked.connect(self._return_to_request)
        request_return_layout.addWidget(self.request_return_note, 1)
        request_return_layout.addWidget(self.return_to_request_button)
        layout.addWidget(self.request_return_context)
        self.request_return_context.setVisible(False)
        tasks = QHBoxLayout()
        tasks.addWidget(self.task_nav)
        tasks.addWidget(self.task_stack, 1)
        layout.addLayout(tasks, 1)
        snapshot = self.refresh_snapshot()
        self.select_task("run" if snapshot.configured else "connect")

    def select_task(self, task: str) -> None:
        """Reveal one task without changing the runtime or another task's draft."""
        index = {"connect": 0, "run": 1, "models": 2, "maintenance": 3}.get(task)
        if index is None:
            raise ValueError("Choose a Local Generation setup task.")
        self.task_nav.setCurrentRow(index)

    def select_default_task(self) -> None:
        self.select_task("run" if self._companion_configured else "connect")

    def set_request_return_available(self, available: bool, *, context: str = "generation") -> None:
        """Expose an explicit return only while one owning workflow is suspended."""
        if context not in {"generation", "model_import"}:
            raise ValueError("Choose a supported Setup return context.")
        if context == "model_import":
            self.request_return_note.setText(
                "Your model import is still open. Return to review the saved choices "
                "and read the SwarmUI source again before approving anything."
            )
            label="Return to Model Import"
            description=(
                "Return to the retained model import. This does not connect, read folders, "
                "register, move, or resume anything."
            )
        else:
            self.request_return_note.setText(
                "Your generation plan is still open. Return when you are ready to check "
                "its connection and exact model again."
            )
            label="Return to Generation Plan"
            description=(
                "Return to the saved generation plan. This does not check, start, bind, "
                "prepare, or submit anything."
            )
        self.return_to_request_button.setText(label)
        self.return_to_request_button.setAccessibleName(label)
        self.return_to_request_button.setAccessibleDescription(description)
        self._request_return_available = bool(available)
        self.request_return_context.setVisible(self._request_return_available)
        self.return_to_request_button.setEnabled(
            self._request_return_available
            and self._task is None
            and self._startup_checks_remaining <= 0
        )
        if available:
            self.select_task("connect" if not self._companion_configured
                else "models" if context == "model_import" else "run")

    def _return_to_request(self) -> None:
        """Return control to the caller without touching the runtime."""
        if self.return_to_request_button.isEnabled():
            self.return_requested.emit()

    def _set_busy(self, busy: bool, text: str = ""):
        for button in (
            self.use_existing_button, self.install_button, self.health_button,
            self.start_button, self.open_button, self.update_check_button, self.update_button,
            self.disconnect_button,
            self.stop_button, self.restart_button, self.logs_button,
            self.review_installation_button,
            self.model_paths_button,
            self.onboarding_button,
            self.configuration_backups_button,
            self.companion_package_button,
        ):
            button.setEnabled(not busy)
        self.return_to_request_button.setEnabled(
            self._request_return_available and not busy and self._startup_checks_remaining <= 0
        )
        if busy:
            self.state_label.setText(text)

    def _run(self, label: str, operation, completed):
        if self._task is not None and not self._task.is_finished():
            return
        self._set_busy(True, label)
        task = AsyncCallTask(operation)
        self._task = task
        task.signals.complete.connect(lambda result: self._finished(result, completed))
        task.signals.failed.connect(self._failed)
        self.pool.start(task)

    def _finished(self, result, completed):
        self._task = None
        try:
            completed(result)
        finally:
            self.refresh_snapshot()

    def _failed(self, message: str):
        self._task = None
        if self._startup_checks_remaining > 0:
            self._startup_checks_remaining = 0
            self._startup_timer.stop()
            self._startup_notice = "Startup checking stopped. Use Check Connection to retry; the launcher remains under observation."
        self.refresh_snapshot()
        QMessageBox.warning(self, "SwarmUI Needs Attention", message)

    def refresh_snapshot(self):
        snapshot = self.manager.snapshot()
        self._companion_configured = snapshot.configured
        waiting_for_start = (
            self._startup_checks_remaining > 0
            and snapshot.state not in {"ready", "busy", "update_available"}
        )
        headline = "Starting" if waiting_for_start else snapshot.headline
        self.state_label.setText(f"{headline} · {snapshot.endpoint}")
        facts = []
        if snapshot.runtime_version:
            facts.append(f"SwarmUI {snapshot.runtime_version}")
        if snapshot.backend_ids:
            label = "Backend ID" if len(snapshot.backend_ids) == 1 else "Backend IDs"
            facts.append(label + ": " + ", ".join(snapshot.backend_ids))
        if snapshot.last_health_check:
            facts.append("Last checked: " + snapshot.last_health_check)
        if snapshot.active_jobs:
            facts.append(f"Active/waiting work: {snapshot.active_jobs}")
        detail = snapshot.detail + (("\n" + " · ".join(facts)) if facts else "")
        if waiting_for_start:
            detail += "\nCollectiGen will keep checking while SwarmUI and its generation backend start."
        elif self._startup_notice and not snapshot.control_plane_online:
            detail += "\n" + self._startup_notice
        if snapshot.update_summary:
            detail += "\n" + snapshot.update_summary
        if snapshot.configured:
            detail += "\n" + (getattr(snapshot, "resource_summary", "") or "Memory observation unavailable.")
            observed = getattr(snapshot, "resource_checked_at", "")
            if observed:
                detail += "\nMemory checked " + observed + "; this does not estimate request capacity."
        if snapshot.control_plane_online and not snapshot.work_status_known:
            detail += "\nCheck Connection to verify server-wide work before stopping, restarting or updating."
        self.state_detail.setText(detail)
        self.state_detail.setAccessibleName(f"SwarmUI {snapshot.headline}. {detail}")
        if snapshot.configured:
            self.install_root.setText(snapshot.install_root)
            self.endpoint.setText(snapshot.endpoint)
        self.model_note.setText(
            "CollectiGen model library: "
            + str(getattr(self.settings, "ai_models_directory", "") or "not configured")
            + ". Linking model paths never downloads, moves, or duplicates a model."
        )
        for button in (self.health_button, self.update_check_button, self.disconnect_button):
            button.setEnabled(snapshot.configured and not waiting_for_start)
        if snapshot.lifecycle_pending:
            self.start_button.setText("Check Connection First")
            self.start_button.setEnabled(False)
        elif snapshot.state == "incompatible" or (
            snapshot.state == "needs_attention" and getattr(snapshot, "compatibility_state", "") == "unavailable"
        ):
            self.start_button.setText("Review Compatibility…" if snapshot.state == "incompatible" else "Review Connection Check…")
            self.start_button.setEnabled(not waiting_for_start)
        elif snapshot.control_plane_online and snapshot.state == "needs_attention":
            self.start_button.setText("Open SwarmUI Setup…")
            self.start_button.setEnabled(not waiting_for_start)
        elif snapshot.state in {"ready", "busy", "update_available"}:
            self.start_button.setText("SwarmUI Running")
            self.start_button.setEnabled(False)
        elif waiting_for_start or snapshot.state == "starting":
            self.start_button.setText("Starting SwarmUI…")
            self.start_button.setEnabled(False)
        else:
            self.start_button.setText("Start SwarmUI")
            self.start_button.setEnabled(snapshot.configured and snapshot.state == "offline")
        self.open_button.setEnabled(snapshot.configured and snapshot.control_plane_online)
        lifecycle_ready = (
            snapshot.configured and snapshot.control_plane_online
            and snapshot.work_status_known and not snapshot.active_jobs
            and not snapshot.lifecycle_pending and not waiting_for_start
            and snapshot.state != "starting"
        )
        self.stop_button.setEnabled(lifecycle_ready)
        self.restart_button.setEnabled(lifecycle_ready)
        self.update_button.setEnabled(snapshot.configured and snapshot.update_available and not waiting_for_start)
        self.update_button.setToolTip("Review the reported update and missing installation recovery. No update is sent.")
        for button in (self.stop_button, self.restart_button):
            button.setToolTip("Requires a fresh server-wide idle check. Active or unknown work blocks this action.")
        self.logs_button.setEnabled(snapshot.configured and snapshot.control_plane_online and not waiting_for_start)
        self.review_installation_button.setEnabled(snapshot.configured and not waiting_for_start)
        self.model_paths_button.setEnabled(snapshot.configured and not waiting_for_start)
        self.onboarding_button.setEnabled(snapshot.configured and not waiting_for_start)
        self.configuration_backups_button.setEnabled(not waiting_for_start)
        self.companion_package_button.setEnabled(not waiting_for_start)
        self.use_existing_button.setEnabled(True)
        self.install_button.setEnabled(not snapshot.configured)
        self.return_to_request_button.setEnabled(
            self._request_return_available and not waiting_for_start and self._task is None
        )
        if self._task is not None:
            self._set_busy(True)
        self.status_changed.emit(snapshot)
        return snapshot

    def choose_existing(self):
        selected = QFileDialog.getExistingDirectory(
            self, "Choose the SwarmUI installation folder", self.install_root.text()
        )
        if selected:
            self.install_root.setText(selected)

    def review_existing(self):
        root = self.install_root.text().strip()
        if not root:
            self.choose_existing()
            root = self.install_root.text().strip()
        if not root:
            return
        endpoint = self.endpoint.text().strip() or DEFAULT_ENDPOINT
        self._run(
            "Inspecting the selected SwarmUI…",
            lambda: self.manager.inspect_existing(root, endpoint),
            self._confirm_existing,
        )

    def _confirm_existing(self, inspection: SwarmInspection):
        if not inspection.recognized:
            QMessageBox.warning(self, "SwarmUI Not Recognized", inspection.issue)
            return
        source_state = {
            "clean": "No tracked changes",
            "modified": "Local changes detected; updates blocked",
        }.get(inspection.tracked_source_state, "Not verified; updates blocked")
        source_observations = format_source_inventory(inspection.source_inventory)
        source_summary = source_observations.splitlines()[0]
        details = (
            f"Folder: {inspection.install_root}\nEndpoint: {inspection.endpoint}\n"
            f"Source revision: {inspection.source_revision or 'not available'}\n"
            f"Source changes: {source_state}\n"
            f"{source_summary}\n"
            f"Runtime: {inspection.runtime_version or 'offline or not configured'}\n"
            f"Backend: {', '.join(inspection.backend_ids) or 'not ready'}\n\n"
            "CollectiGen will manage the saved endpoint, launcher, health checks, "
            "supported backend connection, model-path integration previews, lifecycle requests, and generation "
            "submission. Models, outputs, extensions, credentials, unknown files, and "
            "other custom settings remain user-owned. Disconnecting deletes none of them. "
            "The source check excludes untracked files and does not verify configuration or backups.\n\n"
            "Source observations will be saved with this connection. Show Details to review "
            "candidate extension folders and their revisions. Incomplete scans cannot establish "
            "which folders were added or removed. Activation and extension file changes remain unverified."
        )
        if inspection.issue:
            details += "\n\nCurrent issue: " + inspection.issue
        box = QMessageBox(QMessageBox.Question, "Use Existing SwarmUI?", details,
                          QMessageBox.Yes | QMessageBox.Cancel, self)
        box.setTextFormat(Qt.PlainText)
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("Use Existing SwarmUI")
        box.setDetailedText(source_observations)
        message = box.findChild(QLabel, "qt_msgbox_label")
        if message is not None:
            message.setMinimumWidth(600)
        source_details = box.findChild(QTextEdit)
        if source_details is not None:
            source_details.setMinimumHeight(240)
            source_details.setAccessibleName("Reviewed SwarmUI source observations")
        accepted = box.exec() == QMessageBox.Yes
        box.deleteLater()
        if accepted:
            self._run(
                "Saving the reviewed SwarmUI connection…",
                lambda: self.manager.adopt(inspection),
                lambda _result: self.refresh_snapshot(),
            )

    def review_managed_install(self):
        parent = QFileDialog.getExistingDirectory(self, "Choose where SwarmUI will be installed")
        if not parent:
            return
        destination = Path(parent) / "SwarmUI"
        preview = self.manager.deployment_preview(
            destination, getattr(self.settings, "ai_models_directory", "")
        )
        free_gib = preview.free_bytes / 1024**3 if preview.free_bytes else 0
        details = (
            f"Pinned source: {preview.source}\nRelease: {preview.release}\n"
            f"Exact commit: {preview.commit}\nDestination: {preview.destination}\n"
            f"SwarmUI license: MIT (connected backends, extensions, and models have separate licenses)\n"
            f"Available storage: {free_gib:.1f} GiB\n"
            f"CollectiGen model library: {preview.model_root}\n\n"
            + "\n".join(preview.prerequisites)
            + ("\n\nResolve before continuing:\n• " + "\n• ".join(preview.issues) if preview.issues else "")
            + "\n\nThis prepares and verifies pinned SwarmUI source only. The first start is a separate action because SwarmUI may acquire backend dependencies; no model is downloaded silently."
        )
        if not preview.ready:
            QMessageBox.warning(self, "Managed Install Is Not Ready", details)
            return
        if QMessageBox.question(
            self, "Prepare Managed SwarmUI?", details,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self._run(
            "Downloading and verifying pinned SwarmUI source…",
            lambda: self.manager.prepare_managed_install(preview),
            self._managed_install_prepared,
        )

    def _managed_install_prepared(self, inspection: SwarmInspection):
        try:
            self.manager.adopt(inspection, managed=True)
        except Exception as exc:
            QMessageBox.warning(self, "Managed Install Could Not Be Enrolled", str(exc))
            return
        QMessageBox.information(
            self, "SwarmUI Files Are Ready",
            "The pinned source was verified and enrolled. Choose Start SwarmUI to run "
            "its local first-use setup. CollectiGen will not choose or download a model for you.",
        )

    def refresh_health(self):
        endpoint = self.endpoint.text().strip() or DEFAULT_ENDPOINT
        snapshot = self.manager.snapshot()
        if snapshot.configured and endpoint != snapshot.endpoint:
            try:
                self.manager.change_endpoint(endpoint)
            except Exception as exc:
                QMessageBox.warning(self, "Invalid SwarmUI Endpoint", str(exc))
                return
        self._run("Checking the local SwarmUI connection…", self.manager.refresh_health, lambda _result: None)

    def start_runtime(self):
        snapshot = self.manager.snapshot()
        endpoint = self.endpoint.text().strip() or DEFAULT_ENDPOINT
        if snapshot.configured and endpoint != snapshot.endpoint:
            try:
                snapshot = self.manager.change_endpoint(endpoint)
            except Exception as exc:
                QMessageBox.warning(self, "Invalid SwarmUI Endpoint", str(exc))
                return
        if snapshot.state == "incompatible" or (
            snapshot.state == "needs_attention" and getattr(snapshot, "compatibility_state", "") == "unavailable"
        ):
            self.review_compatibility()
            return
        if snapshot.control_plane_online:
            self.open_runtime()
            return
        self._startup_notice = ""
        self._startup_target = (snapshot.install_root, snapshot.endpoint)
        self._run(
            "Checking the local endpoint before starting SwarmUI…",
            self.manager.start_runtime,
            self._start_completed,
        )

    def _start_completed(self, _status):
        self._startup_checks_remaining = 24
        self._startup_checked(self.manager.snapshot())

    def _poll_startup(self):
        if self._startup_checks_remaining <= 0:
            return
        snapshot = self.manager.snapshot()
        if not snapshot.configured or self._startup_target != (snapshot.install_root, snapshot.endpoint):
            self._startup_checks_remaining = 0
            self._startup_timer.stop()
            self._startup_notice = ""
            self.refresh_snapshot()
            return
        if self._task is not None:
            self._startup_timer.start(1500)
            return
        self._run(
            "Starting · checking the local SwarmUI connection…",
            self.manager.refresh_health,
            self._startup_checked,
        )

    def _startup_checked(self, snapshot):
        if not snapshot.configured or (
            self._startup_target is not None
            and self._startup_target != (snapshot.install_root, snapshot.endpoint)
        ):
            self._startup_checks_remaining = 0
            self._startup_timer.stop()
            self._startup_notice = ""
            return
        if snapshot.state in {"ready", "busy", "update_available", "incompatible", "needs_attention"}:
            self._startup_checks_remaining = 0
            self._startup_timer.stop()
            self._startup_notice = ""
            return
        self._startup_checks_remaining -= 1
        status = self.manager.startup_status()
        if (
            status.returncode is not None
            and not snapshot.control_plane_online
        ):
            self._startup_checks_remaining = 0
            self._startup_timer.stop()
            self._startup_notice = (
                f"The SwarmUI launcher exited with code {status.returncode} before the server became ready. "
                "Review its startup details, then check the connection before trying Start again."
            )
            QMessageBox.warning(
                self, "SwarmUI Could Not Start",
                self._startup_notice,
            )
            return
        if self._startup_checks_remaining > 0:
            self._startup_timer.start(2500)
        else:
            self._startup_timer.stop()
            self._startup_notice = (
                "Automatic startup checks have finished. Use Check Connection to try again; "
                "a running launcher is retained and will not be started twice."
            )

    def open_runtime(self):
        snapshot = self.manager.snapshot()
        if not snapshot.configured or not snapshot.control_plane_online:
            QMessageBox.warning(
                self, "SwarmUI Needs Attention",
                "Start or reconnect the saved local SwarmUI runtime before opening it.",
            )
            return
        QDesktopServices.openUrl(QUrl(snapshot.endpoint))

    def check_updates(self):
        self._run("Checking SwarmUI for updates…", self.manager.check_updates, lambda _result: None)

    def apply_update(self):
        """Retain the shared update entry point as a read-only recovery review."""
        if not self.update_button.isEnabled():
            return
        snapshot = self.manager.snapshot()
        text = "\n\n".join((
            SOURCE_UPDATE_BLOCK_REASON,
            f"Last reported update: {snapshot.update_summary or 'unavailable'}",
            "Update discovery does not establish an exact approved target or a recoverable installation.",
            "Next: close this review and continue using the current installation. "
            "If its connection and backend are ready, return to your saved generation request, "
            "check the connection, bind the exact model and prepare it again.",
            "CollectiGen must qualify installation backup, target verification and rollback "
            "before it can offer source updates. Configuration Backups preserves only the files "
            "you explicitly selected; creating one does not enable updates.",
        ))
        self._show_read_only_report(
            "SwarmUI Update Review", "Updates are paused until installation recovery is supported. "
            "This review does not update or restart SwarmUI.",
            text, "SwarmUI update recovery review", "_update_review_dialog",
        )

    def review_lifecycle(self, action: str):
        button = self.stop_button if action == "stop" else self.restart_button
        if action not in {"stop", "restart"} or not button.isEnabled():
            return
        snapshot = self.manager.snapshot()
        consequence = (
            "SwarmUI will shut down. Use Start SwarmUI when you need it again."
            if action == "stop" else
            "SwarmUI will rebuild and restart its current code without requesting updates."
        )
        if QMessageBox.question(
            self, f"{action.title()} SwarmUI?",
            f"{snapshot.endpoint}\n\n{consequence}\n\n"
            "All sessions must be idle. CollectiGen checks again before sending the request. "
            "Models, outputs and settings are kept. Afterward, use Check Connection to confirm the result.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self._run(
            f"Requesting SwarmUI {action}…",
            lambda: self.manager.request_lifecycle(action), lambda _result: None,
        )

    def view_logs(self):
        if not self.logs_button.isEnabled():
            return
        self._run("Reading local SwarmUI logs…", self.manager.recent_logs, self._show_logs)

    def review_installation(self):
        if not self.review_installation_button.isEnabled():
            return
        self._run("Reviewing the SwarmUI installation…", self.manager.review_installation,
                  self._show_installation_review)

    def review_compatibility(self):
        """Explain the last explicit probe without probing or changing the runtime."""
        snapshot = self.manager.snapshot()
        text = "\n".join((
            f"Last connection check: {snapshot.last_health_check or 'not checked'}",
            f"Saved runtime version: {snapshot.runtime_version or 'unavailable'}",
            f"State: {snapshot.headline}", "",
            getattr(snapshot, "compatibility_issue", "") or snapshot.detail, "",
            "CollectiGen checks the required generation API and local backend. A version label alone does not establish support.", "",
            "Next: use Review Installation to inspect the enrolled runtime. Correct its API or backend setup if needed, then choose Check Connection.",
            "After a successful check, return to the saved generation plan and explicitly check its connection, bind the exact model and prepare the request again.", "",
            "This review does not update, start, restart or submit anything.",
        ))
        self._show_read_only_report(
            "SwarmUI Compatibility Review", "Read-only results from the last connection check.",
            text, "SwarmUI compatibility review", "_compatibility_dialog",
        )

    def review_configuration_backups(self):
        if not self.configuration_backups_button.isEnabled():
            return
        if self._configuration_backups_dialog is not None:
            self._configuration_backups_dialog.raise_()
            self._configuration_backups_dialog.activateWindow()
            return
        from .swarm_configuration_backup_dialog import SwarmConfigurationBackupDialog
        snapshot = self.manager.snapshot()
        dialog = SwarmConfigurationBackupDialog(
            self.manager, self, installation_folder=snapshot.install_root,
            configured=snapshot.configured,
        )
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._configuration_backups_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "_configuration_backups_dialog", None))
        dialog.show()
        dialog.load_backups()

    def review_companion_package(self):
        if not self.companion_package_button.isEnabled():
            return
        if self._companion_package_dialog is not None:
            self._companion_package_dialog.raise_()
            self._companion_package_dialog.activateWindow()
            return
        from core.model_selection import protected_model_roots
        from .swarm_companion_package_dialog import SwarmCompanionPackageDialog
        dialog = SwarmCompanionPackageDialog(
            self.manager, self, configured=self._companion_configured,
            protected_roots=protected_model_roots(self.settings),
        )
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._companion_package_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "_companion_package_dialog", None))
        dialog.show()

    def review_model_paths(self):
        if not self.model_paths_button.isEnabled():
            return
        if self._model_paths_dialog is not None:
            self._model_paths_dialog.raise_()
            self._model_paths_dialog.activateWindow()
            return
        from .swarm_model_paths_dialog import SwarmModelPathsDialog
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from core.paths import MODEL_RESOURCES_PATH
        dialog = SwarmModelPathsDialog(
            self.manager, self,
            model_directory=getattr(self.settings, "ai_models_directory", "") or "",
            model_registry=ModelResourceRegistry(MODEL_RESOURCES_PATH),
            protected_roots=protected_model_roots(self.settings),
            boundary_provider=lambda: protected_model_roots(self.settings),
            jobs=getattr(getattr(self.parent(), "state", None), "jobs", None),
        )
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._model_paths_dialog = dialog
        dialog.finished.connect(lambda _result: self.refresh_snapshot())
        dialog.destroyed.connect(lambda: setattr(self, "_model_paths_dialog", None))
        dialog.show()
        dialog.load_current()

    def review_onboarding(self):
        if not self.onboarding_button.isEnabled(): return
        if self._onboarding_dialog is not None and self._onboarding_dialog.isVisible():
            self._onboarding_dialog.raise_(); self._onboarding_dialog.activateWindow(); return
        from core.paths import MODEL_RESOURCES_PATH
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from .swarm_onboarding_workflow import open_swarm_onboarding
        jobs = getattr(getattr(self.parent(), "state", None), "jobs", None)
        if jobs is None:
            self.state_detail.setText("Open Import Existing Setup from Setup while a workspace is available.")
            return
        self._onboarding_dialog = open_swarm_onboarding(self,
            ModelResourceRegistry(MODEL_RESOURCES_PATH), jobs, settings=self.settings,
            protected_roots=protected_model_roots(self.settings), manager=self.manager)

    def request_onboarding_close(self, continuation):
        dialog = self._onboarding_dialog
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def _show_installation_review(self, text: str):
        self._show_read_only_report(
            "SwarmUI Installation Review",
            "Read-only inspection. Live, saved and unavailable values are labeled separately. "
            "Source changes compare with the observations saved during enrollment. "
            "Paths are displayed locally; this report is not saved or uploaded. "
            "No settings, models or runtime files are changed.",
            text, "SwarmUI installation review", "_installation_dialog",
        )

    def _show_logs(self, text: str):
        self._show_read_only_report(
            "Local SwarmUI Logs",
            "Recent information, warning and error messages. Logs may contain private paths or prompts; they are displayed locally and are not saved or uploaded by CollectiGen.",
            text, "Local SwarmUI diagnostic messages", "_logs_dialog",
        )

    def _show_read_only_report(self, title, description, text, accessible_name, attribute):
        previous = getattr(self, attribute)
        if previous is not None:
            previous.close()
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.resize(850, 560)
        layout = QVBoxLayout(dialog)
        note = QLabel(description)
        note.setWordWrap(True)
        layout.addWidget(note)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setAccessibleName(accessible_name)
        viewer.setPlainText(text)
        layout.addWidget(viewer, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        setattr(self, attribute, dialog)
        dialog.destroyed.connect(
            lambda: setattr(self, attribute, None) if getattr(self, attribute) is dialog else None
        )
        dialog.show()

    def disconnect(self):
        if QMessageBox.question(
            self, "Disconnect SwarmUI?",
            "Remove only CollectiGen's connection and management record? SwarmUI, models, "
            "outputs, extensions, settings, and all user files will remain untouched.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self.manager.disconnect()
        self._startup_timer.stop()
        self._startup_checks_remaining = 0
        self._startup_notice = ""
        self.install_root.clear()
        self.endpoint.setText(DEFAULT_ENDPOINT)
        self.refresh_snapshot()
