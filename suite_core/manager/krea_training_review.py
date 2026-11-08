"""Read-only Krea package review owned by the Manager window."""
from threading import Event

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout,
)

from .resource_executor import ResourceExecutorOwner


class KreaTrainingReviewDialog(QDialog):
    def __init__(self, package_path, registry_path, parent):
        super().__init__(parent)
        self.setWindowTitle("Krea 2 Training Package Review")
        self.resize(850, 630)
        self._executor = ResourceExecutorOwner()
        self._cancel = Event()

        layout = QVBoxLayout(self)
        heading = QLabel("Review Krea 2 compatibility")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        note = QLabel("This review reads the selected package and checks its exact registered base when available. It cannot start training.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel("Checking the selected package…")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        self.sections = []
        for title in ("Readiness", "Settings", "Captions"):
            section = QPlainTextEdit()
            section.setReadOnly(True)
            section.setAccessibleName(f"Krea 2 {title.lower()}")
            self.tabs.addTab(section, title)
            self.sections.append(section)
        self.tabs.setEnabled(False)
        layout.addWidget(self.tabs, 1)
        self.return_button = QPushButton("Cancel Check")
        self.return_button.setMinimumWidth(self.return_button.fontMetrics().horizontalAdvance('Return to Applications') + 32)
        self.return_button.clicked.connect(self.reject)
        layout.addWidget(self.return_button, 0, Qt.AlignRight)

        self._future = self._executor.submit(self._review, package_path, registry_path)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._settle)
        self._timer.start(50)

    def _review(self, package_path, registry_path):
        from core.krea_training_compatibility import KreaTrainingCompatibilityService
        from core.model_resource_registry import ModelResourceRegistry
        from core.training_package import TrainingPackageService, TrainingPackageVerificationLimits

        limits = TrainingPackageVerificationLimits()
        verification = TrainingPackageService().verify(package_path, should_cancel=self._cancel.is_set, limits=limits)
        if verification.inspection_limited:
            raise ValueError("The package exceeds the review limits. Choose a smaller package.")
        if not verification.valid:
            raise ValueError("The selected package could not be verified. Reopen a current Krea 2 package.")
        return KreaTrainingCompatibilityService().report(
            verification, ModelResourceRegistry(registry_path), should_cancel=self._cancel.is_set, limits=limits,
        )

    def _settle(self):
        if not self._future.done() or self._executor.close_held:
            return
        self._timer.stop()
        if self._cancel.is_set():
            super().reject()
            return
        self.return_button.setText("Return to Applications")
        try:
            report = self._future.result()
        except ValueError as exc:
            self.status.setText(f"Compatibility review unavailable. {exc}")
            return
        except Exception:
            self.status.setText("Compatibility review could not finish. Return to Applications and select a current package.")
            return
        base = ("Exact registered base bytes verified" if report.base_status == "identity_verified"
                else "Exact registered base unavailable")
        self.status.setText(f"{base}. Training readiness is unverified.")
        sections = (
            f"Package fingerprint: {report.package_fingerprint}\nRegistered base: {report.base_resource_id}\n\n"
            + "\n\n".join(f"{item.summary}\n{item.reason}\n{item.next_action}" for item in report.requirements),
            "\n\n".join(f"{item.field} [{item.disposition}]\nRequested: {item.requested}\n{item.reason}"
                       for item in report.settings),
            "\n\n".join(f"{item.stable_id} ({item.role}, {item.effective_exposures} exposures)\n{item.caption}"
                       for item in report.images),
        )
        for widget, text in zip(self.sections, sections):
            widget.setPlainText(text)
        self.tabs.setEnabled(True)

    def reject(self):
        if self._executor.close_held:
            self._cancel.set()
            self._executor.begin_close()
            self.status.setText("Stopping the package check. Waiting for the review worker to finish…")
            self.return_button.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event):
        if self._executor.close_held:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
