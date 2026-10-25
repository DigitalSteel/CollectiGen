"""Review and publish a caller-selected local video dependency pack."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from PySide6.QtCore import Signal, QSize
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core.external_write_policy import ExternalWritePolicy
from core.training_source_io import _assert_unredirected
from core.library_profiles import LibraryProfileRegistry
from core.video_dependency_pack import (
    PackFile, PackSelection, VideoDependencyPackService, VideoPackError,
)

from .components import bounded_window_size
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


SELECTION_SCHEMA = "collectigen.video-pack-selection.v1"
MAX_SELECTION_BYTES = 16 * 1024 * 1024
MAX_VISIBLE_FILES = 200


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.0f} {unit}" if unit == "bytes" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return ""


def _read_selection(path: str | Path) -> PackSelection:
    """Read only the explicitly selected JSON, never discover source files."""
    source = Path(path)
    _assert_unredirected(source)
    if not source.is_absolute() or source.is_symlink():
        raise VideoPackError("Choose an ordinary absolute pack-selection JSON file.")
    before = source.stat()
    if not 1 <= before.st_size <= MAX_SELECTION_BYTES:
        raise VideoPackError("Pack-selection JSON must be nonempty and at most 16 MiB.")
    with source.open("rb") as handle:
        opened = handle.read(MAX_SELECTION_BYTES + 1)
    _assert_unredirected(source)
    if (len(opened) > MAX_SELECTION_BYTES or source.stat().st_size != before.st_size
            or source.stat().st_mtime_ns != before.st_mtime_ns):
        raise VideoPackError("Pack-selection JSON changed while reading.")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise VideoPackError("Pack-selection JSON has duplicate fields.")
            result[key] = value
        return result

    try:
        payload = json.loads(opened.decode("utf-8"), object_pairs_hook=unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    except (UnicodeError, ValueError) as exc:
        raise VideoPackError("Pack-selection file needs valid bounded UTF-8 JSON.") from exc
    if not isinstance(payload, dict) or payload.get("schema_id") != SELECTION_SCHEMA:
        raise VideoPackError("Choose a versioned video pack-selection JSON file.")
    rows = payload.get("files")
    if not isinstance(rows, list):
        raise VideoPackError("Pack selection needs an explicit file list.")
    file_keys = {"role", "source_path", "relative_path", "sha256", "byte_length", "license_id", "source_record"}
    files = []
    for row in rows:
        if not isinstance(row, dict) or not file_keys.issubset(row):
            raise VideoPackError("Every selected file needs an absolute source and complete identity.")
        files.append(PackFile(row["role"], row["source_path"], row["relative_path"], row["sha256"],
                              row["byte_length"], row["license_id"], row["source_record"],
                              {key: value for key, value in row.items() if key not in file_keys}))
    top_keys = {"schema_id", "files", "model_roles", "source_revision", "interpreter_relative_path",
                "dependency_manifest_relative_path", "compatibility_pins", "runtime_closure_supplied",
                "attribution_gates"}
    try:
        return PackSelection(tuple(files), tuple(payload["model_roles"]), payload["source_revision"],
                             payload["interpreter_relative_path"], payload["dependency_manifest_relative_path"],
                             payload["compatibility_pins"], payload.get("runtime_closure_supplied", False),
                             tuple(payload.get("attribution_gates", ())),
                             {key: value for key, value in payload.items() if key not in top_keys})
    except (KeyError, TypeError, ValueError) as exc:
        raise VideoPackError("Pack selection has missing or invalid declared fields.") from exc


class VideoDependencyPackDialog(FeedbackEnabledDialog):
    """One explicit selected-file pack, no source discovery or runtime claims."""

    feedback_workflow = "Video Offline Pack"
    background_work_finished = Signal()

    def __init__(self, jobs, settings, parent=None, *, service=None, selection_builder=None):
        super().__init__(parent)
        self.jobs = jobs
        self.settings = settings
        self.service = service or VideoDependencyPackService()
        self.selection_builder = selection_builder
        self.selection: PackSelection | None = None
        self.plan = None
        self.result = None
        self.verification = None
        self._task = None
        self._task_label = ""
        self._shutdown_requested = False
        self._shutdown_notified = False
        self._policy_fingerprint = ""
        self._boundary_message = ""
        self.setWindowTitle("Video Offline Pack")
        self.resize(1040, 720)

        layout = QVBoxLayout(self)
        title = QLabel("Export an exact, selected offline dependency pack")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        intro = QLabel("Build a selection from your reviewed runtime and models, then review its files, licenses, "
                       "and destination. Advanced selection JSON can also be imported. A selected source tree "
                       "and Python executable do not establish a complete runnable runtime or independent offline qualification.")
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        self.build_selection_button = QPushButton("Build from Reviewed Runtime and Models")
        self.build_selection_button.clicked.connect(self.build_runtime_selection)
        layout.addWidget(self.build_selection_button)

        source_row = QHBoxLayout()
        self.selection_path = QLineEdit()
        self.selection_path.setReadOnly(True)
        self.selection_path.setPlaceholderText("Choose a pack-selection JSON…")
        self.choose_selection_button = QPushButton("Import Advanced Selection…")
        self.choose_selection_button.clicked.connect(self.choose_selection)
        source_row.addWidget(self.selection_path, 1)
        source_row.addWidget(self.choose_selection_button)
        layout.addLayout(source_row)

        destination_row = QHBoxLayout()
        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText("Absolute path for a new pack folder")
        self.destination_edit.textChanged.connect(self._invalidate_preview)
        self.choose_destination_button = QPushButton("Choose Parent…")
        self.choose_destination_button.clicked.connect(self.choose_destination_parent)
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(self.choose_destination_button)
        layout.addLayout(destination_row)

        self.summary = QLabel("No selected pack loaded.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.license_status = QLabel("")
        self.license_status.setWordWrap(True)
        self.license_status.setObjectName("mutedLabel")
        layout.addWidget(self.license_status)
        self.closure_status = QLabel("Runtime closure and offline execution have not been verified.")
        self.closure_status.setWordWrap(True)
        self.closure_status.setObjectName("mutedLabel")
        layout.addWidget(self.closure_status)

        self.files = QTreeWidget()
        self.files.setHeaderLabels(("Pack path", "Role", "Size", "License"))
        self.files.setRootIsDecorated(False)
        self.files.setUniformRowHeights(True)
        self.files.setAccessibleName("Selected pack files")
        layout.addWidget(self.files, 1)

        self.boundary = QLabel("Choose a destination to review the external-write boundary.")
        self.boundary.setWordWrap(True)
        self.boundary.setObjectName("mutedLabel")
        layout.addWidget(self.boundary)
        self.confirm_destination = QCheckBox("I reviewed this exact new destination and selected file scope")
        self.confirm_destination.toggled.connect(self._update_actions)
        layout.addWidget(self.confirm_destination)

        actions = QVBoxLayout()
        primary_actions = QHBoxLayout()
        secondary_actions = QHBoxLayout()
        self.preview_button = QPushButton("Preview Exact Pack")
        self.preview_button.clicked.connect(self.preview_pack)
        self.publish_button = QPushButton("Export Selected Pack")
        self.publish_button.setProperty("primary", True)
        self.publish_button.clicked.connect(self.publish_pack)
        self.verify_button = QPushButton("Verify Pack")
        self.verify_button.clicked.connect(self.verify_pack)
        self.recover_button = QPushButton("Finish Recovery")
        self.recover_button.clicked.connect(self.recover_pack)
        self.cancel_work_button = QPushButton("Cancel Work")
        self.cancel_work_button.clicked.connect(self.cancel_background_work)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        for button in (self.preview_button, self.publish_button, self.cancel_work_button):
            primary_actions.addWidget(button)
        for button in (self.verify_button, self.recover_button, self.close_button):
            secondary_actions.addWidget(button)
        actions.addLayout(primary_actions)
        actions.addLayout(secondary_actions)
        layout.addLayout(actions)
        self.status = QLabel("Choose a selection and a new local destination.")
        self.status.setWordWrap(True)
        self.status.setObjectName("mutedLabel")
        layout.addWidget(self.status)
        self._update_actions()

    def showEvent(self, event):
        screen = self.screen()
        if screen is not None:
            self.resize(bounded_window_size(screen.availableGeometry(), QSize(1040, 720)))
        super().showEvent(event)

    def _set_status(self, value):
        self.status.setText(str(value)[:2000])

    def _destination(self) -> Path:
        value = self.destination_edit.text().strip()
        if not value or not Path(value).is_absolute():
            raise VideoPackError("Choose the exact absolute path of a new pack folder.")
        return Path(value)

    def choose_selection(self):
        if self._task:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose Video Pack Selection", "", "JSON files (*.json)")
        if path:
            self.load_selection_file(path)

    def load_selection_file(self, path: str | Path) -> bool:
        if self._task:
            return False
        try:
            selection = _read_selection(path)
        except (OSError, VideoPackError) as exc:
            self.selection = None
            self.selection_path.clear()
            self.files.clear()
            self.summary.setText("No selected pack loaded.")
            self.license_status.clear()
            self.closure_status.setText("Runtime closure and offline execution have not been verified.")
            self._invalidate_preview()
            self._set_status(exc)
            return False
        self._apply_selection(selection, str(path))
        return True

    def build_runtime_selection(self):
        if not callable(self.selection_builder):
            self._set_status("Preview the exact runtime and selected models first.")
            return
        self._run("Build reviewed runtime pack selection",
            lambda job: self.selection_builder(should_cancel=lambda: job.cancel_requested),
            lambda selection: self._apply_selection(selection, "Reviewed runtime and selected models"))

    def _apply_selection(self, selection, label):
        self.selection = selection
        self.selection_path.setText(label)
        self._invalidate_preview()
        self.files.clear()
        for item in selection.files[:MAX_VISIBLE_FILES]:
            QTreeWidgetItem(self.files, (item.relative_path, item.role,
                                         _format_bytes(item.byte_length), item.license_id))
        licenses = Counter(item.license_id for item in selection.files)
        total = sum(item.byte_length for item in selection.files)
        self.summary.setText(f"{len(selection.files):,} selected files • {_format_bytes(total)} • "
                             f"{len(selection.model_roles)} model role(s) • {len(selection.files):,} source records supplied"
                             + (f" • first {MAX_VISIBLE_FILES} files shown" if len(selection.files) > MAX_VISIBLE_FILES else ""))
        self.license_status.setText("Declared licenses: " + ", ".join(
            f"{name} ({count:,})" for name, count in sorted(licenses.items())))
        self.closure_status.setText(
            ("Runtime closure declared by selection; independent execution is unverified. "
             if selection.runtime_closure_supplied else "Runtime closure not supplied. ")
            + "Offline qualification and distribution license review remain open. "
            + ("Attribution gates: " + ", ".join(selection.attribution_gates)
               if selection.attribution_gates else "No attribution gates declared; license review is still required."))
        self._set_status("Selection loaded. Choose an unused destination, then preview exact files and free space.")
        self._update_actions()
        return True

    def choose_destination_parent(self):
        if self._task:
            return
        parent = QFileDialog.getExistingDirectory(self, "Choose Pack Destination Parent", "")
        if parent:
            self.destination_edit.setText(str(Path(parent) / "video-offline-pack"))

    def _invalidate_preview(self, *_):
        self.plan = None
        self.result = None
        self.verification = None
        self._policy_fingerprint = ""
        self.confirm_destination.setChecked(False)
        self._refresh_boundary()
        self._update_actions()

    def _policy(self):
        policy = ExternalWritePolicy.from_settings(self.settings)
        registry = (self.settings.library_registry() if callable(getattr(self.settings, "library_registry", None))
                    else LibraryProfileRegistry())
        return policy, registry

    def _refresh_boundary(self):
        if self.selection is None:
            return
        try:
            destination = self._destination()
            policy, registry = self._policy()
            boundary = policy.boundary_for_pairs(
                registry, ((item.path, destination / item.relative_path) for item in self.selection.files),
                backup_enabled=False, artifact_label="selected video dependency pack",
                workflow_label="video Offline Pack export")
            self._boundary_message = boundary.message
            self.boundary.setText(boundary.message + " This is a new no-clobber copy; no original is replaced.")
        except (OSError, ValueError) as exc:
            self._boundary_message = ""
            self.boundary.setText(str(exc))

    def _run(self, label, operation, completed):
        if self._task or self._shutdown_requested:
            return
        task = CoordinatedCallTask(self.jobs, label, operation, resource="io",
                                   key=f"video-offline-pack:{id(self)}:{label}")
        self._task = task
        self._task_label = label
        task.signals.complete.connect(lambda result, task=task: self._completed(task, completed, result))
        task.signals.failed.connect(lambda error, task=task: self._failed(task, error))
        task.signals.cancelled.connect(lambda task=task: self._failed(task, "Work cancelled. Check Finish Recovery if publication had begun."))
        self._set_status(label + "…")
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            self._failed(task, exc)

    def _completed(self, task, completed, result):
        if task is not self._task:
            return
        self._task = None
        self._task_label = ""
        try:
            completed(result)
        except Exception as exc:
            self._set_status(exc)
        self._update_actions()
        self._notify_shutdown()

    def _failed(self, task, error):
        if task is not self._task:
            return
        self._task = None
        self._task_label = ""
        self._set_status(error)
        self._update_actions()
        self._notify_shutdown()

    def _update_actions(self, *_):
        busy = self._task is not None
        self.build_selection_button.setEnabled(callable(self.selection_builder) and not busy and not self._shutdown_requested)
        ready = self.selection is not None and bool(self.destination_edit.text().strip())
        self.choose_selection_button.setEnabled(not busy and not self._shutdown_requested)
        self.choose_destination_button.setEnabled(not busy and not self._shutdown_requested)
        self.destination_edit.setEnabled(not busy and not self._shutdown_requested)
        self.preview_button.setEnabled(ready and not busy and not self._shutdown_requested)
        self.publish_button.setEnabled(self.plan is not None and self.confirm_destination.isChecked()
                                       and not busy and not self._shutdown_requested)
        self.verify_button.setEnabled(bool(self.destination_edit.text().strip()) and not busy
                                      and not self._shutdown_requested)
        self.recover_button.setEnabled(ready and not busy and not self._shutdown_requested)
        self.cancel_work_button.setEnabled(busy)

    def preview_pack(self):
        if self._task or self.selection is None:
            return
        try:
            destination = self._destination()
            self._refresh_boundary()
        except VideoPackError as exc:
            self._set_status(exc)
            return
        self.plan = None
        self.confirm_destination.setChecked(False)
        selection = self.selection
        self._run("Preview selected video pack",
                  lambda job: self.service.preview(selection, destination,
                                                   should_cancel=lambda: job.cancel_requested),
                  self._preview_done)

    def _preview_done(self, plan):
        self.plan = plan
        policy, _ = self._policy()
        self._policy_fingerprint = policy.fingerprint()
        self._refresh_boundary()
        self._set_status(f"Exact pack preview ready: {_format_bytes(plan.required_bytes)} staging space "
                         f"including reserve; {_format_bytes(plan.free_bytes)} available. "
                         "Review the destination and confirm before export.")

    def _confirmed_policy(self) -> bool:
        if self.selection is None or not self.confirm_destination.isChecked():
            self._set_status("Confirm the exact selected files and destination first.")
            return False
        policy, registry = self._policy()
        if self.plan is not None and policy.fingerprint() != self._policy_fingerprint:
            self.plan = None
            self._set_status("External-write policy changed; preview the exact pack again.")
            self._update_actions()
            return False
        destination = self._destination()
        boundary = policy.boundary_for_pairs(
            registry, ((item.path, destination / item.relative_path) for item in self.selection.files),
            backup_enabled=False, artifact_label="selected video dependency pack",
            workflow_label="video Offline Pack export")
        if self.plan is not None and boundary.message != self._boundary_message:
            self.plan = None
            self._set_status("External-write boundary changed; preview the exact pack again.")
            self._update_actions()
            return False
        answer = QMessageBox.question(self, "Confirm Exact Video Pack Destination",
            f"Copy {len(self.selection.files):,} selected files to this new local folder?\n\n{destination}\n\n"
            f"{boundary.message}\n\nNo original is replaced. License, attribution, runtime closure, "
            "and independent offline execution remain separate reviews.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def publish_pack(self):
        if self._task or self.plan is None or self.selection is None:
            return
        try:
            if self._destination() != self.plan.destination:
                self.plan = None
                self._set_status("Destination changed after preview; preview the exact pack again.")
                self._update_actions()
                return
            if not self._confirmed_policy():
                return
        except (OSError, VideoPackError) as exc:
            self._set_status(exc)
            return
        plan = self.plan
        self._run("Export selected video pack",
                  lambda job: self.service.publish(plan, destination_confirmed=True,
                                                   should_cancel=lambda: job.cancel_requested),
                  self._publish_done)

    def _publish_done(self, result):
        self.result = result
        self.plan = None
        self.confirm_destination.setChecked(False)
        self._set_status(result.message + (" Verify Pack to inspect exact published bytes and remaining gates."
                                         if result.status in {"published", "already_published"}
                                         else " Finish Recovery can inspect the owned journal."))

    def verify_pack(self):
        if self._task:
            return
        try:
            destination = self._destination()
        except VideoPackError as exc:
            self._set_status(exc)
            return
        expected = self.selection.fingerprint if self.selection else None
        self._run("Verify selected video pack",
                  lambda job: self.service.verify(destination, expected_fingerprint=expected,
                                                  should_cancel=lambda: job.cancel_requested),
                  self._verify_done)

    def _verify_done(self, verification):
        self.verification = verification
        gates = ", ".join(verification.unresolved_gates)
        self._set_status(f"Verified {verification.file_count:,} selected files "
                         f"({_format_bytes(verification.byte_length)}). Independent offline execution: "
                         f"not qualified. Open gates: {gates}.")

    def recover_pack(self):
        if self._task or self.selection is None:
            return
        try:
            destination = self._destination()
            if not self._confirmed_policy():
                return
        except (OSError, VideoPackError) as exc:
            self._set_status(exc)
            return
        selection = self.selection
        self._run("Finish video pack recovery",
                  lambda job: self.service.recover(destination, selection=selection, action="finish",
                                                   destination_confirmed=True,
                                                   should_cancel=lambda: job.cancel_requested),
                  self._publish_done)

    def cancel_background_work(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)
            self._set_status("Cancellation requested. Waiting for selected-file operation to reach a safe boundary…")

    def has_active_background_work(self) -> bool:
        return self._task is not None

    def request_background_shutdown(self) -> bool:
        self._shutdown_requested = True
        self._shutdown_notified = False
        self.cancel_background_work()
        self._update_actions()
        self._notify_shutdown()
        return not self.has_active_background_work()

    def resume_after_cancelled_shutdown(self) -> bool:
        if self.has_active_background_work():
            return False
        self._shutdown_requested = False
        self._shutdown_notified = False
        self._update_actions()
        return True

    def _notify_shutdown(self):
        if self._shutdown_requested and not self._shutdown_notified and not self.has_active_background_work():
            self._shutdown_notified = True
            self.background_work_finished.emit()

    def reject(self):
        if self.has_active_background_work():
            self.hide()
            return
        super().reject()

    def closeEvent(self, event):
        if self.has_active_background_work():
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)
