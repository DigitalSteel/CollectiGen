"""Goal-led Generate surface around the canonical retained request workspace.

This widget presents actions only. It grants no native, filesystem or execution
authority; the admitted module window connects its signals to existing owners.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from qt_app.generation_page import GenerationPage
from qt_app.components import PageHeader


class GenerateSurface(QWidget):
    new_requested = Signal(str)
    resume_requested = Signal(str)
    create_setup_requested = Signal()
    use_setup_requested = Signal()
    results_requested = Signal()
    video_setup_requested = Signal()
    video_recovery_requested = Signal()
    manager_requested = Signal(str)
    help_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Generate workflow")
        layout = QVBoxLayout(self)
        self.header = PageHeader("Generate", "Create or resume an image or video request.")
        layout.addWidget(self.header)
        actions = QHBoxLayout()
        self.media = QComboBox()
        self.media.setAccessibleName("Generation media")
        self.media.addItem("Image", "image")
        self.media.addItem("Video", "video")
        self.new = QPushButton("New Image…")
        self.new.setProperty("primary", True)
        self.new.clicked.connect(lambda: self.new_requested.emit(self.media.currentData()))
        self.resume = QPushButton("Resume Draft…")
        self.resume.clicked.connect(lambda: self.resume_requested.emit(self.media.currentData()))
        self.more = QToolButton()
        self.more.setText("More…")
        self.more.setAccessibleName("Other generation workflows and Manager connections")
        self.more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.more)
        self._new_action = menu.addAction("Start Another Request…")
        self._new_action.triggered.connect(lambda: self.new_requested.emit(self.media.currentData()))
        self._resume_action = menu.addAction("Resume Another Draft…")
        self._resume_action.triggered.connect(lambda: self.resume_requested.emit(self.media.currentData()))
        menu.addAction("Use Saved Setup…", self.use_setup_requested.emit)
        menu.addAction("Create Generation Setup…", self.create_setup_requested.emit)
        menu.addAction("Review Saved Results…", self.results_requested.emit)
        menu.addSeparator()
        menu.addAction("Review Video Components…", self.video_setup_requested.emit)
        menu.addAction("Recover Video Setup…", self.video_recovery_requested.emit)
        menu.addSeparator()
        menu.addAction("Save and Return to Manager Connections…",
                       lambda: self.manager_requested.emit("connections"))
        menu.addAction("Save and Return to Manager Setup…",
                       lambda: self.manager_requested.emit("settings"))
        self.more.setMenu(menu)
        self.help = QPushButton('Help')
        self.help.setAccessibleName('Help for Generate')
        self.help.clicked.connect(self.help_requested.emit)
        for widget in (self.media, self.new, self.resume, self.more, self.help):
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.status = QLabel("Choose New Image to start, or resume a saved draft. Nothing runs until you submit.")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Generation workflow status")
        layout.addWidget(self.status)
        self.page = GenerationPage(self, show_launchers=False)
        layout.addWidget(self.page, 1)
        self.page.active_changed.connect(self._active_changed)
        self.media.currentIndexChanged.connect(self._media_changed)
        self._media_changed()

    def _media_changed(self, *_args):
        media = self.media.currentData()
        self.new.setText("New Video…" if media == "video" else "New Image…")
        index = self.page.media_choice.findData(media)
        if index >= 0:
            self.page.media_choice.setCurrentIndex(index)
        self._active_changed(self.page.active_workspace())

    def _active_changed(self, workspace):
        media = self.page.media_choice.currentData()
        index = self.media.findData(media)
        if index >= 0 and index != self.media.currentIndex():
            blocked = self.media.blockSignals(True)
            self.media.setCurrentIndex(index)
            self.media.blockSignals(blocked)
        active = workspace is not None
        self.new.setVisible(not active)
        self.resume.setVisible(not active)
        self.media.setVisible(not active)
        self._new_action.setVisible(active)
        self._resume_action.setVisible(active)
        self.status.setVisible(not active)
        self.header.setVisible(not active)
        self.page.header.hide()
        self.page.empty.hide()

    def set_status(self, message):
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def set_closing(self):
        for widget in (self.media, self.new, self.resume, self.more, self.help):
            widget.setEnabled(False)
