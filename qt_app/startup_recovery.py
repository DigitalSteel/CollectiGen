"""Read-only assistance when verified Knowledge Pack recovery holds startup."""
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from version import PRODUCT_NAME, VERSION


class StartupRecoveryDialog(QDialog):
    def __init__(self, reason: str, recovery_location: str, *, reason_code: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Knowledge Pack recovery needs attention")
        self.resize(680, 380)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        for text in (
            "CollectiGen could not verify an interrupted Knowledge Pack import.",
            "The workspace has not opened. Keep the recovery records and backups unchanged.",
            "Copy the diagnostic below for assistance. Restarting alone may not resolve this hold.",
        ):
            label = QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.diagnostic = (
            f"{PRODUCT_NAME} {VERSION}\nReason: {reason_code}\n"
            f"Details: {reason}\nRecovery records: {recovery_location}"
        )
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText(self.diagnostic)
        self.details.setAccessibleName("Startup recovery diagnostic")
        self.details.setMinimumHeight(90)
        self.details.setMaximumHeight(160)
        layout.addWidget(self.details)
        self.status = QLabel("Copying is optional; nothing is uploaded or changed.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy Diagnostic Summary")
        self.copy_button.setToolTip("Copy only the displayed diagnostic; no Knowledge or backup contents.")
        self.copy_button.clicked.connect(self._copy)
        actions.addWidget(self.copy_button)
        actions.addStretch()
        self.close_button = QPushButton("Close CollectiGen")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def _copy(self):
        QApplication.clipboard().setText(self.diagnostic)
        self.status.setText("Diagnostic copied. You choose whether to share it for assistance.")
