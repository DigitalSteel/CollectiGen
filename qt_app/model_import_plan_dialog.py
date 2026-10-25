"""One explicit, resumable import review over canonical model operation owners."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QPlainTextEdit, QPushButton, QSplitter, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.model_import_plan import ModelImportPlanService
from core.model_import_receipts import ImportReceiptStore
from core.model_resource_registry import ModelResourceError
from .components import DisclosureButton


STEP_LABELS = {
    "registration": "Register selected files", "move": "Organize model file",
    "acquisition": "Download exact model", "companions": "Verify required resources",
    "manual_cache": "Prepare Fast copies", "paths": "Update SwarmUI folders",
}
INTERNAL_EFFECT_FIELDS = frozenset({"policy_digest", "layout_digest", "file_state", "entry_id",
    "fingerprint", "request_fingerprint", "inventory_fingerprint", "catalog_token", "registry_token"})


class SavedModelImportPicker(QDialog):
    """One visible choice over validated receipt summaries, with expert browse."""

    def __init__(self, summaries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Continue a Saved Model Import")
        self.resize(900, 460)
        self.selected_approval_id = ""
        self.browse_requested = False
        body = QVBoxLayout(self)
        intro = QLabel(
            "Choose a saved import to review its recorded work. Opening it does not "
            "connect to SwarmUI, inspect model files, or resume any step."
        )
        intro.setWordWrap(True)
        body.addWidget(intro)
        self.imports = QTreeWidget()
        self.imports.setHeaderLabels(("SwarmUI source", "Recorded progress", "Saved"))
        self.imports.setRootIsDecorated(False)
        self.imports.setAccessibleName("Saved model imports")
        self.imports.setColumnWidth(0, 330)
        self.imports.setColumnWidth(1, 280)
        for summary in summaries:
            try:
                saved = datetime.fromtimestamp(summary["saved_ns"] / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
            except (OverflowError, OSError, ValueError):
                saved = "Time unavailable"
            item = QTreeWidgetItem((summary["source"], summary["status"], saved))
            item.setData(0, Qt.UserRole, summary["approval_id"])
            if summary["error"]:
                item.setToolTip(1, summary["error"])
            self.imports.addTopLevelItem(item)
        body.addWidget(self.imports, 1)
        self.empty = QLabel("No saved imports were found for this Models registry.")
        self.empty.setVisible(not summaries)
        body.addWidget(self.empty)
        row = QHBoxLayout()
        self.browse = QPushButton("Browse Exact Plan…")
        self.browse.setToolTip("Advanced: choose an exact saved import file from this Models registry.")
        self.browse.clicked.connect(self._browse)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.reject)
        self.open_selected = QPushButton("Review Selected Import")
        self.open_selected.setProperty("primary", True)
        self.open_selected.clicked.connect(self._open_selected)
        self.open_selected.setEnabled(False)
        self.imports.itemSelectionChanged.connect(
            lambda: self.open_selected.setEnabled(bool(self.imports.selectedItems()))
        )
        self.imports.itemDoubleClicked.connect(lambda *_: self._open_selected())
        row.addWidget(self.browse); row.addStretch(1)
        row.addWidget(self.cancel); row.addWidget(self.open_selected)
        body.addLayout(row)

    def _browse(self):
        self.browse_requested = True
        self.accept()

    def _open_selected(self):
        selected = self.imports.selectedItems()
        if selected:
            self.selected_approval_id = str(selected[0].data(0, Qt.UserRole) or "")
            self.accept()


class SplitModelImportChoiceDialog(QDialog):
    """Page a reviewed selection; checked rows become one separate choice."""

    PAGE_SIZE = 100

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Split Selected Model Files")
        self.setWindowModality(Qt.WindowModal)
        self.rows = tuple(rows)
        self.resize(1000, 450 if len(self.rows) <= 10 else 690)
        self.new_paths = set()
        self._page = 0
        body = QVBoxLayout(self)
        intro = QLabel("Check the exact files to place in a new import choice. Unchecked files stay in the original choice. "
            "Nothing is registered or moved here; Cancel keeps the original choice unchanged.")
        intro.setWordWrap(True); body.addWidget(intro)
        self.count = QLabel(); body.addWidget(self.count)
        self.files = QTreeWidget(); self.files.setRootIsDecorated(False)
        self.files.setHeaderLabels(("Selected file", "Model type", "Family"))
        self.files.setColumnWidth(0, 650); self.files.setColumnWidth(1, 140)
        self.files.setAccessibleName("Reviewed files to split into separate import choices")
        self.files.itemChanged.connect(self._changed)
        body.addWidget(self.files, 1)
        self.details = QLabel("Select a file to see its complete path and identity.")
        self.details.setWordWrap(True); self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.files.currentItemChanged.connect(lambda item, _previous: self.details.setText(
            f"Full path: {item.data(0, Qt.UserRole)}\nExact identity: {item.data(0, Qt.UserRole + 1)}" if item else
            "Select a file to see its complete path and identity."))
        body.addWidget(self.details)
        paging = QHBoxLayout()
        self.previous = QPushButton("Previous Files"); self.following = QPushButton("Next Files")
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.following.clicked.connect(lambda: self._change_page(1))
        self.page_count = QLabel()
        for widget in (self.previous, self.following, self.page_count): paging.addWidget(widget)
        paging.addStretch(1); body.addLayout(paging)
        actions = QHBoxLayout()
        self.cancel = QPushButton("Cancel"); self.cancel.clicked.connect(self.reject)
        self.confirm = QPushButton("Create Separate Choice"); self.confirm.setProperty("primary", True)
        self.confirm.clicked.connect(self.accept)
        actions.addStretch(1); actions.addWidget(self.cancel); actions.addWidget(self.confirm)
        body.addLayout(actions)
        self._render()

    def _change_page(self, delta):
        self._page = max(0, self._page + delta)
        self._render()

    def _changed(self, item, column):
        if column != 0: return
        path = str(item.data(0, Qt.UserRole))
        if item.checkState(0) == Qt.Checked: self.new_paths.add(path)
        else: self.new_paths.discard(path)
        self._update_count()

    def _update_count(self):
        moved = len(self.new_paths); total = len(self.rows)
        original_label = "file" if total - moved == 1 else "files"
        new_label = "file" if moved == 1 else "files"
        self.count.setText(f"Original choice: {total - moved:,} {original_label} · New choice: {moved:,} {new_label}. "
            "Choose at least one file for each choice.")
        self.confirm.setEnabled(0 < moved < total)

    def _render(self):
        start = self._page * self.PAGE_SIZE
        with QSignalBlocker(self.files):
            self.files.clear()
            for row in self.rows[start:start + self.PAGE_SIZE]:
                path = str(row["path"])
                item = QTreeWidgetItem((Path(path).name, row["role"], row["family"]))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setData(0, Qt.UserRole, path)
                item.setData(0, Qt.UserRole + 1, row["resource_id"])
                item.setToolTip(0, path)
                item.setCheckState(0, Qt.Checked if path in self.new_paths else Qt.Unchecked)
                self.files.addTopLevelItem(item)
        total = len(self.rows)
        self.page_count.setText(f"Files {start + 1:,}–{min(start + self.PAGE_SIZE, total):,} of {total:,}")
        self.previous.setEnabled(self._page > 0)
        self.following.setEnabled(start + self.PAGE_SIZE < total)
        self._update_count()


def _text(value):
    return str(value).replace("_", " ").capitalize()


def _lines(value, prefix=""):
    """Readable exact values, including every file and cleanup victim; no JSON UI."""
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in INTERNAL_EFFECT_FIELDS: continue
            label = prefix + _text(key)
            if isinstance(item, (dict, list, tuple)):
                lines.append(label + ":")
                lines.extend(_lines(item, prefix + "  "))
            else:
                lines.append(f"{label}: {item}")
    elif isinstance(value, (list, tuple)):
        if not value: lines.append(prefix + "None")
        for index, item in enumerate(value, 1):
            if isinstance(item, (dict, list, tuple)):
                lines.append(prefix + str(index) + ".")
                lines.extend(_lines(item, prefix + "  "))
            else: lines.append(prefix + str(item))
    else: lines.append(prefix + str(value))
    return lines


class ModelImportPlanDialog(QDialog):
    changed = Signal(object)
    completed = Signal(object)
    setup_requested = Signal()

    def __init__(self, registry, jobs, parent=None, *, settings=None, protected_roots=(),
                 environment=None, environment_factory=None, open_fast_recovery=None):
        super().__init__(parent)
        self.registry, self.jobs, self.settings = registry, jobs, settings
        self.protected_roots = tuple(protected_roots)
        self.environment, self.environment_factory = environment, environment_factory
        self._open_fast_recovery = open_fast_recovery
        self._injected_environment = environment is not None
        self._service = self._store = None
        self._inventory = self._source_binding = None
        self._choices = []
        self._companion_requests = []
        self._path_choice = None
        self._exposed_resources = ()
        self._plan = self._review = self._approval_id = None
        self._displayed_revision = self._revision_review = None
        self._saved_step_states = {}
        self._approved_plan = None
        self._pending_revision = False
        self._sharing_omission_step = None
        self._task = self._child = None
        self._folder_editor_open = False
        self._storage_mode = "single"
        self._closing = self._closed = self.has_changed = False
        self._cancelled_delivery = False
        self._close_parent = None
        self.setWindowTitle("Import Models from SwarmUI")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1220, 390)
        body = QVBoxLayout(self)
        body.addWidget(self._label("Choose models from SwarmUI, then review the complete import. "
            "If SwarmUI is not connected, open its Setup below and return here. "
            "Registration records existing files. Optional organization moves them; Fast preparation keeps Archive originals."))
        row = QHBoxLayout()
        self.read_source = self._button("Read Source / Folders", self._read_source)
        self.choose_files = self._button("Choose Files…", self._choose_files)
        self.choose_swarm_folder = self._button("Choose SwarmUI Folder…", self._choose_swarm_folder)
        self.add_download = self._button("Add Download…", self._add_download)
        self.open_saved = self._button("Continue Saved Import…", self._open_saved)
        for button in (self.read_source, self.choose_swarm_folder, self.choose_files): row.addWidget(button)
        row.addStretch(1); row.addWidget(self.open_saved); body.addLayout(row)
        source_row = QHBoxLayout()
        self.source = self._label("Source not read. Opening this window does not connect, scan, or start an import.")
        source_row.addWidget(self.source, 1)
        self.open_setup = self._button("Connect or Review SwarmUI Setup…", self._request_setup)
        self.open_setup.setToolTip(
            "Open the existing SwarmUI connection controls in Setup. Return to this import "
            "and explicitly read its folders again; nothing is connected or imported automatically."
        )
        source_row.addWidget(self.open_setup)
        body.addLayout(source_row)
        self.start_hint = self._label("Start here: read the source, choose an existing SwarmUI folder or exact files, "
            "then review what will happen before applying. Nothing is changed by opening this window.")
        body.addWidget(self.start_hint)
        self.splitter = QSplitter(Qt.Vertical)
        self.selection_panel = QWidget(); selection_layout = QVBoxLayout(self.selection_panel)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        self.choices = QTreeWidget(); self.choices.setRootIsDecorated(False)
        self.choices.setHeaderLabels(("Chosen files or downloads", "Model type / family", "Scope / media"))
        self.choices.setColumnWidth(0, 580); self.choices.setColumnWidth(1, 270)
        self.choices.setAccessibleName("Files and downloads included in this import")
        self.choices.itemSelectionChanged.connect(self._update_actions)
        selection_layout.addWidget(self.choices)
        row = QHBoxLayout()
        self.required_resources = self._button("Required Resources…", self._required_resources)
        self.split_choice = self._button("Split Selected Choice…", self._split_choice)
        self.review_choice = self._button("Review Selected Files…", self._review_choice)
        self.remove_choice = self._button("Remove Selected Choice", self._remove_choice)
        row.addWidget(self.required_resources); row.addWidget(self.review_choice); row.addWidget(self.split_choice)
        row.addWidget(self.remove_choice); row.addStretch(1)
        selection_layout.addLayout(row); self.splitter.addWidget(self.selection_panel)
        review_widget = QWidget(); review_layout = QVBoxLayout(review_widget); review_layout.setContentsMargins(0, 0, 0, 0)
        self.steps = QTreeWidget(); self.steps.setRootIsDecorated(False)
        self.steps.setHeaderLabels(("Import step", "Status", "Consequence / next action"))
        self.steps.setColumnWidth(0, 260); self.steps.setColumnWidth(1, 145)
        self.steps.setAccessibleName("Reviewed import steps and their current status")
        self.steps.itemSelectionChanged.connect(self._show_step)
        review_layout.addWidget(self.steps, 1)
        self.selected_step_summary = self._label("Select an import step to review its consequence and next action.")
        self.selected_step_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.selected_step_summary.setAccessibleName("Selected import step summary")
        review_layout.addWidget(self.selected_step_summary)
        self.exact_panel = QWidget()
        exact_layout = QVBoxLayout(self.exact_panel); exact_layout.setContentsMargins(0, 0, 0, 0)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setMaximumHeight(220)
        self.details.setAccessibleName("Exact selected import step consequences")
        self.details.setPlaceholderText("Review the combined plan, then select a step to see every exact path and consequence.")
        exact_layout.addWidget(self.details)
        self.saved_location = self._label(""); self.saved_location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        exact_layout.addWidget(self.saved_location)
        self.show_exact_details = DisclosureButton("Show Exact Files and Consequences",
            "Hide Exact Files and Consequences", self.exact_panel)
        self.show_exact_details.setToolTip("Inspect complete file paths, resource identities, move destinations and saved receipts for the selected step.")
        review_layout.addWidget(self.show_exact_details, 0, Qt.AlignLeft)
        review_layout.addWidget(self.exact_panel); self.splitter.addWidget(review_widget)
        self.splitter.setSizes([180, 310]); body.addWidget(self.splitter, 1)
        self.optional_toggle = QToolButton()
        self.optional_toggle.setText("More options: downloads, organization, Fast copies")
        self.optional_toggle.setCheckable(True)
        self.optional_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.optional_toggle.setArrowType(Qt.RightArrow)
        body.addWidget(self.optional_toggle, 0, Qt.AlignLeft)
        self.optional_panel = QWidget()
        optional = QVBoxLayout(self.optional_panel)
        optional.setContentsMargins(0, 0, 0, 0)
        self.optional_toggle.toggled.connect(lambda checked: (
            self.optional_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow),
            self.optional_panel.setVisible(checked)))
        row = QHBoxLayout()
        row.addWidget(self.add_download)
        self.folder_locations = self._button("Folder Locations…", self._folder_locations)
        self.review_folders = self._button("Review SwarmUI Folder Changes…", self._review_folders)
        row.addWidget(self.folder_locations); row.addWidget(self.review_folders); row.addStretch(1)
        optional.addLayout(row)
        self.organize = QCheckBox("Organize files using Folder Locations")
        self.prepare_fast = QCheckBox("Prepare Fast copies for external use")
        self.share_fast = QCheckBox("Share prepared Fast folders with SwarmUI")
        for checkbox in (self.organize, self.prepare_fast, self.share_fast):
            optional.addWidget(checkbox); checkbox.toggled.connect(self._options_changed)
        media_row = QHBoxLayout()
        self.media_label = QLabel("Selected choice media for optional organization / Fast copies")
        self.media_type = QComboBox(); self.media_type.setAccessibleName("Media type for selected import choice")
        self.media_type.addItem("Choose Images or Video…", None)
        self.media_type.addItem("Images", "Images")
        self.media_type.addItem("Video", "Video")
        self.media_type.setToolTip("Choose media for this selected choice. Custom family names never set media automatically.")
        self.media_type.currentIndexChanged.connect(self._media_changed)
        media_row.addWidget(self.media_label); media_row.addWidget(self.media_type); media_row.addStretch(1)
        optional.addLayout(media_row)
        self.organization_readiness = self._label("")
        optional.addWidget(self.organization_readiness)
        self.consequences = self._label("Moves retire the original file only after verified publication. Downloads never replace existing files. "
            "Sharing folders changes SwarmUI settings. Fast copies stay protected until you explicitly finish external use in Models. "
            "Completed steps remain recorded if later work stops; there is no whole-import rollback.")
        optional.addWidget(self.consequences)
        body.addWidget(self.optional_panel)
        self.optional_panel.hide()
        self.summary = self._label("Read your source, then choose files or exact downloads."); body.addWidget(self.summary)
        self.status = self._label("Nothing has started."); body.addWidget(self.status)
        self.omit_saved_sharing = QCheckBox("Leave SwarmUI folder settings unchanged in the corrected import")
        self.omit_saved_sharing.setToolTip("Omit only the unstarted optional folder-sharing step. Existing SwarmUI settings stay as they are; no folder changes are applied.")
        self.omit_saved_sharing.toggled.connect(self._update_actions)
        body.addWidget(self.omit_saved_sharing)
        row = QHBoxLayout()
        self.review_plan = self._button("Review Combined Plan", self._review_plan)
        self.review_remaining = self._button("Review Remaining Work", self._review_remaining)
        self.correct_saved_files = self._button("Review Selected Saved Files…", self._review_saved_files)
        self.review_fast_recovery = self._button("Review Interrupted Fast Operation…", self._review_fast_recovery)
        self.review_fast_recovery.setToolTip("Open only this saved import's exact bound Fast operation in native Storage Recovery. Nothing resumes automatically.")
        self.apply_plan = self._button("Apply Reviewed Plan", self._apply_plan); self.apply_plan.setProperty("primary", True)
        self.resume = self._button("Resume Saved Import", self._resume)
        self.stop = self._button("Cancel Safely", self._stop)
        self.close_button = self._button("Back to Models", self.reject)
        for button in (self.review_plan, self.review_remaining, self.correct_saved_files,
                       self.review_fast_recovery, self.apply_plan, self.resume, self.stop): row.addWidget(button)
        row.addStretch(1); row.addWidget(self.close_button); body.addLayout(row)
        for button in self.findChildren(QPushButton): button.setAutoDefault(False)
        self._update_actions()

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText); return label

    @staticmethod
    def _button(text, callback):
        button = QPushButton(text); button.clicked.connect(callback); return button

    def _idle(self):
        return not self.has_active_background_work() and not self._closing

    def _update_actions(self, *_):
        if not hasattr(self, "close_button"): return
        has_review_content = bool(self._choices) or self._plan is not None
        if self.splitter.isHidden() == has_review_content:
            self.splitter.setVisible(has_review_content)
            self.resize(self.width(), 650 if has_review_content else 390)
        saved_without_draft = self._approval_id is not None or (self._plan is not None and not self._choices)
        self.selection_panel.setVisible(has_review_content and not saved_without_draft)
        self.start_hint.setVisible(not has_review_content)
        self.summary.setVisible(has_review_content)
        idle = self._idle()
        editing = idle and self._approval_id is None
        selected = self.choices.currentItem() is not None
        media_visible = self.organize.isChecked() or self.prepare_fast.isChecked()
        self.media_label.setVisible(media_visible); self.media_type.setVisible(media_visible)
        self.media_type.setEnabled(editing and selected)
        with QSignalBlocker(self.media_type):
            selected_media = self._choices[self.choices.currentItem().data(0, Qt.UserRole)].get("media_type") if selected else None
            self.media_type.setCurrentIndex(self.media_type.findData(selected_media))
        organization_ready = self._organization_status() if media_visible else True
        self.organization_readiness.setVisible(media_visible)
        self.open_saved.setEnabled(idle)
        self.open_setup.setEnabled(idle and self._approval_id is None)
        for widget in (self.read_source, self.organize, self.prepare_fast): widget.setEnabled(editing)
        self.folder_locations.setEnabled(idle)
        self.review_folders.setEnabled(editing and self._inventory is not None and self._storage_mode == "single")
        self.review_folders.setToolTip("Use Share prepared Fast folders with SwarmUI while tiered storage is enabled."
            if self._storage_mode == "tiered" else "Choose exact folder changes to include in the combined approval.")
        self.share_fast.setEnabled(editing and self.prepare_fast.isChecked() and self._inventory is not None)
        for widget in (self.choose_swarm_folder, self.choose_files, self.add_download): widget.setEnabled(editing and self._inventory is not None)
        self.choices.setEnabled(editing)
        self.required_resources.setEnabled(editing and selected)
        selected_choice = self._choices[self.choices.currentItem().data(0, Qt.UserRole)] if selected else None
        selected_review = selected_choice.get("review") if selected_choice and selected_choice.get("kind") == "registration" else None
        selected_count = len(selected_review.selected_paths if hasattr(selected_review, "selected_paths") else selected_review["selected_paths"]) if selected_review is not None else 0
        self.review_choice.setVisible(editing and selected_review is not None)
        self.review_choice.setEnabled(editing and selected_review is not None)
        self.split_choice.setVisible(editing and selected_count > 1)
        self.split_choice.setEnabled(editing and selected_count > 1)
        self.remove_choice.setEnabled(editing and selected)
        self.review_plan.setVisible(self._approval_id is None and not saved_without_draft)
        self.review_plan.setEnabled(editing and bool(self._choices) and self._source_binding is not None and organization_ready)
        self.review_remaining.setVisible(self._approval_id is not None)
        self.review_remaining.setEnabled(idle and self._approval_id is not None and self._displayed_revision is not None)
        saved_step = self.steps.currentItem().data(0, Qt.UserRole)[0] if self.steps.currentItem() is not None else None
        from core.model_import_builder import saved_file_correction_moves
        safe_saved = False
        needs_sharing_omission = False
        selected_step_id = saved_step.step_id if saved_step is not None else None
        if selected_step_id != self._sharing_omission_step:
            with QSignalBlocker(self.omit_saved_sharing):
                self.omit_saved_sharing.setChecked(False)
            self._sharing_omission_step = selected_step_id
        if self._approved_plan is not None and saved_step is not None and saved_step.kind == "registration":
            try:
                saved_file_correction_moves(self._approved_plan, saved_step.step_id)
                safe_saved = True
            except ModelResourceError:
                try:
                    saved_file_correction_moves(self._approved_plan, saved_step.step_id,
                        allow_omit_terminal_paths=True)
                    safe_saved = needs_sharing_omission = True
                except ModelResourceError:
                    pass
        revisable_selection = (self._approval_id is not None and not self._pending_revision
            and safe_saved and saved_step is not None
            and saved_step.kind == "registration"
            and not self.steps.currentItem().text(1).casefold().startswith(("complete", "completed")))
        self.omit_saved_sharing.setVisible(revisable_selection and needs_sharing_omission)
        self.omit_saved_sharing.setEnabled(idle and self._child is None and revisable_selection)
        self.correct_saved_files.setVisible(revisable_selection)
        self.correct_saved_files.setEnabled(idle and self._child is None and revisable_selection
            and self._displayed_revision is not None
            and (not needs_sharing_omission or self.omit_saved_sharing.isChecked()))
        fast_steps = {step.step_id for step in self._plan.steps if step.kind == "manual_cache"} if self._plan else set()
        recorded_fast = any(self._saved_step_states.get(step_id) in {"started", "recoverable", "uncertain", "cancelled"}
            for step_id in fast_steps)
        displayed_fast = any(item.data(0, Qt.UserRole)[0].step_id in fast_steps
            and (result := item.data(0, Qt.UserRole)[1]) is not None
            and result.receipt is not None and result.status != "completed"
            for item in (self.steps.topLevelItem(index) for index in range(self.steps.topLevelItemCount())))
        show_fast_recovery = (self._approval_id is not None and self._open_fast_recovery is not None
            and (recorded_fast or displayed_fast))
        self.review_fast_recovery.setVisible(show_fast_recovery)
        self.review_fast_recovery.setEnabled(idle and show_fast_recovery and self._displayed_revision is not None)
        review = self._revision_review or self._review
        self.apply_plan.setText("Apply Reviewed Remaining Work" if self._revision_review is not None else "Apply Reviewed Plan")
        self.apply_plan.setVisible(review is not None)
        self.apply_plan.setEnabled(idle and review is not None and not any(row.status == "held" for row in review.steps))
        self.resume.setVisible(self._approval_id is not None)
        self.resume.setEnabled(idle and self._approval_id is not None and not self._pending_revision)
        self.stop.setVisible(self._task is not None); self.stop.setEnabled(self._task is not None)
        self.close_button.setEnabled(not self._closing)

    def _invalidate(self, message="Choices changed. Review the combined plan again."):
        self._review = self._plan = self._approval_id = None
        self._displayed_revision = self._revision_review = None
        self._approved_plan = None
        self._saved_step_states = {}
        self._pending_revision = False
        self.show_exact_details.setChecked(False)
        self.steps.clear(); self.details.clear(); self.saved_location.clear()
        self.summary.setText(message); self._update_actions()

    def _options_changed(self, *_):
        if not self.prepare_fast.isChecked(): self.share_fast.setChecked(False)
        self._invalidate()

    def _media_changed(self, *_):
        item = self.choices.currentItem()
        if item is None or not self._idle() or self._approval_id is not None: return
        selected_index = item.data(0, Qt.UserRole)
        choice = self._choices[selected_index]
        value = self.media_type.currentData()
        if value is None: choice.pop("media_type", None)
        else: choice["media_type"] = value
        self._render_choices(selected_index=selected_index)
        self._invalidate("Media choice changed. Review the combined plan again.")

    def _organization_status(self):
        self.organization_readiness.setToolTip("")
        if not self._choices:
            self.organization_readiness.setText("Choose model files first. Registration can keep them in place.")
            return False
        from core.model_organization import destination_path, family_options, normalize_model_family
        try:
            workspace = self.registry.snapshot()[0].get("workspace", {})
            destinations = []
            for choice in self._choices:
                rows = self._choice_files(choice)
                families = {normalize_model_family(row["family"]) for row in rows}
                if families & set(family_options("Images")) and families & set(family_options("Video")):
                    self.organization_readiness.setText("This choice mixes image and video families. Select it and Split Selected Choice, then set media for each group.")
                    return False
                media_type = choice.get("media_type")
                if media_type is None:
                    self.organization_readiness.setText("Choose Images or Video for every selected choice before organization or Fast preparation.")
                    return False
                if media_type == "Images" and families & set(family_options("Video")):
                    self.organization_readiness.setText("Wan video models cannot use Images folders. Choose Video for this choice.")
                    return False
                if not self.organize.isChecked(): continue
                for row in rows:
                    folder, reason = destination_path(workspace, role=row["role"], family=row["family"], media_type=media_type)
                    if not folder or "conflicting" in reason:
                        self.organization_readiness.setText("Set a destination for " + row["family"] + " / " + row["role"] + " in Folder Locations, then review again.")
                        return False
                    destinations.append(f"{row['family']} / {row['role']} ({media_type}) → {folder} [{reason}]")
            self.organization_readiness.setText(
                f"{len(destinations)} file destination(s) resolved from saved preferences. Review the exact moves before applying."
                if self.organize.isChecked() else "Media set for every selected choice. Review Fast-copy requirements before applying.")
            self.organization_readiness.setToolTip("\n".join(destinations))
            return True
        except (ValueError, OSError, RuntimeError) as exc:
            self.organization_readiness.setText(f"Folder preferences need review: {exc}")
            return False

    def _receipt_store(self):
        from core.model_selection import protected_model_roots
        guards = tuple(dict.fromkeys((*self.protected_roots, *protected_model_roots(self.settings))))
        if self._store is None:
            self._store = ImportReceiptStore(Path(self.registry.path).parent / "model-imports", protected_roots=guards)
        elif tuple(self._store.protected_roots) != guards:
            raise ModelResourceError("Protected folders changed. Reopen the import before reading saved operation records.")
        return self._store

    def _ensure_environment(self):
        store = self._receipt_store()
        if self._injected_environment:
            environment = self.environment
        else:
            snapshot, _ = self.registry.snapshot()
            if self.environment_factory is not None:
                environment = self.environment_factory(snapshot)
            else:
                from core.model_import_actions import ImportActionEnvironment
                from core.swarm_model_paths import SwarmModelPaths
                from core.swarm_runtime import SwarmRuntimeManager
                from .model_companion_workflow import resource_boundaries
                protected, managed = resource_boundaries(snapshot, self.settings, self.protected_roots)
                if self.environment is not None:
                    if (tuple(self.environment.protected_roots), tuple(self.environment.managed_roots)) != (tuple(protected), tuple(managed)):
                        raise ModelResourceError("Protected or managed folders changed. Reopen the import and review the current boundaries.")
                    environment = self.environment
                else:
                    environment = ImportActionEnvironment(self.registry, SwarmModelPaths(SwarmRuntimeManager()),
                        protected_roots=protected, managed_roots=managed,
                        boundary_provider=lambda: resource_boundaries(self.registry.snapshot()[0], self.settings, self.protected_roots))
            if self.environment is not None and environment is not self.environment:
                old = self.environment
                if (tuple(old.protected_roots), tuple(old.managed_roots)) != (tuple(environment.protected_roots), tuple(environment.managed_roots)):
                    raise ModelResourceError("Protected or managed folders changed. Reopen and review the import again.")
                environment = old
            self.environment = environment
        if self._service is None:
            self._service = ModelImportPlanService(store, environment.ports())
        return environment, self._service

    def _read_source(self):
        if not self._idle(): return
        self._invalidate("Reading the enrolled source and its recorded folders…")
        self._inventory = self._source_binding = None
        if not self._injected_environment:
            self.environment = self._service = self._store = None
        def read(job):
            environment, _ = self._ensure_environment()
            if job.cancel_requested: raise InterruptedError("Source read cancelled.")
            inventory = environment.paths.read()
            if inventory.binding != environment.source_binding_reader():
                raise ModelResourceError("The enrolled source changed during the read. Read it again.")
            from core.model_storage_policy import load_policy
            return inventory, load_policy(self.registry.snapshot()[0].get("workspace", {})).mode
        self._run("Reading SwarmUI source and folders", read, self._source_read)

    def _request_setup(self):
        if self._idle() and self._approval_id is None:
            self.setup_requested.emit()

    def resume_from_setup(self):
        """Retain unsaved choices but require fresh source and plan review."""
        if self._approval_id is not None:
            raise RuntimeError("Saved imports need their recorded recovery route.")
        self._invalidate("Choices retained. Read Source / Folders, then review the combined plan again.")
        self._inventory = self._source_binding = None
        if not self._injected_environment:
            self.environment = self._service = self._store = None
        self.source.setText("Returned from Setup. SwarmUI source has not been read again.")
        self.status.setText("No connection or import action was started by returning. Read Source / Folders to continue.")
        self._update_actions()

    def _source_read(self, inventory):
        if isinstance(inventory, tuple): inventory, self._storage_mode = inventory
        self._inventory = inventory; self._source_binding = deepcopy(inventory.binding)
        self.source.setText("Source folders read · " + str(inventory.model_root))
        self.source.setToolTip("\n".join(_lines(asdict(inventory))))
        self.status.setText("Folders read. Choose SwarmUI Folder opens the recorded folder choices; no files have been scanned.")
        self.summary.setText("Choose a SwarmUI folder, other exact files or downloads, then review the combined consequences.")

    def _folder_locations(self):
        if not self._idle(): return
        from .folder_locations import open_folder_locations
        saved = self._approval_id is not None
        if saved:
            self._review = self._revision_review = None
            self._pending_revision = True
            self.summary.setText("Completed work stays recorded. Review Remaining Work after closing Folder Locations.")
        else: self._invalidate("Folder settings may change. Review this import again after closing the editor.")
        self._folder_editor_open = True; self._update_actions()
        try:
            open_folder_locations(self, focus="default_destination", settings=self.settings, registry=self.registry,
                extra_protected_roots=self.protected_roots)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        finally:
            self._folder_editor_open = False
            self._inventory = None
            if not saved: self._source_binding = None
            self._path_choice = None; self._exposed_resources = ()
            self.source.setText("Folder settings review closed. " + ("Review Remaining Work to see its current consequences."
                if saved else "Read Source / Folders again before reviewing this import."))
            self._update_actions(); self._finish_close()

    def _review_folders(self):
        if not self._idle() or self._inventory is None or self._storage_mode != "single": return
        from .swarm_model_paths_dialog import SwarmModelPathsDialog
        from .model_companion_workflow import resource_boundaries
        try:
            environment, _ = self._ensure_environment()
            child = SwarmModelPathsDialog(None, self, service=environment.paths,
                model_directory=str(getattr(self.settings, "ai_models_directory", "") or ""),
                model_registry=self.registry, protected_roots=environment.protected_roots, jobs=self.jobs,
                boundary_provider=lambda: resource_boundaries(self.registry.snapshot()[0], self.settings, self.protected_roots)[0],
                collect_for_import=True)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); return
        self._child = child
        child.finished.connect(lambda result: self._folders_returned(child, result))
        child.show(); self._update_actions()

    def _folders_returned(self, child, result):
        if self._child is not child: return
        self._child = None
        if not self._closing and result == QDialog.Accepted:
            choice = getattr(child, "selected_import_choice", None)
            if choice is not None:
                self.set_path_choice(choice)
                self.summary.setText("Exact SwarmUI folder changes added. Review the combined plan before applying them.")
        self._update_actions(); self._finish_close()

    def _choose_files(self): self._open_collection(False)

    def _review_choice(self):
        item = self.choices.currentItem()
        if not self._idle() or self._approval_id is not None or self._child is not None or item is None: return
        index = item.data(0, Qt.UserRole)
        original = deepcopy(self._choices[index])
        if original.get("kind") != "registration": return
        from .model_workspace_dialog import ModelWorkspaceDialog
        child = ModelWorkspaceDialog(self.registry, self.jobs, self, settings=self.settings,
            protected_roots=self.protected_roots, collect_for_import=True,
            initial_import_review=original["review"])
        self._child = child
        child.finished.connect(lambda result: self._review_choice_returned(child, result, index, original))
        child.show(); self._update_actions()

    def _review_choice_returned(self, child, result, index, original):
        if self._child is not child: return
        self._child = None
        if (not self._closing and result == QDialog.Accepted
                and self._approval_id is None and index < len(self._choices)
                and self._choices[index] == original):
            choice = getattr(child, "selected_import_choice", None)
            try:
                if choice is None or choice.get("kind") != "registration" or not self._choice_files(choice):
                    raise ModelResourceError("Choose at least one unchanged reviewed file before updating this choice.")
                old_ids = {row["resource_id"] for row in self._choice_files(original)}
                new_ids = {row["resource_id"] for row in self._choice_files(choice)}
                removed_ids = old_ids - new_ids
                updated = deepcopy(original)
                updated["review"] = choice["review"]
                self._choices[index] = updated
                self._companion_requests = [row for row in self._companion_requests
                    if row["request"]["main_resource_id"] not in removed_ids
                    and not any(slot["resource_id"] in removed_ids for slot in row["choices"])]
                self._render_choices(selected_index=index)
                self._invalidate("Selected files updated. Held or excluded files remain in the file review; review the combined plan again before applying. Other import choices are unchanged.")
            except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        self._update_actions(); self._finish_close()

    def _choose_swarm_folder(self):
        self._open_collection(False, initial_known_swarm_folders=True)

    def _add_download(self): self._open_collection(True)

    def _open_collection(self, provider=False, *, handoff=None, companion=None,
                         initial_known_swarm_folders=False):
        if companion is None and not self._idle(): return
        if self._inventory is None: return
        from .model_workspace_dialog import ModelWorkspaceDialog
        inventory = self._inventory
        child = ModelWorkspaceDialog(self.registry, self.jobs, companion or self, settings=self.settings,
            protected_roots=self.protected_roots, collect_for_import=True,
            swarm_folder_reader=lambda: inventory, initial_companion_handoff=handoff,
            initial_known_swarm_folders=initial_known_swarm_folders)
        if provider:
            index = next((i for i in range(child.tabs.count()) if child.tabs.tabText(i) == "Provider Evidence"), -1)
            if index >= 0: child.tabs.setCurrentIndex(index)
        if companion is not None:
            companion.track_external_child(child)
            child.finished.connect(lambda result: self._collection_returned(child, result, companion, handoff))
        else:
            self._child = child
            child.finished.connect(lambda result: self._collection_returned(child, result))
        child.show(); self._update_actions()
        return child

    def _collection_returned(self, child, result, companion=None, handoff=None):
        if self._child is child: self._child = None
        choice = getattr(child, "selected_import_choice", None)
        if not self._closing and result == QDialog.Accepted and choice is not None:
            try:
                self.add_import_choice(choice)
                if companion is not None and handoff is not None:
                    from core.model_companion_readiness import CompanionChoice
                    rows = self._choice_files(choice)
                    if len(rows) != 1: raise ModelResourceError("Choose one exact model for the required resource slot.")
                    row = rows[0]
                    selected = CompanionChoice(handoff.slot, row["resource_id"], row["path"], row["role"], row["family"])
                    companion.request = replace(companion.request,
                        choices=tuple(item for item in companion.request.choices if item.slot != selected.slot) + (selected,))
                    companion._remember_choices()
                    self.set_companion_choices(companion.request)
                    companion.refresh_after_child()
                    companion.status.setText("Exact choice added to the combined import. Registration and verification wait for Apply.")
            except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        self._update_actions(); self._finish_close()

    @staticmethod
    def _choice_files(choice):
        from core.model_import_actions import registration_effects
        from core.model_scan_review import _encode
        import json
        if choice["kind"] == "acquisition":
            plan = choice["plan"].receipt() if hasattr(choice["plan"], "receipt") else choice["plan"]
            return [{"path": str(plan["destination"]), "resource_id": "sha256:" + plan["sha256"],
                "size": plan["size"], "role": choice["role"], "family": choice["family"]}]
        review = choice["review"]
        return registration_effects({"review": review if isinstance(review, dict) else json.loads(_encode(review))})["files"]

    def add_import_choice(self, choice):
        if self._task is not None: raise ModelResourceError("Wait for current import work before changing choices.")
        rows = self._choice_files(choice)
        if not rows: raise ModelResourceError("Choose at least one exact file.")
        self._choices.append(deepcopy(choice)); self._render_choices(); self._invalidate()

    def _render_choices(self, *, selected_index=None):
        self.choices.clear()
        for index, choice in enumerate(self._choices):
            files = self._choice_files(choice)
            name = Path(files[0]["path"]).name if len(files) == 1 else f"{len(files):,} selected files"
            types = sorted({f"{row['role']} / {row['family']}" for row in files})
            scope = "Download + registration" if choice["kind"] == "acquisition" else "Registration"
            if choice.get("media_type"): scope += " · " + choice["media_type"]
            item = QTreeWidgetItem((name, "; ".join(types), scope))
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(0, "\n".join(row["path"] for row in files))
            self.choices.addTopLevelItem(item)
        if self.choices.topLevelItemCount():
            index = self.choices.topLevelItemCount() - 1 if selected_index is None else selected_index
            self.choices.setCurrentItem(self.choices.topLevelItem(index))

    def _remove_choice(self):
        item = self.choices.currentItem()
        if not self._idle() or item is None: return
        self._choices.pop(item.data(0, Qt.UserRole))
        self._companion_requests.clear()  # stale dependencies must be chosen again
        self._render_choices(); self._invalidate("Choice removed. Recheck required-resource choices before reviewing again.")

    def _split_choice(self):
        item = self.choices.currentItem()
        if not self._idle() or item is None or self._approval_id is not None: return
        index = item.data(0, Qt.UserRole)
        choice = self._choices[index]
        if choice.get("kind") != "registration": return
        rows = self._choice_files(choice)
        if len(rows) < 2: return
        picker = SplitModelImportChoiceDialog(rows, self)
        if picker.exec() != QDialog.Accepted: return
        from core.model_import_builder import split_registration_choice
        try: first, second = split_registration_choice(choice, tuple(picker.new_paths))
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); return
        self._choices[index:index + 1] = [first, second]
        self._render_choices(selected_index=index + 1)
        self._invalidate("Choice split into two exact file groups. Select each group and choose Images or Video before reviewing organization or Fast copies.")

    def set_path_choice(self, choice, *, exposed_resources=()):
        if not self._idle(): raise ModelResourceError("Wait for the active import review before changing folders.")
        self._path_choice, self._exposed_resources = deepcopy(choice), deepcopy(exposed_resources)
        if choice is not None: self.share_fast.setChecked(False)
        self._invalidate()

    def set_companion_choices(self, request):
        value = asdict(request); choices = value.pop("choices")
        entry = {"request": value, "choices": list(choices)}
        self._companion_requests = [row for row in self._companion_requests
            if row["request"]["main_resource_id"] != value["main_resource_id"]]
        self._companion_requests.append(entry); self._invalidate("Required-resource choices retained for verification in the combined plan.")

    def _required_resources(self):
        item = self.choices.currentItem()
        if not self._idle() or item is None: return
        files = self._choice_files(self._choices[item.data(0, Qt.UserRole)])
        row = files[0]
        if len(files) > 1:
            labels = [f"{value['path']} · {value['role']} / {value['family']}" for value in files]
            selected, accepted = QInputDialog.getItem(self, "Required Resources", "Choose the main model", labels, 0, False)
            if not accepted: return
            row = files[labels.index(selected)]
        from core.model_companion_readiness import CompanionChoice, CompanionReadinessRequest
        from .model_companion_readiness_dialog import ModelCompanionReadinessDialog
        from .model_companion_workflow import resource_boundaries
        request = CompanionReadinessRequest(row["resource_id"], choices=(CompanionChoice("main", row["resource_id"], row["path"], row["role"], row["family"]),))
        for saved in self._companion_requests:
            if saved["request"]["main_resource_id"] == row["resource_id"]:
                request = CompanionReadinessRequest(**saved["request"], choices=tuple(CompanionChoice(**value) for value in saved["choices"]))
                break
        def service_factory(snapshot):
            from core.model_companion_readiness import CompanionReadinessService
            protected, managed = resource_boundaries(snapshot, self.settings, self.protected_roots)
            return CompanionReadinessService(self.registry, protected_roots=protected, managed_roots=managed)
        child = ModelCompanionReadinessDialog(self.registry, self.jobs, row["resource_id"], self,
            settings=self.settings, protected_roots=self.protected_roots, initial_request=request,
            collect_for_import=True, service_factory=service_factory)
        child.import_choices_selected.connect(self.set_companion_choices)
        child.acquisition_requested.connect(lambda handoff: self._open_collection(True, handoff=handoff, companion=child))
        child.preparation_requested.connect(lambda handoff: self._collect_preparation(child, handoff))
        self._child = child
        child.finished.connect(lambda _result: self._companions_returned(child))
        child.show(); self._update_actions()

    def _collect_preparation(self, child, handoff):
        self.set_companion_choices(handoff.report.request); self.prepare_fast.setChecked(True)
        child.status.setText("Resource verification and Fast preparation added to the combined import review. No files were copied.")

    def _companions_returned(self, child):
        if self._child is not child: return
        self._child = None
        if not self._closing: self.set_companion_choices(child.request)
        self._update_actions(); self._finish_close()

    def _review_plan(self):
        if not self._idle() or not self._choices or self._source_binding is None: return
        from core.model_import_builder import build_import_plan
        choices, binding = deepcopy(self._choices), deepcopy(self._source_binding)
        options = dict(organize=self.organize.isChecked(), prepare_fast=self.prepare_fast.isChecked(),
            companion_requests=deepcopy(self._companion_requests), path_choice=deepcopy(self._path_choice),
            exposed_resources=deepcopy(self._exposed_resources),
            share_fast_inventory=self._inventory if self.share_fast.isChecked() else None)
        self._invalidate("Checking current files, settings, and all planned consequences…")
        def review(job):
            environment, service = self._ensure_environment()
            with environment.operation(should_cancel=lambda: job.cancel_requested, progress=self._progress(job)):
                return service.review(build_import_plan(environment, binding, choices, **options))
        self._run("Reviewing combined model import", review, self._reviewed)

    def _reviewed(self, review):
        self._review, self._plan = review, review.plan
        self.optional_toggle.setChecked(False)
        self.show_exact_details.setChecked(False)
        self._show_steps(review.steps)
        held = sum(row.status == "held" for row in review.steps)
        self.summary.setText(f"{len(review.steps)} planned steps · {held} held. " +
            ("Select the held registration choice, Review Selected Files, then review again." if held and any(
                row.status == "held" and step.kind == "registration" for row, step in zip(review.steps, review.plan.steps))
                else "Resolve held choices and review again." if held else
                "Review every step. Apply approves the displayed sequence once; dependent steps recheck before acting."))
        self.status.setText("Review complete. No import effects have been applied.")

    def _show_steps(self, results=(), *, saved=False):
        self.steps.clear(); self.details.clear()
        by_id = {row.step_id: row for row in results}
        if self._plan is None: return
        for step in self._plan.steps:
            result = by_id.get(step.step_id)
            status = _text(result.status) if result is not None else "Not started"
            if saved:
                recorded = self._saved_step_states.get(step.step_id, "unknown")
                status = {
                    "completed": "Complete · recorded",
                    "cancelled": "Stopped · recorded",
                    "not_started": "Not started",
                    "started": "Started · verify",
                    "intent": "Intent · verify",
                }.get(recorded, _text(recorded) + " · recorded")
            message = result.message if result is not None else ""
            if not message: message = "Waiting for earlier import steps" if step.depends_on else "Ready for explicit review"
            item = QTreeWidgetItem((STEP_LABELS[step.kind], status, message))
            item.setData(0, Qt.UserRole, (step, result))
            for column in range(3): item.setToolTip(column, item.text(column))
            self.steps.addTopLevelItem(item)
        self.steps.setCurrentItem(self.steps.topLevelItem(0))

    @staticmethod
    def _step_summary(step, result):
        effects = step.expected_effects
        if step.kind == "registration":
            files = effects.get("files", ())
            consequence = (f"Register {len(files):,} selected file{'s' if len(files) != 1 else ''}; "
                "the existing files stay in place.")
        elif step.kind == "move":
            selection = step.choices.get("selection", {})
            source = selection.get("source", effects.get("source", ""))
            destination = step.choices.get("destination", effects.get("destination", ""))
            name = Path(source).name if source else "Selected model"
            folder = Path(destination).parent.name if destination else "the reviewed folder"
            consequence = f"Organize {name} into {folder}. The original is removed only after the destination is verified."
        elif step.kind == "acquisition":
            consequence = "Download the exact reviewed model; existing files are not replaced."
        elif step.kind == "companions":
            consequence = "Verify the selected required model set before dependent preparation."
        elif step.kind == "manual_cache":
            cleanup = effects.get("cleanup", ())
            consequence = f"Prepare Fast copies and review {len(cleanup):,} proposed removal{'s' if len(cleanup) != 1 else ''}."
        else:
            consequence = "Review the exact SwarmUI model-folder changes before applying."
        if result is not None and result.message:
            return consequence + " Current state: " + result.message
        return consequence

    def _show_step(self):
        item = self.steps.currentItem()
        if item is None:
            self.details.clear()
            self.selected_step_summary.setText("Select an import step to review its consequence and next action.")
            self._update_actions(); return
        step, result = item.data(0, Qt.UserRole)
        self.selected_step_summary.setText(self._step_summary(step, result))
        if self._approval_id is None and step.kind == "registration" and step.step_id.startswith("register_"):
            suffix = step.step_id.removeprefix("register_")
            if suffix.isdecimal() and 0 < int(suffix) <= self.choices.topLevelItemCount():
                self.choices.setCurrentItem(self.choices.topLevelItem(int(suffix) - 1))
        lines = [STEP_LABELS[step.kind], "Status: " + item.text(1)]
        cleanup = step.expected_effects.get("cleanup", ()) if step.kind == "manual_cache" else ()
        if cleanup:
            lines.append("Fast copies proposed for removal:")
            lines.extend(f"{row['path']} ({int(row['size']):,} bytes)" for row in cleanup)
        if result is not None and result.message: lines.append(result.message)
        lines.extend(["", "Exact reviewed consequences", *_lines(step.expected_effects)])
        if result is not None and result.receipt:
            lines.extend(["", "Retained operation: " + result.receipt.operation_id, "Receipt: " + result.receipt.locator])
        self.details.setPlainText("\n".join(lines))
        self._update_actions()

    def _apply_plan(self):
        if self._revision_review is not None:
            self._apply_revision(); return
        if not self._idle() or self._review is None: return
        review = self._review
        def approve(job):
            environment, service = self._ensure_environment()
            if job.cancel_requested: raise InterruptedError("Approval cancelled before saving.")
            if environment.source_binding_reader() != review.plan.source_binding:
                raise ModelResourceError("The enrolled source changed. Read the source and review again.")
            if self.registry.snapshot()[1] != review.plan.registry_token:
                raise ModelResourceError("The registry changed. Review the combined plan again.")
            identity = service.approve(review, confirmed=True)
            return identity, job.cancel_requested
        self._run("Saving approved import", approve, self._approved)

    def _approved(self, result):
        self._approval_id, cancelled = result
        self._displayed_revision = self._approval_id
        self._approved_plan = self._plan
        self._review = None; self._show_saved_location()
        if cancelled or self._cancelled_delivery or self._closing:
            self.status.setText("Approved import saved. No new work starts while stopping; use Resume when ready.")
        else: self._execute(False)

    def _show_saved_location(self):
        self.saved_location.setText("Saved import: " + str(self._receipt_store().directory / self._approval_id / "plan.json"))

    def _resume(self):
        if self._idle() and self._approval_id is not None and not self._pending_revision: self._execute(True)

    def _review_saved_files(self):
        item = self.steps.currentItem()
        if (not self._idle() or self._approval_id is None or self._displayed_revision is None
                or self._pending_revision or self._child is not None or item is None): return
        step_id = item.data(0, Qt.UserRole)[0].step_id
        identity, revision = self._approval_id, self._displayed_revision
        omit_sharing = self.omit_saved_sharing.isVisible() and self.omit_saved_sharing.isChecked()
        def prove(job):
            environment, service = self._ensure_environment()
            captured = []
            cancel, progress = lambda: job.cancel_requested, self._progress(job)
            def probe(plan, prefix, _context):
                from core.model_import_builder import saved_file_correction_moves
                saved_file_correction_moves(plan, step_id,
                    allow_omit_terminal_paths=omit_sharing)
                index = next((i for i, step in enumerate(plan.steps) if step.step_id == step_id), -1)
                if index < prefix or index < 0:
                    raise ModelResourceError("The selected registration is completed or no longer revisable.")
                captured.append(plan.steps[index])
                return plan
            with environment.operation(should_cancel=cancel, progress=progress):
                service.propose_remaining(identity, probe, expected_revision=revision,
                    should_cancel=cancel, progress=progress)
            return captured[0]
        self._run("Proving saved import recovery", prove,
            lambda step: self._saved_files_proven(identity, revision, step, omit_sharing))

    def _review_fast_recovery(self):
        if not self._idle() or self._approval_id is None or self._displayed_revision is None or self._open_fast_recovery is None:
            return
        identity, revision = self._approval_id, self._displayed_revision
        def exact_owner(_job):
            service = ModelImportPlanService(self._receipt_store(), {})
            receipt = service.recorded_fast_recovery(identity, expected_revision=revision)
            environment, _ = self._ensure_environment()
            snapshot, _ = self.registry.snapshot()
            locator = environment.cache._directory(receipt.operation_id) / "baseline.json"
            reference = environment.check_reference(receipt, locator)
            if environment.cache._operation_reference(snapshot, receipt.operation_id) != reference:
                raise ModelResourceError("The saved Fast owner no longer matches its exact native operation. Inspect the saved import and Fast operation records before recovery.")
            return receipt.operation_id
        self._run("Finding this import's exact Fast operation", exact_owner, self._fast_recovery_ready)

    def _fast_recovery_ready(self, operation_id):
        if self._open_fast_recovery is None or self._closing:
            return
        child = self._open_fast_recovery(operation_id, self)
        if child is None:
            raise ModelResourceError("Fast Recovery could not open. The saved import remains unchanged; return to Models Storage to inspect its exact record.")
        self._child = child
        child.finished.connect(lambda _result: self._fast_recovery_closed(child))
        self.status.setText("Review only the selected recorded Fast operation. Returning here will not resume the import automatically.")

    def _fast_recovery_closed(self, child):
        if self._child is not child:
            return
        self._child = None
        if not self._closing:
            self.status.setText("Fast Recovery closed. Resume Saved Import to recheck exact owner receipts; no import step restarted automatically.")
        self._update_actions(); self._finish_close()

    def _saved_files_proven(self, identity, revision, step, omit_sharing=False):
        if self._approval_id != identity or self._displayed_revision != revision: return
        from .model_workspace_dialog import ModelWorkspaceDialog
        child = ModelWorkspaceDialog(self.registry, self.jobs, self, settings=self.settings,
            protected_roots=self.protected_roots, collect_for_import=True,
            initial_import_review=step.choices["review"])
        self._child = child
        child.finished.connect(lambda result: self._saved_files_returned(child, result, identity, revision, step, omit_sharing))
        child.show(); self._update_actions()

    def _saved_files_returned(self, child, result, identity, revision, original, omit_sharing=False):
        if self._child is not child: return
        self._child = None
        if not self._closing and result == QDialog.Accepted:
            choice = getattr(child, "selected_import_choice", None)
            try:
                if choice is None or choice.get("kind") != "registration":
                    raise ModelResourceError("Choose an unchanged reviewed subset before revising this saved import.")
                from core.model_scan_review import _encode
                import json
                selection = {"review": json.loads(_encode(choice["review"]))}
                self._pending_revision = True
                def revise(job):
                    environment, service = self._ensure_environment()
                    cancel, progress = lambda: job.cancel_requested, self._progress(job)
                    def propose(plan, prefix, context):
                        from core.model_import_builder import correct_saved_registration
                        snapshot, _token = environment.observe(context)
                        return correct_saved_registration(plan, prefix, original, selection, snapshot,
                            allow_omit_terminal_paths=omit_sharing)
                    with environment.operation(should_cancel=cancel, progress=progress):
                        proposed = service.propose_remaining(identity, propose,
                            expected_revision=revision, should_cancel=cancel, progress=progress)
                        return service.review_remaining(identity, proposed,
                            should_cancel=cancel, progress=progress)
                self._run("Reviewing corrected saved files", revise, self._remaining_reviewed)
            except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        self._update_actions(); self._finish_close()

    def _review_remaining(self):
        if not self._idle() or self._approval_id is None or self._displayed_revision is None: return
        identity, revision, approved_plan = self._approval_id, self._displayed_revision, self._approved_plan
        self._review = self._revision_review = None; self._pending_revision = True
        def review(job):
            from core.model_import_builder import propose_remaining_import_plan
            environment, service = self._ensure_environment()
            cancel, progress = lambda: job.cancel_requested, self._progress(job)
            with environment.operation(should_cancel=cancel, progress=progress):
                proposed = propose_remaining_import_plan(environment, service, identity,
                    expected_revision=revision, should_cancel=cancel, progress=progress)
                if proposed == approved_plan:
                    return None
                return service.review_remaining(identity, proposed, should_cancel=cancel, progress=progress)
        self._run("Reviewing remaining import work", review, self._remaining_reviewed)

    def _remaining_reviewed(self, review):
        if review is None:
            self._pending_revision = False
            self._show_steps(saved=True)
            self.summary.setText("Saved choices are unchanged. Completed work stays recorded; unfinished steps require recheck.")
            self.status.setText("No changed choices need approval. Select a remaining step to inspect its saved consequences, then Resume. Resume rechecks current source and operation receipts.")
            return
        if review.approval_id != self._approval_id or review.prior_revision != self._displayed_revision:
            raise ModelResourceError("The displayed saved import revision changed. Reopen it before reviewing remaining work.")
        self._revision_review, self._plan = review, review.plan
        self._reflect_plan_options()
        self.optional_toggle.setChecked(False)
        self._show_steps(review.steps)
        held = sum(row.status == "held" for row in review.steps)
        victims = [(index, victim) for index, step in enumerate(review.plan.steps)
            if index >= review.prefix_length and step.kind == "manual_cache"
            for victim in step.expected_effects.get("cleanup", ())]
        cleanup = f" Fast copies proposed for removal: {len(victims)}. Select the preparation step to review every path." if victims else " No Fast copies are proposed for removal."
        excluded = ""
        if self._approved_plan is not None:
            before = {step.step_id: step for step in self._approved_plan.steps}
            revised_ids = {step.step_id for step in review.plan.steps}
            removed_moves = [step for step in self._approved_plan.steps
                if step.kind == "move" and step.step_id not in revised_ids]
            removed_files = sum(len(before[step.step_id].expected_effects["files"]) - len(step.expected_effects["files"])
                for step in review.plan.steps if step.kind == "registration"
                and step.step_id in before and before[step.step_id].kind == "registration")
            if removed_files > 0:
                excluded = f" Excluded files: {removed_files}; their moves removed: {len(removed_moves)}."
            omitted_fast = 0
            for step in review.plan.steps:
                previous = before.get(step.step_id)
                if step.kind != "manual_cache" or previous is None or previous.kind != "manual_cache":
                    continue
                def selected_resources(cache_step):
                    requirements = cache_step.choices.get("requirements", {})
                    return {tuple(row.get(key) for key in ("resource_id", "archive_path", "role", "family", "media_type"))
                        for row in requirements.get("resources", ())}
                omitted_fast += len(selected_resources(previous) - selected_resources(step))
            if omitted_fast:
                excluded += f" Planned Fast preparation omitted for {omitted_fast} excluded file{'s' if omitted_fast != 1 else ''}; no existing Fast copy removed."
            if any(step.kind == "paths" and step.step_id not in revised_ids
                    for step in self._approved_plan.steps):
                excluded += " Optional SwarmUI folder sharing omitted; existing folder settings stay unchanged."
        self.summary.setText(f"Completed steps retained: {review.prefix_length} · Remaining steps: {len(review.plan.steps) - review.prefix_length} · Held: {held}." + excluded + cleanup)
        self.status.setText("Completed operations rechecked. " + ("Resolve the held choices and review again." if held else
            "Apply Reviewed Remaining Work approves this displayed revision. Completed effects will not repeat.") +
            " Archive originals, Keep Warm models, and active or manually pinned copies stay protected.")
        target = victims[0][0] if victims else min(review.prefix_length, self.steps.topLevelItemCount() - 1)
        self.steps.setCurrentItem(self.steps.topLevelItem(target))
        if victims:
            self.show_exact_details.setChecked(True)

    def _apply_revision(self):
        if not self._idle() or self._revision_review is None: return
        review = self._revision_review
        def approve(job):
            environment, service = self._ensure_environment()
            cancel, progress = lambda: job.cancel_requested, self._progress(job)
            with environment.operation(should_cancel=cancel, progress=progress):
                token = service.approve_revision(review, confirmed=True, should_cancel=cancel, progress=progress)
                return token, job.cancel_requested
        self._run("Saving reviewed remaining work", approve, self._revision_approved)

    def _revision_approved(self, result):
        self._displayed_revision, cancelled = result
        self._approved_plan = self._plan
        self._revision_review = None; self._pending_revision = False
        self._show_saved_location()
        if cancelled or self._cancelled_delivery or self._closing:
            self.status.setText("Revised remaining work saved. Use Resume when ready; completed effects remain recorded.")
        else: self._execute(True)

    def _execute(self, resume):
        identity, revision = self._approval_id, self._displayed_revision
        def run(job):
            environment, service = self._ensure_environment()
            with environment.operation(should_cancel=lambda: job.cancel_requested, progress=self._progress(job)):
                method = service.resume if resume else service.execute
                return method(identity, confirmed=True, expected_revision=revision,
                    should_cancel=lambda: job.cancel_requested, progress=self._progress(job))
        self._run("Resuming recorded import" if resume else "Applying reviewed model import", run, self._executed)

    def _executed(self, result):
        self._review = self._revision_review = None; self._pending_revision = False
        self._show_steps(result.steps)
        self.summary.setText("Import " + _text(result.status).lower() + ". Completed steps remain recorded.")
        self.status.setText(result.message or ("Import complete. Exact resources are recorded; execution still needs workflow preflight."
            if result.status == "completed" else "Review each held or recoverable step. Resume rechecks canonical operation records before continuing."))
        if result.status != "completed" and self._approved_plan is not None:
            from core.model_import_builder import saved_file_correction_moves
            fast_owner = next((row for row in result.steps if row.receipt is not None
                and row.status != "completed"
                and any(step.step_id == row.step_id and step.kind == "manual_cache"
                    for step in self._approved_plan.steps)), None)
            held_registration = next((row.step_id for row in result.steps if row.status == "held"
                and any(step.step_id == row.step_id and step.kind == "registration"
                    for step in self._approved_plan.steps)), None)
            if fast_owner is not None and self._open_fast_recovery is not None:
                self.status.setText("An exact Fast operation is interrupted. Review Interrupted Fast Operation, then return and explicitly Resume Saved Import. Completed registrations and moves remain recorded; nothing restarts automatically.")
            elif held_registration is not None:
                try:
                    saved_file_correction_moves(self._approved_plan, held_registration)
                except ModelResourceError:
                    try:
                        saved_file_correction_moves(self._approved_plan, held_registration,
                            allow_omit_terminal_paths=True)
                    except ModelResourceError:
                        self.status.setText("A registration is held, but this saved import has other dependent work. Preserve its receipts; do not restart or remove completed operations. Review the exact held step before recovery.")
                    else:
                        self.status.setText("A registration is held. Select its step, choose to leave SwarmUI folder settings unchanged, then Review Selected Saved Files. The unstarted sharing step will be omitted only in a separately reviewed revision; existing settings and completed receipts stay unchanged.")
                else:
                    self.status.setText("A registration is held. If selected files changed, choose its step and Review Selected Saved Files to recheck and exclude them. Unstarted moves and planned Fast preparation for excluded files can be omitted; completed steps stay recorded. Review and approve the corrected remainder before Resume.")
        self.has_changed = True; self.changed.emit(result); self.completed.emit(result)
        if result.status == "completed":
            # Keep the visible saved location; prevent unnecessary repeat clicks.
            self.source.setText("Import complete. Review the recorded steps below; open another saved import to inspect its status.")
            self._approval_id = None

    def _open_saved(self):
        if not self._idle(): return
        def list_saved(_job):
            store = self._receipt_store()
            return ModelImportPlanService(store, {}).recent_saved_imports()
        self._run("Finding saved model imports", list_saved, self._saved_imports_listed)

    def _saved_imports_listed(self, summaries):
        picker = SavedModelImportPicker(summaries, self)
        if picker.exec() != QDialog.Accepted:
            return
        if picker.browse_requested:
            self._browse_saved_import()
        elif picker.selected_approval_id:
            store = self._receipt_store()
            self.open_saved_import(store.directory / picker.selected_approval_id / "plan.json")

    def _browse_saved_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Saved Model Import", str(Path(self.registry.path).parent / "model-imports"), "Saved import (plan.json)")
        if path: self.open_saved_import(path)

    def open_saved_import(self, path):
        if not self._idle(): return
        path = Path(path)
        try: store = self._receipt_store()
        except (ValueError, RuntimeError, OSError) as exc:
            self._failed(str(exc)); return
        identity = path.parent.name
        if not path.is_absolute() or path != store.directory / identity / "plan.json":
            self._failed("Choose this registry's exact saved import plan.json. Arbitrary operation receipts cannot be imported here."); return
        self._invalidate("Opening the selected saved import…")
        def read(_job):
            # Metadata only: no environment, registry, model, or Swarm reads.
            service = ModelImportPlanService(store, {})
            plan, revision, states = service.read_recorded_import(identity)
            return identity, plan, revision, states
        self._run("Opening saved import choices", read, self._saved_opened)

    def _saved_opened(self, result):
        self._approval_id, self._plan, self._displayed_revision, self._saved_step_states = result
        self._approved_plan = self._plan
        self._review = self._revision_review = None; self._pending_revision = False
        self.show_exact_details.setChecked(False)
        self._choices.clear(); self.choices.clear(); self._companion_requests.clear()
        self._path_choice = None; self._exposed_resources = ()
        self._source_binding = deepcopy(self._plan.source_binding); self._inventory = None
        self._reflect_plan_options()
        self._show_steps(saved=True); self._show_saved_location()
        self.source.setText("Saved source binding loaded. Resume will check that the enrolled source still matches.")
        self.summary.setText("Saved choices opened without running work. Resume explicitly rechecks completed operations and continues eligible steps.")
        self.status.setText("No files were inspected and no source connection was made.")

    def _reflect_plan_options(self):
        kinds = {step.kind for step in self._plan.steps}
        with QSignalBlocker(self.organize), QSignalBlocker(self.prepare_fast), QSignalBlocker(self.share_fast):
            self.organize.setChecked("move" in kinds)
            self.prepare_fast.setChecked("manual_cache" in kinds)
            self.share_fast.setChecked(any(step.kind == "paths" and step.choices.get("mode") == "tiered" for step in self._plan.steps))

    @staticmethod
    def _progress(job):
        def report(done, total, label):
            # Owner progress can be bytes or items; cap Qt's signed integers.
            scale = 2**20 if max(done, total) > 2**30 else 1
            job.update(int(done // scale), max(1, int((total + scale - 1) // scale)), str(label))
        return report

    def _run(self, title, function, callback):
        if self._task is not None or self._closing: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        retained = []
        def run(job):
            result = function(job)
            retained.append(result)
            return result
        task = CoordinatedCallTask(self.jobs, title, run, resource="io")
        task._import_result = retained
        self._task = task; self.status.setText(title)
        binding = bind_job_progress(self, task, title=title, unit="units", cancel_button=self.stop)
        task.signals.complete.connect(lambda result: self._finish(task, callback, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._cancelled(task, callback))
        self.destroyed.connect(task.cancel); self._update_actions()
        try: task.start()
        except Exception as exc:
            binding.finish(task); task.cancel(); self._finish(task, self._failed, f"Could not start: {exc}")

    def _cancelled(self, task, callback):
        # Job history deliberately discards results. Retain this one operation's
        # return value locally until its queued terminal signal is delivered.
        if task._import_result:
            self._finish(task, callback, task._import_result[0], cancelled=True)
        else: self._finish(task, self._failed, "Stopped safely. Choices and saved operation receipts are retained.")

    def _finish(self, task, callback, result, *, cancelled=False):
        from shiboken6 import isValid
        if not isValid(self): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        task._import_result.clear()
        self._cancelled_delivery = cancelled
        try: callback(result)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        finally: self._cancelled_delivery = False
        self._update_actions(); self._finish_close()

    def _failed(self, message):
        if self._pending_revision and self._approved_plan is not None:
            self._plan = self._approved_plan; self._pending_revision = False
            self._reflect_plan_options()
            self._show_steps(saved=True)
        self._review = self._revision_review = None; self.status.setText(str(message))
        self.summary.setText("Import held. Choices and saved receipts are retained; review the reason before retrying.")
        self._update_actions()

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping at the next safe boundary. Completed effects and recovery receipts remain available.")

    def request_parent_close(self, continuation):
        pending = self.has_active_background_work()
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def has_active_background_work(self):
        return self._task is not None or self._child is not None or self._folder_editor_open

    def request_background_shutdown(self): self.reject()

    def reject(self):
        self._closing = True
        if self._folder_editor_open:
            from .folder_locations import FolderLocationsDialog
            for child in self.findChildren(FolderLocationsDialog, options=Qt.FindDirectChildrenOnly): child.reject()
        if self._child is not None:
            child = self._child
            close = getattr(child, "request_parent_close", None)
            if callable(close): close(self._finish_close)
            else: child.reject()
        if self._task is not None: self._stop()
        self._finish_close()

    def _finish_close(self):
        if not self._closing or self._closed or self.has_active_background_work(): return
        self._closed = True
        continuation, self._close_parent = self._close_parent, None
        super().reject()
        if continuation: continuation()

    def accept(self): self.reject()

    def closeEvent(self, event):
        self.reject()
        if self._closed: event.accept()
        else: event.ignore()
