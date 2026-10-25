"""Opt-in read-only format details for the existing image File tab."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QThreadPool, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from core.image_derivatives import MAX_SOURCE_BYTES
from core.training_source_io import read_stable_training_source
from core.image_format_inspection import inspect_image_format
from .workers import AsyncCallTask, start_qt_delivery_task


def inspection_text(report):
    """Present observations without inferring precision or conversion support."""
    frames = ("Multiple frames detected; frames were not enumerated."
              if report.multiple_frames is True else
              "Single frame detected." if report.multiple_frames is False else
              "Unknown; animation and page count were not established.")
    lines = [f"Format: {report.actual_format}",
             f"Dimensions: {report.width:,} × {report.height:,} pixels",
             f"File size: {report.source_bytes:,} bytes",
             f"Animation / pages: {frames}",
             "Transparency: " + ("Channel or transparency marker detected."
                 if report.has_alpha_channel_or_transparency else "No channel or marker detected."),
             f"Decoded color mode: {report.decoded_mode}",
             "Original bit depth: Not established by this inspection.", "",
             "Inspection limits"]
    reasons = {
        "frame_count_unassessed": "Animation and page count remain unknown; no frame was extracted.",
        "multiple_frames_inspection_only": "Animated or multi-frame content was identified without decoding every frame.",
        "source_format_inspection_only": "This format received header inspection only; its pixels were not fully checked.",
        "source_mode_inspection_only": "This color mode received header inspection only; its pixels were not fully checked.",
    }
    lines.extend(reasons.get(reason, "An additional inspection limit requires review.") for reason in report.reasons)
    lines.append("Single-frame pixels were verified and decoded." if report.single_frame_pixels_verified
                 else "Full pixel verification was not performed.")
    lines.extend(("Decoded color mode does not prove the original encoded bit depth.",
                  "Color-profile accuracy and complete Metadata preservation were not assessed.",
                  "These observations do not establish conversion or training compatibility."))
    return "\n".join(lines)


class ImageFormatDetailsDialog(QDialog):
    """Inspect only one explicitly selected source; Close drains owned work."""

    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.path = Path(image_path)
        self.setWindowTitle("Image Format Details")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(760, 600)
        self._task = None
        self._generation = 0
        self._cancel = Event()
        self._lifetime = Event()
        self.destroyed.connect(self._lifetime.set)
        self._closing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("Image format details")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.source = QLabel(self.path.name)
        self.source.setTextFormat(Qt.PlainText)
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        intro = QLabel("Inspect this image's actual format and limitations. The image, Metadata and Image Knowledge stay unchanged.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel("Choose Inspect to read this selected image.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setAccessibleName("Image format inspection status")
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Image format observations and limits")
        layout.addWidget(self.details, 1)
        snapshot = QLabel("Results describe the bytes read during this inspection. Inspect again after changing the file.")
        snapshot.setWordWrap(True)
        snapshot.setObjectName("mutedLabel")
        layout.addWidget(snapshot)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.inspect_button = self.buttons.addButton("Inspect", QDialogButtonBox.ActionRole)
        self.inspect_button.setProperty("primary", True)
        self.cancel_button = self.buttons.addButton("Cancel Inspection", QDialogButtonBox.ActionRole)
        self.cancel_button.setEnabled(False)
        for button in self.buttons.buttons():
            button.setAutoDefault(False)
        self.inspect_button.clicked.connect(self.start_inspection)
        self.cancel_button.clicked.connect(self.cancel_inspection)
        self.buttons.rejected.connect(self.close)
        layout.addWidget(self.buttons)
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._settle)

    def start_inspection(self):
        if self._closing or self._task is not None:
            return
        self.details.clear()
        self._generation += 1
        generation = self._generation
        self._cancel = Event()
        cancelled, lifetime, path = self._cancel, self._lifetime, self.path
        def inspect():
            if cancelled.is_set() or lifetime.is_set():
                raise InterruptedError()
            # Reuse the byte-bounded, stable, regular-file source reader.
            # No companions, directory discovery, or pixel writers are used.
            raw, _identity = read_stable_training_source(path, max_bytes=MAX_SOURCE_BYTES)
            report = inspect_image_format(raw,
                should_continue=lambda: not cancelled.is_set() and not lifetime.is_set())
            return generation, report
        self._task = AsyncCallTask(inspect)
        self._task.signals.complete.connect(self._deliver)
        self.inspect_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("Inspecting this image… You can cancel or close; a file read or decoder call may need to finish first.")
        self._poll.start()
        start_qt_delivery_task(QThreadPool.globalInstance(), self._task)

    @Slot(object)
    def _deliver(self, result):
        generation, report = result
        if self._closing or self._cancel.is_set() or generation != self._generation:
            return
        self.details.setPlainText(inspection_text(report))
        self.status.setText("Inspection complete. Review the observations and limits below.")

    def cancel_inspection(self):
        self._cancel.set()
        if self._task is not None:
            self._task.cancel()
        self.cancel_button.setEnabled(False)
        self.status.setText("Cancelling inspection after the current read or decoder call finishes…")

    def _settle(self):
        if self._task is None or not self._task.is_finished():
            return
        if self._closing:
            self._task = None
            self.close()
            return
        if self._cancel.is_set():
            self.status.setText("Inspection cancelled. You can inspect again or close.")
        elif self._task.state == "failed":
            self.status.setText("Inspection unavailable. The file may have changed, be unreadable, exceed inspection limits, or use an unsupported encoding. Check the file and try again.")
        self._task = None
        self.cancel_button.setEnabled(False)
        self.inspect_button.setEnabled(True)
        self._poll.stop()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        self._closing = True
        self._cancel.set()
        if self._task is not None and not self._task.is_finished():
            self._task.cancel()
            self.inspect_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.status.setText("Closing after the current image inspection stops…")
            self._poll.start()
            event.ignore()
            return
        self._poll.stop()
        self._lifetime.set()
        event.accept()


def file_details_panel(overview, image_path, parent=None):
    """Wrap the original File overview unchanged; perform no read until Inspect."""
    panel = QWidget(parent)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(overview, 1)
    panel.inspect_format_button = QPushButton("Inspect Image Format…")
    panel.inspect_format_button.setAutoDefault(False)
    panel.inspect_format_button.setAccessibleName("Inspect selected image format")
    panel._format_dialog = None
    path = Path(image_path)
    def open_details():
        dialog = panel._format_dialog
        if dialog is not None and isValid(dialog):
            dialog.show()
            dialog.raise_()
            return
        panel._format_dialog = ImageFormatDetailsDialog(path, panel)
        panel._format_dialog.show()
    panel.inspect_format_button.clicked.connect(open_details)
    layout.addWidget(panel.inspect_format_button)
    return panel
