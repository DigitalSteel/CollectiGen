"""Guided Character/Outfit evidence review into one canonical Generation Setup."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.generation_character_suggestions import (
    SuggestionFocus, load_suggestion_sources, recheck_suggestion_preview_source,
    recheck_suggestion_sources,
    qualified_look_relationship_ids, reviewed_look_alternatives,
    saved_owner_setup_details, suggest_character_outfit_setup, suggestion_focus_options,
)
from core.character_database import CharacterDatabase
from core.image_record import _path_fingerprint
from core.tag_database import TagDatabase
from core.knowledge_relationships import KnowledgeRelationshipRegistry
from core.relationship_evaluator import RelationshipEvaluator
from core.relationship_store import load_effective_relationship_registry
from core.tag_collection import normalize_tag
from .components import DisclosureButton
from .gallery import ProtectedImageView, content_safety_policy
from .generation_manual_setup_dialog import ManualGenerationSetupDialog, _PARTS
from .workers import CoordinatedCallTask


def _look_group_label(group):
    """Translate a diagnostic rule heading into a neutral choice heading."""
    return group.label.removeprefix("Contrary ")


class SuggestionSourcePreviewDialog(QDialog):
    """Read-only local example; the owning setup draft stays open behind it."""

    def __init__(self, path, *, label, source_type, settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("View Source Image")
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        heading = QLabel(f"Source example: {label}")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        relation = ("This detail was assigned to the selected Character or Outfit in this image."
                    if source_type == "owner_tag" else
                    "This detail belongs to the image evidence; it is not confirmed for this Character or Outfit.")
        explanation = QLabel(
            relation + " Other people, outfits or scene details may also appear. "
            "Viewing this example does not change Image Knowledge or your setup."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.protected_preview = ProtectedImageView(
            policy=content_safety_policy(), settings=settings, parent=self,
        )
        self.protected_preview.setAccessibleName("Protected source image example")
        layout.addWidget(self.protected_preview, 1)
        self.protected_preview.set_path(path)
        self.close_button = QPushButton("Close Source Image")
        self.close_button.clicked.connect(self.accept)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def prepare_for_privacy_capture(self):
        self.protected_preview.prepare_for_privacy_capture()


class CharacterOutfitSetupDialog(ManualGenerationSetupDialog):
    """Suggest, explicitly choose and author text without changing its source."""

    feedback_workflow = "Suggest generation setup from Character or Outfit"

    def __init__(self, jobs, storage_root, *, dataset, owner_kind, owner_id,
                 owner_name, representative_tag_id="", representative_names=(),
                 authority_paths=(), alternative_paths=(), registry=None, settings=None,
                 protected_model_roots=(), parent=None):
        self.dataset = dataset
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.representative_tag_id = representative_tag_id
        self.representative_names = tuple(representative_names)
        self._source_capture = None
        self._review_task = None
        self._source_session = None
        self._active_report = None
        self._source_preview_dialog = None
        self._active_focus = None
        self._review_mode = None
        self._authority_paths = tuple(Path(path) for path in authority_paths)
        self._authority_fingerprints = tuple(
            (path, _path_fingerprint(path)) for path in self._authority_paths
        )
        self._alternative_paths = tuple(Path(path) for path in alternative_paths)
        self._alternative_fingerprints = ()
        self._look_evaluator = None
        self._qualified_look_ids = ()
        self._look_alternatives = ()
        self._saved_detail_fingerprints = ()
        self._saved_details_used = False
        self._saved_owner_ready = not bool(self._authority_paths)
        self._image_details_used = False
        super().__init__(jobs, storage_root, registry=registry, settings=settings,
                         protected_model_roots=protected_model_roots,
                         pre_save_check=self._check_sources_before_save,
                         part_order=("outfit", "character", "scene", "pose", "style", "other")
                         if owner_kind == "outfit" else None, parent=parent)
        noun = "Character" if owner_kind == "character" else "Outfit"
        self.setWindowTitle(f"Suggest Generation Setup from {noun}")
        self.heading.setText(f"Make a reusable setup from {noun}: {owner_name}")
        self.resize(980, 820)
        self.name.setText(f"{owner_name} appearance" if owner_kind == "character"
                          else f"{owner_name} outfit")
        self.status.setText("Choose details or write your own on Edit Setup, then review. Nothing is saved or generated yet.")
        body = self.setup_scroll.widget()
        body_layout = body.layout()

        saved_group = QGroupBox(f"Saved {noun} Details")
        self.saved_group = saved_group
        saved_layout = QVBoxLayout(saved_group)
        saved_hint = QLabel(
            "These are authored Character or Outfit record details, not counts from images. "
            "Choose only what you want to reuse; edit the copied text before saving."
        )
        saved_hint.setWordWrap(True)
        saved_layout.addWidget(saved_hint)
        self.saved_tree = QTreeWidget()
        self.saved_tree.setHeaderLabels(("Use", "Saved detail", "Source", "Prompt part"))
        self.saved_tree.setRootIsDecorated(False)
        self.saved_tree.setFixedHeight(118)
        self.saved_tree.setAccessibleName(f"Selectable saved {noun} setup details")
        self.saved_tree.setColumnWidth(0, 54)
        self.saved_tree.setColumnWidth(1, 330)
        self.saved_tree.setColumnWidth(2, 150)
        self.saved_tree.setColumnWidth(3, 150)
        self.saved_tree.itemChanged.connect(self._update_saved_use_button)
        self.saved_tree.currentItemChanged.connect(self._show_saved_detail)
        saved_layout.addWidget(self.saved_tree)
        self.saved_detail_text = QTextEdit()
        self.saved_detail_text.setReadOnly(True)
        self.saved_detail_text.setFixedHeight(66)
        self.saved_detail_text.setAccessibleName("Full selected saved Character or Outfit detail")
        self.saved_detail_text.hide()
        saved_layout.addWidget(self.saved_detail_text)
        saved_actions = QHBoxLayout()
        self.use_saved_button = QPushButton("Use Selected Saved Details")
        self.use_saved_button.setEnabled(False)
        self.use_saved_button.clicked.connect(self.use_saved_details)
        saved_actions.addWidget(self.use_saved_button)
        self.refresh_saved_button = QPushButton("Refresh Saved Details")
        self.refresh_saved_button.clicked.connect(self.refresh_saved_details)
        self.refresh_saved_button.hide()
        saved_actions.addWidget(self.refresh_saved_button)
        saved_actions.addStretch(1)
        saved_layout.addLayout(saved_actions)
        self.saved_status = QLabel()
        self.saved_status.setWordWrap(True)
        self.saved_status.setTextFormat(Qt.PlainText)
        saved_layout.addWidget(self.saved_status)
        body_layout.insertWidget(0, saved_group)

        scope_group = QGroupBox("Choose image libraries")
        scope_layout = QVBoxLayout(scope_group)
        scope_hint = QLabel("Only checked, currently included libraries are searched. Existing Image Knowledge stays unchanged.")
        scope_hint.setWordWrap(True)
        scope_layout.addWidget(scope_hint)
        self.library_tree = QTreeWidget()
        self.library_tree.setHeaderLabels(("Include", "Named library", "Availability"))
        self.library_tree.setRootIsDecorated(False)
        self.library_tree.setFixedHeight(72)
        self.library_tree.setAccessibleName("Image libraries to include in Character or Outfit suggestions")
        for profile, _manager in (dataset.scoped_profile_managers()
                                  if callable(getattr(dataset, "scoped_profile_managers", None)) else ()):
            item = QTreeWidgetItem(("", profile.name,
                                    dataset.library_availability(profile.stable_id).replace("_", " ")))
            item.setData(0, Qt.UserRole, profile.stable_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked)
            self.library_tree.addTopLevelItem(item)
        self.library_tree.setColumnWidth(0, 62)
        self.library_tree.setColumnWidth(1, 370)
        scope_layout.addWidget(self.library_tree)
        self.library_tree.itemChanged.connect(self._scope_changed)

        suggestions_group = QGroupBox("Review image suggestions")
        suggestion_layout = QVBoxLayout(suggestions_group)
        prompt = QLabel("Find suggested details, then choose what to reuse. An assigned detail may come from one image; repeated description text needs multiple images. Counts refer to distinct eligible images.")
        prompt.setWordWrap(True)
        suggestion_layout.addWidget(prompt)
        actions = QHBoxLayout()
        self.find_button = QPushButton("Find Suggested Details")
        self.find_button.clicked.connect(self.find_suggestions)
        self.stop_button = QPushButton("Stop Search")
        self.stop_button.clicked.connect(self.stop_search)
        self.stop_button.hide()
        actions.addWidget(self.find_button)
        actions.addWidget(self.stop_button)
        actions.addStretch(1)
        suggestion_layout.addLayout(actions)
        self.suggestion_status = QLabel("No image evidence has been reviewed for this setup yet.")
        self.suggestion_status.setWordWrap(True)
        self.suggestion_status.setTextFormat(Qt.PlainText)
        suggestion_layout.addWidget(self.suggestion_status)
        self.starter_panel = QGroupBox("Suggested appearance starter" if owner_kind == "character"
                                       else "Suggested outfit starter")
        starter_layout = QVBoxLayout(self.starter_panel)
        self.starter_summary = QLabel()
        self.starter_summary.setWordWrap(True)
        self.starter_summary.setTextFormat(Qt.PlainText)
        starter_layout.addWidget(self.starter_summary)
        self.starter_tree = QTreeWidget()
        self.starter_tree.setHeaderLabels(("Use", "Observed together on this owner"))
        self.starter_tree.setRootIsDecorated(False)
        self.starter_tree.setFixedHeight(112)
        self.starter_tree.setColumnWidth(0, 54)
        self.starter_tree.setAccessibleName("Selectable suggested setup starter details")
        self.starter_tree.itemChanged.connect(self._update_starter_button)
        starter_layout.addWidget(self.starter_tree)
        self.use_starter_button = QPushButton("Use Suggested Setup")
        self.use_starter_button.clicked.connect(self.use_starter_setup)
        starter_layout.addWidget(self.use_starter_button, 0, Qt.AlignLeft)
        self.starter_panel.hide()
        suggestion_layout.addWidget(self.starter_panel)
        self.context_panel = QGroupBox("Recurring image ideas (optional)")
        context_layout = QVBoxLayout(self.context_panel)
        self.context_summary = QLabel()
        self.context_summary.setWordWrap(True)
        self.context_summary.setTextFormat(Qt.PlainText)
        context_layout.addWidget(self.context_summary)
        self.context_tree = QTreeWidget()
        self.context_tree.setHeaderLabels(("Use", "Prompt idea", "Images", "Source"))
        self.context_tree.setRootIsDecorated(False)
        self.context_tree.setFixedHeight(132)
        self.context_tree.setColumnWidth(0, 54)
        self.context_tree.setColumnWidth(1, 280)
        self.context_tree.setColumnWidth(2, 75)
        self.context_tree.setAccessibleName("Optional recurring image-wide prompt ideas")
        self.context_tree.itemChanged.connect(self._update_context_button)
        self.context_tree.currentItemChanged.connect(self._context_selection_changed)
        context_layout.addWidget(self.context_tree)
        self.context_review_button = QPushButton("Review Highlighted Idea Sources…")
        self.context_review_button.setEnabled(False)
        self.context_review_button.clicked.connect(self.review_context_sources)
        context_layout.addWidget(self.context_review_button, 0, Qt.AlignLeft)
        self.context_evidence_panel = QWidget()
        context_evidence_layout = QVBoxLayout(self.context_evidence_panel)
        context_evidence_layout.setContentsMargins(0, 0, 0, 0)
        self.context_evidence = QTextEdit()
        self.context_evidence.setReadOnly(True)
        self.context_evidence.setFixedHeight(92)
        self.context_evidence.setAccessibleName("Highlighted recurring idea source evidence")
        context_evidence_layout.addWidget(self.context_evidence)
        context_source_row = QHBoxLayout()
        self.context_source_choice = QComboBox()
        self.context_source_choice.setAccessibleName("Exact example source for recurring idea")
        self.context_source_choice.setToolTip("Up to three exact supporters of this filtered idea are shown.")
        context_source_row.addWidget(self.context_source_choice, 1)
        self.context_view_source_button = QPushButton("View Source Image…")
        self.context_view_source_button.clicked.connect(self.view_context_source_image)
        context_source_row.addWidget(self.context_view_source_button)
        context_evidence_layout.addLayout(context_source_row)
        self.context_source_error = QLabel()
        self.context_source_error.setWordWrap(True)
        self.context_source_error.setTextFormat(Qt.PlainText)
        self.context_source_error.hide()
        context_evidence_layout.addWidget(self.context_source_error)
        self.context_evidence_panel.hide()
        context_layout.addWidget(self.context_evidence_panel)
        context_destination = QHBoxLayout()
        self.context_destination_label = QLabel("Place checked ideas in (your prompt choice):")
        self.context_destination_choice = QComboBox()
        self.context_destination_choice.setAccessibleName("Prompt part for checked recurring image ideas")
        self.context_destination_choice.addItem("Other prompt text", "other")
        self.context_destination_choice.addItem("Character traits", "character")
        self.context_destination_choice.addItem("Outfit details", "outfit")
        self.context_destination_choice.setToolTip(
            "Choosing a prompt part does not confirm that these image-wide ideas describe this Character or Outfit."
        )
        context_destination.addWidget(self.context_destination_label)
        context_destination.addWidget(self.context_destination_choice)
        context_destination.addStretch(1)
        context_layout.addLayout(context_destination)
        self.use_context_button = QPushButton("Use Checked Ideas")
        self.use_context_button.setEnabled(False)
        self.use_context_button.clicked.connect(self.use_context_ideas)
        context_layout.addWidget(self.use_context_button, 0, Qt.AlignLeft)
        self.context_panel.hide()
        suggestion_layout.addWidget(self.context_panel)
        self.clothing_group_panel = QWidget()
        clothing_layout = QVBoxLayout(self.clothing_group_panel)
        clothing_layout.setContentsMargins(0, 0, 0, 0)
        clothing_hint = QLabel(
            "Recurring clothing details (optional): assigned together to one person "
            "in multiple images. Shared details may be only part of a look, not a complete Outfit."
        )
        clothing_hint.setWordWrap(True)
        clothing_layout.addWidget(clothing_hint)
        clothing_row = QHBoxLayout()
        self.clothing_group_choice = QComboBox()
        self.clothing_group_choice.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.clothing_group_choice.setMinimumContentsLength(28)
        self.clothing_group_choice.setAccessibleName("Choose recurring clothing details for this setup")
        self.clothing_group_choice.setToolTip(
            "Exact sets and shared clothing details are separate suggestions. Both need distinct supporting images."
        )
        self.clothing_group_choice.currentIndexChanged.connect(
            lambda _index: self.use_clothing_group_button.setEnabled(
                self.clothing_group_choice.currentData() is not None
            )
        )
        clothing_row.addWidget(self.clothing_group_choice, 1)
        self.use_clothing_group_button = QPushButton("Use These Details")
        self.use_clothing_group_button.setEnabled(False)
        self.use_clothing_group_button.clicked.connect(self.use_clothing_group)
        clothing_row.addWidget(self.use_clothing_group_button)
        clothing_layout.addLayout(clothing_row)
        self.clothing_group_panel.hide()
        suggestion_layout.addWidget(self.clothing_group_panel)
        self.coverage_details = QLabel()
        self.coverage_details.setWordWrap(True)
        self.coverage_details.setTextFormat(Qt.PlainText)
        self.coverage_details.setAccessibleName("Character or Outfit suggestion source coverage")
        self.coverage_toggle = DisclosureButton("Source coverage (optional)",
                                                "Hide source coverage", self.coverage_details)
        self.coverage_toggle.hide()
        suggestion_layout.addWidget(self.coverage_toggle, 0, Qt.AlignLeft)
        suggestion_layout.addWidget(self.coverage_details)
        self.alternative_summary = QLabel()
        self.alternative_summary.setWordWrap(True)
        self.alternative_summary.setTextFormat(Qt.PlainText)
        self.alternative_summary.hide()
        suggestion_layout.addWidget(self.alternative_summary)
        self.focus_summary = QLabel()
        self.focus_summary.setWordWrap(True)
        self.focus_summary.setTextFormat(Qt.PlainText)
        self.focus_summary.hide()
        suggestion_layout.addWidget(self.focus_summary)
        self.focus_choice = QComboBox()
        self.focus_choice.setAccessibleName("Focus suggestions on an observed Character or Outfit look")
        self.focus_choice.currentIndexChanged.connect(self._focus_changed)
        self.focus_toggle = DisclosureButton("Focus on a look (optional)",
                                             "Hide look choices", self.focus_choice)
        self.focus_toggle.setToolTip(
            "Narrow suggestions to an Outfit or appearance detail assigned to this owner. "
            "An absent tag does not mean the opposite look."
        )
        self.focus_toggle.hide()
        suggestion_layout.addWidget(self.focus_toggle, 0, Qt.AlignLeft)
        suggestion_layout.addWidget(self.focus_choice)
        self.individual_panel = QWidget()
        individual_layout = QVBoxLayout(self.individual_panel)
        individual_layout.setContentsMargins(0, 0, 0, 0)
        self.individual_toggle = DisclosureButton(
            "Choose Individual Details (optional)", "Hide Individual Details",
            self.individual_panel, self,
        )
        self.individual_toggle.hide()
        suggestion_layout.addWidget(self.individual_toggle, 0, Qt.AlignLeft)
        suggestion_layout.addWidget(self.individual_panel)
        self.suggestion_tree = QTreeWidget()
        self.suggestion_tree.setHeaderLabels(("Use", "Suggested detail", "Support", "Evidence", "Prompt part"))
        self.suggestion_tree.setRootIsDecorated(False)
        self.suggestion_tree.setFixedHeight(125)
        self.suggestion_tree.setAccessibleName("Selectable Character or Outfit setup suggestions")
        self.suggestion_tree.setColumnWidth(0, 54)
        self.suggestion_tree.setColumnWidth(1, 260)
        self.suggestion_tree.setColumnWidth(2, 85)
        self.suggestion_tree.setColumnWidth(3, 220)
        self.suggestion_tree.setColumnWidth(4, 155)
        self.suggestion_tree.hide()
        self.suggestion_tree.itemChanged.connect(self._update_use_button)
        self.suggestion_tree.currentItemChanged.connect(self._show_evidence)
        individual_layout.addWidget(self.suggestion_tree)
        self.show_one_image_details = QCheckBox()
        self.show_one_image_details.setAccessibleName("Show details found in one image")
        self.show_one_image_details.setToolTip(
            "These details may be useful, but they do not recur across the selected images."
        )
        self.show_one_image_details.toggled.connect(self._update_detail_visibility)
        self.show_one_image_details.hide()
        individual_layout.addWidget(self.show_one_image_details, 0, Qt.AlignLeft)
        destination_row = QHBoxLayout()
        self.destination_label = QLabel("Place highlighted detail in setup (prompt text, not a confirmed fact)")
        self.destination_choice = QComboBox()
        self.destination_choice.setAccessibleName("Prompt part for highlighted suggestion")
        for kind, label in _PARTS:
            self.destination_choice.addItem(label, kind)
        self.destination_choice.currentIndexChanged.connect(self._change_destination)
        destination_row.addWidget(self.destination_label)
        destination_row.addWidget(self.destination_choice)
        destination_row.addStretch(1)
        individual_layout.addLayout(destination_row)
        self.destination_label.hide()
        self.destination_choice.hide()
        self.use_details_button = QPushButton("Use Selected Details Below")
        self.use_details_button.setEnabled(False)
        self.use_details_button.clicked.connect(self.use_selected_details)
        self.use_details_button.hide()
        individual_layout.addWidget(self.use_details_button, 0, Qt.AlignLeft)
        self.evidence = QTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setFixedHeight(105)
        self.evidence.setAccessibleName("Selected suggestion source and example images")
        self.evidence_panel = QWidget()
        evidence_layout = QVBoxLayout(self.evidence_panel)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        self.source_review_row = QWidget()
        source_review_layout = QHBoxLayout(self.source_review_row)
        source_review_layout.setContentsMargins(0, 0, 0, 0)
        source_review_layout.addWidget(QLabel("Example source image"))
        self.source_choice = QComboBox()
        self.source_choice.setAccessibleName("Exact example source image for highlighted suggestion")
        self.source_choice.setToolTip("Up to three exact supporting images are shown; this is not every image counted above.")
        source_review_layout.addWidget(self.source_choice, 1)
        self.view_source_button = QPushButton("View Source Image…")
        self.view_source_button.clicked.connect(self.view_source_image)
        source_review_layout.addWidget(self.view_source_button)
        evidence_layout.addWidget(self.source_review_row)
        self.source_review_row.hide()
        self.source_error = QLabel()
        self.source_error.setWordWrap(True)
        self.source_error.setTextFormat(Qt.PlainText)
        self.source_error.hide()
        evidence_layout.addWidget(self.source_error)
        evidence_layout.addWidget(self.evidence)
        self.evidence_toggle = DisclosureButton("Evidence and Customize",
                                                "Hide Evidence", self.evidence_panel)
        self.evidence_toggle.hide()
        individual_layout.addWidget(self.evidence_toggle, 0, Qt.AlignLeft)
        individual_layout.addWidget(self.evidence_panel)
        self.image_panel = QWidget()
        image_layout = QVBoxLayout(self.image_panel)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.addWidget(scope_group)
        image_layout.addWidget(suggestions_group)
        self.refresh_saved_details(initial=True)
        has_libraries = self.library_tree.topLevelItemCount() > 0
        self._organize_setup_tasks(has_libraries)
        if not has_libraries and self.saved_tree.topLevelItemCount() == 0:
            self.saved_status.setText("No saved details or image libraries are available. You can still write a setup below.")

    def _organize_setup_tasks(self, has_libraries):
        """Keep source choices and authored settings in separate named tasks."""
        body_layout = self.setup_body_layout
        outer = self.layout()
        body_layout.removeWidget(self.setup_section_title)
        self.setup_section_title.hide()
        body_layout.removeItem(self.name_form)
        outer.insertLayout(2, self.name_form)
        for widget in (self.parts_panel, self.advanced_toggle,
                       self.advanced_toggle.target):
            body_layout.removeWidget(widget)
        outer.removeWidget(self.setup_scroll)
        self.task_tabs = QTabWidget()
        self.task_tabs.setAccessibleName("Character or Outfit setup tasks")
        self.saved_scroll = self.setup_scroll
        self.saved_scroll.setAccessibleName("Saved Character or Outfit details")
        self.task_tabs.addTab(self.saved_scroll, "Saved Details")
        self.task_tabs.setTabVisible(0, bool(self.saved_tree.topLevelItemCount()) or not has_libraries)

        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setAccessibleName("Image suggestion choices")
        image_body = QWidget()
        image_layout = QVBoxLayout(image_body)
        image_layout.addWidget(self.image_panel)
        image_layout.addStretch(1)
        self.image_scroll.setWidget(image_body)
        self.task_tabs.addTab(self.image_scroll, "Image Suggestions")
        self.task_tabs.setTabVisible(1, has_libraries)

        self.edit_scroll = QScrollArea()
        self.edit_scroll.setWidgetResizable(True)
        self.edit_scroll.setAccessibleName("Editable setup prompt and model choices")
        edit_body = QWidget()
        edit_layout = QVBoxLayout(edit_body)
        edit_layout.addWidget(self.parts_panel)
        edit_layout.addWidget(self.advanced_toggle, 0, Qt.AlignLeft)
        edit_layout.addWidget(self.advanced_toggle.target)
        edit_layout.addStretch(1)
        self.edit_scroll.setWidget(edit_body)
        self.task_tabs.addTab(self.edit_scroll, "Edit Setup")
        self.parts_panel.show()
        self.image_panel.show()

        outer.insertWidget(3, self.task_tabs, 1)
        if self.saved_tree.topLevelItemCount() == 0:
            self.task_tabs.setCurrentWidget(self.image_scroll if has_libraries else self.edit_scroll)

    def _scope(self) -> tuple[str, ...]:
        return tuple(self.library_tree.topLevelItem(index).data(0, Qt.UserRole)
                     for index in range(self.library_tree.topLevelItemCount())
                     if self.library_tree.topLevelItem(index).checkState(0) == Qt.Checked)

    def _read_saved_owner_details(self):
        if not self._authority_paths:
            return self.owner_name, (), ()
        if len(self._authority_paths) != 4:
            raise ValueError("Character or Outfit Knowledge paths are incomplete.")
        before = tuple((path, _path_fingerprint(path)) for path in self._authority_paths)
        owners = CharacterDatabase(self._authority_paths[0].parent,
                                   recover_interrupted=False)
        records = owners.characters if self.owner_kind == "character" else owners.outfits
        owner = records.get(self.owner_id)
        if owner is None:
            raise ValueError("This Character or Outfit no longer exists. Choose a current record.")
        details = saved_owner_setup_details(self.owner_kind, owner)
        after = tuple((path, _path_fingerprint(path)) for path in self._authority_paths)
        if before != after:
            raise ValueError("Character or Outfit Knowledge changed while reading saved details.")
        return owner.name, details, after

    def refresh_saved_details(self, _checked=False, *, initial=False):
        if self.save_in_progress:
            return
        checked = {
            (item.data(0, Qt.UserRole).kind, item.data(0, Qt.UserRole).text)
            for index in range(self.saved_tree.topLevelItemCount())
            if (item := self.saved_tree.topLevelItem(index)).checkState(0) == Qt.Checked
        }
        try:
            owner_name, details, fingerprints = self._read_saved_owner_details()
        except (OSError, ValueError, RuntimeError) as exc:
            self._saved_owner_ready = False
            self.saved_status.setText(f"Saved details could not be refreshed: {exc} Your authored setup text remains.")
            self.saved_group.show()
            self.refresh_saved_button.show()
            if hasattr(self, "task_tabs"):
                self.task_tabs.setTabVisible(0, True)
                self.task_tabs.setCurrentWidget(self.saved_scroll)
            self._invalidate()
            return
        if not initial and self._source_capture is not None:
            self._scope_changed()
            self.suggestion_status.setText(
                "Saved owner details were refreshed. Find More Details in Images again if you want current image evidence."
            )
        previous_default = (f"{self.owner_name} appearance" if self.owner_kind == "character"
                            else f"{self.owner_name} outfit")
        self.owner_name = owner_name
        noun = "Character" if self.owner_kind == "character" else "Outfit"
        self.heading.setText(f"Make a reusable setup from {noun}: {owner_name}")
        if self.name.text() == previous_default:
            self.name.setText(f"{owner_name} appearance" if self.owner_kind == "character"
                              else f"{owner_name} outfit")
        self._saved_detail_fingerprints = fingerprints
        self._saved_owner_ready = True
        if fingerprints and self._source_capture is None:
            self._authority_fingerprints = fingerprints
        self.saved_tree.clear()
        self.saved_detail_text.hide()
        part_names = dict(_PARTS)
        for detail in details:
            item = QTreeWidgetItem(("", detail.text, detail.source, part_names[detail.kind]))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if (detail.kind, detail.text) in checked else Qt.Unchecked)
            item.setData(0, Qt.UserRole, detail)
            item.setToolTip(1, detail.text)
            self.saved_tree.addTopLevelItem(item)
        self.saved_tree.setVisible(bool(details))
        self.use_saved_button.setVisible(bool(details))
        has_libraries = self.library_tree.topLevelItemCount() > 0
        self.saved_group.setVisible(bool(details) or not has_libraries)
        if hasattr(self, "task_tabs"):
            self.task_tabs.setTabVisible(0, bool(details) or not has_libraries)
        self.refresh_saved_button.hide()
        self.saved_status.setText(
            f"{len(details)} saved {noun.lower()} details available. Select only what belongs in this reusable setup; image support is separate."
            if details else
            "No saved details on this record. Use Image Suggestions or Edit Setup."
        )
        self._update_saved_use_button()
        if not initial:
            self._invalidate()

    def _update_saved_use_button(self, *_args):
        self.use_saved_button.setEnabled(any(
            self.saved_tree.topLevelItem(index).checkState(0) == Qt.Checked
            for index in range(self.saved_tree.topLevelItemCount())
        ))

    def _show_saved_detail(self, item, _previous):
        detail = item.data(0, Qt.UserRole) if item is not None else None
        if detail is None:
            self.saved_detail_text.hide()
            return
        self.saved_detail_text.setPlainText(
            f"{detail.source} · {dict(_PARTS)[detail.kind]}\n{detail.text}"
        )
        self.saved_detail_text.show()

    def use_saved_details(self):
        selected = [
            (item.data(0, Qt.UserRole).kind, item.data(0, Qt.UserRole).text)
            for index in range(self.saved_tree.topLevelItemCount())
            if (item := self.saved_tree.topLevelItem(index)).checkState(0) == Qt.Checked
        ]
        if not selected or not self._saved_detail_fingerprints:
            return
        self._saved_details_used = True
        self._copy_selected_text(selected, source="Saved details")

    def _load_alternative_context(self, *, should_cancel=None):
        if not self._alternative_paths:
            return None, (), ()
        if len(self._alternative_paths) != 3 or len(self._authority_paths) != 4:
            raise ValueError("Reviewed look authority paths are incomplete.")
        before = tuple((path, _path_fingerprint(path)) for path in self._alternative_paths)
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Look comparison stopped.")
        tags = TagDatabase(self._authority_paths[3])
        if getattr(tags, "_source_error", ""):
            raise ValueError("Reviewed Tags are unavailable for look comparison.")
        base = load_effective_relationship_registry(
            default_path=self._alternative_paths[0], user_path=self._alternative_paths[1],
        )
        registry = KnowledgeRelationshipRegistry(base.relationships, tags=tags)
        evaluator = RelationshipEvaluator(registry)
        qualified = qualified_look_relationship_ids(registry, self._alternative_paths[2])
        after = tuple((path, _path_fingerprint(path)) for path in self._alternative_paths)
        if before != after:
            raise ValueError("Reviewed look rules changed during review; refresh suggestions.")
        return evaluator, qualified, after

    def _scope_changed(self, *_args):
        self.stop_search()
        self._source_capture = None
        self._source_session = None
        self._active_report = None
        self._active_focus = None
        self._look_evaluator = None
        self._qualified_look_ids = ()
        self._look_alternatives = ()
        self._alternative_fingerprints = ()
        self.alternative_summary.hide()
        self.coverage_toggle.setChecked(False)
        self.coverage_toggle.hide()
        self.focus_choice.clear()
        self.focus_choice.hide()
        self.focus_toggle.hide()
        self.focus_summary.hide()
        self.suggestion_tree.clear()
        self.suggestion_tree.hide()
        self.individual_toggle.hide()
        self.individual_panel.hide()
        self.starter_tree.clear()
        self.starter_panel.hide()
        self.context_panel.hide()
        self.context_evidence_panel.hide()
        self.context_source_choice.clear()
        self.context_source_error.hide()
        self.clothing_group_choice.clear()
        self.clothing_group_panel.hide()
        self.show_one_image_details.setChecked(False)
        self.show_one_image_details.hide()
        self.destination_label.hide()
        self.destination_choice.hide()
        self.evidence_toggle.setChecked(False)
        self.evidence_toggle.hide()
        self.evidence_panel.hide()
        self.source_choice.clear()
        self.source_error.hide()
        self.use_details_button.hide()
        self.suggestion_status.setText("Image scope changed. Find suggested details again; your authored setup text remains.")
        self._invalidate()

    def find_suggestions(self):
        if self._review_task is not None or self.save_in_progress:
            return
        scope = self._scope()
        if not scope:
            self.suggestion_status.setText("Check at least one named library, then find suggested details.")
            return
        self._source_capture = None
        self._source_session = None
        self._active_report = None
        self._active_focus = None
        self._look_evaluator = None
        self._qualified_look_ids = ()
        self._look_alternatives = ()
        self._alternative_fingerprints = ()
        self.alternative_summary.hide()
        self.coverage_toggle.setChecked(False)
        self.coverage_toggle.hide()
        self.focus_choice.clear()
        self.focus_choice.hide()
        self.focus_toggle.hide()
        self.focus_summary.hide()
        self.suggestion_tree.clear()
        self.suggestion_tree.hide()
        self.individual_toggle.hide()
        self.individual_panel.hide()
        self.starter_tree.clear()
        self.starter_panel.hide()
        self.context_panel.hide()
        self.context_evidence_panel.hide()
        self.context_source_choice.clear()
        self.context_source_error.hide()
        self.clothing_group_choice.clear()
        self.clothing_group_panel.hide()
        self.show_one_image_details.setChecked(False)
        self.show_one_image_details.hide()
        self.destination_label.hide()
        self.destination_choice.hide()
        self.evidence_toggle.setChecked(False)
        self.evidence_toggle.hide()
        self.evidence_panel.hide()
        self.source_choice.clear()
        self.source_error.hide()
        self.use_details_button.hide()
        self.find_button.setEnabled(False)
        self.stop_button.show()
        self.suggestion_status.setText("Reading current Image Knowledge in the selected libraries…")
        self._invalidate()

        def work(job):
            owner_context = self._current_owner_context()
            sources, capture = load_suggestion_sources(
                self.dataset, scope, should_cancel=lambda: job.cancel_requested,
                progress=lambda done, total: job.update(done, total,
                    f"Reviewing image evidence ({done:,} checked)"),
            )
            report = suggest_character_outfit_setup(
                self.owner_kind, self.owner_id, sources,
                included_library_ids=scope,
                representative_tag_id=owner_context[1],
                representative_names=owner_context[2],
                should_cancel=lambda: job.cancel_requested,
            )
            if any(_path_fingerprint(path) != fingerprint
                   for path, fingerprint in owner_context[3]):
                raise ValueError("Character or Outfit Knowledge changed during review; refresh suggestions.")
            catalog = suggestion_focus_options(
                self.owner_kind, self.owner_id, sources,
                included_library_ids=scope,
                should_cancel=lambda: job.cancel_requested,
            )
            look_error = ""
            try:
                evaluator, qualified, fingerprints = self._load_alternative_context(
                    should_cancel=lambda: job.cancel_requested,
                )
                alternatives = (reviewed_look_alternatives(
                    self.owner_kind, self.owner_id, sources,
                    included_library_ids=scope, evaluator=evaluator,
                    qualified_relationship_ids=qualified,
                    should_cancel=lambda: job.cancel_requested,
                ) if qualified else ())
            except InterruptedError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                evaluator, qualified, fingerprints, alternatives = None, (), (), ()
                look_error = str(exc) or "Look comparison unavailable."
            if any(_path_fingerprint(path) != fingerprint
                   for path, fingerprint in owner_context[3]):
                raise ValueError("Character or Outfit Knowledge changed during review; refresh suggestions.")
            return report, capture, owner_context, sources, catalog, evaluator, qualified, fingerprints, alternatives, look_error

        task = CoordinatedCallTask(self.jobs, "Review Character or Outfit image evidence",
                                   work, resource="io", key=f"generation-owner-suggestions:{id(self)}")
        self._review_task = task
        self._review_mode = "find"
        task.signals.complete.connect(lambda result, active=task: self._found(active, result))
        task.signals.failed.connect(lambda error, active=task: self._find_failed(active, str(error)))
        task.signals.cancelled.connect(lambda active=task: self._find_failed(active, "Search stopped."))
        task.signals.progress.connect(lambda done, total, message: self.suggestion_status.setText(message))
        try:
            task.start()
        except Exception as exc:
            self._find_failed(task, str(exc) or "The evidence search could not start.")

    def stop_search(self):
        if self._review_task is None:
            return
        self._review_task.cancel()
        self._review_task = None
        mode = self._review_mode
        self._review_mode = None
        self.find_button.setEnabled(True)
        self.stop_button.hide()
        self.focus_choice.setEnabled(True)
        if mode == "focus" and self._active_report is not None:
            self._restore_focus_choice()
            self._render_report(self._active_report)
            self.alternative_summary.setVisible(
                self._active_focus is None and bool(self._look_alternatives))
            self.focus_summary.setText("Look change stopped. The previous suggestions are still shown; authored setup text is unchanged.")
        else:
            self.suggestion_status.setText("Search stopped. Authored setup text is unchanged; choose Find Suggested Details to retry.")

    def _find_failed(self, task, error):
        if self._review_task is not task:
            return
        self._review_task = None
        mode = self._review_mode
        self._review_mode = None
        self.find_button.setEnabled(True)
        self.stop_button.hide()
        self.focus_choice.setEnabled(True)
        if mode == "focus" and self._active_report is not None:
            self._restore_focus_choice()
            self._render_report(self._active_report)
            self.alternative_summary.setVisible(
                self._active_focus is None and bool(self._look_alternatives))
            self.focus_summary.setText(f"Could not change look: {error} Previous suggestions remain; authored text is unchanged.")
        else:
            self.suggestion_status.setText(f"No suggestion review was completed: {error} Authored setup text is unchanged.")

    def _found(self, task, result):
        if self._review_task is not task:
            return
        self._review_task = None
        self._review_mode = None
        self.find_button.setEnabled(True)
        self.stop_button.hide()
        capture = result[1]
        if capture.included_library_ids != self._scope():
            self.suggestion_status.setText(
                "Image scope changed during review. Find suggested details again; authored text remains."
            )
            return
        (report, capture, owner_context, sources, catalog, evaluator, qualified,
         fingerprints, alternatives, look_error) = result
        self._source_capture = capture
        self._source_session = (sources, report)
        self._active_report = report
        self._active_focus = None
        self._look_evaluator = evaluator
        self._qualified_look_ids = qualified
        self._alternative_fingerprints = fingerprints
        self._look_alternatives = alternatives
        previous_default = (f"{self.owner_name} appearance" if self.owner_kind == "character"
                            else f"{self.owner_name} outfit")
        self.owner_name, self.representative_tag_id, self.representative_names, fingerprints, outfit_names = owner_context
        noun = "Character" if self.owner_kind == "character" else "Outfit"
        self.heading.setText(f"Make a reusable setup from {noun}: {self.owner_name}")
        self._authority_fingerprints = fingerprints
        current_default = (f"{self.owner_name} appearance" if self.owner_kind == "character"
                           else f"{self.owner_name} outfit")
        if self.name.text() == previous_default:
            self.name.setText(current_default)
        self.focus_choice.blockSignals(True)
        self.focus_choice.clear()
        self.focus_choice.addItem("All observed details", None)
        for group in alternatives:
            for branch in group.branches:
                unit = "image" if branch.supporting_images == 1 else "images"
                self.focus_choice.addItem(
                    f"{_look_group_label(group)}: {branch.label} ({branch.supporting_images:,} {unit})",
                    SuggestionFocus("reviewed_branch", branch.root_id, group.relationship_id),
                )
        for option in catalog.options:
            label = (outfit_names.get(option.focus.value, option.text)
                     if option.focus.mode == "character_outfit" else option.text)
            kind = "Outfit" if option.focus.mode == "character_outfit" else option.focus.group.title()
            unit = "image" if option.supporting_images == 1 else "images"
            self.focus_choice.addItem(f"{kind}: {label} ({option.supporting_images:,} {unit})", option.focus)
        self.focus_choice.setCurrentIndex(0)
        self.focus_choice.blockSignals(False)
        self.focus_toggle.setVisible(bool(catalog.options or alternatives))
        self.focus_summary.hide()
        if catalog.options or alternatives:
            omitted = (f" {catalog.omitted_options:,} rarer look choices are not listed; "
                       "use All observed details and customize below for those."
                       if catalog.omitted_options else "")
            self.focus_summary.setText("Across all observed images. Optionally focus on a named Outfit or a detail assigned to this owner. A missing tag does not mean the opposite look." + omitted)
        if alternatives:
            labels = "; ".join(
                f"{_look_group_label(group)}: " + " or ".join(branch.label for branch in group.branches)
                + (f" ({group.unresolved_images:,} unresolved images excluded)"
                   if group.unresolved_images else "")
                for group in alternatives
            )
            self.alternative_summary.setText(
                f"Different observed looks: {labels}. Choose a look under Focus on a look, "
                "or keep all observed details. No look is selected automatically."
            )
            self.alternative_summary.show()
        elif look_error:
            self.alternative_summary.setText(
                f"Look comparison unavailable ({look_error}). You can still review individual details."
            )
            self.alternative_summary.show()
        else:
            self.alternative_summary.hide()
        self._render_report(report)

    def _render_report(self, report):
        """Show a completed report; selection is intentionally reset per look."""
        noun = "Character" if self.owner_kind == "character" else "Outfit"
        proposal = report.starter_proposal
        # A reviewed conflict needs a deliberate look choice. An arbitrary
        # aggregate of the branches is not a safe beginner default.
        if self._look_alternatives and self._active_focus is None:
            proposal = None
        self.starter_tree.clear()
        if proposal is not None:
            for detail in proposal.details:
                item = QTreeWidgetItem(("", detail))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked)
                self.starter_tree.addTopLevelItem(item)
            self.starter_tree.setFixedHeight(
                min(128, max(56, 28 + 27 * self.starter_tree.topLevelItemCount()))
            )
            noun_lower = "appearance" if self.owner_kind == "character" else "clothing"
            self.starter_summary.setText(
                f"These {noun_lower} details were assigned together to this {noun} in "
                f"{proposal.supporting_images:,}/{proposal.eligible_images:,} eligible images. "
                "Uncheck anything you do not want. Use copies text into the editable setup; "
                "it does not change source facts or generate an image."
            )
        self.starter_panel.setVisible(proposal is not None)
        self._update_starter_button()
        self.context_tree.clear()
        self.context_evidence_panel.hide()
        self.context_source_choice.clear()
        self.context_source_error.hide()
        context_ready = (proposal is None and bool(report.image_context_ideas)
                         and not (self._look_alternatives and self._active_focus is None))
        if context_ready:
            for idea in report.image_context_ideas:
                source = {"image_tag": "Image tag", "description": "Description",
                          "description_excerpt": "Description phrase"}[idea.source_type]
                item = QTreeWidgetItem(("", idea.text,
                                        f"{idea.supporting_images}/{idea.eligible_images}", source))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                item.setData(0, Qt.UserRole, idea)
                item.setToolTip(1, idea.text)
                self.context_tree.addTopLevelItem(item)
            excluded = (f" Excluded from these counts: {report.context_excluded_images:,} "
                        "known multi-person or duplicate image entries."
                        if report.context_excluded_images else "")
            self.context_summary.setText(
                f"No recurring owner-assigned starter is available for {self.owner_name}. "
                "These words recur in images containing this name or Outfit, but may describe "
                "another person or the scene. Each count is separate; the ideas need not occur "
                "together. Check only what you want, then edit before saving." + excluded
            )
        self.context_panel.setVisible(context_ready)
        self._context_selection_changed(self.context_tree.currentItem())
        self._update_context_button()
        self.clothing_group_choice.clear()
        self.clothing_group_choice.addItem("Choose recurring clothing details…", None)
        for group in report.clothing_groups:
            members = ", ".join(group.details)
            kind = "Exact set" if group.complete_set else "Shared details"
            self.clothing_group_choice.addItem(
                f"{kind}: {members} ({group.supporting_images}/{group.eligible_images} images)", group,
            )
            self.clothing_group_choice.setItemData(
                self.clothing_group_choice.count() - 1,
                f"{members}\nAssigned together to the same owner in distinct images. "
                + ("Exact repeated set. " if group.complete_set else "Shared subset; other clothing may vary. ")
                + f"Examples: {', '.join(group.examples) or 'none retained'}",
                Qt.ToolTipRole,
            )
        self.use_clothing_group_button.setEnabled(False)
        self.clothing_group_panel.setVisible(bool(report.clothing_groups))
        self.suggestion_tree.clear()
        for suggestion in report.suggestions:
            source = {"owner_tag": f"Assigned to this {noun}",
                      "image_tag": "Image-wide co-occurrence",
                      "description": "Image description",
                      "description_excerpt": "Recurring description excerpt"}[suggestion.source_type]
            part_label = dict(_PARTS)[suggestion.kind]
            item = QTreeWidgetItem(("", suggestion.text,
                                    f"{suggestion.supporting_images}/{suggestion.eligible_images}",
                                    source, part_label))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            item.setData(0, Qt.UserRole, suggestion)
            item.setData(0, Qt.UserRole + 1, suggestion.kind)
            item.setToolTip(1, suggestion.text)
            self.suggestion_tree.addTopLevelItem(item)
        self.suggestion_tree.setVisible(bool(report.suggestions))
        self.individual_toggle.setVisible((proposal is not None or context_ready)
                                          and bool(report.suggestions))
        self.individual_toggle.setChecked(proposal is None and not context_ready)
        self.individual_panel.setVisible(bool(report.suggestions) and proposal is None
                                         and not context_ready)
        single_image = sum(row.supporting_images == 1 for row in report.suggestions)
        recurring = len(report.suggestions) - single_image
        offer_filter = report.eligible_images > 1 and single_image > 0 and recurring > 0
        self.show_one_image_details.setText(
            f"Show details found in one image ({single_image})"
        )
        self.show_one_image_details.setVisible(offer_filter)
        self._update_detail_visibility()
        self.destination_label.hide()
        self.destination_choice.hide()
        self.evidence_toggle.setVisible(bool(report.suggestions))
        self.use_details_button.setVisible(bool(report.suggestions))
        coverage = (f"{report.eligible_images:,} distinct eligible images; "
                    f"{report.owner_assigned_images:,} with direct owner assignments; "
                    f"{report.ambiguous_images:,} ownership-ambiguous; "
                    f"{report.stale_images:,} stale, {report.unavailable_images:,} unavailable "
                    "in the selected indexed scope.")
        if report.duplicate_copies:
            coverage += f" {report.duplicate_copies:,} recorded duplicate copies counted once."
        if report.omitted_suggestions:
            coverage += f" {report.omitted_suggestions:,} lower-ranked details omitted by the review limit."
        if report.omitted_clothing_groups:
            coverage += f" {report.omitted_clothing_groups:,} less-common clothing suggestions omitted by the review limit."
        if report.clothing_vocabulary_limited:
            coverage += " Shared clothing combinations used only the 48 most-observed clothing details."
        if report.long_clothing_images:
            coverage += (f" {report.long_clothing_images:,} images had clothing sets too long "
                         "or otherwise unsafe to propose as one group.")
        self.coverage_details.setText(coverage)
        self.coverage_toggle.show()
        image_noun = "image" if report.eligible_images == 1 else "images"
        summary = f"{report.eligible_images:,} eligible {image_noun} reviewed. "
        if report.stale_images or report.unavailable_images or report.ambiguous_images:
            summary += "Some evidence needs care; see Source coverage. "
        summary += ("Check details to reuse; image-wide text is not a confirmed trait of this owner."
                    if report.suggestions else
                    "No suggested details were found. You can still author a setup below.")
        self.suggestion_status.setText(summary)
        for index in range(self.suggestion_tree.topLevelItemCount()):
            item = self.suggestion_tree.topLevelItem(index)
            if not item.isHidden():
                self.suggestion_tree.setCurrentItem(item)
                self._show_evidence(item)
                break

    def _update_starter_button(self, *_args):
        self.use_starter_button.setEnabled(
            self._source_capture is not None and self._review_task is None and
            any(self.starter_tree.topLevelItem(index).checkState(0) == Qt.Checked
                for index in range(self.starter_tree.topLevelItemCount()))
        )

    def use_starter_setup(self):
        if not self.use_starter_button.isEnabled() or self._source_capture is None:
            return
        selected = [("character" if self.owner_kind == "character" else "outfit",
                     self.starter_tree.topLevelItem(index).text(1))
                    for index in range(self.starter_tree.topLevelItemCount())
                    if self.starter_tree.topLevelItem(index).checkState(0) == Qt.Checked]
        if not selected:
            return
        self._image_details_used = True
        self._copy_selected_text(selected, source="Suggested setup details")

    def _update_context_button(self, *_args):
        self.use_context_button.setEnabled(
            self._source_capture is not None and self._review_task is None and
            any(self.context_tree.topLevelItem(index).checkState(0) == Qt.Checked
                for index in range(self.context_tree.topLevelItemCount()))
        )

    def use_context_ideas(self):
        if not self.use_context_button.isEnabled() or self._source_capture is None:
            return
        destination = self.context_destination_choice.currentData()
        if destination not in {"other", "character", "outfit"}:
            return
        selected = [(destination, self.context_tree.topLevelItem(index).data(0, Qt.UserRole).text)
                    for index in range(self.context_tree.topLevelItemCount())
                    if self.context_tree.topLevelItem(index).checkState(0) == Qt.Checked]
        if not selected:
            return
        self._image_details_used = True
        self._copy_selected_text(selected, source="Recurring image ideas")

    def use_clothing_group(self):
        group = self.clothing_group_choice.currentData()
        if group is None or self._source_capture is None or self._review_task is not None:
            return
        field = self.part_fields["outfit"]
        existing = field.text()
        known = {normalize_tag(part) for part in existing.split(",") if part.strip()}
        additions = [part for part in group.details if normalize_tag(part) not in known]
        proposed = (existing + (", " if additions else "") if existing.strip() else "")
        proposed += ", ".join(additions)
        if len(proposed) > 4096:
            self.suggestion_status.setText(
                "These clothing details would make Outfit details too long. Shorten that text and try again."
            )
            return
        self.task_tabs.setCurrentWidget(self.edit_scroll)
        self._image_details_used = True
        field.setText(proposed)
        field.setFocus()
        self.status.setText(
            "Clothing details copied into editable Outfit details. Review before saving; no Outfit or image fact was changed."
        )
        QTimer.singleShot(0, lambda: self._show_authored_field(field))

    def _update_detail_visibility(self, *_args):
        """Keep checked details visible while simplifying a recurring shortlist."""
        filter_one_image = (not self.show_one_image_details.isHidden()
                            and not self.show_one_image_details.isChecked())
        for index in range(self.suggestion_tree.topLevelItemCount()):
            item = self.suggestion_tree.topLevelItem(index)
            suggestion = item.data(0, Qt.UserRole)
            item.setHidden(bool(filter_one_image and suggestion is not None
                                and suggestion.supporting_images == 1
                                and item.checkState(0) != Qt.Checked))
        current = self.suggestion_tree.currentItem()
        if current is not None and current.isHidden():
            for index in range(self.suggestion_tree.topLevelItemCount()):
                item = self.suggestion_tree.topLevelItem(index)
                if not item.isHidden():
                    self.suggestion_tree.setCurrentItem(item)
                    break

    def _restore_focus_choice(self):
        self.focus_choice.blockSignals(True)
        index = self.focus_choice.findData(self._active_focus)
        self.focus_choice.setCurrentIndex(max(0, index))
        self.focus_choice.blockSignals(False)

    def _focus_changed(self, _index):
        if self._source_session is None or self._review_task is not None:
            return
        focus = self.focus_choice.currentData()
        if focus == self._active_focus:
            return
        sources, default_report = self._source_session
        if focus is None:
            self._active_focus = None
            self._active_report = default_report
            self._render_report(default_report)
            self.focus_summary.hide()
            self.alternative_summary.setVisible(bool(self._look_alternatives))
            return
        self.alternative_summary.hide()
        self.find_button.setEnabled(False)
        self.focus_choice.setEnabled(False)
        self.stop_button.show()
        self.focus_summary.show()
        self.focus_summary.setText("Focusing on the selected look from the reviewed images…")
        scope = self._source_capture.included_library_ids

        def work(job):
            return suggest_character_outfit_setup(
                self.owner_kind, self.owner_id, sources,
                included_library_ids=scope,
                representative_tag_id=self.representative_tag_id,
                representative_names=self.representative_names,
                focus=focus, evaluator=self._look_evaluator,
                qualified_relationship_ids=self._qualified_look_ids,
                should_cancel=lambda: job.cancel_requested,
            )

        task = CoordinatedCallTask(self.jobs, "Focus reviewed Character or Outfit evidence",
                                   work, resource="cpu", key=f"generation-owner-focus:{id(self)}")
        self._review_task = task
        self._review_mode = "focus"
        task.signals.complete.connect(lambda result, active=task, chosen=focus: self._focus_found(active, chosen, result))
        task.signals.failed.connect(lambda error, active=task: self._find_failed(active, str(error)))
        task.signals.cancelled.connect(lambda active=task: self._find_failed(active, "Look change stopped."))
        try:
            task.start()
        except Exception as exc:
            self._find_failed(task, str(exc) or "The look review could not start.")

    def _focus_found(self, task, focus, report):
        if self._review_task is not task:
            return
        self._review_task = None
        self._review_mode = None
        self.find_button.setEnabled(True)
        self.focus_choice.setEnabled(True)
        self.stop_button.hide()
        self._active_focus = focus
        self._active_report = report
        self._render_report(report)
        self.focus_summary.setText(
            f"Focused on {self.focus_choice.currentText()}: {report.eligible_images:,} matching distinct images. "
            "Only exact owner assignments establish this look; image-wide text is context. "
            "Previous suggestion checks were cleared, but authored setup text is unchanged."
        )

    def _show_evidence(self, item, _previous=None):
        suggestion = item.data(0, Qt.UserRole) if item is not None else None
        self.source_choice.clear()
        self.source_error.hide()
        if suggestion is None:
            self.evidence.clear()
            self.source_review_row.hide()
            self.destination_choice.setEnabled(False)
            self.destination_label.hide()
            self.destination_choice.hide()
            return
        destination = item.data(0, Qt.UserRole + 1)
        self.destination_choice.blockSignals(True)
        self.destination_choice.setCurrentIndex(self.destination_choice.findData(destination))
        self.destination_choice.blockSignals(False)
        can_place = suggestion.source_type != "owner_tag"
        self.destination_choice.setEnabled(can_place)
        self.destination_label.setVisible(can_place)
        self.destination_choice.setVisible(can_place)
        source = {"owner_tag": "Per-image assignment to this selected owner",
                  "image_tag": "Confirmed on the image; owner not established",
                  "description": "Confirmed image description; owner not established",
                  "description_excerpt": "Exact recurring image-description text; owner not established"}[suggestion.source_type]
        self.evidence.setPlainText(
            f"{suggestion.text}\n{source}. Supported by "
            f"{suggestion.supporting_images:,} of {suggestion.eligible_images:,} eligible distinct images.\n"
            "Example filenames (up to three, not all supporters): " +
            (", ".join(suggestion.examples) or "none retained") +
            ("\nSource descriptions:\n" + "\n".join(suggestion.source_examples)
             if suggestion.source_examples else "")
        )
        for source_ref in suggestion.source_refs:
            profile_for = getattr(self.dataset, "profile", lambda _library_id: None)
            profile = profile_for(source_ref.library_id)
            library_name = profile.name if profile is not None else source_ref.library_id
            self.source_choice.addItem(f"{library_name} — {source_ref.label}", source_ref)
        self.source_review_row.setVisible(bool(suggestion.source_refs))

    def _context_selection_changed(self, item, _previous=None):
        idea = item.data(0, Qt.UserRole) if item is not None else None
        self.context_review_button.setEnabled(idea is not None)
        if self.context_evidence_panel.isVisible():
            self._populate_context_evidence(idea)

    def review_context_sources(self):
        item = self.context_tree.currentItem()
        idea = item.data(0, Qt.UserRole) if item is not None else None
        if idea is None:
            return
        self._populate_context_evidence(idea)
        self.context_evidence_panel.show()
        def reveal_source_action():
            if self.context_evidence_panel.isVisible():
                self.image_scroll.ensureWidgetVisible(
                    self.context_view_source_button, 0, 0,
                )
        QTimer.singleShot(0, reveal_source_action)

    def _populate_context_evidence(self, idea):
        self.context_source_choice.clear()
        self.context_source_error.hide()
        if idea is None:
            self.context_evidence.clear()
            self.context_evidence_panel.hide()
            return
        source = {"image_tag": "Image tag", "description": "Image description",
                  "description_excerpt": "Exact repeated description phrase"}[idea.source_type]
        self.context_evidence.setPlainText(
            f"{idea.text}\n{source}; present in {idea.supporting_images:,} of "
            f"{idea.eligible_images:,} eligible distinct images. Known ambiguous or "
            "duplicate sources did not count as supporters. This does not establish that the idea "
            "describes this Character or Outfit.\n"
            "Example filenames (up to three, not all supporters): "
            + (", ".join(idea.examples) or "none retained")
            + ("\nOriginal descriptions:\n" + "\n".join(idea.source_examples)
               if idea.source_examples else "")
        )
        for source_ref in idea.source_refs:
            profile_for = getattr(self.dataset, "profile", lambda _library_id: None)
            profile = profile_for(source_ref.library_id)
            library_name = profile.name if profile is not None else source_ref.library_id
            self.context_source_choice.addItem(f"{library_name} — {source_ref.label}", source_ref)
        self.context_source_choice.setEnabled(bool(idea.source_refs))
        self.context_view_source_button.setEnabled(bool(idea.source_refs))

    def view_context_source_image(self):
        item = self.context_tree.currentItem()
        idea = item.data(0, Qt.UserRole) if item is not None else None
        source_ref = self.context_source_choice.currentData()
        if idea is None or source_ref not in idea.source_refs:
            return
        self._open_source_ref(idea, source_ref, self.context_source_choice.currentText(),
                              self.context_source_error)

    def view_source_image(self):
        item = self.suggestion_tree.currentItem()
        suggestion = item.data(0, Qt.UserRole) if item is not None else None
        source_ref = self.source_choice.currentData()
        if suggestion is None or source_ref not in suggestion.source_refs:
            return
        self._open_source_ref(suggestion, source_ref, self.source_choice.currentText(),
                              self.source_error)

    def _open_source_ref(self, suggestion, source_ref, label, error_label):
        if self._source_preview_dialog is not None and self._source_preview_dialog.isVisible():
            self._source_preview_dialog.raise_()
            return
        try:
            path = recheck_suggestion_preview_source(
                self.dataset, self._source_capture, source_ref,
                selected_library_ids=self._scope(),
            )
        except (OSError, ValueError) as exc:
            self._show_source_preview_error(exc, error_label)
            return
        error_label.hide()
        preview = SuggestionSourcePreviewDialog(
            path, label=label,
            source_type=suggestion.source_type, settings=self.settings, parent=self,
        )
        try:
            recheck_suggestion_preview_source(
                self.dataset, self._source_capture, source_ref,
                selected_library_ids=self._scope(),
            )
        except (OSError, ValueError) as exc:
            preview.deleteLater()
            self._show_source_preview_error(exc, error_label)
            return
        preview.setModal(True)
        self._source_preview_dialog = preview
        preview.finished.connect(lambda _result: setattr(self, "_source_preview_dialog", None))
        preview.open()

    def _show_source_preview_error(self, error, target=None):
        target = target or self.source_error
        target.setText(f"Source image not opened: {error} Your setup text is unchanged. "
                       "Choose Find Suggested Details to refresh the source review.")
        target.show()
        self.task_tabs.setCurrentWidget(self.image_scroll)
        self.image_scroll.ensureWidgetVisible(target)

    def _change_destination(self, _index):
        item = self.suggestion_tree.currentItem()
        suggestion = item.data(0, Qt.UserRole) if item is not None else None
        if suggestion is None or suggestion.source_type == "owner_tag":
            return
        destination = self.destination_choice.currentData()
        if destination not in self.part_fields:
            return
        item.setData(0, Qt.UserRole + 1, destination)
        item.setText(4, dict(_PARTS)[destination])

    def _update_use_button(self, *_args):
        self._update_detail_visibility()
        self.use_details_button.setEnabled(any(
            self.suggestion_tree.topLevelItem(index).checkState(0) == Qt.Checked
            for index in range(self.suggestion_tree.topLevelItemCount())
        ))

    def use_selected_details(self):
        selected = [(self.suggestion_tree.topLevelItem(index).data(0, Qt.UserRole + 1),
                     self.suggestion_tree.topLevelItem(index).data(0, Qt.UserRole).text)
                    for index in range(self.suggestion_tree.topLevelItemCount())
                    if self.suggestion_tree.topLevelItem(index).checkState(0) == Qt.Checked]
        if not selected or self._source_capture is None:
            return
        self._image_details_used = True
        self._copy_selected_text(selected, source="Selected details")

    def _copy_selected_text(self, selected, *, source):
        self.task_tabs.setCurrentWidget(self.edit_scroll)
        first_changed = None
        too_long = []
        for kind in self.part_fields:
            additions = [text for destination, text in selected if destination == kind]
            if not additions:
                continue
            field = self.part_fields[kind]
            existing = field.text().strip()
            # Compare complete comma-separated entries, not substrings of authored
            # text: "silver hair" must not disappear inside "silver hairband".
            known = {normalize_tag(part) for part in existing.split(",") if part.strip()}
            known.add(normalize_tag(existing))
            new = []
            for value in additions:
                key = normalize_tag(value)
                if key and key not in known:
                    new.append(value)
                    known.add(key)
            if not new:
                continue
            proposed = ", ".join(part for part in (existing, *new) if part)
            if len(proposed) <= 4096:
                field.setText(proposed)
                if first_changed is None:
                    first_changed = field
            else:
                too_long.append(kind.title())
        if too_long:
            self.status.setText(
                f"Could not add selected details to {', '.join(too_long)}: the text would be too long. "
                "Shorten those parts and try again; other edits remain."
            )
        elif first_changed is None:
            self.status.setText(f"{source} are already in the editable setup parts; no source fact was changed.")
        else:
            self.status.setText(f"{source} were copied into editable setup parts. Review the setup before saving; no source fact was changed.")
        if first_changed is not None:
            first_changed.setFocus()
            QTimer.singleShot(0, lambda field=first_changed: self._show_authored_field(field))

    def _show_authored_field(self, field):
        from shiboken6 import isValid
        if isValid(self) and isValid(field) and field.isVisible():
            self.task_tabs.setCurrentWidget(self.edit_scroll)
            self.edit_scroll.ensureWidgetVisible(field)

    def _check_sources_before_save(self, *, should_cancel=None):
        if not self._saved_owner_ready:
            raise ValueError("Saved Character or Outfit details are unavailable; refresh saved details.")
        if self._saved_details_used:
            for path, expected in self._saved_detail_fingerprints:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Setup source recheck cancelled.")
                if _path_fingerprint(path) != expected:
                    raise ValueError("Saved Character or Outfit details changed; refresh saved details.")
        if self._image_details_used and self._source_capture is not None:
            recheck_suggestion_sources(self.dataset, self._source_capture,
                                       should_cancel=should_cancel)
        for path, expected in self._authority_fingerprints:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Setup source recheck cancelled.")
            if _path_fingerprint(path) != expected:
                if self._source_capture is None:
                    raise ValueError("Character or Outfit Knowledge changed; refresh saved details.")
                raise ValueError("Character or Outfit Knowledge changed; refresh suggestions.")
        if self._image_details_used:
            for path, expected in self._alternative_fingerprints:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Setup source recheck cancelled.")
                if _path_fingerprint(path) != expected:
                    raise ValueError("Reviewed look rules changed; refresh suggestions.")

    def _current_owner_context(self):
        """Read the exact current owner/tag relationship before source analysis."""
        if not self._authority_paths:
            return (self.owner_name, self.representative_tag_id,
                    self.representative_names, self._authority_fingerprints, {})
        if len(self._authority_paths) != 4:
            raise ValueError("Character or Outfit Knowledge paths are incomplete.")
        before = tuple((path, _path_fingerprint(path)) for path in self._authority_paths)
        owners = CharacterDatabase(self._authority_paths[0].parent,
                                   recover_interrupted=False)
        records = owners.characters if self.owner_kind == "character" else owners.outfits
        owner = records.get(self.owner_id)
        if owner is None:
            raise ValueError("This Character or Outfit no longer exists. Keep your text and select a current record.")
        tag_id = owners.entity_tag_id(self.owner_kind, self.owner_id)
        tags = TagDatabase(self._authority_paths[3])
        tag = tags.tags.get(tag_id)
        names = (tag.display, *tag.aliases) if tag is not None else ()
        after = tuple((path, _path_fingerprint(path)) for path in self._authority_paths)
        if before != after:
            raise ValueError("Character or Outfit Knowledge changed during review; refresh suggestions.")
        return owner.name, tag_id, names, after, {
            outfit_id: outfit.name for outfit_id, outfit in owners.outfits.items()
        }

    def review(self):
        if not self._saved_owner_ready:
            self.status.setText("Saved Character or Outfit details are unavailable. Choose Refresh Saved Details before reviewing; your text remains.")
            self.task_tabs.setCurrentWidget(self.saved_scroll)
            return
        if self._image_details_used and self._source_capture is None:
            self.status.setText(
                "Copied image suggestions need a fresh Find Suggested Details review before saving. "
                "Your editable text remains; recheck the image scope, then Review Setup again."
            )
            self.task_tabs.setCurrentWidget(self.image_scroll)
            return
        super().review()

    def _save_failed(self, task, error):
        super()._save_failed(task, error)
        if "refresh saved details" in error:
            self._invalidate()
            self.refresh_saved_button.show()
            self.task_tabs.setTabVisible(0, True)
            self.task_tabs.setCurrentWidget(self.saved_scroll)
            self.status.setText(error + " Your authored text remains. Choose Refresh Saved Details, then Review Setup again.")
        elif "refresh suggestions" in error:
            self._invalidate()
            self.task_tabs.setCurrentWidget(self.image_scroll)
            self.status.setText(error + " Your authored text and choices remain. Choose Find Suggested Details, review the new evidence, then Review Setup again.")

    def reject(self):
        self.stop_search()
        self._source_session = None
        super().reject()

    def closeEvent(self, event):
        self.stop_search()
        self._source_session = None
        super().closeEvent(event)
