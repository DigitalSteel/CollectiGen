from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .modal_feedback import FeedbackEnabledDialog


class DownstreamReviewDialog(FeedbackEnabledDialog):
    """Keep the saved-work checklist visible across contextual job handoffs."""

    requested = Signal(str, object)
    dismissal_requested = Signal(str, str)
    feedback_workflow = "Related updates"

    def __init__(self, identity, projection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Related Updates")
        self.resize(760, 660)
        self.setAttribute(Qt.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        intro = QLabel(
            "Start with the first pending step. Each step checks the saved Tag scope "
            "in your included libraries, then previews any changes for confirmation. "
            "Return here for the remaining steps; completed steps disappear. "
            "Dismiss Reminder skips an optional file update and keeps your files unchanged. "
            "You can still run it later from Dataset."
        )
        self.intro = intro
        intro.setWordWrap(True)
        layout.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.body)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.refresh(identity, projection)

    def refresh(self, identity, projection):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.actions = {}
        self.dismiss_actions = {}
        pending = int(identity.active) + len(projection.pending_targets)
        self.intro.setVisible(bool(pending))
        self.summary.setText(
            f"{pending} pending step{'s' if pending != 1 else ''}"
            if pending else "No saved update reminders remain."
        )
        if identity.active:
            detail = (
                f"Last check: {identity.remaining_count:,} images need attention; "
                f"{identity.held_count:,} are held for review."
                if identity.audit_state == "audited" else
                "Check which image Tag names still need correction."
            )
            scope = identity.handoff_scope()
            scope["start_audit"] = True
            self._row("identity", "1. Check Image Tags", detail,
                      identity.change_labels, "Review Matching Images…", scope)
        count = projection.cached_affected_image_count
        scope_text = (
            "No matching images were found in the saved index. Check the current "
            "libraries to finish this step if nothing needs updating."
            if count == 0 else
            f"The saved index estimates {count:,} matching images. Check the current scope before updating."
            if count is not None else
            "The matching-image count is not known yet. Check the current included libraries."
        )
        labels = {
            "captions": ("Training Captions", "Preview changes to adjacent Training TXT files."),
            "metadata": ("Metadata Backups", "Preview the managed image Metadata fields and backup protection."),
            "organization": ("Folder Organization", "Audit the affected images against the current folder rules."),
        }
        for number, target in enumerate(projection.pending_targets, 2 if identity.active else 1):
            title, detail = labels[target]
            self._row(target, f"{number}. {title}", f"{scope_text} {detail}",
                      projection.change_labels, f"Check {title}…",
                      projection.handoff_scope(target), dismiss=True)
        if identity.recovery_available:
            scope = identity.handoff_scope()
            scope["start_audit"] = False
            self._row("identity_recovery", "Previous Tag Update — Optional Recovery",
                      "This is completed work, not a pending update. Open the saved "
                      "record only if you want to review restoring it; newer edits are protected.",
                      (), "Review Recovery…", scope)

    def _row(self, target, title, detail, changes, action, scope, *, dismiss=False):
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        text = QLabel(detail)
        text.setWordWrap(True)
        text.setTextFormat(Qt.PlainText)
        layout.addWidget(text)
        if changes:
            saved = QLabel("Saved changes: " + "; ".join(changes[:3]) +
                           (f"; and {len(changes)-3} more" if len(changes) > 3 else ""))
            saved.setWordWrap(True)
            saved.setTextFormat(Qt.PlainText)
            layout.addWidget(saved)
        button = QPushButton(action)
        button.setAccessibleDescription(detail)
        button.clicked.connect(lambda: self.requested.emit(target, scope))
        actions = QHBoxLayout()
        actions.addStretch()
        if dismiss:
            skip = QPushButton("Dismiss Reminder")
            skip.setToolTip("Remove this reminder without updating files. Future Tag changes can create a new reminder.")
            skip.setAccessibleDescription(skip.toolTip())
            skip.clicked.connect(lambda: self.dismissal_requested.emit(
                target, scope["notice_updated_at"],
            ))
            self.dismiss_actions[target] = skip
            actions.addWidget(skip)
        actions.addWidget(button)
        layout.addLayout(actions)
        self.actions[target] = button
        self.rows.addWidget(group)
