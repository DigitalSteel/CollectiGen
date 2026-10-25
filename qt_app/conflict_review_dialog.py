from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QSignalBlocker, QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    ConflictReviewService,
    ConflictReviewStaleError,
    ConflictRuleEngine,
    ImageRecord,
    OperationJournal,
)
from core.paths import DEFAULTS_DIR, USER_DATA_DIR
from .components import (
    BoundedSearchableComboBox,
    set_progress_bar,
    show_operation_in_progress,
)
from .gallery import EmbeddedGallery
from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask


class ConflictReviewDialog(FeedbackEnabledDialog):
    """Review Center's bounded queue for canonical diagnostic conflicts."""

    feedback_workflow = "Conflicts and guided checks"

    data_changed = Signal()
    edit_image_data = Signal(str)
    open_dataset_image = Signal(str)
    open_automation = Signal(str, object)
    force_ai_requested = Signal(object, str)
    background_work_finished = Signal()

    PAGE_SIZE = 500
    MAX_ENTRIES = 5000

    def __init__(
        self,
        state,
        parent=None,
        *,
        scope=None,
        conflicts: ConflictRuleEngine | None = None,
        history_operation_factory=None,
        history_opener=None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self._history_operation_factory = history_operation_factory
        self._history_opener = history_opener
        self._history_target = None
        self._pending_history_images = set()
        self._close_requested = False
        self.scope_payload = dict(scope or {}) if isinstance(scope, dict) else {}
        self.conflicts = conflicts or ConflictRuleEngine.load(
            DEFAULTS_DIR / "conflicts.json", tags=state.knowledge,
        )
        self.service = ConflictReviewService(
            self.conflicts,
            OperationJournal(USER_DATA_DIR / "operation_journal.jsonl"),
            knowledge=state.knowledge,
        )
        self.pool = QThreadPool.globalInstance()
        self._scan_task = None
        self.entries = []
        self.position = 0
        self._last_result = None
        self._last_entry = None
        self._last_entries = ()
        self._last_position = 0
        self._stale_rule_request = ""
        self._path_scope_error = ""
        self._prefilled_paths = tuple(
            dict.fromkeys(
                Path(value)
                for value in (
                    self.scope_payload.get("images")
                    or ([self.scope_payload["image"]] if self.scope_payload.get("image") else ())
                )
                if str(value)
            )
        )
        self.setWindowTitle("Conflicts & Guided Checks")
        self.resize(1240, 790)
        self.setMinimumSize(980, 650)
        self._build()
        self._apply_scope()
        self._show_current()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        title = QLabel("Conflicts & Guided Checks")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        intro = QLabel(
            "Review current Image Knowledge against canonical diagnostic rules. "
            "Correct image facts in the universal editor, record a reasoned "
            "per-image exception, defer the decision, or open the exact reusable "
            "rule. No decision here edits Training TXT, Metadata, pixels, or file locations."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        root.addWidget(intro)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Rule"))
        self.rule = BoundedSearchableComboBox(
            placeholder="Type a rule name or involved Tag…", max_visible=12,
        )
        self.rule.setAccessibleName("Conflict rule")
        self.rule.setAccessibleDescription(
            "Choose all active conflict rules, or type part of a rule name, "
            "stable identifier, or involved SDXL Tag to find one exact rule."
        )
        self.rule.addItem("All active conflict rules", "")
        for item in sorted(
            self.conflicts.rules,
            key=lambda value: ((value.label or value.id).casefold(), value.id),
        ):
            values=tuple(dict.fromkeys(self.conflicts.effective_tags(item)))
            if values:
                shown=", ".join(values[:3])
                if len(values)>3:
                    shown+=f" +{len(values)-3} more"
                label=f"{item.label or item.id} — {shown}"
            else:
                label=f"{item.label or item.id} — {item.relationship_id or item.id}"
            self.rule.addItem(
                label,item.relationship_id or item.id,
                search_terms=(
                    item.label,item.id,item.relationship_id,item.scope,*values,
                ),
            )
            tooltip=(
                f"{item.label or item.id}\n"
                f"Rule: {item.relationship_id or item.id}\n"
                f"Scope: {item.scope}"
            )
            if values:
                tooltip+="\nInvolved SDXL Tags: "+", ".join(values)
            self.rule.setItemData(self.rule.count()-1,tooltip,Qt.ToolTipRole)
        controls.addWidget(self.rule, 1)
        controls.addWidget(QLabel("Scope"))
        self.scope = QComboBox()
        self.scope.addItem("Whole Library", "library")
        self.scope.addItem("Prefilled Image(s)", "prefilled")
        prefilled_item = self.scope.model().item(1)
        if prefilled_item is not None:
            prefilled_item.setEnabled(bool(self._prefilled_paths))
        controls.addWidget(self.scope)
        self.include_accepted = QCheckBox("Include accepted exceptions")
        self.include_deferred = QCheckBox("Include deferred")
        controls.addWidget(self.include_accepted)
        controls.addWidget(self.include_deferred)
        self.build_queue = QPushButton("Build Queue")
        self.build_queue.setProperty("primary", True)
        self.build_queue.clicked.connect(self.start_scan)
        self.rule.currentIndexChanged.connect(self._rule_scope_changed)
        controls.addWidget(self.build_queue)
        root.addLayout(controls)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.hide()
        self.progress_text = QLabel()
        self.progress_text.setObjectName("mutedLabel")
        self.progress_text.hide()
        self.cancel = QPushButton("Cancel Safely")
        self.cancel.clicked.connect(self.cancel_scan)
        self.cancel.hide()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_text)
        progress_row.addWidget(self.cancel)
        root.addLayout(progress_row)
        from .components import bind_status_footer
        bind_status_footer(self,self.progress_text,self.progress,self.cancel,title="Conflict review")
        self.status = QLabel(
            "Build a queue to find current diagnostic conflicts. Opening this "
            "workspace has not scanned or changed any image."
        )
        self.status.setWordWrap(True)
        self.status.setObjectName("mutedLabel")
        root.addWidget(self.status)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)
        image_panel = QWidget()
        image_panel.setMinimumWidth(360)
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(0, 0, 8, 0)
        self.queue = QTreeWidget()
        self.queue.setHeaderLabels(("Image", "Rule / Owner", "Decision"))
        self.queue.setRootIsDecorated(False)
        self.queue.setAlternatingRowColors(False)
        self.queue.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.queue.setAccessibleName("Conflict review queue")
        self.queue.setAccessibleDescription(
            "Use Ctrl or Shift to select findings. Shared decisions are enabled "
            "only when every selected image uses the same canonical rule and state."
        )
        queue_header = self.queue.header()
        queue_header.setStretchLastSection(False)
        queue_header.setMinimumSectionSize(90)
        queue_header.setSectionResizeMode(0, QHeaderView.Fixed)
        queue_header.setSectionResizeMode(1, QHeaderView.Stretch)
        queue_header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.queue.setColumnWidth(0, 145)
        self.queue.setColumnWidth(2, 110)
        self.queue.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue.setMinimumHeight(150)
        self.queue.setMaximumHeight(230)
        self.queue.currentItemChanged.connect(self._queue_current_changed)
        self.queue.itemSelectionChanged.connect(self._queue_selection_changed)
        image_layout.addWidget(self.queue)
        self.selection_summary = QLabel("No conflict finding selected")
        self.selection_summary.setObjectName("mutedLabel")
        self.selection_summary.setWordWrap(True)
        image_layout.addWidget(self.selection_summary)
        self.gallery = EmbeddedGallery(
            page_size=1, card_width=320, show_open_dataset=True,
        )
        self.gallery.edit_requested.connect(self.correct_image_facts)
        self.gallery.open_dataset_requested.connect(self.open_dataset)
        self.gallery.force_ai_requested.connect(self.force_ai_requested)
        image_layout.addWidget(self.gallery, 1)
        splitter.addWidget(image_panel)

        decision = QWidget()
        layout = QVBoxLayout(decision)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(8)
        self.queue_position = QLabel("No queue loaded")
        self.queue_position.setObjectName("sectionTitle")
        layout.addWidget(self.queue_position)
        self.image_context = QLabel()
        self.image_context.setWordWrap(True)
        self.image_context.setObjectName("mutedLabel")
        layout.addWidget(self.image_context)

        finding = QGroupBox("Why This Image Was Flagged")
        finding_layout = QVBoxLayout(finding)
        self.match_message = QLabel()
        self.match_message.setWordWrap(True)
        self.match_message.setObjectName("errorLabel")
        finding_layout.addWidget(self.match_message)
        self.owner_evidence = QLabel()
        self.owner_evidence.setWordWrap(True)
        self.owner_evidence.setObjectName("mutedLabel")
        finding_layout.addWidget(self.owner_evidence)
        layout.addWidget(finding)

        authority = QGroupBox("Canonical Rule Authority")
        authority_layout = QVBoxLayout(authority)
        self.rule_authority = QLabel()
        self.rule_authority.setWordWrap(True)
        authority_layout.addWidget(self.rule_authority)
        self.allow_when = QLabel()
        self.allow_when.setWordWrap(True)
        self.allow_when.setObjectName("mutedLabel")
        authority_layout.addWidget(self.allow_when)
        layout.addWidget(authority)

        history = QGroupBox("Existing Image Decision")
        history_layout = QVBoxLayout(history)
        self.decision_history = QLabel()
        self.decision_history.setWordWrap(True)
        self.decision_history.setObjectName("mutedLabel")
        history_layout.addWidget(self.decision_history)
        layout.addWidget(history)
        layout.addStretch(1)

        actions = QHBoxLayout()
        self.undo = QPushButton("Review Saved Changes…")
        self.undo.setToolTip("Compare recorded versions for the exact last conflict decision before restoring selected facts.")
        self.undo.clicked.connect(self.undo_last)
        self.undo.setEnabled(False)
        self.rule_button = QPushButton("Review Canonical Rule…")
        self.rule_button.clicked.connect(self.open_rule)
        self.defer_button = QPushButton("Defer…")
        self.defer_button.clicked.connect(self.defer_current)
        self.reopen_button = QPushButton("Reopen Exception")
        self.reopen_button.clicked.connect(self.reopen_exception)
        self.correct_button = QPushButton("Correct Image Facts…")
        self.correct_button.clicked.connect(self.correct_image_facts)
        self.valid_button = QPushButton("Valid Together on This Image…")
        self.valid_button.setProperty("primary", True)
        self.valid_button.clicked.connect(self.accept_exception)
        for button in (
            self.undo, self.rule_button, self.defer_button, self.reopen_button,
            self.correct_button, self.valid_button,
        ):
            button.setProperty("compact", True)
        actions.addWidget(self.undo)
        actions.addWidget(self.rule_button)
        actions.addStretch(1)
        actions.addWidget(self.defer_button)
        actions.addWidget(self.reopen_button)
        actions.addWidget(self.correct_button)
        actions.addWidget(self.valid_button)
        layout.addLayout(actions)
        nav = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self.move_current(-1))
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self.move_current(1))
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.previous)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next)
        layout.addLayout(nav)
        splitter.addWidget(decision)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)

        close_row = QHBoxLayout()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.request_close)
        close_row.addStretch(1)
        close_row.addWidget(self.close_button)
        root.addLayout(close_row)

    def _apply_scope(self) -> None:
        requested = str(
            self.scope_payload.get("relationship_id")
            or self.scope_payload.get("rule_id")
            or ""
        )
        if requested:
            index = self.rule.findData(requested)
            if index < 0:
                matching = next(
                    (
                        item for item in self.conflicts.rules
                        if requested in item.accepted_ids
                    ),
                    None,
                )
                if matching is not None:
                    index = self.rule.findData(
                        matching.relationship_id or matching.id
                    )
            if index >= 0:
                self.rule.setCurrentIndex(index)
            else:
                self._stale_rule_request = requested
                blocker = QSignalBlocker(self.rule)
                self.rule.insertItem(
                    0,
                    f"Unavailable requested rule — {requested}",
                    "__stale_rule__",
                )
                self.rule.setCurrentIndex(0)
                del blocker
                self.build_queue.setEnabled(False)
                self.status.setText(
                    "The requested canonical conflict rule is no longer active. "
                    "Choose another rule explicitly; the scope was not broadened."
                )
        if self._prefilled_paths:
            self.scope.setCurrentIndex(self.scope.findData("prefilled"))

    def _rule_scope_changed(self, *_args) -> None:
        if self.rule.currentData() == "__stale_rule__":
            self.build_queue.setEnabled(False)
            return
        if not self._stale_rule_request:
            return
        requested = self._stale_rule_request
        self._stale_rule_request = ""
        stale_index = self.rule.findData("__stale_rule__")
        if stale_index >= 0:
            blocker = QSignalBlocker(self.rule)
            self.rule.removeItem(stale_index)
            del blocker
        self.build_queue.setEnabled(True)
        self.status.setText(
            f"The unavailable requested rule {requested!r} was not used. "
            "The newly selected rule will be checked when you build the queue."
        )

    def _scope_paths(self):
        dataset = getattr(self.state, "dataset", None)
        library_paths = tuple(getattr(dataset, "images", ()) or ())
        self._path_scope_error = ""
        if self.scope.currentData() == "prefilled":
            allowed = {Path(value) for value in library_paths}
            if dataset is not None:
                missing = [
                    path for path in self._prefilled_paths if path not in allowed
                ]
                if missing:
                    self._path_scope_error = (
                        f"{len(missing):,} of {len(self._prefilled_paths):,} "
                        "prefilled image(s) are no longer in the current Dataset."
                    )
                    return []
            return list(self._prefilled_paths)
        return list(library_paths)

    def start_scan(self) -> None:
        if self.rule.currentData() == "__stale_rule__":
            QMessageBox.warning(
                self,
                "Requested Rule Is Unavailable",
                "The exact requested canonical rule is no longer active. Choose "
                "another rule explicitly before building a queue.",
            )
            return
        paths = self._scope_paths()
        if self._path_scope_error:
            self.status.setText(
                self._path_scope_error + " Refresh the originating Dataset scope."
            )
            QMessageBox.warning(
                self,
                "Prefilled Scope Changed",
                self._path_scope_error
                + " The complete requested scope was preserved and no scan started.",
            )
            return
        if not paths:
            QMessageBox.information(
                self,
                "Open libraries first",
                "Choose or refresh one or more included image libraries before "
                "building the conflict queue.",
            )
            return
        begin = getattr(self.state, "begin_long_operation", None)
        if callable(begin):
            started, active = begin(
                "conflict_review",
                "Building diagnostic conflict review queue",
                resource="io",
                dedupe_key=(
                    f"{self.rule.currentData()}:{self.scope.currentData()}:"
                    f"{self.include_accepted.isChecked()}:{self.include_deferred.isChecked()}"
                ),
            )
            if not started:
                show_operation_in_progress(self, active)
                return
        self._set_scan_busy(True)
        self.status.setText(
            f"Checking {len(paths):,} image(s) against the selected canonical "
            "diagnostic rule scope…"
        )
        task = CancelableProgressCallTask(
            lambda progress, should_cancel: self.service.build_queue(
                paths,
                rule_id=str(self.rule.currentData() or ""),
                include_accepted=self.include_accepted.isChecked(),
                include_deferred=self.include_deferred.isChecked(),
                max_entries=self.MAX_ENTRIES,
                progress=progress,
                should_cancel=should_cancel,
            )
        )
        self._scan_task = task
        task.signals.progress.connect(self._scan_progress)
        task.signals.complete.connect(self._scan_complete)
        task.signals.failed.connect(self._scan_failed)
        self.pool.start(task)

    def _set_scan_busy(self, busy: bool) -> None:
        for control in (
            self.rule, self.scope, self.include_accepted, self.include_deferred,
            self.build_queue, self.queue, self.previous, self.next, self.defer_button,
            self.reopen_button, self.correct_button, self.valid_button,
            self.rule_button, self.undo, self.gallery,
        ):
            control.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.progress_text.setVisible(busy)
        self.cancel.setVisible(busy)
        self.cancel.setEnabled(busy)
        if busy:
            set_progress_bar(self.progress, unknown_text="Preparing queue…")
            self.progress_text.setText("Preparing queue…")
        elif self.rule.currentData() == "__stale_rule__":
            self.build_queue.setEnabled(False)
        if not busy:
            self.undo.setEnabled(self._history_target is not None)

    def _scan_progress(self, index, total, name) -> None:
        update = getattr(self.state, "update_long_operation", None)
        if callable(update):
            update("conflict_review", index, total, name)
        set_progress_bar(self.progress, index, total, detail=name)
        self.progress_text.setText(f"{index:,} of {total:,}: {name}")

    def cancel_scan(self) -> None:
        if self._scan_task is None or not hasattr(self._scan_task, "cancel"):
            return
        request = getattr(self.state, "request_long_operation_cancel", None)
        if callable(request):
            request("conflict_review")
        self._scan_task.cancel()
        self.cancel.setEnabled(False)
        self.progress_text.setText("Stopping safely after the current image…")

    def _finish_operation(self) -> None:
        finish = getattr(self.state, "finish_long_operation", None)
        if callable(finish):
            finish("conflict_review")

    def _scan_complete(self, result) -> None:
        self._finish_operation()
        self._scan_task = None
        self._set_scan_busy(False)
        self.entries = list(result.entries)
        self.position = 0
        stopped = "Stopped early • " if result.cancelled else ""
        bounded = (
            f" • showing the first {self.MAX_ENTRIES:,}" if result.truncated else ""
        )
        self.status.setText(
            f"{stopped}{result.matching:,} matching conflict finding(s) across "
            f"{result.scanned:,} checked image(s) • {result.existing_exceptions:,} "
            f"accepted exception(s) hidden • {result.deferred:,} deferred hidden • "
            f"{result.errors:,} unavailable or invalid{bounded}."
        )
        self._refresh_queue_rows()
        self._show_current()

        self._notify_background_work_finished()

    def _scan_failed(self, message) -> None:
        self._finish_operation()
        self._scan_task = None
        self._set_scan_busy(False)
        self._show_current()
        self.status.setText(
            "The conflict queue could not be built. Existing Image Knowledge and "
            "canonical rules were unchanged."
        )
        QMessageBox.critical(self, "Could Not Build Conflict Queue", message)
        self._notify_background_work_finished()

    def current_entry(self):
        return self.entries[self.position] if 0 <= self.position < len(self.entries) else None

    @staticmethod
    def _entry_group_key(entry) -> tuple[str, str]:
        return str(entry.image).casefold(), entry.relationship_id

    def _refresh_queue_rows(self) -> None:
        selected = {
            tuple(item.data(0, Qt.UserRole + 1) or ())
            for item in self.queue.selectedItems()
        }
        blocker = QSignalBlocker(self.queue)
        self.queue.clear()
        current_item = None
        for index, entry in enumerate(self.entries):
            owner = entry.owner_label or entry.owner_id or self._scope_label(entry.scope)
            if entry.character_slot is not None:
                owner = f"Character {entry.character_slot} • {owner}"
            elif entry.object_slot is not None:
                owner = f"Object {entry.object_slot} • {owner}"
            state = "Accepted" if entry.accepted else "Deferred" if entry.deferred else "Needs decision"
            item = QTreeWidgetItem((entry.image.name, f"{entry.label} • {owner}", state))
            item.setData(0, Qt.UserRole, index)
            selection_key = (str(entry.image), entry.match_identity)
            item.setData(0, Qt.UserRole + 1, selection_key)
            item.setToolTip(0, str(entry.image))
            item.setToolTip(1, entry.message)
            self.queue.addTopLevelItem(item)
            if selection_key in selected:
                item.setSelected(True)
            if index == self.position:
                current_item = item
        if current_item is not None:
            self.queue.setCurrentItem(
                current_item, 0, QItemSelectionModel.NoUpdate,
            )
            if not self.queue.selectedItems():
                current_item.setSelected(True)
            self.queue.scrollToItem(current_item)
        del blocker
        self._update_selection_actions()

    def _queue_current_changed(self, current, _previous) -> None:
        if current is None:
            return
        index = current.data(0, Qt.UserRole)
        if index is None or not 0 <= int(index) < len(self.entries):
            return
        self.position = int(index)
        self._show_current(sync_queue=False)

    def _queue_selection_changed(self) -> None:
        self._update_selection_actions()

    def selected_entries(self) -> tuple:
        selected = []
        seen = set()
        for item in self.queue.selectedItems():
            index = item.data(0, Qt.UserRole)
            if index is None or not 0 <= int(index) < len(self.entries):
                continue
            entry = self.entries[int(index)]
            key = self._entry_group_key(entry)
            if key in seen:
                continue
            seen.add(key)
            selected.append(entry)
        if not selected and self.current_entry() is not None:
            selected.append(self.current_entry())
        return tuple(selected)

    def _selection_state(self):
        selected = self.selected_entries()
        if not selected:
            return selected, "Select one or more queue findings."
        if len({entry.relationship_id for entry in selected}) != 1:
            return selected, "Selected findings use different canonical rules."
        if len({entry.accepted for entry in selected}) != 1:
            return selected, "Selected findings mix accepted and active decisions."
        if not all(self._writable(entry.image) for entry in selected):
            return selected, "At least one selected image is unavailable or read-only."
        if any(entry.image in self._pending_history_images for entry in selected):
            return selected, "Review saved-history recovery before another decision."
        return selected, ""

    def _update_selection_actions(self) -> None:
        selected, blocker = self._selection_state()
        count = len(selected)
        if not count:
            self.selection_summary.setText("No conflict finding selected")
            return
        rows = len(self.queue.selectedItems()) or 1
        summary = (
            f"{rows:,} finding{'s' if rows != 1 else ''} selected • "
            f"{count:,} image decision{'s' if count != 1 else ''}"
        )
        if blocker:
            summary += f" • {blocker}"
        elif count > 1:
            summary += " • one compatible canonical rule and decision state"
        self.selection_summary.setText(summary)
        entry = self.current_entry()
        accepted = bool(selected and selected[0].accepted)
        compatible = not blocker
        self.rule_button.setEnabled(
            entry is not None
            and len({item.relationship_id for item in selected}) == 1
        )
        self.defer_button.setVisible(not accepted)
        self.defer_button.setEnabled(compatible and not accepted)
        self.reopen_button.setVisible(accepted)
        self.reopen_button.setEnabled(compatible and accepted)
        recovering = count == 1 and selected[0].image in self._pending_history_images
        self.correct_button.setEnabled((compatible or recovering) and count == 1)
        self.correct_button.setText("Review Image Recovery…" if recovering else "Correct Image Facts…")
        self.valid_button.setVisible(not accepted)
        self.valid_button.setEnabled(compatible and not accepted)
        self.defer_button.setText(
            f"Defer Selected… ({count:,})" if count > 1 else "Defer…"
        )
        self.reopen_button.setText(
            f"Reopen Selected Exceptions… ({count:,})"
            if count > 1 else "Reopen Exception"
        )
        self.valid_button.setText(
            f"Valid Together on Selected… ({count:,})"
            if count > 1 else "Valid Together on This Image…"
        )
        self.correct_button.setToolTip(
            "Correct Image Facts opens one image at a time."
            if count > 1 else "Open the universal editor for this image."
        )

    @staticmethod
    def _same_rule_group(first, second) -> bool:
        return (
            first.image == second.image
            and first.relationship_id == second.relationship_id
        )

    def _replace_rule_group(self, previous, replacements, *, insertion=None) -> None:
        replacements = list(replacements)
        indices = [
            index for index, item in enumerate(self.entries)
            if self._same_rule_group(item, previous)
        ]
        insert_at = (
            indices[0] if indices
            else min(max(0, int(insertion or 0)), len(self.entries))
        )
        self.entries = [
            item for item in self.entries
            if not self._same_rule_group(item, previous)
        ]
        for offset, item in enumerate(replacements):
            self.entries.insert(insert_at + offset, item)
        preferred = next(
            (
                offset for offset, item in enumerate(replacements)
                if item.match_identity == previous.match_identity
            ),
            0,
        )
        self.position = (
            min(insert_at + preferred, len(self.entries) - 1)
            if self.entries else 0
        )

    @staticmethod
    def _scope_label(scope: str) -> str:
        return {
            "image": "Image-wide",
            "same_character": "Same subject / character",
            "same_object": "Same owned object / assignment",
        }.get(scope, str(scope).replace("_", " ").title())

    def _writable(self, image: Path) -> bool:
        dataset = getattr(self.state, "dataset", None)
        try:
            if dataset is not None and hasattr(dataset, "manager_for_path"):
                if dataset.manager_for_path(image) is not None:
                    return bool(dataset.storage_availability(image).ready_for_write)
            return image.is_file()
        except OSError:
            return False

    def _show_current(self, *, sync_queue: bool = True) -> None:
        entry = self.current_entry()
        total = len(self.entries)
        if entry is None:
            self.queue_position.setText("No queue item selected")
            self.image_context.setText("Build a queue or choose another rule and scope.")
            self.match_message.setText("No diagnostic finding is active.")
            self.owner_evidence.clear()
            self.rule_authority.clear()
            self.allow_when.clear()
            self.decision_history.setText("No image decision is active.")
            self.gallery.set_unavailable(
                "No image is active in this conflict queue.", context="Current image",
            )
            self.gallery.status.clear()
            self.page_label.setText("0 of 0")
            self.previous.setEnabled(False)
            self.next.setEnabled(False)
            for button in (
                self.rule_button, self.defer_button, self.reopen_button,
                self.correct_button, self.valid_button,
            ):
                button.setEnabled(False)
            self.selection_summary.setText("No conflict finding selected")
            return
        if sync_queue:
            item = self.queue.topLevelItem(self.position)
            if item is not None:
                blocker = QSignalBlocker(self.queue)
                self.queue.setCurrentItem(item, 0, QItemSelectionModel.NoUpdate)
                if not self.queue.selectedItems():
                    item.setSelected(True)
                self.queue.scrollToItem(item)
                del blocker
        page = self.position // self.PAGE_SIZE + 1
        pages = max(1, (total - 1) // self.PAGE_SIZE + 1)
        self.queue_position.setText(
            f"Finding {self.position + 1:,} of {total:,} • {entry.label}"
        )
        self.image_context.setText(
            f"{entry.image.name} • Queue page {page:,} of {pages:,} "
            f"(up to {self.PAGE_SIZE:,} decisions per page)"
        )
        self.gallery.set_paths([entry.image], total_count=1, context="Current conflict")
        self.match_message.setText(entry.message)
        owner = entry.owner_label or entry.owner_id or "No exact owner assignment"
        if entry.character_slot is not None:
            owner = f"Character slot {entry.character_slot} • {owner}"
        elif entry.object_slot is not None:
            owner = f"Object slot {entry.object_slot} • {owner}"
        elif entry.context_label:
            owner = f"Context: {entry.context_label} • {owner}"
        ambiguity = (
            "Ownership is unresolved; this remains an ambiguous review finding."
            if entry.ambiguous else "The displayed owner/scope evidence is confirmed."
        )
        self.owner_evidence.setText(
            f"Owner evidence: {owner}\n{ambiguity}\n"
            f"Exact involved SDXL Tags: {', '.join(entry.found_tags)}"
        )
        self.rule_authority.setText(
            f"{entry.label} • {self._scope_label(entry.scope)} • version "
            f"{entry.rule_version}\nStable relationship ID: {entry.relationship_id}\n"
            f"Provenance: {entry.provenance}"
        )
        self.allow_when.setText(f"Allow when: {entry.allow_when}")
        if entry.accepted:
            reason = entry.accepted_reason or "Legacy exception: no reason was recorded."
            history = f"Accepted exception • {reason}"
        elif entry.deferred:
            history = "Deferred" + (
                f" • {entry.deferred_reason}" if entry.deferred_reason else ""
            )
        else:
            history = "No accepted exception or deferred decision is saved."
        if entry.image in self._pending_history_images:
            history = (
                "Saving this decision was interrupted. The displayed finding predates that save. "
                "Review Saved Changes or open Image Recovery for this exact image, then reopen the queue."
            )
        self.decision_history.setText(history)
        writable = self._writable(entry.image)
        if not writable:
            self.decision_history.setText(
                history + "\nThis image is unavailable, conflicted, or read-only; "
                "restore writable access before changing its Image Knowledge."
            )
        self.page_label.setText(f"{self.position + 1:,} of {total:,}")
        self.previous.setEnabled(self.position > 0)
        self.next.setEnabled(self.position + 1 < total)
        self._update_selection_actions()

    def _decision_complete(self, result, entries) -> None:
        entries = tuple(entries)
        if not entries:
            return
        entry = entries[0]
        self._last_result = result
        self._last_entry = entry
        self._last_entries = entries
        self._last_position = self.position
        operation_id = getattr(result, "history_operation_id", "") or getattr(result, "operation_id", "")
        if operation_id:
            self._history_target = (tuple(dict.fromkeys(item.image for item in entries)), operation_id)
        self.undo.setEnabled(self._history_target is not None)
        outcomes = tuple(getattr(result, "items", ()))
        if outcomes:
            self._pending_history_images.update(item.image for item in outcomes if item.status == "interrupted")
            completed = {item.image for item in outcomes if item.status == "complete"}
            entries = tuple(item for item in entries if item.image in completed)
        dataset = getattr(self.state, "dataset", None)
        events = getattr(self.state, "events", None)
        refresh_errors = []
        for selected in entries:
            if dataset is not None and getattr(dataset, "index", None):
                try:
                    dataset.cache_managed_record(selected.image, ImageRecord(selected.image))
                except Exception as exc:
                    refresh_errors.append(str(exc))
            if events is not None:
                events.publish("image_data_changed", image=str(selected.image))
        if events is not None and entries:
            events.publish("reviews_changed")
        if entries:
            self.data_changed.emit()
        targets = {self._entry_group_key(item) for item in entries}
        self.entries = [
            item for item in self.entries
            if self._entry_group_key(item) not in targets
        ]
        self.position = min(self._last_position, max(0, len(self.entries) - 1))
        self._refresh_queue_rows()
        self._show_current()
        if outcomes:
            incomplete = [item for item in outcomes if item.status != "complete"]
            self.status.setText(
                f"Saved {len(entries):,} image decision(s). " + (
                    f"{len(incomplete):,} remain. {incomplete[0].error} "
                    "Use Review Saved Changes for the exact results and any recovery needed."
                    if incomplete else "Review Saved Changes to compare or selectively restore this operation."
                )
            )
        if refresh_errors:
            self.status.setText(self.status.text() + " Saved decisions are retained; library refresh needs attention: " + refresh_errors[0])

    def _apply(self, callback, entries=None) -> None:
        if self._scan_task is not None:
            return
        entries = tuple(entries or self.selected_entries())
        if not entries:
            return
        try:
            operation = self._history_operation_factory(tuple(dict.fromkeys(item.image for item in entries))) if self._history_operation_factory else None
        except Exception as exc:
            QMessageBox.warning(self, "Review Saved History Configuration", str(exc))
            return
        if operation is not None:
            begin = getattr(self.state, "begin_long_operation", None)
            if callable(begin):
                started, active = begin("conflict_review", "Saving reviewed conflict decisions", resource="io")
                if not started:
                    show_operation_in_progress(self, active)
                    return
            self._history_target = (tuple(dict.fromkeys(item.image for item in entries)), operation.operation_id)
            task = CancelableProgressCallTask(lambda progress, cancelled: callback(
                entries, history_operation=operation, should_cancel=cancelled, progress=progress,
            ))
            self._scan_task = task
            self._set_scan_busy(True)
            self.progress_text.setText("Saving reviewed decisions; completed images remain saved if stopped…")
            task.signals.progress.connect(self._scan_progress)
            task.signals.complete.connect(lambda result: self._apply_complete(result, entries))
            task.signals.failed.connect(self._apply_failed)
            task.signals.cancelled.connect(lambda: self._apply_failed("Stopped before the decision completed. Review the exact saved changes before retrying."))
            self.pool.start(task)
            return
        try:
            result = callback(entries)
        except ConflictReviewStaleError as exc:
            QMessageBox.warning(self, "Refresh This Conflict", str(exc))
            self.refresh_current()
            return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could Not Complete Conflict Decision",
                str(exc),
            )
            return
        self._decision_complete(result, entries)

    def _apply_complete(self, result, entries):
        self._finish_operation()
        self._scan_task = None
        self._set_scan_busy(False)
        try:
            self._decision_complete(result, entries)
        finally:
            self._notify_background_work_finished()

    def _apply_failed(self, message):
        self._finish_operation()
        self._scan_task = None
        self._set_scan_busy(False)
        self._show_current()
        self.status.setText(f"Decision stopped. {message} Use Review Saved Changes before retrying.")
        self._notify_background_work_finished()

    def accept_exception(self) -> None:
        entries, blocker = self._selection_state()
        if not entries or blocker:
            return
        count = len(entries)
        reason, accepted = QInputDialog.getMultiLineText(
            self,
            "Why Are These Values Valid Together?",
            (
                f"Record one reviewed reason for {count:,} selected image"
                f"{'s' if count != 1 else ''}. This writes only Image Knowledge "
                "and does not weaken or edit the canonical rule:"
            ),
        )
        if not accepted:
            return
        self._apply(
            lambda selected, **options: self.service.accept_exceptions(selected, reason, **options),
            entries,
        )

    def defer_current(self) -> None:
        entries, blocker = self._selection_state()
        if not entries or blocker:
            return
        count = len(entries)
        reason, accepted = QInputDialog.getMultiLineText(
            self,
            "Defer Selected Conflicts",
            f"Optional note for the later review of {count:,} selected image"
            f"{'s' if count != 1 else ''}:",
        )
        if accepted:
            self._apply(
                lambda selected, **options: self.service.defer_many(selected, reason, **options),
                entries,
            )

    def reopen_exception(self) -> None:
        entries, blocker = self._selection_state()
        if not entries or blocker or not all(entry.accepted for entry in entries):
            return
        count = len(entries)
        if QMessageBox.question(
            self,
            "Reopen Selected Exceptions?",
            f"Remove the saved per-image exception from {count:,} selected image"
            f"{'s' if count != 1 else ''} and return this exact conflict to active "
            "review? No SDXL Tag or external file will be changed.",
        ) == QMessageBox.Yes:
            self._apply(self.service.clear_exceptions, entries)

    def correct_image_facts(self, *_args) -> None:
        entries = self.selected_entries()
        if len(entries) > 1:
            QMessageBox.information(
                self,
                "Correct One Image at a Time",
                "The universal editor preserves one exact image authority. "
                "Choose one queue finding before correcting Image Knowledge.",
            )
            return
        entry = entries[0] if entries else self.current_entry()
        if entry is None:
            return
        self.edit_image_data.emit(str(entry.image))
        self.refresh_current(advance_if_resolved=True)

    def open_dataset(self) -> None:
        entry = self.current_entry()
        if entry is not None:
            self.open_dataset_image.emit(str(entry.image))

    def open_rule(self) -> None:
        entry = self.current_entry()
        selected = self.selected_entries()
        if selected and entry not in selected:
            entry = selected[0]
        if entry is None:
            return
        self.open_automation.emit(
            "conflicts.guided_checks",
            {
                "relationship_id": entry.relationship_id,
                "rule_id": entry.rule_id,
                "origin": "review_center",
                "open_editor": True,
            },
        )
        self.accept()

    def refresh_current(self, *, advance_if_resolved: bool = False) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        try:
            replacements = [
                item for item in self.service.entries_for_image(
                    entry.image, rule_id=entry.rule_id,
                )
                if (self.include_accepted.isChecked() or not item.accepted)
                and (self.include_deferred.isChecked() or not item.deferred)
            ]
        except Exception as exc:
            QMessageBox.warning(self, "Could Not Refresh Conflict", str(exc))
            return
        self._replace_rule_group(entry, replacements)
        self._refresh_queue_rows()
        self._show_current()

    def move_current(self, amount: int) -> None:
        if not self.entries:
            return
        self.position = min(
            len(self.entries) - 1,
            max(0, self.position + int(amount)),
        )
        item = self.queue.topLevelItem(self.position)
        if item is not None:
            blocker = QSignalBlocker(self.queue)
            self.queue.clearSelection()
            self.queue.setCurrentItem(item)
            item.setSelected(True)
            del blocker
        self._show_current(sync_queue=False)

    def undo_last(self) -> None:
        if self._scan_task is not None:
            return
        if self._history_target is None or self._history_opener is None:
            QMessageBox.information(self, "Saved Changes Unavailable",
                "There is no proven saved-history route for this decision. Older untracked "
                "changes cannot be restored from an in-memory snapshot.")
            return
        images, operation_id = self._history_target
        try:
            opened = self._history_opener(images, operation_id)
        except Exception as exc:
            QMessageBox.warning(self, "Could Not Open Saved Changes", str(exc))
            return
        if opened is not None:
            self.accept()

    def _notify_background_work_finished(self):
        if self._scan_task is None:
            if self._close_requested:
                super().reject()
            self.background_work_finished.emit()

    def reject(self) -> None:
        if self._scan_task is not None:
            self._close_requested = True
            self.cancel_scan()
            return
        super().reject()

    def request_close(self) -> None:
        self.reject()
