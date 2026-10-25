"""Keep a reviewed setup editor open until its local save outcome is known."""
from __future__ import annotations

from .modal_feedback import FeedbackEnabledDialog


class SetupSaveDialog(FeedbackEnabledDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.save_in_progress = False

    def reject(self):
        if not self.save_in_progress:
            super().reject()

    def closeEvent(self, event):
        if self.save_in_progress:
            event.ignore()
        else:
            super().closeEvent(event)
