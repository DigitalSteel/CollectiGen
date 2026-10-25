from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    PreferredCaptionPreparationApplyResult,
    PreferredCaptionPreparationPreview,
    PreferredCaptionPreparationService,
)

from .components import bind_status_footer,compact_button,counted_noun,set_progress_bar
from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask


class PreferredCaptionPreparationDialog(FeedbackEnabledDialog):
    """Preview and apply attributable preferred-captioner prose."""

    feedback_workflow = "Preferred description preparation"

    def __init__(self, service, images, *, pool=None, parent=None,history_operation_factory=None,history_opener=None):
        super().__init__(parent)
        self.service: PreferredCaptionPreparationService = service
        self.images = tuple(dict.fromkeys(Path(value) for value in images))
        self.pool = pool or QThreadPool.globalInstance()
        self.preview: PreferredCaptionPreparationPreview | None = None
        self.result: PreferredCaptionPreparationApplyResult | None = None
        self._task = None
        self.history_operation_factory=history_operation_factory
        self.history_opener=history_opener
        self._history_operation=None
        self._history_target=None

        self.setWindowTitle("Prepare Preferred Descriptions")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        self.heading = QLabel(
            f"Checking {counted_noun(len(self.images),'selected image')} for safe description updates…"
        )
        self.heading.setObjectName("sectionTitle")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        authority = QLabel(
            "Only exact, attributable captioner results are eligible. Edited, mixed, "
            "rejected, stale, or uncertain descriptions remain held for review. This "
            "changes Image Knowledge JSON only; raw AI evidence, TXT training data, "
            "Metadata, and image files are preserved."
        )
        authority.setObjectName("mutedLabel")
        authority.setWordWrap(True)
        layout.addWidget(authority)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.details = QTreeWidget()
        self.details.setHeaderLabels(
            ("Image", "Outcome", "Current source", "Preferred source", "Reason")
        )
        self.details.setRootIsDecorated(False)
        header = self.details.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.details, 1)

        self.progress = QProgressBar()
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.clicked.connect(self._cancel_or_close)
        self.apply_button = compact_button(
            self.buttons.addButton(
                "Apply Eligible Descriptions", QDialogButtonBox.AcceptRole
            ),
            primary=True,
        )
        self.apply_button.clicked.connect(self._apply)
        self.apply_button.setEnabled(False)
        self.undo_button = compact_button(
            self.buttons.addButton("Review Preparation Changes…", QDialogButtonBox.ActionRole)
        )
        self.undo_button.clicked.connect(self._undo)
        self.undo_button.hide()
        bind_status_footer(
            self,self.progress,self.cancel_button,title="Description preparation",
        )
        layout.addWidget(self.buttons)
        QTimer.singleShot(0, self._start_preview)

    def _start_preview(self):
        if self._task is not None:return
        try:
            self._history_operation=self.history_operation_factory(self.images) if self.history_operation_factory is not None else None
        except Exception as exc:
            self.heading.setText("Saved history needs attention")
            self.summary.setText(f"{exc}\nReview history storage in Settings or narrow the selection. No preparation changes started.")
            return
        self._history_target=(tuple(self.images),self._history_operation.operation_id) if self._history_operation is not None else None
        self._set_busy(True, "Checking selected images…")

        def run(progress, cancelled):
            if self._history_operation is None:self.service.recover_interrupted()
            return self.service.preview(
                self.images, progress=progress, should_cancel=cancelled,
                **({"history_operation":self._history_operation} if self._history_operation is not None else {}),
            )

        self._task = CancelableProgressCallTask(run)
        self._task.signals.progress.connect(self._progress)
        self._task.signals.complete.connect(self._preview_ready)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.cancelled.connect(self._cancelled)
        self.pool.start(self._task)

    def _progress(self, current: int, total: int, label: str):
        set_progress_bar(
            self.progress,
            current,
            total,
            detail=label,
            unknown_text="Working…",
        )

    def _set_busy(self, busy: bool, detail: str = ""):
        self.cancel_button.setEnabled(True)
        self.undo_button.setEnabled(not busy)
        self.apply_button.setEnabled(
            not busy
            and self.preview is not None
            and not self.preview.cancelled
            and self.preview.actionable > 0
        )
        self.cancel_button.setText("Cancel Safely" if busy else "Close")
        if busy:
            set_progress_bar(
                self.progress, detail=detail, unknown_text=detail or "Working…"
            )
            self.progress.show()
        else:
            self.progress.hide()

    def _preview_ready(self, preview: PreferredCaptionPreparationPreview):
        self._task = None
        self.preview = preview
        preferred = self._model_label(preview.preferred_model_id)
        self.heading.setText(
            f"Prepare selected images with {preferred}"
            if preferred
            else "Prepare selected images with the preferred captioner"
        )
        self.summary.setText(
            f"Fill empty: {preview.fill_empty:,}  •  Upgrade prior model output: "
            f"{preview.upgrade_prior:,}  •  Already preferred: "
            f"{preview.already_preferred:,}  •  Needs review: "
            f"{preview.needs_review:,}  •  Unavailable: {preview.unavailable:,}  •  "
            f"Failed: {preview.failed:,}"
            + ("  •  Preview cancelled before completion" if preview.cancelled else "")
        )
        self.details.clear()
        labels = {
            "fill_empty": "Fill empty",
            "upgrade_prior": "Upgrade prior output",
            "already_preferred": "Already preferred",
            "needs_review": "Needs review — held",
            "unavailable": "Unavailable",
            "failed": "Failed",
        }
        for item in preview.items:
            current = (
                self._model_label(item.prior_model_id)
                if item.prior_model_id
                else "Empty"
                if item.status == "fill_empty"
                else "Confirmed description"
            )
            row = QTreeWidgetItem(
                (
                    item.image.name,
                    labels.get(item.status, item.status),
                    current,
                    self._model_label(item.preferred_model_id),
                    item.error or item.detail,
                )
            )
            row.setToolTip(0, str(item.image))
            row.setToolTip(
                2,
                self._identity_tooltip(
                    item.prior_model_id, item.prior_model_revision
                )
                if item.prior_model_id
                else current,
            )
            row.setToolTip(
                3,
                self._identity_tooltip(
                    item.preferred_model_id, item.preferred_model_revision
                ),
            )
            row.setToolTip(4, item.error or item.detail)
            row.setData(0, Qt.UserRole, str(item.image))
            row.setData(
                0,
                Qt.AccessibleTextRole,
                " — ".join(
                    value
                    for value in (
                        item.image.name,
                        labels.get(item.status, item.status),
                        current,
                        self._model_label(item.preferred_model_id),
                        item.error or item.detail,
                    )
                    if value
                ),
            )
            self.details.addTopLevelItem(row)
        self._set_busy(False)

    def _apply(self):
        if self.preview is None or self._task is not None or self.result is not None:
            return
        self._set_busy(True, "Saving eligible description updates…")

        def run(progress, cancelled):
            return self.service.apply(
                self.preview, progress=progress, should_cancel=cancelled,
                **({"history_operation":self._history_operation} if self._history_operation is not None else {}),
            )

        self._task = CancelableProgressCallTask(run)
        self._task.signals.progress.connect(self._progress)
        self._task.signals.complete.connect(self._apply_ready)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.cancelled.connect(self._cancelled)
        self.pool.start(self._task)

    def _apply_ready(self, result: PreferredCaptionPreparationApplyResult):
        self._task = None
        self.result = result
        self.heading.setText("Saved history needs recovery" if result.history_interrupted else "Preferred-description preparation finished")
        self.summary.setText(
            f"Changed: {result.changed:,}  •  Already preferred: "
            f"{result.already_current:,}  •  Held for review: {result.held:,}  •  "
            f"Stale: {result.stale:,}  •  Failed: {result.failed:,}  •  "
            f"Recovery required: {result.count('interrupted'):,}  •  Not processed: {result.not_processed:,}"
            + ("  •  Cancelled safely after completed writes" if result.cancelled else "")
            + ("  •  Completed changes were kept; review the affected image before continuing." if result.history_interrupted else "")
        )
        outcomes={str(item.image):item for item in result.items}
        labels={"changed":"Saved","interrupted":"Recovery required","not_processed":"Not processed",
                "held":"Held for review","stale":"Changed since preview","failed":"Failed","already_current":"Already current"}
        for index in range(self.details.topLevelItemCount()):
            row=self.details.topLevelItem(index)
            item=outcomes.get(str(row.data(0,Qt.UserRole)))
            if item is not None:
                row.setText(1,labels.get(item.status,item.status))
                if item.error:row.setText(4,item.error); row.setToolTip(4,item.error)
                row.setData(0,Qt.AccessibleTextRole," • ".join(row.text(column) for column in range(5)))
        self.apply_button.hide()
        self.undo_button.setVisible(bool(self._history_target and self.history_opener))
        self._set_busy(False)

    def _undo(self):
        if self._task is not None:return
        if self._history_target is not None and self.history_opener is not None:
            return self.history_opener(*self._history_target)
        self.summary.setText("This batch has no proven saved-history target. Older untracked changes cannot be restored with a whole-record Undo.")

    def changed_paths(self) -> tuple[Path, ...]:
        if self.result is None:
            return ()
        return tuple(
            item.image for item in self.result.items if item.status in {"changed","interrupted"}
        )

    def _cancel_or_close(self):
        if self._task is not None:
            self._task.cancel()
            self.cancel_button.setEnabled(False)
            self.heading.setText("Cancelling safely after the current image…")
            return
        self.reject()

    def reject(self):
        if self._task is not None:
            self._cancel_or_close()
            return
        super().reject()

    def _failed(self, message: str):
        self._task = None
        self._set_busy(False)
        self.undo_button.setVisible(bool(self._history_target and self.history_opener))
        self.apply_button.setEnabled(False)
        QMessageBox.warning(
            self, "Preferred-description preparation could not finish", message
        )

    def _cancelled(self):
        self._task=None
        self._set_busy(False)
        self.apply_button.setEnabled(False)
        self.undo_button.setVisible(bool(self._history_target and self.history_opener))
        self.heading.setText("Preparation stopped")
        self.summary.setText("Completed changes were kept. Review saved changes before starting another preparation run.")

    @staticmethod
    def _model_label(model_id: str) -> str:
        value = str(model_id or "").strip()
        if not value:
            return ""
        labels = {
            "fancyfeast/llama-joycaption-beta-one-hf-llava": "JoyCaption",
            "florence-community/Florence-2-base-ft": "Florence-2 Base FT",
        }
        return labels.get(value, value.rsplit("/", 1)[-1])

    @staticmethod
    def _identity_tooltip(model_id: str, revision: str) -> str:
        model = str(model_id or "").strip() or "Unknown captioner"
        version = str(revision or "").strip() or "Legacy or unverified revision"
        return f"{model}\nRevision: {version}"
