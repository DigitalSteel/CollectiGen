"""Explicit detached companion preparation and read-only recovery review."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QProgressBar, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from core.model_workspace import _path as guarded_path
from core.swarm_companion_installation import SwarmCompanionInstallation
from .components import compact_button
from .workers import CancelableProgressCallTask


class SwarmCompanionPackageDialog(QDialog):
    def __init__(self, manager, parent=None, *, configured=False, protected_roots=(), service=None):
        super().__init__(parent)
        self.setWindowTitle("SwarmUI Companion Package")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1020, 740)
        # Constructing the canonical service performs no candidate/host IO.
        self.service = service if service is not None else SwarmCompanionInstallation.for_manager(manager)
        self.configured = bool(configured)
        self.protected_roots = tuple(protected_roots)
        self.pool = QThreadPool.globalInstance()
        self._task = None
        self.preview = None
        self._creating = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Prepare a separate local package for the optional read-only SwarmUI companion, "
            "or inspect a package you already created. Package creation does not install or activate the companion."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel("Choose a new folder and preview the package, or reopen an existing package.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Companion package status")
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        prepare = QWidget()
        prepare_layout = QVBoxLayout(prepare)
        note = QLabel(
            "Preparation currently requires the exact retained developer candidate and a saved SwarmUI connection. "
            "The DLL and two C# source files passed an isolated dispatch check. The project file is reviewed "
            "packaging material only. Normal SwarmUI loading and installation are not qualified. "
            "Preview checks the available local evidence; nothing is downloaded or run."
        )
        note.setWordWrap(True)
        prepare_layout.addWidget(note)
        self.connection_note = QLabel("" if configured else "Connect SwarmUI in Setup before preparing a new package. Existing packages can still be reopened.")
        self.connection_note.setWordWrap(True)
        prepare_layout.addWidget(self.connection_note)
        self.destination = QLineEdit()
        self.destination.setAccessibleName("New companion package folder")
        self.destination.setPlaceholderText("New folder outside image libraries, protected backups and SwarmUI")
        self.destination.textChanged.connect(self._invalidate_preview)
        self.choose_parent_button = compact_button(QPushButton("Choose Parent…"))
        self.choose_parent_button.clicked.connect(self._choose_parent)
        row = QHBoxLayout()
        row.addWidget(QLabel("New folder"))
        row.addWidget(self.destination, 1)
        row.addWidget(self.choose_parent_button)
        prepare_layout.addLayout(row)
        self.preview_details = QPlainTextEdit()
        self.preview_details.setReadOnly(True)
        self.preview_details.setAccessibleName("Companion package preview")
        self.preview_details.setPlaceholderText("Preview shows the exact folder, files, evidence scope and local ownership record before creation.")
        prepare_layout.addWidget(self.preview_details, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.preview_button = compact_button(QPushButton("Preview Package"))
        self.create_button = compact_button(QPushButton("Create Package"), primary=True)
        self.preview_button.clicked.connect(self.preview_package)
        self.create_button.clicked.connect(self.create_package)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.create_button)
        prepare_layout.addLayout(actions)
        self.tabs.addTab(prepare, "Create Package")

        existing = QWidget()
        existing_layout = QVBoxLayout(existing)
        explanation = QLabel(
            "Choose an existing package, then verify its files or review recovery. These read-only checks "
            "work without the original candidate or a connected SwarmUI. Recovery identifies unchanged "
            "files using CollectiGen's independent ownership record; it never removes or restores files."
        )
        explanation.setWordWrap(True)
        existing_layout.addWidget(explanation)
        self.existing_path = QLineEdit()
        self.existing_path.setAccessibleName("Existing companion package folder")
        self.existing_path.setPlaceholderText("Choose the exact package folder")
        self.existing_path.textChanged.connect(self._invalidate_existing)
        self.reopen_button = compact_button(QPushButton("Reopen Package…"))
        self.reopen_button.clicked.connect(self._choose_existing)
        row = QHBoxLayout()
        row.addWidget(QLabel("Package folder"))
        row.addWidget(self.existing_path, 1)
        row.addWidget(self.reopen_button)
        existing_layout.addLayout(row)
        self.existing_details = QPlainTextEdit()
        self.existing_details.setReadOnly(True)
        self.existing_details.setAccessibleName("Companion package verification and recovery results")
        existing_layout.addWidget(self.existing_details, 1)
        actions = QHBoxLayout()
        self.verify_button = compact_button(QPushButton("Verify Package"))
        self.recovery_button = compact_button(QPushButton("Review Recovery"))
        self.verify_button.clicked.connect(self.verify_package)
        self.recovery_button.clicked.connect(self.review_recovery)
        actions.addWidget(self.verify_button)
        actions.addWidget(self.recovery_button)
        actions.addStretch(1)
        existing_layout.addLayout(actions)
        self.tabs.addTab(existing, "Existing Package")

        self.progress = QProgressBar()
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

    def _update_actions(self):
        idle = self._task is None
        for control in (self.destination, self.choose_parent_button):
            control.setEnabled(idle and self.configured)
        for control in (self.existing_path, self.reopen_button):
            control.setEnabled(idle)
        self.preview_button.setEnabled(idle and self.configured and bool(self.destination.text().strip()))
        self.create_button.setEnabled(idle and self.configured and self.preview is not None)
        self.verify_button.setEnabled(idle and bool(self.existing_path.text().strip()))
        self.recovery_button.setEnabled(self.verify_button.isEnabled())
        self.cancel_button.setEnabled(not idle)
        self.close_button.setEnabled(idle)

    def _invalidate_preview(self, *_):
        self.preview = None
        if hasattr(self, "preview_details"):
            self.preview_details.clear()
        if hasattr(self, "close_button"):
            self._update_actions()

    def _invalidate_existing(self, *_):
        if hasattr(self, "existing_details"):
            self.existing_details.clear()
        if hasattr(self, "close_button"):
            self._update_actions()

    def _choose_parent(self):
        if self._task is not None:
            return
        path = QFileDialog.getExistingDirectory(self, "Choose Parent for New Companion Package")
        if path:
            self.destination.setText(str(Path(path) / f"companion-package-{uuid4().hex[:8]}"))

    def _choose_existing(self):
        if self._task is not None:
            return
        path = QFileDialog.getExistingDirectory(self, "Reopen Companion Package")
        if path:
            self.existing_path.setText(path)
            self.status.setText("Choose Verify Package or Review Recovery. No package files have been read yet.")

    def _selected_path(self, text):
        try:
            return guarded_path(text.strip(), self.protected_roots)
        except (ValueError, RuntimeError, OSError):
            raise ValueError("Choose an ordinary local package folder separate from image libraries and protected backups.") from None

    def _run(self, message, operation, completed):
        if self._task is not None:
            return
        self.status.setText(message)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        task = CancelableProgressCallTask(lambda report, cancel: operation(
            lambda role, done, total: report(done, total, role), cancel))
        self._task = task
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(lambda value: self._finished(value, completed))
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        self._update_actions()
        try:
            self.pool.start(task)
        except RuntimeError:
            self._failed("The operation could not be started. No worker was scheduled; retry or close this window.")

    def _progress(self, done, total, label):
        self.progress.setValue(int(100 * min(done, total) / total) if total else 0)
        self.progress.setFormat(f"{label}: %p%")

    def _finished(self, value, completed):
        self._task = None
        self._creating = False
        self.progress.setVisible(False)
        try:
            completed(value)
        except (RuntimeError, ValueError, OSError) as exc:
            self._failed(str(exc))
        self._update_actions()

    def _failed(self, message):
        self._task = None
        self.preview = None
        self.progress.setVisible(False)
        if self._creating:
            self.tabs.setCurrentIndex(1)
            message = f"{message}\nA partial package may remain at the shown folder. Verify it before relying on it."
        self._creating = False
        self.status.setText(str(message))
        self._update_actions()

    def _cancelled(self):
        self._failed("Operation cancelled. Existing files were preserved; preview again to retry.")

    def cancel_operation(self):
        if self._task is not None:
            self._task.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancelling at the next safe checkpoint. Wait for completion before closing.")

    def preview_package(self):
        if not self.preview_button.isEnabled():
            return
        self.preview = None
        self.preview_details.clear()
        target = self.destination.text()
        self._run("Checking the exact retained candidate and new destination…",
                  lambda progress, cancel: self.service.preview_qualified(self._selected_path(target),
                      progress=progress, should_cancel=cancel), self._show_preview)

    def _show_preview(self, preview):
        if Path(self.destination.text().strip()) != Path(preview.destination):
            self.preview = None
            self.status.setText("The destination changed during review. Preview the current folder again.")
            return
        self.preview = preview
        lines = [f"New package folder: {preview.destination}", "", "Files to copy:"]
        for source in preview.files:
            scope = "Reviewed packaging material only" if source.role.endswith(".csproj") else "Isolated dispatch qualification"
            lines.extend((f"{source.role} — {source.size:,} bytes", scope, f"SHA-256: {source.sha256}", ""))
        lines.extend(("Ownership receipt: saved separately in CollectiGen's existing private recovery archive.",
                      "Normal SwarmUI loading and installation remain unqualified. This creates a detached package only."))
        self.preview_details.setPlainText("\n".join(lines))
        self.status.setText("Review the exact folder and files, then choose Create Package.")

    def create_package(self):
        if not self.create_button.isEnabled():
            return
        preview = self.preview
        self.preview = None
        self._creating = True
        self.existing_path.setText(preview.destination)

        def create(progress, cancel):
            self._selected_path(preview.destination)
            return self.service.materialize(preview, progress=progress, should_cancel=cancel)

        self._run("Creating, verifying and recording the detached package…", create, self._created)

    def _created(self, receipt):
        self.existing_path.setText(receipt.directory)
        self.tabs.setCurrentIndex(1)
        self.existing_details.setPlainText(
            f"Created package: {receipt.directory}\nPackage ID: {receipt.package_id}\n\n"
            "Exact files verified. Independent ownership receipt saved.\n"
            "Nothing was installed into SwarmUI. Verify Package or Review Recovery can be used again after reopening."
        )
        self.status.setText("Package created and verified. Its exact folder is shown below.")

    def verify_package(self):
        if not self.verify_button.isEnabled():
            return
        target = self.existing_path.text()
        self.existing_details.clear()
        self._run("Verifying only the selected package…",
                  lambda _progress, cancel: self.service.verify(self._selected_path(target), should_cancel=cancel), self._verified)

    def _verified(self, report):
        if Path(self.existing_path.text().strip()) != Path(report.directory):
            self.status.setText("The selected package changed during verification. Verify the current folder again.")
            return
        state = {"verified-bytes": "Package files match their recorded hashes.",
                 "incomplete": "Package is incomplete. Preserve its files for review.",
                 "needs-review": "Package needs review. Changed, missing or unknown files were retained."}.get(report.state, "Package needs review.")
        lines = [state, f"Folder: {report.directory}", "", *[f"{r.name}: {r.state}" for r in report.files],
                 "", "Byte verification does not establish ownership or qualify runtime loading."]
        self.existing_details.setPlainText("\n".join(lines))
        self.status.setText(state)

    def review_recovery(self):
        if not self.recovery_button.isEnabled():
            return
        target = self.existing_path.text()
        self.existing_details.clear()
        self._run("Reviewing independent ownership and unchanged files…",
                  lambda _progress, cancel: self.service.preview_recovery(self._selected_path(target), should_cancel=cancel), self._recovery)

    def _recovery(self, report):
        if Path(self.existing_path.text().strip()) != Path(report.directory):
            self.status.setText("The selected package changed during recovery review. Review the current folder again.")
            return
        labels = {"unchanged-owned-preview-only": "Unchanged owned file; preview only",
                  "retain-untrusted": "Retain — independent ownership unavailable",
                  "retain-changed": "Retain — changed", "retain-unknown": "Retain — unknown file",
                  "retain-unsafe": "Retain — unsafe file", "retain-incomplete": "Retain — incomplete package"}
        lines = [f"Folder: {report.directory}", "", *[f"{r.name}: {labels.get(r.state, r.state)}" for r in report.files],
                 "", f"Unchanged owned payload files: {len(report.recovery_candidates)}.",
                 "This is a read-only recovery preview. No file removal or restoration is available."]
        self.existing_details.setPlainText("\n".join(lines))
        self.status.setText("Recovery preview complete. All files were preserved.")

    def reject(self):
        if self._task is None:
            super().reject()

    def accept(self):
        if self._task is None:
            super().accept()

    def done(self, result):
        if self._task is None:
            super().done(result)

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore()
        else:
            super().closeEvent(event)
