from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from core import ImageTagRemovalApplyResult, ImageTagRemovalPreview, ImageTagRemovalService

from .components import bind_status_footer,compact_button,counted_noun,set_progress_bar
from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask


class ImageTagRemovalDialog(FeedbackEnabledDialog):
    """Preview one explicit remove-everywhere tag lifecycle choice."""

    feedback_workflow = "Remove SDXL Tag everywhere"
    background_work_finished = Signal()

    def __init__(
        self,
        service: ImageTagRemovalService,
        images,
        tag_ids,
        *,
        relationship_summary: str = "",
        history_operation_factory=None,
        history_opener=None,
        recovery_handler=None,
        pool=None,
        parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.images = tuple(dict.fromkeys(Path(value) for value in images))
        self.tag_ids = tuple(dict.fromkeys(str(value) for value in tag_ids if str(value)))
        self.relationship_summary = str(relationship_summary or "")
        self.pool = pool or QThreadPool.globalInstance()
        self.preview: ImageTagRemovalPreview | None = None
        self.result: ImageTagRemovalApplyResult | None = None
        self._task = None
        self._close_requested = False
        self._cancel_requested = False
        self._history_operation_factory = history_operation_factory
        self._history_opener = history_opener
        self._recovery_handler = recovery_handler
        self._history_target = ""

        self.setWindowTitle("Remove SDXL Tag Everywhere")
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        self.heading = QLabel(
            f"Checking {counted_noun(len(self.images),'affected image')} before removing the selected SDXL Tag everywhere…"
        )
        self.heading.setObjectName("sectionTitle")
        self.heading.setTextFormat(Qt.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.target_summary = QLabel()
        self.target_summary.setTextFormat(Qt.PlainText)
        self.target_summary.setWordWrap(True)
        self.target_summary.setAccessibleName("Selected Tags to remove")
        layout.addWidget(self.target_summary)
        authority = QGroupBox("What changes")
        authority.setAccessibleName("Remove-everywhere data authority")
        authority_layout = QVBoxLayout(authority)
        for text in (
            "Image Knowledge: remove the selected confirmed Tag facts.",
            "Reusable Tags: remove only after every affected image is verified.",
            "Unchanged: TXT captions, embedded Metadata, original images, raw evidence, folders and Automation rules.",
        ):
            label = QLabel(text)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            authority_layout.addWidget(label)
        authority.setToolTip("Image Knowledge saves are stale-checked, recorded and read-back verified. Image History restores image facts, not reusable System Knowledge Tags.")
        layout.addWidget(authority)
        if self.relationship_summary:
            if len(self.relationship_summary) <= 160:
                relationships = QLabel(self.relationship_summary)
                relationships.setTextFormat(Qt.PlainText)
                relationships.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
                relationships.setWordWrap(True)
            else:
                relationships = QTextEdit()
                relationships.setReadOnly(True)
                relationships.setAcceptRichText(False)
                relationships.setPlainText(self.relationship_summary)
                relationships.setMinimumHeight(48)
                relationships.setMaximumHeight(76)
            relationships.setAccessibleName("Reusable Tag relationship changes")
            layout.addWidget(QLabel("Planned reusable Tag changes"))
            layout.addWidget(relationships)

        self.counts = QGroupBox("Affected images")
        counts_layout = QGridLayout(self.counts)
        self.count_labels = []
        self.count_values = []
        for column, title in enumerate(("Checked", "To update", "Already absent", "Needs attention")):
            label, value = QLabel(title), QLabel("—")
            label.setObjectName("mutedLabel")
            value.setTextFormat(Qt.PlainText)
            counts_layout.addWidget(label, 0, column)
            counts_layout.addWidget(value, 1, column)
            counts_layout.setColumnStretch(column, 1)
            self.count_labels.append(label)
            self.count_values.append(value)
        layout.addWidget(self.counts)
        self.counts.hide()
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Remove-everywhere next step")
        layout.addWidget(self.summary)
        self.details = QTreeWidget()
        self.details.setHeaderLabels(("Image", "Outcome", "Confirmed Tag facts"))
        self.details.setRootIsDecorated(False)
        self.details.setAccessibleName("Affected images and exact Tag removals")
        header = self.details.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.resizeSection(0, 230)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.details, 1)
        self.selected_details = QTextEdit()
        self.selected_details.setReadOnly(True)
        self.selected_details.setAcceptRichText(False)
        self.selected_details.setAccessibleName("Selected image: full path, outcome and reason")
        self.selected_details.setPlaceholderText("Select an image to read its full Tag facts or reason.")
        self.selected_details.setMinimumHeight(62)
        self.selected_details.setMaximumHeight(104)
        layout.addWidget(self.selected_details)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.clicked.connect(self._cancel_or_close)
        self.remove_button = compact_button(
            self.buttons.addButton("Remove Everywhere", QDialogButtonBox.AcceptRole),
            primary=True,
        )
        self.remove_button.setAccessibleDescription(
            "Remove the previewed confirmed Tag facts from all affected images and continue with System Knowledge removal."
        )
        self.remove_button.clicked.connect(self._apply)
        self.remove_button.setEnabled(False)
        self.history_button = compact_button(
            self.buttons.addButton("Review Image Changes…", QDialogButtonBox.ActionRole)
        )
        self.history_button.clicked.connect(self._open_history)
        self.history_button.setToolTip("Review the exact saved image changes. Restoring facts for a retired reusable Tag first requires resolving that Tag in System Knowledge.")
        self.history_button.hide()
        self.recovery_button = compact_button(
            self.buttons.addButton("Review Image Recovery…", QDialogButtonBox.ActionRole)
        )
        self.recovery_button.clicked.connect(self._open_recovery)
        self.recovery_button.hide()
        self.details.itemSelectionChanged.connect(self._selection_changed)
        bind_status_footer(
            self,self.progress,self.cancel_button,title="Remove Tag everywhere",
        )
        layout.addWidget(self.buttons)
        QTimer.singleShot(0, self._start_preview)

    @property
    def completed(self) -> bool:
        return bool(not self._close_requested and not self._cancel_requested and self.result is not None and self.result.complete)

    def changed_paths(self) -> tuple[Path, ...]:
        if self.result is None:
            return ()
        return tuple(item.image for item in self.result.items if item.status == "changed")

    def _start_preview(self):
        if self._close_requested:
            return
        self._set_busy(True, "Checking affected Image Knowledge…")

        def run(progress, cancelled):
            return self.service.preview(
                self.images,
                self.tag_ids,
                progress=progress,
                should_cancel=cancelled,
            )

        self._task = CancelableProgressCallTask(run)
        self._task.signals.progress.connect(self._progress)
        self._task.signals.complete.connect(self._preview_ready)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.cancelled.connect(self._cancelled)
        self.pool.start(self._task)

    def _preview_ready(self, preview: ImageTagRemovalPreview):
        self._task = None
        self.preview = preview
        self.heading.setText(f"Review Tag removal from {counted_noun(len(self.images), 'image')}")
        names = ", ".join(target.display for target in preview.targets)
        self.target_summary.setText("Selected Tags: " + (names if len(names) <= 110 else names[:107] + "…"))
        self.target_summary.setToolTip(names)
        self._set_counts(len(preview.items), preview.will_remove, preview.already_absent,
            preview.unavailable + preview.failed)
        self.summary.setText(
            "Confirm to remove the listed facts, then continue with reusable Tag removal."
            if preview.ready else "Check cancelled. Nothing has changed; close and review removal again."
            if preview.cancelled else "Nothing has changed. Close this review, resolve the listed image problems, then review removal again."
        )
        self.details.clear()
        self.selected_details.clear()
        labels = {
            "will_remove": "Will remove",
            "already_absent": "Already absent",
            "unavailable": "Unavailable",
            "failed": "Could not verify",
        }
        for item in preview.items:
            detail = ", ".join(item.values) if item.values else item.error
            row = QTreeWidgetItem((item.image.name, labels.get(item.status, item.status), detail))
            row.setToolTip(0, str(item.image))
            row.setToolTip(2, detail)
            row.setData(0, Qt.UserRole, str(item.image))
            self.details.addTopLevelItem(row)
        if self.details.topLevelItemCount():
            self.details.setCurrentItem(self.details.topLevelItem(0))
        self.remove_button.setText(
            "Remove Everywhere" if preview.will_remove else "Continue Removal"
        )
        self._set_busy(False)
        self._notify_background_work_finished()

    def _apply(self):
        if self._task is not None or self.result is not None or self.preview is None or not self.preview.ready:
            return
        preview = self.preview
        try:
            operation = self._history_operation_factory(self.images) if self._history_operation_factory else None
        except Exception as exc:
            self._failed(str(exc))
            return
        self._history_target = operation.operation_id if operation is not None else ""
        self._set_busy(True, "Removing confirmed Tag facts from affected images…")

        def run(progress, cancelled):
            return self.service.apply(
                preview,
                progress=progress,
                should_cancel=cancelled,
                **({"history_operation": operation} if operation is not None else {}),
            )

        self._task = CancelableProgressCallTask(run)
        self._task.signals.progress.connect(self._progress)
        self._task.signals.complete.connect(self._apply_ready)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.cancelled.connect(self._cancelled)
        self.pool.start(self._task)

    def _apply_ready(self, result: ImageTagRemovalApplyResult):
        self._task = None
        self.result = result
        self._history_target = result.history_operation_id or result.operation_id
        if self.completed:
            self.accept()
            self.background_work_finished.emit()
            return
        self.heading.setText("Removal stopped — the reusable Tag is still available")
        self._set_counts(len(result.items), result.changed, result.already_absent,
            len(result.items) - result.changed - result.already_absent, updated=True)
        self.summary.setText(
            "Completed image changes stay saved. Review them or select an image that needs attention to read its reason."
        )
        self.details.clear()
        self.selected_details.clear()
        labels = {
            "changed": "Removed",
            "already_absent": "Already absent",
            "stale": "Changed after preview",
            "failed": "Failed",
            "not_processed": "Not processed",
            "held": "Held",
            "interrupted": "Review recovery",
        }
        for item in result.items:
            detail = ", ".join(item.removed_values) if item.removed_values else item.error
            row = QTreeWidgetItem((item.image.name, labels.get(item.status, item.status), detail))
            row.setToolTip(0, str(item.image))
            row.setToolTip(2, detail)
            row.setData(0, Qt.UserRole, str(item.image))
            self.details.addTopLevelItem(row)
            if item.status == "interrupted" and self.details.currentItem() is None:
                self.details.setCurrentItem(row)
        self.remove_button.hide()
        if self.details.currentItem() is None and self.details.topLevelItemCount():
            self.details.setCurrentItem(self.details.topLevelItem(0))
        self.history_button.setVisible(bool(self._history_opener and self._history_target))
        self.recovery_button.setVisible(bool(self._recovery_handler and result.history_interrupted))
        self._set_busy(False)
        self._notify_background_work_finished()

    def _open_history(self):
        if self._task is None and self._history_opener and self._history_target:
            self._history_opener(self.images, self._history_target)
            self.reject()

    def _recovery_image(self):
        row = self.details.currentItem()
        if row is None or self.result is None:
            return None
        image = Path(row.data(0, Qt.UserRole))
        return next((item.image for item in self.result.items if item.image == image and item.status == "interrupted"), None)

    def _update_recovery(self):
        self.recovery_button.setEnabled(self._task is None and self._recovery_image() is not None)

    def _set_counts(self, checked, changed, absent, attention, *, updated=False):
        self.count_labels[1].setText("Updated" if updated else "To update")
        for label, title, value in zip(self.count_values, self.count_labels, (checked, changed, absent, attention)):
            label.setText(f"{value:,}")
            label.setAccessibleName(f"{title.text()}: {value:,}")
        self.counts.show()

    def _selection_changed(self):
        row = self.details.currentItem()
        targets = ", ".join(target.display for target in self.preview.targets) if self.preview is not None else ""
        self.selected_details.setPlainText(
            f"{row.data(0, Qt.UserRole)}\n{row.text(1)}\n{row.text(2)}\nSelected Tags: {targets}" if row is not None else ""
        )
        self._update_recovery()

    def _open_recovery(self):
        image = self._recovery_image()
        if self._task is None and image is not None and self._recovery_handler:
            self._recovery_handler(image)
            self.reject()

    def _progress(self, current: int, total: int, label: str):
        set_progress_bar(
            self.progress,
            current,
            total,
            detail=label,
            unknown_text="Working…",
        )

    def _set_busy(self, busy: bool, detail: str = ""):
        self.remove_button.setEnabled(
            not busy and self.preview is not None and self.preview.ready
        )
        self.cancel_button.setText("Cancel Safely" if busy else "Close")
        self.cancel_button.setEnabled(not self._close_requested)
        self.history_button.setEnabled(not busy)
        self._update_recovery()
        if busy:
            set_progress_bar(self.progress, detail=detail, unknown_text=detail or "Working…")
            self.progress.show()
        else:
            self.progress.hide()

    def _cancel_or_close(self):
        if self._task is not None:
            self._cancel_requested = True
            self._task.cancel()
            self.cancel_button.setEnabled(False)
            return
        self.reject()

    def _failed(self, message: str):
        self._task = None
        self.heading.setText("Remove-everywhere operation stopped")
        self.summary.setText("The reusable Tag was not removed. Review any saved image changes before starting a new removal.")
        self.counts.hide()
        self.details.clear()
        self.selected_details.setPlainText(message)
        self.remove_button.hide()
        self.history_button.setVisible(bool(self._history_opener and self._history_target))
        self._set_busy(False)
        self._notify_background_work_finished()

    def _cancelled(self):
        self._failed("The operation was cancelled before completion.")

    def reject(self):
        self._close_requested = True
        if self._task is not None:
            self._task.cancel()
            self.cancel_button.setEnabled(False)
            return
        super().reject()

    def done(self, result):
        if self._task is not None:
            self.reject()
            return
        super().done(result)

    def closeEvent(self, event):
        if self._task is not None:
            self.reject()
            event.ignore()
            return
        self._close_requested = True
        super().closeEvent(event)

    def _notify_background_work_finished(self):
        if self._task is None:
            if self._close_requested:
                super().reject()
            self.background_work_finished.emit()
