"""Manage reusable, explicitly selected image collections for training plans."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .modal_feedback import FeedbackEnabledDialog
from .components import EmptyStatePanel,compact_button
from .workers import CoordinatedCallTask


class ConceptGroupDialog(FeedbackEnabledDialog):
    """Edit collection membership without changing source files or image facts."""

    feedback_workflow = "Training collections"
    training_requested = Signal(object)
    image_requested = Signal(object)
    background_work_finished = Signal()

    GROUP_PAGE_SIZE = 50
    MEMBER_PAGE_SIZE = 100

    def __init__(
        self,
        jobs,
        store,
        dataset,
        *,
        selected_paths=(),
        scope_current=None,
        dataset_current=None,
        history_opener=None,
        selected_group_id="",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.store = store
        self.dataset = dataset
        self.selected_paths = tuple(
            dict.fromkeys(Path(value) for value in selected_paths)
        )
        self.scope_current = scope_current
        self.dataset_current = dataset_current
        self.history_opener = history_opener
        self._history_dialog = None
        self._history_results = None
        self._payload = {"schema_version": 1, "groups": {}}
        self._revision = ""
        self._group_id = str(selected_group_id)
        self._group_page = 0
        self._member_page = 0
        self._new_group = False
        self._create_return_id = ""
        self._snapshot_error = ""
        self._pending = None
        self._generation = 0
        self._closed = False
        self._training_handoff_active = False
        self._create_then_add = False
        self._add_after_reload_id = ""
        self._shutdown_requested = False
        self._details_dirty = False
        self._rules_dirty = False
        self._rules_loading = False
        self._rules_verified = True
        self._rules_failed = False
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self._rules_verified_dataset = None
        self._skip_rule_refresh_once = False
        self._rule_after_reload = None
        self._rule_published_fingerprint = ""
        self._ignored_page = 0
        self._rule_preview_page = 0

        self.setWindowTitle("Training Collections")
        self.resize(1040, 760)
        root = QVBoxLayout(self)
        heading = QLabel("Training Collections")
        heading.setObjectName("sectionTitle")
        root.addWidget(heading)
        note = QLabel(
            "Group Dataset images for training. Collections may overlap; "
            "membership leaves your images and Image Knowledge unchanged."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        self.start_actions=QWidget()
        actions=QHBoxLayout(self.start_actions)
        actions.setContentsMargins(0,0,0,0)
        self.new_button=compact_button(QPushButton("Create Collection…"),primary=True)
        self.new_button.setAutoDefault(False)
        self.new_button.clicked.connect(self.begin_create)
        self.reload_button=compact_button(QPushButton("Reload"))
        self.reload_button.setAutoDefault(False)
        self.reload_button.clicked.connect(self.reload)
        self.reload_button.setToolTip("Reload saved collections and discard unsaved collection details.")
        self.recover_button=compact_button(QPushButton("Recover Saved Collections"))
        self.recover_button.clicked.connect(self.recover)
        self.recover_button.hide()
        actions.addWidget(self.new_button)
        actions.addWidget(self.reload_button)
        actions.addWidget(self.recover_button)
        self.history_button=compact_button(QPushButton("History & Versions…"))
        self.history_button.clicked.connect(self.open_history)
        actions.addWidget(self.history_button)
        self.observe_history_button=compact_button(QPushButton("Record Current State…"))
        self.observe_history_button.setToolTip("Review the saved Collection after a history gap. Unsaved editor choices are kept separately.")
        self.observe_history_button.clicked.connect(lambda: self.open_history(observation_only=True))
        actions.addWidget(self.observe_history_button)
        actions.addStretch(1)
        root.addWidget(self.start_actions)

        splitter = QSplitter(Qt.Horizontal)
        self.browser=self._build_browser()
        splitter.addWidget(self.browser)
        self.editor_stack=QStackedWidget()
        empty=QWidget(); empty_layout=QVBoxLayout(empty)
        empty_layout.setContentsMargins(0,0,0,0)
        self.empty_panel=EmptyStatePanel(
            "Choose Create Collection above to name your first image collection.",
            "No Training Collections",
        )
        self.empty_panel.setMaximumHeight(240)
        empty_layout.addWidget(self.empty_panel)
        empty_layout.addStretch(1)
        self.editor_stack.addWidget(empty)
        self.editor=self._build_editor()
        self.editor_stack.addWidget(self.editor)
        splitter.addWidget(self.editor_stack)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        self.status = QLabel("Loading saved training collections…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        root.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.prepare_button = QPushButton("Build Training Data…")
        self.prepare_button.setProperty("primary", True)
        self.prepare_button.setToolTip(
            "Open read-only review and configuration for this collection's full "
            "saved image set, training goal, model, output, and compatible "
            "trainer. This does not write a package or start training."
        )
        self.prepare_button.clicked.connect(self._prepare_training)
        buttons.rejected.connect(self.reject)
        self.prepare_button.setAutoDefault(False)
        compact_button(self.prepare_button)
        self.finish_actions=QWidget()
        finish=QHBoxLayout(self.finish_actions)
        finish.setContentsMargins(0,0,0,0)
        finish.addWidget(buttons)
        finish.addStretch(1)
        finish.addWidget(self.prepare_button)
        root.addWidget(self.finish_actions)

        self._render()
        for button in self.findChildren(QPushButton):
            compact_button(button)
            button.setAutoDefault(False)
        for button in (self.new_button,self.prepare_button):
            button.setProperty("compact",False)
            button.setMinimumHeight(38)
        self.reload()

    def _build_browser(self) -> QWidget:
        panel = QGroupBox("Saved collections")
        layout = QVBoxLayout(panel)
        self.groups = QListWidget()
        self.groups.setSelectionMode(QAbstractItemView.SingleSelection)
        self.groups.currentItemChanged.connect(self._group_selected)
        layout.addWidget(self.groups, 1)
        paging = QHBoxLayout()
        self.group_previous = QPushButton("Previous")
        self.group_next = QPushButton("Next")
        self.group_page_label = QLabel()
        self.group_previous.clicked.connect(self._previous_group_page)
        self.group_next.clicked.connect(self._next_group_page)
        paging.addWidget(self.group_previous)
        paging.addWidget(self.group_page_label, 1, Qt.AlignCenter)
        paging.addWidget(self.group_next)
        layout.addLayout(paging)
        return panel

    def _build_editor(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0,0,0,0)
        layout.setAlignment(Qt.AlignTop)
        details = QGroupBox("Collection details")
        details.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        form = QFormLayout(details)
        self.name = QLineEdit()
        self.name.setMaxLength(120)
        self.name.setPlaceholderText("Required collection name")
        self.description = QPlainTextEdit()
        self.description.setFixedHeight(64)
        self.description.setPlaceholderText("Optional collection purpose")
        self.name.textChanged.connect(self._details_changed)
        self.description.textChanged.connect(self._details_changed)
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        layout.addWidget(details)

        edit_actions = QHBoxLayout()
        self.save_button = compact_button(QPushButton("Save Changes"),primary=True)
        self.create_add_button = compact_button(QPushButton(),primary=True)
        self.cancel_details_button=compact_button(QPushButton("Cancel Changes"))
        self.cancel_details_button.clicked.connect(self._discard_details)
        self.save_button.setAutoDefault(False)
        self.create_add_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save)
        self.create_add_button.clicked.connect(self.create_and_add_selected)
        edit_actions.addStretch(1)
        edit_actions.addWidget(self.cancel_details_button)
        edit_actions.addWidget(self.save_button)
        edit_actions.addWidget(self.create_add_button)
        form.addRow(edit_actions)
        self.selection_context = QLabel()
        self.selection_context.setWordWrap(True)
        self.selection_context.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.selection_context.setObjectName("mutedLabel")
        self.selection_context.setAccessibleName("Images selected in Dataset")
        layout.addWidget(self.selection_context, 0, Qt.AlignTop)

        members_box = QGroupBox("Exact members")
        members_layout = QVBoxLayout(members_box)
        self.concept_summary = QLabel()
        self.concept_summary.setWordWrap(True)
        self.concept_summary.setAccessibleName("Chosen collection training scope")
        members_layout.addWidget(self.concept_summary)
        self.members = QTableWidget(0, 4)
        self.members.setHorizontalHeaderLabels(
            ("Image", "Library", "Added by", "SHA-256")
        )
        self.members.horizontalHeader().setStretchLastSection(False)
        self.members.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.members.setColumnWidth(1,180)
        self.members.setColumnWidth(2,130)
        self.members.setColumnWidth(3,200)
        self.members.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.members.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.members.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.members.itemSelectionChanged.connect(self._update_actions)
        self.members.itemDoubleClicked.connect(self._included_image_requested)
        members_layout.addWidget(self.members, 1)
        self.members_empty=EmptyStatePanel(
            "Add images selected in Dataset, or choose Automatic Membership.",
            "No Images in This Collection",minimum_height=120,
        )
        members_layout.addWidget(self.members_empty,1)
        self.show_identity_details = QCheckBox("Show proof hashes")
        self.show_identity_details.toggled.connect(
            lambda checked: self.members.setColumnHidden(3, not checked)
        )
        self.members.setColumnHidden(3, True)
        members_layout.addWidget(self.show_identity_details)
        member_paging = QHBoxLayout()
        self.member_previous = QPushButton("Previous")
        self.member_next = QPushButton("Next")
        self.member_page_label = QLabel()
        self.member_previous.clicked.connect(self._previous_member_page)
        self.member_next.clicked.connect(self._next_member_page)
        member_paging.addWidget(self.member_previous)
        member_paging.addWidget(self.member_page_label, 1, Qt.AlignCenter)
        member_paging.addWidget(self.member_next)
        members_layout.addLayout(member_paging)
        member_actions = QHBoxLayout()
        self.member_selection_count=QLabel("Selected: 0")
        self.member_selection_count.setObjectName("mutedLabel")
        member_actions.addWidget(self.member_selection_count)
        self.add_button = QPushButton()
        self.remove_button = QPushButton()
        self.add_button.clicked.connect(self.add_selected_images)
        self.remove_button.clicked.connect(self.remove_selected_members)
        member_actions.addStretch(1)
        member_actions.addWidget(self.add_button)
        member_actions.addWidget(self.remove_button)
        members_layout.addLayout(member_actions)
        self.members_tabs = QTabWidget()
        self.members_tabs.addTab(members_box, "Included Images")
        self.members_tabs.addTab(self._build_rules_panel(), "Automatic Membership")
        self.members_tabs.addTab(self._build_ignored_panel(), "Ignored Images")
        layout.addWidget(self.members_tabs, 1)
        return panel

    def _build_rules_panel(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        explanation = QLabel(
            "Automatically include images from named libraries using confirmed tags "
            "and reviewed description words. Exclusions and ignored images always win."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        libraries_box = QGroupBox("Source libraries")
        libraries_layout = QVBoxLayout(libraries_box)
        self.rule_library_checks = {}
        profile_provider = getattr(self.dataset, "profile", None)
        for library_id in tuple(getattr(self.dataset, "scope_library_ids", ()) or ()):
            library_id = str(library_id)
            name = library_id
            if callable(profile_provider):
                try:
                    profile = profile_provider(library_id)
                    name = str(getattr(profile, "name", "") or library_id)
                except Exception:
                    pass
            check = QCheckBox(name)
            check.setToolTip(f"Library ID: {library_id}")
            check.toggled.connect(self._rules_changed)
            libraries_layout.addWidget(check)
            self.rule_library_checks[library_id] = check
        if not self.rule_library_checks:
            unavailable = QLabel("No named Dataset libraries are available in this scope.")
            unavailable.setWordWrap(True)
            libraries_layout.addWidget(unavailable)
        library_scroll = QScrollArea()
        library_scroll.setWidgetResizable(True)
        library_scroll.setMaximumHeight(130)
        library_scroll.setWidget(libraries_box)
        layout.addWidget(library_scroll)
        form = QFormLayout()
        self.rule_match_mode = QComboBox()
        self.rule_match_mode.addItem("Match all include terms", "all")
        self.rule_match_mode.addItem("Match any include term", "any")
        self.rule_include_tags = QLineEdit()
        self.rule_exclude_tags = QLineEdit()
        self.rule_include_words = QLineEdit()
        self.rule_exclude_words = QLineEdit()
        for field in (
            self.rule_include_tags,
            self.rule_exclude_tags,
            self.rule_include_words,
            self.rule_exclude_words,
        ):
            field.setMaxLength(4000)
            field.textChanged.connect(self._rules_changed)
        self.rule_match_mode.currentIndexChanged.connect(self._rules_changed)
        self.rule_include_tags.setPlaceholderText("Comma-separated confirmed tags")
        self.rule_exclude_tags.setPlaceholderText("Optional tags to exclude")
        self.rule_include_words.setPlaceholderText("Comma-separated reviewed description words or phrases")
        self.rule_exclude_words.setPlaceholderText("Optional description words or phrases to exclude")
        form.addRow("Positive matching", self.rule_match_mode)
        form.addRow("Include tags", self.rule_include_tags)
        form.addRow("Exclude tags", self.rule_exclude_tags)
        form.addRow("Include description", self.rule_include_words)
        form.addRow("Exclude description", self.rule_exclude_words)
        layout.addLayout(form)
        self.rule_status = QLabel("Choose a saved collection to configure automatic membership.")
        self.rule_status.setWordWrap(True)
        layout.addWidget(self.rule_status)
        self.rule_preview = QTableWidget(0, 3)
        self.rule_preview.setHorizontalHeaderLabels(("Matching image", "Library", "Result"))
        self.rule_preview.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.rule_preview.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rule_preview.setSelectionMode(QAbstractItemView.SingleSelection)
        self.rule_preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rule_preview.itemDoubleClicked.connect(self._preview_image_requested)
        self.rule_preview.setMinimumHeight(120)
        layout.addWidget(self.rule_preview, 1)
        preview_paging = QHBoxLayout()
        self.rule_preview_previous = QPushButton("Previous")
        self.rule_preview_next = QPushButton("Next")
        self.rule_preview_page_label = QLabel("No preview")
        self.rule_preview_previous.clicked.connect(self._previous_rule_preview_page)
        self.rule_preview_next.clicked.connect(self._next_rule_preview_page)
        preview_paging.addWidget(self.rule_preview_previous)
        preview_paging.addWidget(self.rule_preview_page_label, 1, Qt.AlignCenter)
        preview_paging.addWidget(self.rule_preview_next)
        layout.addLayout(preview_paging)
        actions = QHBoxLayout()
        self.preview_rules_button = QPushButton("Preview Matching Images")
        self.save_rules_button = QPushButton("Save Rules")
        self.discard_rules_button = QPushButton("Discard Rule Changes")
        self.disable_rules_button = QPushButton("Disable Automatic Membership")
        self.preview_rules_button.clicked.connect(self._preview_rules)
        self.save_rules_button.clicked.connect(self._save_rules)
        self.discard_rules_button.clicked.connect(self._discard_rule_changes)
        self.disable_rules_button.clicked.connect(self._disable_rules)
        actions.addWidget(self.preview_rules_button)
        actions.addWidget(self.discard_rules_button)
        actions.addWidget(self.disable_rules_button)
        actions.addStretch(1)
        actions.addWidget(self.save_rules_button)
        panel = QScrollArea()
        panel.setWidgetResizable(True)
        panel.setWidget(content)
        self.rules_scroll = panel
        wrapper=QWidget(); wrapper_layout=QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0,0,0,0)
        wrapper_layout.addWidget(panel,1)
        wrapper_layout.addLayout(actions)
        return wrapper

    def _build_ignored_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        explanation = QLabel(
            "Images removed from this collection stay ignored so automatic rules do "
            "not add them again. Restore explicitly after reviewing the exact image."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.ignored_members = QTableWidget(0, 3)
        self.ignored_members.setHorizontalHeaderLabels(
            ("Image", "Library", "Previously added by")
        )
        self.ignored_members.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ignored_members.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.ignored_members.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ignored_members.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.ignored_members.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ignored_members.itemSelectionChanged.connect(self._update_actions)
        self.ignored_members.itemDoubleClicked.connect(self._ignored_image_requested)
        layout.addWidget(self.ignored_members, 1)
        paging = QHBoxLayout()
        self.ignored_previous = QPushButton("Previous")
        self.ignored_next = QPushButton("Next")
        self.ignored_page_label = QLabel()
        self.ignored_previous.clicked.connect(self._previous_ignored_page)
        self.ignored_next.clicked.connect(self._next_ignored_page)
        paging.addWidget(self.ignored_previous)
        paging.addWidget(self.ignored_page_label, 1, Qt.AlignCenter)
        paging.addWidget(self.ignored_next)
        layout.addLayout(paging)
        ignored_actions = QHBoxLayout()
        self.ignored_selection_count=QLabel("Selected: 0")
        self.ignored_selection_count.setObjectName("mutedLabel")
        self.view_ignored_button = QPushButton("View Selected Image")
        self.restore_button = QPushButton("Restore to Collection")
        self.view_ignored_button.clicked.connect(self._view_selected_ignored)
        self.restore_button.clicked.connect(self.restore_selected_members)
        ignored_actions.addWidget(self.view_ignored_button)
        ignored_actions.addWidget(self.ignored_selection_count)
        ignored_actions.addStretch(1)
        ignored_actions.addWidget(self.restore_button)
        layout.addLayout(ignored_actions)
        return panel

    def _groups(self) -> list[dict]:
        raw = self._payload.get("groups", {})
        values = raw.values() if isinstance(raw, dict) else raw
        return sorted(
            (dict(value) for value in values if isinstance(value, dict)),
            key=lambda value: (
                str(value.get("name", "")).casefold(), str(value.get("id", ""))
            ),
        )

    def _current_group(self) -> dict | None:
        raw = self._payload.get("groups", {})
        if isinstance(raw, dict):
            value = raw.get(self._group_id)
            return value if isinstance(value, dict) else None
        return next(
            (
                value
                for value in raw
                if isinstance(value, dict) and str(value.get("id", "")) == self._group_id
            ),
            None,
        )

    def _run(
        self, label: str, work, done, *, revision: str | None = None,
        cancellable: bool = False, on_failed=None,
    ) -> None:
        if self._pending is not None or self._closed or self._history_dialog is not None:
            return
        generation = self._generation = self._generation + 1
        task = CoordinatedCallTask(
            self.jobs,
            label,
            work,
            resource="io",
            key=f"concept-groups:{id(self)}:{generation}:{label}",
        )
        self._pending = (task, generation, done, revision, cancellable, on_failed)
        task.signals.complete.connect(
            lambda result, task=task, generation=generation: self._finished(
                task, generation, result
            )
        )
        task.signals.failed.connect(
            lambda message, task=task, generation=generation: self._failed(
                task, generation, str(message)
            )
        )
        task.signals.cancelled.connect(
            lambda task=task, generation=generation: self._failed(
                task, generation, "The training-collection operation was cancelled."
            )
        )
        self.status.setText(label + "…")
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            self._failed(task, generation, str(exc))

    def _finished(self, task, generation: int, result) -> None:
        pending = self._pending
        if pending is None or pending[0] is not task:
            return
        self._pending = None
        if self._shutdown_requested:
            self._closed = True
            super().reject()
            self.background_work_finished.emit()
            return
        if self._closed or generation != self._generation:
            self.background_work_finished.emit()
            return
        try:
            pending[2](result)
        except Exception as exc:
            self.status.setText(
                f"The training-collection result could not be displayed: {exc}. "
                "Reload the saved collections before continuing."
            )
        self._update_actions()
        if self._pending is None:
            self.background_work_finished.emit()

    def _failed(self, task, generation: int, message: str) -> None:
        pending = self._pending
        if pending is None or pending[0] is not task:
            return
        self._pending = None
        if self._shutdown_requested:
            self._closed = True
            super().reject()
            self.background_work_finished.emit()
            return
        if self._closed or generation != self._generation:
            self.background_work_finished.emit()
            return
        detail = message or "The saved collection operation failed."
        handled = bool(pending[5](detail)) if callable(pending[5]) else False
        pending_error = any(
            word in detail.casefold() for word in ("interrupted", "pending", "recover")
        )
        if not handled:
            self.status.setText(
                detail
                + (
                    " Reload to compare current saved collections, or recover the last valid file."
                    if pending_error
                    else " Reload to compare the current saved collections before trying again."
                )
            )
        self._create_then_add = False
        self._add_after_reload_id = ""
        self.recover_button.setVisible(pending_error)
        self._update_actions()
        self.background_work_finished.emit()

    def reload(self, *, select_id: str = "") -> None:
        if self._pending is not None:
            return
        self._snapshot_error=""
        target = str(select_id or self._group_id)
        self._run(
            "Loading saved training collections",
            lambda _job: self.store.snapshot(),
            lambda result: self._loaded(result, target),
            cancellable=True,
            on_failed=lambda message:setattr(self,"_snapshot_error",message),
        )

    def _loaded(self, result, select_id: str = "") -> None:
        payload, revision = result
        self._payload = deepcopy(payload)
        self._revision = str(revision)
        self._snapshot_error=""
        self._new_group = False
        self._details_dirty = False
        self._rules_dirty = False
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self.recover_button.hide()
        ordered = self._groups()
        ids = {str(group.get("id", "")) for group in ordered}
        self._group_id = (
            select_id if select_id in ids else
            (str(ordered[0].get("id", "")) if ordered else "")
        )
        self._render()
        count = len(ids)
        self.status.setText(f"Loaded {count:,} saved training collection{'s' if count != 1 else ''}.")
        if self._add_after_reload_id and self._group_id == self._add_after_reload_id:
            self._add_after_reload_id = ""
            self.add_selected_images()
            return
        if self._skip_rule_refresh_once:
            self._skip_rule_refresh_once = False
            from core.concept_groups import group_fingerprint

            loaded_group = self._current_group()
            if (
                self._rules_verified_dataset is not self.dataset
                or not self._dataset_is_current()
                or loaded_group is None
                or group_fingerprint(loaded_group) != self._rule_published_fingerprint
            ):
                self._rule_after_reload = None
                self._rule_published_fingerprint = ""
                self._rule_failed(
                    "The saved collection or Dataset libraries changed. Refresh and retry."
                )
                return
            self._rule_published_fingerprint = ""
            self._rules_verified = True
            self._rules_failed = False
            group = self._current_group()
            if group is not None and group.get("rules") is not None:
                self.rule_status.setText(
                    f"Saved rules are current · {len(group.get('members', ())):,} included images."
                )
            after = self._rule_after_reload
            self._rule_after_reload = None
            self._update_actions()
            if callable(after):
                after()
            return
        group = self._current_group()
        if group is not None and group.get("rules") is not None:
            self._refresh_saved_rules()
        else:
            self._rules_verified = True
            self._rules_verified_dataset = self.dataset
            self._rules_failed = False
            self._update_actions()

    def recover(self) -> None:
        self._run(
            "Recovering saved training collections",
            lambda _job: self.store.recover(),
            lambda _revision: self._recovered(),
        )

    def _recovered(self) -> None:
        self.recover_button.hide()
        self.status.setText("Recovered the last valid saved training collections. Reloading…")
        self.reload()

    def begin_create(self) -> None:
        if (self._pending is not None or self._details_dirty or self._rules_dirty
                or self._new_group or self._snapshot_error):
            return
        self._create_return_id=self._group_id
        self._new_group = True
        self._details_dirty = False
        self._rules_dirty = False
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self._rules_verified = True
        self._rules_verified_dataset = self.dataset
        self._rules_failed = False
        self._group_id = ""
        self._member_page = 0
        self.groups.clearSelection()
        self.name.clear()
        self.description.clear()
        self._details_dirty = False
        self._render_members()
        self._render_rules()
        self._render_ignored()
        self._update_actions()
        self.name.setFocus(Qt.OtherFocusReason)
        self.status.setText(
            "Enter a name, then create the collection. If images were selected in "
            "Dataset for this window, Create and Add keeps that exact selection."
        )

    def _discard_details(self) -> None:
        if self._pending is not None or self._rules_dirty:
            return
        if self._new_group:
            self._group_id=self._create_return_id
            self._new_group=False
        self._render()
        self.status.setText("Unsaved collection details discarded.")
        self.new_button.setFocus(Qt.OtherFocusReason)

    def save(self) -> None:
        name = self.name.text().strip()
        description = self.description.toPlainText().strip()
        if not name:
            self._create_then_add = False
            self.status.setText("Enter a collection name before saving.")
            self.name.setFocus(Qt.OtherFocusReason)
            return
        revision = self._revision
        if self._new_group:
            self._run(
                "Creating training collection",
                lambda _job: self.store.create(
                    name, description, expected_revision=revision
                ),
                self._created,
                revision=revision,
            )
        elif self._group_id:
            group_id = self._group_id
            self._run(
                "Saving collection details",
                lambda _job: self.store.edit(
                    group_id, name, description, expected_revision=revision
                ),
                lambda group: self.reload(select_id=str(group.get("id", group_id))),
                revision=revision,
            )

    def _created(self, group: dict) -> None:
        group_id = str(group.get("id", ""))
        if self._create_then_add and self.selected_paths:
            self._create_then_add = False
            self._add_after_reload_id = group_id
            self.reload(select_id=group_id)
        else:
            self._create_then_add = False
            self.reload(select_id=group_id)

    def create_and_add_selected(self) -> None:
        if not self._new_group or not self.selected_paths:
            return
        self._create_then_add = True
        self.save()

    def _scope_is_current(self) -> bool:
        if not callable(self.scope_current):
            return True
        try:
            return bool(self.scope_current())
        except Exception:
            return False

    def _dataset_is_current(self) -> bool:
        callback = self.dataset_current
        if not callable(callback):
            callback = self.scope_current
        if not callable(callback):
            return True
        try:
            return bool(callback())
        except Exception:
            return False

    @staticmethod
    def _rule_terms(text: str) -> list[str]:
        return [value.strip() for value in re.split(r"[,\n]", text) if value.strip()]

    def _rules_from_fields(self) -> dict | None:
        libraries = [
            library_id
            for library_id, check in self.rule_library_checks.items()
            if check.isChecked()
        ]
        include_tags = self._rule_terms(self.rule_include_tags.text())
        include_words = self._rule_terms(self.rule_include_words.text())
        if not libraries:
            self.rule_status.setText("Choose at least one named source library.")
            return None
        if not include_tags and not include_words:
            self.rule_status.setText(
                "Add at least one include tag or reviewed description word or phrase."
            )
            return None
        return {
            "schema_version": 1,
            "library_ids": libraries,
            "match_mode": str(self.rule_match_mode.currentData() or "all"),
            "include_tags": include_tags,
            "exclude_tags": self._rule_terms(self.rule_exclude_tags.text()),
            "include_words": include_words,
            "exclude_words": self._rule_terms(self.rule_exclude_words.text()),
        }

    def _rules_changed(self, *_args) -> None:
        if self._rules_loading or self._pending is not None:
            return
        if self._current_group() is None or self._details_dirty:
            return
        self._rules_dirty = True
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self.rule_preview.setRowCount(0)
        self.rule_preview_page_label.setText("No preview")
        self._rules_verified = False
        self._rules_failed = False
        self.rule_status.setText(
            "Rule changes are a draft. Preview them, then Save Rules, or discard them."
        )
        self._update_actions()

    def _rule_failed(self, message: str) -> bool:
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self.rule_preview.setRowCount(0)
        self.rule_preview_page_label.setText("No preview")
        self._rules_verified = False
        self._rules_failed = True
        self.rule_status.setText(
            f"Automatic membership was not changed: {message} Retry the preview or refresh."
        )
        self.status.setText(
            "Automatic membership could not be verified. Existing collection membership was kept."
        )
        return True

    def _preview_rules(self) -> None:
        group = self._current_group()
        if (
            group is not None
            and group.get("rules") is not None
            and not self._rules_dirty
            and (self._rules_failed or not self._rules_verified)
        ):
            self._refresh_saved_rules()
            return
        rules = self._rules_from_fields()
        if group is None or rules is None or self._pending is not None:
            return
        self._begin_rule_preparation(group, rules, automatic=False)

    def _begin_rule_preparation(self, group: dict, rules: dict, *, automatic: bool,
                                after=None) -> None:
        frozen_dataset = self.dataset
        if not self._dataset_is_current():
            self._rule_failed("The Dataset libraries changed. Refresh and retry.")
            return
        frozen_group = deepcopy(group)
        frozen_rules = deepcopy(rules)
        self._rules_verified = False
        self._rules_failed = False

        def prepare(job):
            from core.concept_groups import prepare_rule_update

            if self.dataset is not frozen_dataset or not self._dataset_is_current():
                raise ValueError("The Dataset libraries changed before the preview.")
            return prepare_rule_update(
                frozen_group,
                frozen_rules,
                frozen_dataset,
                progress=job.update,
                should_cancel=lambda: not job.checkpoint(),
            )

        def prepared(result):
            if self.dataset is not frozen_dataset or not self._dataset_is_current():
                self._rule_failed("The Dataset libraries changed. Preview again.")
                return
            if str(getattr(result, "group_id", "")) != self._group_id:
                self._rule_failed("The chosen collection changed. Preview again.")
                return
            if automatic:
                self._publish_prepared_rules(
                    result, after=after, frozen_dataset=frozen_dataset
                )
                return
            self._prepared_rules = result
            self._prepared_rules_dataset = frozen_dataset
            self._rule_preview_page = 0
            self._render_rule_preview()
            self.rules_scroll.ensureWidgetVisible(self.rule_preview, 0, 20)
            if not self._rules_dirty:
                self.rule_status.setText(
                    self.rule_status.text().replace(
                        "Save Rules to publish this complete result.",
                        "Automatic membership is already saved.",
                    )
                )
            self.status.setText("Rule preview completed without changing collection membership.")
            self._update_actions()

        self._run(
            "Refreshing automatic collection membership" if automatic
            else "Previewing automatic collection membership",
            prepare,
            prepared,
            revision=self._revision,
            cancellable=True,
            on_failed=self._rule_failed,
        )

    def _save_rules(self) -> None:
        if self._prepared_rules is None or not self._rules_dirty:
            return
        self._publish_prepared_rules(
            self._prepared_rules, frozen_dataset=self._prepared_rules_dataset
        )

    def _publish_prepared_rules(self, prepared, *, after=None, frozen_dataset=None) -> None:
        frozen_dataset = frozen_dataset or self.dataset
        if self.dataset is not frozen_dataset or not self._dataset_is_current():
            self._rule_failed("The Dataset libraries changed. Preview again.")
            return
        revision = self._revision

        def publish(_job):
            if self.dataset is not frozen_dataset or not self._dataset_is_current():
                raise ValueError("The Dataset libraries changed before rules were saved.")
            return self.store.apply_rule_update(
                prepared, frozen_dataset, expected_revision=revision
            )

        self._run(
            "Saving automatic membership rules",
            publish,
            lambda group: self._rules_published(
                group, after=after, frozen_dataset=frozen_dataset
            ),
            revision=revision,
            on_failed=self._rule_failed,
        )

    def _rules_published(self, group: dict, *, after=None, frozen_dataset=None) -> None:
        from core.concept_groups import group_fingerprint

        self._rules_dirty = False
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self._rules_verified_dataset = frozen_dataset
        self._rule_published_fingerprint = group_fingerprint(group)
        self._skip_rule_refresh_once = True
        self._rule_after_reload = after
        self.reload(select_id=str(group.get("id", self._group_id)))

    def _refresh_saved_rules(self, *, after=None) -> None:
        group = self._current_group()
        if group is None or group.get("rules") is None or self._pending is not None:
            if callable(after):
                after()
            return
        self._rules_verified = False
        self._rules_failed = False
        self.rule_status.setText("Refreshing saved automatic membership…")
        self._update_actions()
        self._begin_rule_preparation(
            group, deepcopy(group["rules"]), automatic=True, after=after
        )

    def _discard_rule_changes(self) -> None:
        if self._pending is not None:
            return
        self._rules_dirty = False
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self._rules_failed = False
        self._render_rules()
        self.status.setText("Discarded the unsaved rule draft.")
        self._update_actions()
        group = self._current_group()
        if group is not None and group.get("rules") is not None:
            self._refresh_saved_rules()
        else:
            self._rules_verified = True
            self._rules_verified_dataset = self.dataset
            self._update_actions()

    def _disable_rules(self) -> None:
        group = self._current_group()
        if group is None or group.get("rules") is None or self._pending is not None:
            return
        group_id = str(group.get("id", ""))
        revision = self._revision
        self._run(
            "Disabling automatic collection membership",
            lambda _job: self.store.disable_rules(
                group_id, expected_revision=revision
            ),
            lambda updated: self._rules_disabled(updated),
            revision=revision,
        )

    def _rules_disabled(self, group: dict) -> None:
        self._rules_dirty = False
        self._prepared_rules = None
        self._prepared_rules_dataset = None
        self._rules_verified_dataset = self.dataset
        self.reload(select_id=str(group.get("id", self._group_id)))

    def add_selected_images(self) -> None:
        if self._details_dirty:
            self.status.setText(
                "Save Changes before adding images so membership stays attached to the displayed collection details."
            )
            return
        group = self._current_group()
        if group is None or not self.selected_paths or self._pending is not None:
            return
        group_id = str(group.get("id", ""))
        revision = self._revision
        paths = self.selected_paths

        def capture(job):
            from core.concept_groups import capture_members

            return capture_members(
                paths,
                self.dataset,
                progress=job.update,
                should_cancel=lambda: bool(
                    getattr(job, "cancel_requested", False)
                ) or not job.checkpoint(),
            )

        self._run(
            f"Hashing {len(paths):,} selected image{'s' if len(paths) != 1 else ''}",
            capture,
            lambda members: self._members_captured(group_id, revision, members),
            revision=revision,
            cancellable=True,
        )

    def _members_captured(self, group_id: str, revision: str, members) -> None:
        if not self._scope_is_current():
            self.status.setText(
                "The Dataset selection or library changed while images were being checked. "
                "No membership changed; reopen the action from the current selection."
            )
            self._update_actions()
            return
        frozen = deepcopy(list(members))

        self._run(
            f"Adding {len(frozen):,} selected image{'s' if len(frozen) != 1 else ''}",
            lambda _job: self.store.add_members(
                group_id, frozen, expected_revision=revision
            ),
            lambda group: self.reload(select_id=str(group.get("id", group_id))),
            revision=revision,
        )

    def _selected_member_dicts(self) -> list[dict]:
        rows = sorted({index.row() for index in self.members.selectedIndexes()})
        values = []
        for row in rows:
            item = self.members.item(row, 0)
            value = item.data(Qt.UserRole) if item is not None else None
            if isinstance(value, dict):
                values.append(deepcopy(value))
        return values

    @staticmethod
    def _table_member(table: QTableWidget, row: int) -> dict | None:
        item = table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return deepcopy(value) if isinstance(value, dict) else None

    def _included_image_requested(self, item: QTableWidgetItem) -> None:
        value = self._table_member(self.members, item.row())
        if value is not None:
            self.image_requested.emit(value)

    def _ignored_image_requested(self, item: QTableWidgetItem) -> None:
        value = self._table_member(self.ignored_members, item.row())
        if value is not None:
            self.image_requested.emit(value)

    def _view_selected_ignored(self) -> None:
        selected = self._selected_ignored_dicts()
        if len(selected) == 1:
            self.image_requested.emit(selected[0])

    def _selected_ignored_dicts(self) -> list[dict]:
        rows = sorted({index.row() for index in self.ignored_members.selectedIndexes()})
        return [
            value for row in rows
            if (value := self._table_member(self.ignored_members, row)) is not None
        ]

    def restore_selected_members(self) -> None:
        group = self._current_group()
        selected = self._selected_ignored_dicts()
        if group is None or not selected or self._pending is not None:
            return
        frozen_dataset = self.dataset
        if not self._dataset_is_current():
            self.status.setText(
                "Dataset libraries changed. Refresh the collection before restoring."
            )
            return
        group_id = str(group.get("id", ""))
        revision = self._revision
        frozen = deepcopy(selected)

        def recapture(job):
            from core.concept_groups import capture_members
            from core.federated_dataset import LibraryImageIdentity

            if self.dataset is not frozen_dataset or not self._dataset_is_current():
                raise ValueError("Dataset libraries changed before image restoration.")
            paths = []
            for row in frozen:
                identity = LibraryImageIdentity(
                    str(row["library_id"]), str(row["relative_path"])
                )
                path = Path(frozen_dataset.path_for_identity(identity)).absolute()
                if frozen_dataset.identity_for_path(path) != identity:
                    raise ValueError(
                        "An ignored image no longer has the same library identity."
                    )
                paths.append(path)
            recaptured = capture_members(
                paths,
                frozen_dataset,
                progress=job.update,
                should_cancel=lambda: not job.checkpoint(),
            )
            expected = {
                (str(row["library_id"]), str(row["relative_path"])):
                str(row["image_sha256"])
                for row in frozen
            }
            actual = {
                (str(row["library_id"]), str(row["relative_path"])):
                str(row["image_sha256"])
                for row in recaptured
            }
            if actual != expected:
                raise ValueError(
                    "An ignored image was replaced or changed. It was not restored."
                )
            return recaptured

        self._run(
            f"Checking {len(frozen):,} ignored image{'s' if len(frozen) != 1 else ''}",
            recapture,
            lambda members: self._restore_recaptured(
                group_id, revision, members, frozen_dataset
            ),
            revision=revision,
            cancellable=True,
        )

    def _restore_recaptured(
        self, group_id: str, revision: str, members, frozen_dataset
    ) -> None:
        if self.dataset is not frozen_dataset or not self._dataset_is_current():
            self.status.setText(
                "Dataset libraries changed during image checks. Nothing was restored."
            )
            return
        frozen = deepcopy(list(members))

        def restore(_job):
            if self.dataset is not frozen_dataset or not self._dataset_is_current():
                raise ValueError("Dataset libraries changed before image restoration.")
            return self.store.restore_members(
                group_id, frozen, expected_revision=revision
            )

        self._run(
            f"Restoring {len(frozen):,} image{'s' if len(frozen) != 1 else ''} to collection",
            restore,
            lambda updated: self.reload(select_id=str(updated.get("id", group_id))),
            revision=revision,
        )

    def remove_selected_members(self) -> None:
        if self._details_dirty:
            self.status.setText(
                "Save Changes before removing members so the displayed collection details are not lost."
            )
            return
        group = self._current_group()
        selected = self._selected_member_dicts()
        if group is None or not selected or self._pending is not None:
            return
        group_id = str(group.get("id", ""))
        revision = self._revision
        self._run(
            f"Ignoring {len(selected):,} selected image{'s' if len(selected) != 1 else ''}",
            lambda _job: self.store.remove_members(
                group_id, selected, expected_revision=revision
            ),
            lambda updated: self.reload(select_id=str(updated.get("id", group_id))),
            revision=revision,
        )

    def _prepare_training(self) -> None:
        if self._training_handoff_active:
            return
        if not self._dataset_is_current():
            self._rules_verified = False
            self.status.setText(
                "Dataset libraries changed. Refresh this collection before building training data."
            )
            self._update_actions()
            return
        if self._details_dirty:
            self.status.setText(
                "Save Changes before building training data so the frozen handoff matches the displayed collection."
            )
            return
        if self._rules_dirty:
            self.status.setText(
                "Save Rules or discard the rule draft before building training data."
            )
            return
        group = self._current_group()
        if group is None or self._pending is not None:
            return
        if group.get("rules") is not None:
            if self._rules_failed:
                self.status.setText(
                    "Retry the saved automatic-membership refresh before building training data."
                )
                return
            self._refresh_saved_rules(after=self._emit_training_snapshot)
            return
        self._emit_training_snapshot()

    def _emit_training_snapshot(self) -> None:
        group = self._current_group()
        if (
            group is None or self._pending is not None or self._rules_dirty
            or not self._dataset_is_current()
            or (
                group.get("rules") is not None
                and self._rules_verified_dataset is not self.dataset
            )
        ):
            self.status.setText(
                "Dataset libraries changed. Refresh this collection before building training data."
            )
            self._rules_verified = False
            self._update_actions()
            return
        members = group.get("members", ())
        if not isinstance(members, (list, tuple)) or not members:
            self.status.setText(
                "Add images to this collection from Dataset before building training data."
            )
            self._update_actions()
            return
        snapshot = deepcopy(group)
        snapshot["members"] = deepcopy(list(members))
        # The Dataset receiver opens the goal dialog synchronously. Keep this
        # collection's saved context alive while that decision is pending so a
        # cancelled goal returns here instead of dropping the user at Dataset.
        self._training_handoff_active = True
        self.hide()
        try:
            self.training_requested.emit(snapshot)
        finally:
            self._training_handoff_active = False
        if not self._closed and not self._shutdown_requested:
            self.show()
            self.raise_()
            self.activateWindow()
            self.prepare_button.setFocus(Qt.OtherFocusReason)

    def complete_training_handoff(self) -> bool:
        """Close only after Dataset accepted the frozen collection handoff."""
        if self._closed or self._shutdown_requested:
            return False
        self._closed = True
        super().accept()
        return True

    def _group_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if self._details_dirty or self._rules_dirty:
            self.status.setText(
                "Save or discard the current draft before choosing another collection."
            )
            return
        if current is None:
            return
        self._group_id = str(current.data(Qt.UserRole) or "")
        self._new_group = False
        self._member_page = 0
        self._ignored_page = 0
        self._rules_verified = False
        self._rules_failed = False
        self._render_editor()
        group = self._current_group()
        if group is not None and group.get("rules") is not None:
            self._refresh_saved_rules()
        else:
            self._rules_verified = True
            self._rules_verified_dataset = self.dataset
            self._update_actions()

    def _render(self) -> None:
        self._render_groups()
        self._render_editor()

    def _render_groups(self) -> None:
        values = self._groups()
        pages = max(1, (len(values) + self.GROUP_PAGE_SIZE - 1) // self.GROUP_PAGE_SIZE)
        self._group_page = min(self._group_page, pages - 1)
        selected_position = next(
            (i for i, group in enumerate(values) if str(group.get("id", "")) == self._group_id),
            -1,
        )
        if selected_position >= 0:
            self._group_page = selected_position // self.GROUP_PAGE_SIZE
        start = self._group_page * self.GROUP_PAGE_SIZE
        page = values[start : start + self.GROUP_PAGE_SIZE]
        self.groups.blockSignals(True)
        self.groups.clear()
        selected_row = -1
        for row, group in enumerate(page):
            members = group.get("members", ())
            count = len(members) if isinstance(members, (list, tuple)) else 0
            item = QListWidgetItem(
                f"{str(group.get('name', '')).strip() or 'Unnamed collection'}  ·  {count:,} image{'s' if count != 1 else ''}"
            )
            item.setData(Qt.UserRole, str(group.get("id", "")))
            item.setToolTip(str(group.get("description", "")))
            self.groups.addItem(item)
            if str(group.get("id", "")) == self._group_id:
                selected_row = row
        if selected_row >= 0:
            self.groups.setCurrentRow(selected_row)
        self.groups.blockSignals(False)
        self.group_page_label.setText(
            f"{self._group_page + 1} / {pages} · {len(values):,} collection{'s' if len(values) != 1 else ''}"
            if values
            else "No saved collections"
        )
        self.group_previous.setEnabled(self._group_page > 0 and self._pending is None)
        self.group_next.setEnabled(self._group_page + 1 < pages and self._pending is None)
        self.group_previous.setVisible(pages>1)
        self.group_next.setVisible(pages>1)

    def _render_editor(self) -> None:
        group = None if self._new_group else self._current_group()
        self.name.blockSignals(True)
        self.description.blockSignals(True)
        self.name.setText(str(group.get("name", "")) if group else "")
        self.description.setPlainText(str(group.get("description", "")) if group else "")
        self.name.blockSignals(False)
        self.description.blockSignals(False)
        self._details_dirty = False
        self._render_members()
        self._render_rules()
        self._render_ignored()
        self._update_actions()

    def _render_members(self) -> None:
        group = None if self._new_group else self._current_group()
        members = list(group.get("members", ())) if group else []
        pages = max(1, (len(members) + self.MEMBER_PAGE_SIZE - 1) // self.MEMBER_PAGE_SIZE)
        self._member_page = min(self._member_page, pages - 1)
        start = self._member_page * self.MEMBER_PAGE_SIZE
        page = members[start : start + self.MEMBER_PAGE_SIZE]
        self.members.setRowCount(len(page))
        for row, member in enumerate(page):
            relative = str(member.get("relative_path", ""))
            digest = str(member.get("image_sha256", ""))
            image = QTableWidgetItem(relative)
            image.setData(Qt.UserRole, deepcopy(member))
            image.setToolTip(f"{relative}\nSHA-256: {digest}")
            self.members.setItem(row, 0, image)
            library_id = str(member.get("library_id", ""))
            library_name = library_id
            profile_provider = getattr(self.dataset, "profile", None)
            if callable(profile_provider):
                try:
                    profile = profile_provider(library_id)
                    library_name = str(getattr(profile, "name", "") or library_id)
                except Exception:
                    library_name = library_id
            library_item = QTableWidgetItem(library_name)
            library_item.setToolTip(f"Library ID: {library_id}")
            self.members.setItem(row, 1, library_item)
            origin = (
                "Automatic rule"
                if member.get("membership_source") == "automatic"
                else "You"
            )
            self.members.setItem(row, 2, QTableWidgetItem(origin))
            digest_item = QTableWidgetItem(digest)
            digest_item.setToolTip(digest)
            self.members.setItem(row, 3, digest_item)
        self.member_page_label.setText(
            f"{self._member_page + 1} / {pages} · {len(members):,} image{'s' if len(members) != 1 else ''}"
            if members
            else "No members"
        )
        self.member_previous.setEnabled(self._member_page > 0 and self._pending is None)
        self.member_next.setEnabled(self._member_page + 1 < pages and self._pending is None)
        self.member_previous.setVisible(pages>1)
        self.member_next.setVisible(pages>1)
        self.member_page_label.setVisible(bool(members))
        self.members.setVisible(bool(members))
        self.members_empty.setVisible(not members)
        self.show_identity_details.setVisible(bool(members))

    def _render_rules(self) -> None:
        group = None if self._new_group else self._current_group()
        rules = group.get("rules") if group is not None else None
        self._rules_loading = True
        try:
            libraries = set(rules.get("library_ids", ())) if isinstance(rules, dict) else set()
            for library_id, check in self.rule_library_checks.items():
                check.setChecked(library_id in libraries)
            mode = str(rules.get("match_mode", "all")) if isinstance(rules, dict) else "all"
            index = self.rule_match_mode.findData(mode)
            self.rule_match_mode.setCurrentIndex(max(0, index))

            def joined(key: str) -> str:
                values = rules.get(key, ()) if isinstance(rules, dict) else ()
                return ", ".join(str(value) for value in values)

            self.rule_include_tags.setText(joined("include_tags"))
            self.rule_exclude_tags.setText(joined("exclude_tags"))
            self.rule_include_words.setText(joined("include_words"))
            self.rule_exclude_words.setText(joined("exclude_words"))
        finally:
            self._rules_loading = False
        if group is None:
            self.rule_status.setText(
                "Create or choose a saved collection to configure automatic membership."
            )
        elif rules is None:
            self.rule_status.setText(
                "Automatic membership is off. Manual membership remains unchanged."
            )
        elif not self._rules_failed:
            self.rule_status.setText("Saved rules are waiting for a current worker refresh.")
        if self._prepared_rules is None:
            self._rule_preview_page = 0
            self.rule_preview.setRowCount(0)
            self.rule_preview_page_label.setText("No preview")

    @staticmethod
    def _member_key(member: dict) -> tuple[str, str]:
        return (
            str(member.get("library_id", "")).casefold(),
            str(member.get("relative_path", "")).casefold(),
        )

    def _render_rule_preview(self) -> None:
        prepared = self._prepared_rules
        group = self._current_group()
        preview = getattr(prepared, "preview", None)
        paths = tuple(getattr(preview, "paths", ()) or ())
        if prepared is None or group is None:
            self.rule_preview.setRowCount(0)
            self.rule_preview_page_label.setText("No preview")
            return
        manual = {
            self._member_key(row): row
            for row in group.get("members", ())
            if row.get("membership_source", "manual") == "manual"
        }
        ignored = {
            self._member_key(row): row for row in group.get("ignored_members", ())
        }
        automatic_count = len(tuple(getattr(prepared, "members", ()) or ()))
        pages = max(1, (len(paths) + self.MEMBER_PAGE_SIZE - 1) // self.MEMBER_PAGE_SIZE)
        self._rule_preview_page = min(self._rule_preview_page, pages - 1)
        start = self._rule_preview_page * self.MEMBER_PAGE_SIZE
        page = paths[start : start + self.MEMBER_PAGE_SIZE]
        self.rule_preview.setRowCount(len(page))
        provider = getattr(self.dataset, "profile", None)
        identities = []
        for path in page:
            identity = self.dataset.identity_for_path(path)
            identities.append(identity)
        for row, identity in enumerate(identities):
            if identity is None:
                continue
            library_id = str(getattr(identity, "library_id", ""))
            relative = str(getattr(identity, "relative_path", ""))
            key = (library_id.casefold(), relative.casefold())
            if key in ignored:
                result = "Ignored"
                value = ignored[key]
            elif key in manual:
                result = "Already included by you"
                value = manual[key]
            else:
                result = "Will be added automatically"
                value = {
                    "library_id": library_id, "relative_path": relative
                }
            image = QTableWidgetItem(relative)
            image.setData(Qt.UserRole, deepcopy(value))
            image.setToolTip(relative)
            self.rule_preview.setItem(row, 0, image)
            library_name = library_id
            if callable(provider):
                try:
                    profile = provider(library_id)
                    library_name = str(getattr(profile, "name", "") or library_id)
                except Exception:
                    pass
            library = QTableWidgetItem(library_name)
            library.setToolTip(f"Library ID: {library_id}")
            self.rule_preview.setItem(row, 1, library)
            self.rule_preview.setItem(row, 2, QTableWidgetItem(result))
        result_count = len(manual) + automatic_count
        self.rule_status.setText(
            f"Preview ready · {len(paths):,} matching · {automatic_count:,} automatic · "
            f"{len(manual):,} manual retained · {len(ignored):,} ignored overrides · "
            f"{result_count:,} resulting images. Save Rules to publish this complete result."
        )
        self.rule_preview_page_label.setText(
            f"{self._rule_preview_page + 1} / {pages} · {len(paths):,} matching images"
            if paths else "0 matching images"
        )

    def _preview_image_requested(self, item: QTableWidgetItem) -> None:
        value = self._table_member(self.rule_preview, item.row())
        if value is not None:
            self.image_requested.emit(value)

    def _previous_rule_preview_page(self) -> None:
        self._rule_preview_page = max(0, self._rule_preview_page - 1)
        self._render_rule_preview()
        self._update_actions()

    def _next_rule_preview_page(self) -> None:
        self._rule_preview_page += 1
        self._render_rule_preview()
        self._update_actions()

    def _render_ignored(self) -> None:
        group = None if self._new_group else self._current_group()
        ignored = list(group.get("ignored_members", ())) if group else []
        pages = max(1, (len(ignored) + self.MEMBER_PAGE_SIZE - 1) // self.MEMBER_PAGE_SIZE)
        self._ignored_page = min(self._ignored_page, pages - 1)
        start = self._ignored_page * self.MEMBER_PAGE_SIZE
        page = ignored[start : start + self.MEMBER_PAGE_SIZE]
        self.ignored_members.setRowCount(len(page))
        for row, member in enumerate(page):
            relative = str(member.get("relative_path", ""))
            digest = str(member.get("image_sha256", ""))
            image = QTableWidgetItem(relative)
            image.setData(Qt.UserRole, deepcopy(member))
            image.setToolTip(f"{relative}\nSHA-256: {digest}")
            self.ignored_members.setItem(row, 0, image)
            library_id = str(member.get("library_id", ""))
            library_name = library_id
            provider = getattr(self.dataset, "profile", None)
            if callable(provider):
                try:
                    profile = provider(library_id)
                    library_name = str(getattr(profile, "name", "") or library_id)
                except Exception:
                    pass
            library = QTableWidgetItem(library_name)
            library.setToolTip(f"Library ID: {library_id}")
            self.ignored_members.setItem(row, 1, library)
            origin = (
                "Automatic rule"
                if member.get("membership_source") == "automatic"
                else "You"
            )
            self.ignored_members.setItem(row, 2, QTableWidgetItem(origin))
        self.ignored_page_label.setText(
            f"{self._ignored_page + 1} / {pages} · {len(ignored):,} ignored image"
            f"{'s' if len(ignored) != 1 else ''}"
            if ignored else "No ignored images"
        )
        self.members_tabs.setTabText(
            2, f"Ignored Images ({len(ignored):,})"
        )

    def _previous_ignored_page(self) -> None:
        self._ignored_page = max(0, self._ignored_page - 1)
        self._render_ignored()
        self._update_actions()

    def _next_ignored_page(self) -> None:
        self._ignored_page += 1
        self._render_ignored()
        self._update_actions()

    def _previous_group_page(self) -> None:
        self._group_page = max(0, self._group_page - 1)
        self._group_id = ""
        self._render()

    def _next_group_page(self) -> None:
        self._group_page += 1
        self._group_id = ""
        self._render()

    def _previous_member_page(self) -> None:
        self._member_page = max(0, self._member_page - 1)
        self._render_members()
        self._update_actions()

    def _next_member_page(self) -> None:
        self._member_page += 1
        self._render_members()
        self._update_actions()

    def _update_actions(self) -> None:
        busy = self._pending is not None or self._history_dialog is not None
        dirty = self._details_dirty or self._rules_dirty
        group = None if self._new_group else self._current_group()
        self.history_button.setVisible(callable(self.history_opener))
        self.history_button.setEnabled(not busy and not dirty and group is not None and not self._new_group)
        self.observe_history_button.setVisible(callable(self.history_opener))
        self.observe_history_button.setEnabled(not busy and group is not None and not self._new_group)
        selected_count = len(self.selected_paths)
        member_count = len(self._selected_member_dicts())
        saved_members = group.get("members", ()) if group is not None else ()
        full_count = (
            len(saved_members) if isinstance(saved_members, (list, tuple)) else 0
        )
        self.new_button.setEnabled(not busy and not dirty and not self._new_group and not self._snapshot_error)
        self.reload_button.setEnabled(not busy and not self._rules_dirty)
        self.recover_button.setEnabled(not busy)
        self.groups.setEnabled(not busy and not dirty and not self._new_group)
        self.browser.setVisible(bool(self._groups()))
        self.editor_stack.setCurrentIndex(1 if self._new_group or group is not None else 0)
        self.members_tabs.setVisible(group is not None)
        self.selection_context.setVisible(selected_count>0 and (self._new_group or group is not None))
        self.prepare_button.setVisible(group is not None and not self._new_group)
        self.cancel_details_button.setVisible(self._new_group or self._details_dirty)
        self.cancel_details_button.setEnabled(not busy and not self._rules_dirty)
        self.save_button.setVisible(self._new_group or self._details_dirty)
        if group is None and not self._new_group:
            if busy:
                title,message="Loading Collections…","Your saved collections will appear here when loading finishes."
            elif self._snapshot_error:
                title,message="Collections Unavailable","Use Reload above to try again. The operation status below explains the problem."
            elif self._groups():
                title,message="Choose a Collection","Select a saved collection to review its images, or create another above."
            else:
                title,message="No Training Collections","Choose Create Collection above to name your first image collection."
            self.empty_panel.setTitle(title); self.empty_panel.setText(message)
        self.name.setEnabled(
            not busy and not self._rules_dirty and (self._new_group or group is not None)
        )
        self.description.setEnabled(
            not busy and not self._rules_dirty and (self._new_group or group is not None)
        )
        self.save_button.setText("Create Collection" if self._new_group else "Save Changes")
        self.save_button.setEnabled(
            not busy and not self._rules_dirty
            and bool(self.name.text().strip())
            and (self._new_group or (group is not None and self._details_dirty))
        )
        self.create_add_button.setText("Create and Add")
        self.create_add_button.setToolTip(f"Create this collection and add the {selected_count:,} images selected in Dataset.")
        self.create_add_button.setVisible(self._new_group and selected_count > 0)
        self.create_add_button.setEnabled(not busy and self._new_group and selected_count > 0 and bool(self.name.text().strip()))
        primary=not (self._new_group and selected_count>0)
        if self.save_button.property("primary")!=primary:
            self.save_button.setProperty("primary",primary)
            self.save_button.style().unpolish(self.save_button)
            self.save_button.style().polish(self.save_button)
        self.add_button.setText("Add Selected Images")
        self.add_button.setToolTip(f"Add the {selected_count:,} images selected in Dataset to this saved collection.")
        self.add_button.setVisible(selected_count>0)
        self.add_button.setEnabled(
            not busy and not dirty and group is not None and selected_count > 0
        )
        self.remove_button.setText("Remove Selected Images")
        self.remove_button.setVisible(full_count>0)
        self.member_selection_count.setVisible(full_count>0)
        self.member_selection_count.setText(f"Selected: {member_count:,}")
        self.remove_button.setToolTip(
            f"Remove {member_count:,} selected images from this collection only. "
            "Removed images stay ignored so automatic rules do not add them again."
        )
        self.remove_button.setEnabled(
            not busy and not dirty and group is not None and member_count > 0
        )
        self.prepare_button.setEnabled(
            not busy and not dirty and group is not None and full_count > 0
            and (group.get("rules") is None or self._rules_verified)
            and not self._rules_failed
        )
        has_group = group is not None
        has_rules = has_group and group.get("rules") is not None
        rule_editable = not busy and has_group and not self._details_dirty
        for check in self.rule_library_checks.values():
            check.setEnabled(rule_editable)
        for control in (
            self.rule_match_mode,
            self.rule_include_tags,
            self.rule_exclude_tags,
            self.rule_include_words,
            self.rule_exclude_words,
        ):
            control.setEnabled(rule_editable)
        self.preview_rules_button.setEnabled(rule_editable)
        self.save_rules_button.setEnabled(
            not busy and self._rules_dirty and self._prepared_rules is not None
        )
        self.discard_rules_button.setEnabled(not busy and self._rules_dirty)
        self.disable_rules_button.setEnabled(not busy and has_rules)
        preview_paths = tuple(
            getattr(getattr(self._prepared_rules, "preview", None), "paths", ()) or ()
        )
        preview_pages = max(
            1, (len(preview_paths) + self.MEMBER_PAGE_SIZE - 1) // self.MEMBER_PAGE_SIZE
        )
        self.rule_preview_previous.setEnabled(
            not busy and self._rule_preview_page > 0
        )
        self.rule_preview_next.setEnabled(
            not busy and self._rule_preview_page + 1 < preview_pages
        )
        ignored_selected = len(self._selected_ignored_dicts())
        self.ignored_selection_count.setText(f"Selected: {ignored_selected:,}")
        self.restore_button.setText("Restore Selected Images")
        self.restore_button.setToolTip(f"Restore {ignored_selected:,} selected ignored images to this collection.")
        self.restore_button.setEnabled(
            not busy and not dirty and has_group and ignored_selected > 0
        )
        self.view_ignored_button.setEnabled(
            not busy and not dirty and has_group and ignored_selected == 1
        )
        ignored = list(group.get("ignored_members", ())) if group else []
        ignored_pages = max(
            1, (len(ignored) + self.MEMBER_PAGE_SIZE - 1) // self.MEMBER_PAGE_SIZE
        )
        self.ignored_previous.setEnabled(
            not busy and not dirty and self._ignored_page > 0
        )
        self.ignored_next.setEnabled(
            not busy and not dirty and self._ignored_page + 1 < ignored_pages
        )
        self.ignored_previous.setVisible(ignored_pages>1)
        self.ignored_next.setVisible(ignored_pages>1)
        self.rule_preview_previous.setVisible(preview_pages>1)
        self.rule_preview_next.setVisible(preview_pages>1)
        self.members_empty.setText(
            "Use Add Selected Images below to include the images selected in Dataset."
            if selected_count else
            "In Dataset, select images and choose Training → Add to Collection, or use Automatic Membership here."
        )
        if group is None:
            self.concept_summary.setText(
                "New collection: create it before building training data."
                if self._new_group else
                "Choose or create a collection to review its full saved image set."
            )
        else:
            concept_name = str(group.get("name", "")).strip() or "Unnamed collection"
            if full_count == 0:
                scope = f"Chosen collection: “{concept_name}” · 0 saved images."
            elif full_count == 1:
                scope = (
                    f"Chosen collection: “{concept_name}” · 1 saved image. "
                    "Build Training Data opens read-only review and configuration "
                    "for this saved image, goal, model, output, and compatible "
                    "trainer; it does not write a package or start training. Add more "
                    "representative images from Dataset when they help cover "
                    "the collection."
                )
            else:
                scope = (
                    f"Chosen collection: “{concept_name}” · {full_count:,} saved "
                    f"images. Build Training Data opens read-only review and "
                    f"configuration for all {full_count:,} saved images, goal, model, output, and "
                    "compatible trainer; it does not write a package or start training."
                )
            self.concept_summary.setText(scope)
        if selected_count:
            target = (
                "the new collection with Create and Add"
                if self._new_group else
                f"“{str(group.get('name','')).strip() or 'Unnamed collection'}” with Add"
                if group is not None else
                "a chosen collection with Add"
            )
            self.selection_context.setText(
                f"Images selected in Dataset: {selected_count:,} image"
                f"{'s' if selected_count != 1 else ''}. Add this exact selection to "
                f"{target}; saving membership does not change the Dataset images."
            )
        else:
            self.selection_context.setText(
                "No Dataset images were selected for this window. In Dataset, select "
                "images, right-click, and choose Add to Collection."
            )
        self.group_previous.setEnabled(
            self.group_previous.isEnabled() and not busy and not dirty
        )
        self.group_next.setEnabled(
            self.group_next.isEnabled() and not busy and not dirty
        )

    def _details_changed(self) -> None:
        if self._pending is not None:
            return
        self._details_dirty = True
        self.status.setText(
            "Create the collection to keep these details."
            if self._new_group
            else "Save Changes, or Reload to discard them, before changing membership or building training data."
        )
        self._update_actions()

    def reject(self) -> None:
        if self._history_dialog is not None:
            self._shutdown_requested = True
            self._history_dialog.reject()
            return
        if self._pending is not None and not self._pending[4]:
            self._shutdown_requested = True
            self.status.setText(
                "Closing after the saved collection update finishes safely…"
            )
            self._update_actions()
            return
        self._closed = True
        self._generation += 1
        if self._pending is not None:
            self._pending[0].cancel()
            self._pending = None
            self.background_work_finished.emit()
        super().reject()

    def closeEvent(self, event) -> None:
        if self._history_dialog is not None:
            event.ignore()
            self.reject()
            return
        if self._pending is not None and not self._pending[4]:
            self._shutdown_requested = True
            self.status.setText(
                "Closing after the saved collection update finishes safely…"
            )
            self._update_actions()
            event.ignore()
            return
        self._closed = True
        self._generation += 1
        if self._pending is not None:
            self._pending[0].cancel()
            self._pending = None
            self.background_work_finished.emit()
        super().closeEvent(event)

    def has_active_background_work(self) -> bool:
        return self._pending is not None or (self._history_dialog is not None and self._history_dialog._task is not None)

    def request_background_shutdown(self) -> bool:
        if self._history_dialog is not None:
            self.reject()
            return self.has_active_background_work()
        pending = self._pending
        if pending is None:
            return False
        self._shutdown_requested = True
        if pending[4]:
            pending[0].cancel()
            self._pending = None
            self._closed = True
            super().reject()
            self.background_work_finished.emit()
        else:
            self.status.setText(
                "Closing after the saved collection update finishes safely…"
            )
            self._update_actions()
        return True

    def open_history(self, *, observation_only=False) -> None:
        group = self._current_group()
        if (group is None or not callable(self.history_opener) or self._pending is not None
                or ((self._details_dirty or self._rules_dirty) and not observation_only)
                or self._new_group or self._closed):
            return
        if self._history_dialog is not None:
            self._history_dialog.raise_()
            return
        self._history_results = None
        def restored(outcomes):
            for outcome in outcomes:
                if outcome.status == "complete" and outcome.group_id == group["id"]:
                    self._history_results = outcome
        history = (self.history_opener(group["id"], restored, observation_only=True)
            if observation_only else self.history_opener(group["id"], restored))
        self._history_dialog = history
        history.busyChanged.connect(lambda _busy: self._update_actions())
        def closed(_result):
            self._history_dialog = None
            outcome = self._history_results
            self._history_results = None
            if self._shutdown_requested:
                self.reject()
            elif outcome is not None:
                import json
                self._rules_published(json.loads(outcome.group_json), frozen_dataset=self.dataset)
            else:
                self._update_actions()
            self.background_work_finished.emit()
        history.finished.connect(closed)
        self._update_actions()
