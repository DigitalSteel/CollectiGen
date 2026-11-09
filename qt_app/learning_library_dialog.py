"""Offline instructional workspace; content and private reading state stay separate."""
from __future__ import annotations

from collections import Counter
from datetime import date
from hashlib import sha256
from pathlib import Path
from threading import Event

from PySide6.QtCore import QByteArray, QThreadPool, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser,
    QVBoxLayout,
)
from shiboken6 import isValid

from core.learning_library import (
    export_pack, open_bundled_library, read_pack_file, write_pack_file,
)
from core.learning_topic_search import KINDS, LearningTopicIndex
from .components import ContextHelpDialog, DisclosureButton, compact_button
from .learning_help_search import LEARNING_CONTEXT_TOPICS, LearningHelpSearchDialog
from .workers import AsyncCallTask, start_qt_delivery_task


def _review_label(state):
    return {"reviewed-source": "source editorial review recorded", "review-needed": "review needed",
            "deprecated": "deprecated guidance", "unsupported": "unsupported workflow"}[state]


def _applicability_text(topic):
    return "; ".join(f"{tool}: {version}" for tool, version in topic.applicability) or "not recorded"


def _date_text(value):
    return "not recorded" if value == date.min else value.isoformat()


class SafeTopicBrowser(QTextBrowser):
    """No article can fetch a file, image, stylesheet or network resource."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextInteractionFlags(self.textInteractionFlags() | Qt.TextSelectableByKeyboard)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)

    def loadResource(self, resource_type, name):
        return QByteArray()


class _SafeExcerptDocument(QTextDocument):
    def loadResource(self, resource_type, name):
        return QByteArray()


class LearningTopicReader(ContextHelpDialog):
    topic_requested = Signal(str)

    def __init__(self, article, related, parent=None):
        # The original reader receives neither an untrusted title nor body.
        super().__init__("Learning topic", "", parent)
        self.setWindowTitle(article.topic.title)
        heading = self.layout().itemAt(0).widget()
        heading.setTextFormat(Qt.PlainText)
        heading.setWordWrap(True)
        heading.setText(article.topic.title)
        if len(article.topic.title) > 120:
            # Keep a valid long title fully reachable without squeezing the
            # article and its persistent actions out of the reading viewport.
            self.title_text = QPlainTextEdit(self)
            self.title_text.setAccessibleName("Full learning topic title")
            self.title_text.setReadOnly(True)
            self.title_text.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            self.title_text.setFrameShape(QFrame.NoFrame)
            self.title_text.setFont(heading.font())
            self.title_text.setMaximumHeight(110)
            self.title_text.setPlainText(article.topic.title)
            self.title_text.setToolTip("Full topic title. Use arrow keys or Ctrl+End to read its end.")
            self.layout().replaceWidget(heading, self.title_text)
            heading.hide()
            heading.deleteLater()
        self.evidence_status = QLabel()
        self.evidence_status.setTextFormat(Qt.PlainText)
        self.evidence_status.setWordWrap(True)
        self.evidence_status.setAccessibleName("Learning guidance qualification")
        origin = "Bundled guidance" if article.origin == "bundled" else "Imported guidance; publisher/review unverified"
        evidence = f"{origin} · {_review_label(article.review_state)} · As of {_date_text(article.topic.as_of)}. Installed compatibility unverified."
        if getattr(parent, "_revision_counts", {}).get(article.topic.topic_id, 0) > 1:
            evidence += " Multiple revisions are available; this is only the chosen revision."
        self.evidence_status.setText(evidence)
        self.layout().insertWidget(1, self.evidence_status)
        old = self.guide
        self.guide = SafeTopicBrowser(self)
        self.guide.setAccessibleName("Learning topic article")
        self.layout().replaceWidget(old, self.guide)
        old.deleteLater()
        self.guide.anchorClicked.connect(self._anchor)
        self.guide.setMarkdown(article.topic.body)
        self.topics.clear()
        block = self.guide.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() > 0:
                self.topics.addItem(block.text(), block.position())
            block = block.next()
        self.topics.setVisible(self.topics.count() > 1)
        for label in self.findChildren(QLabel):
            if label.text() == "Topic":
                label.setVisible(self.topics.count() > 1)
        self._find_in_guide("")
        row = QHBoxLayout()
        self.related = QComboBox()
        self.related.setAccessibleName("Related learning topics")
        for identity, title in related:
            self.related.addItem(title, identity)
        self.follow = compact_button(QPushButton("Read Related Topic"))
        self.follow.setAutoDefault(False)
        self.follow.setEnabled(bool(related))
        self.follow.clicked.connect(lambda: self.topic_requested.emit(self.related.currentData()))
        row.addWidget(self.related, 1)
        row.addWidget(self.follow)
        self.layout().insertLayout(self.layout().count() - 1, row)
        # The inherited footer already knows how to return to the same owner.
        # Name that action accurately and avoid two competing return controls.
        for button in self.findChildren(QPushButton):
            if button.accessibleName() == "Search all local Help guides":
                button.setText("Return to Library")
                button.setAccessibleName("Return to Learning Library")
            elif button.accessibleName() == "Open offline Learning Library":
                button.hide()
        self.library_help_shortcut = QShortcut(QKeySequence(Qt.Key_F1), self)
        self.library_help_shortcut.activated.connect(lambda: self.topic_requested.emit("learning:library"))
        # Article provenance and related links need more room than plain Help.
        # Keep the initial reading area useful without forcing a screen minimum.
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(1000, max(1, available.width() - 48)),
                        min(840, max(1, available.height() - 80)))

    def _anchor(self, url):
        if url.scheme() == "learning" and not url.query() and not url.fragment():
            self.topic_requested.emit(url.toString())
        elif not url.scheme() and not url.path() and url.fragment():
            self.guide.scrollToAnchor(url.fragment())


class LearningLibraryDialog(LearningHelpSearchDialog):
    """Reuse shared search controls and one physically drained AsyncCallTask owner.

    All parsing, indexing and state IO happen in that owner. Mutating calls are
    never cancelled after dispatch; Close waits for their final physical return.
    """

    def __init__(self, parent=None, *, library=None, state_dir=None, context=None, topic_id=None):
        self.library = library
        self._state_dir = state_dir
        self._provided_library = library
        self._articles = ()
        self._cohorts = ()
        self._bookmarks = ()
        self._history = ()
        self._current = None
        self._drafts = {}
        self._saved_notes = {}
        self._setting_note = False
        self._op_result = None
        self._on_done = None
        self._mutating = False
        self._review = None
        self._packs = None
        self._initial_topic = topic_id
        self._reading_stack = []
        self._refresh_notice = None
        self._context = context if context in LEARNING_CONTEXT_TOPICS else None
        self._unknown_context = context is not None and self._context is None
        super().__init__(parent, topics=())
        self.setProperty("collectigenLearningLibraryOwner", True)
        self.setWindowTitle("Learning Library")
        self.resize(960, 680)
        self.layout().setContentsMargins(16, 12, 16, 12)
        self.layout().setSpacing(8)
        self.preview.setMinimumHeight(45)
        self.preview.setMaximumHeight(85)
        self.guide_details.setMinimumHeight(45)
        self.guide_details.setMaximumHeight(85)
        self.layout().itemAt(0).widget().setText("Learning Library")
        self.layout().itemAt(1).widget().setText(
            "Read offline guidance for your goal. Saved reading and notes stay separate from image data.")
        self.query.setAccessibleName("Search the Learning Library")
        self.query.setPlaceholderText("Search words or leave empty to browse topics")
        self.search_button.setText("Find Topics")
        self.open_button.setText("Read Topic")
        self.back_button = self.buttons.addButton("Previous Topic", QDialogButtonBox.ActionRole)
        self.back_button.setAutoDefault(False)
        self.back_button.clicked.connect(self._go_back)
        self.library_help_button = self.buttons.addButton("Using This Library", QDialogButtonBox.HelpRole)
        self.library_help_button.setAutoDefault(False)
        self.library_help_button.clicked.connect(lambda: self._navigate("learning:library"))
        self.library_help_shortcut = QShortcut(QKeySequence(Qt.Key_F1), self)
        self.library_help_shortcut.activated.connect(self.library_help_button.click)
        self.cancel_button.setText("Stop Search")
        self.scope.setAccessibleName("Learning subject")
        self.scope.setEditable(True)
        self.scope.setInsertPolicy(QComboBox.NoInsert)
        self.scope.lineEdit().setPlaceholderText("All subjects, or type an exact subject")
        self.scope.setToolTip("The list shows at most 128 subjects. Type any exact subject, including one absent from the list.")
        self.scope.clear()
        self.scope.addItem("", None)
        self.layout().itemAt(3).layout().itemAt(0).widget().setText("Subject")
        self.kind = QComboBox()
        self.kind.setAccessibleName("Instruction kind")
        self.kind.addItem("All kinds", None)
        for value in sorted(KINDS):
            self.kind.addItem(value.replace("-", " ").title(), value)
        self.reading_view = QComboBox()
        self.reading_view.setAccessibleName("Saved reading view")
        self.reading_view.addItem("All topics", "all")
        self.reading_view.addItem("Bookmarks", "bookmarks")
        self.reading_view.addItem("Reading history", "history")
        row = QHBoxLayout()
        row.addWidget(QLabel("Kind"))
        row.addWidget(self.kind, 1)
        row.addWidget(self.reading_view, 1)
        self.context_button = compact_button(QPushButton("For This Page"))
        self.context_button.setCheckable(True)
        self.context_button.setChecked(self._context is not None)
        self.context_button.setEnabled(self._context is not None)
        self.context_button.setToolTip(f"Exact Help context: {self._context}" if self._context else "No exact page context supplied")
        row.addWidget(self.context_button)
        self.packs_button = compact_button(QPushButton("Learning Packs…"))
        row.addWidget(self.packs_button)
        self.layout().insertLayout(4, row)
        # Disabled filters keep their choices while progress uses the space.
        subject_row = self.layout().itemAt(3).layout()
        self._reading_filter_controls = tuple(
            group.itemAt(index).widget()
            for group in (subject_row, row) for index in range(group.count()))
        self.kind.currentIndexChanged.connect(self._invalidate)
        self.scope.editTextChanged.connect(self._invalidate)
        self.reading_view.currentIndexChanged.connect(self.start_search)
        self.context_button.toggled.connect(self.start_search)
        self.packs_button.clicked.connect(self.show_packs)
        self.results.setAccessibleName("Learning topics")
        self.preview.setAccessibleName("Selected learning topic excerpt")
        self.preview.setPlaceholderText("Select a topic, then choose Read Topic.")
        self.details_button.show_text = "Source and Review"
        self.details_button.hide_text = "Hide Source Details"
        self.details_button.setText(self.details_button.show_text)
        self.note = QPlainTextEdit()
        self.note.setAccessibleName("Private learning note")
        self.note.setPlaceholderText("Your note for this topic. Save explicitly; pack transfer never exports notes.")
        self.note.setMinimumHeight(45)
        self.note.setMaximumHeight(85)
        self.notes_button = DisclosureButton("My Note", "Hide My Note", self.note)
        self.notes_button.setAutoDefault(False)
        self.save_note_button = compact_button(QPushButton("Save Note"))
        self.bookmark_button = compact_button(QPushButton("Bookmark Topic"))
        self.reload_note_button = compact_button(QPushButton("Reload Saved Note…"))
        note_row = QHBoxLayout()
        # The selected excerpt, provenance and private note share reading space.
        self.layout().removeWidget(self.details_button)
        self.layout().removeWidget(self.guide_details)
        note_row.addWidget(self.details_button)
        note_row.addWidget(self.notes_button)
        note_row.addWidget(self.save_note_button)
        note_row.addWidget(self.reload_note_button)
        note_row.addWidget(self.bookmark_button)
        note_row.addStretch(1)
        self.layout().insertLayout(self.layout().count() - 1, note_row)
        self.layout().insertWidget(self.layout().count() - 1, self.guide_details)
        self.layout().insertWidget(self.layout().count() - 1, self.note)
        self.note.textChanged.connect(self._note_changed)
        self.save_note_button.clicked.connect(self.save_note)
        self.bookmark_button.clicked.connect(self.toggle_bookmark)
        self.reload_note_button.clicked.connect(self.reload_note)
        self.notes_button.toggled.connect(lambda checked: checked and self.details_button.setChecked(False))
        self.details_button.toggled.connect(lambda checked: checked and self.notes_button.setChecked(False))
        show_excerpt = lambda *_: self.preview.setVisible(
            not (self.notes_button.isChecked() or self.details_button.isChecked()))
        self.notes_button.toggled.connect(show_excerpt)
        self.details_button.toggled.connect(show_excerpt)
        self.notes_button.toggled.connect(lambda _: self._note_changed())
        self._submit("Loading installed guidance", lambda: library or open_bundled_library(state_dir), self._loaded)

    def _set_busy(self, busy):
        for widget in self._reading_filter_controls:
            widget.setVisible(not busy)
        for widget in (self.query, self.scope, self.kind, self.reading_view, self.results,
                       self.search_button, self.packs_button):
            widget.setEnabled(not busy and not self._closing)
        self.context_button.setEnabled(not busy and self._context is not None and not self._closing)
        selected = self.results.currentItem() is not None
        self.open_button.setEnabled(not busy and selected and not self._closing)
        self.bookmark_button.setEnabled(not busy and self._current is not None)
        self.save_note_button.setEnabled(not busy and self._note_dirty())
        self.reload_note_button.setEnabled(not busy and getattr(self, "_note_identity", None) is not None)
        self.note.setEnabled(not busy and not self._closing and getattr(self, "_note_identity", None) is not None)
        self.back_button.setEnabled(not busy and len(self._reading_stack) > 1)
        self.library_help_button.setEnabled(not busy and not self._closing)
        self.cancel_button.setEnabled(busy and not self._mutating and not self._closing)
        if self._packs is not None and isValid(self._packs):
            self._packs.setEnabled(not busy)
            if busy:
                self._packs.hide()

    def _submit(self, phase, function, done, *, mutating=False):
        if self._closing or self._task is not None:
            return False
        self._mutating = mutating
        self._active_scope = phase
        self._on_done = done
        self._op_result = None
        self._cancel = Event()
        self._elapsed.start()
        self._task = AsyncCallTask(function)
        self._task.signals.complete.connect(self._received)
        self.status.setText(phase + (". Close waits for this save to finish." if mutating else ". You can stop or close."))
        self._set_busy(True)
        self._update_activity()
        self._poll.start()
        start_qt_delivery_task(QThreadPool.globalInstance(), self._task)
        return True

    def _received(self, result):
        self._op_result = (result,)
        self._settle_task()

    def _settle_task(self):
        self._update_activity()
        task = self._task
        if task is None or not task.is_finished():
            return
        if task.state == "complete" and self._op_result is None:
            return  # queued delivery still owns its result
        done, outcome, was_mutating = self._on_done, self._op_result, self._mutating
        self._task = self._cancel = self._on_done = self._op_result = None
        self._mutating = False
        if self._closing and was_mutating and task.state == "failed":
            # A requested Close must not turn a failed save into lost draft text.
            self._closing = False
        self._poll.stop()
        self._set_busy(False)
        self._update_activity()
        if self._closing:
            self.close()
        elif task.state == "failed":
            self.status.setText(self._after_refresh(f"{self._active_scope} failed: {task.error}. Your unsaved note stays available; review and try again."))
            if self.library is None:
                self.search_button.setText("Retry Loading")
            self._pack_status()
        elif task.state == "cancelled":
            self.status.setText(self._after_refresh("Stopped. Your saved content and unsaved note stay available."))
            self._pack_status()
        elif outcome is not None:
            done(outcome[0])

    def _update_activity(self):
        if self._task is None:
            self.activity.set_active(False)
            self.activity.hide()
            self.activity_scope.hide()
            return
        seconds = max(0, self._elapsed.elapsed() // 1000)
        phase = "Finishing before Close" if self._closing else self._active_scope
        message = f"{phase} • Elapsed {seconds}s • Total not yet known"
        self.activity_scope.setText(message)
        self.activity_scope.show()
        self.activity.set_active(True, message)
        self.activity.show()

    def _loaded(self, library):
        self.library = library
        self.search_button.setText("Find Topics")
        self._reload()

    def _snapshot(self):
        cohorts = self.library.cohorts()
        subjects = set()
        for article in (a for c in cohorts for a in c.articles):
            for tag in article.topic.tags:
                subjects.add(tag)
                if len(subjects) > 128:
                    break
            if len(subjects) > 128:
                break
        return cohorts, self.library.bookmarks(), self.library.history(1000), tuple(sorted(subjects)[:128]), len(subjects) > 128

    def _reload(self):
        if self.library is None:
            self._submit("Loading installed guidance", lambda: self._provided_library or open_bundled_library(self._state_dir), self._loaded)
        else:
            self._submit("Reading installed topics and saved reading", self._snapshot, self._snapshot_ready)

    def _snapshot_ready(self, snapshot):
        self._cohorts, self._bookmarks, self._history, subjects, self._more_subjects = snapshot
        self._articles = tuple(article for cohort in self._cohorts for article in cohort.articles)
        self._revision_counts = Counter(article.topic.topic_id for article in self._articles)
        previous = self._subject_value()
        self.scope.blockSignals(True)
        self.scope.clear()
        self.scope.addItem("", None)
        for tag in subjects:
            self.scope.addItem(tag, tag)
        index = self.scope.findData(previous)
        self.scope.setCurrentIndex(max(0, index))
        if previous is not None and index < 0:
            self.scope.setEditText(previous)
        self.scope.blockSignals(False)
        self._refresh_packs()
        self.start_search()

    def _subject_value(self):
        text = self.scope.currentText()
        return text if text else None

    def _invalidate(self, *_):
        if not hasattr(self, "kind"):
            return
        self._stash_note()
        self._clear_results()
        self.status.setText("Choose Find Topics to apply these words and filters.")

    def start_search(self, *_):
        if self._task is not None or self._closing:
            return
        if self.library is None:
            self._reload()
            return
        self._stash_note()
        self._clear_results()
        query, subject, kind = self.query.text().strip(), self._subject_value(), self.kind.currentData()
        view = self.reading_view.currentData()
        context = self._context if self.context_button.isChecked() else None
        articles, bookmarks, history = self._articles, set(self._bookmarks), self._history
        history_keys = {row[:2] for row in history}
        cancel = Event()
        def search():
            candidates = tuple(a for a in articles if (not context or context in a.contexts)
                and (view != "bookmarks" or (a.topic.topic_id, a.topic.revision) in bookmarks)
                and (view != "history" or (a.topic.topic_id, a.topic.revision) in history_keys))
            if query:
                index = LearningTopicIndex((a.topic for a in candidates), should_continue=lambda: not cancel.is_set())
                try:
                    matches = index.search(query, tag=subject, kind=kind, limit=100,
                        dated_before=date.today(), should_continue=lambda: not cancel.is_set())
                    by_key = {(a.topic.topic_id, a.topic.revision): a for a in candidates}
                    found = [(by_key[(m.topic.topic_id, m.topic.revision)], m.excerpt, m.qualifications) for m in matches]
                finally:
                    index.close()
            else:
                revisions = Counter(a.topic.topic_id for a in candidates)
                candidates = tuple(a for a in candidates if (subject is None or subject in a.topic.tags)
                    and (kind is None or kind == a.topic.kind))
                if view == "history":
                    order = {row[:2]: number for number, row in reversed(list(enumerate(history)))}
                    candidates = tuple(sorted(candidates, key=lambda a: order.get((a.topic.topic_id, a.topic.revision), 1000)))
                found = [(a, a.topic.body[:800], tuple(
                    (["multiple-topic-revisions"] if revisions[a.topic.topic_id] > 1 else [])
                    + (["dated-before-today"] if a.topic.as_of < date.today() else []))) for a in candidates[:100]]
            return found
        if self._submit("Finding local topics", search, self._results_ready):
            self._cancel = cancel

    def _results_ready(self, found):
        self.results.blockSignals(True)
        for article, excerpt, qualifications in found:
            item = QListWidgetItem(f"{article.topic.title} · {article.topic.revision}")
            item.setData(Qt.UserRole, (article, excerpt, qualifications))
            self.results.addItem(item)
        self.results.setCurrentRow(-1)
        self.results.blockSignals(False)
        self.status.setText(f"{len(found)} topics shown (maximum 100). Select one and choose Read Topic. Narrow words or filters for more."
                            if found else "No topics match. Broaden words, subjects, kind or saved-reading view; turn off For This Page to browse all guidance.")
        if self._unknown_context:
            self.status.setText("No exact guidance mapping for this page. Browse or search all installed topics. " + self.status.text())
        if self._more_subjects:
            self.status.setText(self.status.text() + " First 128 subjects listed; type any exact subject to reach others.")
        if self.reading_view.currentData() != "all":
            self.status.setText(self.status.text() + " Only active installed revisions are shown; saved references to inactive revisions are retained.")
        self.status.setText(self._after_refresh(self.status.text()))
        if self._initial_topic:
            identity, self._initial_topic = self._initial_topic, None
            self._navigate(identity)

    def _after_refresh(self, text):
        notice, self._refresh_notice = self._refresh_notice, None
        return notice + "\n" + text if notice else text

    def _clear_results(self):
        if hasattr(self, "note"):
            self._stash_note()
        self.results.blockSignals(True)
        try:
            super()._clear_results()
        finally:
            self.results.blockSignals(False)
        self.source.setText("Select a topic to inspect its source and review details.")
        if hasattr(self, "note"):
            self._current = None
            self._set_note(None, "")

    def _selection_changed(self, item, _previous=None):
        if not hasattr(self, "note"):
            return
        self._stash_note()
        if item is None:
            self.open_button.setEnabled(False)
            return
        article, excerpt, qualifications = item.data(Qt.UserRole)
        self._set_note(None, "")
        self._display_article(article, excerpt, qualifications)
        identity = article.topic.topic_id
        def ready(saved):
            self._saved_notes[identity] = saved
            self._set_note(identity, self._drafts.get(identity, saved))
            self.status.setText(f"Selected {article.topic.title}. Choose Read Topic; notes save separately.")
        self._submit("Loading this topic's note", lambda: self.library.note(identity), ready)

    def _display_article(self, article, excerpt, qualifications=()):
        self._current = article
        # Both Markdown paths block resource loading before parsing any content.
        document = _SafeExcerptDocument()
        document.setMarkdown(excerpt)
        self.preview.setPlainText(document.toPlainText())
        topic = article.topic
        qualifications = tuple(qualifications)
        if self._revision_counts.get(topic.topic_id, 0) > 1 and "multiple-topic-revisions" not in qualifications:
            qualifications += ("multiple-topic-revisions",)
        if topic.as_of != date.min and topic.as_of < date.today() and not any(value.startswith("dated-before") for value in qualifications):
            qualifications += ("dated-before-today",)
        labels = []
        if topic.as_of == date.min:
            labels.append("Source date not recorded")
        elif any(value.startswith("dated-before") for value in qualifications):
            labels.append("Earlier source date; freshness unverified")
        if "multiple-topic-revisions" in qualifications:
            labels.append("Multiple revisions; guidance is not merged")
        qualifiers = " · ".join(labels)
        origin = "Bundled guidance" if article.origin == "bundled" else "Imported guidance; publisher/review unverified"
        self.source.setText(f"{origin} · {_review_label(article.review_state)}\nAs of {_date_text(topic.as_of)}"
            + (f" · {qualifiers}" if qualifiers else "") + ". Installed compatibility unverified.")
        self.source.setToolTip(topic.source)
        self.guide_details.setPlainText(
            f"Source: {topic.source}\nTopic/revision: {topic.topic_id} / {topic.revision}\n"
            f"Author: {topic.author}\nReviewer: {topic.reviewer}\nLicense: {topic.license}\n"
            f"Origin: {origin}\nReview claim: {_review_label(article.review_state)}\n"
            f"Applicability: {_applicability_text(topic)}\nAs of: {_date_text(topic.as_of)}\n"
            "Imported review claims are unverified. Dates and checksums do not establish publisher trust or machine readiness.")
        self.details_button.setEnabled(True)
        self.open_button.setEnabled(self._task is None)
        self.bookmark_button.setText("Remove Bookmark" if (topic.topic_id, topic.revision) in self._bookmarks else "Bookmark Topic")
        self._set_busy(self._task is not None)

    def _navigate(self, identity):
        if self._closing or self._task is not None:
            return
        matches = [a for a in self._articles if a.topic.topic_id == identity]
        if not matches:
            message = "That related topic is not installed. Your current article and note remain available."
            self.status.setText(message)
            if self._reader is not None and isValid(self._reader):
                self._reader.find_status.setText(message)
                self._reader.find_status.show()
            return
        if len(matches) > 1:
            dialog = QDialog(self)
            dialog.setAttribute(Qt.WA_DeleteOnClose)
            dialog.setWindowTitle("Choose Exact Topic Revision")
            dialog.setModal(True)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("More than one installed revision matches. Choose one; their guidance is not merged."))
            choices = QComboBox()
            choices.setAccessibleName("Exact learning topic revision")
            for article in matches:
                choices.addItem(f"Revision {article.topic.revision} · {_date_text(article.topic.as_of)} · {_review_label(article.review_state)}", article)
            layout.addWidget(choices)
            buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
            read = buttons.addButton("Read Chosen Revision", QDialogButtonBox.AcceptRole)
            read.setAutoDefault(False)
            buttons.rejected.connect(dialog.close)
            def apply():
                article = choices.currentData()
                dialog.close()
                self._open_article(article)
            read.clicked.connect(apply)
            layout.addWidget(buttons)
            self._review = dialog
            dialog.show()
            return
        article = matches[0]
        self._open_article(article)

    def open_selected(self, *_):
        item = self.results.currentItem()
        if item is not None and self._task is None and not self._closing:
            self._open_article(item.data(Qt.UserRole)[0])

    def _open_article(self, article, *, add_navigation=True):
        if self._closing or self._task is not None:
            return
        self._stash_note()
        self._current = article
        identity, revision = article.topic.topic_id, article.topic.revision
        def load():
            self.library.record_read(identity, revision)
            return self.library.note(identity), self.library.history(1000)
        def ready(value):
            saved, self._history = value
            self._saved_notes[identity] = saved
            # Context/related navigation must agree with the visible results.
            # Select its exact row when present; otherwise clear a prior row
            # rather than leaving Read Topic attached to a different article.
            self.results.blockSignals(True)
            try:
                row = next((n for n in range(self.results.count())
                    if self.results.item(n).data(Qt.UserRole)[0].topic == article.topic), -1)
                self.results.setCurrentRow(row)
            finally:
                self.results.blockSignals(False)
            self._display_article(article, article.topic.body[:800])
            self._set_note(identity, self._drafts.get(identity, saved))
            key = (identity, revision)
            if add_navigation and (not self._reading_stack or self._reading_stack[-1] != key):
                self._reading_stack.append(key)
            self.back_button.setEnabled(len(self._reading_stack) > 1)
            related = [(value, next((a.topic.title for a in self._articles if a.topic.topic_id == value), f"{value} (not installed)")) for value in article.links]
            if self._reader is not None and isValid(self._reader):
                self._reader.close()
            self._reader = LearningTopicReader(article, related, self)
            self._reader.topic_requested.connect(self._navigate)
            self._reader.show()
            self._reader.raise_()
            self.status.setText(f"Reading {article.topic.title}. Reading history saved; image data unchanged.")
        self._submit("Opening topic and saving reading history", load, ready, mutating=True)

    def _go_back(self, *_):
        if len(self._reading_stack) < 2 or self._task is not None:
            return
        key = self._reading_stack[-2]
        article = next((a for a in self._articles if (a.topic.topic_id, a.topic.revision) == key), None)
        if article is None:
            self.status.setText("The previous exact revision is not active. Your reading history is retained.")
            return
        self._reading_stack.pop()
        self._open_article(article, add_navigation=False)

    def _set_note(self, identity, body):
        self._setting_note = True
        self.note.setPlainText(body)
        self._setting_note = False
        self._note_identity = identity
        self.note.setEnabled(identity is not None)
        self._note_changed()

    def _stash_note(self):
        identity = getattr(self, "_note_identity", None)
        if identity is not None:
            body = self.note.toPlainText()
            if body != self._saved_notes.get(identity, ""):
                self._drafts[identity] = body
            else:
                self._drafts.pop(identity, None)

    def _note_dirty(self):
        identity = getattr(self, "_note_identity", None)
        return identity is not None and self.note.toPlainText() != self._saved_notes.get(identity, "")

    def _note_changed(self):
        if self._setting_note:
            return
        self._stash_note()
        dirty = self._note_dirty()
        self.notes_button.setText("My Note (Unsaved)" if dirty else "My Note")
        self.save_note_button.setEnabled(dirty and self._task is None)

    def save_note(self, *_):
        identity = getattr(self, "_note_identity", None)
        if identity is None:
            return
        body = self.note.toPlainText()
        def done(_):
            self._saved_notes[identity] = body
            self._note_changed()
            self.status.setText("Note saved on this machine. Notes are excluded from Learning Pack transfers.")
        expected = self._saved_notes.get(identity, "")
        self._submit("Saving private note", lambda: self.library.save_note(identity, body, expected=expected), done, mutating=True)

    def toggle_bookmark(self, *_):
        if self._current is None:
            return
        key = self._current.topic.topic_id, self._current.topic.revision
        enable = key not in self._bookmarks
        def work():
            self.library.bookmark(*key, enabled=enable)
            return self.library.bookmarks()
        def done(bookmarks):
            self._bookmarks = bookmarks
            self.bookmark_button.setText("Remove Bookmark" if enable else "Bookmark Topic")
            self.status.setText("Bookmark saved." if enable else "Bookmark removed.")
        self._submit("Saving bookmark", work, done, mutating=True)

    def reload_note(self, *_):
        identity = getattr(self, "_note_identity", None)
        if identity is None:
            return
        draft = self.note.toPlainText()
        def ready(saved):
            def apply():
                self._saved_notes[identity] = saved
                self._drafts.pop(identity, None)
                self._set_note(identity, saved)
                self.status.setText("Saved note reloaded. The discarded draft was not written.")
            self._confirm("Review Saved Note", f"Topic: {identity}\n\nSaved note:\n{saved}\n\nYour unsaved draft:\n{draft}\n\nUse Saved Note discards this topic's current draft. Cancel keeps it.", "Use Saved Note", apply)
        self._submit("Reading saved note for comparison", lambda: self.library.note(identity), ready)

    def _confirm(self, title, body, action, confirmed):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setModal(True)
        dialog.resize(620, 500)
        layout = QVBoxLayout(dialog)
        label = QLabel(title)
        label.setTextFormat(Qt.PlainText)
        label.setObjectName("pageTitle")
        layout.addWidget(label)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        detail.setPlainText(body)
        detail.setAccessibleName("Learning Pack review")
        layout.addWidget(detail)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        accept = buttons.addButton(action, QDialogButtonBox.AcceptRole)
        accept.setAutoDefault(False)
        buttons.rejected.connect(dialog.close)
        def apply():
            dialog.close()
            confirmed()
        accept.clicked.connect(apply)
        layout.addWidget(buttons)
        self._review = dialog
        dialog.show()

    def show_packs(self, *_):
        if self._packs is not None and isValid(self._packs):
            self._packs.show()
            self._packs.raise_()
            return
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle("Learning Packs")
        dialog.resize(620, 280)
        layout = QVBoxLayout(dialog)
        intro = QLabel("Transfer instructional content as files. Imports cannot replace shipped topics. Your notes and saved reading stay here.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        choose = QPushButton("Choose Pack to Review…")
        choose.setAutoDefault(False)
        choose.clicked.connect(self.choose_import)
        layout.addWidget(choose)
        self.pack_cohort = QComboBox()
        self.pack_cohort.setAccessibleName("Installed topic cohort")
        for cohort in self._cohorts:
            self.pack_cohort.addItem(f"{cohort.cohort_id} · revision {cohort.revision}", cohort.cohort_id)
        layout.addWidget(self.pack_cohort)
        row = QHBoxLayout()
        export = QPushButton("Export Chosen Cohort…")
        recover = QPushButton("Review Previous Revision…")
        export.setAutoDefault(False)
        recover.setAutoDefault(False)
        export.clicked.connect(self.choose_export)
        recover.clicked.connect(self.review_recovery)
        row.addWidget(export)
        row.addWidget(recover)
        layout.addLayout(row)
        self.pack_status = QPlainTextEdit("Choose a pack to review, or choose an installed cohort for transfer or recovery.")
        self.pack_status.setReadOnly(True)
        self.pack_status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.pack_status.setAccessibleName("Learning Pack result and recovery path")
        self.pack_status.setMaximumHeight(100)
        layout.addWidget(self.pack_status)
        reload = QPushButton("Reload Installed Topics")
        reload.setAutoDefault(False)
        reload.clicked.connect(self._reload)
        layout.addWidget(reload)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        self._packs = dialog
        dialog.show()

    def choose_import(self, *_):
        path, _ = QFileDialog.getOpenFileName(self, "Choose Learning Pack", "", "Learning Packs (*.zip)")
        if path:
            self._submit("Checking chosen Learning Pack", lambda: self.library.preview_import(read_pack_file(Path(path))), self._import_preview)

    def _import_preview(self, preview):
        cohort = preview.cohort
        effect = ("This exact content is already installed. Confirmation makes no content changes. "
                  if preview.already_active else "Import activates this cohort; prior revisions stay available for scoped recovery. ")
        summaries = "\n".join(f"{a.topic.title} | {a.topic.topic_id} / {a.topic.revision}\n  Source: {a.topic.source}\n  Author/reviewer: {a.topic.author} / {a.topic.reviewer}\n  Declared: {_review_label(a.review_state)}; applicability: {_applicability_text(a.topic)}" for a in cohort.articles[:100])
        body = (f"Cohort: {cohort.cohort_id}\nRevision: {cohort.revision}\nAs of: {_date_text(cohort.as_of)}\nLicense: {cohort.license}\n"
            f"Topics: {len(cohort.articles)}\nPayload SHA256: {preview.digest}\n\n"
            "Integrity checks passed. Publisher identity and review claims are unverified. " + effect + "Shipped content and your notes are preserved.\n\n"
            + summaries + ("\nOnly the first 100 topic summaries are shown." if len(cohort.articles) > 100 else ""))
        def apply():
            self._stash_note()
            old_ids = {a.topic.topic_id for c in self._cohorts if c.cohort_id == cohort.cohort_id for a in c.articles}
            removed = old_ids - {a.topic.topic_id for a in cohort.articles}
            if removed.intersection(self._drafts):
                self.status.setText("Save the pending notes for topics this update removes before importing. Your draft and installed content are unchanged.")
                self.raise_()
                self.activateWindow()
                return
            def work():
                changed = self.library.import_reviewed(preview)
                return changed, self._snapshot()
            def done(value):
                changed, snapshot = value
                if changed:
                    self._refresh_notice = "Learning Pack imported; review claims remain unverified."
                    self._snapshot_ready(snapshot)
                else:
                    self.status.setText("This exact pack is already active; no content changed.")
                self._refresh_packs()
            self._submit("Importing reviewed Learning Pack", work, done, mutating=True)
        self._confirm("Review Learning Pack Import", body, "Import Reviewed Pack", apply)

    def _chosen_cohort(self):
        identity = self.pack_cohort.currentData()
        return next((c for c in self._cohorts if c.cohort_id == identity), None)

    def choose_export(self, *_):
        cohort = self._chosen_cohort()
        if cohort is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Learning Pack", "", "Learning Packs (*.zip)", options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        destination = Path(path)
        def prepare():
            raw = export_pack(cohort)
            old = read_pack_file(destination) if destination.exists() else None
            return raw, sha256(old).hexdigest() if old is not None else None
        def ready(value):
            raw, expected = value
            body = (f"Destination: {destination}\nCohort: {cohort.cohort_id} / {cohort.revision}\n"
                + (f"Replace existing file with SHA256 {expected}. Its prior bytes will be retained in a uniquely named .previous file beside it.\n" if expected else "Create a new file. A newly appearing destination will stop this export.\n")
                + "Content only: no notes, bookmarks or reading history. Recheck the destination before confirming.")
            def apply():
                def done(backup):
                    self.status.setText(f"Learning Pack exported and read-back verified. Prior file retained: {backup}" if backup else "Learning Pack exported and read-back verified.")
                    self._pack_status()
                self._submit("Exporting reviewed Learning Pack", lambda: write_pack_file(destination, raw, expected_digest=expected), done, mutating=True)
            self._confirm("Review Learning Pack Export", body, "Export Reviewed File", apply)
        self._submit("Reviewing export destination", prepare, ready)

    def review_recovery(self, *_):
        cohort = self._chosen_cohort()
        if cohort is None:
            return
        self._stash_note()
        if {a.topic.topic_id for a in cohort.articles}.intersection(self._drafts):
            self.status.setText("Save this cohort's pending notes before restoring another revision. Your draft and current activation are unchanged.")
            self._packs.hide()
            self.raise_()
            self.activateWindow()
            return
        if not hasattr(self.library, "preview_rollback"):
            self.status.setText("Exact prior-revision review is unavailable. Keep the current content; no recovery was started.")
            return
        def ready(preview):
            # The service owns this exact immutable review; no private database reads.
            old = preview.previous_revision
            effect = f"Restore revision {old}." if old is not None else "Deactivate this first import; retained revision bytes remain stored."
            body = (f"Cohort: {cohort.cohort_id}\nCurrent revision: {preview.current_revision}\n{effect}\n"
                    "Only this cohort activation changes. Notes, bookmarks and reading history are retained. Conflicting current content or stale state stops recovery.")
            def apply():
                def work():
                    self.library.rollback_import(cohort.cohort_id, expected_generation=preview.generation)
                    return self._snapshot()
                def done(snapshot):
                    self._refresh_notice = "Chosen cohort activation restored. Notes and saved reading preserved."
                    self._snapshot_ready(snapshot)
                    self._refresh_packs()
                self._submit("Restoring reviewed cohort revision", work, done, mutating=True)
            self._confirm("Review Learning Pack Recovery", body, "Restore Reviewed Activation", apply)
        self._submit("Reviewing prior cohort revision", lambda: self.library.preview_rollback(cohort.cohort_id), ready)

    def _refresh_packs(self):
        if self._packs is not None and isValid(self._packs):
            self._packs.close()
            self._packs = None

    def _pack_status(self):
        if self._packs is not None and isValid(self._packs):
            self.pack_status.setPlainText(self.status.text())
            self._packs.show()

    def cancel_search(self):
        if self._task is not None and not self._mutating:
            self._cancel.set()
            self._task.cancel()
            self.status.setText("Stopping after the current read finishes…")

    def closeEvent(self, event):
        self._stash_note()
        dirty = any(body != self._saved_notes.get(identity, "") for identity, body in self._drafts.items())
        if dirty and not self._closing:
            message = "Unsaved notes are kept only in this window. Return to each topic and save, or discard these drafts before closing."
            if self._mutating:
                message += " A save already in progress still finishes. If it fails, this window stays open with your draft."
            answer = QMessageBox.warning(self, "Unsaved learning notes", message, QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Discard:
                event.ignore()
                return
        self._closing = True
        if self._reader is not None and isValid(self._reader):
            self._reader.close()
        if self._task is not None:
            if not self._mutating:
                self._cancel.set()
                self._task.cancel()
            self.status.setText("Closing when the current operation finishes. Your saved content is retained.")
            self._set_busy(True)
            self._update_activity()
            self._poll.start()
            event.ignore()
            return
        self._poll.stop()
        self._lifetime.set()
        event.accept()
