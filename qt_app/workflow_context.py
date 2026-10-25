"""Passive, shared answers for a workflow's current scope and next decision."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget


class WorkflowContextSummary(QWidget):
    """Render caller-owned state; never read files, select resources or run work."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Current workflow summary")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setVerticalSpacing(3)
        layout.setColumnStretch(1, 1)
        self.labels = {}
        for row, (key, title) in enumerate((
            ("using", "Using"), ("model", "Model / method"),
            ("result", "Result"), ("readiness", "Status"),
            ("next_step", "Next"),
        )):
            caption = QLabel(title)
            caption.setObjectName("mutedLabel")
            layout.addWidget(caption, row, 0, Qt.AlignTop)
            value = QLabel()
            value.setTextFormat(Qt.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setAccessibleName(title)
            value.setMinimumWidth(0)
            value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.labels[key] = value
            layout.addWidget(value, row, 1)

    def update_context(self, *, using, model, result, readiness, next_step):
        for key, value in (("using", using), ("model", model), ("result", result),
                           ("readiness", readiness), ("next_step", next_step)):
            self.labels[key].setText(str(value))
