from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.training_caption_review import decision_page, decision_text, review_caption, text_page


class CaptionPageSpinBox(QSpinBox):
    """Enter changes the page without activating the enclosing dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, event):
        if (obj is self.lineEdit() and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            self.interpretText()
            event.accept()
            return True
        return super().eventFilter(obj, event)


class PagedCaptionText(QWidget):
    """One bounded exact-text window, including long Unicode source facts."""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self._text = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.status = QLabel(name)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.page = CaptionPageSpinBox()
        self.page.setPrefix("Part ")
        self.page.setAccessibleName(name + " text part")
        self.page.valueChanged.connect(self._render)
        row.addWidget(self.status, 1)
        row.addWidget(self.page)
        layout.addLayout(row)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(name)
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.text, 1)
        self._name = name
        self.set_text("")

    def set_text(self, value):
        self._text = value
        self.page.blockSignals(True)
        self.page.setRange(1, text_page(value).pages)
        self.page.setValue(1)
        self.page.blockSignals(False)
        self._render()

    def _render(self, *_args):
        part = text_page(self._text, self.page.value() - 1)
        self.text.setPlainText(part.text)
        extent = f"{part.start + 1:,}–{part.end:,} of {part.total:,}" if part.total else "0"
        self.status.setText(f"{self._name} · characters {extent}")
        self.status.setToolTip(self.status.text())
        self.page.setVisible(part.pages > 1)


class TrainingCaptionReviewView(QWidget):
    inspect_requested = Signal(str)
    edit_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._candidate = None
        self._plan = None
        self._rows = ()
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.summary.setAccessibleName("Selected caption review context")
        layout.addWidget(self.summary)
        self.question = QLabel()
        self.question.setWordWrap(True)
        self.question.setTextFormat(Qt.PlainText)
        layout.addWidget(self.question)
        self.context_button = QPushButton("Show Context")
        self.context_button.setCheckable(True)
        self.context_button.toggled.connect(self._context_toggled)
        self.back = QPushButton("Show Image")
        self.back.clicked.connect(lambda: self._emit(self.inspect_requested))
        self.edit = QPushButton("Open Image Knowledge…")
        self.edit.setToolTip("Open the source editor to inspect or correct facts. Omitted or combined caption facts do not require source edits; their reasons describe the selected projection policy.")
        self.edit.clicked.connect(lambda: self._emit(self.edit_requested))
        actions = QHBoxLayout()
        actions.addWidget(self.back)
        actions.addWidget(self.edit)
        actions.addStretch(1)
        actions.addWidget(self.context_button)
        layout.addLayout(actions)
        self.context = PagedCaptionText("Goal, profile and measurement context")
        self.context.setMaximumHeight(150)
        self.context.hide()
        layout.addWidget(self.context)
        self.caption = PagedCaptionText("Package caption")
        self.caption.setMaximumHeight(165)
        layout.addWidget(self.caption, 1)
        self.fact_status = QLabel()
        self.fact_status.setWordWrap(True)
        layout.addWidget(self.fact_status)
        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Recorded Fact", "Source / Owner", "Treatment", "Caption Output"))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAccessibleName("Profile decisions for the selected image")
        for column in range(4):
            self.tree.header().setSectionResizeMode(column, QHeaderView.Interactive)
            self.tree.setColumnWidth(column, (145, 135, 95, 145)[column])
        self.tree.currentItemChanged.connect(self._selected_decision)
        splitter.addWidget(self.tree)
        self.detail = PagedCaptionText("Exact fact and projection reason")
        splitter.addWidget(self.detail)
        splitter.setSizes((520, 440))
        layout.addWidget(splitter, 2)
        pager = QHBoxLayout()
        self.fact_page = CaptionPageSpinBox()
        self.fact_page.setPrefix("Fact Page ")
        self.fact_page.setAccessibleName("Caption source fact page")
        self.fact_page.valueChanged.connect(self._render_decisions)
        note = QLabel("Image Knowledge stays unchanged. These are projection decisions, not a training-quality score.")
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")
        pager.addWidget(note, 1)
        pager.addWidget(self.fact_page)
        layout.addLayout(pager)
        self.clear("Choose a candidate in Candidates to inspect its caption.")

    def _emit(self, signal):
        if self._candidate is not None:
            signal.emit(self._candidate.stable_id)

    def _context_toggled(self, shown):
        self.context.setVisible(shown)
        self.context_button.setText("Hide Context" if shown else "Show Context")

    def clear(self, message):
        self._candidate = self._plan = None
        self._rows = ()
        self.summary.setText(message)
        self.question.clear()
        self.tree.clear()
        self.caption.set_text("")
        self.context.set_text("")
        self.detail.set_text("")
        self.context_button.setChecked(False)
        self.context_button.setEnabled(False)
        self.back.setEnabled(False)
        self.edit.setEnabled(False)
        self.fact_status.setText("No current caption evidence is displayed.")
        self.fact_page.setRange(1, 1)
        self.fact_page.setEnabled(False)

    def set_candidate(self, plan, candidate, *, can_edit=False):
        if candidate is None:
            self.clear("No candidate matches the current view. Choose an image in Candidates.")
            return
        if self._plan is plan and self._candidate is candidate:
            self.edit.setEnabled(can_edit)
            return
        self._plan, self._candidate = plan, candidate
        review = review_caption(plan, candidate)
        # Full identity/context remains in the paged exact-text disclosure.
        short_profile = review.profile[:110]
        short_target = review.target[:100]
        self.summary.setText(f"Goal: {review.goal[:140]}\nSelected image: {candidate.image.name[:120]} · {short_profile} · {short_target}")
        self.question.setText("Review for this goal: " + review.question)
        self.context.set_text(f"Goal: {review.goal}\n\nProfile: {review.profile}\nTarget: {review.target}\n\n{review.token_status}\n\n{review.context}")
        self.caption._name = (
            "Excluded caption preview" if candidate.decision != "include" else
            "Package caption" if plan.configuration is not None else "Canonical caption (handoff not configured)"
        )
        self.caption.text.setAccessibleName(self.caption._name)
        self.caption.set_text(review.caption)
        self.context_button.setEnabled(True)
        self.back.setEnabled(True)
        self.edit.setEnabled(can_edit)
        self.fact_page.blockSignals(True)
        self.fact_page.setRange(1, decision_page(candidate)[2])
        self.fact_page.setValue(1)
        self.fact_page.blockSignals(False)
        self.fact_page.setEnabled(True)
        self._render_decisions()

    def _render_decisions(self, *_args):
        self.tree.clear()
        if self._candidate is None:
            return
        self._rows, page, _pages, total = decision_page(self._candidate, self.fact_page.value() - 1)
        self.fact_status.setText(
            f"Recorded projection decisions: {len(self._rows):,} shown of {total:,}. Model tokens: not measured. Select a row for exact reasons."
            if total else "No per-fact projection trace was recorded in this snapshot. Caption quality has not been assessed."
        )
        for index, decision in enumerate(self._rows):
            values = (
                decision.get("source_value", ""),
                decision.get("owner_scope") or decision.get("source", ""),
                decision.get("action", ""), decision.get("output", ""),
            )
            row = QTreeWidgetItem(tuple(str(value) if len(str(value)) <= 160 else str(value)[:160] + "…" for value in values))
            row.setData(0, Qt.UserRole, index)
            self.tree.addTopLevelItem(row)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.detail.set_text("")

    def _selected_decision(self, current, *_args):
        if current is None:
            self.detail.set_text("")
            return
        index = current.data(0, Qt.UserRole)
        self.detail.set_text(decision_text(self._rows[index]))
