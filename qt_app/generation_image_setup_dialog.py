"""Review one image's recorded evidence into a reusable Generation Setup."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QThreadPool, QSize, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.generation_recipes import SHA, make_image_inspired_recipe
from .components import DisclosureButton
from .gallery import ProtectedImageView, content_safety_policy
from .generation_setup_save import SetupSaveDialog
from .workers import AsyncCallTask


class _SavedTextPartDialog(QDialog):
    """Edit a copy of reviewed image text into one reusable prompt part."""

    def __init__(self, text, source, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Use Saved Text as Prompt Part")
        self.resize(640, 390)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            f"Saved {source} is unchanged. Keep only the words you want in future images, "
            "then choose what they describe."
        )
        explanation.setTextFormat(Qt.PlainText)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        original = QTextEdit()
        original.setReadOnly(True)
        original.setMaximumHeight(90)
        original.setAccessibleName("Original saved image text")
        original.setPlainText(text)
        layout.addWidget(original)
        layout.addWidget(QLabel("Editable copy"))
        self.edited_text = QTextEdit()
        self.edited_text.setAcceptRichText(False)
        self.edited_text.setAccessibleName("Text to reuse in future images")
        self.edited_text.setPlainText(text)
        layout.addWidget(self.edited_text)
        self.purpose = QComboBox()
        self.purpose.setAccessibleName("What the saved text describes")
        for label, kind in (("Character traits", "character"), ("Scene elements", "scene"),
                            ("Pose or action", "pose"), ("Style", "style"),
                            ("Other or unclassified", "other")):
            self.purpose.addItem(label, kind)
        layout.addWidget(QLabel("Use as"))
        layout.addWidget(self.purpose)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.use_button = self.buttons.addButton("Use Edited Copy", QDialogButtonBox.AcceptRole)
        self.use_button.clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.edited_text.textChanged.connect(self._update_use)
        self._update_use()

    def _update_use(self):
        length = len(self.edited_text.toPlainText().strip())
        self.use_button.setEnabled(0 < length <= 4096)


class ImageGenerationSetupDialog(SetupSaveDialog):
    """Choose recorded settings and saved text; never analyze pixels implicitly."""

    feedback_workflow = "Create generation setup from image"
    setup_requested = Signal()

    def __init__(self, records, fragments, parent=None, *, jobs=None, settings=None,
                 suggestion_source=None, source_identity=None, source_current_provider=None,
                 registry=None, protected_model_roots=()):
        super().__init__(parent)
        self.setWindowTitle("Create Generation Setup from Image")
        self.resize(900, 760)
        self.records = tuple(records)
        self.fragments = tuple(item for item in fragments
                               if isinstance(item, dict) and item.get("reviewed")
                               and isinstance(item.get("text"), str) and item["text"].strip())[:200]
        self.recipe = None
        self.notes = ()
        self.jobs = jobs
        self.settings = settings
        self.registry = registry
        self.protected_model_roots = tuple(protected_model_roots)
        self.suggestion_source = suggestion_source
        self.source_identity = source_identity
        self.source_current_provider = source_current_provider
        self._suggestion_task = None
        self._source_preview_task = None
        self._suggestion_candidates = []
        self._loading_suggestion = False
        outer = QVBoxLayout(self)
        heading = QLabel("Reuse what you choose from this image")
        heading.setObjectName("sectionTitle"); outer.addWidget(heading)
        intro = QLabel(
            "Choose saved text, optional image suggestions, and recorded settings in the tabs below. "
            "You decide what to keep. Source pixels, raw metadata and masks are not copied; nothing runs."
        )
        intro.setWordWrap(True); outer.addWidget(intro)

        self.source_preview = ProtectedImageView(
            policy=content_safety_policy(), settings=settings,
        )
        self.source_preview.setAccessibleName("Selected image for generation setup")
        self.source_preview.setFixedSize(210, 165)
        self.source_preview.clear("Loading selected image…")
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_preview)
        source_hint = QLabel(
            "Selected image\nThis source stays unchanged."
        )
        source_hint.setWordWrap(True)
        source_hint.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.source_preview_note = source_hint
        source_row.addWidget(source_hint, 1)
        outer.addLayout(source_row)
        self._load_source_preview()

        name_form = QFormLayout()
        self.name = QLineEdit(); self.name.setPlaceholderText("For example, Blue-eyes portrait")
        self.name.setAccessibleName("Reusable setup name")
        name_form.addRow("Setup name", self.name)
        outer.addLayout(name_form)

        self.task_tabs = QTabWidget()
        self.task_tabs.setAccessibleName("Generation setup tasks")
        task_layouts = []
        for title, accessible_name in (
            ("Prompt Text", "Prompt text choices"),
            ("Image Suggestions", "Optional image suggestions"),
            ("Models & Settings", "Model and recorded setting choices"),
        ):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setAccessibleName(accessible_name)
            body = QWidget()
            page_layout = QVBoxLayout(body)
            scroll.setWidget(body)
            self.task_tabs.addTab(scroll, title)
            task_layouts.append((scroll, page_layout))
        (self.scroll, prompt_layout), (self.suggestions_scroll, suggestion_layout), (
            self.models_scroll, models_layout,
        ) = task_layouts
        outer.addWidget(self.task_tabs, 1)
        layout = prompt_layout

        recorded_layout = QVBoxLayout()
        recorded_heading = QLabel("Optional: reuse recorded generation settings")
        recorded_heading.setObjectName("sectionTitle")
        recorded_layout.addWidget(recorded_heading)
        form = QFormLayout()
        self.record_choice = QComboBox(); self.record_choice.setAccessibleName("Recorded generation to use")
        self.record_choice.addItem("Saved text only — no recorded settings", None)
        for index, record in enumerate(self.records):
            model = str(record.model or "").replace("\\", "/").rsplit("/", 1)[-1][:80]
            summary = " · ".join(value for value in (record.generator_name[:50], model) if value)
            self.record_choice.addItem(f"Record {index + 1}" + (f" · {summary}" if summary else ""), index)
        if len(self.records) == 1:
            self.record_choice.setCurrentIndex(1)
        form.addRow("Recorded settings", self.record_choice)
        recorded_layout.addLayout(form)

        self.record_status = QLabel(); self.record_status.setWordWrap(True)
        recorded_layout.addWidget(self.record_status)
        choices = QGridLayout()
        self.include_model = QCheckBox("Recorded model and exact components")
        self.include_sampling = QCheckBox("Recorded size and sampling")
        self.include_prompt = QCheckBox("Recorded positive prompt")
        self.include_negative = QCheckBox("Recorded negative prompt")
        for index, choice in enumerate((self.include_model, self.include_sampling,
                                         self.include_prompt, self.include_negative)):
            choices.addWidget(choice, index // 2, index % 2)
        recorded_layout.addLayout(choices)
        self.lora_tree = QTreeWidget()
        self.lora_tree.setHeaderLabels(("Keep", "Recorded LoRA", "Recorded strength",
                                        "Change image", "Change text"))
        self.lora_tree.setRootIsDecorated(False)
        self.lora_tree.setAccessibleName("Recorded LoRAs to keep in this setup")
        self.lora_tree.setColumnWidth(0, 56)
        self.lora_tree.setColumnWidth(1, 190)
        self.lora_tree.setColumnWidth(2, 185)
        self.lora_tree.setColumnWidth(3, 150)
        self.lora_tree.header().setStretchLastSection(True)
        self.lora_tree.setFixedHeight(106)
        self.lora_toggle = DisclosureButton("Choose Recorded LoRAs", "Hide Recorded LoRAs", self.lora_tree)
        self.lora_toggle.setAccessibleDescription(
            "Include or exclude each exact recorded LoRA. Optional strength changes are authored overrides; "
            "unchanged values stay exactly as recorded. No model is guessed from the image."
        )
        self.lora_toggle.hide()
        recorded_layout.addWidget(self.lora_toggle, 0, Qt.AlignLeft)
        recorded_layout.addWidget(self.lora_tree)
        self._lora_strength_controls = {}
        self.reset_lora_strengths = QPushButton("Use Recorded Strengths for All LoRAs")
        self.reset_lora_strengths.setAccessibleDescription(
            "Remove every unsaved LoRA strength override in this setup; selected LoRAs keep their recorded values."
        )
        self.reset_lora_strengths.clicked.connect(self._reset_lora_strengths)
        self.reset_lora_strengths.hide()
        recorded_layout.addWidget(self.reset_lora_strengths, 0, Qt.AlignLeft)
        self.lora_toggle.toggled.connect(self._update_reset_visibility)

        self.resource_panel = QWidget()
        resource_layout = QVBoxLayout(self.resource_panel)
        resource_hint = QLabel(
            "Keep only the exact recorded components you want, or choose a registered replacement. "
            "These choices do not change the source image or select a generator."
        )
        resource_hint.setWordWrap(True)
        resource_layout.addWidget(resource_hint)
        self.recorded_resource_tree = QTreeWidget()
        self.recorded_resource_tree.setHeaderLabels(("Keep", "Recorded exact component", "Role"))
        self.recorded_resource_tree.setRootIsDecorated(False)
        self.recorded_resource_tree.setAccessibleName("Recorded model components to keep")
        self.recorded_resource_tree.setFixedHeight(110)
        self.recorded_resource_tree.setColumnWidth(0, 58)
        self.recorded_resource_tree.setColumnWidth(1, 330)
        resource_layout.addWidget(self.recorded_resource_tree)
        choice_row = QHBoxLayout()
        self.choose_checkpoint = QPushButton("Choose Model…")
        self.add_lora = QPushButton("Add LoRA…")
        self.choose_vae = QPushButton("Choose VAE…")
        for button, role in ((self.choose_checkpoint, "checkpoint"),
                             (self.add_lora, "lora"), (self.choose_vae, "vae")):
            button.setEnabled(registry is not None)
            button.clicked.connect(lambda _checked=False, value=role: self._choose_resource(value))
            choice_row.addWidget(button)
        choice_row.addStretch(1)
        resource_layout.addLayout(choice_row)
        if registry is None:
            unavailable = QLabel(
                "Registered model choices are unavailable here. Saved text and recorded settings still work; "
                "register models in Models first."
            )
            unavailable.setWordWrap(True)
            resource_layout.addWidget(unavailable)
        self.chosen_resource_tree = QTreeWidget()
        self.chosen_resource_tree.setHeaderLabels(("Keep", "Chosen registered component", "Image strength", "Text strength"))
        self.chosen_resource_tree.setRootIsDecorated(False)
        self.chosen_resource_tree.setAccessibleName("Chosen registered model components for this setup")
        self.chosen_resource_tree.setFixedHeight(110)
        self.chosen_resource_tree.setColumnWidth(0, 58)
        self.chosen_resource_tree.setColumnWidth(1, 290)
        self.chosen_resource_tree.setColumnWidth(2, 130)
        resource_layout.addWidget(self.chosen_resource_tree)
        self.resource_toggle = DisclosureButton(
            "Customize Exact Model Components", "Hide Exact Model Components", self.resource_panel,
        )
        self.resource_toggle.setAccessibleDescription(
            "Review individual recorded components or choose a registered model, LoRA or VAE. "
            "Nothing is copied from image appearance."
        )
        recorded_layout.addWidget(self.resource_toggle, 0, Qt.AlignLeft)
        recorded_layout.addWidget(self.resource_panel)

        text_heading = QLabel("Choose saved text to keep in the prompt")
        text_heading.setObjectName("sectionTitle"); layout.addWidget(text_heading)
        text_hint = QLabel("Nothing is selected automatically. These are saved Tags or Descriptions, not new visual analysis.")
        text_hint.setWordWrap(True); layout.addWidget(text_hint)
        self.text_tree = QTreeWidget()
        self.text_tree.setHeaderLabels(("Keep", "Saved text", "Source"))
        self.text_tree.setRootIsDecorated(False)
        self.text_tree.setAlternatingRowColors(True)
        self.text_tree.setAccessibleName("Saved image text to reuse")
        for fragment in self.fragments:
            source = str(fragment.get("kind") or "Saved text")
            row = QTreeWidgetItem(("", fragment["text"], source))
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(0, Qt.Unchecked)
            row.setData(0, Qt.UserRole, fragment["text"])
            self.text_tree.addTopLevelItem(row)
        self.text_tree.setColumnWidth(0, 58)
        self.text_tree.setColumnWidth(1, 560)
        self.text_tree.setFixedHeight(min(260, max(106, 53 + 27 * len(self.fragments))))
        layout.addWidget(self.text_tree)
        saved_text_actions = QHBoxLayout()
        self.edit_saved_text_button = QPushButton("Edit Selected Text as Prompt Part…")
        self.edit_saved_text_button.setAccessibleDescription(
            "Edit a copy of the highlighted saved Tag or Description and choose a prompt purpose. "
            "The original image text stays unchanged."
        )
        self.edit_saved_text_button.setEnabled(False)
        self.edit_saved_text_button.setVisible(bool(self.fragments))
        self.edit_saved_text_button.clicked.connect(self._edit_saved_text)
        saved_text_actions.addWidget(self.edit_saved_text_button)
        saved_text_actions.addStretch(1)
        layout.addLayout(saved_text_actions)
        self.saved_text_status = QLabel()
        self.saved_text_status.setWordWrap(True)
        self.saved_text_status.hide()
        layout.addWidget(self.saved_text_status)

        self.parts_panel = QWidget()
        parts_layout = QVBoxLayout(self.parts_panel)
        parts_hint = QLabel(
            "Optional local suggestions are uncertain. Choose a goal, then edit, use or discard each answer. "
            "You can also write prompt parts manually. Nothing here changes Image Knowledge."
        )
        parts_hint.setWordWrap(True); parts_layout.addWidget(parts_hint)
        self.part_fields = {}
        self.part_checks = {}
        part_form = QFormLayout()
        for kind, label in (("character", "Character traits"), ("scene", "Scene elements"),
                            ("pose", "Pose or action"), ("style", "Style"),
                            ("other", "Other or unclassified")):
            check = QCheckBox(label)
            if kind == "other":
                field = QTextEdit(); field.setAcceptRichText(False)
                field.setMaximumHeight(82)
            else:
                field = QLineEdit(); field.setMaxLength(4096)
            field.setPlaceholderText("Text to reuse in new images")
            field.setAccessibleName(f"Reusable {label.lower()} prompt text")
            check.toggled.connect(self._invalidate)
            field.textChanged.connect(self._invalidate)
            self.part_checks[kind] = check; self.part_fields[kind] = field
            part_form.addRow(check, field)
        self.manual_parts_panel = QWidget()
        manual_parts_layout = QVBoxLayout(self.manual_parts_panel)
        manual_parts_layout.addLayout(part_form)
        suggestion_request = QHBoxLayout()
        suggestion_request.addWidget(QLabel("Suggest for"))
        self.suggestion_goal = QComboBox()
        self.suggestion_goal.setAccessibleName("Image suggestion goal")
        suggestion_request.addWidget(self.suggestion_goal, 1)
        self.refresh_suggestion_goals()
        self.suggest_button = QPushButton("Suggest Text from Image…")
        self.suggest_button.setEnabled(jobs is not None and settings is not None
                                       and suggestion_source is not None)
        self.suggest_button.clicked.connect(self._suggest_from_image)
        suggestion_request.addWidget(self.suggest_button)
        parts_layout.addLayout(suggestion_request)
        self.suggestion_status = QLabel(
            "Local analysis is optional and starts only when you choose Suggest. "
            "Its answers are uncertain and will not be selected automatically."
        )
        self.suggestion_status.setWordWrap(True); parts_layout.addWidget(self.suggestion_status)
        self.review_description_setup_button = QPushButton("Review Local Description Setup…")
        self.review_description_setup_button.setAccessibleDescription(
            "Open the existing AI Description model settings while keeping this unsaved Generation Setup. "
            "Returning does not start analysis automatically."
        )
        self.review_description_setup_button.clicked.connect(self._request_description_setup)
        self.review_description_setup_button.hide()
        parts_layout.addWidget(self.review_description_setup_button, 0, Qt.AlignLeft)
        self.suggestion_source_detail = QLabel()
        self.suggestion_source_detail.setTextFormat(Qt.PlainText)
        self.suggestion_source_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.suggestion_source_detail.setWordWrap(True)
        self.suggestion_source_detail.setAccessibleName("Exact local suggestion source")
        self.suggestion_source_toggle = DisclosureButton(
            "Show Analysis Model", "Hide Analysis Model", self.suggestion_source_detail,
        )
        self.suggestion_source_toggle.setAccessibleDescription(
            "Shows which local model proposed the text, not which model generated the image."
        )
        self.suggestion_source_toggle.toggled.connect(self._suggestion_source_toggled)
        self.suggestion_source_toggle.hide()
        parts_layout.addWidget(self.suggestion_source_toggle, 0, Qt.AlignLeft)
        parts_layout.addWidget(self.suggestion_source_detail)
        self.suggestion_review = QWidget()
        suggestion_review_layout = QVBoxLayout(self.suggestion_review)
        suggestion_review_hint = QLabel(
            "Review each uncertain suggestion. Edit its text, choose where it belongs, then Use it or Discard it. "
            "Nothing enters the setup until you choose Use."
        )
        suggestion_review_hint.setWordWrap(True)
        suggestion_review_layout.addWidget(suggestion_review_hint)
        self.suggestion_choice = QComboBox()
        self.suggestion_choice.setAccessibleName("Suggestion to review")
        self.suggestion_choice.currentIndexChanged.connect(self._show_suggestion_candidate)
        suggestion_review_layout.addWidget(self.suggestion_choice)
        self.suggestion_candidate_source = QLabel()
        self.suggestion_candidate_source.setWordWrap(True)
        self.suggestion_candidate_source.setTextFormat(Qt.PlainText)
        suggestion_review_layout.addWidget(self.suggestion_candidate_source)
        self.suggestion_text = QTextEdit()
        self.suggestion_text.setAcceptRichText(False)
        self.suggestion_text.setMaximumHeight(94)
        self.suggestion_text.setAccessibleName("Editable image suggestion")
        self.suggestion_text.textChanged.connect(self._edit_suggestion_candidate)
        suggestion_review_layout.addWidget(self.suggestion_text)
        use_row = QHBoxLayout()
        self.suggestion_target = QComboBox()
        self.suggestion_target.setAccessibleName("Prompt part for suggestion")
        for kind, label in (("character", "Character traits"), ("scene", "Scene elements"),
                            ("pose", "Pose or action"), ("style", "Style"),
                            ("other", "Other or unclassified")):
            self.suggestion_target.addItem(label, kind)
        self.suggestion_target.currentIndexChanged.connect(self._assign_suggestion_target)
        use_row.addWidget(self.suggestion_target, 1)
        self.use_suggestion_button = QPushButton("Use in Setup")
        self.use_suggestion_button.clicked.connect(self._use_suggestion)
        use_row.addWidget(self.use_suggestion_button)
        self.discard_suggestion_button = QPushButton("Discard Suggestion")
        self.discard_suggestion_button.clicked.connect(self._discard_suggestion)
        use_row.addWidget(self.discard_suggestion_button)
        suggestion_review_layout.addLayout(use_row)
        self.suggestion_review.hide()
        parts_layout.addWidget(self.suggestion_review)
        self.part_summary = QLabel("No prompt parts chosen yet.")
        self.part_summary.setWordWrap(True)
        self.part_summary.hide()
        self.manual_parts_toggle = DisclosureButton(
            "Edit Prompt Parts Manually", "Hide Prompt Part Editor", self.manual_parts_panel,
        )
        suggestion_layout.addWidget(self.parts_panel)
        suggestion_layout.addStretch(1)
        layout.addWidget(self.part_summary)
        layout.addWidget(self.manual_parts_toggle, 0, Qt.AlignLeft)
        layout.addWidget(self.manual_parts_panel)
        layout.addStretch(1)
        models_layout.addLayout(recorded_layout)
        models_layout.addStretch(1)

        self.review_button = QPushButton("Review Proposed Setup")
        self.review_button.clicked.connect(self.review)
        self.review_status = QLabel("Choose the parts to reuse, then review the proposed setup before saving.")
        self.review_status.setWordWrap(True); outer.addWidget(self.review_status)
        self.strength_review = QLabel()
        self.strength_review.setWordWrap(True)
        self.strength_review.hide()
        outer.addWidget(self.strength_review)
        self.exact = QTextEdit(); self.exact.setReadOnly(True)
        self.exact.setMaximumHeight(140)
        self.exact.setAccessibleName("Exact proposed setup details")
        self.exact_toggle = DisclosureButton("Show Exact Proposed Settings", "Hide Exact Proposed Settings", self.exact)
        self.exact_toggle.toggled.connect(self._exact_toggled)
        self.exact_toggle.hide()
        outer.addWidget(self.exact_toggle, 0, Qt.AlignLeft)
        outer.addWidget(self.exact)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save Setup")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_reviewed)
        self.buttons.rejected.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(self.review_button)
        footer.addStretch(1)
        footer.addWidget(self.buttons)
        outer.addLayout(footer)

        self.name.textChanged.connect(self._invalidate)
        self.record_choice.currentIndexChanged.connect(self._record_changed)
        self.text_tree.itemChanged.connect(self._invalidate)
        self.text_tree.currentItemChanged.connect(
            lambda current, _previous: self.edit_saved_text_button.setEnabled(current is not None)
        )
        self.lora_tree.itemChanged.connect(self._lora_selection_changed)
        self.recorded_resource_tree.itemChanged.connect(self._invalidate)
        self.chosen_resource_tree.itemChanged.connect(self._invalidate)
        self.include_model.toggled.connect(self._model_inclusion_changed)
        for choice in (self.include_model, self.include_sampling,
                       self.include_prompt, self.include_negative):
            choice.toggled.connect(self._invalidate)
        self._record_changed()

    def _suggestion_source_toggled(self, checked):
        if checked:
            QTimer.singleShot(0, self._scroll_to_suggestion_review)

    def refresh_suggestion_goals(self):
        from core.ai_interrogation import JOYCAPTION_MODEL
        previous = self.suggestion_goal.currentData()
        self.suggestion_goal.clear()
        self.suggestion_goal.addItem("General description", ("other",))
        model_id = getattr(self.settings, "plain_language_model", JOYCAPTION_MODEL) or JOYCAPTION_MODEL
        if model_id == JOYCAPTION_MODEL:
            self.suggestion_goal.addItem("Character traits", ("character",))
            self.suggestion_goal.addItem("Scene elements", ("scene",))
            self.suggestion_goal.addItem("Pose or action", ("pose",))
            self.suggestion_goal.addItem("Style", ("style",))
            self.suggestion_goal.addItem("Character, scene, pose and style", ("character", "scene", "pose", "style"))
            self.suggestion_goal.setToolTip(
                "JoyCaption can answer a separate question for each selected purpose. Answers are uncertain; "
                "review them before use. Asking for all four runs four local analyses."
            )
        else:
            self.suggestion_goal.setToolTip(
                "This caption model provides a general description only. You can edit or split its text into prompt parts."
            )
        index = self.suggestion_goal.findData(previous)
        if index >= 0:
            self.suggestion_goal.setCurrentIndex(index)

    def _show_suggestion_candidate(self, index):
        active = 0 <= index < len(self._suggestion_candidates)
        self.suggestion_review.setVisible(active)
        if not active:
            self.suggestion_source_toggle.hide()
            self.suggestion_source_detail.clear()
            self.suggestion_candidate_source.clear()
            return
        candidate = self._suggestion_candidates[index]
        self.suggestion_candidate_source.setText(candidate["source_label"] + ". Not included until you choose Use in Setup.")
        self._loading_suggestion = True
        try:
            self.suggestion_text.setPlainText(candidate["text"])
            target = self.suggestion_target.findData(candidate["target"])
            self.suggestion_target.setCurrentIndex(target if target >= 0 else 0)
        finally:
            self._loading_suggestion = False
        self.suggestion_source_detail.setText(
            f"Analysis model: {candidate['model_id']} (proposal only; not the image's generating model)."
        )
        self.suggestion_source_toggle.setVisible(True)

    def _edit_suggestion_candidate(self):
        index = self.suggestion_choice.currentIndex()
        if not self._loading_suggestion and 0 <= index < len(self._suggestion_candidates):
            self._suggestion_candidates[index]["text"] = self.suggestion_text.toPlainText()

    def _assign_suggestion_target(self):
        index = self.suggestion_choice.currentIndex()
        if not self._loading_suggestion and 0 <= index < len(self._suggestion_candidates):
            self._suggestion_candidates[index]["target"] = self.suggestion_target.currentData()

    def _scroll_to_suggestion_review(self):
        from shiboken6 import isValid
        if isValid(self.suggestions_scroll) and isValid(self.suggestion_review) and self.suggestion_review.isVisible():
            self.task_tabs.setCurrentWidget(self.suggestions_scroll)
            self.suggestions_scroll.ensureWidgetVisible(self.suggestion_review, 0, 24)

    def _use_suggestion(self):
        index = self.suggestion_choice.currentIndex()
        if not 0 <= index < len(self._suggestion_candidates):
            return
        text = self.suggestion_text.toPlainText().strip()
        if not text or len(text) > 4096:
            self.suggestion_status.setText("Edit this suggestion to 1–4096 characters before using it.")
            return
        target = self.suggestion_target.currentData()
        existing = self._part_text(target).strip()
        if existing and existing != text:
            answer = QMessageBox.question(
                self, "Replace This Prompt Part?",
                f"{self.suggestion_target.currentText()} already has your text. Replace it with this edited suggestion? "
                "Cancel keeps your current text and suggestion.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        field = self.part_fields[target]
        if isinstance(field, QTextEdit):
            field.setPlainText(text)
        else:
            field.setText(text)
        self.part_checks[target].setChecked(True)
        self.suggestion_status.setText(
            f"Used edited suggestion as {self.suggestion_target.currentText().lower()}. "
            "Review the proposed setup before saving; other suggestions remain optional."
        )

    def _discard_suggestion(self):
        index = self.suggestion_choice.currentIndex()
        if not 0 <= index < len(self._suggestion_candidates):
            return
        self._suggestion_candidates.pop(index)
        self.suggestion_choice.removeItem(index)
        self._show_suggestion_candidate(self.suggestion_choice.currentIndex())
        self.suggestion_status.setText("Suggestion discarded. Your authored prompt parts are unchanged.")

    def _exact_toggled(self, checked):
        if checked:
            self.exact.setFocus(Qt.OtherFocusReason)

    def _record_changed(self, *_args):
        index = self.record_choice.currentData()
        record = self.records[index] if isinstance(index, int) else None
        available = record is not None
        for choice in (self.include_model, self.include_sampling,
                       self.include_prompt, self.include_negative):
            choice.blockSignals(True)
            choice.setChecked(False)
            choice.setEnabled(available)
            choice.blockSignals(False)
        self.lora_toggle.setChecked(False)
        self.lora_tree.blockSignals(True)
        self.lora_tree.clear()
        self.recorded_resource_tree.blockSignals(True)
        self.recorded_resource_tree.clear()
        self._lora_strength_controls.clear()
        self.reset_lora_strengths.hide()
        if record is not None:
            for index, resource in enumerate(record.resources):
                digest = resource.hashes.get("sha256")
                if (resource.role in {"checkpoint", "vae"} and isinstance(digest, str)
                        and SHA.fullmatch(digest)):
                    name = str(resource.name).replace("\\", "/").rsplit("/", 1)[-1]
                    component = QTreeWidgetItem(("", name, resource.role.replace("_", " ").title()))
                    component.setFlags(component.flags() | Qt.ItemIsUserCheckable)
                    component.setCheckState(0, Qt.Checked)
                    component.setData(0, Qt.UserRole, index)
                    component.setToolTip(1, name)
                    self.recorded_resource_tree.addTopLevelItem(component)
                if resource.role != "lora" or not isinstance(digest, str) or not SHA.fullmatch(digest):
                    continue
                name = str(resource.name).replace("\\", "/").rsplit("/", 1)[-1]
                weights = ", ".join(
                    f"{str(key).replace('_', ' ')[:32]} " +
                    (f"{value:g}" if type(value) in (int, float)
                     and abs(value) <= 1e308 and math.isfinite(value)
                     else "unusable recorded value")
                    for key, value in list(resource.weights.items())[:4]
                ) or "Strength not recorded"
                row = QTreeWidgetItem(("", name, weights, "", ""))
                row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
                row.setCheckState(0, Qt.Checked)
                row.setData(0, Qt.UserRole, index)
                row.setToolTip(1, name)
                row.setToolTip(2, weights)
                self.lora_tree.addTopLevelItem(row)
                controls = {}
                for column, key, label in ((3, "model", "image"),
                                           (4, "text_encoder", "text")):
                    container = QWidget()
                    control_layout = QHBoxLayout(container)
                    control_layout.setContentsMargins(2, 0, 2, 0)
                    control_layout.setSpacing(3)
                    change = QCheckBox()
                    change.setAccessibleName(f"Change {label} strength for {name}")
                    change.setToolTip(f"Check to replace only this LoRA's recorded {label} strength.")
                    value = QDoubleSpinBox()
                    value.setRange(-10, 10)
                    value.setDecimals(6)
                    value.setSingleStep(.05)
                    recorded = resource.weights.get(key)
                    value.setValue(float(recorded) if type(recorded) in (int, float)
                                   and math.isfinite(recorded) and -10 <= recorded <= 10 else 1.0)
                    value.setEnabled(False)
                    value.setAccessibleName(f"New {label} strength for {name}")
                    value.setToolTip("Used only when Change is checked; otherwise the recorded value stays exact.")
                    change.toggled.connect(value.setEnabled)
                    change.toggled.connect(self._lora_strength_changed)
                    value.valueChanged.connect(self._lora_strength_changed)
                    control_layout.addWidget(change)
                    control_layout.addWidget(value, 1)
                    self.lora_tree.setItemWidget(row, column, container)
                    controls[key] = (change, value)
                self._lora_strength_controls[index] = controls
        self.lora_tree.blockSignals(False)
        self.recorded_resource_tree.blockSignals(False)
        self.lora_tree.setFixedHeight(min(190, max(106, 54 + 27 * self.lora_tree.topLevelItemCount())))
        self.recorded_resource_tree.setFixedHeight(
            min(190, max(85, 54 + 27 * self.recorded_resource_tree.topLevelItemCount()))
        )
        self.recorded_resource_tree.setVisible(self.recorded_resource_tree.topLevelItemCount() > 0)
        self.resource_toggle.setVisible(
            self.registry is not None or self.recorded_resource_tree.topLevelItemCount() > 0
        )
        self._model_inclusion_changed()
        if record is None:
            self.record_status.setText(
                "No recorded generator settings selected. You can still save chosen Tags or Descriptions" +
                (", or choose a registered model under Customize Exact Model Components."
                 if self.registry is not None else ".")
            )
        else:
            count = len(record.resources)
            size = (f"{record.width} × {record.height}" if record.width and record.height
                    else "size not recorded")
            self.record_status.setText(
                f"Recorded: {count} component(s), {size}" +
                (f", {record.steps} steps" if record.steps else "") +
                ". Choose only what to reuse. Exact models and LoRAs still need current checks."
            )
        self._invalidate()

    def _model_inclusion_changed(self, *_args):
        enabled = self.include_model.isChecked() and self.lora_tree.topLevelItemCount() > 0
        self.lora_toggle.setVisible(enabled)
        self.recorded_resource_tree.setEnabled(self.include_model.isChecked())
        if not enabled:
            self.lora_toggle.setChecked(False)
        self._update_reset_visibility()

    def _selected_lora_indices(self):
        return tuple(self.lora_tree.topLevelItem(index).data(0, Qt.UserRole)
                     for index in range(self.lora_tree.topLevelItemCount())
                     if self.lora_tree.topLevelItem(index).checkState(0) == Qt.Checked)

    def _selected_recorded_resource_indices(self):
        if not self.include_model.isChecked():
            return ()
        chosen = list(self._selected_lora_indices())
        chosen.extend(
            self.recorded_resource_tree.topLevelItem(index).data(0, Qt.UserRole)
            for index in range(self.recorded_resource_tree.topLevelItemCount())
            if self.recorded_resource_tree.topLevelItem(index).checkState(0) == Qt.Checked
        )
        return tuple(sorted(chosen))

    def _chosen_registered_resources(self):
        rows = []
        for index in range(self.chosen_resource_tree.topLevelItemCount()):
            item = self.chosen_resource_tree.topLevelItem(index)
            if item.checkState(0) != Qt.Checked:
                continue
            row = deepcopy(item.data(0, Qt.UserRole))
            if row["role"] == "lora":
                row["weights"] = {
                    key: self.chosen_resource_tree.itemWidget(item, column).value()
                    for column, key in ((2, "model"), (3, "text_encoder"))
                }
            rows.append(row)
        return tuple(rows)

    def _choose_resource(self, role):
        if self.registry is None or self.save_in_progress:
            return
        from .generation_manual_setup_dialog import choose_registered_setup_resource

        main = next((row for row in self._chosen_registered_resources()
                     if row["role"] == "checkpoint"), None)
        try:
            row = choose_registered_setup_resource(
                self, self.registry, self.jobs, role,
                main_model_resource_id=main["id"] if main else "",
                settings=self.settings, protected_model_roots=self.protected_model_roots,
            )
        except (ValueError, RuntimeError) as exc:
            self.review_status.setText(str(exc))
            return
        if row is None:
            return
        for index in reversed(range(self.chosen_resource_tree.topLevelItemCount())):
            existing = self.chosen_resource_tree.topLevelItem(index).data(0, Qt.UserRole)
            if (row["role"] in {"checkpoint", "vae"} and existing["role"] == row["role"]) or (
                    row["role"] == existing["role"] == "lora" and existing["id"] == row["id"]):
                self.chosen_resource_tree.takeTopLevelItem(index)
        if row["role"] in {"checkpoint", "vae"}:
            for index in range(self.recorded_resource_tree.topLevelItemCount()):
                recorded = self.recorded_resource_tree.topLevelItem(index)
                if recorded.text(2).lower() == row["role"]:
                    recorded.setCheckState(0, Qt.Unchecked)
        item = QTreeWidgetItem(("", row.get("display_label") or row["name"], "", ""))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(0, Qt.Checked)
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(1, row.get("display_label") or row["name"])
        self.chosen_resource_tree.addTopLevelItem(item)
        if row["role"] == "lora":
            for column, key, label in ((2, "model", "image"), (3, "text_encoder", "text")):
                strength = QDoubleSpinBox()
                strength.setRange(-10, 10)
                strength.setDecimals(6)
                strength.setSingleStep(.05)
                strength.setValue(float(row["weights"].get(key, 1)))
                strength.setAccessibleName(f"Chosen {label} strength for {item.text(1)}")
                strength.valueChanged.connect(self._invalidate)
                self.chosen_resource_tree.setItemWidget(item, column, strength)
        self.chosen_resource_tree.setFixedHeight(
            min(190, max(85, 54 + 27 * self.chosen_resource_tree.topLevelItemCount()))
        )
        self._invalidate()

    def _selected_lora_weight_overrides(self):
        selected = set(self._selected_lora_indices())
        return {index: {key: value.value() for key, (change, value) in controls.items()
                        if change.isChecked()}
                for index, controls in self._lora_strength_controls.items()
                if index in selected and any(change.isChecked()
                                             for change, _value in controls.values())}

    def _update_reset_visibility(self, *_args):
        self.reset_lora_strengths.setVisible(
            self.include_model.isChecked() and self.lora_toggle.isChecked() and any(
            change.isChecked() for controls in self._lora_strength_controls.values()
            for change, _value in controls.values()
            )
        )

    def _lora_strength_changed(self, *_args):
        self._update_reset_visibility()
        self._invalidate()

    def _reset_lora_strengths(self):
        for controls in self._lora_strength_controls.values():
            for change, _value in controls.values():
                change.setChecked(False)
        self._lora_strength_changed()

    def _lora_selection_changed(self, row, _column):
        controls = self._lora_strength_controls.get(row.data(0, Qt.UserRole), {})
        selected = row.checkState(0) == Qt.Checked
        for change, value in controls.values():
            change.setEnabled(selected)
            value.setEnabled(selected and change.isChecked())
        self._invalidate()

    def _invalidate(self, *_args):
        self.recipe = None
        self.notes = ()
        self.save_button.setEnabled(False)
        self.review_status.setText("Choose the parts to reuse, then review the proposed setup before saving.")
        self.strength_review.hide()
        self.exact.clear()
        self.exact_toggle.setChecked(False)
        self.exact_toggle.hide()
        if hasattr(self, "part_summary"):
            chosen = [check.text() for check in self.part_checks.values() if check.isChecked()]
            self.part_summary.setVisible(bool(chosen))
            if chosen:
                self.part_summary.setText("Chosen prompt parts: " + ", ".join(chosen) + ".")

    def _selected_text(self):
        return tuple(self.text_tree.topLevelItem(index).data(0, Qt.UserRole)
                     for index in range(self.text_tree.topLevelItemCount())
                     if self.text_tree.topLevelItem(index).checkState(0) == Qt.Checked)

    def _edit_saved_text(self):
        row = self.text_tree.currentItem()
        if row is None:
            return
        editor = _SavedTextPartDialog(row.data(0, Qt.UserRole), row.text(2), self)
        if editor.exec() != QDialog.Accepted:
            return
        target = editor.purpose.currentData()
        text = editor.edited_text.toPlainText().strip()
        existing = self._part_text(target).strip()
        if existing and existing != text:
            answer = QMessageBox.question(
                self, "Replace This Prompt Part?",
                f"{editor.purpose.currentText()} already has your text. Replace it with this edited copy? "
                "Cancel keeps your current text and saved-text choice.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        field = self.part_fields[target]
        if isinstance(field, QTextEdit):
            field.setPlainText(text)
        else:
            field.setText(text)
        self.part_checks[target].setChecked(True)
        row.setCheckState(0, Qt.Unchecked)
        self.saved_text_status.setText(
            f"Edited copy used as {editor.purpose.currentText().lower()}. The original saved text "
            "is unchanged and its whole-string choice is off to avoid duplicate prompt text. "
            "Review the proposed setup before saving."
        )
        self.saved_text_status.show()

    def _selected_parts(self):
        return tuple({"kind": kind, "text": self._part_text(kind).strip()}
                     for kind in self.part_fields if self.part_checks[kind].isChecked())

    def _part_text(self, kind):
        field = self.part_fields[kind]
        return field.toPlainText() if isinstance(field, QTextEdit) else field.text()

    def _request_description_setup(self):
        if self._suggestion_task is None and not self.save_in_progress:
            self.setup_requested.emit()

    def _suggest_from_image(self):
        if self._suggestion_task is not None or self.jobs is None or self.suggestion_source is None:
            return
        if len(self._suggestion_candidates) >= 32:
            self.suggestion_status.setText("Review or discard some pending suggestions before requesting more.")
            return
        from copy import deepcopy
        from core.generation_image_suggestions import suggest_image_setup_parts
        from .workers import CoordinatedCallTask

        identity = deepcopy(self.source_identity)
        provider = self.source_current_provider
        selected_kinds = tuple(self.suggestion_goal.currentData() or ("other",))
        self.suggest_button.setEnabled(False)
        self.suggestion_goal.setEnabled(False)
        self.review_description_setup_button.hide()
        self.suggestion_status.setText("Analyzing this selected image locally… Cancel stops at the next safe boundary.")
        def work(job):
            if callable(provider) and provider() != identity:
                raise ValueError("The selected image changed. Reselect it before requesting suggestions.")
            result = suggest_image_setup_parts(
                self.suggestion_source, self.settings, selected_kinds=selected_kinds,
                cancelled=lambda: job.cancel_requested,
            )
            if callable(provider) and provider() != identity:
                raise ValueError("The selected image changed during analysis. Reselect it before requesting suggestions.")
            return result
        task = CoordinatedCallTask(self.jobs, "Suggest generation setup text from image",
                                   work, resource="gpu", key=f"image-setup-suggestion:{id(self)}")
        self._suggestion_task = task
        task.signals.complete.connect(lambda result, active=task: self._suggestion_complete(active, result))
        task.signals.failed.connect(lambda error, active=task: self._suggestion_failed(active, str(error)))
        task.signals.cancelled.connect(lambda active=task: self._suggestion_failed(active, "Analysis was cancelled."))
        try:
            task.start()
        except Exception as exc:
            self._suggestion_failed(task, str(exc) or "Local analysis could not start.")

    def _suggestion_complete(self, task, result):
        if self._suggestion_task is not task:
            return
        self._suggestion_task = None
        self.suggest_button.setEnabled(self.jobs is not None and self.settings is not None
                                       and self.suggestion_source is not None)
        self.suggestion_goal.setEnabled(True)
        self.review_description_setup_button.hide()
        proposals = getattr(result, "proposals", ())
        valid = tuple(part for part in proposals
                      if getattr(part, "kind", None) in self.part_fields
                      and isinstance(getattr(part, "text", None), str)
                      and 0 < len(part.text.strip()) <= 4096)
        if not valid:
            self.suggestion_status.setText("No usable local suggestion was returned. Saved text and manual parts still work.")
            return
        if len(self._suggestion_candidates) + len(valid) > 32:
            self.suggestion_status.setText(
                "These suggestions were not added because the review is full. Discard unneeded suggestions, then retry."
            )
            return
        first = self.suggestion_choice.count()
        for part in valid:
            kind = part.kind
            label = dict(character="Character traits", scene="Scene elements",
                         pose="Pose or action", style="Style", other="General description")[kind]
            self._suggestion_candidates.append({
                "kind": kind, "target": kind, "text": part.text.strip(),
                "source_label": " ".join(str(getattr(part, "source_label", "") or "Local AI suggestion (uncertain)").split()),
                "model_id": " ".join(str(getattr(part, "model_id", "") or "Not reported").split()),
            })
            self.suggestion_choice.addItem(f"{label} — uncertain local suggestion")
        self.suggestion_choice.setCurrentIndex(first)
        self.task_tabs.setCurrentWidget(self.suggestions_scroll)
        self.suggestion_status.setText(
            f"{len(valid)} uncertain local suggestion(s) ready for review. Edit, use or discard each one. "
            "Existing prompt parts were not changed; no image fact or generating-model identity was inferred or saved."
        )
        QTimer.singleShot(0, self._scroll_to_suggestion_review)

    def _suggestion_failed(self, task, error):
        if self._suggestion_task is not task:
            return
        self._suggestion_task = None
        self.suggest_button.setEnabled(self.jobs is not None and self.settings is not None
                                       and self.suggestion_source is not None)
        self.suggestion_goal.setEnabled(True)
        self.suggestion_status.setText(
            "Suggestion unavailable: " + error + " Saved text and manual parts remain usable."
        )
        self.review_description_setup_button.setVisible(
            "cancel" not in error.lower() and "selected image changed" not in error.lower()
        )

    def review(self):
        index = self.record_choice.currentData()
        record = self.records[index] if isinstance(index, int) else None
        strength_changes = self._selected_lora_weight_overrides() if self.include_model.isChecked() else {}
        selected_recorded = self._selected_recorded_resource_indices()
        default_non_lora = tuple(
            index for index, resource in enumerate(record.resources)
            if resource.role in {"checkpoint", "vae"}
            and isinstance(resource.hashes.get("sha256"), str)
            and SHA.fullmatch(resource.hashes["sha256"])
        ) if record is not None and self.include_model.isChecked() else ()
        selected_non_lora = tuple(
            index for index in selected_recorded if record.resources[index].role != "lora"
        ) if record is not None else ()
        chosen_registered = self._chosen_registered_resources()
        advanced_resources = bool(chosen_registered or selected_non_lora != default_non_lora)
        resource_choices = (
            {"selected_recorded_resource_indices": selected_recorded,
             "authored_resources": chosen_registered}
            if advanced_resources else
            {"selected_lora_indices": (self._selected_lora_indices()
                                       if self.include_model.isChecked() else None)}
        )
        try:
            recipe, notes = make_image_inspired_recipe(
                self.name.text(), record, selected_text=self._selected_text(),
                include_recorded_prompt=self.include_prompt.isChecked(),
                include_recorded_negative=self.include_negative.isChecked(),
                include_model=self.include_model.isChecked(),
                include_sampling=self.include_sampling.isChecked(),
                lora_weight_overrides=strength_changes,
                selected_parts=self._selected_parts(),
                **resource_choices,
            )
        except ValueError as exc:
            self._invalidate()
            self.review_status.setText(str(exc))
            return
        self.recipe, self.notes = recipe, notes
        parts = []
        if "resources" in recipe or any(key in recipe["settings"] for key in ("model", "model_hash", "vae")):
            parts.append(f"model/components ({len(recipe.get('resources', []))} exact declarations)")
        if any(key in recipe["settings"] for key in ("width", "height", "steps", "sampler", "scheduler", "cfg_scale")):
            parts.append("size/sampling")
        if "prompts" in recipe or recipe.get("prompt_parts") or recipe.get("negative_prompt"):
            parts.append("chosen prompt text")
        self.review_status.setText(
            "Ready to save: " + (", ".join(parts) if parts else "no reusable fields") +
            (f". Recorded exact components kept: {len(selected_recorded)}; "
             f"registered choices added: {len(chosen_registered)}" if advanced_resources else "") +
            ". No source pixels or execution readiness are included." +
            (" Check the omitted evidence in Exact Proposed Settings." if notes else "")
        )
        comparisons = []
        for resource_index, weights in strength_changes.items():
            resource = record.resources[resource_index]
            name = str(resource.name).replace("\\", "/").rsplit("/", 1)[-1][:80]
            for key, value in weights.items():
                original = resource.weights.get(key)
                before = (str(original) if type(original) in (int, float)
                          and math.isfinite(original) else
                          "Not recorded" if original is None else "Unusable recorded value")
                comparisons.append({"lora": name, "field": key,
                                    "recorded": before, "chosen": value})
        if comparisons:
            visible = [f"{row['lora']}: {row['field'].replace('_', ' ')} "
                       f"{row['recorded']} → {row['chosen']:g}"
                       for row in comparisons[:3]]
            self.strength_review.setText(
                "Authored strength changes: " + "; ".join(visible) +
                (f"; {len(comparisons) - 3} more in Exact Proposed Settings" if len(comparisons) > 3 else "")
            )
            self.strength_review.show()
        self.exact.setPlainText(json.dumps({"setup": recipe,
                                            "authored_strength_changes": comparisons,
                                            "not_copied": notes},
                                           indent=2, ensure_ascii=False, sort_keys=True))
        self.exact_toggle.show()
        self.save_button.setEnabled(True)

    def _save_reviewed(self):
        if self.recipe is not None:
            self.save_button.setEnabled(False)
            self.save_requested(self.recipe)

    def save_requested(self, recipe):
        """The owning Generate workspace installs the scoped save callback."""
        callback = getattr(self, "save_callback", None)
        if callback is None:
            # Component consumers can still inspect the reviewed proposal.
            self.accept()
        else:
            callback(recipe)

    def result_payload(self):
        return self.recipe if self.result() == QDialog.Accepted else None

    def _load_source_preview(self):
        """Show a bounded, policy-protected reference without importing pixels into a setup."""
        if self.suggestion_source is None:
            self.source_preview.clear("No source image available")
            return
        if self.source_preview.policy is None:
            self.source_preview.clear("Preview unavailable until content safety is ready")
            return
        path = Path(self.suggestion_source)
        identity = deepcopy(self.source_identity)
        provider = self.source_current_provider

        def decode():
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) > 640:
                size.scale(QSize(640, 640), Qt.KeepAspectRatio)
                reader.setScaledSize(size)
            image = reader.read()
            if image.isNull():
                raise ValueError("The selected image preview is unavailable.")
            if callable(provider) and provider() != identity:
                raise ValueError("The selected image changed. Reselect it to review a fresh setup.")
            return image

        task = AsyncCallTask(decode)
        self._source_preview_task = task
        task.signals.complete.connect(
            lambda image, active=task: self._source_preview_ready(active, path, image))
        task.signals.failed.connect(
            lambda error, active=task: self._source_preview_failed(active, str(error)))
        QThreadPool.globalInstance().start(task)

    def _source_preview_ready(self, task, path, image):
        if self._source_preview_task is not task:
            return
        self._source_preview_task = None
        self.source_preview.set_image(QPixmap.fromImage(image), path)

    def _source_preview_failed(self, task, error):
        if self._source_preview_task is not task:
            return
        self._source_preview_task = None
        self.source_preview.clear("Selected image preview unavailable")
        self.source_preview_note.setText(
            f"{error} You can still review recorded settings and saved text."
        )

    def reject(self):
        if self._source_preview_task is not None:
            self._source_preview_task.cancel()
            self._source_preview_task = None
        if self._suggestion_task is not None:
            self._suggestion_task.cancel()
            self._suggestion_task = None
        super().reject()

    def closeEvent(self, event):
        if self._source_preview_task is not None:
            self._source_preview_task.cancel()
            self._source_preview_task = None
        if self._suggestion_task is not None:
            self._suggestion_task.cancel()
            self._suggestion_task = None
        super().closeEvent(event)
