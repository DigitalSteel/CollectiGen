"""Explicit local export of loaded prompt choices; ordinary draft IO owns saving."""
from pathlib import Path
from threading import Event
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from .status_notifications import FooterMessageBox as QMessageBox
from .components import IndeterminateActivityBar
from .workers import CoordinatedCallTask
from core.generation_prompt_catalog import capture_prompt_catalog, catalog_draft
from core.generation_planning import save_plan


def _ready(page):
    if page.editor_context.standalone:
        return False
    if not page.allow_external_knowledge_action("exporting prompt choices"):
        return False
    page._flush_tag_save()
    return not (page._tag_save_pending or page._modifier_policy_save_task is not None
                or page.category_workspace.has_unsaved_changes() or page.has_unsaved_order_changes())


class PromptCatalogExportDialog(QDialog):
    """One coordinated writer stays retained through normal save/recovery settlement."""
    def __init__(self, page, catalog):
        super().__init__(page)
        self.page, self.catalog = page, catalog
        self._task = None
        self._cancelled = Event()
        self._close_requested = False
        self._picker_open = False
        self._started = 0.0
        self.setWindowTitle("Export Prompt Catalog for Generate")
        self.setModal(True)
        self.resize(700, 320)
        layout = QVBoxLayout(self)
        heading = QLabel(catalog.summary)
        heading.setTextFormat(Qt.PlainText)
        heading.setObjectName("sectionTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        scope = QLabel(
            "Save a read-only copy of these reusable names, aliases and Outfit prompt text for Manager. "
            "The file may contain sensitive prompt vocabulary. It contains no images, image paths, "
            "library contents or model files, and nothing is uploaded. Knowledge editing stays in Organizer. "
            "Later Knowledge edits require a new export.")
        scope.setWordWrap(True)
        layout.addWidget(scope)
        self.status = QLabel("Choose a new filename. Existing files will be preserved.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.activity = IndeterminateActivityBar(self)
        self.activity.hide()
        layout.addWidget(self.activity)
        actions = QHBoxLayout()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        actions.addStretch(1)
        self.save_button = QPushButton("Save Prompt Catalog…")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._progress)

    def has_active_background_work(self):
        return self._task is not None or self._picker_open

    def request_background_shutdown(self):
        if self.has_active_background_work():
            self._close_requested = True
            self._cancelled.set()
            self.status.setText("Finishing the owned save safely before closing. Keep any recovery receipt if saving fails.")
            return True
        return False

    def _progress(self):
        if not self._close_requested:
            self.status.setText(f"Saving prompt choices… {time.monotonic() - self._started:.0f}s. Close waits for the original save.")

    def save(self):
        if self.has_active_background_work() or self._close_requested:
            return
        self._picker_open = True
        try:
            target = QFileDialog.getSaveFileName(self, "Save Prompt Catalog", "CollectiGen-prompt-catalog.json", "Image draft (*.json)")[0]
        finally:
            self._picker_open = False
        if self._close_requested:
            self.reject()
            return
        if not target:
            return
        # Reinspect only the already-loaded canonical services; never discover files.
        if not _ready(self.page):
            self.status.setText("Finish the current Knowledge save, then reopen this export.")
            return
        try:
            if capture_prompt_catalog(self.page.tags, self.page.characters).fingerprint != self.catalog.fingerprint:
                raise ValueError("Knowledge changed after review. Close and export its current prompt choices.")
            plan = catalog_draft(self.catalog)
        except (ValueError, RuntimeError, TypeError, AttributeError) as error:
            self.status.setText(str(error))
            return
        self._cancelled.clear()
        def write(_context):
            if self._cancelled.is_set():
                return None
            save_plan(Path(target), plan)
            return Path(target)
        task = CoordinatedCallTask(self.page.state.jobs, "Export prompt catalog", write, resource="io")
        self._task = task
        def complete(value):
            if self._task is not task:
                return
            self._settled()
            if value is not None:
                self.status.setText(
                    f"Saved {value.name}. In Manager > Generate, choose Use Organizer Prompt Catalog, "
                    "then Import File and open its new local draft. Search Tags on Request or browse Outfits in Advanced.")
            else:
                self.status.setText("Export cancelled before writing. The selected destination was not changed.")
            if self._close_requested:
                QDialog.reject(self)
        def failed(error):
            if self._task is not task:
                return
            self._settled()
            self._close_requested = False
            self._cancelled.clear()
            self.status.setText(f"Export was not confirmed: {error}. Existing files and any save recovery receipt remain available.")
        task.signals.complete.connect(complete)
        task.signals.failed.connect(failed)
        task.signals.cancelled.connect(lambda: failed("Save did not run"))
        self.save_button.setEnabled(False)
        self._started = time.monotonic()
        self.activity.show()
        self.timer.start()
        self._progress()
        try:
            task.start()
        except Exception as error:
            failed(str(error))

    def _settled(self):
        self._task = None
        self.timer.stop()
        self.activity.hide()
        self.save_button.setEnabled(True)

    def reject(self):
        if not self.request_background_shutdown():
            super().reject()

    def closeEvent(self, event):
        if self.request_background_shutdown():
            event.ignore()
        else:
            super().closeEvent(event)


def export_prompt_catalog(page):
    current = getattr(page, "_prompt_catalog_export_dialog", None)
    if current is not None:
        current.raise_()
        return False
    if not _ready(page):
        QMessageBox.information(page, "Finish Knowledge Changes", "Finish or correct the current Knowledge edit before exporting prompt choices.")
        return False
    try:
        catalog = capture_prompt_catalog(page.tags, page.characters)
    except (ValueError, RuntimeError, TypeError, AttributeError) as error:
        QMessageBox.warning(page, "Prompt Catalog Unavailable", str(error))
        return False
    dialog = PromptCatalogExportDialog(page, catalog)
    page._prompt_catalog_export_dialog = dialog
    try:
        dialog.exec()
    finally:
        page._prompt_catalog_export_dialog = None
        dialog.deleteLater()
    return True
