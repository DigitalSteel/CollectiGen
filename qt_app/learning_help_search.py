"""Opt-in local Help search; the shared Help entry is integrated separately."""
from __future__ import annotations

from datetime import date
from threading import Event

from PySide6.QtCore import QElapsedTimer, QEvent, QThreadPool, QTimer, Qt, Slot
from PySide6.QtGui import QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout,
)
from shiboken6 import isValid

from core.learning_help_catalog import help_guide_topics, search_help_guides
from .components import ContextHelpDialog, DisclosureButton, IndeterminateActivityBar, compact_button
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
        self.setProperty("collectigenLearningSearchOwner", True)
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
        self._elapsed = QElapsedTimer()
        self._active_scope = ""
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
        self.activity_scope = QLabel()
        self.activity_scope.setTextFormat(Qt.PlainText)
        self.activity_scope.setWordWrap(True)
        self.activity_scope.setAccessibleName("Local Help search phase and elapsed time")
        self.activity = IndeterminateActivityBar(self)
        layout.addWidget(self.activity_scope)
        layout.addWidget(self.activity)
        self.activity_scope.hide()
        self.activity.hide()
        self.results = QListWidget()
        self.results.setAccessibleName("Matching Help guides")
        layout.addWidget(self.results, 2)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Selected Help excerpt")
        self.preview.setPlaceholderText("Select a guide to read its matching excerpt.")
        layout.addWidget(self.preview, 1)
        self.source = QLabel("Select a guide to inspect its source and review details.")
        self.source.setTextFormat(Qt.PlainText)
        self.source.setWordWrap(True)
        self.source.setObjectName("mutedLabel")
        layout.addWidget(self.source)
        self.guide_details = QPlainTextEdit()
        self.guide_details.setReadOnly(True)
        self.guide_details.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.guide_details.setAccessibleName("Selected guide provenance")
        self.guide_details.setMaximumHeight(120)
        self.details_button = DisclosureButton("Guide Details", "Hide Guide Details", self.guide_details)
        self.details_button.setAutoDefault(False)
        self.details_button.setEnabled(False)
        layout.addWidget(self.details_button)
        layout.addWidget(self.guide_details)
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
        self.source.setText("Select a guide to inspect its source and review details.")
        self.source.setToolTip("")
        self.details_button.setChecked(False)
        self.details_button.setEnabled(False)
        self.guide_details.clear()

    def _cancel_task(self):
        if self._cancel is not None:
            self._cancel.set()
        if self._task is not None:
            self._task.cancel()

    def _invalidate(self, *_):
        self._generation += 1
        self._pending = None
        self._cancel_task()
        self._update_activity()
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
        self._pending = (self._generation, query, self.scope.currentData(), self.scope.currentText())
        self.status.setText("You can change the words or choose Cancel Search.")
        self.cancel_button.setEnabled(True)
        self._poll.start()
        self._settle_task()

    def _settle_task(self):
        self._update_activity()
        if self._task is not None:
            if not self._task.is_finished():
                return
            if self._task.state == "failed" and self._task_generation == self._generation and not self._closing:
                self.status.setText("Help search is unavailable. Try again or keep reading the original guide.")
            self._task = None
            self._cancel = None
            self._update_activity()
            self.cancel_button.setEnabled(False)
        if self._closing:
            self.close()
            return
        if self._pending is None:
            self._poll.stop()
            return
        generation, query, guide_id, scope_name = self._pending
        self._pending = None
        topics, cancelled, lifetime = self._topics, Event(), self._lifetime
        self._cancel = cancelled
        self._active_scope = scope_name
        self._elapsed.start()
        def search():
            matches = search_help_guides(topics, query, guide_id=guide_id,
                should_continue=lambda: not cancelled.is_set() and not lifetime.is_set())
            return generation, matches
        self._task_generation = generation
        self._task = AsyncCallTask(search)
        self._task.signals.complete.connect(self._deliver)
        self.cancel_button.setEnabled(True)
        self._update_activity()
        start_qt_delivery_task(QThreadPool.globalInstance(), self._task)

    def _update_activity(self):
        busy = self._task is not None
        if not busy:
            self.activity.set_active(False)
            self.activity.hide()
            self.activity_scope.hide()
            return
        phase = "Searching local Help"
        if self._closing:
            phase = "Closing after local Help search stops"
        elif self._cancel is not None and self._cancel.is_set():
            phase = ("Stopping the previous local Help search" if self._pending is not None
                     else "Stopping local Help search")
        seconds = max(0, self._elapsed.elapsed() // 1000) if self._elapsed.isValid() else 0
        detail = f"{phase} • {self._active_scope} • Elapsed {seconds}s • Total not yet known"
        self.activity_scope.setText(detail)
        self.activity_scope.show()
        self.activity.set_active(True, detail)
        self.activity.show()

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
            self.details_button.setChecked(False)
            self.details_button.setEnabled(False)
            self.guide_details.clear()
            self.source.setText("Select a guide to inspect its source and review details.")
            self.source.setToolTip("")
            return
        match = item.data(Qt.UserRole)
        excerpt = QTextDocument()
        excerpt.setMarkdown(match.excerpt)
        self.preview.setPlainText(excerpt.toPlainText())
        topic = match.topic
        review_date = "not recorded" if topic.as_of == date.min else topic.as_of.isoformat()
        self.source.setText(f"Guide: {topic.title}\nAs-of date: {review_date}. Search does not verify freshness.")
        self.source.setToolTip(topic.source)
        applicability = "\n".join(f"  {tool}: {version}" for tool, version in topic.applicability)
        self.guide_details.setPlainText(
            f"Source: {topic.source}\nRevision: {topic.revision}\n"
            f"Author: {topic.author}\nReviewer: {topic.reviewer}\n"
            f"License: {topic.license}\nAs-of date: {review_date}\n"
            f"Declared applicability:\n{applicability or '  Not recorded'}\n"
            "These details do not verify freshness or compatibility with your installed tools.")
        self.details_button.setEnabled(True)

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
        if self._reader is not None and isValid(self._reader) and not self._reader.close():
            event.ignore()
            return
        self._closing = True
        self._pending = None
        self._cancel_task()
        self._update_activity()
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


LEARNING_CONTEXT_TOPICS = {
    'image-tag-removal': 'learning:image-tag-removal',
    'image-tag-batch': 'learning:image-tag-batch',
    'tag-details': 'learning:tag-details',
    'completed-pack-restore': 'learning:completed-pack-restore',
    'organizer-prompt-authoring': 'learning:organizer-prompt-authoring',
    "video-resource-runtime": "learning:video-resources-and-packs",
    "video-resource-preparation": "learning:video-resources-and-packs",
    "video-finishing": "learning:video-finishing-workflow",
    "video-generation-workspace": "learning:video-generation-workflow",
    "video-review-transfer": "learning:video-review-and-transfer",
    "video-review-preview": "learning:video-review-and-transfer",
    "training-lora-registration": "learning:training-result-registration",
    "training-experiments": "learning:training-experiment-history",
    "training-run-review": "learning:training-run-and-recovery",
    "training-execution-preparation": "learning:training-local-setup",
    "training-saved-resources": "learning:training-local-setup",
    "training-software-setup": "learning:training-local-setup",
    "training-txt-export-plan": "learning:training-txt-export",
    "training-txt-destination": "learning:training-txt-export",
    "training-package-recovery-finder": "learning:training-manual-package",
    "training-package-comparison": "learning:training-manual-package",
    "training-package-result": "learning:training-manual-package",
    "training-package-plan": "learning:training-manual-package",
    "training-package-destination": "learning:training-manual-package",
    "training-adapter-review": "learning:training-goal-plan",
    "training-configuration": "learning:training-goal-plan",
    "training-candidate-planning": "learning:training-goal-plan",
    "training-goal-planning": "learning:training-goal-plan",
    "swarm-preset-import": "learning:swarm-preset-import",
    "swarm-existing-setup": "learning:swarm-existing-setup",
    "swarm-model-folders": "learning:swarm-model-folders",
    "swarm-model-metadata": "learning:swarm-model-metadata",
    "swarm-configuration-backup": "learning:swarm-configuration-backup",
    "swarm-companion-package": "learning:swarm-companion-package",
    "upscaler-models": "learning:upscaler-models",
    "resolution-workflow": "learning:resolution-workflow",
    "app-update-review": "learning:app-update-review",
    "guided-setup": "learning:guided-setup",
    "new-vocabulary-decisions": "learning:new-vocabulary-decisions",
    "tag-family-policy": "learning:tag-family-policy",
    "tag-family-organization": "learning:tag-family-organization",
    "tag-family-expansion": "learning:tag-family-expansion",
    "tag-family-exclusion": "learning:tag-family-builder",
    "tag-family-discovery": "learning:tag-family-builder",
    "tag-family-builder": "learning:tag-family-builder",
    "tag-reference-selection": "learning:tag-reference-selection",
    "image-tag-detail-review": "learning:image-tag-detail-review",
    "retired-image-tag-review": "learning:retired-image-tag-review",
    "removed-vocabulary": "learning:removed-vocabulary",
    "broader-tag-findings": "learning:broader-tag-findings",
    "tag-presentation-workspace": "learning:tag-presentation-workspace",
    "tag-alias-preflight": "learning:tag-alias-preflight",
    "knowledge-pack-import-review": "learning:knowledge-pack-import-review",
    "character-record-editor": "learning:character-record-editor",
    "tag-identity-merge": "learning:tag-identity-merge",
    "add-images-recovery": "learning:add-images-recovery",
    "preparation-results": "learning:preparation-results",
    "run-ai-review": "learning:run-ai-review",
    "relationship-migration-review": "learning:relationship-migration-review",
    "modifier-role-decision": "learning:modifier-role-decision",
    'relationship-workbench': 'learning:relationship-workbench',
    'relationship-batch-preview': 'learning:relationship-batch-preview',

    "recycle-recovery": "learning:recycle-recovery",
    "preparation-impact": "learning:preparation-impact",
    "model-relocation": "learning:model-relocation",
    "training-saved-history": "learning:training-saved-history",
    "training-retained-review": "learning:training-retained-review",
    "training-anima": "learning:training-anima",
    "training-validation": "learning:training-validation",
    "training-comparison": "learning:training-comparison",
    "image-knowledge-history": "learning:image-knowledge-history",
    "saved-history-workspace": "learning:saved-history-workspace",
    "image-change-goal": "learning:image-change-goal",
    "image-context": "learning:image-context",
    "image-record-recovery": "learning:image-record-recovery",
    "add-category-tags": "learning:add-category-tags",
    "modifier-batch-preview": "learning:modifier-batch-preview",
    "modifier-compatibility-review": "learning:modifier-compatibility-review",
    "modifier-value-scope": "learning:modifier-value-scope",
    "tag-category-change": "learning:tag-category-change",
    "safe-fix-review": "learning:safe-fix-review",
    "future-defaults-review": "learning:future-defaults-review",
    "knowledge-pack-export-review": "learning:knowledge-pack-export-review",
    "model-artwork": "learning:model-artwork",
    "model-cache": "learning:model-cache",
    "training-checkpoint-export": "learning:training-checkpoint-export",
    "character-ownership": "learning:character-ownership",
    "guided-tag-review": "learning:guided-tag-review",
    "generation-setup-source": "learning:generation-setup-source",
    "saved-generation-work": "learning:saved-generation-work",
    "category-details": "learning:category-details",
    "character-source": "learning:character-source",
    "category-member-reset": "learning:category-member-reset",
    "category-creation": "learning:category-creation",
    "exact-model-copy": "learning:exact-model-copy",
    "known-model-folders": "learning:known-model-folders",
    "model-file-review": "learning:model-file-review",
    "model-selection": "learning:model-selection",
    "model-import-review": "learning:model-import-review",
    "model-import-choices": "learning:model-import-choices",
    "model-companion-resources": "learning:model-companion-resources",
    "model-workflow-guidance": "learning:model-workflow-guidance",
    "model-usage": "learning:model-usage",
    "ingest-import-review": "learning:ingest-import-review",
    "source-write-review": "learning:source-write-review",
    "automation-value-review": "learning:automation-value-review",
    "training-plan-review": "learning:training-plan-review",
    "generation-setup-export": "learning:generation-setup-export",
    "generation-setup-review": "learning:generation-setup-review",
    "generation-mask": "learning:generation-mask",
    "image-finishing": "learning:image-finishing",
    "generation-alternatives": "learning:generation-alternatives",
    "generation-request": "learning:generation-request",
    "folder-locations": "learning:folder-locations",
    "external-evidence-import": "learning:external-evidence-import",
    "training-profile-manager": "learning:training-profile-manager",
    "gallery-duplicate-options": "learning:gallery-duplicate-options",
    "duplicate-preflight": "learning:duplicate-preflight",
    "duplicate-review": "learning:duplicate-review",
    "distinct-history": "learning:distinct-history",
    "related-updates": "learning:related-updates",
    "wd-setup-choice": "learning:wd-setup-choice",
    "default-overrides": "learning:default-overrides",
    "projection-batch": "learning:projection-batch",
    "training-txt-profile": "learning:training-txt-profile",
    "image-tag-maintenance": "learning:image-tag-maintenance",
    "remembered-exceptions": "learning:remembered-exceptions",
    "conflict-authoring": "learning:conflict-authoring",
    "conflict-review": "learning:conflict-review",
    "training-collections": "learning:training-collections",
    "command-search": "learning:command-search",
    "character-tag-links": "learning:character-tag-links",
    "category-base-discovery": "learning:category-base-discovery",
    "component-management": "learning:component-management",
    "feedback-archive": "learning:feedback-archive",
    "feedback-report": "learning:feedback-report",
    "automation-projection-repair": "learning:automation-projection-repair",
    "automation-rule-intent": "learning:automation-rule-intent",
    "automation-legacy-values": "learning:automation-legacy-values",
    "automation-rule-authoring": "learning:automation-rule-authoring",
    "generation-setup-from-owner": "learning:generation-setup-from-owner",
    "generation-setup-from-image": "learning:generation-setup-from-image",
    "generation-setup-authoring": "learning:generation-setup-authoring",
    "caption-quality-review": "learning:caption-quality-review",
    "ai-baseline-review": "learning:ai-baseline-review",
    "library-scope": "learning:library-scope",
    "library-profile": "learning:library-profile",
    "description-ignores": "learning:description-ignores",
    "preferred-descriptions": "learning:preferred-descriptions",
    "dashboard": "learning:foundations", "setup": "learning:model-workflow",
    "ingest": "learning:image-workflow", "dataset": "learning:datasets",
    "training": "learning:training", "generate": "learning:generation",
    "knowledge": "learning:sdxl-tags", "review": "learning:sdxl-tags",
    "automation": "learning:safety", "models": "learning:model-compatibility",
    "review-media": "learning:image-workflow", "generation-regions": "learning:generation",
    "related-tags": "learning:related-tags", "image-data": "learning:image-data-editor",
    "library-move": "learning:library-move", "ai-descriptions": "learning:ai-description-settings",
    "ai-tag-models": "learning:ai-tag-models",
    "problems": "learning:problems-recovery",
    "fast-storage": "learning:fast-storage",
    "components": "learning:components",
    "prompt-presets": "learning:prompt-presets",
}


def close_learning_libraries(owner):
    """Honor marked instructional notes and search work before owner completion."""
    for dialog in owner.findChildren(QDialog):
        if isValid(dialog) and (dialog.property("collectigenLearningLibraryOwner")
                                or dialog.property("collectigenLearningSearchOwner")):
            if not dialog.close():
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                return False
    return True




def _new_learning_library_dialog(parent, context, *, topic_id=None):
    from .learning_library_dialog import LearningLibraryDialog
    return LearningLibraryDialog(parent, context=context,
        topic_id=topic_id if topic_id is not None else LEARNING_CONTEXT_TOPICS.get(context))


def show_learning_library(owner, *, context=None, topic_id=None):
    """Reuse one Library without replacing an active reader, query or draft."""
    if context is not None and type(context) is not str:
        raise ValueError("Help context must be an exact string.")
    if topic_id is not None and (type(topic_id) is not str or not topic_id.strip()):
        raise ValueError("Help topic must be an exact non-empty string.")
    if context is not None and topic_id is not None:
        raise ValueError("Choose a Help context or a topic, not both.")
    from .learning_library_dialog import LearningLibraryDialog
    parent = owner.parentWidget()
    if isinstance(parent, LearningLibraryDialog) and not parent._closing:
        owner.close()
        parent.raise_()
        parent.activateWindow()
        return parent
    current = getattr(owner, "_learning_library_dialog", None)
    if current is not None and isValid(current) and current.isVisible():
        current.raise_()
        current.activateWindow()
        return current
    current = (_new_learning_library_dialog(owner, context) if topic_id is None
               else _new_learning_library_dialog(owner, None, topic_id=topic_id))
    owner._learning_library_dialog = current
    current.show()
    return current


def add_help_search_button(dialog, buttons, *, help_text=None, learning_context=None):
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
            (parent.results if parent.results.currentItem() is not None else parent.query).setFocus()
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
    library_button = buttons.addButton("Learning Library…", QDialogButtonBox.ActionRole)
    library_button.setAutoDefault(False)
    library_button.setAccessibleName("Open offline Learning Library")
    contexts = [key for guides in (WORKSPACE_HELP, SECTION_HELP)
                for key, text in guides.items() if text == help_text]
    if learning_context is not None and type(learning_context) is not str:
        raise ValueError("Help context must be an exact string.")
    # Explicit identity wins even when unknown; never infer a different context
    # from the guide's wording after a producer supplied an identity.
    context = learning_context if learning_context is not None else (contexts[0] if len(contexts) == 1 else None)
    def show_library():
        show_learning_library(dialog, context=context)
    library_button.clicked.connect(show_library)
    return button
