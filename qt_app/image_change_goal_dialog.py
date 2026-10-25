"""One source-image goal choice; the existing workspaces own every effect."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout,
)
from .status_notifications import FooterMessageBox as QMessageBox


class ImageChangeGoalDialog(QDialog):
    _CHOICES = (
        ("faithful", "Make it larger without redrawing it",
         "Resize existing pixels. This cannot recover missing detail."),
        ("detail", "Enlarge with an AI detail model",
         "A local ESRGAN upscaler may invent fine detail. It is not a general blur, noise, or damage repair tool; a compatible model is required."),
        ("close", "Make a close variation",
         "Start a new image from this one with a lower-change starting strength. Details may still change."),
        ("transform", "Change the image substantially",
         "Start a new image with a higher-change starting strength. Identity, text, pose, and style may change."),
    )

    def __init__(self, source: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose What to Do with This Image")
        self.resize(660, 430)
        layout = QVBoxLayout(self)
        heading = QLabel("What would you like to do with this image?")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        scope = QLabel(f"Inspected image: {source.name}")
        scope.setTextFormat(Qt.PlainText)
        scope.setWordWrap(True)
        layout.addWidget(scope)
        self.choices = QButtonGroup(self)
        self.buttons = {}
        for index, (key, title, explanation) in enumerate(self._CHOICES):
            button = QRadioButton(title)
            button.setAccessibleDescription(explanation)
            self.choices.addButton(button, index)
            self.buttons[key] = button
            layout.addWidget(button)
            detail = QLabel(explanation)
            detail.setWordWrap(True)
            detail.setContentsMargins(24, 0, 0, 5)
            layout.addWidget(detail)
        self.buttons["faithful"].setChecked(True)
        assurance = QLabel(
            "Continue opens a separate review; it does not change this image or start a job. "
            "Original pixels and reviewed Image Knowledge stay unchanged."
        )
        assurance.setWordWrap(True)
        layout.addWidget(assurance)
        layout.addStretch(1)
        self.actions = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.continue_button = self.actions.addButton("Continue", QDialogButtonBox.AcceptRole)
        self.actions.accepted.connect(self.accept)
        self.actions.rejected.connect(self.reject)
        layout.addWidget(self.actions)

    def selected_intent(self) -> str:
        checked = self.choices.checkedId()
        return self._CHOICES[checked][0]


def choose_image_change_goal(parent, source: Path) -> str | None:
    dialog = ImageChangeGoalDialog(Path(source), parent)
    return dialog.selected_intent() if dialog.exec() == QDialog.Accepted else None


def open_image_change_goal(parent, source: Path) -> None:
    """Route one reviewed choice to its existing, separate authority."""
    source = Path(source)
    if not source.is_file():
        QMessageBox.information(parent, "Inspected image unavailable",
            "This image is no longer at its inspected location. Refresh the gallery and choose an available image.")
        return
    intent = choose_image_change_goal(parent, source)
    if intent is not None and not source.is_file():
        QMessageBox.information(parent, "Inspected image unavailable",
            "This image changed location while you were choosing a goal. Refresh the gallery and choose it again.")
        return
    if intent in {"faithful", "detail"}:
        from .resolution_dialog import open_resolution_workspace
        open_resolution_workspace(parent, paths=(source,),
            initial_method="lanczos" if intent == "faithful" else "esrgan")
    elif intent in {"close", "transform"}:
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(parent, paths=(source,), remix=True,
            initial_image_intent=intent)
