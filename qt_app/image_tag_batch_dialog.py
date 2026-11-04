from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    ImageTagBatchApplyResult,
    ImageTagBatchPreview,
    ImageTagBatchService,
)

from .components import bind_status_footer,compact_button,counted_noun,set_progress_bar
from .modal_feedback import FeedbackEnabledDialog
from .history_task_lifetime import HistoryTaskLifetime
from shiboken6 import isValid


class ImageTagBatchDialog(FeedbackEnabledDialog):
    """Retain the original selected-image operation through scoped drain."""

    feedback_workflow = "Selected-image tag review"

    def __init__(
        self,
        service: ImageTagBatchService,
        images,
        primary_tag_id: str,
        *,
        manager=None,
        pool=None,  # Legacy keyword; all work uses the original shared coordinator.
        parent=None,
        history_operation_factory=None,
        history_opener=None,
    ):
        super().__init__(parent)
        self.service = service
        self.images = tuple(dict.fromkeys(Path(value) for value in images))
        self.primary_tag_id = str(primary_tag_id)
        self.manager = manager
        self._attempts = []
        self._active_attempt = self._consuming_attempt = self._return_hold = None
        self._preparing_history = False
        self._intent_reentry = 0
        self._prepared_history = None
        self._history_preparation_hold = None
        self.history_operation_factory = history_operation_factory
        self.history_opener = history_opener
        self.preview: ImageTagBatchPreview | None = None
        self.result: ImageTagBatchApplyResult | None = None
        self.review_requested = False
        self._task = None
        self._history_operation_id = ""

        self.setWindowTitle("Add SDXL Tag to Selected Images")
        self.resize(940, 680)
        layout = QVBoxLayout(self)
        self.heading = QLabel(
            f"Checking {counted_noun(len(self.images),'selected image')} before changing Image Knowledge…"
        )
        self.heading.setObjectName("sectionTitle")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        authority = QLabel(
            "This operation changes confirmed Image Knowledge only. TXT training data, "
            "Metadata, and image files are not rewritten."
        )
        authority.setObjectName("mutedLabel")
        authority.setWordWrap(True)
        layout.addWidget(authority)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.details = QTreeWidget()
        self.details.setHeaderLabels(("Image", "Outcome", "Details"))
        self.details.setRootIsDecorated(False)
        header = self.details.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.details, 2)

        related_heading = QLabel("Related SDXL Tags")
        related_heading.setObjectName("sectionTitle")
        layout.addWidget(related_heading)
        related_note = QLabel(
            "Each checked relationship is a separate reviewed fact. Blocked or conflicting "
            "relationships remain unchecked and are never added automatically."
        )
        related_note.setObjectName("mutedLabel")
        related_note.setWordWrap(True)
        layout.addWidget(related_note)
        self.related = QTreeWidget()
        self.related.setHeaderLabels(
            ("Add", "SDXL Tag", "Relationship and path", "Scope", "Eligible")
        )
        self.related.setRootIsDecorated(False)
        related_header = self.related.header()
        related_header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.related.setColumnWidth(0, 48)
        related_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        related_header.setSectionResizeMode(2, QHeaderView.Stretch)
        related_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        related_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.related.itemChanged.connect(self._update_actions)
        layout.addWidget(self.related, 2)

        self.progress = QProgressBar()
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.clicked.connect(self._cancel_or_close)
        self.review_button = compact_button(
            self.buttons.addButton("Review Conflicts…", QDialogButtonBox.ActionRole)
        )
        self.review_button.clicked.connect(self._review_conflicts)
        self.review_button.setEnabled(False)
        self.apply_button = compact_button(
            self.buttons.addButton(
                "Add and Skip Conflicts", QDialogButtonBox.AcceptRole
            ),
            primary=True,
        )
        self.apply_button.clicked.connect(self._apply)
        self.apply_button.setEnabled(False)
        self.undo_button = compact_button(
            self.buttons.addButton("Review Batch Changes…", QDialogButtonBox.ActionRole)
        )
        self.undo_button.clicked.connect(self._undo)
        self.undo_button.hide()
        bind_status_footer(
            self,self.progress,self.cancel_button,title="Selected-image Tag batch",
        )
        layout.addWidget(self.buttons)
        QTimer.singleShot(0, self._start_preview)

    def _start_preview(self):
        if self._work_pending() or self.result is not None or self._history_preparation_hold is not None:return
        if not self._intent_allowed():
            self._set_busy(False);return
        if not self._manager_ready():return
        self._set_busy(True, "Checking selected images…")

        def run(progress, cancelled):
            return self.service.preview(
                self.images,
                self.primary_tag_id,
                progress=progress,
                should_cancel=cancelled,
            )

        self._run_scoped(run, self._preview_ready, writes=False)

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
            and not self._work_pending()
            and self.result is None and self._history_preparation_hold is None
            and (self.preview is None or not self.preview.cancelled and self.preview.actionable > 0)
        )
        self.related.setEnabled(not busy and not self._work_pending() and self.result is None and self._history_preparation_hold is None)
        self.review_button.setEnabled(
            not busy and not self._work_pending()
            and self.preview is not None
            and not self.preview.cancelled
            and self._has_reviewable_conflicts()
        )
        self.apply_button.setText("Check Images" if self.preview is None else "Add and Skip Conflicts")
        self.cancel_button.setText("Cancel Safely" if busy else "Close")
        if busy:
            if detail:self.heading.setText(detail)
            set_progress_bar(
                self.progress, detail=detail, unknown_text=detail or "Working…"
            )
            self.progress.show()
        else:
            self.progress.hide()

    def _preview_ready(self, preview: ImageTagBatchPreview):
        blocked = self.related.blockSignals(True)
        try:self._render_preview(preview)
        finally:
            self.related.blockSignals(blocked)
            self._accepted_related = {str(self.related.topLevelItem(i).data(0,Qt.UserRole)):self.related.topLevelItem(i).checkState(0)
                for i in range(self.related.topLevelItemCount())}

    def _render_preview(self, preview: ImageTagBatchPreview):
        self._task = None
        self.preview = preview
        self.heading.setText(f'Add “{preview.primary_display}” to selected images')
        self.summary.setText(
            f"Will add: {preview.will_add:,}  •  Already present: "
            f"{preview.already_present:,}  •  Conflicts: {preview.conflicts:,}  •  "
            f"Unavailable/stale: {preview.unavailable_or_stale:,}  •  "
            f"Failed: {preview.failed:,}"
            + ("  •  Preview cancelled before completion" if preview.cancelled else "")
        )
        self.details.clear()
        labels = {
            "will_add": "Will add",
            "already_present": "Already present",
            "conflict": "Conflict — held",
            "unavailable": "Unavailable",
            "failed": "Failed",
        }
        for item in preview.images:
            detail = item.error
            if item.conflicts:
                detail = "\n".join(conflict.message for conflict in item.conflicts)
            row = QTreeWidgetItem(
                (item.image.name, labels.get(item.status, item.status), detail)
            )
            row.setToolTip(0, str(item.image))
            row.setData(0, Qt.UserRole, str(item.image))
            self.details.addTopLevelItem(row)
        self.related.clear()
        for related in preview.related:
            eligibility = (
                f"{related.eligible_images:,} eligible"
                + (
                    f"; {related.blocked_images:,} blocked"
                    if related.blocked_images
                    else ""
                )
                + (
                    f"; {related.conflict_images:,} conflict"
                    + ("s" if related.conflict_images != 1 else "")
                    if related.conflict_images
                    else ""
                )
            )
            row = QTreeWidgetItem(
                (
                    "",
                    related.display,
                    f"{related.relationship} • {related.path}",
                    related.scope,
                    eligibility,
                )
            )
            row.setData(0, Qt.UserRole, related.key)
            row.setToolTip(
                2,
                f"Source: {related.source}\nRelationship: {related.relationship}\n"
                f"Path: {related.path}",
            )
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(0, Qt.Unchecked)
            if related.eligible_images <= 0:
                row.setDisabled(True)
            self.related.addTopLevelItem(row)
        self._set_busy(False)

    def _update_actions(self, *_):
        if self._task is None:
            self._set_busy(False)

    def _has_reviewable_conflicts(self) -> bool:
        if self.preview is None:
            return False
        if self.preview.conflicts:
            return True
        selected = set(self.selected_related_keys())
        return any(
            key in selected and related.conflict_images > 0
            for related in self.preview.related
            for key in (related.key,)
        )

    def selected_related_keys(self) -> tuple[str, ...]:
        values = []
        for index in range(self.related.topLevelItemCount()):
            item = self.related.topLevelItem(index)
            if (
                item.checkState(0) == Qt.Checked
                and bool(item.flags() & Qt.ItemIsEnabled)
            ):
                values.append(str(item.data(0, Qt.UserRole) or ""))
        return tuple(value for value in values if value)

    def _apply(self):
        if self._work_pending() or self.result is not None or self._history_preparation_hold is not None:return
        if not self._intent_allowed() or not self._manager_ready():return
        if self.preview is None:
            self._start_preview();return
        preview=self.preview
        if preview.cancelled or preview.actionable<=0:return
        before=self._intent_context()
        selected=self.selected_related_keys()
        history=None
        def run(progress,cancelled):
            return self.service.apply(preview,related_keys=selected,progress=progress,should_cancel=cancelled,
                **({"history_operation":history} if history is not None else {}))
        # Scope construction is inert and precedes any History factory side effect.
        attempt=self._run_scoped(run,self._apply_ready,writes=True,prepare_only=True)
        if attempt is None:return
        try:
            self._preparing_history=True
            try:history=self.history_operation_factory(self.images) if self.history_operation_factory is not None else None
            finally:self._preparing_history=False
        except Exception as error:
            attempt.receipt['history_preparation_error']=error
            try:attempt.scope.pause_admission()
            except Exception as scope_error:attempt.receipt['scope_error']=scope_error
            self._attempts.append(attempt)
            self._history_preparation_hold=attempt
            self.heading.setText("Saved History preparation needs review")
            self.summary.setText(str(error)+" The History factory did not return; its original preparation receipt is retained. No image service was submitted. Do not apply again in this review; close and inspect original History before reopening.")
            self._set_busy(False)
            return
        self._prepared_history=history
        attempt.receipt['history_preparation']=history
        if not self._intent_allowed(expected_context=before):
            try:attempt.scope.pause_admission()
            except Exception as scope_error:attempt.receipt['scope_error']=scope_error
            self._attempts.append(attempt)
            if history is not None:
                self._history_operation_id=getattr(history,"operation_id","")
                self._return_hold=attempt
                self._hold_return("The original History preparation is retained. Review it before a fresh image action; nothing will replay.")
            return
        self._history_operation_id=getattr(history,"operation_id","")
        self._set_busy(True,"Saving compatible Image Knowledge changes…")
        self._activate_scoped(attempt)

    def _apply_ready(self, result: ImageTagBatchApplyResult):
        self._task = None
        self.result = result
        self.details.clear()
        labels = {"changed": "Saved", "already_present": "Already present",
            "interrupted": "Recovery required", "not_processed": "Not processed",
            "conflict_skipped": "Conflict — held", "held": "Held", "stale": "Changed since preview"}
        for item in result.items:
            detail = item.error or ("Added: " + ", ".join(item.added_tags) if item.added_tags else "")
            row = QTreeWidgetItem((item.image.name, labels.get(item.status, item.status.replace("_", " ").title()), detail))
            row.setData(0, Qt.UserRole, str(item.image))
            row.setToolTip(0, str(item.image))
            row.setToolTip(2, detail)
            self.details.addTopLevelItem(row)
        self.related.setEnabled(False)
        self.summary.setText(
            f"Changed: {result.changed:,}  •  Already present: "
            f"{result.already_present:,}  •  Conflicts skipped: "
            f"{result.conflicts_skipped:,}  •  Stale: {result.stale:,}  •  "
            f"Failed: {result.failed:,}  •  Not processed: {result.not_processed:,}"
            + ("  •  Cancelled safely after completed writes" if result.cancelled else "")
        )
        self.heading.setText("Saved history needs recovery" if result.history_interrupted else "Selected-image SDXL Tag batch finished")
        if result.history_interrupted:
            self.summary.setText(self.summary.text() + "\nAn image may already be saved. Review Batch Changes and open its recovery before another edit.")
        self.apply_button.hide()
        self.review_button.setEnabled(self._has_reviewable_conflicts())
        self.undo_button.setVisible(bool(result.history_operation_id and result.items or result.operation_id and result.changed))
        self._set_busy(False)

    def _undo(self):
        if not self._intent_allowed():return
        if self._task is not None:
            return
        operation_id = (getattr(self.result,"history_operation_id","") or getattr(self.result,"operation_id","") or self._history_operation_id)
        if not operation_id:
            return
        if self.history_opener is None:
            QMessageBox.information(self, "History unavailable", "Open History & Versions from the original image selection. Older changes without proven saved history cannot be restored here.")
            return
        try:
            self.history_opener(self.images, operation_id)
        except Exception as exc:
            QMessageBox.warning(self, "History could not open", str(exc))
            return
        self.accept()

    def _review_conflicts(self):
        if not self._work_pending() and not self._intent_allowed():return
        if self._work_pending():return
        if self.preview is None or not self._has_reviewable_conflicts():
            return
        self.review_requested = True
        self.accept()

    def conflict_review_context(self) -> dict[str, dict]:
        if self.preview is None:
            return {}
        selected_related = set(self.selected_related_keys())
        context = {}
        for item in self.preview.images:
            proposals = []
            if item.status == "conflict":
                proposals.append(
                    {
                        "tag_id": self.preview.primary_tag_id,
                        "display": self.preview.primary_display,
                        "conflicts": tuple(item.conflicts),
                        "owner_id": "",
                        "owner_group": "",
                        "suggestion": None,
                    }
                )
            for key in selected_related:
                conflicts = item.related_conflicts.get(key, ())
                suggestion = item.related_suggestions.get(key)
                if not conflicts or suggestion is None:
                    continue
                proposals.append(
                    {
                        "tag_id": suggestion.tag_id,
                        "display": suggestion.display,
                        "conflicts": tuple(conflicts),
                        "owner_id": suggestion.owner_id,
                        "owner_group": suggestion.owner_group,
                        "suggestion": suggestion,
                    }
                )
            if not proposals:
                continue
            value = {"proposals": tuple(proposals)}
            if item.status == "conflict":
                value.update(
                    {
                        "primary_tag_id": self.preview.primary_tag_id,
                        "primary_display": self.preview.primary_display,
                        "conflicts": tuple(item.conflicts),
                    }
                )
            context[str(item.image)] = value
        return context

    def changed_paths(self) -> tuple[Path, ...]:
        if self.result is None:
            return ()
        return tuple(
            item.image for item in self.result.items if item.status == "changed"
        )

    def _cancel_or_close(self):
        if self._task is not None:
            self._active_attempt.cancel() if self._active_attempt is not None else None
            self.cancel_button.setEnabled(False)
            self.heading.setText("Cancelling safely after the current image…")
            return
        self.reject()

    def _failed(self, message: str):
        if self._active_attempt is not None:
            self._return_hold=self._active_attempt
            self._hold_return("The original operation remains owned. Review its retained receipt; do not retry.")
            return
        self._task = None
        self._set_busy(False)
        self.apply_button.setEnabled(False)
        self.undo_button.setVisible(bool(self._history_operation_id))
        QMessageBox.warning(self, "Selected-image tag batch could not finish", message)

    def _cancelled(self):
        # A terminal signal is not the returned receipt or physical drain.
        if self._active_attempt is not None:self._active_attempt.poll()

    def _manager_ready(self):
        if self.manager is not None and all(callable(getattr(self.manager,name,None))
                for name in ("create_execution_scope","submit_observed","cancel")):return True
        self.heading.setText("Shared background work is unavailable")
        self.summary.setText("No image service was submitted. Reopen this review from its original workspace; Close remains available.")
        self._set_busy(False)
        return False

    def _work_pending(self):
        return self._task is not None or self._consuming_attempt is not None or self._return_hold is not None

    def has_active_background_work(self):
        return self._work_pending() or self._preparing_history

    def _hold_return(self,message):
        self.heading.setText("Original image operation needs review")
        self.summary.setText(message)
        self.progress.hide()
        for name in ("apply_button","remove_button","review_button","recovery_button"):
            button=getattr(self,name,None)
            if button is not None:button.setEnabled(False)
        self.undo_button.setVisible(bool(self._history_operation_id))
        self.undo_button.setEnabled(bool(self._history_operation_id))
        self.cancel_button.setText("Close held")
        self.cancel_button.setEnabled(False)

    def _run_scoped(self,run,consume,*,writes,prepare_only=False):
        if self._work_pending() or not self._manager_ready():return
        def work(job,cancelled):return run(job.update,cancelled)
        def drained(attempt):
            if self._active_attempt is not attempt:
                self._return_hold=attempt
                self._hold_return("The original return is retained for its exact owner. No replacement or retry will run.")
                return
            self._active_attempt=None
            self._task=None
            receipt=attempt.receipt
            self._consuming_attempt=attempt
            try:
                if receipt['returned']:
                    consume(receipt['value'])
                elif writes or receipt['error'] is None:
                    self._return_hold=attempt
                    self._hold_return("A complete image-operation result is unavailable. Saved changes may exist; retain the original History and recovery evidence. Do not repeat the operation.")
                else:
                    self._failed(str(receipt['error']))
            except Exception as error:
                receipt['presentation_error']=error
                self._return_hold=attempt
                self._hold_return("The original result is retained, but its display needs review. Saved changes may exist; do not repeat the operation.")
            finally:self._consuming_attempt=None
            self._set_busy(False) if self._return_hold is None else None
        def hold(message):
            if attempt.settled and 'presentation_error' in attempt.receipt:self._return_hold=attempt
            self.summary.setText(message)
        try:attempt=HistoryTaskLifetime(self,self.manager,"Selected-image Tag batch",work,drained,hold)
        except Exception as error:
            self._set_busy(False)
            self.heading.setText("Background work could not be prepared")
            self.summary.setText(str(error)+" No image service was submitted; Close remains available.")
            return
        attempt.receipt['writes']=writes
        if prepare_only:return attempt
        self._activate_scoped(attempt)
        return attempt

    def _activate_scoped(self,attempt):
        self._attempts.append(attempt)
        self._active_attempt=attempt
        self._task=attempt.task
        attempt.task.signals.progress.connect(
            lambda current,total,label:self._progress(current,total,label) if self._active_attempt is attempt else None,
            Qt.QueuedConnection)
        attempt.start()

    def done(self,result):
        if self._work_pending():
            if self._active_attempt is not None:self._active_attempt.cancel()
            return
        if not self._intent_allowed():return
        super().done(result)

    def reject(self):
        self.done(0)

    def closeEvent(self,event):
        event.ignore()
        if self._work_pending():
            if self._active_attempt is not None:self._active_attempt.cancel()
            event.ignore();return
        if not self._intent_allowed():event.ignore();return
        super().closeEvent(event)

    def _intent_context(self):
        return (id(self.service), id(self.manager), self.images, self.primary_tag_id,
                id(self.preview), id(self.result), id(self._task), id(self._return_hold),
                id(self._history_preparation_hold), id(self.history_operation_factory),
                id(self.history_opener), self.review_requested, self._intent_reentry,
                self.selected_related_keys())

    def _intent_allowed(self, expected_context=None):
        if self._preparing_history or self._consuming_attempt is not None:
            self._intent_reentry += 1
            return False
        return expected_context is None or self._intent_context() == expected_context
