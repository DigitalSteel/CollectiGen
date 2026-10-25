"""Explicit local backups of user-selected SwarmUI configuration files."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThreadPool, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.swarm_configuration_backup import SwarmConfigurationBackup
from core.swarm_recovery_scope import PROTECTED
from .components import compact_button
from .workers import CancelableProgressCallTask


ROLE_LABELS = {"settings": "Server settings", "backends": "Backend settings"}


class SwarmConfigurationBackupDialog(QDialog):
    """Back up exact selected bytes; never replace a live runtime file."""

    def __init__(self, manager, parent=None, *, service=None, installation_folder="", configured=True):
        super().__init__(parent)
        self.setWindowTitle("SwarmUI Configuration Backups")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1000, 760)
        self.service = service or SwarmConfigurationBackup(manager)
        self.installation_folder = str(installation_folder)
        self.configured = configured
        self.pool = QThreadPool.globalInstance()
        self._task = None
        self.preview = None
        self.recovery_preview = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Keep an exact local copy of your selected SwarmUI settings and backend configuration. "
            "This does not back up models, images, extensions or the runtime itself."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel("Choose the configuration files, then review the backup.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Configuration backup status")
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        create_page = QWidget()
        create_layout = QVBoxLayout(create_page)
        explanation = QLabel(
            "Select the two files actually used by your SwarmUI launch. Custom launches can override "
            "their locations; CollectiGen does not assume the default files are active. "
            "File contents are copied exactly and are not displayed or parsed."
        )
        explanation.setWordWrap(True)
        create_layout.addWidget(explanation)
        form = QGridLayout()
        self.fields = {}
        self.browse_buttons = {}
        for row, role in enumerate(ROLE_LABELS):
            edit = QLineEdit()
            edit.setAccessibleName(f"SwarmUI {ROLE_LABELS[role]} file")
            edit.setPlaceholderText("Choose the configuration file used by this launch")
            edit.textChanged.connect(self._invalidate_preview)
            browse = compact_button(QPushButton("Browse…"))
            browse.setAccessibleName(f"Choose {ROLE_LABELS[role]} file")
            browse.clicked.connect(lambda _checked=False, key=role: self._choose_file(key))
            form.addWidget(QLabel(ROLE_LABELS[role].capitalize() + " file"), row, 0)
            form.addWidget(edit, row, 1)
            form.addWidget(browse, row, 2)
            self.fields[role] = edit
            self.browse_buttons[role] = browse
        form.setColumnStretch(1, 1)
        create_layout.addLayout(form)
        privacy = QLabel(
            "These files may contain passwords or API keys. The backup stays beside CollectiGen's "
            "local connection record. Keep it private; there is no upload or automatic replacement "
            "of SwarmUI files. Avoid editing either file while the copy is being made."
        )
        privacy.setWordWrap(True)
        create_layout.addWidget(privacy)
        self.preview_details = QPlainTextEdit()
        self.preview_details.setReadOnly(True)
        self.preview_details.setAccessibleName("Exact configuration backup scope")
        self.preview_details.setPlaceholderText("Review Backup will show the exact files, sizes and fingerprints.")
        create_layout.addWidget(self.preview_details, 1)
        create_actions = QHBoxLayout()
        create_actions.addStretch(1)
        self.review_button = compact_button(QPushButton("Review Backup"))
        self.create_button = compact_button(QPushButton("Create Backup"), primary=True)
        self.review_button.clicked.connect(self.review_backup)
        self.create_button.clicked.connect(self.create_backup)
        create_actions.addWidget(self.review_button)
        create_actions.addWidget(self.create_button)
        create_layout.addLayout(create_actions)
        self.tabs.addTab(create_page, "Create Backup")

        saved_page = QWidget()
        saved_layout = QVBoxLayout(saved_page)
        saved_note = QLabel(
            "Verify a saved copy before relying on it. Compare Originals reports whether the selected "
            "source files still match. Verification works while SwarmUI is offline; comparison needs "
            "the same saved connection. Incomplete copies are retained and clearly marked."
        )
        saved_note.setWordWrap(True)
        saved_layout.addWidget(saved_note)
        self.history = QTreeWidget()
        self.history.setHeaderLabels(["Created", "State", "Size", "Backup ID"])
        self.history.setRootIsDecorated(False)
        self.history.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history.setAccessibleName("Saved SwarmUI configuration backups")
        self.history.itemSelectionChanged.connect(self._selection_changed)
        saved_layout.addWidget(self.history, 1)
        self.saved_details = QPlainTextEdit()
        self.saved_details.setReadOnly(True)
        self.saved_details.setAccessibleName("Configuration backup verification results")
        self.saved_details.setPlaceholderText("No backup selected. Create a backup or refresh the saved copies.")
        saved_layout.addWidget(self.saved_details, 2)
        saved_actions = QGridLayout()
        self.refresh_button = compact_button(QPushButton("Refresh Backups"))
        self.verify_button = compact_button(QPushButton("Verify Backup"))
        self.compare_button = compact_button(QPushButton("Compare Originals"))
        self.open_button = compact_button(QPushButton("Open Backup Folder"))
        self.recovery_button = compact_button(QPushButton("Prepare Recovery Files…"))
        self.coverage_button = compact_button(QPushButton("Review Recovery Coverage"))
        self.coverage_button.setAccessibleName("Review recovery coverage of the selected backup")
        self.refresh_button.clicked.connect(self.load_backups)
        self.verify_button.clicked.connect(self.verify_selected)
        self.compare_button.clicked.connect(self.compare_selected)
        self.open_button.clicked.connect(self.open_selected)
        self.recovery_button.clicked.connect(self.prepare_recovery)
        self.coverage_button.clicked.connect(self.review_coverage)
        for index, button in enumerate((self.refresh_button, self.verify_button, self.compare_button,
                                        self.open_button, self.coverage_button, self.recovery_button)):
            saved_actions.addWidget(button, index // 3, index % 3)
        saved_layout.addLayout(saved_actions)
        self.tabs.addTab(saved_page, "Saved Backups")

        recovery_page = QWidget()
        recovery_layout = QVBoxLayout(recovery_page)
        self.recovery_source = QLabel("Select a complete backup in Saved Backups, then choose Prepare Recovery Files.")
        self.recovery_source.setWordWrap(True)
        self.recovery_source.setTextFormat(Qt.PlainText)
        recovery_layout.addWidget(self.recovery_source)
        recovery_note = QLabel(
            "Create verified settings.fds and backends.fds copies in a new folder. Existing folders and "
            "original files are never replaced. This does not restore or start SwarmUI; its actual launch "
            "paths and stopped state have not been verified. Copies may contain passwords or API keys. "
            "Choose private local storage; a synced or shared folder can expose these files."
        )
        recovery_note.setWordWrap(True)
        recovery_layout.addWidget(recovery_note)
        destination_row = QHBoxLayout()
        self.recovery_destination = QLineEdit()
        self.recovery_destination.setAccessibleName("Recovery copy folder")
        self.recovery_destination.setPlaceholderText("New folder for creation, or an existing recovery copy for verification")
        self.recovery_destination.textChanged.connect(self._invalidate_recovery)
        self.recovery_parent_button = compact_button(QPushButton("Choose Parent…"))
        self.recovery_existing_button = compact_button(QPushButton("Choose Existing Copy…"))
        self.recovery_parent_button.clicked.connect(self._choose_recovery_parent)
        self.recovery_existing_button.clicked.connect(self._choose_recovery_copy)
        destination_row.addWidget(self.recovery_destination, 1)
        destination_row.addWidget(self.recovery_parent_button)
        destination_row.addWidget(self.recovery_existing_button)
        recovery_layout.addLayout(destination_row)
        self.recovery_details = QPlainTextEdit()
        self.recovery_details.setReadOnly(True)
        self.recovery_details.setAccessibleName("Configuration recovery copy review")
        self.recovery_details.setPlaceholderText("Review the exact backup and destination before creating a copy.")
        recovery_layout.addWidget(self.recovery_details, 1)
        recovery_actions = QHBoxLayout()
        self.recovery_verify_button = compact_button(QPushButton("Verify Recovery Copy"))
        self.recovery_open_button = compact_button(QPushButton("Open Recovery Folder"))
        self.recovery_review_button = compact_button(QPushButton("Review Recovery Copy"))
        self.recovery_create_button = compact_button(QPushButton("Create Recovery Copy"), primary=True)
        self.recovery_verify_button.clicked.connect(self.verify_recovery)
        self.recovery_open_button.clicked.connect(lambda: self.verify_recovery(open_folder=True))
        self.recovery_review_button.clicked.connect(self.review_recovery)
        self.recovery_create_button.clicked.connect(self.create_recovery)
        recovery_actions.addWidget(self.recovery_verify_button)
        recovery_actions.addWidget(self.recovery_open_button)
        recovery_actions.addStretch(1)
        recovery_actions.addWidget(self.recovery_review_button)
        recovery_actions.addWidget(self.recovery_create_button)
        recovery_layout.addLayout(recovery_actions)
        self.tabs.addTab(recovery_page, "Recovery Files")

        self.progress = QProgressBar()
        self.progress.setAccessibleName("Configuration backup progress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = compact_button(QPushButton("Cancel Operation"))
        self.close_button = compact_button(QPushButton("Close"))
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self._update_actions()
        if not configured:
            self.tabs.setCurrentIndex(1)
            self.status.setText("Connect SwarmUI in Setup to create or compare backups. Saved backups and recovery copies remain available offline.")

    def _selected(self):
        rows = self.history.selectedItems()
        return rows[0].data(0, Qt.UserRole) if rows else None

    def _update_actions(self):
        idle = self._task is None
        selected = self._selected()
        self.tabs.setEnabled(idle)
        self.review_button.setEnabled(idle and self.configured and all(edit.text() for edit in self.fields.values()))
        self.create_button.setEnabled(idle and self.preview is not None)
        self.refresh_button.setEnabled(idle)
        self.verify_button.setEnabled(idle and selected is not None)
        self.coverage_button.setEnabled(idle and selected is not None)
        self.compare_button.setEnabled(idle and self.configured and selected is not None and selected.status == "complete")
        self.open_button.setEnabled(idle and selected is not None and selected.status == "complete")
        ready = idle and selected is not None and selected.status == "complete"
        self.recovery_button.setEnabled(ready)
        self.recovery_parent_button.setEnabled(ready)
        self.recovery_existing_button.setEnabled(ready)
        self.recovery_review_button.setEnabled(ready and bool(self.recovery_destination.text().strip()))
        self.recovery_create_button.setEnabled(ready and self.recovery_preview is not None)
        self.recovery_verify_button.setEnabled(ready and bool(self.recovery_destination.text().strip()))
        self.recovery_open_button.setEnabled(self.recovery_verify_button.isEnabled())
        self.cancel_button.setEnabled(not idle)
        self.close_button.setEnabled(idle)

    def _invalidate_preview(self, *_args):
        self.preview = None
        self.preview_details.clear()
        if hasattr(self, "create_button"):
            self._update_actions()

    def _choose_file(self, role):
        if self._task is not None:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, f"Choose {ROLE_LABELS[role]} File", self.installation_folder,
            "SwarmUI configuration (*.fds);;All files (*)",
        )
        if path:
            self.fields[role].setText(path)

    def _run(self, message, operation, completed):
        if self._task is not None:
            return
        self.status.setText(message)
        self.progress.setValue(0)
        self.progress.setVisible(True)

        def execute(report, cancelled):
            return operation(lambda role, done, total: report(done, total, ROLE_LABELS.get(role, role)), cancelled)

        task = CancelableProgressCallTask(execute)
        self._task = task
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(lambda result: self._finished(result, completed))
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        self._update_actions()
        self.pool.start(task)

    def _progress(self, current, total, label):
        self.progress.setValue(int(100 * min(current, total) / total) if total > 0 else 0)
        self.progress.setFormat(f"{label}: %p%")

    def _finished(self, result, completed):
        self._task = None
        self.progress.setVisible(False)
        try:
            completed(result)
        finally:
            self._update_actions()

    def _failed(self, message):
        self._task = None
        self.preview = None
        self.recovery_preview = None
        self.progress.setVisible(False)
        self.status.setText(str(message))
        self._update_actions()

    def _cancelled(self):
        self._task = None
        self.preview = None
        self.recovery_preview = None
        self.progress.setVisible(False)
        self.status.setText("Operation cancelled. Original files are unchanged. A partial folder may remain; refresh backups or verify the chosen recovery copy before using it.")
        self._update_actions()

    def cancel_operation(self):
        if self._task is not None:
            self._task.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancelling at the next safe checkpoint…")

    def review_backup(self):
        if not self.review_button.isEnabled():
            return
        self.preview = None
        settings, backends = (self.fields[key].text() for key in ROLE_LABELS)
        self._run("Reviewing the selected configuration files…",
                  lambda progress, cancel: self.service.preview(settings, backends, progress=progress, should_cancel=cancel),
                  self._received_preview)

    def _received_preview(self, preview):
        self.preview = preview
        lines = ["Exact selected files (active launch paths are not verified):", ""]
        for source in preview.files:
            lines.extend((ROLE_LABELS[source.role], source.path,
                          f"Size: {source.size:,} bytes", f"SHA-256: {source.sha256}", ""))
        lines.extend((f"Total: {preview.total_bytes:,} bytes", f"Local backup storage: {self.service.backup_dir}",
                      "", "The copy can contain credentials. Create Backup saves only these two files locally."))
        self.preview_details.setPlainText("\n".join(lines))
        self.status.setText("Review the exact files and private storage below, then choose Create Backup.")

    def create_backup(self):
        if not self.create_button.isEnabled():
            return
        preview = self.preview
        self.preview = None
        self._run("Creating and verifying the local configuration backup…",
                  lambda progress, cancel: self.service.create(preview, progress=progress, should_cancel=cancel), self._created)

    def _created(self, summary):
        self.tabs.setCurrentIndex(1)
        self.load_backups(select_id=summary.backup_id,
                          message="Configuration backup saved and verified. Original files are unchanged.")

    def load_backups(self, _checked=False, *, select_id="", message=""):
        self._run("Reading saved configuration backups…",
                  lambda _progress, _cancel: self.service.list_backups(),
                  lambda summaries: self._received_backups(summaries, select_id, message))

    def _received_backups(self, summaries, select_id="", message=""):
        self.history.clear()
        labels = {"complete": "Saved (Verify)", "incomplete": "Incomplete", "invalid": "Invalid"}
        for summary in summaries:
            item = QTreeWidgetItem([summary.created_at or "Unknown", labels.get(summary.status, summary.status),
                                  f"{summary.total_bytes:,} bytes", summary.backup_id])
            item.setData(0, Qt.UserRole, summary)
            self.history.addTopLevelItem(item)
            if summary.backup_id == select_id:
                self.history.setCurrentItem(item)
        for column in range(3):
            self.history.resizeColumnToContents(column)
        self.status.setText(message or (
            f"{len(summaries)} saved configuration copies. Select one to verify or compare."
            if summaries else "No configuration backups yet. Choose files in Create Backup to begin."
        ))
        if not self._selected():
            self.saved_details.clear()

    def _selection_changed(self):
        self._invalidate_recovery()
        selected = self._selected()
        if selected:
            lines = [selected.message, ""]
            lines.extend(f"{ROLE_LABELS[source.role]}: {source.path}" for source in selected.files)
            self.saved_details.setPlainText("\n".join(lines))
        else:
            self.saved_details.clear()
        if hasattr(self, "recovery_source"):
            self.recovery_source.setText(
                f"Selected backup: {selected.backup_id} · {selected.created_at} · {selected.total_bytes:,} bytes"
                if selected else "Select a complete backup in Saved Backups, then choose Prepare Recovery Files."
            )
        self._update_actions()

    def _invalidate_recovery(self, *_args):
        self.recovery_preview = None
        if hasattr(self, "recovery_details"):
            if self.recovery_details.toPlainText():
                self.status.setText("Recovery selection changed. Review the new folder or verify the selected existing copy.")
            self.recovery_details.clear()
            self._update_actions()

    def prepare_recovery(self):
        if self.recovery_button.isEnabled():
            self.tabs.setCurrentIndex(2)
            self.recovery_destination.setFocus()
            self.status.setText("Choose a new private local folder and review the recovery copy, or choose an existing copy to verify.")

    def _choose_recovery_parent(self):
        if not self.recovery_parent_button.isEnabled():
            return
        parent = QFileDialog.getExistingDirectory(self, "Choose a Private Local Parent Folder")
        if parent:
            self.recovery_destination.setText(str(Path(parent) / ("swarm-recovery-" + uuid4().hex[:12])))

    def _choose_recovery_copy(self):
        if not self.recovery_existing_button.isEnabled():
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose an Existing Recovery Copy")
        if folder:
            self.recovery_destination.setText(folder)

    def review_recovery(self):
        if not self.recovery_review_button.isEnabled():
            return
        selected = self._selected()
        destination = self.recovery_destination.text().strip()
        self.recovery_preview = None
        self._run("Verifying the archive and reviewing the new recovery folder…",
                  lambda progress, cancel: self.service.preview_materialization(selected.backup_id, destination,
                      progress=progress, should_cancel=cancel), self._received_recovery_preview)

    def _received_recovery_preview(self, preview):
        self.recovery_preview = preview
        lines = [f"Backup: {preview.backup_id}", f"New folder: {preview.destination}", ""]
        for source in preview.files:
            lines.extend((f"{ROLE_LABELS[source.role]} → {source.role}.fds",
                          f"Size: {source.size:,} bytes", f"SHA-256: {source.sha256}", ""))
        lines.extend((f"Total: {preview.total_bytes:,} bytes", "",
            "Create Recovery Copy writes only these opaque files and recovery evidence in the new folder.",
            "Files may contain credentials. Existing files and runtime setup are unchanged.",
            "An interrupted copy remains incomplete and must not be used."))
        self.recovery_details.setPlainText("\n".join(lines))
        self.status.setText("Review the backup and private destination, then choose Create Recovery Copy.")

    def create_recovery(self):
        if not self.recovery_create_button.isEnabled():
            return
        preview = self.recovery_preview
        self.recovery_preview = None
        self._run("Creating and verifying recovery files…",
                  lambda progress, cancel: self.service.materialize(preview, progress=progress, should_cancel=cancel),
                  self._show_recovery_report)

    def _show_recovery_report(self, report):
        self.status.setText(report.message)
        lines = [report.message, f"Backup: {report.backup_id}", f"Folder: {report.destination}", ""]
        for source in report.files:
            lines.extend((f"{ROLE_LABELS[source.role]}: {source.status}", source.path,
                          f"Size: {source.size:,} bytes", f"SHA-256: {source.sha256}", ""))
        lines.append("This is a recovery copy, not an installed or restored SwarmUI configuration.")
        self.recovery_details.setPlainText("\n".join(lines))

    def verify_recovery(self, *, open_folder=False):
        if not self.recovery_verify_button.isEnabled():
            return
        selected = self._selected()
        destination = self.recovery_destination.text().strip()
        self.recovery_preview = None
        def verified(report):
            self._show_recovery_report(report)
            if open_folder and report.status == "verified":
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(report.destination))):
                    self.status.setText("The recovery copy verified, but its folder could not be opened.")
        self._run("Verifying the recovery copy against its archived configuration…",
                  lambda progress, cancel: self.service.verify_materialization(selected.backup_id, destination,
                      progress=progress, should_cancel=cancel), verified)

    def _show_report(self, report):
        self.status.setText(report.message)
        lines = [report.message, ""]
        for file in report.files:
            lines.extend((f"{ROLE_LABELS[file.role]}: {file.status}", file.path,
                          f"Saved size: {file.size:,} bytes", f"Saved SHA-256: {file.sha256}", ""))
        self.saved_details.setPlainText("\n".join(lines))

    def review_coverage(self):
        selected = self._selected()
        if not self.coverage_button.isEnabled() or selected is None:
            return
        backup_id = selected.backup_id
        # Logical archive roles are declarations, never active installation paths.
        declaration = dict(schema_version=1, scope_id="selected-configuration",
            installation_ref="not-checked", excluded_categories=list(PROTECTED), open_questions=[],
            components=[dict(item_id=role, role=role, source="declared", ownership="unknown",
                treatment="preserve", root="selected-archive", relative_path=role + ".fds",
                classification="credential-bearing", inspection="selected-artifact") for role in ROLE_LABELS])
        self.saved_details.clear()
        def received(report):
            current = self._selected()
            if current is None or current.backup_id != backup_id:
                self.status.setText("Backup selection changed. Review coverage for the current selection.")
                return
            self._show_coverage(report, backup_id)
        self._run("Reviewing recovery coverage of the selected archive…",
            lambda progress, cancel: self.service.review_recovery_scope(declaration, backup_id=backup_id,
                progress=progress, should_cancel=cancel), received)

    def _show_coverage(self, report, backup_id):
        observation = next((row for row in report.observations
                            if row.kind == "configuration-archive" and row.reference == backup_id), None)
        verified = observation is not None and observation.status == "archive-integrity-verified"
        result = ("Both archived configuration files match their saved fingerprints."
                  if verified else "Archive integrity is unavailable. Do not rely on this copy until it verifies.")
        lines = ["RECOVERY COVERAGE", f"Selected backup: {backup_id}", "", result]
        if verified:
            lines.append(f"Checked: {observation.observed_at}")
            for source in observation.files:
                lines.append(f"{ROLE_LABELS.get(source.role, source.role)}: {source.size:,} bytes · SHA-256 {source.sha256}")
        lines.extend(("", "FULL INSTALLATION RECOVERY IS NOT AVAILABLE",
            "Configuration backups cover server settings and backend settings only. They may contain passwords or API keys; keep all copies private.",
            "Runtime code, dependencies, extensions, accounts and other installation state are not covered. Models, outputs and libraries are outside this backup.",
            "Active file locations, installed ownership and current originals have not been checked. Matching archive fingerprints do not prove the original capture was complete or consistent.",
            "A full restore also needs a consistent private-state backup, all writers stopped, sufficient private storage and verified replacement and rollback.",
            "No original files, installed configuration or runtime were read or changed by this coverage review.", "",
            ("NEXT: Prepare Recovery Files creates a separately reviewed copy in a new private folder. It does not install or restore SwarmUI."
             if verified else "NEXT: Verify Backup for details, or choose another saved backup.")))
        self.saved_details.setPlainText("\n".join(lines))
        self.status.setText("Archive integrity verified; full installation recovery remains unavailable."
                            if verified else "Recovery coverage unavailable for this archive. Choose another copy or verify it.")

    def verify_selected(self):
        selected = self._selected()
        if not self.verify_button.isEnabled() or selected is None:
            return
        self._run("Verifying the saved configuration bytes…",
                  lambda progress, cancel: self.service.verify(selected.backup_id, progress=progress, should_cancel=cancel),
                  self._show_report)

    def compare_selected(self):
        selected = self._selected()
        if not self.compare_button.isEnabled() or selected is None:
            return
        self._run("Comparing original files without changing them…",
                  lambda progress, cancel: self.service.compare(selected.backup_id, progress=progress, should_cancel=cancel),
                  self._show_report)

    def open_selected(self):
        selected = self._selected()
        if not self.open_button.isEnabled() or selected is None:
            return

        def verified(report):
            self._show_report(report)
            if report.status == "verified":
                try:
                    path = self.service.safe_backup_path(selected.backup_id)
                except (ValueError, OSError, RuntimeError) as exc:
                    self.status.setText(str(exc))
                    return
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    self.status.setText("The backup verified, but its folder could not be opened.")

        self._run("Verifying the backup before opening its local folder…",
                  lambda progress, cancel: self.service.verify(selected.backup_id, progress=progress, should_cancel=cancel), verified)

    def reject(self):
        if self._task is None:
            super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore()
        else:
            super().closeEvent(event)
