"""One explicit media inventory, reusing typed video review and cancellation."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QListWidgetItem, QPushButton

from core.media_inventory import scan_media
from .video_ingest_dialog import VideoIngestDialog
from .components import ContextHelpButton
from .help_content import section_help


class MediaLibraryDialog(VideoIngestDialog):
    def __init__(self, state, roots, *, parent=None, operation_owner="ingest"):
        self._inventory = None
        self._filtered = []
        self._roots = tuple((label, Path(path).absolute()) for label, path in roots)
        super().__init__(state, self._roots[0][1], parent=parent, operation_owner=operation_owner)
        self.setWindowTitle("Review Library Media")
        self.title_label.setText("Images and videos")
        self.explanation_label.setText(
            "Inspect one library and its subfolders. Images and videos keep separate review workflows. "
            "Unexpected files remain in place; no scan confirms knowledge or adds videos to training. "
            "Close to return to your unchanged image selection.")
        controls = QHBoxLayout()
        self.library_combo = QComboBox()
        for label, path in self._roots:
            self.library_combo.addItem(label, str(path))
        self.library_combo.setAccessibleName("Library to inspect")
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All media", "Images", "Videos", "Needs attention"])
        self.filter_combo.setAccessibleName("Media type or issues")
        controls.addWidget(QLabel("Library")); controls.addWidget(self.library_combo, 1)
        controls.addWidget(self.filter_combo)
        self.subfolder_button = QPushButton("Choose Subfolder…")
        self.subfolder_button.clicked.connect(self._choose_subfolder)
        controls.addWidget(self.subfolder_button)
        self.help_button = ContextHelpButton("Review Library Media", section_help("review-media"), self)
        self.help_button.setShortcut("F1")
        controls.addWidget(self.help_button)
        self.layout().insertLayout(2, controls)
        self.inventory_summary = QLabel("Inspecting media…")
        self.inventory_summary.setWordWrap(True)
        self.layout().insertWidget(3, self.inventory_summary)
        self.library_combo.currentIndexChanged.connect(self._change_library)
        self.filter_combo.currentIndexChanged.connect(lambda: self._render(0))
        self.refresh_button.clicked.disconnect()
        self.refresh_button.clicked.connect(self.refresh_inventory)

    def _change_library(self):
        self.root = Path(self.library_combo.currentData())
        self.refresh_inventory()

    def _choose_subfolder(self):
        folder = QFileDialog.getExistingDirectory(self, "Inspect a library subfolder", str(self.root))
        if not folder:
            return
        path = Path(folder).absolute()
        from core.verified_transfer import VerifiedTransferEngine
        if VerifiedTransferEngine._redirected(path) or not any(path.is_relative_to(root) for _label, root in self._roots):
            self.status.setText("Choose an original subfolder inside one of these libraries.")
            return
        index = self.library_combo.findData(str(path))
        if index < 0:
            self.library_combo.addItem(str(path), str(path))
            index = self.library_combo.count() - 1
        self.library_combo.setCurrentIndex(index)

    def refresh_inventory(self):
        if self._task is not None or self._closing:
            return
        self._inventory = None
        self.inventory_summary.setText("Inspecting media…")
        self._record = None
        self._selected_path = None
        self.rows.clear(); self.details.clear()
        if self._player is not None:
            self._player.close()
        self.load_page(0)

    def load_page(self, offset=0):
        if self._inventory is not None:
            self._render(offset)
            return
        root = self.root
        def loaded(inventory):
            self._inventory = inventory
            self._render(0)
        self._run("Inspecting media headers (no files change)…",
                  lambda cancel: scan_media(root, should_cancel=cancel), loaded)

    def _render(self, offset):
        if self._inventory is None:
            return
        mode = self.filter_combo.currentText()
        self._filtered = [row for row in self._inventory.rows if (
            mode == "All media" or (mode == "Images" and row.kind in {"Image", "Animation"})
            or (mode == "Videos" and row.kind == "Video")
            or (mode == "Needs attention" and not (row.image_ready or row.video_ready)))]
        self._offset = offset
        self._next_offset = offset + 100 if offset + 100 < len(self._filtered) else None
        self.rows.clear()
        self.details.clear()
        for row in self._filtered[offset:offset + 100]:
            item = QListWidgetItem(f"{row.path.relative_to(self.root)}\n{row.kind} · {row.status}")
            item.setData(Qt.UserRole, str(row.path))
            item.setData(Qt.UserRole + 1, row)
            item.setToolTip(row.detail)
            self.rows.addItem(item)
        all_rows = self._inventory.rows
        images = sum(row.kind in {"Image", "Animation"} for row in all_rows)
        videos = sum(row.kind == "Video" for row in all_rows)
        attention = sum(not (row.image_ready or row.video_ready) for row in all_rows)
        self.inventory_summary.setText(
            f"{'Inspected' if self._inventory.complete else 'Partial inspection'}: {images:,} images · "
            f"{videos:,} videos · {attention:,} need attention. "
            f"Showing {len(self._filtered[offset:offset + 100]):,} of {len(self._filtered):,} matching files.")
        self.status.setText(" ".join(self._inventory.issues) or "Select a file to inspect its type and next step.")
        if not self._filtered:
            self.details.setPlainText("No media matches this view. Choose another filter or library, or refresh after adding files.")
        self._update_actions()

    def _selection_changed(self, selected, previous):
        if selected is not None:
            row = selected.data(Qt.UserRole + 1)
            if row.video_ready:
                return super()._selection_changed(selected, previous)
            if self._player is not None:
                self._player.close()
            self._record = None
            self._selected_path = None
            self.details.setPlainText(f"{row.path}\n\n{row.kind} · {row.status}\n\n{row.detail}")
            self._update_actions()
            return
        super()._selection_changed(selected, previous)

    def _update_actions(self, *args):
        super()._update_actions(*args)
        if hasattr(self, "library_combo"):
            idle = self._task is None and not self._closing and self._player is None
            self.library_combo.setEnabled(idle)
            self.filter_combo.setEnabled(idle)
            self.subfolder_button.setEnabled(idle)
            self.refresh_button.setEnabled(idle)
            self.rows.setEnabled(idle)
            self.previous_button.setEnabled(idle and self._offset > 0)
            self.next_button.setEnabled(idle and self._next_offset is not None)


def open_media_library(owner, roots, *, operation_owner="ingest"):
    """Keep one review per page and integrate its safe shutdown with the page."""
    if getattr(owner, "_shutdown_requested", False):
        return
    current = getattr(owner, "_media_library_dialog", None)
    if current is not None:
        current.show(); current.raise_()
        return current
    roots = tuple(roots)
    if not roots:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(owner, "Choose a library", "Configure an available library in Setup before inspecting its media.")
        return
    dialog = MediaLibraryDialog(owner.state, roots, parent=owner, operation_owner=operation_owner)
    owner._media_library_dialog = dialog
    dialog.background_work_finished.connect(owner._notify_background_work_finished)
    def finished(_result):
        owner._media_library_dialog = None
        owner._notify_background_work_finished()
    dialog.finished.connect(finished)
    dialog.show()
    return dialog
