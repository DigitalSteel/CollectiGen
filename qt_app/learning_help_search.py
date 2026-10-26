"""Opt-in local Help search; the shared Help entry is integrated separately."""
from __future__ import annotations

from threading import Event

from PySide6.QtCore import QEvent, QThreadPool, QTimer, Qt, Slot
from PySide6.QtGui import QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout,
)
from shiboken6 import isValid

from core.learning_help_catalog import help_guide_topics, search_help_guides
from .components import ContextHelpDialog, compact_button
from .help_content import SECTION_HELP, WORKSPACE_HELP
from .workers import AsyncCallTask, start_qt_delivery_task


def bundled_help_topics():
    """Use current canonical guide strings; no discovery, network or user data."""
    titles = {"knowledge": "Tag Encyclopedia", "generate": "Generate",
              "automation": "Rules & Automation", "review-media": "Review Media",
              "generation-regions": "Generation Regions"}
    rows = ((f"{group}:{key}", titles.get(key, key.replace("-", " ").title()), text,
             f"qt_app/help_content.py::{group}[{key!r}]")
            for group, guides in (("WORKSPACE_HELP", WORKSPACE_HELP), ("SECTION_HELP", SECTION_HELP))
            for key, text in guides.items())
    return help_guide_topics(rows)


class LearningHelpSearchDialog(QDialog):
    """One bounded search at a time, with stale delivery and Close protection."""

    def __init__(self, parent=None, *, topics=None):
        super().__init__(parent)
        self.setWindowTitle("Search Local Help")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(880, 650)
        self._topics = bundled_help_topics() if topics is None else tuple(topics)
        self._generation = 0
        self._task = None
        self._cancel = None
        self._pending = None
        self._closing = False
        self._reader = None
        self._lifetime = Event()
        self.destroyed.connect(self._lifetime.set)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("Search local Help")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        intro = QLabel("Find words across the bundled guides, then open the original guide and its topics.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search all words, for example: training captions")
        self.query.setAccessibleName("Search local Help")
        self.query.setMaxLength(512)
        self.query.setClearButtonEnabled(True)
        self.query.installEventFilter(self)
        self.search_button = compact_button(QPushButton("Search"))
        self.search_button.setAutoDefault(False)
        self.cancel_button = compact_button(QPushButton("Cancel Search"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.query, 1)
        row.addWidget(self.search_button)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        scope_row = QHBoxLayout()
        label = QLabel("Look in")
        self.scope = QComboBox()
        self.scope.setAccessibleName("Help guide filter")
        self.scope.addItem("All local guides", None)
        for topic in self._topics:
            self.scope.addItem(topic.title, topic.topic_id)
        label.setBuddy(self.scope)
        scope_row.addWidget(label)
        scope_row.addWidget(self.scope, 1)
        layout.addLayout(scope_row)
        self.status = QLabel("Enter words and choose Search. Help stays read-only.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Local Help search status")
        layout.addWidget(self.status)
        self.results = QListWidget()
        self.results.setAccessibleName("Matching Help guides")
        layout.addWidget(self.results, 2)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Selected Help excerpt")
        self.preview.setPlaceholderText("Select a guide to read its matching excerpt.")
        layout.addWidget(self.preview, 1)
        self.source = QLabel("Review dates and individual reviewer details are not recorded in these guides.")
        self.source.setTextFormat(Qt.PlainText)
        self.source.setWordWrap(True)
        self.source.setObjectName("mutedLabel")
        layout.addWidget(self.source)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.open_button = self.buttons.addButton("Open Guide", QDialogButtonBox.ActionRole)
        self.open_button.setEnabled(False)
        for button in self.buttons.buttons():
            button.setAutoDefault(False)
        self.buttons.rejected.connect(self.close)
        self.open_button.clicked.connect(self.open_selected)
        layout.addWidget(self.buttons)
        self.query.textChanged.connect(self._invalidate)
        self.scope.currentIndexChanged.connect(self._invalidate)
        self.query.returnPressed.connect(self.start_search)
        self.search_button.clicked.connect(self.start_search)
        self.cancel_button.clicked.connect(self.cancel_search)
        self.results.currentItemChanged.connect(self._selection_changed)
        self.results.itemActivated.connect(self.open_selected)
        self._find = QShortcut(QKeySequence.Find, self)
        self._find.activated.connect(self._focus_search)
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._settle_task)
        self.query.setFocus()

    def _focus_search(self):
        self.query.setFocus()
        self.query.selectAll()

    def eventFilter(self, watched, event):
        if watched is self.query and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Down:
            if self.results.count():
                self.results.setCurrentRow(0)
                self.results.setFocus()
                return True
        return super().eventFilter(watched, event)

    def _clear_results(self):
        self.results.clear()
        self.preview.clear()
        self.open_button.setEnabled(False)
        self.source.setText("Review dates and individual reviewer details are not recorded in these guides.")
        self.source.setToolTip("")

    def _cancel_task(self):
        if self._cancel is not None:
            self._cancel.set()
        if self._task is not None:
            self._task.cancel()

    def _invalidate(self, *_):
        self._generation += 1
        self._pending = None
        self._cancel_task()
        self._clear_results()
        self.cancel_button.setEnabled(False)
        self.status.setText("Search changed. Press Enter or choose Search." if self.query.text().strip()
                            else "Enter words and choose Search. Help stays read-only.")

    def start_search(self, *_):
        if self._closing:
            return
        self._invalidate()
        query = self.query.text().strip()
        if not query:
            return
        self._pending = (self._generation, query, self.scope.currentData())
        self.status.setText("Searching local Help…")
        self.cancel_button.setEnabled(True)
        self._poll.start()
        self._settle_task()

    def _settle_task(self):
        if self._task is not None:
            if not self._task.is_finished():
                return
            if self._task.state == "failed" and self._task_generation == self._generation and not self._closing:
                self.status.setText("Help search is unavailable. Try again or keep reading the original guide.")
            self._task = None
            self._cancel = None
            self.cancel_button.setEnabled(False)
        if self._closing:
            self.close()
            return
        if self._pending is None:
            self._poll.stop()
            return
        generation, query, guide_id = self._pending
        self._pending = None
        topics, cancelled, lifetime = self._topics, Event(), self._lifetime
        self._cancel = cancelled
        def search():
            matches = search_help_guides(topics, query, guide_id=guide_id,
                should_continue=lambda: not cancelled.is_set() and not lifetime.is_set())
            return generation, matches
        self._task_generation = generation
        self._task = AsyncCallTask(search)
        self._task.signals.complete.connect(self._deliver)
        self.cancel_button.setEnabled(True)
        start_qt_delivery_task(QThreadPool.globalInstance(), self._task)

    @Slot(object)
    def _deliver(self, result):
        generation, matches = result
        if self._closing or generation != self._generation:
            return
        self._clear_results()
        for match in matches:
            item = QListWidgetItem(match.topic.title)
            item.setData(Qt.UserRole, match)
            self.results.addItem(item)
        self.status.setText(f"{len(matches)} matching guides. Select one and choose Open Guide."
                            if matches else "No matching guides. Try fewer words or choose All local guides.")

    def _selection_changed(self, item, _previous=None):
        self.open_button.setEnabled(item is not None and not self._closing)
        if item is None:
            self.preview.clear()
            return
        match = item.data(Qt.UserRole)
        excerpt = QTextDocument()
        excerpt.setMarkdown(match.excerpt)
        self.preview.setPlainText(excerpt.toPlainText())
        self.source.setText(f"Bundled guide: {match.topic.title}\nReview date: not recorded. Search does not verify freshness.")
        self.source.setToolTip(match.topic.source)

    def open_selected(self, *_):
        item = self.results.currentItem()
        if item is None or self._closing or not self.open_button.isEnabled():
            return
        topic = item.data(Qt.UserRole).topic
        if self._reader is not None and isValid(self._reader):
            self._reader.close()
        self._reader = ContextHelpDialog(topic.title, topic.body, self)
        self._reader.show()
        self._reader.raise_()

    def cancel_search(self):
        self._invalidate()
        self.status.setText("Search cancelled. Change the words or search again.")

    def reject(self):
        self.close()

    def closeEvent(self, event):
        self._closing = True
        self._pending = None
        self._cancel_task()
        if self._reader is not None and isValid(self._reader):
            self._reader.close()
        if self._task is not None and not self._task.is_finished():
            self.query.setEnabled(False)
            self.scope.setEnabled(False)
            self.search_button.setEnabled(False)
            self.open_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.status.setText("Closing after the search stops…")
            self._poll.start()
            event.ignore()
            return
        self._poll.stop()
        self._lifetime.set()
        event.accept()


def add_help_search_button(dialog, buttons):
    """Minimal opt-in join for the shared ContextHelpDialog footer owner."""
    button = buttons.addButton("Search All Help…", QDialogButtonBox.ActionRole)
    button.setAutoDefault(False)
    button.setAccessibleName("Search all local Help guides")
    def show_search():
        parent = dialog.parentWidget()
        if isinstance(parent, LearningHelpSearchDialog) and not parent._closing:
            dialog.close()
            parent.raise_()
            parent.activateWindow()
            return
        current = getattr(dialog, "_learning_search_dialog", None)
        if current is not None and isValid(current) and current.isVisible():
            current.raise_()
            current.activateWindow()
            return
        current = LearningHelpSearchDialog(dialog)
        dialog._learning_search_dialog = current
        current.show()
    button.clicked.connect(show_search)
    return button
