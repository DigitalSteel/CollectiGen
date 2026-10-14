"""Persistent shell for canonical generation request workspaces."""
from __future__ import annotations

from .help_content import workspace_help

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from .components import PageHeader


class GenerationPage(QWidget):
    new_requested = Signal()
    create_setup_requested = Signal()
    use_setup_requested = Signal()
    active_changed = Signal(object)
    background_work_finished = Signal()
    draft_save_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Generate")
        self._dialogs = []
        self._tasks = {}
        self._media = {}
        self._shutdown_requested = False
        self._shutdown_dispatching = False
        self._draft_close_pending = False
        layout = QVBoxLayout(self)
        self.header = PageHeader("Generate", "Create and review a local generation request",
            help_text=workspace_help("generate"))
        layout.addWidget(self.header)
        actions = QHBoxLayout()
        self.media_choice = QComboBox()
        self.media_choice.setAccessibleName("Media type")
        self.media_choice.addItem("Image", "image")
        self.media_choice.addItem("Video", "video")
        self.task_choice = QComboBox()
        self.task_choice.setAccessibleName("Generation task")
        self.task_choice.addItem("New", "new")
        self.task_choice.addItem("Remix", "remix")
        self.task_choice.addItem("Upscale", "upscale")
        self.begin_button = QPushButton("Start New Image…")
        self.begin_button.setProperty("primary", True)
        self.begin_button.clicked.connect(self._begin)
        actions.addWidget(self.media_choice)
        self.task_choice.hide()
        actions.addWidget(self.begin_button)
        self.resume_button = QPushButton("Resume Image Draft…")
        self.resume_button.setToolTip(
            "Choose a managed saved image request before opening its workspace. Cancel leaves current drafts unchanged."
        )
        self.resume_button.clicked.connect(self._resume_image_draft)
        actions.addWidget(self.resume_button)
        self.open_video_draft_button = QPushButton("Open Video Draft…")
        self.open_video_draft_button.setToolTip(
            "Choose one saved video request before opening a separate workspace."
        )
        self.open_video_draft_button.clicked.connect(self._open_video_draft)
        actions.addWidget(self.open_video_draft_button)
        self.use_setup_button = QPushButton("Use Saved Setup…")
        self.use_setup_button.setToolTip(
            "Choose a saved Generation Setup, then review its changes in a separate new image draft."
        )
        self.use_setup_button.clicked.connect(self.use_setup_requested.emit)
        actions.addWidget(self.use_setup_button)
        self.create_setup_button = QPushButton("Create Generation Setup…")
        self.create_setup_button.setToolTip("Make reusable prompt, model, LoRA and setting choices without starting an image request.")
        self.create_setup_button.clicked.connect(self.create_setup_requested.emit)
        actions.addWidget(self.create_setup_button)
        self.draft_choice = QComboBox()
        self.draft_choice.setAccessibleName("Generation drafts")
        self.draft_choice.setMaximumWidth(420)
        self.draft_choice.currentIndexChanged.connect(self._choose_draft)
        self.draft_choice.hide()
        actions.addWidget(self.draft_choice, 1)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.task_status = QLabel()
        self.task_status.setWordWrap(True)
        self.task_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.task_status)
        self.empty_panel = QWidget()
        empty_layout = QVBoxLayout(self.empty_panel)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        self.empty = QLabel()
        self.empty.setWordWrap(True)
        empty_layout.addWidget(self.empty, 0, Qt.AlignTop)
        empty_layout.addStretch(1)
        layout.addWidget(self.empty_panel, 1)
        self.workspaces = QStackedWidget()
        layout.addWidget(self.workspaces, 1)
        self.media_choice.currentIndexChanged.connect(self._update_task)
        self.task_choice.currentIndexChanged.connect(self._update_task)
        self._update_task()

    def _update_task(self):
        media, task = self.media_choice.currentData(), self.task_choice.currentData()
        if media == "image":
            message = "Create an image from a prompt. Review the model and local generator before starting."
            enabled = True
        else:
            message = "Create a short silent video from a prompt or one image. Review exact Wan2.2 models, timing and an isolated local runtime."
            enabled = True
        self.task_status.setText(message)
        self.begin_button.setEnabled(True)
        self.begin_button.setText("Start New Image…" if media == "image" else "Start New Video…")
        self.begin_button.setToolTip("Create a separate draft; keep existing drafts.")
        self.resume_button.setVisible(media == "image")
        self.open_video_draft_button.setVisible(media == "video")
        self.use_setup_button.setVisible(media == "image")
        self.create_setup_button.setVisible(media == "image")
        active = self.workspaces.currentWidget()
        matching = active is not None and self._media.get(active) == media
        self.workspaces.setVisible(matching)
        self.task_status.setVisible(matching)
        self.empty_panel.setVisible(not matching)
        self.empty.setText(
            "Start a new video request or open a saved video draft. Review exact Wan2.2 models, timing and the local runtime before starting."
            if media == "video" else
            "Resume a saved image draft, start a new request, or use or create a reusable setup. "
            "To change an existing image, inspect it in Dataset and choose Remix This Image from Image Actions. "
            "Review the model and local generator before starting."
        )
        self.active_changed.emit(self.active_workspace())

    def _begin(self):
        if self.media_choice.currentData() == "video":
            from .video_generation_workflow import open_video_generation_workspace
            open_video_generation_workspace(self)
        else:
            self.new_requested.emit()

    def _resume_image_draft(self):
        from .generation_workflow import choose_generation_draft
        choose_generation_draft(self)

    def _open_video_draft(self):
        from .video_generation_workflow import choose_video_generation_draft
        choose_video_generation_draft(self)

    def confirm_draft_loss(self):
        self._draft_close_pending = False
        for dialog in tuple(self._dialogs):
            busy = getattr(dialog, "has_active_background_work", lambda: False)()
            if busy: continue
            resume = getattr(dialog, "resume_after_cancelled_shutdown", None)
            if callable(resume): resume()
            confirm = getattr(dialog, "confirm_draft_loss", None)
            if callable(confirm) and not confirm():
                self._draft_close_pending = bool(getattr(dialog, "_draft_close_pending", False))
                return False
        return True

    def _draft_saved(self):
        if self._draft_close_pending:
            self._draft_close_pending = False
            self.draft_save_finished.emit()

    def add_workspace(self, dialog, *, label="New Image", task="new", media_type="image"):
        # The dialog remains the sole owner of request, job and result state.
        dialog.setParent(self.workspaces)
        dialog.setWindowFlags(Qt.Widget)
        self._dialogs.append(dialog)
        self._tasks[dialog] = task
        self._media[dialog] = media_type
        self.workspaces.addWidget(dialog)
        self.draft_choice.addItem(label)
        self.draft_choice.show()
        self.draft_choice.setCurrentIndex(len(self._dialogs) - 1)
        self.workspaces.setCurrentWidget(dialog)
        self.empty_panel.hide()
        self.task_status.show()
        dialog.workspace_closed.connect(lambda d=dialog: self.remove_workspace(d))
        finished = getattr(dialog, "background_work_finished", None)
        if finished is not None:
            finished.connect(self._background_changed)
        saved = getattr(dialog, "draft_save_finished", None)
        if saved is not None: saved.connect(self._draft_saved)
        dialog.show()

    def has_active_background_work(self):
        return any(callable(check := getattr(dialog, "has_active_background_work", None)) and check()
                   for dialog in self._dialogs)

    def retained_workspace(self, media_type):
        """Return the active or latest retained draft for this media kind."""
        current = self.workspaces.currentWidget()
        if current in self._dialogs and self._media.get(current) == media_type:
            return current
        return next((dialog for dialog in reversed(self._dialogs)
                     if self._media.get(dialog) == media_type), None)

    def request_background_shutdown(self):
        self._shutdown_requested = True
        self.begin_button.setEnabled(False)
        self._shutdown_dispatching = True
        try:
            for dialog in tuple(self._dialogs):
                shutdown = getattr(dialog, "request_background_shutdown", None)
                if callable(shutdown):
                    shutdown()
        finally:
            self._shutdown_dispatching = False
        self._background_changed()
        return not self.has_active_background_work()

    def _background_changed(self):
        if self._shutdown_requested and not self._shutdown_dispatching and not self.has_active_background_work():
            self.background_work_finished.emit()

    def resume_after_cancelled_shutdown(self):
        if self.has_active_background_work():
            return False
        for dialog in tuple(self._dialogs):
            resume = getattr(dialog, "resume_after_cancelled_shutdown", None)
            if callable(resume):
                resume()
        self._shutdown_requested = False
        self._update_task()
        return True

    def remove_workspace(self, dialog):
        if dialog not in self._dialogs:
            return
        index = self._dialogs.index(dialog)
        self._dialogs.pop(index)
        self._tasks.pop(dialog, None)
        self._media.pop(dialog, None)
        self.draft_choice.removeItem(index)
        if not self._dialogs:
            self.draft_choice.hide()
        self.workspaces.removeWidget(dialog)
        dialog.deleteLater()
        self._update_task()
        self.active_changed.emit(self.active_workspace())

    def _choose_draft(self, index):
        if 0 <= index < len(self._dialogs):
            self.workspaces.setCurrentWidget(self._dialogs[index])
            self.media_choice.blockSignals(True)
            self.task_choice.blockSignals(True)
            self.media_choice.setCurrentIndex(self.media_choice.findData(self._media[self._dialogs[index]]))
            self.task_choice.setCurrentIndex(max(0, self.task_choice.findData(self._tasks[self._dialogs[index]])))
            self.media_choice.blockSignals(False)
            self.task_choice.blockSignals(False)
            self._update_task()
            self.active_changed.emit(self._dialogs[index])

    def show_workspace(self, dialog):
        if dialog in self._dialogs:
            self.draft_choice.setCurrentIndex(self._dialogs.index(dialog))
            self._choose_draft(self._dialogs.index(dialog))
            dialog.show()

    def active_workspace(self):
        widget = self.workspaces.currentWidget()
        return widget if widget in self._dialogs and self._media.get(widget) == self.media_choice.currentData() else None
