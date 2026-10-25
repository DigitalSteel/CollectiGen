"""One compact decision surface for a saved Add Images selection."""
from __future__ import annotations

from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout

from core.ingest_add_batch import AddBatchState

from .components import DisclosureButton
from .modal_feedback import FeedbackEnabledDialog


class SavedAddBatchDialog(FeedbackEnabledDialog):
    feedback_workflow = "Saved Add Images"

    def __init__(self, state: AddBatchState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.choice = ""
        self.setWindowTitle("Review Saved Add Images")
        self.resize(720, 330)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"{state.completed:,} completed  •  {state.remaining:,} not started  •  "
            f"{state.held:,} need recovery\n"
            f"Saved method: {'Copy' if state.action == 'copy' else 'Move'} to Ingest. "
            "Completed images will not be added again."
        )
        summary.setWordWrap(True)
        summary.setObjectName("sectionTitle")
        summary.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(summary)
        guidance = QLabel(
            "Review the exact remaining files and destination before continuing. "
            "Closing this window keeps the saved selection."
            if state.remaining and not state.held else
            "One or more image families have an uncertain attempt. Do not retry them; "
            "review the recorded recovery details. Unstarted files stay saved."
            if state.held else
            "Every family has a completed receipt. Finish this saved batch without copying or moving again."
        )
        guidance.setWordWrap(True)
        guidance.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(guidance)
        exact = QPlainTextEdit()
        exact.setReadOnly(True)
        exact.setAccessibleName("Exact saved Add Images source paths and destinations")
        exact.setPlainText("\n".join(
            f"{item.state.upper()} — {item.source} → {item.destination}\n"
            f"{item.reason or 'Saved family identity retained.'}"
            for item in state.items
        ))
        disclosure = DisclosureButton("Show Exact Saved Files", "Hide Exact Saved Files", exact)
        layout.addWidget(disclosure)
        layout.addWidget(exact)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.review_button = buttons.addButton(
            "Review Remaining Images", QDialogButtonBox.AcceptRole,
        )
        self.review_button.clicked.connect(lambda: self._choose("review"))
        self.review_button.setVisible(bool(state.remaining and not state.held))
        self.discard_button = buttons.addButton(
            "Discard Unstarted Selection…", QDialogButtonBox.ActionRole,
        )
        self.discard_button.clicked.connect(lambda: self._choose("discard"))
        self.discard_button.setVisible(bool(state.remaining and not state.held))
        self.discard_button.setToolTip(
            "Discard every unstarted saved family without removing completed files or History. "
            "To use intentionally changed files, choose Add Images again for a fresh preview."
        )
        self.finish_button = buttons.addButton(
            "Finish Saved Batch", QDialogButtonBox.AcceptRole,
        )
        self.finish_button.clicked.connect(lambda: self._choose("finish"))
        self.finish_button.setVisible(not state.remaining and not state.held)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _choose(self, value: str) -> None:
        self.choice = value
        self.accept()
