from __future__ import annotations

from PySide6.QtCore import QEvent,QThreadPool,QTimer,Qt,Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,QDialog,QDialogButtonBox,QFormLayout,QGroupBox,QHBoxLayout,QLabel,
    QLineEdit,QMessageBox,QPushButton,QSizePolicy,QTabWidget,QTextEdit,QVBoxLayout,
    QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import CHARACTER_IDENTITY_VARIANT_TYPE_LABELS, CharacterRecordEditService
from core.knowledge_relationships import (
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    VERSION_OF,
)
from .components import bind_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .tag_identity_review import review_removed_identities
from .tag_relationship_editor import TagReferenceEditor,TagReferencePickerDialog,TokenChipEditor
from .workers import AsyncCallTask,start_qt_delivery_task


def _configure_form(form: QFormLayout) -> None:
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)


class CharacterSourcePickerDialog(TagReferencePickerDialog):
    """Search, create and choose one reviewed source without leaving the form."""

    def __init__(self, knowledge, categories, *, service, **kwargs):
        self.service = service
        self.review_requested = False
        super().__init__(
            knowledge, categories, allow_identity_creation=True,
            identity_category_id="copyright_series",
            identity_source="character_source_picker",
            identity_create_label="Create New Source",
            identity_consequence=(
                "Create New Source saves and selects the reusable Character Source (a series, world, or other origin). "
                "It remains available if character editing is cancelled. No image is saved here."
            ), **kwargs,
        )
        self.search.setPlaceholderText("Search Character Sources (series, worlds, or origins), or enter a new name…")
        self.search.setAccessibleName("Search or create a Character Source")
        self.identity_notice.setText(
            "Choose a Character Source (a series, world, or other origin) from the results, or create one with a new name. "
            "New Character Sources save immediately, even if character editing is cancelled. "
            "Image changes still require Save Changes."
        )
        # Keep exact-name creation beside its input, separate from row selection.
        buttons = self.create_exact_button.parentWidget()
        buttons.removeButton(self.create_exact_button)
        self.layout().removeWidget(self.exact_status)
        creation_row = QHBoxLayout()
        creation_row.addWidget(self.exact_status, 1)
        creation_row.addWidget(self.create_exact_button)
        creation_row.setAlignment(self.create_exact_button, Qt.AlignRight)
        self.layout().insertLayout(self.layout().indexOf(self.search) + 1, creation_row)
        self.selection_notice = QLabel()
        self.selection_notice.setWordWrap(True)
        self.selection_notice.setTextFormat(Qt.PlainText)
        self.selection_notice.setObjectName("mutedLabel")
        self.exact_status.setTextFormat(Qt.PlainText)
        self.layout().insertWidget(self.layout().indexOf(self.results) + 1, self.selection_notice)
        self.results.header().setStretchLastSection(True)
        self.results.header().resizeSection(0, 360)
        self.results.doubleClicked.disconnect()
        self.results.doubleClicked.connect(lambda *_: self._activate_selection())
        self.review_button = QPushButton("Review Source Tag Links…")
        self.review_button.clicked.connect(self._review_links)
        self.layout().addWidget(self.review_button)
        self.create_exact_button.setAutoDefault(False)
        self.search.installEventFilter(self)
        self._refresh()

    def eventFilter(self, watched, event):
        if (watched is self.search and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            # Consume the search key so QDialog cannot also click its default
            # selection button after we focus the intended explicit action.
            self._accept_search()
            return True
        return super().eventFilter(watched, event)

    def _refresh(self):
        super()._refresh()
        # A model reset to zero rows need not emit currentChanged. Clear the
        # previous target disclosure even when there is no new selection.
        self._selection_changed(self.results.currentIndex())
        source_name = self.search.text().strip()
        if not source_name:
            self.create_exact_button.setText("Create New Source…")
            self.create_exact_button.setAccessibleName(
                "Start creating a new Character Source"
            )
            self.create_exact_button.setToolTip(
                "Enter a new Character Source name, then create and use it."
            )
            self.create_exact_button.setVisible(True)
            self.create_exact_button.setEnabled(True)
        elif (not self.create_exact_button.isHidden()
                and self.create_exact_button.text() != "Review Removed Tag…"):
            self.create_exact_button.setToolTip(
                "Create this reusable Character Source and select it for the character."
            )
            self.create_exact_button.setAccessibleName(
                f'Create new source “{source_name}” and use it for this character'
            )
        self.choose_button.setVisible(bool(self.model.rowCount()))
        if (source_name and not self.create_exact_button.isHidden()
                and self.create_exact_button.text() != "Review Removed Tag…"):
            self.exact_status.setText(
                f'New source: “{source_name}”. Create it and use it for this character.'
            )
        if hasattr(self, "review_button"):
            conflict = self.service.database.conflicting_series([self.search.text().strip()])
            needs_review = bool(conflict and self.create_exact_button.isVisible()
                                and self.create_exact_button.text() != "Review Removed Tag…")
            self.review_button.setVisible(needs_review)
            if needs_review:
                self.create_exact_button.hide()
                self._show_identity_error(
                    f'“{conflict.name}” already has a source record. '
                    "Review its Tag link to use it here."
                )
        try:
            self.service.assert_fresh()
        except (OSError, RuntimeError) as error:
            self.choose_button.setEnabled(False)
            self.create_exact_button.setEnabled(False)
            self._show_identity_error(str(error))

    def _selection_changed(self, current, *_):
        super()._selection_changed(current)
        tag_id = self.model.stable_id(current)
        needs_source = bool(tag_id and self.service.source_tag_error((tag_id,)))
        needs_review = bool(needs_source and self.service.source_creation_error(tag_id))
        self.choose_button.setText("Review Source Tag Links…" if needs_review else
                                   "Use Selected Source")
        tag = self.knowledge.tags.get(tag_id)
        selected_name = tag.display if tag else ""
        consequence = (
            f'Review the source link for “{selected_name}” before using it.' if needs_review else
            f'Use “{selected_name}”. This also saves it as a reusable source.' if needs_source else
            f'Use “{selected_name}” for this character.' if tag else ""
        )
        self.choose_button.setToolTip(consequence)
        self.choose_button.setAccessibleName(consequence or "Use Selected Source")
        if hasattr(self, "selection_notice"):
            self.selection_notice.setText(consequence)
            self.selection_notice.setVisible(bool(tag))

    def _accept_search(self):
        # Typing a new name is a draft; the visible Create action is the decision.
        if self.create_exact_button.isVisible() and self.create_exact_button.isEnabled():
            self.create_exact_button.setFocus()
        elif self.model.rowCount():
            self._activate_selection()
        else:
            super()._accept_search()

    def _activate_selection(self):
        tag_id = self.model.stable_id(self.results.currentIndex())
        if tag_id and self.service.source_tag_error((tag_id,)):
            self.choose_button.setFocus()
        else:
            self._accept_existing()

    def _resolve_exact_search(self):
        source_name = self.search.text().strip()
        if not source_name:
            self.search.setFocus()
            self.exact_status.setText(
                "Enter the new Character Source name above. The action will change to "
                "Create New Source when the name is ready."
            )
            self.exact_status.setObjectName("mutedLabel")
            self.exact_status.style().unpolish(self.exact_status)
            self.exact_status.style().polish(self.exact_status)
            self.exact_status.show()
            return
        plan = self.identity_service.plan((source_name,), confirmation=True)
        if any(entry.status == "tombstone" for entry in plan.entries):
            try:
                self.service.assert_fresh()
                outcome = review_removed_identities(
                    self, self.identity_service, plan, source=self.identity_source,
                    continuation=self.identity_consequence,
                    recovery_resolver=self.identity_recovery_resolver,
                )
                if outcome.restored_ids:
                    self.service.accept_verified_tag_write()
                    self._create_or_use(self.knowledge.tags[outcome.restored_ids[0]].display)
            except (OSError, RuntimeError, ValueError) as error:
                self._show_identity_error(str(error))
            return
        self._create_or_use(source_name)

    def _accept_existing(self):
        try:
            self.service.assert_fresh()
        except (OSError, RuntimeError) as error:
            self._show_identity_error(str(error))
            return
        tag = self.knowledge.tags.get(self.model.stable_id(self.results.currentIndex()))
        if tag is not None:
            if (self.service.source_tag_error((tag.id,))
                    and self.service.source_creation_error(tag.id)):
                self._review_links()
                return
            self._create_or_use(tag.display)

    def _create_or_use(self, name):
        try:
            self._selected_tag_id = self.service.create_source_from_name(name, self.identity_service)
        except (OSError, RuntimeError, ValueError) as error:
            self._records = sorted(
                (tag for tag in self.knowledge.tags.values()
                 if tag.id not in self.exclude_ids and tag.category in self.category_ids),
                key=lambda tag: (tag.display.casefold(), tag.id),
            )
            self._show_identity_error(str(error))
            return
        super().accept()

    def _review_links(self):
        try:
            self.service.assert_fresh()
        except (OSError, RuntimeError) as error:
            self._show_identity_error(str(error))
            return
        self.review_requested = True
        self.reject()


class CharacterRecordEditorDialog(FeedbackEnabledDialog):
    """Focused automatic editor for one typed record linked behind an SDXL Tag."""

    feedback_workflow = "Character and outfit editor"

    record_saved=Signal(str,str)
    AUTOSAVE_DELAY_MS = 500
    CHARACTER_IDENTITY_CATEGORY = "character_identity"
    CHARACTER_SOURCE_CATEGORY = "copyright_series"
    CHARACTER_OUTFIT_CATEGORY = "character_outfit"

    def _role_for_kind(self) -> str:
        return (
            "character_source"
            if self.kind == "series"
            else "character_outfit"
            if self.kind == "outfit"
            else "character_identity"
        )

    def _tag_has_role(self, tag, role: str) -> bool:
        return bool(
            tag is not None
            and self.categories.effective_semantic_role(tag.category) == role
        )

    def _category_ids_for_role(self, role: str) -> tuple[str, ...]:
        return self.categories.category_ids_for_role(role)

    def __init__(
        self, service, tags, categories, *, kind: str, record_id: str = "",
        tag_id: str = "", create: bool = False, embedded: bool = False, parent=None,
    ):
        super().__init__(parent)
        self.embedded=bool(embedded)
        if self.embedded:
            self.setWindowFlags(Qt.Widget)
        self.service: CharacterRecordEditService = service
        self.database = service.database
        self.tags = tags
        self.categories = categories
        kind_key = str(kind).casefold()
        self.kind = (
            "series"
            if kind_key.startswith(("series", "source"))
            else "character"
            if kind_key.startswith("character")
            else "outfit"
        )
        self.record_id = str(record_id)
        self.tag_id = str(tag_id)
        self.creating = bool(create)
        if self.embedded and self.creating:
            raise ValueError("Embedded Character Knowledge editing is available after the typed record is created.")
        self.saved = False
        self._loading = True
        self._dirty = False
        self._last_error = ""
        self._save_task = None
        self._save_pool = QThreadPool.globalInstance()
        self._close_after_save = False
        self._unlinked_character_ids: tuple[str, ...] = ()
        self._unlinked_outfit_ids: tuple[str, ...] = ()
        self._unlinked_series_id = ""
        self._unlinked_base_character_id = ""
        self._source_link_touched = False
        self._version_link_touched = False
        self._outfit_link_touched = False
        self._source_relationship_unresolved = False
        self._version_relationship_unresolved = False
        self._associated_character_relationship_unresolved = False
        self._associated_character_link_touched = False
        self._tag_to_character: dict[str, str] = {}
        record = self._record()
        if not self.creating and record is not None:
            self.tag_id = self.database.entity_tag_id(self.kind, record.id)
        tag = self.tags.tags.get(self.tag_id) if self.creating else None
        if not self.creating and record is None:
            raise ValueError("The linked typed record is no longer available.")
        if self.creating and not self._tag_has_role(tag, self._role_for_kind()):
            label = (
                "Character Source"
                if self.kind == "series"
                else "Character Outfit"
                if self.kind == "outfit"
                else "Character Identity"
            )
            raise ValueError(f"The {label} SDXL Tag is no longer available.")

        noun = (
            "Character Source"
            if self.kind == "series"
            else "Character"
            if self.kind == "character"
            else "Outfit"
        )
        self.setWindowTitle(f"Create {noun} for SDXL Tag — {tag.display}" if self.creating else f"Edit {noun} — {record.name}")
        if self.embedded:
            self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        else:
            self.resize(900, 680)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Preferred name and aliases are edited once in the shared Tag Details fields above. "
            "These character-only fields save automatically after validation; Tag Details Save/Discard does not roll back an already-saved Character record. and do not assign facts to images or rewrite captions, Metadata, TXT, or image files."
            if self.embedded else
            ("This creates one typed Character Knowledge record linked behind the existing SDXL Tag. "
             if self.creating else "This edits the typed Character Knowledge record linked behind the SDXL Tag. ")
            + "It does not rename the tag, assign facts to images, or rewrite captions, Metadata, TXT, or image files."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        intro.setVisible(not self.embedded)
        if self.creating and self.kind == "character":
            intro.setText(
                f'Create a reusable character for “{tag.display}”. Choose its Character Source below: a series, world, or other origin. '
                "Optional relationships and descriptive knowledge are in the other tabs. "
                "Image changes still require Save Changes in the image editor."
            )
        layout.addWidget(intro)

        self.tabs = None
        if not self.embedded:
            self.tabs = QTabWidget()
            layout.addWidget(self.tabs, 1)
        basics = QWidget()
        self.identity_page=basics
        basics_form = QFormLayout(basics)
        _configure_form(basics_form)
        self.name = QLineEdit()
        self.name.setAccessibleName("Typed record preferred name")
        self.aliases = TokenChipEditor(kind="alias", allow_text=True)
        basics_form.addRow("Preferred name", self.name)
        if self.kind == "character":
            self.identity_type = QComboBox()
            for label, value in (
                ("Known character", "canonical"), ("Original character", "original"),
                ("Random/generated character", "generic"), ("Unknown character", "unknown"),
            ):
                self.identity_type.addItem(label, value)
            basics_form.addRow("Identity type", self.identity_type)
        basics_form.addRow("Other names / aliases", self.aliases)
        if self.embedded:
            for field in (self.name,self.aliases):
                label=basics_form.labelForField(field)
                if label is not None:
                    label.hide()
                field.hide()
            if self.kind == "character":
                self._add_embedded_section(layout, "Character details", basics)
        else:
            self.tabs.addTab(basics, "Identity")

        relationships = QWidget()
        relationships_form = QFormLayout(relationships)
        _configure_form(relationships_form)
        self.sdxl_tags = TokenChipEditor(kind="collection component SDXL Tag", allow_text=True)
        self.style_tags = TokenChipEditor(kind="style SDXL Tag", allow_text=True)
        self.descriptions = QTextEdit()
        self.descriptions.setPlaceholderText("One reusable Plain Language Description per line")
        self.descriptions.setAccessibleName("Reusable Plain Language Descriptions")
        self.descriptions.setMaximumHeight(120)
        if self.kind == "character":
            self.source_tags = TagReferenceEditor(
                tags, categories, relation_label="Character Source",
                single=True, category_ids=self._category_ids_for_role("character_source"),
                picker_factory=self._source_picker,
            )
            self.source_tags.set_context(self.tag_id, self._source_link_error)
            source_form = basics_form if self.creating else relationships_form
            source_form.addRow("Character Source", self.source_tags)
            self.source_link_status = QLabel()
            self.source_link_status.setWordWrap(True)
            self.source_link_status.setObjectName("mutedLabel")
            self.clear_legacy_source = QPushButton("Remove Legacy Source")
            self.clear_legacy_source.setProperty("compact", True)
            self.clear_legacy_source.hide()
            self.review_source_links = QPushButton("Review Character Source Links…")
            self.review_source_links.setProperty("compact", True)
            self.review_source_links.setToolTip(
                "Review exact Character Source records against existing Copyright / Series SDXL Tags."
            )
            self.source_status_container = QWidget()
            self.source_status_layout = QVBoxLayout(self.source_status_container)
            self.source_status_layout.setContentsMargins(0, 0, 0, 0)
            self.source_status_layout.addWidget(self.source_link_status)
            source_status_actions = QHBoxLayout()
            source_status_actions.addWidget(self.clear_legacy_source)
            source_status_actions.addWidget(self.review_source_links)
            source_status_actions.addStretch(1)
            self.source_status_layout.addLayout(source_status_actions)
            source_form.addRow("Character Source status", self.source_status_container)
            self.outfits = TagReferenceEditor(
                tags, categories, relation_label="Associated Outfit",
                category_ids=self._category_ids_for_role("character_outfit"),
            )
            self.outfits.set_context(self.tag_id, self._outfit_link_error)
            relationships_form.addRow("Potential outfits", self.outfits)
            self.outfit_link_status = QLabel()
            self.outfit_link_status.setWordWrap(True)
            self.outfit_link_status.setObjectName("mutedLabel")
            relationships_form.addRow("Outfit status", self.outfit_link_status)
            self.version_base = TagReferenceEditor(
                tags, categories, relation_label="Version Of",
                single=True, category_ids=self._category_ids_for_role("character_identity"),
            )
            self.version_base.set_context(self.tag_id, self._version_base_error)
            relationships_form.addRow("Version of", self.version_base)
            self.version_type = QComboBox()
            self.version_type.addItem("Not a version", "")
            for value, label in CHARACTER_IDENTITY_VARIANT_TYPE_LABELS.items():
                self.version_type.addItem(label, value)
            relationships_form.addRow("Version type", self.version_type)
            self.identity_relationship_status = QLabel()
            self.identity_relationship_status.setWordWrap(True)
            self.identity_relationship_status.setObjectName("mutedLabel")
            relationships_form.addRow("Identity relationship", self.identity_relationship_status)
            self.inference_status = QLabel()
            self.inference_status.setWordWrap(True)
            self.inference_status.setObjectName("mutedLabel")
            relationships_form.addRow("Name suggestions", self.inference_status)
        elif self.kind == "outfit":
            self.source_tags = TagReferenceEditor(
                tags, categories, relation_label="Character Source",
                single=True, category_ids=self._category_ids_for_role("character_source"),
            )
            self.source_tags.set_context(self.tag_id,self.service.source_tag_error)
            relationships_form.addRow("Character Source",self.source_tags)
            self.source_link_status=QLabel(); self.source_link_status.setWordWrap(True); self.source_link_status.setObjectName("mutedLabel")
            relationships_form.addRow("Character Source status",self.source_link_status)
            self.characters = TagReferenceEditor(
                tags, categories, relation_label="Associated Character",
                category_ids=self._category_ids_for_role("character_identity"),
            )
            self.characters.set_context("", self._character_link_error)
            relationships_form.addRow("Associated characters", self.characters)
            self.character_link_status = QLabel()
            self.character_link_status.setWordWrap(True)
            self.character_link_status.setObjectName("mutedLabel")
            relationships_form.addRow("Relationship status", self.character_link_status)
        if self.kind == "outfit":
            relationships_form.addRow("Outfit collection SDXL Tags", self.sdxl_tags)
            relationships_form.addRow("Reusable descriptions", self.descriptions)
            relationships_form.addRow("Original / style SDXL Tags", self.style_tags)
        elif self.kind == "series":
            relationships_form.addRow("Original / style SDXL Tags", self.style_tags)
        if self.embedded:
            title = "Character Source details" if self.kind == "series" else "Relationships"
            self._add_embedded_section(layout, title, relationships)
        else:
            self.tabs.addTab(relationships, "Relationships")

        details = QWidget()
        details_form = QFormLayout(details)
        _configure_form(details_form)
        if self.kind == "character":
            self.origin_label = QLineEdit()
            self.creator = QLineEdit()
            details_form.addRow("Origin label", self.origin_label)
            details_form.addRow("Creator", self.creator)
            self.evidence = QTextEdit()
            self.evidence.setReadOnly(True)
            self.evidence.setAccessibleName("Observed tendency evidence and references")
            self.evidence.setMaximumHeight(120)
            details_form.addRow("Tendency evidence", self.evidence)
            details_form.addRow("Character collection SDXL Tags", self.sdxl_tags)
            details_form.addRow("Reusable descriptions", self.descriptions)
            details_form.addRow("Original / style SDXL Tags", self.style_tags)
        elif self.kind == "outfit":
            self.notes = QTextEdit()
            self.notes.setAccessibleName("Outfit notes")
            self.notes.setMaximumHeight(120)
            details_form.addRow("Notes", self.notes)
        if self.embedded and self.kind != "series":
            self._add_embedded_section(
                layout,
                "Reusable knowledge and references" if self.kind == "character" else "Notes",
                details,
            )
        elif self.kind != "series":
            self.tabs.addTab(details, "Notes && References")

        initial_status = "Nothing is created until you choose Create." if self.creating else "Changes save automatically after each validated edit."
        self.readiness_status = QLabel(initial_status if self.creating else "")
        self.readiness_status.setWordWrap(True)
        self.readiness_status.setObjectName("mutedLabel")
        self.readiness_status.setAccessibleName("Typed record readiness")
        self.readiness_status.setVisible(self.creating)
        layout.addWidget(self.readiness_status)
        self.status = QLabel(initial_status, self)
        self.status.setWordWrap(True)
        self.status.setObjectName("mutedLabel")
        self.status.setAccessibleName("Typed record automatic save status")
        self.status.setVisible(not self.creating)
        bind_status_footer(
            self, self.status, title="Character Knowledge",
            follow_visibility=self.embedded,
        )
        self.buttons=None
        if not self.embedded:
            buttons = QDialogButtonBox(QDialogButtonBox.Cancel if self.creating else QDialogButtonBox.Close)
            self.buttons=buttons
            if self.creating:
                self.create_button = buttons.addButton(f"Create {noun} Record", QDialogButtonBox.AcceptRole)
                self.create_button.setProperty("primary", True)
                self.create_button.clicked.connect(self._create_record)
            buttons.rejected.connect(self.reject if self.creating else self.close)
            layout.addWidget(buttons)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.AUTOSAVE_DELAY_MS)
        self.timer.timeout.connect(self._autosave)
        if self.creating:
            self.name.setText(tag.display)
            self.aliases.set_values(())
            self.sdxl_tags.set_values(())
            self.style_tags.set_values(())
            self.descriptions.clear()
            if self.kind == "outfit":
                self.source_tags.set_values(())
                self._refresh_outfit_source_status()
                self._load_outfit_characters(())
                self.notes.clear()
            elif self.kind == "character":
                self.identity_relationship_status.setText(
                    "Primary identity; no reviewed broader-character relationship is selected."
                )
                self.evidence.setPlainText("No observed tendency evidence recorded.")
                suggestions = self.service.relationship_suggestions(tag.id)
                self.source_tags.set_values(
                    (suggestions.source_tag_id,) if suggestions.source_tag_id else (),
                )
                self.outfits.set_values(suggestions.outfit_tag_ids)
                suggested = []
                if suggestions.source_tag_id:
                    suggested.append("Character Source")
                if suggestions.outfit_tag_ids:
                    suggested.append(
                        f"{len(suggestions.outfit_tag_ids):,} outfit"
                        + ("s" if len(suggestions.outfit_tag_ids) != 1 else "")
                    )
                text = (
                    "Suggested from the character tag name: " + " and ".join(suggested) + "."
                    if suggested else "No exact source or outfit relationship was inferred from the tag name."
                )
                if suggestions.review_notes:
                    text += " Review: " + " ".join(suggestions.review_notes)
                self.inference_status.setText(text)
                self._refresh_character_relationship_status()
        else:
            self._load(record)
        self._connect_changes()
        self._loading = False

    def _add_embedded_section(
        self, layout: QVBoxLayout, title: str, page: QWidget,
    ) -> None:
        section = QGroupBox(title)
        section.setAccessibleName(title)
        section.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        section_layout = QVBoxLayout(section)
        section_layout.addWidget(page)
        layout.addWidget(section)

    def _record(self):
        records = (
            self.database.series
            if self.kind == "series"
            else self.database.characters
            if self.kind == "character"
            else self.database.outfits
        )
        return records.get(self.record_id)

    def _load(self, record) -> None:
        self.name.setText(record.name)
        self.aliases.set_values(record.aliases)
        self.sdxl_tags.set_values(getattr(record, "sdxl_tags", ()))
        self.descriptions.setPlainText("\n".join(getattr(record, "plain_language", ())))
        if self.kind == "character":
            self._load_character_relationships(record)
            self.identity_type.setCurrentIndex(max(0, self.identity_type.findData(record.identity_type)))
            self.style_tags.set_values(record.original_style_tags)
            self.origin_label.setText(record.origin_label)
            self.creator.setText(record.creator)
            lines = []
            for relation in record.tag_associations:
                label = str(relation.get("tag") or "Unnamed tendency")
                source = str(relation.get("source") or "No evidence reference recorded")
                parts = [label]
                observations = relation.get("observations")
                if observations not in (None, ""):
                    parts.append(f"{int(observations):,} observations")
                parts.append(f"evidence/reference: {source}")
                lines.append(" — ".join(parts))
            self.evidence.setPlainText("\n".join(lines) or "No observed tendency evidence or references recorded.")
        elif self.kind == "outfit":
            self._load_outfit_source(record)
            self.style_tags.set_values(record.style_tags)
            associated = self.database.character_relationship(
                ASSOCIATED_CHARACTER,"outfit",record.id
            )
            self._associated_character_relationship_unresolved = bool(
                associated.represented and not associated.target_ids
            )
            self._load_outfit_characters(associated.target_ids)
            if self._associated_character_relationship_unresolved:
                self.character_link_status.setText(
                    "The canonical Associated Character decision is disabled or unavailable. "
                    "Legacy compatibility values are preserved but are not active; choose Character Identity Tags to review the relationship."
                )
            self._associated_character_link_touched=False
            self.notes.setPlainText(record.notes)
        else:
            self.style_tags.set_values(record.original_style_tags)

    def _load_outfit_source(self,record) -> None:
        self._unlinked_series_id=""
        values=()
        relationship=self.database.character_relationship(
            CHARACTER_SOURCE,"outfit",record.id
        )
        self._source_relationship_unresolved=bool(
            relationship.represented and not relationship.target_id
        )
        source_id=relationship.target_id
        source=self.database.series.get(source_id)
        if source is not None:
            link=self.database.effective_knowledge_tag("series",source.id)
            tag_id=str(link.get("tag_id") or "")
            if (
                link.get("status")=="reviewed"
                and tag_id in self.tags.tags
                and self._tag_has_role(self.tags.tags[tag_id],"character_source")
            ):
                values=(tag_id,)
            else:
                self._unlinked_series_id=source.id
        elif self._source_relationship_unresolved:
            self._unlinked_series_id=str(record.series_id or "")
        self.source_tags.set_values(values)
        self._source_link_touched=False
        self._refresh_outfit_source_status()

    def _refresh_outfit_source_status(self) -> None:
        if self.kind!="outfit":
            return
        source=self.database.series.get(self._unlinked_series_id)
        if self._source_relationship_unresolved:
            self.source_link_status.setText(
                "The canonical Character Source decision is disabled, conflicting, or unavailable. "
                "Its legacy projection is preserved but is not active until you choose a Character Source Tag."
            )
        elif source is not None:
            self.source_link_status.setText(
                f'Legacy Character Source "{source.name}" is retained until you select its reviewed Character Source SDXL Tag.'
            )
        elif self.source_tags.values():
            self.source_link_status.setText("The outfit's Character Source is selected through its reviewed Character Source SDXL Tag.")
        else:
            self.source_link_status.setText("No Character Source is selected.")

    def _load_character_relationships(self, record) -> None:
        self._unlinked_base_character_id = ""
        version_relationship=self.database.character_relationship(
            VERSION_OF,"character",record.id
        )
        self._version_relationship_unresolved=bool(
            version_relationship.represented and not version_relationship.target_id
        )
        base_id, variant_type = self.database.version_of(record.id)
        if self._version_relationship_unresolved:
            self._unlinked_base_character_id=str(record.base_character_id or "")
            variant_type=str(record.variant_type or "")
        base = self.database.characters.get(base_id)
        if base is not None:
            base_tag_id = self.database.entity_tag_id("character", base.id)
            base_tag = self.tags.tags.get(base_tag_id) if base_tag_id else None
            if base_tag is not None and self._tag_has_role(base_tag, "character_identity"):
                self.version_base.set_values((base_tag.id,))
                self._tag_to_character[base_tag.id] = base.id
            else:
                self.version_base.set_values(())
                self._unlinked_base_character_id = base.id
            base_label = base_tag.display if base_tag is not None else base.name
            relationship_label = CHARACTER_IDENTITY_VARIANT_TYPE_LABELS.get(
                variant_type,
                str(variant_type or "Character Variation").replace("_", " ").title(),
            )
            self.identity_relationship_status.setText(
                f"{relationship_label} of {base_label}. The identities remain distinct; this reviewed Version Of relationship does not merge their Tags."
            )
        else:
            self.version_base.set_values(())
        self.version_type.setCurrentIndex(max(0, self.version_type.findData(variant_type)))
        if self._version_relationship_unresolved:
            self.identity_relationship_status.setText(
                "The canonical Version Of decision is disabled, conflicting, or unavailable. "
                "Its legacy projection is preserved but is not active until you review this relationship."
            )
        elif base_id and (base is None or not variant_type):
            self.identity_relationship_status.setText(
                "This variation relationship is incomplete and needs System Knowledge review."
            )
        elif not base_id:
            self.identity_relationship_status.setText(
                "Primary identity; no reviewed broader-character relationship is selected."
            )
        self._version_link_touched = False
        self._unlinked_series_id = ""
        source_tag_ids = ()
        source_relationship=self.database.character_relationship(
            CHARACTER_SOURCE,"character",record.id
        )
        self._source_relationship_unresolved=bool(
            source_relationship.represented and not source_relationship.target_id
        )
        source_id = source_relationship.target_id
        source = self.database.series.get(source_id)
        if source is not None:
            source_link = self.database.effective_knowledge_tag("series", source.id)
            source_tag_id = str(source_link.get("tag_id") or "")
            if (
                source_link.get("status") == "reviewed"
                and source_tag_id in self.tags.tags
                and self._tag_has_role(self.tags.tags[source_tag_id], "character_source")
            ):
                source_tag_ids = (source_tag_id,)
            else:
                self._unlinked_series_id = source.id
        elif self._source_relationship_unresolved:
            self._unlinked_series_id=str(record.series_id or "")
        self.source_tags.set_values(source_tag_ids)

        linked_outfit_tags = []
        unlinked_outfits = []
        for outfit in self.database.outfits.values():
            if record.id not in self.database.associated_character_ids(outfit.id):
                continue
            outfit_link = self.database.effective_knowledge_tag("outfit", outfit.id)
            outfit_tag_id = str(outfit_link.get("tag_id") or "")
            if (
                outfit_link.get("status") == "reviewed"
                and outfit_tag_id in self.tags.tags
                and self._tag_has_role(self.tags.tags[outfit_tag_id], "character_outfit")
            ):
                linked_outfit_tags.append(outfit_tag_id)
            else:
                unlinked_outfits.append(outfit.id)
        self._unlinked_outfit_ids = tuple(dict.fromkeys(unlinked_outfits))
        self.outfits.set_values(linked_outfit_tags)
        self._outfit_link_touched=False
        suggestions = self.service.relationship_suggestions(
            self.tag_id, expected_series_id=source_id,
        )
        suggested = []
        if suggestions.source_tag_id and suggestions.source_tag_id not in source_tag_ids:
            source_tag = self.tags.tags.get(suggestions.source_tag_id)
            if source_tag is not None:
                suggested.append(f'Character Source "{source_tag.display}"')
        for outfit_tag_id in suggestions.outfit_tag_ids:
            if outfit_tag_id in linked_outfit_tags:
                continue
            outfit_tag = self.tags.tags.get(outfit_tag_id)
            if outfit_tag is not None:
                suggested.append(f'outfit "{outfit_tag.display}"')
        text = (
            "Safe name suggestion available: " + ", ".join(suggested) + ". Choose the tag to apply it."
            if suggested else "No unapplied exact relationship suggestion from the character tag name."
        )
        if suggestions.review_notes:
            text += " Review: " + " ".join(suggestions.review_notes)
        self.inference_status.setText(text)
        self._source_link_touched = False
        self._refresh_character_relationship_status()

    def _refresh_character_relationship_status(self) -> None:
        if self.kind != "character":
            return
        source = self.database.series.get(self._unlinked_series_id)
        if self._source_relationship_unresolved:
            self.source_link_status.setText(
                "The canonical Character Source decision is disabled, conflicting, or unavailable. "
                "Its legacy projection is preserved but is not active until you choose a Character Source Tag."
            )
            self.clear_legacy_source.hide()
        elif source is not None:
            self.source_link_status.setText(
                f'Legacy Character Source "{source.name}" is retained. Choose its existing '
                "Character Source SDXL Tag to review the link, or remove the legacy association explicitly."
            )
            self.clear_legacy_source.show()
        elif self.source_tags.values():
            self.source_link_status.setText(
                "The Character Source is selected through its reviewed Copyright / Series SDXL Tag."
            )
            self.clear_legacy_source.hide()
        else:
            self.source_link_status.setText(
                "No Character Source is selected."
            )
            self.clear_legacy_source.hide()
        if self._unlinked_outfit_ids:
            names = [
                self.database.outfits[value].name
                for value in self._unlinked_outfit_ids
                if value in self.database.outfits
            ]
            self.outfit_link_status.setText(
                f"{len(self._unlinked_outfit_ids):,} legacy outfit association"
                + ("s are" if len(self._unlinked_outfit_ids) != 1 else " is")
                + " retained but cannot be edited as tag chips until reviewed: "
                + ", ".join(names[:6])
            )
        else:
            self.outfit_link_status.setText(
                "Potential outfits are reviewed Outfit SDXL Tags linked to this character; this does not assign an outfit to an image."
            )

    def _load_outfit_characters(self, character_ids) -> None:
        linked_tags = []
        unlinked = []
        for character_id in character_ids:
            character = self.database.characters.get(character_id)
            link = (
                self.database.effective_knowledge_tag("character", character.id)
                if character is not None else {}
            )
            tag_id = str(link.get("tag_id") or "")
            if (
                character is not None
                and link.get("status") == "reviewed"
                and tag_id in self.tags.tags
                and self._tag_has_role(self.tags.tags[tag_id], "character_identity")
            ):
                linked_tags.append(tag_id)
                self._tag_to_character[tag_id] = character_id
            elif character_id:
                unlinked.append(character_id)
        for character in self.database.characters.values():
            link = self.database.effective_knowledge_tag("character", character.id)
            tag_id = str(link.get("tag_id") or "")
            if (
                link.get("status") == "reviewed"
                and tag_id in self.tags.tags
                and self._tag_has_role(self.tags.tags[tag_id], "character_identity")
            ):
                self._tag_to_character.setdefault(tag_id, character.id)
        self._unlinked_character_ids = tuple(dict.fromkeys(unlinked))
        self.characters.set_values(linked_tags)
        if unlinked:
            names = [self.database.characters[value].name for value in unlinked if value in self.database.characters]
            self.character_link_status.setText(
                f"{len(unlinked):,} legacy association{'s are' if len(unlinked) != 1 else ' is'} retained but cannot be changed as tag chips until linked: "
                + ", ".join(names[:6])
            )
        else:
            self.character_link_status.setText(
                "Associated characters are reviewed Character Identity SDXL Tag links; they do not assign a wearer to an image."
            )

    def _connect_changes(self) -> None:
        self.name.textEdited.connect(self._queue_save)
        self.name.editingFinished.connect(self._flush)
        self.aliases.changed.connect(self._queue_save)
        self.sdxl_tags.changed.connect(self._queue_save)
        self.style_tags.changed.connect(self._queue_save)
        self.descriptions.textChanged.connect(self._queue_save)
        if self.kind == "character":
            self.source_tags.changed.connect(self._source_relationship_changed)
            self.outfits.changed.connect(self._outfit_relationship_changed)
            self.version_base.changed.connect(self._version_relationship_changed)
            self.version_type.activated.connect(self._version_type_changed)
            self.clear_legacy_source.clicked.connect(self._clear_legacy_source)
            self.review_source_links.clicked.connect(self._review_source_tag_links)
            self.identity_type.activated.connect(self._queue_save)
            self.origin_label.textEdited.connect(self._queue_save)
            self.origin_label.editingFinished.connect(self._flush)
            self.creator.textEdited.connect(self._queue_save)
            self.creator.editingFinished.connect(self._flush)
        elif self.kind == "outfit":
            self.source_tags.changed.connect(self._outfit_source_changed)
            self.characters.changed.connect(self._associated_characters_changed)
            self.notes.textChanged.connect(self._queue_save)

    def _outfit_source_changed(self,*_args) -> None:
        self._source_link_touched=True
        self._source_relationship_unresolved=False
        self._unlinked_series_id=""
        self._refresh_outfit_source_status()
        self._queue_save()

    def _associated_characters_changed(self,*_args) -> None:
        self._associated_character_link_touched=True
        self._associated_character_relationship_unresolved=False
        self._queue_save()

    def _character_link_error(self, tag_ids) -> str:
        missing = [value for value in tag_ids if value not in self._tag_to_character]
        if missing:
            return "Choose Character Identity SDXL Tags that have one reviewed typed character link."
        owners = [self._tag_to_character[value] for value in tag_ids]
        if len(set(owners)) != len(owners):
            return "Two selected SDXL Tags resolve to the same typed character record."
        return ""

    def _version_base_error(self, tag_ids) -> str:
        values = tuple(str(value) for value in tag_ids if str(value))
        if len(values) > 1:
            return "Choose one broader Character Identity SDXL Tag."
        if not values:
            return ""
        try:
            character_id = self.service.character_id_for_tag(values[0])
        except (RuntimeError, ValueError) as error:
            return str(error)
        if character_id == self.record_id:
            return "A character cannot be a version of itself."
        return ""

    def _version_relationship_changed(self, *_args) -> None:
        self._version_link_touched = True
        self._version_relationship_unresolved = False
        self._unlinked_base_character_id = ""
        if self.version_base.values() and not self.version_type.currentData():
            self.version_type.setCurrentIndex(self.version_type.findData("form"))
        if not self.version_base.values():
            self.version_type.setCurrentIndex(0)
        self._queue_save()

    def _version_type_changed(self, *_args) -> None:
        self._version_link_touched = True
        self._version_relationship_unresolved = False
        if not self.version_type.currentData():
            self.version_base.set_values(())
            self._unlinked_base_character_id = ""
        self._queue_save()

    def _source_link_error(self, tag_ids) -> str:
        error = self.service.source_tag_error(tag_ids)
        if error or not tag_ids:
            return error
        outfits = getattr(self, "outfits", None)
        return self.service.outfit_tag_error(
            outfits.values() if outfits is not None else (),
            source_tag_id=str(tag_ids[0]),
        )

    def _outfit_link_error(self, tag_ids) -> str:
        source_tag_ids = self.source_tags.values()
        return self.service.outfit_tag_error(
            tag_ids,
            source_tag_id=source_tag_ids[0] if source_tag_ids else "",
        )

    def _source_relationship_changed(self, *_args) -> None:
        self._source_link_touched = True
        self._source_relationship_unresolved = False
        self._refresh_character_relationship_status()
        self._queue_save()

    def _outfit_relationship_changed(self,*_args) -> None:
        self._outfit_link_touched=True
        self._queue_save()

    def _clear_legacy_source(self) -> None:
        self._unlinked_series_id = ""
        self._source_link_touched = True
        self._source_relationship_unresolved = False
        self._refresh_character_relationship_status()
        self._queue_save()

    def _review_source_tag_links(self, _checked=False, *, query="") -> None:
        from core import CharacterTagBridge
        from .character_tag_bridge_dialog import CharacterTagBridgeDialog

        dialog = CharacterTagBridgeDialog(
            CharacterTagBridge(self.tags, self.database, self.categories),
            self.tags,
            self.database,
            categories=self.categories,
            parent=self,
        )
        dialog.filter.setCurrentIndex(dialog.filter.findData("series"))
        source = self.database.series.get(self._unlinked_series_id)
        if query:
            dialog.search.setText(query)
        elif source is not None:
            dialog.search.setText(source.name)
        elif self.source_tags.values():
            tag = self.tags.tags.get(self.source_tags.values()[0])
            if tag is not None:
                dialog.search.setText(tag.display)
        dialog.exec()
        if not dialog.applied:
            return
        self.database.load()
        self.service = CharacterRecordEditService(self.database, self.tags, self.categories)
        self._loading = True
        try:
            if self.creating:
                suggestions = self.service.relationship_suggestions(self.tag_id)
                if suggestions.source_tag_id and not self.source_tags.values():
                    self.source_tags.set_values((suggestions.source_tag_id,))
                if suggestions.outfit_tag_ids and not self.outfits.values():
                    self.outfits.set_values(suggestions.outfit_tag_ids)
                self.inference_status.setText(
                    "Exact reviewed source and outfit suggestions were refreshed from the character tag name."
                )
                self._refresh_character_relationship_status()
            else:
                record = self._record()
                if record is not None:
                    self._load_character_relationships(record)
        finally:
            self._loading = False

    def _source_picker(self, *args, **kwargs):
        if self._save_task is not None:
            self._set_status("The current edit is saving. Choose a Character Source after it finishes.")
            return None
        self.timer.stop()
        dialog = CharacterSourcePickerDialog(*args, service=self.service, **kwargs)

        def finished(_result):
            if dialog.review_requested:
                self._review_source_tag_links(query=dialog.search.text().strip())
            if self._dirty and not self.creating:
                self.timer.start()
            if self.creating:
                self._set_status(
                    "Character Source changes save independently. The character is created "
                    "only when you choose Create Character Record."
                )

        dialog.finished.connect(finished)
        return dialog

    def _queue_save(self, *_args) -> None:
        if self._loading or self.creating:
            return
        self._dirty = True
        self._set_status("Saving typed Character Knowledge…")
        self.timer.start()

    def _flush(self, *_args) -> None:
        if self.creating:
            return
        if self._dirty:
            self.timer.stop()
            self._autosave()

    def _description_values(self) -> list[str]:
        return [line.strip() for line in self.descriptions.toPlainText().splitlines() if line.strip()]

    def _create_record(self) -> None:
        try:
            if self.kind == "series":
                result = self.service.create_series(
                    self.tag_id,
                    name=self.name.text(),
                    aliases=self.aliases.values(),
                    original_style_tags=self.style_tags.values(),
                )
            elif self.kind == "character":
                source_tag_ids = self.source_tags.values()
                source_error = self._source_link_error(source_tag_ids)
                outfit_error = self._outfit_link_error(self.outfits.values())
                if source_error or outfit_error:
                    raise ValueError(source_error or outfit_error)
                result = self.service.create_character(
                    self.tag_id, name=self.name.text(), series_id=None,
                    identity_type=str(self.identity_type.currentData() or "unknown"), aliases=self.aliases.values(),
                    sdxl_tags=self.sdxl_tags.values(), plain_language=self._description_values(),
                    original_style_tags=self.style_tags.values(), origin_label=self.origin_label.text(),
                    creator=self.creator.text(),
                    source_tag_id=source_tag_ids[0] if source_tag_ids else "",
                    outfit_tag_ids=self.outfits.values(),
                    base_character_id=(
                        self.service.character_id_for_tag(self.version_base.values()[0])
                        if self.version_base.values() else ""
                    ),
                    variant_type=str(self.version_type.currentData() or ""),
                )
            else:
                tag_ids = self.characters.values()
                error = self._character_link_error(tag_ids)
                if error:
                    raise ValueError(error)
                source_tag_ids=self.source_tags.values()
                source_error=self.service.source_tag_error(source_tag_ids)
                if source_error:
                    raise ValueError(source_error)
                result = self.service.create_outfit(
                    self.tag_id, name=self.name.text(), series_id=None,
                    character_ids=tuple(self._tag_to_character[value] for value in tag_ids),
                    aliases=self.aliases.values(), sdxl_tags=self.sdxl_tags.values(),
                    plain_language=self._description_values(), style_tags=self.style_tags.values(),
                    notes=self.notes.toPlainText(),
                    source_tag_id=source_tag_ids[0] if source_tag_ids else "",
                )
        except Exception as error:
            self._last_error = str(error)
            self._set_status(f"Not created: {error}", error=True)
            return
        self.record_id = result.record_id
        self.saved = True
        self._last_error = ""
        self.accept()

    def _save_request(self):
        """Validate and snapshot Qt-owned form state before background I/O."""
        try:
            if self.kind == "series":
                return {
                    "kind":"series","record_id":self.record_id,
                    "name":self.name.text(),"aliases":self.aliases.values(),
                    "original_style_tags":self.style_tags.values(),
                }
            elif self.kind == "character":
                source_tag_ids = self.source_tags.values()
                source_explicit = self._source_link_touched
                current_record=self._record()
                series_id=(
                    (self._unlinked_series_id or None)
                    if source_explicit
                    else (current_record.series_id if current_record is not None else None)
                )
                source_error = self._source_link_error(source_tag_ids)
                outfit_error = self._outfit_link_error(self.outfits.values())
                if source_error or outfit_error:
                    raise ValueError(source_error or outfit_error)
                return {
                    "kind":"character","record_id":self.record_id,
                    "name":self.name.text(),"series_id":series_id,
                    "identity_type":str(self.identity_type.currentData() or "unknown"),
                    "aliases":self.aliases.values(),"sdxl_tags":self.sdxl_tags.values(),
                    "plain_language":self._description_values(),
                    "original_style_tags":self.style_tags.values(),
                    "origin_label":self.origin_label.text(),"creator":self.creator.text(),
                    "source_tag_id":source_tag_ids[0] if source_tag_ids else "",
                    "source_tag_explicit":source_explicit,
                    "outfit_tag_ids":(
                        self.outfits.values() if self._outfit_link_touched else None
                    ),
                    "base_character_id":(
                        self.service.character_id_for_tag(self.version_base.values()[0])
                        if self.version_base.values()
                        else self._unlinked_base_character_id or None
                    ),
                    "variant_type":str(self.version_type.currentData() or ""),
                    "version_explicit":self._version_link_touched,
                }
            else:
                tag_ids = self.characters.values()
                error = self._character_link_error(tag_ids)
                if error:
                    raise ValueError(error)
                source_tag_ids=self.source_tags.values()
                source_error=self.service.source_tag_error(source_tag_ids)
                if source_error:
                    raise ValueError(source_error)
                character_ids = (*self._unlinked_character_ids, *(self._tag_to_character[value] for value in tag_ids))
                current_record=self._record()
                source_explicit=self._source_link_touched
                series_id=(
                    (self._unlinked_series_id or None)
                    if source_explicit
                    else (current_record.series_id if current_record is not None else None)
                )
                return {
                    "kind":"outfit","record_id":self.record_id,
                    "name":self.name.text(),"series_id":series_id,
                    "character_ids":character_ids,"aliases":self.aliases.values(),
                    "sdxl_tags":self.sdxl_tags.values(),
                    "plain_language":self._description_values(),
                    "style_tags":self.style_tags.values(),"notes":self.notes.toPlainText(),
                    "source_tag_id":source_tag_ids[0] if source_tag_ids else "",
                    "source_tag_explicit":source_explicit,
                    "character_ids_explicit":self._associated_character_link_touched,
                }
        except Exception as error:
            self._last_error = str(error)
            self._set_status(f"Not saved: {error}", error=True)
            return None

    def _save_request_in_background(self,request):
        kind=request.pop("kind")
        record_id=request.pop("record_id")
        if kind=="series":
            return self.service.update_series(record_id,**request)
        if kind=="character":
            return self.service.update_character(record_id,**request)
        return self.service.update_outfit(record_id,**request)

    def _autosave(self) -> None:
        if not self._dirty or self._save_task is not None:
            return
        request=self._save_request()
        if request is None:
            return
        self._dirty = False
        self._source_link_touched=False
        self._version_link_touched=False
        self._outfit_link_touched=False
        self._associated_character_link_touched=False
        task=AsyncCallTask(lambda request=request:self._save_request_in_background(request))
        task.relationship_flags=(
            bool(request.get("source_tag_explicit",False)),
            bool(request.get("version_explicit",False)),
            request.get("outfit_tag_ids") is not None,
            bool(request.get("character_ids_explicit",False)),
        )
        self._save_task=task
        task.signals.complete.connect(self._autosave_complete)
        task.signals.failed.connect(self._autosave_failed)
        self._set_status("Saving typed Character Knowledge…")
        start_qt_delivery_task(self._save_pool,task)

    def _autosave_complete(self,_result) -> None:
        if self._save_task is None:
            return
        self._save_task=None
        self._last_error = ""
        self.saved = True
        record = self._record()
        noun = (
            "Character Source"
            if self.kind == "series"
            else "Character"
            if self.kind == "character"
            else "Outfit"
        )
        self.setWindowTitle(
            f"Edit {noun} — {record.name if record else self.name.text().strip()}"
        )
        self._set_status("Saved typed Character Knowledge automatically. No image data changed.")
        self.record_saved.emit(self.kind,self.record_id)
        if self._dirty:
            self._autosave()
        elif self._close_after_save:
            self._close_after_save=False
            self.close()

    def _autosave_failed(self,message) -> None:
        task=self._save_task
        if task is None:
            return
        self._save_task=None
        self._dirty=True
        (
            source_touched,version_touched,outfit_touched,characters_touched,
        )=getattr(task,"relationship_flags",(False,False,False,False))
        self._source_link_touched|=source_touched
        self._version_link_touched|=version_touched
        self._outfit_link_touched|=outfit_touched
        self._associated_character_link_touched|=characters_touched
        self._last_error=str(message)
        self._set_status(f"Not saved: {message}", error=True)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status.setText(text)
        if self.creating:
            self.status.hide()
            self.readiness_status.setText(text)
            self.readiness_status.setObjectName("errorLabel" if error else "mutedLabel")
            self.readiness_status.style().unpolish(self.readiness_status)
            self.readiness_status.style().polish(self.readiness_status)
            self.readiness_status.show()
            return
        self.readiness_status.hide()
        self.status.show()
        self.status.setObjectName("errorLabel" if error else "mutedLabel")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _release_reference_contexts(self) -> None:
        for name in ("source_tags", "outfits", "characters", "version_base"):
            editor = getattr(self, name, None)
            if editor is not None:
                editor.release_context()

    def flush_pending(self) -> bool:
        self._flush()
        return not self._dirty and self._save_task is None

    def dispose(self) -> None:
        self.timer.stop()
        self._release_reference_contexts()

    def done(self, result: int) -> None:
        self.timer.stop()
        self._release_reference_contexts()
        super().done(result)

    def reject(self) -> None:
        if self.creating:
            super().reject()
            return
        # Escape follows the same save-before-close boundary as the Close
        # button and window chrome.
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.creating:
            self._release_reference_contexts()
            event.accept()
            return
        self._flush()
        if self._save_task is not None:
            self._close_after_save=True
            self._set_status("Saving typed Character Knowledge before closing…")
            event.ignore()
            return
        if self._dirty:
            QMessageBox.warning(
                self, "Typed record was not saved",
                f"Fix the highlighted edit before closing. The saved record remains unchanged.\n\n{self._last_error}",
            )
            event.ignore()
            return
        self._release_reference_contexts()
        event.accept()
