from __future__ import annotations

from core.evidence_freshness import current_source_fingerprint

import copy
import json
from dataclasses import dataclass

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QKeySequence, QPalette, QPixmap, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHeaderView, QHBoxLayout, QInputDialog, QLabel, QLayout, QLineEdit, QMenu, QMessageBox, QProgressBar,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStyle, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    CategoryRegistry, CharacterAnnotation, CharacterDatabase, ImageAnnotation, ImageKnowledgeReconciliationService, ImageRecord, MetadataSyncService,PLAIN_LANGUAGE_MODELS,
    CHARACTER_OWNERSHIP_GROUP_LABELS,CharacterOwnershipError,
    CharacterOwnershipService,
    CharacterImageKnowledgeService,
    ConfirmedTagIdentityError,ConfirmedTagIdentityService,
    DeterministicFactExpansionPolicy,FindingDecisionTarget,
    ImageFindingDecisionService,
    ImageRecordEditSnapshot,
    ImageRecordStaleWriteError,
    CONTEXT_KIND_LABELS,CONTEXT_KINDS,ImageContextAnnotation,ImageContextError,
    ImageRecordRecoveryPlan,ImageRecordRecoveryService,
    PreparationOptions,RelatedTagSuggestionService, TagCollection, TagOrderCandidate, TagOrderingService, active_automation_engine,
    RATING_ORDER,rating_conflict_detail,
    add_related_tag_to_destination,apply_context_partition,context_placements,
    move_image_facts,
    WD_TAGGER_MODELS, audit_typed_boundary,description_review_classification,
    merge_plain_language_in_source_order, normalize_plain_language_exclusion,
    current_candidate_sources,current_evidence_keys_by_source,
    split_plain_language,
)
from core.generation_metadata import format_generation_record, format_raw_generation_payload
from core.automation_templates import has_automation_placeholder
from core.description_exclusions import add_description_exclusions
from core.paths import CATEGORIES_PATH,DEFAULTS_DIR,USER_DATA_DIR
from core.tag_collection import normalize_tag
from core.confirmed_tag_identity import (
    replace_retired_image_tag, image_tag_occurrences, active_refinement_inputs, retired_refinement_hold,
)
from core.tag_refinement_plan import plan_refinement
from core.relation_native_snapshot import capture_live_refinement
from core.related_tag_suggestions import RelatedTagSuggestion
import hashlib
from core.character_image_knowledge import (
    apply_character_source_autofill_state,character_source_autofill_state,
    apply_character_description_autofill_state,character_description_autofill_state,
    reconcile_character_description,
)
from .accessibility import announce_accessibly
from .components import ContextHelpButton,DisclosureButton,ElidingLabel,ExtendedSelectionTreeWidget,RangeCheckTreeWidget,bounded_window_size,compact_button,counted_noun,set_progress_bar
from .confidence import STATUS_COLORS
from .description_exclusions_dialog import DescriptionExclusionsDialog
from .gallery import ProtectedImageView,content_safety_policy
from .modal_feedback import FeedbackEnabledDialog
from .preparation_controls import PreparationControls
from .tag_editor import (
    FlowLayout,TagChipEditor,TagPalette,parse_tag_paste,prepare_tag_palette_index,
)
from .tag_identity_review import review_removed_identities,RetiredImageTagDialog,removed_identity_record
from .tag_relationship_editor import TagReferencePickerDialog


REQUEST_SOURCE_CORRECTION = 106
_RATING_DETAIL_UNSET = object()


class _TagWorkspaceSplitter(QSplitter):
    resized = Signal()

    def minimumSizeHint(self):
        size=super().minimumSizeHint()
        # Allow the host to shrink far enough to activate the stacked layout.
        size.setWidth(0)
        return size

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.resized.emit()


@dataclass(frozen=True)
class PreparedImageDataEditorContext:
    categories: object
    automation_engine: object
    related_tag_service: object
    tag_palette_index: object


def prepare_image_data_editor_context(
    knowledge,characters,
) -> PreparedImageDataEditorContext:
    """Prepare read-only, high-cardinality editor dependencies off the UI thread."""
    categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
    automation_engine=active_automation_engine(knowledge)
    semantic_relationships=getattr(
        knowledge,"_app_tag_semantic_service",None,
    )
    if getattr(semantic_relationships,"tags",None) is not knowledge:
        semantic_relationships=None
    related_tag_service=RelatedTagSuggestionService(
        knowledge,categories,characters,
        semantic_relationships=semantic_relationships,
    )
    return PreparedImageDataEditorContext(
        categories=categories,
        automation_engine=automation_engine,
        related_tag_service=related_tag_service,
        tag_palette_index=prepare_tag_palette_index(knowledge,categories),
    )


def reconcile_embedded_with_preview(parent, record: ImageRecord, metadata: MetadataSyncService, knowledge=None, characters=None, *, pending_editor_save=False) -> bool:
    """Show the exact JSON-to-metadata delta before a verified, backed-up write."""
    try:
        service = ImageKnowledgeReconciliationService(metadata,knowledge=knowledge,characters=characters)
        preview = service.preview(record.image_path, include_metadata=True, record=record)
    except Exception as exc:
        QMessageBox.warning(parent, "Metadata was not inspected", str(exc))
        return False
    difference = preview.difference("metadata")
    if preview.errors:
        QMessageBox.warning(parent, "Metadata cannot be updated", "\n".join(preview.errors))
        return False
    if difference is None or not difference.will_write:
        return True
    summary = [
        (
            "Continuing updates this image's embedded Metadata, then continues saving your pending image edits. "
            "Returning to the editor leaves those edits pending."
            if pending_editor_save else
            "This optional step updates embedded Metadata only. Skipping leaves the existing Image Knowledge and TXT unchanged."
        ),
        "Unrelated Metadata and exact generator payloads will be preserved and verified.",
    ]
    if preview.metadata_portability_replacements:
        count=preview.metadata_portability_replacements
        summary.append(
            f"Metadata-safe copy: replace {count} unsupported control "
            f"character{'s' if count != 1 else ''}; exact Image Knowledge remains unchanged."
        )
    details = []
    for label, values in (
        ("Add confirmed SDXL Tags", difference.added_tags),
        ("Remove old embedded SDXL Tags", difference.removed_tags),
        ("Add Plain Language Descriptions", difference.added_descriptions),
        ("Remove old Plain Language Descriptions", difference.removed_descriptions),
    ):
        if values:
            summary.append(f"{label}: {len(values)}")
            details.extend((f"{label} ({len(values)}):", *[f"• {value}" for value in values], ""))
    if difference.order_changed:
        summary.append("Reorder confirmed SDXL Tags: yes")
        details.extend(("Reorder confirmed SDXL Tags:","• Apply the current System Knowledge category and reviewed SDXL Tag priorities.",""))
    dialog = QMessageBox(parent)
    dialog.setWindowTitle("Review Metadata before saving" if pending_editor_save else "Update image Metadata")
    dialog.setIcon(QMessageBox.Question)
    dialog.setText("\n".join(summary))
    dialog.setDetailedText("\n".join(details).strip())
    dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    dialog.button(QMessageBox.Yes).setText("Continue Save" if pending_editor_save else "Update Metadata")
    dialog.button(QMessageBox.No).setText("Return to Editor" if pending_editor_save else "Skip Metadata")
    dialog.setDefaultButton(QMessageBox.Yes)
    if dialog.exec() != QMessageBox.Yes:
        return False
    result = service.apply(preview, targets=("metadata",), record=record)
    if result.errors:
        QMessageBox.warning(parent, "Metadata was not updated", "\n".join(result.errors))
        return False
    return True


class ImageRecordRecoveryDialog(QDialog):
    """Collect deliberate choices for one stale Image Knowledge save."""

    APPLY = QDialog.Accepted
    DISCARD_LOCAL = 2

    def __init__(self, plan: ImageRecordRecoveryPlan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.choice_boxes: dict[str, QComboBox] = {}
        self.setWindowTitle("Review newer image data")
        self.setModal(True)
        screen = self.screen() or QApplication.primaryScreen()
        self.resize(
            bounded_window_size(screen.availableGeometry(), QSize(980, 620))
            if screen is not None else QSize(980, 620)
        )
        layout = QVBoxLayout(self)
        title = QLabel("This image changed after the editor opened")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        summary = QLabel(
            "CollectiGen can combine changes that affect different image facts. "
            +(
                f"Choose which value to keep for {len(plan.conflicts):,} "
                f"overlapping change{'s' if len(plan.conflicts) != 1 else ''}. "
                if plan.conflicts else
                "The Image Knowledge changes do not overlap and can be combined safely. "
            )
            +"The newest Image Knowledge record is backed up and checked again "
            "before anything is saved."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if plan.caption_changed:
            caption = QLabel(
                "The Training TXT also changed. Recovery leaves that TXT unchanged; "
                "review or export it separately after saving Image Knowledge."
            )
            caption.setObjectName("warningLabel")
            caption.setWordWrap(True)
            caption.setAccessibleName("Training TXT changed and will remain unchanged")
            layout.addWidget(caption)
        unchanged = QLabel(
            "Recovery changes Image Knowledge only. Metadata and image pixels remain unchanged."
        )
        unchanged.setObjectName("mutedLabel")
        unchanged.setWordWrap(True)
        layout.addWidget(unchanged)

        if plan.conflicts:
            self.conflicts = QTreeWidget()
            self.conflicts.setHeaderLabels(("Information", "Current", "My Change", "Choice"))
            self.conflicts.setRootIsDecorated(False)
            self.conflicts.setAccessibleName(
                f"{len(plan.conflicts)} overlapping Image Knowledge changes"
            )
            header = self.conflicts.header()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.Stretch)
            header.setSectionResizeMode(2, QHeaderView.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
            for conflict in plan.conflicts:
                item = QTreeWidgetItem((
                    conflict.label,
                    conflict.current_text,
                    conflict.local_text,
                    "",
                ))
                item.setToolTip(0, " › ".join(conflict.path))
                item.setToolTip(1, conflict.current_text)
                item.setToolTip(2, conflict.local_text)
                self.conflicts.addTopLevelItem(item)
                choice = QComboBox()
                choice.addItem("Choose…", "")
                choice.addItem("Keep Current", "current")
                choice.addItem("Use My Change", "local")
                choice.setAccessibleName(f"Choice for {conflict.label}")
                choice.setAccessibleDescription(
                    f"Current: {conflict.current_text}. My Change: {conflict.local_text}."
                )
                choice.setToolTip(
                    "Keep the newest saved value or use the change staged in this editor."
                )
                choice.currentIndexChanged.connect(self._update_apply_state)
                self.conflicts.setItemWidget(item, 3, choice)
                self.choice_boxes[conflict.id] = choice
            layout.addWidget(self.conflicts, 1)
        else:
            self.conflicts = None
            layout.addStretch(1)

        actions = QHBoxLayout()
        self.keep_button = QPushButton("Keep Editing")
        self.keep_button.setToolTip("Close this review without changing saved data or staged edits.")
        self.keep_button.clicked.connect(self.reject)
        self.discard_button = QPushButton("Reload and Discard My Changes")
        self.discard_button.setToolTip(
            "Reload the newest saved Image Knowledge and discard every staged editor change."
        )
        self.discard_button.clicked.connect(lambda: self.done(self.DISCARD_LOCAL))
        actions.addWidget(self.keep_button)
        actions.addWidget(self.discard_button)
        actions.addStretch(1)
        self.use_local_all_button = None
        if plan.conflicts:
            self.use_local_all_button = QPushButton("Use My Changes for All…")
            self.use_local_all_button.setToolTip(
                "Choose My Change for every displayed conflict while preserving unrelated current changes."
            )
            self.use_local_all_button.clicked.connect(self._use_local_for_all)
            actions.addWidget(self.use_local_all_button)
        self.apply_button = QPushButton("Apply Changes")
        self.apply_button.setProperty("primary", True)
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.accept)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self._update_apply_state()

    def choices(self) -> dict[str, str]:
        return {
            conflict_id: str(box.currentData() or "")
            for conflict_id, box in self.choice_boxes.items()
        }

    def _update_apply_state(self, *_):
        self.apply_button.setEnabled(
            all(box.currentData() in {"current", "local"} for box in self.choice_boxes.values())
        )

    def _use_local_for_all(self):
        choice = QMessageBox.question(
            self,
            "Use my changes for every conflict?",
            f"Use the editor's value for all {len(self.plan.conflicts):,} displayed "
            "conflicts? Unrelated changes from the newest record will still be preserved.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if choice != QMessageBox.Yes:
            return
        for box in self.choice_boxes.values():
            box.setCurrentIndex(box.findData("local"))
        self.accept()


class ImageContextAssignmentDialog(QDialog):
    """Choose Main Scene, an existing context, or one new reviewed context."""

    NEW_CONTEXT = "__new_context__"

    def __init__(
        self,annotation,selected_values,knowledge,categories,*,current_context_id="",
        parent=None,
    ):
        super().__init__(parent)
        self.annotation=annotation
        self.knowledge=knowledge
        self.categories=categories
        self._owner_tag_id=""
        self._owner_tag_label=""
        selected=tuple(str(value) for value in selected_values)
        selected_keys=TagCollection(selected).normalized_set()
        main_keys=TagCollection(
            annotation.all_structured_tags(include_contexts=False)
        ).normalized_set()
        self._confirmed_owner_ids={
            stable_id
            for value,stable_id in annotation.tag_identities.items()
            if value in main_keys and value not in selected_keys and stable_id
        }
        self.setWindowTitle("Set Image Context")
        self.setModal(True)
        self.resize(620,450)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Move only the confirmed facts listed below to Main Scene or one "
            "secondary view. They stay searchable and are not copied."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        selected_label=QLabel(f"Selected confirmed facts ({len(selected):,})")
        layout.addWidget(selected_label)
        self.selected_facts=QPlainTextEdit()
        self.selected_facts.setReadOnly(True)
        self.selected_facts.setAccessibleName("Exact selected confirmed facts")
        self.selected_facts.setPlainText("\n".join(selected))
        self.selected_facts.setMaximumHeight(90)
        layout.addWidget(self.selected_facts)
        form=QFormLayout()
        self.destination=QComboBox()
        self.destination.setAccessibleName("Image context destination")
        self.destination.addItem("Main Scene","")
        for context in annotation.contexts:
            self.destination.addItem(context.display_label,context.id)
        self.destination.addItem("Create New Secondary Context…",self.NEW_CONTEXT)
        current_index=self.destination.findData(current_context_id)
        self.destination.setCurrentIndex(max(0,current_index))
        self.destination.currentIndexChanged.connect(self._destination_changed)
        form.addRow("Destination",self.destination)
        layout.addLayout(form)
        staged_note=QLabel(
            "This change stays staged until Save Changes. Undo Tag Change "
            "restores the previous placement before saving."
        )
        staged_note.setWordWrap(True); staged_note.setObjectName("mutedLabel")
        layout.addWidget(staged_note)

        self.new_context_group=QGroupBox("New Secondary Context")
        new_form=QFormLayout(self.new_context_group)
        self.kind=QComboBox()
        self.kind.setAccessibleName("Secondary context kind")
        for kind in CONTEXT_KINDS:
            self.kind.addItem(CONTEXT_KIND_LABELS[kind],kind)
        new_form.addRow("Relationship",self.kind)
        self.owner=QComboBox()
        self.owner.setAccessibleName("Secondary context owner")
        self.owner.addItem("No owning Tag or object",("", "", ""))
        self.owner.addItem("Confirmed Main Scene Tag…",("tag", "", ""))
        for item in annotation.objects:
            label=item.identity_tag or f"Object {item.slot}"
            self.owner.addItem(
                f"Object: {label}",("object",item.id,label),
            )
        self.owner.currentIndexChanged.connect(self._owner_changed)
        new_form.addRow("Owner",self.owner)
        self.owner_tag_button=compact_button(QPushButton("Choose Confirmed Tag…"))
        self.owner_tag_button.clicked.connect(self._choose_owner_tag)
        self.owner_tag_status=QLabel()
        self.owner_tag_status.setWordWrap(True); self.owner_tag_status.setObjectName("mutedLabel")
        owner_row=QHBoxLayout(); owner_row.addWidget(self.owner_tag_button); owner_row.addWidget(self.owner_tag_status,1)
        self.owner_tag_label=QLabel("Owning Tag")
        new_form.addRow(self.owner_tag_label,owner_row)
        layout.addWidget(self.new_context_group)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel|QDialogButtonBox.Ok)
        self.ok_button=buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Set Context"); self.ok_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addStretch(1)
        layout.addWidget(buttons)
        self._destination_changed()

    def _destination_changed(self,*_):
        self.new_context_group.setVisible(
            self.destination.currentData()==self.NEW_CONTEXT
        )
        self._owner_changed()

    def _owner_changed(self,*_):
        data=self.owner.currentData() or ("", "", "")
        choosing_tag=(
            self.destination.currentData()==self.NEW_CONTEXT and data[0]=="tag"
        )
        self.owner_tag_button.setVisible(choosing_tag)
        self.owner_tag_status.setVisible(choosing_tag)
        self.owner_tag_label.setVisible(choosing_tag)
        self.owner_tag_button.setEnabled(bool(self._confirmed_owner_ids))
        if choosing_tag and not self._confirmed_owner_ids:
            self.owner_tag_status.setText(
                "No other identity-complete Main Scene Tag is available."
            )
        elif choosing_tag:
            self.owner_tag_status.setText(
                self._owner_tag_label or "Choose the Tag that owns this view."
            )
        self.ok_button.setEnabled(not choosing_tag or bool(self._owner_tag_id))

    def _choose_owner_tag(self):
        if self.knowledge is None:
            return
        excluded=set(self.knowledge.tags)-self._confirmed_owner_ids
        dialog=TagReferencePickerDialog(
            self.knowledge,self.categories,title="Choose Context Owner Tag",
            exclude_ids=excluded,parent=self,
        )
        if dialog.exec()!=QDialog.Accepted:
            return
        record=self.knowledge.tags.get(dialog.selected_tag_id())
        if record is None:
            return
        self._owner_tag_id=record.id
        self._owner_tag_label=next((
            value for value in self.annotation.all_structured_tags(
                include_contexts=False
            )
            if self.annotation.tag_identities.get(normalize_tag(value))==record.id
        ),record.display)
        self._owner_changed()

    def assignment(self):
        destination=str(self.destination.currentData() or "")
        if destination!=self.NEW_CONTEXT:
            return {"context_id":destination}
        owner_kind,owner_id,owner_label=self.owner.currentData() or ("", "", "")
        if owner_kind=="tag":
            owner_id,owner_label=self._owner_tag_id,self._owner_tag_label
        return {
            "context_id":self.NEW_CONTEXT,
            "kind":str(self.kind.currentData() or CONTEXT_KINDS[0]),
            "owner_kind":str(owner_kind),
            "owner_id":str(owner_id),
            "owner_label":str(owner_label),
        }


class CharacterOwnershipDialog(QDialog):
    """Review one exact confirmed fact against confirmed Character identities."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view=view
        self.setWindowTitle("Review Character Ownership")
        self.setModal(True)
        self.resize(720,520)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose which confirmed Character Identity owns one exact confirmed "
            "fact. This optional context is needed only when the image-wide Tags "
            "do not distinguish subjects; reusable identity and outfit knowledge "
            "remains in System Knowledge."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        form=QFormLayout()
        self.fact=QComboBox()
        self.fact.setAccessibleName("Confirmed fact needing Character ownership")
        for item in view.facts:
            current=(
                f" — {len(item.owner_keys):,} owner"
                f"{'s' if len(item.owner_keys)!=1 else ''}"
                if item.owner_keys else " — Ownership not specified"
            )
            self.fact.addItem(
                f"{item.display} ({item.role_label}){current}",item.tag_id,
            )
        form.addRow("Confirmed fact",self.fact)
        self.group=QComboBox()
        self.group.setAccessibleName("Character fact group")
        self.group.addItem("Choose group…","")
        for key,label in CHARACTER_OWNERSHIP_GROUP_LABELS.items():
            self.group.addItem(label,key)
        self.group_label=QLabel("Fact group")
        form.addRow(self.group_label,self.group)
        layout.addLayout(form)
        owner_group=QGroupBox("Owning Character Identities")
        owner_layout=QVBoxLayout(owner_group)
        owner_help=QLabel(
            "Check every confirmed identity that owns this fact. Leave every "
            "identity unchecked to return an ordinary fact to image-wide scope."
        )
        owner_help.setWordWrap(True); owner_help.setObjectName("mutedLabel")
        owner_layout.addWidget(owner_help)
        self.owners=QTreeWidget()
        self.owners.setHeaderLabels(("Character Identity","Context"))
        self.owners.setRootIsDecorated(False)
        self.owners.setAccessibleName("Confirmed Character owners")
        owner_header=self.owners.header()
        owner_header.setSectionResizeMode(0,QHeaderView.Stretch)
        owner_header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        for choice in view.owners:
            row=QTreeWidgetItem((
                choice.label,
                "Retained context" if choice.existing else "Create only if selected",
            ))
            row.setData(0,Qt.UserRole,choice.key)
            row.setFlags(row.flags()|Qt.ItemIsUserCheckable)
            row.setCheckState(0,Qt.Unchecked)
            self.owners.addTopLevelItem(row)
        owner_layout.addWidget(self.owners,1)
        layout.addWidget(owner_group,1)
        self.status=QLabel()
        self.status.setWordWrap(True); self.status.setObjectName("mutedLabel")
        self.status.setAccessibleName("Character ownership review status")
        layout.addWidget(self.status)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel|QDialogButtonBox.Ok)
        self.apply_button=buttons.button(QDialogButtonBox.Ok)
        self.apply_button.setText("Stage Ownership")
        self.apply_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fact.currentIndexChanged.connect(self._fact_changed)
        self.group.currentIndexChanged.connect(self._update_apply_state)
        self.owners.itemChanged.connect(self._update_apply_state)
        self._fact_changed()

    def _current_fact(self):
        tag_id=str(self.fact.currentData() or "")
        return next(
            (item for item in self.view.facts if item.tag_id==tag_id),None,
        )

    def _selected_owner_keys(self):
        return tuple(
            str(row.data(0,Qt.UserRole) or "")
            for index in range(self.owners.topLevelItemCount())
            if (row:=self.owners.topLevelItem(index)).checkState(0)==Qt.Checked
        )

    def _fact_changed(self,*_):
        fact=self._current_fact()
        selected=set(fact.owner_keys if fact is not None else ())
        blocked=self.owners.blockSignals(True)
        try:
            for index in range(self.owners.topLevelItemCount()):
                row=self.owners.topLevelItem(index)
                row.setCheckState(
                    0,Qt.Checked
                    if str(row.data(0,Qt.UserRole) or "") in selected
                    else Qt.Unchecked,
                )
        finally:
            self.owners.blockSignals(blocked)
        outfit=bool(fact is not None and fact.role=="character_outfit")
        self.group_label.setVisible(not outfit)
        self.group.setVisible(not outfit)
        current_groups=set(fact.owner_groups if fact is not None else ())
        group=next(iter(current_groups)) if len(current_groups)==1 else ""
        self.group.setCurrentIndex(max(0,self.group.findData(group)))
        if fact is None:
            self.status.setText("No confirmed fact is available for owner review.")
        elif outfit:
            self.status.setText(
                "An outfit keeps its confirmed Tag and reusable System Knowledge "
                "record. This stages only which confirmed character wears it here."
            )
        else:
            self.status.setText(
                "The selected Tag remains confirmed and searchable. Choosing an "
                "owner moves only its image-specific placement into that Character "
                "fact group."
            )
        if fact is not None and fact.preserved_owner_count:
            self.status.setText(
                self.status.text()+f" {fact.preserved_owner_count:,} unrelated "
                "retained context(s) are not changed by this review."
            )
        self._update_apply_state()

    def _update_apply_state(self,*_):
        fact=self._current_fact()
        selected=self._selected_owner_keys()
        group=str(self.group.currentData() or "")
        changed=bool(fact is not None and (
            set(selected)!=set(fact.owner_keys)
            or (
                fact.role!="character_outfit" and selected
                and set(fact.owner_groups)!={group}
            )
        ))
        complete=bool(
            fact is not None
            and (fact.role=="character_outfit" or not selected or group)
        )
        self.apply_button.setEnabled(changed and complete)

    def selection(self):
        fact=self._current_fact()
        return {
            "fact_tag_id":fact.tag_id if fact is not None else "",
            "owner_keys":self._selected_owner_keys(),
            "group":str(self.group.currentData() or ""),
        }


class ImageDataEditor(FeedbackEnabledDialog):
    """Workflow-neutral editor used anywhere an image's organized data is edited."""

    feedback_workflow = "Image data editor"

    ai_requested = Signal(str)
    preparation_requested = Signal(object)
    edit_tag_requested = Signal(str,object)
    create_tag_requested = Signal(str,object)

    REQUEST_AI_TAGS = 101
    REQUEST_AI_DESCRIPTION = 102
    REQUEST_PREVIOUS = 103
    REQUEST_SKIP_NEXT = 104
    SAVE_AND_NEXT = 105
    REQUEST_SOURCE_CORRECTION = REQUEST_SOURCE_CORRECTION

    def __init__(self, record: ImageRecord, report=None, settings=None, parent=None, *, allow_ai: bool = True,
                 knowledge=None, characters=None, queue_index: int = 0, queue_count: int = 1,
                 preview_image: QImage | None = None, batch_conflict_context=None,
                 image_knowledge_only: bool = False, initial_tab_index: int = 0,
                 content_safety=None,source_correction_available: bool=False,
                 prepared_context: PreparedImageDataEditorContext | None=None,
                 preparation_complete: bool=False):
        super().__init__(parent)
        self._history_context=getattr(record,"history_context",None)
        self._history_image=record.image_path.absolute()
        self._recovery_service=(
            ImageRecordRecoveryService(
                backup_root=self._history_context.store.scope.history_root / "recovery",
                history_context=self._history_context,
            ) if self._history_context is not None else ImageRecordRecoveryService()
        )
        self._startup_recovery_error=""
        self._startup_recovered=False
        self._history_completion_pending=False
        try:
            self._startup_recovered=bool(
                self._recovery_service.recover_pending(record.image_path)
            )
            if self._startup_recovered:
                record.load(force=True)
        except Exception as exc:
            self._startup_recovery_error=str(exc)
        self.record = record
        self._edit_snapshot=ImageRecordEditSnapshot.capture(record)
        self.persisted_changes=False
        self.source_correction_scope={}
        self.report = report
        self.settings = settings
        self.knowledge = knowledge
        self.allow_ai=bool(allow_ai)
        self.source_correction_available=bool(source_correction_available)
        self.preview_image=preview_image
        self.content_safety=(
            content_safety
            if content_safety is not None else
            content_safety_policy()
        )
        self.batch_conflict_context=dict(batch_conflict_context or {})
        self.image_knowledge_only=bool(image_knowledge_only)
        self.characters = characters or CharacterDatabase(USER_DATA_DIR)
        self.categories = (
            prepared_context.categories if prepared_context is not None else
            CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
        )
        self._related_tag_service=(
            prepared_context.related_tag_service
            if prepared_context is not None else None
        )
        self._tag_palette_index=(
            prepared_context.tag_palette_index
            if prepared_context is not None else None
        )
        self._saved_content_safety_result=self._content_safety_result()
        self._working_characters=copy.deepcopy(record.annotation.characters)
        self._original_characters=[
            item.to_dict() for item in record.annotation.characters
        ]
        self._character_ownership_changes={}
        self._working_objects=copy.deepcopy(record.annotation.objects)
        self._original_objects=[
            item.to_dict() for item in record.annotation.objects
        ]
        self._working_contexts=copy.deepcopy(record.annotation.contexts)
        self._original_contexts=[
            item.to_dict() for item in record.annotation.contexts
        ]
        non_object_annotation=copy.deepcopy(record.annotation)
        non_object_annotation.objects=[]
        self._non_object_original_keys={
            normalize_tag(value)
            for value in non_object_annotation.all_structured_tags()
        }
        self._object_owned_keys={
            normalize_tag(value)
            for item in self._working_objects
            for value in item.all_tags()
        }
        self.current_tags = TagCollection(record.annotation.all_structured_tags())
        self.original_tags = self.current_tags.to_list()
        self._working_ownership_acknowledgements=copy.deepcopy(record.annotation.source.get("ownership_acknowledgements",[])) if isinstance(record.annotation.source.get("ownership_acknowledgements",[]),list) else []
        self._working_tag_identities=dict(record.annotation.tag_identities)
        self._pending_retired_tag_replacements={}
        self._source_autofill_state=character_source_autofill_state(record.annotation)
        self._source_autofill_seen_present=set()
        self._source_autofill_added=set()
        self._source_autofill_ready=False
        self._description_autofill_state=character_description_autofill_state(record.annotation)
        self._applying_description_autofill=False
        self.pending_ignored_tags={}
        self.pending_ignored_descriptions={}
        self.pending_restored_tags=set()
        self.pending_restored_absent_tags=set()
        self.pending_restored_descriptions=set()
        self.pending_dismissed_tags={}
        self.pending_dismissed_descriptions={}
        self.pending_accepted_descriptions=set()
        self.pending_used_as_edited_targets=[]
        self.pending_restored_dismissed_tags=set()
        self.pending_restored_dismissed_descriptions=set()
        self._pending_scoped_tag_additions={}
        self._scoped_ingress_restore_states={}
        self._tag_history=[]
        self._prepared_absent_restores=set()
        self._approved_conflict_states=set()
        self._prepared_conflict_replacements=()
        self._diagnostic_conflict_context={}
        self._pending_tag_selection=set()
        self._clear_legacy_character_assignments=False
        self._restoring_pending_tag_selection=False
        self._confirmed_conflict_count=0
        self._related_suggestion_count=0
        favorite_tags=getattr(settings,"favorite_sdxl_tags",[]) if settings is not None else []
        recent_tags=getattr(settings,"recent_sdxl_tags",[]) if settings is not None else []
        self._favorite_tags=list(favorite_tags) if isinstance(favorite_tags,(list,tuple)) else []
        self._recent_tags=list(recent_tags) if isinstance(recent_tags,(list,tuple)) else []
        self.original_prose = split_plain_language("\n".join(record.annotation.plain_language))
        self.queue_index = max(0, int(queue_index))
        self.queue_count = max(1, int(queue_count))
        self.automation_engine=(
            prepared_context.automation_engine
            if prepared_context is not None else
            active_automation_engine(knowledge)
        )
        self._identity_service=ConfirmedTagIdentityService(
            knowledge,
            evidence_rejected=self.automation_engine.is_evidence_rejected,
        )
        self._ignored_suggestions=0
        self._automation_rejected_suggestions=set()
        self._absent_suggestions=0
        self._suggestion_status_detail=""
        self.setWindowTitle(f"Image data editor — {record.image_path.name}")
        screen=self.screen() or QApplication.primaryScreen()
        self.resize(
            bounded_window_size(screen.availableGeometry(),QSize(1180,760))
            if screen is not None else QSize(1180,760)
        )

        layout = QVBoxLayout(self)
        self.editor_header=QWidget()
        heading=QHBoxLayout(self.editor_header)
        heading.setContentsMargins(0,0,0,0)
        heading.setSpacing(6)
        heading_title=QLabel("Edit Image Data"); heading_title.setObjectName("pageTitle")
        self.help_button=ContextHelpButton(
            "Image Data Editor",
            """## What you can change

Use **Tags** for concise SDXL concepts and **Descriptions** for natural-language facts. Review findings before adding them to the confirmed image record.

## Saving

Changes remain in this editor until you choose **Save Changes** or **Save and Next**. Raw source evidence is kept even when you reject or ignore a suggestion.

## Useful shortcuts

- **Ctrl+S:** save
- **Ctrl+Enter:** save and continue
- **Alt+Left / Alt+Right:** move through a review queue""",
            self,
        )
        from .ai_runtime_controls import TaskMemoryHoldPanel
        self.memory_hold=TaskMemoryHoldPanel(self)
        layout.addWidget(self.memory_hold)
        self.prepare_action_bar=QWidget()
        prepare_actions=QHBoxLayout(self.prepare_action_bar)
        prepare_actions.setContentsMargins(0,0,0,0)
        self.preparation_controls=None
        self.generate_ai_button=None
        if self.allow_ai and self.settings is not None:
            preparation_complete=bool(preparation_complete)
            self.preparation_controls=PreparationControls(
                self.settings,self.prepare_action_bar,inline_options=False,
            )
            self.preparation_controls.set_scope(
                1,button_text=(
                    "Re-run Preparation…"
                    if preparation_complete else "Prepare Image…"
                ),
                description=(
                    f"this complete image: {record.image_path.name}"
                    if preparation_complete else
                    f"this image: {record.image_path.name}"
                ),
            )
            self.preparation_controls.set_context(
                refresh=preparation_complete,secondary=preparation_complete,
            )
            self.preparation_controls.undo_button.hide()
            self.preparation_controls.summary.hide()
            self.preparation_controls.requested.connect(
                lambda:self._request_preparation(self.preparation_controls.take_submitted_options())
            )
            self.generate_ai_button=self.preparation_controls.action_button
            preparation_description=(
                "This image is complete. Choose which source evidence or AI "
                "results to refresh; current Image Knowledge remains until the "
                "reviewed preparation result is applied."
                if preparation_complete else
                "Prepare this image from its current source evidence and the "
                "enabled local AI options."
            )
            self.generate_ai_button.setToolTip(preparation_description)
            self.generate_ai_button.setAccessibleName(
                "Re-run preparation for this complete image"
                if preparation_complete else "Prepare this image"
            )
            self.generate_ai_button.setAccessibleDescription(
                preparation_description
            )
            prepare_actions.addWidget(self.preparation_controls)
        self.prepare_action_bar.setVisible(self.preparation_controls is not None)
        heading.addWidget(heading_title)
        heading.addStretch(1)
        self.clear_character_identity_button=compact_button(QPushButton("Clear Character Identity"))
        self.clear_character_identity_button.setToolTip(
            "For this image, clear Character Identity and Character Source Tags, "
            "named-character associations, and matching description wording. "
            "Keep appearance and outfit facts. Changes support Undo and wait for Save Changes; "
            "raw evidence stays available. Removed Tags are ignored for this image."
        )
        self.clear_character_identity_button.clicked.connect(self._clear_character_identity)
        self.clear_character_identity_button.hide()
        heading.addWidget(self.prepare_action_bar)
        heading.addWidget(self.help_button)
        layout.addWidget(self.editor_header)
        self.identity_clear_status=QLabel()
        self.identity_clear_status.setWordWrap(True)
        self.identity_clear_status.setObjectName("mutedLabel")
        self.identity_clear_status.hide()
        layout.addWidget(self.identity_clear_status)
        self.queue_status = QLabel()
        self.queue_status.setObjectName("mutedLabel")
        self.queue_status.setText(
            f"Reviewing image {self.queue_index + 1:,} of {self.queue_count:,}  •  "
            "Ctrl+Enter: save and next  •  Alt+Left/Right: previous or skip"
            if self.queue_count > 1 else "Reviewing one image"
        )
        self.queue_status.setVisible(self.queue_count > 1)
        layout.addWidget(self.queue_status)

        body = QSplitter()
        self.body_splitter=body
        body.addWidget(self._build_preview())
        body.addWidget(self._build_editor(allow_ai))
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 3)
        body.setSizes((330, 850))
        layout.addWidget(body, 1)
        self.tabs.setCurrentIndex(max(0,min(int(initial_tab_index),self.tabs.count()-1)))

        self.save_status=QLabel()
        self.save_status.setObjectName("errorLabel")
        self.save_status.setTextFormat(Qt.PlainText)
        self.save_status.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        self.save_status.setWordWrap(True)
        self.save_status.hide()
        self.save_failure_details_button=compact_button(QPushButton("Save Details…"))
        self.save_failure_details_button.clicked.connect(self._show_save_failure_details)
        self.save_failure_details_button.hide()
        status_row=QHBoxLayout()
        status_row.addWidget(self.save_status,1)
        status_row.addWidget(self.save_failure_details_button)
        layout.addLayout(status_row)

        buttons = QHBoxLayout(); cancel_button=QPushButton("Cancel"); cancel_button.clicked.connect(self.reject)
        self.pending_status=QLabel("Unsaved changes"); self.pending_status.setObjectName("mutedLabel"); self.pending_status.hide()
        self.save_button=QPushButton("Save Changes"); self.save_button.setProperty("primary",self.queue_count==1); self.save_button.clicked.connect(self.save)
        self.previous_button = QPushButton("Previous")
        self.skip_button = QPushButton("Skip")
        self.save_next_button = QPushButton("Save and Next")
        self.save_next_button.setProperty("primary", True)
        self.previous_button.setEnabled(self.queue_count > 1 and self.queue_index > 0)
        self.skip_button.setEnabled(self.queue_count > 1 and self.queue_index + 1 < self.queue_count)
        self.save_next_button.setEnabled(self.queue_count > 1 and self.queue_index + 1 < self.queue_count)
        self.previous_button.setVisible(self.queue_count > 1)
        self.skip_button.setVisible(self.queue_count > 1)
        self.save_next_button.setVisible(self.queue_count > 1)
        self.previous_button.clicked.connect(lambda: self._navigate(self.REQUEST_PREVIOUS))
        self.skip_button.clicked.connect(lambda: self._navigate(self.REQUEST_SKIP_NEXT))
        self.save_next_button.clicked.connect(self.save_and_next)
        buttons.addWidget(cancel_button); buttons.addWidget(self.undo_tag_button)
        buttons.addStretch(1); buttons.addWidget(self.pending_status); buttons.addWidget(self.previous_button)
        buttons.addWidget(self.skip_button); buttons.addWidget(self.save_button); buttons.addWidget(self.save_next_button)
        layout.addLayout(buttons)

        from .components import bind_status_footer
        self.preparation_phase=QLabel(); self.preparation_phase.hide()
        bind_status_footer(self,self.preparation_phase,self.ai_progress,title="Image preparation")
        bind_status_footer(self,self.save_status,self.save_failure_details_button)
        bind_status_footer(self,self.queue_status,title="Review queue")
        bind_status_footer(self,self.identity_clear_status)

        self.prose.textChanged.connect(self._update_pending_status)
        self.prose.textChanged.connect(self._refresh_confirmed_context)
        self.prose.textChanged.connect(self._description_autofill_edited)
        self._update_pending_status()

        self._shortcut_save = QShortcut(QKeySequence.Save, self)
        self._shortcut_save.activated.connect(self.save)
        self._shortcut_save_next = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_save_next.activated.connect(self.save_and_next)
        self._shortcut_previous = QShortcut(QKeySequence("Alt+Left"), self)
        self._shortcut_previous.activated.connect(lambda: self._navigate(self.REQUEST_PREVIOUS))
        self._shortcut_skip = QShortcut(QKeySequence("Alt+Right"), self)
        self._shortcut_skip.activated.connect(lambda: self._navigate(self.REQUEST_SKIP_NEXT))
        if self._startup_recovery_error:
            self.save_status.setText(
                "An interrupted Image Knowledge recovery needs attention before this image can be saved."
            )
            self.save_status.setToolTip(self._startup_recovery_error)
            self.save_status.show()
            self.save_button.setEnabled(False)
            self.save_next_button.setEnabled(False)
        elif self._startup_recovered:
            self.save_status.setObjectName("mutedLabel")
            self.save_status.setText(
                "Verified interrupted Image Knowledge recovery before opening this editor."
            )
            self.save_status.setToolTip(
                "The last incomplete recovery was either verified complete or restored exactly before this editor loaded."
            )
            self.save_status.show()
        if self.queue_count>1:
            QTimer.singleShot(0,lambda:self.tabs.setFocus(Qt.OtherFocusReason))
        self._source_autofill_ready=not bool(self._startup_recovery_error)
        self._refresh_tag_lists(refresh_related=False,refresh_pending=False,refresh_character=False)
        # Related suggestions immediately complete this same pending surface.
        self._populate(refresh_pending=False)
        self._refresh_related_tags()

    def _build_preview(self):
        panel = QWidget()
        panel.setMinimumWidth(260)
        panel.setMaximumWidth(620)
        panel_layout = QVBoxLayout(panel)
        self.preview_title = ElidingLabel(self.record.image_path.name)
        self.preview_title.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.preview_title.setToolTip(self.record.image_path.name)
        self.preview_title.setAccessibleName(
            f"Selected image: {self.record.image_path.name}"
        )
        panel_layout.addWidget(self.preview_title)
        self.protected_preview=ProtectedImageView(
            policy=self.content_safety,settings=self.settings
        )
        self.preview=self.protected_preview.viewer
        self.protected_preview.setMinimumSize(240,320)
        pixmap=(QPixmap.fromImage(self.preview_image)
                if self.preview_image is not None else QPixmap(str(self.record.image_path)))
        self.protected_preview.set_image(
            pixmap,self.record.image_path,record=self.record
        )
        panel_layout.addWidget(self.protected_preview, 1)
        return panel

    def prepare_for_privacy_capture(self) -> None:
        self.protected_preview.prepare_for_privacy_capture()

    def _build_editor(self, allow_ai):
        panel = QWidget()
        panel.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Ignored)
        panel_layout = QVBoxLayout(panel)
        self.boundary_review_panel=QGroupBox("Confirmed Values Need Review")
        boundary_layout=QVBoxLayout(self.boundary_review_panel)
        boundary_actions=QHBoxLayout()
        self.boundary_review_values=QComboBox()
        self.boundary_review_values.setMinimumWidth(0)
        self.boundary_review_values.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.boundary_review_values.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.boundary_review_values.setMinimumContentsLength(12)
        self.boundary_review_values.setAccessibleName("Confirmed values needing review")
        self.boundary_review_values.currentIndexChanged.connect(self._show_boundary_review_detail)
        boundary_actions.addWidget(self.boundary_review_values,1)
        self.boundary_review_button=compact_button(QPushButton("Review Value"))
        self.boundary_review_button.clicked.connect(self._review_boundary_value)
        boundary_actions.addWidget(self.boundary_review_button)
        boundary_layout.addLayout(boundary_actions)
        self.boundary_review_detail=QLabel()
        self.boundary_review_detail.setTextFormat(Qt.PlainText)
        self.boundary_review_detail.setWordWrap(True)
        boundary_layout.addWidget(self.boundary_review_detail)
        self.boundary_review_panel.hide()
        panel_layout.addWidget(self.boundary_review_panel)
        tabs = QTabWidget()
        tabs.setMinimumSize(0,0)
        tabs.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Ignored)
        self.tabs = tabs
        self._attention_phase=False
        self._attention_timer=QTimer(self)
        self._attention_timer.setInterval(900)
        self._attention_timer.timeout.connect(self._pulse_review_attention)
        panel_layout.addWidget(tabs, 1)

        image_data = QWidget(); tag_layout = QVBoxLayout(image_data)
        self.tag_conflict_filter=QComboBox()
        self.tag_conflict_filter.setAccessibleName("Conflict filter for all displayed Tags")
        self.tag_conflict_filter.setProperty("sharedTagFilter",True)
        for label,value in (("All Tags","all"),("No Review Flags","clear"),("Conflicts / Ownership","conflicts")):
            self.tag_conflict_filter.addItem(label,value)
        confirmed_group=QGroupBox("Confirmed SDXL Tags on This Image")
        confirmed_layout=QVBoxLayout(confirmed_group)
        self.rating_review_panel=QGroupBox("Rating Needs Review")
        rating_review_layout=QVBoxLayout(self.rating_review_panel)
        rating_review_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.rating_review_panel.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.Maximum,
        )
        self.rating_review_panel.setMaximumHeight(150)
        self.rating_review_summary=QLabel()
        self.rating_review_summary.setObjectName("warningLabel")
        self.rating_review_summary.setWordWrap(True)
        self.rating_review_summary.setAccessibleName("Rating conflict summary")
        rating_review_layout.addWidget(self.rating_review_summary)
        rating_actions=QHBoxLayout()
        self.use_required_rating_button=compact_button(
            QPushButton("Use Stronger Rating"),primary=True,
        )
        self.use_required_rating_button.setToolTip(
            "Replace the confirmed rating Tag with the minimum rating required "
            "by the reviewed Tag rules. This remains staged until Save Changes."
        )
        self.use_required_rating_button.clicked.connect(
            self._stage_required_rating
        )
        self.show_rating_tags_button=compact_button(
            QPushButton("Review Tags")
        )
        self.show_rating_tags_button.setToolTip(
            "Show and select the confirmed Tags whose reviewed sensitivity "
            "rules require a stronger rating."
        )
        self.show_rating_tags_button.clicked.connect(
            self._show_rating_conflict_tags
        )
        rating_actions.addWidget(self.use_required_rating_button)
        rating_actions.addWidget(self.show_rating_tags_button)
        rating_actions.addStretch(1)
        rating_review_layout.addLayout(rating_actions)
        self.rating_review_panel.hide()
        confirmed_layout.addWidget(self._build_current_panel())
        self.confirmed_tags_group=confirmed_group
        self.confirmed_tags_scroll=QScrollArea()
        self.confirmed_tags_scroll.setObjectName("confirmedTagsWorkspaceScroll")
        self.confirmed_tags_scroll.setAccessibleName(
            "Scrollable confirmed SDXL Tags workspace"
        )
        self.confirmed_tags_scroll.setFrameShape(QScrollArea.NoFrame)
        self.confirmed_tags_scroll.setWidgetResizable(True)
        self.confirmed_tags_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self.confirmed_tags_scroll.setWidget(confirmed_group)
        self.confirmed_tags_scroll.setMinimumSize(0,190)
        self.confirmed_tags_scroll.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.Ignored
        )
        self.tag_activity_tabs=QTabWidget()
        self.tag_activity_tabs.setAccessibleName("Image Knowledge secondary workspaces")
        self.tag_activity_tabs.setMinimumSize(0,170)
        self.tag_activity_tabs.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.Ignored
        )
        self.tag_root_category=QComboBox()
        self.tag_root_category.setAccessibleName("Root category for all displayed Tags")
        self.tag_root_category.setToolTip(
            "Filter confirmed Tags, pending findings, related suggestions, and Add Tags "
            "by this category and its descendants. Staged edits stay unchanged."
        )
        self.tag_root_category.setMaxVisibleItems(16)
        self._refresh_tag_root_categories()
        category_row=QHBoxLayout()
        category_label=QLabel("Category")
        category_label.setBuddy(self.tag_root_category)
        category_row.addWidget(category_label)
        category_row.addWidget(self.tag_root_category)
        tag_layout.addLayout(category_row)
        self.related_tag_suggestions=()
        self.related_tag_editors=[]
        self.available_tags_group=self._build_available_panel()
        self.tag_palette.search.setPlaceholderText("Search all displayed Tags and aliases…")
        self.tag_palette.search.setAccessibleName("Search both Tag panes")
        self.tag_palette.search.setAccessibleDescription("Search stays active across Review Pending and Add Tags, within the shared category and filters.")
        category_row.insertWidget(0,self.tag_palette.search,1)
        category_row.addWidget(self.tag_palette.filter_toggle)
        self.reset_tag_filters_button=compact_button(QPushButton("Reset Filters"))
        self.reset_tag_filters_button.setToolTip("Show all Tags in both panes without changing staged edits.")
        self.reset_tag_filters_button.clicked.connect(lambda:self._reset_shared_tag_filters())
        category_row.addWidget(self.reset_tag_filters_button)
        self.tag_palette.filter_toggle.setAccessibleName("Show filters for both Tag panes")
        self.tag_palette.filter_summary.hide()
        tag_layout.addWidget(self.tag_palette.filters_panel)
        self.tag_palette.filters_changed.connect(self._tag_root_category_changed)
        self.tag_activity_tabs.addTab(
            self.available_tags_group,"Add Tags"
        )
        self.tag_activity_tabs.setTabToolTip(
            self.tag_activity_tabs.indexOf(self.available_tags_group),
            "Browse reviewed System Knowledge tags and stage additions on this image.",
        )
        self.tag_workspace_splitter=_TagWorkspaceSplitter(Qt.Vertical)
        self.tag_workspace_splitter.setObjectName("tagManagerWorkspaceSplitter")
        self.tag_workspace_splitter.setAccessibleName(
            "Resize confirmed and secondary Image Knowledge workspaces"
        )
        self.tag_workspace_splitter.setChildrenCollapsible(False)
        self.tag_workspace_splitter.setHandleWidth(8)
        self.tag_workspace_splitter.addWidget(self.confirmed_tags_scroll)
        self.tag_workspace_splitter.addWidget(self.tag_activity_tabs)
        self.tag_workspace_splitter.setStretchFactor(0,2)
        self.tag_workspace_splitter.setStretchFactor(1,3)
        # Keep the compact confirmed-action rows visible at the supported
        # editor height while retaining a usable pending workspace below.
        self.tag_workspace_splitter.setSizes((360,620))
        self._tag_workspace_sizes={}
        self._tag_workspace_layout_timer=QTimer(self)
        self._tag_workspace_layout_timer.setSingleShot(True)
        self._tag_workspace_layout_timer.timeout.connect(
            self._update_tag_workspace_layout
        )
        self.tag_workspace_splitter.resized.connect(
            lambda:self._tag_workspace_layout_timer.start(0)
        )
        self.tag_workspace_splitter.splitterMoved.connect(
            self._remember_tag_workspace_sizes
        )
        tag_layout.addWidget(self.rating_review_panel)
        tag_layout.addWidget(self.tag_workspace_splitter,1)
        self.image_data_tab=image_data

        descriptions=QWidget(); descriptions_layout=QVBoxLayout(descriptions)
        descriptions_intro=QLabel(
            "Review pending Plain Language findings and edit confirmed "
            "descriptions for this image. Changes remain staged until Save "
            "Changes."
        )
        descriptions_intro.setWordWrap(True); descriptions_intro.setObjectName("mutedLabel"); descriptions_layout.addWidget(descriptions_intro)
        confirmed_descriptions_group=QGroupBox(
            "Confirmed Plain Language Descriptions"
        )
        confirmed_descriptions_layout=QVBoxLayout(
            confirmed_descriptions_group
        )
        self.automatic_character_description=QCheckBox(
            "Include confirmed character identity automatically"
        )
        self.automatic_character_description.setChecked(bool(
            self._description_autofill_state and self._description_autofill_state["enabled"]
        ))
        self.automatic_character_description.setEnabled(self._description_autofill_state is not None)
        self.automatic_character_description.setToolTip(
            "For one confirmed known character, add its name and confirmed source "
            "when missing. Existing descriptions and raw AI output are preserved. "
            "Editing or undoing the added sentence turns this off for this image; Save keeps your choice."
        )
        self.automatic_character_description.toggled.connect(self._toggle_automatic_character_description)
        confirmed_descriptions_layout.addWidget(self.automatic_character_description)
        self.character_description_status=QLabel()
        self.character_description_status.setWordWrap(True)
        self.character_description_status.setObjectName("mutedLabel")
        self.character_description_status.hide()
        confirmed_descriptions_layout.addWidget(self.character_description_status)
        self.prose = QTextEdit()
        self.prose.setPlaceholderText("One or more Plain Language Descriptions")
        self.prose.setPlainText("\n".join(self.record.annotation.plain_language))
        confirmed_descriptions_layout.addWidget(self.prose,1)
        self.description_edit_actions_button=compact_button(QPushButton("Description Actions"))
        self.description_edit_actions_button.setToolTip(
            "Edit confirmed Descriptions only. Changes stay in this editor until Save Changes."
        )
        description_edit_menu=QMenu(self.description_edit_actions_button)
        self.undo_description_action=description_edit_menu.addAction("Undo Description Edit")
        self.undo_description_action.triggered.connect(self.prose.undo)
        self.clear_descriptions_action=description_edit_menu.addAction("Clear Descriptions…")
        self.clear_descriptions_action.triggered.connect(self._clear_confirmed_descriptions)
        self.description_edit_actions_button.setMenu(description_edit_menu)
        description_edit_actions=QHBoxLayout()
        description_edit_actions.addStretch(1)
        description_edit_actions.addWidget(self.description_edit_actions_button)
        confirmed_descriptions_layout.addLayout(description_edit_actions)
        self.prose.undoAvailable.connect(self._refresh_confirmed_context)
        descriptions_layout.addWidget(confirmed_descriptions_group,1)
        self.description_tab=descriptions

        character=QWidget(); character_layout=QVBoxLayout(character)
        character_intro=QLabel(
            "Confirmed character Tags identify the characters shown below "
            "automatically. Known source Tags can be added automatically too. "
            "You can remove an incorrect addition or turn this off for the image. "
            "Other suggested Tags remain available to review."
        )
        character_intro.setWordWrap(True); character_intro.setObjectName("mutedLabel"); character_layout.addWidget(character_intro)
        character_layout.addWidget(self._build_character_information()); character_layout.addStretch(1); self.character_tab=character
        self.tabs.currentChanged.connect(self._refresh_identity_clear_visibility)
        self.prose.textChanged.connect(self._refresh_identity_clear_visibility)

        self.suggestion_status=QLabel()
        self.suggestion_status.setObjectName("mutedLabel")
        self.suggestion_status.setWordWrap(True)
        self.suggestion_status.setAccessibleName("Incoming findings status")
        panel_layout.insertWidget(0,self.suggestion_status)
        self.ai_progress=QProgressBar()
        self.ai_progress.setTextVisible(True)
        self.ai_progress.hide()
        panel_layout.insertWidget(0,self.ai_progress)
        pending_tags=QGroupBox("Tags to Review")
        self.pending_tags_group=pending_tags
        pending_tags_layout=QVBoxLayout(pending_tags)
        self.pending_tags_layout=pending_tags_layout
        pending_tags.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.MinimumExpanding
        )
        pending_tags_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.batch_conflict_panel=self._build_batch_conflict_panel()
        pending_tags_layout.addWidget(self.batch_conflict_panel)
        if allow_ai:
            tag_ai_actions=QHBoxLayout()
            self.suggest_ai_tags_button=compact_button(
                QPushButton("Generate AI Tags")
            )
            self.suggest_ai_tags_button.setToolTip(
                "Run the enabled AI Tag models. New, non-redundant results "
                "remain here until you add them, decide later, or ignore them."
            )
            self.suggest_ai_tags_button.clicked.connect(
                lambda:self._request_ai("tags")
            )
            self.suggest_ai_tags_button.hide()
            tag_ai_actions.addStretch(1)
            pending_tags_layout.addLayout(tag_ai_actions)
        self.pending_tag_filter_panel=QWidget()
        self.pending_tag_filter_panel.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.Maximum
        )
        tag_filter_row=QHBoxLayout(self.pending_tag_filter_panel)
        tag_filter_row.setContentsMargins(0,0,0,0)
        self.pending_tag_filter=self.tag_conflict_filter
        self.pending_tag_conflict_status=QLabel()
        self.pending_tag_conflict_status.setObjectName("mutedLabel")
        self.pending_tag_conflict_status.setWordWrap(True)
        self.pending_tag_conflict_status.setAccessibleName(
            "Pending SDXL Tag conflict status"
        )
        tag_filter_row.addWidget(self.pending_tag_conflict_status,1)
        pending_tags_layout.addWidget(self.pending_tag_filter_panel)
        self.pending_tag_chips=TagChipEditor(
            editable=False,show_status=False,selectable=True,draggable=True,
            accessible_role="Pending SDXL Tag",
            drag_values_provider=self._pending_drag_values,
            context_menu_provider=self._pending_tag_actions_menu,
        )
        self.pending_tag_chips.scroll.setAccessibleName("Pending SDXL Tags")
        self.pending_tag_chips.scroll.setMinimumHeight(100)
        self.pending_tag_chips.setMinimumHeight(120)
        self.pending_tag_chips.setMaximumHeight(220)
        self.pending_tag_chips.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.Expanding
        )
        self.pending_tag_chips.selection_changed.connect(
            self._pending_tag_selection_changed
        )
        pending_tags_layout.addWidget(self.pending_tag_chips)
        self.pending_tag_empty=QLabel("No Tags need review.")
        self.pending_tag_empty.setAlignment(Qt.AlignCenter)
        self.pending_tag_empty.setObjectName("mutedLabel")
        self.pending_tag_empty.setWordWrap(True)
        pending_tags_layout.addWidget(self.pending_tag_empty)
        self.pending_tag_actions_panel=QWidget()
        tag_actions=FlowLayout(self.pending_tag_actions_panel,v_spacing=3)
        tag_actions.setContentsMargins(0,0,0,0)
        self.confirm_tag_suggestions=compact_button(
            QPushButton("Add Selected"),primary=True
        )
        self.confirm_tag_suggestions.setToolTip(
            "Stage the selected SDXL Tags as confirmed Image Knowledge. "
            "They are written when you choose Save Changes."
        )
        self.confirm_tag_suggestions.clicked.connect(
            self._add_selected_review_tags
        )
        self.dismiss_tag_suggestions=compact_button(
            QPushButton("Decide Later")
        )
        self.dismiss_tag_suggestions.setToolTip(
            "Hide the selected findings for now without rejecting them or "
            "removing their source evidence."
        )
        self.dismiss_tag_suggestions.clicked.connect(
            self._dismiss_selected_tag_suggestions
        )
        self.ignore_tag_suggestions=compact_button(
            QPushButton("Ignore Selected on This Image")
        )
        self.ignore_tag_suggestions.clicked.connect(
            self._ignore_selected_tag_suggestions
        )
        tag_actions.addWidget(self.confirm_tag_suggestions)
        tag_actions.addWidget(self.dismiss_tag_suggestions)
        tag_actions.addWidget(self.ignore_tag_suggestions)
        self.inspect_related_tag_button=compact_button(
            QPushButton("Why Suggested…")
        )
        self.inspect_related_tag_button.setToolTip(
            "Inspect the reviewed relationship behind the selected related Tag."
        )
        self.inspect_related_tag_button.clicked.connect(
            lambda:self._review_related_tags(
                self._selected_related_suggestions()
            )
        )
        self.inspect_related_tag_button.hide()
        tag_actions.addWidget(self.inspect_related_tag_button)
        self.review_tag_more_button=compact_button(QPushButton("More"))
        self.review_tag_more_menu=QMenu(self.review_tag_more_button)
        self.safe_related_action=self.review_tag_more_menu.addAction(
            "Add Safe Related Tags…"
        )
        self.safe_related_action.triggered.connect(
            self._add_safe_related_tags
        )
        self.review_tag_more_button.setMenu(self.review_tag_more_menu)
        self.review_tag_more_button.setAccessibleName(
            "More actions for Tags to review"
        )
        self.review_tag_more_button.hide()
        tag_actions.addWidget(self.review_tag_more_button)
        self.pending_source_actions_panel=QWidget()
        pending_source_actions=FlowLayout(self.pending_source_actions_panel,v_spacing=3)
        pending_source_actions.setContentsMargins(0,0,0,0)
        self.remove_pending_from_training_txt_button=compact_button(
            QPushButton("Remove from Training TXT…")
        )
        self.remove_pending_from_training_txt_button.setAccessibleName(
            "Remove selected pending SDXL Tag from Training TXT"
        )
        self.remove_pending_from_training_txt_button.setToolTip(
            "Open the Dataset-owned preview for one exact Tag currently found "
            "in this image's adjacent Training TXT. Image Knowledge is not "
            "changed by the source-only action."
        )
        self.remove_pending_from_training_txt_button.clicked.connect(
            self._request_pending_training_txt_correction
        )
        self.remove_pending_from_training_txt_button.setVisible(
            self.source_correction_available
        )
        pending_source_actions.addWidget(
            self.remove_pending_from_training_txt_button
        )
        self.remove_pending_from_metadata_button=compact_button(
            QPushButton("Remove from Metadata…")
        )
        self.remove_pending_from_metadata_button.setAccessibleName(
            "Remove selected pending SDXL Tag from managed Metadata"
        )
        self.remove_pending_from_metadata_button.setToolTip(
            "Open the Dataset-owned preview for one exact Tag currently found "
            "in this image's app-managed XMP:Subject field. Image Knowledge "
            "and every unrelated Metadata field remain unchanged."
        )
        self.remove_pending_from_metadata_button.clicked.connect(
            self._request_pending_metadata_tag_correction
        )
        self.remove_pending_from_metadata_button.setVisible(
            self.source_correction_available
        )
        pending_source_actions.addWidget(
            self.remove_pending_from_metadata_button
        )
        self.pending_tags_page=QWidget()
        pending_page_layout=QVBoxLayout(self.pending_tags_page)
        pending_page_layout.setContentsMargins(0,0,0,0)
        pending_page_layout.setSpacing(4)
        self.pending_tags_scroll=QScrollArea()
        self.pending_tags_scroll.setObjectName("reviewPendingWorkspaceScroll")
        self.pending_tags_scroll.setAccessibleName(
            "Scrollable Review Pending Tag findings"
        )
        self.pending_tags_scroll.setFrameShape(QScrollArea.NoFrame)
        self.pending_tags_scroll.setWidgetResizable(True)
        self.pending_tags_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self.pending_tags_scroll.setWidget(pending_tags)
        pending_page_layout.addWidget(self.pending_tags_scroll,1)
        self.pending_tag_footer=QWidget()
        self.pending_tag_footer.setAccessibleName(
            "Actions for selected Tags to review"
        )
        pending_footer_layout=QVBoxLayout(self.pending_tag_footer)
        pending_footer_layout.setContentsMargins(0,0,0,0)
        pending_footer_layout.setSpacing(3)
        pending_footer_layout.addWidget(self.pending_tag_actions_panel)
        pending_footer_layout.addWidget(self.pending_source_actions_panel)
        pending_page_layout.addWidget(self.pending_tag_footer)
        self.tag_activity_tabs.insertTab(
            0,self.pending_tags_page,"Review Pending"
        )
        self.tag_activity_tabs.setTabToolTip(
            self.tag_activity_tabs.indexOf(self.pending_tags_page),
            "Review pending Tag findings and related Tag suggestions.",
        )
        self.tag_activity_tabs.setCurrentWidget(self.pending_tags_page)

        pending_descriptions=QGroupBox("Pending Plain Language Descriptions")
        self.pending_descriptions_group=pending_descriptions
        pending_descriptions_layout=QVBoxLayout(pending_descriptions)
        if allow_ai:
            description_ai_actions=QHBoxLayout()
            self.suggest_ai_descriptions_button=compact_button(
                QPushButton("Generate AI Descriptions")
            )
            self.suggest_ai_descriptions_button.setToolTip(
                "Draft a Plain Language Description and keep it here as a "
                "reviewable finding until you add it, decide later, or ignore it."
            )
            self.suggest_ai_descriptions_button.clicked.connect(
                lambda:self._request_ai("description")
            )
            self.suggest_ai_descriptions_button.hide()
            description_ai_actions.addStretch(1)
            pending_descriptions_layout.addLayout(description_ai_actions)
        self.description_suggestion_status=QLabel()
        self.description_suggestion_status.setObjectName("mutedLabel")
        self.description_suggestion_status.setAccessibleName(
            "Pending Plain Language Description selection status"
        )
        pending_descriptions_layout.addWidget(self.description_suggestion_status)
        self.description_suggestions=ExtendedSelectionTreeWidget()
        self.description_suggestions.setHeaderLabels(
            ("Description","Source","Review")
        )
        self.description_suggestions.setRootIsDecorated(False)
        description_header=self.description_suggestions.header()
        description_header.setStretchLastSection(False)
        description_header.setSectionResizeMode(0,QHeaderView.Stretch)
        description_header.setSectionResizeMode(1,QHeaderView.Fixed)
        description_header.setSectionResizeMode(2,QHeaderView.Fixed)
        self.description_suggestions.setColumnWidth(1,170)
        self.description_suggestions.setColumnWidth(2,150)
        self.description_suggestions.setAccessibleName(
            "Pending Plain Language Descriptions"
        )
        self.description_suggestions.itemSelectionChanged.connect(
            self._update_suggestion_actions
        )
        pending_descriptions_layout.addWidget(self.description_suggestions,1)
        self.description_suggestion_empty=QLabel(
            "No pending Plain Language Descriptions."
        )
        self.description_suggestion_empty.setAlignment(Qt.AlignCenter)
        self.description_suggestion_empty.setObjectName("mutedLabel")
        self.description_suggestion_empty.setWordWrap(True)
        pending_descriptions_layout.addWidget(
            self.description_suggestion_empty
        )
        self.pending_description_actions_panel=QWidget()
        description_actions=FlowLayout(self.pending_description_actions_panel,v_spacing=3)
        description_actions.setContentsMargins(0,0,0,0)
        self.confirm_description_suggestions=compact_button(
            QPushButton("Add Selected"),primary=True
        )
        self.confirm_description_suggestions.clicked.connect(
            self._confirm_selected_description_suggestions
        )
        self.dismiss_description_suggestions=compact_button(
            QPushButton("Decide Later")
        )
        self.dismiss_description_suggestions.setToolTip(
            "Hide the selected findings for now without rejecting them or "
            "removing their source evidence."
        )
        self.dismiss_description_suggestions.clicked.connect(
            self._dismiss_selected_description_suggestions
        )
        self.ignore_description_suggestions=compact_button(
            QPushButton("Ignore Selected on This Image")
        )
        self.ignore_description_suggestions.clicked.connect(
            self._ignore_selected_description_suggestions
        )
        description_actions.addWidget(self.confirm_description_suggestions)
        description_actions.addWidget(self.dismiss_description_suggestions)
        description_actions.addWidget(self.ignore_description_suggestions)
        pending_descriptions_layout.addWidget(self.pending_description_actions_panel)
        self.remove_pending_description_from_training_txt_button=compact_button(
            QPushButton("Remove from Training TXT…")
        )
        self.remove_pending_description_from_training_txt_button.setAccessibleName(
            "Remove selected pending Plain Language Description from Training TXT"
        )
        self.remove_pending_description_from_training_txt_button.setToolTip(
            "Open the Dataset-owned preview for one exact description currently "
            "found in the adjacent Training TXT. Image Knowledge remains unchanged."
        )
        self.remove_pending_description_from_training_txt_button.clicked.connect(
            self._request_pending_training_txt_description_correction
        )
        self.remove_pending_description_from_training_txt_button.setVisible(
            self.source_correction_available
        )
        description_actions.addWidget(
            self.remove_pending_description_from_training_txt_button
        )
        self.remove_pending_description_from_metadata_button=compact_button(
            QPushButton("Remove from Metadata…")
        )
        self.remove_pending_description_from_metadata_button.setAccessibleName(
            "Remove selected pending Plain Language Description from managed Metadata"
        )
        self.remove_pending_description_from_metadata_button.setToolTip(
            "Open the Dataset-owned preview for one exact description currently "
            "found in this image's app-managed XMP:Description field. Image "
            "Knowledge and unrelated Metadata remain unchanged."
        )
        self.remove_pending_description_from_metadata_button.clicked.connect(
            self._request_pending_metadata_description_correction
        )
        self.remove_pending_description_from_metadata_button.setVisible(
            self.source_correction_available
        )
        description_actions.addWidget(
            self.remove_pending_description_from_metadata_button
        )
        self.ignore_button=compact_button(QPushButton("Description Ignores…"))
        ignore_menu=QMenu(self.ignore_button)
        self.ignore_everywhere_action=ignore_menu.addAction("Descriptions Everywhere")
        self.ignore_everywhere_action.triggered.connect(self._ignore_selected_descriptions_everywhere)
        ignore_menu.addSeparator()
        self.manage_description_ignores_action=ignore_menu.addAction("Review Ignored Descriptions Everywhere…")
        self.manage_description_ignores_action.triggered.connect(self._manage_global_description_ignores)
        self.ignore_button.setMenu(ignore_menu)
        description_actions.addWidget(self.ignore_button)
        descriptions_layout.insertWidget(1,pending_descriptions,1)

        # Common action aliases retained for integrations that target tag
        # findings while the visible surfaces remain strongly typed.
        self.confirm_suggestions=self.confirm_tag_suggestions
        self.dismiss_suggestions=self.dismiss_tag_suggestions
        self.discard_suggestions=self.ignore_tag_suggestions
        tabs.addTab(image_data, "Image Knowledge")
        tabs.addTab(descriptions,"Descriptions")
        tabs.addTab(character,"Characters")
        self.ignored_tab=self._build_ignored_tab()
        tabs.addTab(self.ignored_tab,"Ignored")

        sources = QWidget()
        sources_layout = QVBoxLayout(sources)
        reference_tabs = QTabWidget()
        self.reference_tabs = reference_tabs
        self.file_overview=QTextEdit()
        self.file_overview.setReadOnly(True)
        from .image_format_details import file_details_panel
        reference_tabs.addTab(file_details_panel(self.file_overview,self.record.image_path,self),"File")
        self.sources = QTextEdit()
        self.sources.setReadOnly(True)
        reference_tabs.addTab(self.sources, "Sources")
        self.generation_overview=QTextEdit()
        self.generation_overview.setReadOnly(True)
        generation_panel=QWidget(); generation_layout=QVBoxLayout(generation_panel)
        generation_layout.addWidget(self.generation_overview,1)
        self.plan_generation_button=QPushButton("Plan Generation from This Image…")
        self.plan_generation_button.clicked.connect(self._open_generation_plan)
        generation_layout.addWidget(self.plan_generation_button)
        reference_tabs.addTab(generation_panel,"Generation")
        raw_panel = QWidget()
        raw_layout = QVBoxLayout(raw_panel)
        self.raw_sources = QTextEdit()
        self.raw_sources.setReadOnly(True)
        self.raw_sources.setLineWrapMode(QTextEdit.NoWrap)
        raw_layout.addWidget(self.raw_sources, 1)
        reference_tabs.addTab(raw_panel, "Exact Raw Metadata")
        sources_layout.addWidget(reference_tabs)
        self.metadata_tab=sources
        tabs.addTab(sources, "Metadata")

        self._suggested = {}
        self.tag_activity_tabs.currentChanged.connect(
            self._tag_activity_tab_changed
        )
        self._update_tag_workspace_layout(force=True)
        if self.batch_conflict_context:
            self._show_pending_tag_workspace()

        return panel

    def _show_pending_tag_workspace(self):
        self.tabs.setCurrentWidget(self.image_data_tab)
        self.tag_activity_tabs.setCurrentWidget(self.pending_tags_page)

    def _refresh_tag_root_categories(self):
        control=self.tag_root_category
        selected=control.currentData() or "__all__"
        blocked=control.blockSignals(True)
        control.clear(); control.addItem("All Categories","__all__")
        for category in self.categories.children_of(None):
            control.addItem(category.name,category.id)
        control.setCurrentIndex(max(0,control.findData(selected)))
        control.blockSignals(blocked)

    def _tag_matches_shared_filters(self,value,stable_id=""):
        if not hasattr(self,"tag_palette"):
            return True
        return self.tag_palette.matches_view(
            value,stable_id=stable_id,
            conflict=bool(self.tag_palette.conflicts.get(normalize_tag(value))),
        )

    def _visible_pending_tag_items(self):
        return {
            key[1]:item for key,item in self._suggested.items()
            if key[0]=="tag" and self.tag_palette.matches_view(
                item["value"],conflict=key[1] in getattr(self,"_pending_conflicts",{}),
            )
        }

    def _reset_shared_tag_filters(self,conflicts=False):
        controls=(self.tag_root_category,self.tag_palette.main,self.tag_palette.child,
                  self.tag_palette.search,self.tag_conflict_filter)
        blocked=[control.blockSignals(True) for control in controls]
        self.tag_root_category.setCurrentIndex(0)
        self.tag_palette.main.setCurrentIndex(self.tag_palette.main.findData("__all__"))
        self.tag_palette.child.setCurrentIndex(0)
        self.tag_palette.search.clear()
        self.tag_conflict_filter.setCurrentIndex(self.tag_conflict_filter.findData("conflicts" if conflicts else "all"))
        for control,state in zip(controls,blocked):control.blockSignals(state)
        self.tag_palette._selection_changed()

    def _update_shared_tag_filter_state(self):
        if not hasattr(self,"reset_tag_filters_button"):
            return
        palette=self.tag_palette
        active=sum((
            bool(palette.search.text().strip()),
            self.tag_root_category.currentData()!="__all__",
            palette.main.currentData()!="__all__",
            bool(palette.child.currentData()),
            self.tag_conflict_filter.currentData()!="all",
        ))
        button=palette.filter_toggle
        button.setText(f"Filters Applied ({active})" if active else "Filters...")
        description=(
            f"{active} filters applied to both Tag panes. Reset Filters shows all Tags without changing staged edits."
            if active else "No filters applied to either Tag pane."
        )
        button.setAccessibleName(button.text()+"; show filters for both Tag panes")
        button.setAccessibleDescription(description)
        button.setToolTip(description)
        self.reset_tag_filters_button.setAccessibleDescription(description)
        self.reset_tag_filters_button.setEnabled(bool(active))
        for control,key in ((button,"activeFilter"),(self.reset_tag_filters_button,"primary")):
            if bool(control.property(key))!=bool(active):
                control.setProperty(key,bool(active))
                control.style().unpolish(control); control.style().polish(control)
                control.update()

    def _tag_root_category_changed(self,*_):
        self._update_shared_tag_filter_state()
        self._refresh_confirmed_tag_surface()
        self._refresh_related_tags()
        self._refresh_pending_tag_surface()
        self._update_suggestion_actions()

    def _tag_activity_tab_changed(self,*_):
        """Keep the shared view filters stable when changing the source pane."""
        self._update_tag_workspace_layout()

    def _update_tag_workspace_layout(self,*,force=False):
        """Keep both Tag lists readable and retain each layout's proportions."""
        if not hasattr(self,"tag_workspace_splitter"):
            return
        pending=int(getattr(self,"_pending_tag_count",0))
        if hasattr(self,"_suggested") and hasattr(self,"tag_palette"):
            pending=len(self._visible_pending_tag_items())
        related=int(getattr(self,"_visible_related_tag_count",getattr(self,"_related_suggestion_count",0)))
        compact=(
            self.tag_activity_tabs.currentWidget() is self.pending_tags_page
            and not pending and not related
        )
        splitter=self.tag_workspace_splitter
        # Use the available workspace, including preview-divider changes, and
        # actual control minima so font scaling cannot clip either column.
        # Chip and action rows already wrap. Reserve a useful column width,
        # rather than forcing normal editor windows into the stacked fallback.
        scroll_width=self.confirmed_tags_scroll.verticalScrollBar().sizeHint().width()
        confirmed_width=max(300,self.confirmed_tags_group.minimumSizeHint().width()+scroll_width)
        activity_width=max(
            320,self.tag_activity_tabs.minimumSizeHint().width(),
            self.available_tags_content.minimumSizeHint().width()+scroll_width+4,
        )
        wide=splitter.width()>=confirmed_width+activity_width+splitter.handleWidth()+32
        orientation=Qt.Horizontal if wide else Qt.Vertical
        if hasattr(self,"pending_tag_chips"):
            chips=self.pending_tag_chips
            natural_height=chips.flow.heightForWidth(chips.scroll.viewport().width())+16
            chips.setMaximumHeight(max(120,natural_height) if wide else 220)
        key=(orientation,compact)
        previous=getattr(self,"_tag_workspace_layout_key",None)
        if not force and key==previous:
            return
        self._tag_workspace_layout_key=key
        self._tag_workspace_compact=compact
        self.confirmed_tags_scroll.setMinimumWidth(confirmed_width if wide else 0)
        self.tag_activity_tabs.setMinimumWidth(activity_width if wide else 0)
        splitter.setOrientation(orientation)
        confirmed_index=1 if wide else 0
        if splitter.indexOf(self.confirmed_tags_scroll)!=confirmed_index:
            splitter.insertWidget(confirmed_index,self.confirmed_tags_scroll)
        splitter.setStretchFactor(confirmed_index,3 if compact else (1 if wide else 2))
        splitter.setStretchFactor(1-confirmed_index,1 if wide or compact else 3)
        defaults=((180,720) if wide else (720,180)) if compact else ((720,720) if wide else (360,620))
        splitter.setSizes(self._tag_workspace_sizes.get(key,defaults))
        self._tag_workspace_layout_timer.start(0)

    def _remember_tag_workspace_sizes(self,*_):
        key=getattr(self,"_tag_workspace_layout_key",None)
        if key is not None:
            self._tag_workspace_sizes[key]=self.tag_workspace_splitter.sizes()
        self._tag_workspace_layout_timer.start(0)

    def _refresh_pending_tag_empty_state(self):
        if not hasattr(self,"pending_tag_empty"):return
        self.pending_tag_empty.setText(
            "No Tags match these filters."
            if any(key[0]=="tag" for key in getattr(self,"_suggested",{}))
            or getattr(self,"_related_suggestion_count",0)
            else "No Tags need review."
        )
        self.pending_tag_empty.setVisible(
            not self.pending_tag_chips._chips
            and not getattr(self,"_visible_related_tag_count",0)
        )

    def _batch_conflict_proposals(self):
        proposals=tuple(self.batch_conflict_context.get("proposals") or ())
        if proposals:
            return proposals
        primary=str(self.batch_conflict_context.get("primary_display") or "").strip()
        conflicts=tuple(self.batch_conflict_context.get("conflicts") or ())
        if not primary or not conflicts:
            return ()
        return ({
            "tag_id":str(self.batch_conflict_context.get("primary_tag_id") or ""),
            "display":primary,
            "conflicts":conflicts,
            "owner_id":"",
            "owner_group":"",
            "suggestion":None,
        },)

    def _build_batch_conflict_panel(self):
        proposals=self._batch_conflict_proposals()
        panel=QGroupBox(
            "Proposed Batch Tag Conflict"
            if len(proposals)==1 else
            f"Proposed Batch Tag Conflicts ({len(proposals):,})"
        )
        layout=QVBoxLayout(panel)
        detail=QLabel(
            "The selected-image batch held these proposed SDXL Tags for review. "
            "Resolve the disclosed conflicting confirmed tags, or stage an exception "
            "deliberately. Saving this review updates Image Knowledge only; TXT training "
            "data, Metadata, and image files remain unchanged."
        )
        detail.setWordWrap(True); layout.addWidget(detail)
        for proposal in proposals:
            display=str(proposal.get("display") or "").strip()
            conflicts=tuple(proposal.get("conflicts") or ())
            proposed=QGroupBox(f'Proposed “{display}”')
            proposed_layout=QVBoxLayout(proposed)
            conflict_text=QLabel(
                "\n".join(
                    f"• {getattr(conflict,'message',str(conflict))}"
                    for conflict in conflicts
                )
            )
            conflict_text.setWordWrap(True); conflict_text.setObjectName("errorLabel")
            proposed_layout.addWidget(conflict_text)
            actions=QHBoxLayout(); actions.addStretch(1)
            add=compact_button(QPushButton(f'Stage “{display}” Anyway'))
            add.setToolTip(
                "Stage this proposed SDXL Tag on its disclosed image or owner scope. "
                "Review and remove an incorrect conflicting fact before saving unless "
                "both are a valid exception."
            )
            add.clicked.connect(
                lambda _checked=False,value=proposal:self._stage_batch_conflict_tag(value)
            )
            actions.addWidget(add); proposed_layout.addLayout(actions)
            layout.addWidget(proposed)
        panel.setVisible(bool(proposals))
        return panel

    def _stage_batch_conflict_tag(self,proposal=None):
        proposal=proposal or (
            self._batch_conflict_proposals()[0]
            if self._batch_conflict_proposals() else {}
        )
        value=str(proposal.get("display") or "").strip()
        if not value:
            return
        suggestion=proposal.get("suggestion")
        self._approved_conflict_states.add(frozenset(
            {
                *self.current_tags.normalized_set(),
                normalize_tag(value),
            }
        ))
        if suggestion is not None:
            added=bool(self._stage_add_related_suggestions((suggestion,)))
        else:
            added=bool(self._stage_add_tag(value))
        if not added:
            return
        button=self.sender()
        if isinstance(button,QPushButton):
            button.setText("Staged")
            button.setEnabled(False)
        self._refresh_tag_lists()
        self._refresh_suggestions()
        self._update_pending_status()
        self.tabs.setCurrentWidget(self.image_data_tab)

    def _build_ignored_tab(self):
        panel=QWidget(); layout=QVBoxLayout(panel)
        intro=QLabel(
            "Decide Later findings are removed from review without marking their evidence incorrect. "
            "Ignored items remain blocked on this image. Select either kind to restore it; changes are written when you choose Save Changes."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.ignored_status=QLabel(); self.ignored_status.setObjectName("mutedLabel")
        self.ignored_status.setAccessibleName("Ignored image data selection status")
        layout.addWidget(self.ignored_status)
        self.ignored_items=ExtendedSelectionTreeWidget(); self.ignored_items.setHeaderLabels(("Information","Kind")); self.ignored_items.setRootIsDecorated(False)
        header=self.ignored_items.header(); header.setSectionResizeMode(0,QHeaderView.Stretch)
        header.setSectionResizeMode(1,QHeaderView.Fixed); self.ignored_items.setColumnWidth(1,210)
        self.ignored_items.itemSelectionChanged.connect(self._update_ignored_actions); layout.addWidget(self.ignored_items,1)
        self.ignored_empty=QLabel("Nothing is ignored on this image."); self.ignored_empty.setAlignment(Qt.AlignCenter); self.ignored_empty.setObjectName("mutedLabel"); layout.addWidget(self.ignored_empty,1)
        actions=QHBoxLayout(); self.restore_ignored_button=compact_button(QPushButton("Restore Selected"),primary=True); self.restore_ignored_button.clicked.connect(self._restore_selected_ignores)
        self.global_ignores_button=compact_button(QPushButton("Global Description Ignores…")); self.global_ignores_button.clicked.connect(self._manage_global_description_ignores)
        actions.addWidget(self.restore_ignored_button); actions.addStretch(1); actions.addWidget(self.global_ignores_button); layout.addLayout(actions)
        return panel

    def _build_character_information(self):
        panel=QWidget(); layout=QVBoxLayout(panel)
        layout.setContentsMargins(0,0,0,0)
        self._character_view_service=CharacterImageKnowledgeService(
            self.knowledge,self.characters,self.categories,
        )
        self._character_ownership_service=CharacterOwnershipService(
            self.knowledge,self.characters,self.categories,
        )
        self.automatic_character_sources=QCheckBox(
            "Add known Character Source tags automatically"
        )
        self.automatic_character_sources.setChecked(
            bool(self._source_autofill_state and self._source_autofill_state[0])
        )
        self.automatic_character_sources.setEnabled(self._source_autofill_state is not None)
        self.automatic_character_sources.setToolTip(
            "For this image, add a confirmed character's unambiguous reviewed "
            "source when its rules allow automation. Save keeps this choice; "
            "removing or undoing a source addition prevents automatic re-addition."
        )
        self.automatic_character_sources.toggled.connect(self._toggle_automatic_character_sources)
        layout.addWidget(self.automatic_character_sources)
        self.character_source_status=QLabel()
        self.character_source_status.setWordWrap(True)
        self.character_source_status.setAccessibleName("Automatic Character Source prerequisites")
        layout.addWidget(self.character_source_status)
        self.character_summary=QLabel()
        self.character_summary.setWordWrap(True)
        self.character_summary.setAccessibleName("Character Tag summary")
        layout.addWidget(self.character_summary)
        self.character_facts=QTreeWidget()
        self.character_facts.setHeaderLabels(
            ("Character knowledge","Role","Reusable link")
        )
        self.character_facts.setRootIsDecorated(True)
        self.character_facts.setAlternatingRowColors(True)
        facts_header=self.character_facts.header()
        facts_header.setSectionResizeMode(0,QHeaderView.Stretch)
        facts_header.setSectionResizeMode(1,QHeaderView.Fixed)
        facts_header.setSectionResizeMode(2,QHeaderView.Fixed)
        self.character_facts.setColumnWidth(1,145)
        self.character_facts.setColumnWidth(2,230)
        self.character_facts.setAccessibleName("Confirmed Character Tags")
        layout.addWidget(self.character_facts,1)
        self.character_empty=QLabel(
            "No confirmed Character Identity, Character Source, or Character "
            "Outfit Tags are on this image."
        )
        self.character_empty.setAlignment(Qt.AlignCenter)
        self.character_empty.setWordWrap(True)
        self.character_empty.setObjectName("mutedLabel")
        layout.addWidget(self.character_empty,1)

        self.legacy_character_group=QGroupBox("Retained Ownership Context")
        legacy_layout=QVBoxLayout(self.legacy_character_group)
        self.legacy_character_summary=QLabel()
        self.legacy_character_summary.setWordWrap(True)
        self.legacy_character_summary.setObjectName("mutedLabel")
        legacy_layout.addWidget(self.legacy_character_summary)
        self.legacy_character_assignments=QTreeWidget()
        self.legacy_character_assignments.setHeaderLabels(
            ("Context","Identity / role","Outfits and owned facts","State")
        )
        self.legacy_character_assignments.setRootIsDecorated(False)
        legacy_header=self.legacy_character_assignments.header()
        legacy_header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        legacy_header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        legacy_header.setSectionResizeMode(2,QHeaderView.Stretch)
        legacy_header.setSectionResizeMode(3,QHeaderView.ResizeToContents)
        self.legacy_character_assignments.setAccessibleName(
            "Retained legacy character ownership context"
        )
        legacy_layout.addWidget(self.legacy_character_assignments)
        layout.addWidget(self.legacy_character_group)

        actions=QHBoxLayout()
        self.stage_legacy_character_tags_button=compact_button(
            QPushButton("Add Suggested Tags"),primary=True
        )
        self.stage_legacy_character_tags_button.setToolTip(
            "Add the Tags listed under Suggested additions to this image. "
            "The confirmed character is already recognized; these are separate "
            "facts supported by reviewed Character Knowledge. Nothing is "
            "written to the image record until Save Changes."
        )
        self.stage_legacy_character_tags_button.clicked.connect(
            self._stage_reviewed_character_tags
        )
        self.review_character_findings_button=compact_button(
            QPushButton("Review Character Findings")
        )
        self.review_character_findings_button.clicked.connect(
            self._review_character_findings
        )
        self.review_character_ownership_button=compact_button(
            QPushButton("Review Ownership…")
        )
        self.review_character_ownership_button.setToolTip(
            "Optionally assign one confirmed outfit or other confirmed fact to "
            "one or more confirmed Character identities on this image."
        )
        self.review_character_ownership_button.clicked.connect(
            self._review_character_ownership
        )
        actions.addWidget(self.stage_legacy_character_tags_button)
        actions.addWidget(self.review_character_findings_button)
        actions.addWidget(self.review_character_ownership_button)
        actions.addStretch(1)
        actions.addWidget(self.clear_character_identity_button)
        layout.addLayout(actions)

        return panel

    def _character_role(self,tag):
        if tag is None:
            return ""
        role=(
            self.categories.effective_semantic_role(tag.category)
            if self.categories is not None else tag.category
        )
        if role=="copyright_series":
            role="character_source"
        return role if role in {
            "character_identity","character_source","character_outfit",
        } else ""

    def _pending_character_tag_values(self):
        if self.knowledge is None:
            return ()
        values=[]
        for key,item in getattr(self,"_suggested",{}).items():
            if key[0]!="tag":
                continue
            value=str(item.get("value") or "").strip()
            tag=self.knowledge.find(value)
            if value and self._character_role(tag):
                values.append(value)
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _add_character_fact_row(parent,fact):
        linked=(
            f"{fact.link_state}: {fact.linked_name}"
            if fact.linked_name else fact.link_state
        )
        item=QTreeWidgetItem(parent,(fact.display,fact.role_label,linked))
        item.setData(0,Qt.UserRole,fact.display)
        item.setToolTip(0,fact.display)
        item.setToolTip(2,linked)
        return item

    def _refresh_character_view(self):
        if not hasattr(self,"character_facts"):
            return
        self._refresh_identity_clear_visibility()
        annotation=self._related_annotation_state()
        view=self._character_view_service.analyze(
            annotation,self.current_tags,
        )
        self._character_view=view
        ownership_view=self._character_ownership_service.analyze(
            annotation,self.current_tags,
        )
        self._character_ownership_view=ownership_view
        pending=self._pending_character_tag_values()
        review_count=len({
            normalize_tag(value)
            for value in (*view.suggested_values,*pending)
            if normalize_tag(value)
        })
        self._character_review_count=review_count
        self.character_summary.setText(
            f"{len(view.facts):,} confirmed Character Tag"
            f"{'s' if len(view.facts)!=1 else ''}. {view.ownership_state}."
            +(f" {len(view.suggested_values):,} additional Tag(s) suggested."
              if view.suggested_values else "")
            +(f" {len(pending):,} incoming Tag finding(s) await review."
              if pending else "")
            +(f" {len(self._source_autofill_added):,} source Tag(s) added automatically; Save keeps them."
              if self._source_autofill_added else "")
        )
        self.character_facts.clear()
        consumed=set()
        identities=[
            fact for fact in view.facts
            if fact.role=="character_identity" and fact.linked_kind=="character"
        ]
        identity_children={identity.tag_id:[] for identity in identities}
        shared=[]
        for fact in view.facts:
            if fact.role=="character_identity":
                continue
            matches=[
                identity for identity in identities
                if (
                    fact.role=="character_source"
                    and identity.source_id
                    and fact.source_id==identity.source_id
                ) or (
                    fact.role=="character_outfit"
                    and identity.linked_id in fact.character_ids
                )
            ]
            if len(matches)==1:
                identity_children[matches[0].tag_id].append(fact)
                consumed.add(fact.tag_id)
            elif len(matches)>1:
                shared.append(fact)
                consumed.add(fact.tag_id)
        for identity in identities:
            parent=QTreeWidgetItem((
                identity.linked_name or identity.display,
                "Character",
                "Tag-rooted reusable knowledge",
            ))
            parent.setData(0,Qt.UserRole,identity.display)
            self.character_facts.addTopLevelItem(parent)
            self._add_character_fact_row(parent,identity)
            consumed.add(identity.tag_id)
            for fact in identity_children[identity.tag_id]:
                self._add_character_fact_row(parent,fact)
        if shared:
            parent=QTreeWidgetItem((
                "Shared Character Tags","Character knowledge",
                "Applies to multiple confirmed identities",
            ))
            self.character_facts.addTopLevelItem(parent)
            for fact in shared:
                self._add_character_fact_row(parent,fact)
        remaining=[fact for fact in view.facts if fact.tag_id not in consumed]
        if remaining:
            parent=QTreeWidgetItem((
                "Other Character Tags","Character knowledge",
                "No image-specific owner inferred",
            ))
            self.character_facts.addTopLevelItem(parent)
            for fact in remaining:
                self._add_character_fact_row(parent,fact)
        if view.suggested_tag_ids:
            parent=QTreeWidgetItem((
                "Suggested additions","Not confirmed",
                "Add only if correct for this image",
            ))
            self.character_facts.addTopLevelItem(parent)
            for tag_id in view.suggested_tag_ids:
                tag=self.knowledge.tags.get(tag_id)
                if tag is None:
                    continue
                role=self._character_role(tag).replace("_"," ").title()
                item=QTreeWidgetItem(parent,(
                    tag.display,role,"Suggested from reviewed Character Knowledge",
                ))
                item.setData(0,Qt.UserRole,tag.display)
                for column in range(3):
                    item.setToolTip(column,item.text(column))
        self.character_facts.expandAll()
        self.character_facts.setVisible(bool(view.facts or view.suggested_tag_ids))
        self.character_empty.setVisible(not (view.facts or view.suggested_tag_ids))

        self.legacy_character_assignments.clear()
        for context in view.legacy_contexts:
            identity=context.identity
            if context.source:
                identity=f"{identity} — {context.source}"
            role=context.role.replace("_"," ").title() if context.role else ""
            identity_role="; ".join(value for value in (identity,role) if value)
            details=[*context.outfits,*context.owned_tags]
            row=QTreeWidgetItem((
                f"Character {context.slot}",identity_role,
                ", ".join(details) or "No owner-scoped facts",context.state,
            ))
            row.setData(0,Qt.UserRole,context.assignment_id)
            row.setToolTip(2,"\n".join(details))
            self.legacy_character_assignments.addTopLevelItem(row)
        self.legacy_character_group.setVisible(bool(view.legacy_contexts))
        self.legacy_character_summary.setText(
            "These compatibility records retain stable ownership, roles, outfit "
            "links, and unknown data. Shared Tag edits can deliberately change "
            "their owned facts; reusable identity, source, and outfit facts "
            "belong in Character Tags."
        )
        self.stage_legacy_character_tags_button.setVisible(
            bool(view.suggested_values)
        )
        self.stage_legacy_character_tags_button.setText(
            "Add Suggested Tags"
            +(f" ({len(view.suggested_values):,})" if view.suggested_values else "")
        )
        self.review_character_findings_button.setVisible(bool(pending))
        self.review_character_findings_button.setText(
            "Review Character Findings"
            +(f" ({len(pending):,})" if pending else "")
        )
        ownership_available=bool(
            ownership_view.owners and ownership_view.facts
            and (
                len(ownership_view.owners)>1
                or any(fact.owner_keys for fact in ownership_view.facts)
            )
        )
        self.review_character_ownership_button.setVisible(
            view.identity_count>1
            or any(fact.owner_keys for fact in ownership_view.facts)
        )
        self.review_character_ownership_button.setEnabled(ownership_available)
        self.review_character_ownership_button.setToolTip(
            "Review which confirmed Character Identity owns a confirmed outfit "
            "or other confirmed fact on this image."
            if ownership_available else
            "Confirm at least one exact outfit or other fact before assigning "
            "ownership between the confirmed Character identities."
        )
        if hasattr(self,"tabs") and hasattr(self,"character_tab"):
            index=self.tabs.indexOf(self.character_tab)
            if index>=0:
                self.tabs.setTabText(
                    index,"Characters"
                    +(f" ({review_count:,} suggested)" if review_count else ""),
                )
                self.tabs.setTabToolTip(
                    index,
                    f"{len(view.facts):,} confirmed Character Tag(s); "
                    f"{review_count:,} item(s) to review",
                )
                self._update_attention_animation()

    def _review_character_ownership(self):
        annotation=self._related_annotation_state()
        view=self._character_ownership_service.analyze(
            annotation,self.current_tags,
        )
        if not view.owners or not view.facts:
            return
        dialog=CharacterOwnershipDialog(view,self)
        if dialog.exec()!=QDialog.Accepted:
            return
        selection=dialog.selection()
        previous=(
            tuple(item.to_dict() for item in self._working_characters),
            copy.deepcopy(self._character_ownership_changes),
        )
        try:
            changed=self._character_ownership_service.set_fact_owners(
                annotation,self.current_tags,
                fact_tag_id=str(selection.get("fact_tag_id") or ""),
                owner_keys=tuple(selection.get("owner_keys") or ()),
                group=str(selection.get("group") or ""),
            )
        except CharacterOwnershipError as exc:
            QMessageBox.warning(
                self,"Character ownership was not changed",str(exc),
            )
            return
        if not changed:
            return
        fact=next(
            item for item in view.facts
            if item.tag_id==str(selection.get("fact_tag_id") or "")
        )
        self._tag_history.append((
            "character_ownership",previous,None,-1,
        ))
        self._working_characters=copy.deepcopy(annotation.characters)
        self._character_ownership_changes[fact.tag_id]={
            "display":fact.display,
            "owned":bool(selection.get("owner_keys")),
            "role":fact.role,
        }
        self._refresh_tag_lists(); self._refresh_suggestions()
        self.tabs.setCurrentWidget(self.character_tab)

    def _stage_reviewed_character_tags(self):
        values=getattr(getattr(self,"_character_view",None),"suggested_values",())
        if not values:
            return
        added=self._stage_add_tags_batch(values)
        if not added:
            return
        self._refresh_tag_lists()
        self._refresh_suggestions()
        self.tabs.setCurrentWidget(self.character_tab)

    def _refresh_identity_clear_visibility(self):
        # Resolve description-only identities only when this section is open.
        # Ordinary prose typing and hidden-tab refreshes need no vocabulary scan.
        if (self.knowledge is None or not hasattr(self,"character_tab")
                or self.tabs.currentWidget() is not self.character_tab):
            self.clear_character_identity_button.hide()
            return
        plan=self._character_view_service.identity_clear_plan(
            self._related_annotation_state(),self.current_tags,
            self.prose.toPlainText(),prior_values=self.original_tags,
        )
        self.clear_character_identity_button.setVisible(bool(
            plan.removed_tags or plan.changed_descriptions or plan.association_count
        ))

    def _review_character_findings(self):
        values=self._pending_character_tag_values()
        self._show_pending_tag_workspace()
        if values:
            self.pending_tag_chips.focus_tag(values[0])

    def _pending_object_tags(self,owner_id):
        return [
            value
            for (kind,pending_owner,_group,_tag_id),value
            in self._pending_scoped_tag_additions.items()
            if kind=="object" and pending_owner==owner_id
        ]

    def _object_values(self):
        values=TagCollection()
        for item in self._working_objects:
            values.extend(item.all_tags())
            values.extend(self._pending_object_tags(item.id))
        return values.to_list()

    def _current_tags_after_object_sync(self):
        new_values=self._object_values()
        new_keys={normalize_tag(value) for value in new_values}
        non_object_pending={
            normalize_tag(value)
            for (kind,_owner,_group,_tag_id),value
            in self._pending_scoped_tag_additions.items()
            if kind!="object"
        }
        preserve=self._non_object_original_keys|non_object_pending
        result=self.current_tags.copy()
        for value in tuple(result):
            key=normalize_tag(value)
            if (
                key in self._object_owned_keys
                and key not in new_keys
                and key not in preserve
            ):
                result.remove(value)
        result.extend(new_values)
        return result,new_keys

    def _sync_current_tags_from_objects(self):
        self.current_tags,new_keys=self._current_tags_after_object_sync()
        self._object_owned_keys=new_keys
        self._reconcile_scoped_ingress_restores()

    def _reconcile_scoped_ingress_restores(self):
        current=self.current_tags.normalized_set()
        for scope,(value,state) in tuple(self._scoped_ingress_restore_states.items()):
            if normalize_tag(value) in current:
                continue
            self._undo_restored_ignore_for_add(value,state)
            self._scoped_ingress_restore_states.pop(scope,None)

    def _build_available_panel(self):
        panel = QGroupBox("Add SDXL Tags")
        panel.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.MinimumExpanding)
        panel.setToolTip(
            "Browse reviewed System Knowledge tags. Drag a tag to the confirmed "
            "region or use Add to This Image from its Actions menu. Changes "
            "remain staged until Save Changes."
        )
        layout = QVBoxLayout(panel)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        creation_actions=QHBoxLayout()
        creation_actions.addWidget(self.create_tag_button)
        creation_actions.addWidget(self.add_exact_tags_button)
        creation_actions.addStretch(1)
        layout.addLayout(creation_actions)
        layout.addWidget(self.new_tag_panel)
        palette_label=QLabel(
            "Select one or more tags, drag them to confirmed tags, or open a tag's Actions menu"
        )
        palette_label.setAccessibleName(
            "Known SDXL Tag palette for deliberate tag additions"
        )
        palette_label.setWordWrap(True)
        layout.addWidget(palette_label)
        self.tag_palette=TagPalette(
            self.knowledge,self.categories,
            prepared_index=self._tag_palette_index,
            shared_root_category=self.tag_root_category,
            shared_conflict_filter=self.tag_conflict_filter,
            defer_initial_render=True,
        )
        # The palette owns its projection fields now. Keeping the construction
        # receipt here would pin an obsolete vocabulary after a nested Tag edit.
        self._tag_palette_index=None
        self.tag_palette.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.MinimumExpanding
        )
        self.tag_palette.scroll.setMinimumHeight(140)
        self.tag_palette.tag_requested.connect(self._add_palette_tag)
        self.tag_palette.favorite_requested.connect(self._set_favorite_tag)
        self.tag_palette.show_present_requested.connect(
            self._show_confirmed_tag_from_palette
        )
        self.tag_palette.create_requested.connect(
            self._request_reusable_tag_creation
        )
        self.tag_palette.selection_changed.connect(
            self._palette_selection_changed
        )
        layout.addWidget(self.tag_palette,1)
        selection_actions=QHBoxLayout()
        self.palette_selection_status=QLabel()
        self.palette_selection_status.setObjectName("mutedLabel")
        selection_actions.addWidget(self.palette_selection_status,1)
        self.add_palette_selected_button=compact_button(
            QPushButton("Add Selected"),primary=True
        )
        self.add_palette_selected_button.clicked.connect(
            self._add_selected_palette_tags
        )
        self.add_palette_selected_button.setEnabled(False)
        selection_actions.addWidget(self.add_palette_selected_button)
        layout.addLayout(selection_actions)

        page=QScrollArea()
        page.setObjectName("addTagsWorkspaceScroll")
        page.setAccessibleName("Scrollable Add SDXL Tags workspace")
        page.setFrameShape(QScrollArea.NoFrame)
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page.setToolTip(panel.toolTip())
        page.setWidget(panel)
        self.available_tags_content=panel
        return page

    def _palette_selection_changed(self,values):
        count=len(tuple(values))
        self.palette_selection_status.setText(
            f"{count:,} tag{'s' if count!=1 else ''} selected" if count else ""
        )
        self.add_palette_selected_button.setEnabled(bool(count))

    def _add_selected_palette_tags(self):
        selected=self.tag_palette.selected_values()
        if not selected:return
        added=self._stage_add_tags_batch(selected)
        if not added:return
        self.tag_palette.clear_selection()
        self._refresh_tag_lists(); self._refresh_suggestions()

    def _show_confirmed_tag_from_palette(self,value):
        if not self._tag_matches_shared_filters(value):
            self._reset_shared_tag_filters()
        self.confirmed_tags_scroll.ensureWidgetVisible(self.tag_chips)
        self.tag_chips.focus_tag(value)
        QTimer.singleShot(0,lambda:self.tag_chips.focus_tag(value))

    def _build_current_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        heading=QHBoxLayout(); heading.addWidget(QLabel("SDXL Tags on this image")); heading.addStretch(1)
        layout.addLayout(heading)
        self.create_tag_button=compact_button(QPushButton("Create New Tag\u2026"))
        self.create_tag_button.setToolTip(
            "Create one reusable System Knowledge SDXL Tag in the shared Tag "
            "Details editor, then stage it on this image."
        )
        self.create_tag_button.clicked.connect(self._create_reusable_tag)
        self.add_exact_tags_button=compact_button(
            QPushButton("Paste Tag List\u2026")
        )
        self.add_exact_tags_button.setToolTip(
            "Paste comma-separated SDXL Tags. Each exact value resolves to one "
            "reusable System Knowledge identity; genuinely new values create "
            "uncategorized records before being staged on this image."
        )
        self.add_exact_tags_button.clicked.connect(self._show_new_tag_entry)
        self.undo_tag_button=compact_button(QPushButton("Undo Tag Change")); self.undo_tag_button.setEnabled(False); self.undo_tag_button.clicked.connect(self._undo_tag_change)
        self.new_tag_panel=QWidget(); entry_layout=QVBoxLayout(self.new_tag_panel); entry_layout.setContentsMargins(0,0,0,0)
        add_row=QHBoxLayout()
        self.new_tag=QLineEdit(); self.new_tag.setPlaceholderText("Paste comma-separated SDXL Tags, or enter one exact tag…")
        self.new_tag.setAccessibleName("Paste a comma-separated list or add one exact SDXL Tag")
        self.new_tag.setToolTip("Use CSV quotes around a tag that contains a comma. Every value is resolved or created in reusable System Knowledge before it is staged on this image.")
        self.add_tag_button=compact_button(QPushButton("Resolve and Add"))
        self.add_tag_button.setToolTip("Resolve or create reusable System Knowledge identities, then stage these exact tags on the current image.")
        self.add_tag_button.clicked.connect(self._add_exact_tag); self.new_tag.returnPressed.connect(self._add_exact_tag)
        add_row.addWidget(self.new_tag,1); add_row.addWidget(self.add_tag_button); entry_layout.addLayout(add_row)
        self.new_tag_status=QLabel(); self.new_tag_status.setObjectName("mutedLabel"); self.new_tag_status.hide(); entry_layout.addWidget(self.new_tag_status)
        self.new_tag_panel.hide()
        self.identity_repair_panel=QGroupBox("Confirmed Tag Identity Repair")
        identity_repair_layout=QHBoxLayout(self.identity_repair_panel)
        self.identity_repair_status=QLabel()
        self.identity_repair_status.setWordWrap(True)
        self.identity_repair_status.setObjectName("errorLabel")
        self.identity_repair_status.setAccessibleName(
            "Confirmed SDXL Tag identity repair status"
        )
        self.identity_repair_button=compact_button(
            QPushButton("Review Identity Repair\u2026"),primary=True,
        )
        self.identity_repair_button.clicked.connect(
            self._review_confirmed_identity_repair
        )
        identity_repair_layout.addWidget(self.identity_repair_status,1)
        identity_repair_layout.addWidget(
            self.identity_repair_button,alignment=Qt.AlignRight|Qt.AlignVCenter,
        )
        layout.addWidget(self.identity_repair_panel)
        self.identity_repair_panel.hide()
        confirmed_filter_row=QHBoxLayout()
        self.confirmed_tag_filter=self.tag_conflict_filter
        self.confirmed_conflict_status=QLabel()
        self.confirmed_conflict_status.setObjectName("mutedLabel")
        self.confirmed_conflict_status.setWordWrap(True)
        self.confirmed_conflict_status.setAccessibleName(
            "Confirmed SDXL Tag conflict status"
        )
        confirmed_filter_row.addWidget(self.confirmed_conflict_status,1)
        self.ownership_ack_panel=QWidget()
        ownership_ack_row=QHBoxLayout(self.ownership_ack_panel)
        ownership_ack_row.setContentsMargins(0,0,0,0)
        self.ownership_ack_choices=QComboBox()
        self.ownership_ack_choices.setAccessibleName("Exact ownership check to acknowledge")
        ownership_ack_row.addWidget(self.ownership_ack_choices,1)
        self.ownership_ack_button=compact_button(QPushButton("Both apply to different subjects"))
        self.ownership_ack_button.setAccessibleName("Acknowledge both Tags apply to different subjects")
        self.ownership_ack_button.setToolTip("Review this exact Tag pair. Staged until Save Changes; no owners or image facts are assigned.")
        self.ownership_ack_button.clicked.connect(self._stage_ownership_acknowledgement)
        ownership_ack_row.addWidget(self.ownership_ack_button)
        self.ownership_ack_status=QLabel()
        self.ownership_ack_status.setWordWrap(True)

        self.show_confirmed_conflicts_button=compact_button(
            QPushButton("Show Conflicts")
        )
        self.show_confirmed_conflicts_button.setToolTip(
            "Show the full conflict explanation and isolate the involved confirmed Tags."
        )
        self.show_confirmed_conflicts_button.clicked.connect(
            self._show_confirmed_conflicts
        )
        self.show_confirmed_conflicts_button.hide()
        confirmed_filter_row.addWidget(self.show_confirmed_conflicts_button)
        layout.addLayout(confirmed_filter_row)
        layout.addWidget(self.ownership_ack_panel)
        layout.addWidget(self.ownership_ack_status)
        self.confirmed_conflict_details=QLabel()
        self.confirmed_conflict_details.setTextFormat(Qt.PlainText)
        self.confirmed_conflict_details.setWordWrap(True)
        self.confirmed_conflict_details.setObjectName("mutedLabel")
        self.confirmed_conflict_details.setAccessibleName("Confirmed Tag conflict details")
        self.confirmed_conflict_details.hide()
        layout.addWidget(self.confirmed_conflict_details)
        self.tag_chips=TagChipEditor(
            selectable=True,accept_drops=True,accessible_role="Confirmed SDXL Tag",
        )
        self.tag_chips.drop_requested.connect(self._add_dropped_pending_tags)
        self.tag_chips.remove_requested.connect(self._remove_chip_tag)
        self.tag_chips.remove_ignore_requested.connect(self._remove_and_ignore_chip_tag)
        self.tag_chips.favorite_requested.connect(self._set_favorite_tag)
        self.tag_chips.edit_requested.connect(lambda stable_id:self.edit_tag_requested.emit(stable_id,self))
        self.tag_chips.promote_requested.connect(
            lambda _value:self._review_confirmed_identity_repair()
        )
        self.tag_chips.selection_changed.connect(self._update_confirmed_tag_actions)
        layout.addWidget(self.tag_chips,1)
        selection_actions=FlowLayout(v_spacing=3)
        self.confirmed_tag_selection_status=QLabel()
        self.confirmed_tag_selection_status.setObjectName("mutedLabel")
        self.confirmed_tag_selection_status.setAccessibleName(
            "Confirmed SDXL Tag selection status"
        )
        layout.addWidget(self.confirmed_tag_selection_status)
        self.remove_selected_tags_button=compact_button(
            QPushButton("Remove Selected")
        )
        self.remove_selected_tags_button.clicked.connect(
            self._remove_selected_confirmed_tags
        )
        self.remove_ignore_selected_tags_button=compact_button(
            QPushButton("Remove and Ignore Selected")
        )
        self.remove_ignore_selected_tags_button.clicked.connect(
            self._remove_ignore_selected_confirmed_tags
        )
        self.set_context_button=compact_button(
            QPushButton("Set Context…"),primary=True,
        )
        self.set_context_button.setToolTip(
            "Move the selected facts between Main Scene and a secondary view."
        )
        self.set_context_button.clicked.connect(self._set_selected_context)
        self.refine_selected_tag_button=compact_button(QPushButton("Review Details."))
        self.refine_selected_tag_button.setToolTip("Keep this fact and review one more specific detail for one exact occurrence.")
        self.refine_selected_tag_button.clicked.connect(self._review_selected_tag_details)
        selection_actions.addWidget(self.refine_selected_tag_button)
        selection_actions.addWidget(self.set_context_button)
        selection_actions.addWidget(self.remove_selected_tags_button)
        selection_actions.addWidget(self.remove_ignore_selected_tags_button)
        layout.addLayout(selection_actions)
        source_actions=FlowLayout(v_spacing=3)
        self.remove_confirmed_from_training_txt_button=compact_button(
            QPushButton("Remove from Training TXT…")
        )
        self.remove_confirmed_from_training_txt_button.setAccessibleName(
            "Remove selected confirmed SDXL Tag from Training TXT"
        )
        self.remove_confirmed_from_training_txt_button.setToolTip(
            "Open the Dataset-owned preview for one exact Tag currently found "
            "in this image's adjacent Training TXT. The confirmed Image "
            "Knowledge fact remains unless a later paired correction is chosen."
        )
        self.remove_confirmed_from_training_txt_button.clicked.connect(
            self._request_confirmed_training_txt_correction
        )
        self.remove_confirmed_from_training_txt_button.setVisible(
            self.source_correction_available
        )
        source_actions.addWidget(
            self.remove_confirmed_from_training_txt_button
        )
        self.remove_confirmed_from_metadata_button=compact_button(
            QPushButton("Remove from Metadata…")
        )
        self.remove_confirmed_from_metadata_button.setAccessibleName(
            "Remove selected confirmed SDXL Tag from managed Metadata"
        )
        self.remove_confirmed_from_metadata_button.setToolTip(
            "Open the Dataset-owned preview for one exact Tag currently found "
            "in this image's app-managed XMP:Subject field. The confirmed Image "
            "Knowledge fact remains unchanged."
        )
        self.remove_confirmed_from_metadata_button.clicked.connect(
            self._request_confirmed_metadata_tag_correction
        )
        self.remove_confirmed_from_metadata_button.setVisible(
            self.source_correction_available
        )
        source_actions.addWidget(
            self.remove_confirmed_from_metadata_button
        )
        layout.addLayout(source_actions)
        return panel

    def prepare_system_knowledge_refresh(self,index,revision):
        """Accept a worker-prepared palette for this exact saved Tag revision."""
        self._prepared_system_knowledge_refresh=(index,revision)

    def _refresh_automation_authority(self):
        # Nested saves can invalidate the retained engine and its bound callbacks.
        # Reacquire authority before evaluating or staging any new image facts.
        engine=active_automation_engine(self.knowledge)
        self.automation_engine=engine
        self._identity_service=ConfirmedTagIdentityService(
            self.knowledge,evidence_rejected=engine.is_evidence_rejected,
        )

    def refresh_system_knowledge(self):
        """Refresh chip presentation after a nested System Knowledge edit."""
        self._refresh_automation_authority()
        if self.characters is not None:self.characters.load()
        prepared=getattr(self,"_prepared_system_knowledge_refresh",None)
        self._prepared_system_knowledge_refresh=None
        index=(prepared[0] if prepared is not None and
               prepared[1]==getattr(self.knowledge,"_loaded_bytes",None) else None)
        self._related_tag_service=None
        self._refresh_tag_root_categories()
        # The following state update performs the complete palette render once.
        self.tag_palette.refresh_knowledge(prepared_index=index,defer_refresh=True)
        self._refresh_tag_lists(refresh_related=False,refresh_pending=False)
        self._refresh_suggestions()

    def _refresh_tag_lists(self,*,refresh_related=True,refresh_pending=True,refresh_character=True):
        self._stage_automatic_character_sources()
        self._update_automatic_character_description()
        raw_ordered=self._current_confirmed_tags()
        ordered,self._confirmed_display_identities=self._canonical_confirmed_state(
            raw_ordered
        )
        self.tag_palette.set_state(
            present=ordered,conflicts=self._palette_conflicts(ordered),
            favorites=self._favorite_tags,recents=self._recent_tags,
        )
        self._refresh_confirmed_tag_surface(ordered)
        self._refresh_confirmed_identity_repair()
        if refresh_pending and not refresh_related and hasattr(self,"_suggested") and hasattr(self,"description_suggestions"):
            self._refresh_pending_tag_surface()
        self.undo_tag_button.setEnabled(bool(self._tag_history))
        self.undo_tag_button.setText(
            "Undo Identity Clear" if self._tag_history and self._tag_history[-1][0]=="clear_character_identity"
            else "Undo Tag Change"
        )
        if refresh_related and hasattr(self,"pending_tag_chips"):
            self._refresh_related_tags()
        if hasattr(self,"prose"): self._refresh_confirmed_context()
        if hasattr(self,"pending_status"): self._update_pending_status()
        if refresh_character:self._refresh_character_view()
        self._update_shared_tag_filter_state()

    def _toggle_automatic_character_sources(self,enabled):
        if self._source_autofill_state is None:
            return
        self._source_autofill_state=(bool(enabled),self._source_autofill_state[1])
        self._refresh_tag_lists(); self._refresh_suggestions()

    def _toggle_automatic_character_description(self,enabled):
        if self._description_autofill_state is None:
            return
        self._description_autofill_state={
            **self._description_autofill_state,"enabled":bool(enabled),"sentence":"","original_sentence":"",
        }
        self._update_automatic_character_description()
        self._update_pending_status()

    def _description_autofill_edited(self):
        if self._applying_description_autofill or self._description_autofill_state is None:
            return
        state=self._description_autofill_state
        if state["enabled"] and state["sentence"] and state["sentence"] not in self.prose.toPlainText().split("\n"):
            self._description_autofill_state={**state,"enabled":False}
            blocked=self.automatic_character_description.blockSignals(True)
            self.automatic_character_description.setChecked(False)
            self.automatic_character_description.blockSignals(blocked)
            self.character_description_status.setText("Your description edit is preserved. Automatic identity additions are off for this image.")
            self.character_description_status.show()
            self._update_pending_status()

    def _update_automatic_character_description(self):
        if not self._source_autofill_ready or self._description_autofill_state is None:
            return
        annotation=self._related_annotation_state()
        identity=self._character_view_service.description_identity(annotation,self.current_tags)
        if identity:
            engine=active_automation_engine(self.knowledge)
            if engine.conflicts_for_tags(self._staged_image_record(),self.current_tags):
                identity=None
        text,state=reconcile_character_description(
            self.prose.toPlainText(),self._description_autofill_state,identity,
        )
        self._description_autofill_state=state
        if text!=self.prose.toPlainText():
            self._applying_description_autofill=True
            try:
                cursor=self.prose.textCursor()
                cursor.beginEditBlock(); cursor.select(QTextCursor.Document)
                cursor.insertText(text); cursor.endEditBlock()
            finally:
                self._applying_description_autofill=False
        blocked=self.automatic_character_description.blockSignals(True)
        self.automatic_character_description.setChecked(bool(state["enabled"]))
        self.automatic_character_description.blockSignals(blocked)
        description_status=""
        if state["enabled"]:
            if state["sentence"]:
                description_status="Identity sentence staged from confirmed Character Knowledge. Save Changes keeps it; editing or undoing preserves your override."
                if identity and not identity[2]:description_status+=" Source wording needs a separately confirmed, reviewed source Tag."
            else:
                view=self._character_view_service.analyze(annotation,self.current_tags)
                identities=[fact for fact in view.facts if fact.role=="character_identity"]
                if not identities:description_status="Confirm a Character Identity Tag to stage an identity sentence. The checkbox alone does not identify a character."
                elif len(identities)>1:description_status="Multiple Character Identity Tags are confirmed. A single-character sentence is withheld; write the description manually or review the identities."
                elif identities[0].linked_kind!="character":description_status="Review this Character Identity Tag's exact reusable character link before automatic description."
                else:description_status="Identity description is held by image evidence or conflict checks. Review rejected, absent and conflicting facts; your prose is retained."
        self.character_description_status.setText(description_status)
        self.character_description_status.setVisible(bool(self.character_description_status.text()))

    def _stage_automatic_character_sources(self):
        if not self._source_autofill_ready or self._source_autofill_state is None:
            return
        annotation=self._related_annotation_state()
        view=self._character_view_service.analyze(annotation,self.current_tags)
        present={fact.tag_id for fact in view.facts if fact.role=="character_source"}
        enabled,excluded=self._source_autofill_state
        excluded={self.knowledge.resolve_tag_id(tag_id) or tag_id for tag_id in excluded}
        # A direct removal or Undo is a durable per-image automation override.
        excluded=(excluded|(self._source_autofill_seen_present-present))-present
        self._source_autofill_state=(enabled,frozenset(excluded))
        self._source_autofill_added.intersection_update(present)
        self._source_autofill_seen_present=present
        if not enabled or not view.identity_count:
            self.character_source_status.setText(
                "Automatic source additions are off for this image." if not enabled else
                "Confirm a Character Identity Tag first; then review its Character Source and reusable source Tag link."
            )
            return
        if self._related_tag_service is None:
            self._related_tag_service=RelatedTagSuggestionService(
                self.knowledge,self.categories,self.characters,
            )
        engine=active_automation_engine(self.knowledge)
        staged=self._staged_image_record()
        targets=self._character_view_service.automatic_source_tags(
            annotation,self.current_tags,
            consumer_policies=self._related_tag_service.consumer_policies,
            automation_engine=engine,
            conflict_check=lambda before,after:engine.conflict_warnings(staged,before,after),
        )
        blocked=self._ignored_tags()|self._confirmed_absent_tags()
        blocked_ids={entry.stable_id for entry in self._identity_service.plan(
            blocked,stored_ids=self._working_tag_identities,confirmation=False,
        ).entries if entry.resolved}
        targets=tuple(tag for tag in targets if tag.id not in present|excluded
                      and tag.id not in blocked_ids
                      and normalize_tag(tag.display) not in blocked)
        if not targets:
            self.character_source_status.setText(
                "Reviewed source Tags are already confirmed, or excluded for this image. Review the source facts and per-image exclusions."
                if present else
                "No source addition is eligible. Review the character's unambiguous Character Source link and the source's reviewed Tag link; automation rules, rejected/absent facts and per-image exclusions must also allow it."
            )
            return
        self.character_source_status.setText("Reviewed Character Source Tags staged. Save Changes keeps these image facts; Undo/removal preserves your per-image override.")
        added=self._stage_add_tags_batch(tag.display for tag in targets)
        added_keys={normalize_tag(value) for value in added}
        added_ids={tag.id for tag in targets if normalize_tag(tag.display) in added_keys}
        self._source_autofill_added.update(added_ids)
        self._source_autofill_seen_present.update(added_ids)

    def _canonical_confirmed_state(self,values):
        displays=[]; identities={}; seen=set()
        plan=self._identity_service.plan(
            values,stored_ids=self._working_tag_identities,confirmation=False,
        )
        for entry in plan.entries:
            display=entry.canonical_display if entry.resolved else entry.raw_value
            display_key=normalize_tag(display)
            if not display_key or display_key in seen:
                continue
            seen.add(display_key); displays.append(display)
            if entry.resolved:
                identities[display_key]=entry.stable_id
        return displays,identities

    def _raw_confirmed_value(self,display):
        target=normalize_tag(display)
        target_id=str(getattr(self,"_confirmed_display_identities",{}).get(target) or
            getattr(
                self.knowledge.find(display) if self.knowledge is not None else None,
                "id","",
            ) or ""
        )
        plan=self._identity_service.plan(
            self.current_tags,stored_ids=self._working_tag_identities,confirmation=False,
        )
        for entry in plan.entries:
            raw=entry.raw_value; raw_key=entry.normalized_value
            raw_id=entry.stable_id if entry.resolved else ""
            if (target_id and raw_id==target_id) or raw_key==target:
                return raw
        return display

    def _confirmed_identity_plan(self,*,confirmation=False):
        return self._identity_service.plan(
            self._current_confirmed_tags(),
            stored_ids=self._working_tag_identities,
            confirmation=confirmation,
        )

    @staticmethod
    def _identity_plan_details(plan):
        details=[]
        for entry in plan.blockers:
            details.append(entry.message or f"{entry.raw_value}: {entry.status}")
        if plan.new_entries:
            details.append(
                "New reusable identities: "
                +", ".join(entry.raw_value for entry in plan.new_entries)
            )
        return "\n".join(details)

    def _refresh_confirmed_identity_repair(self):
        if not hasattr(self,"identity_repair_panel"):
            return
        plan=self._retired_review_plan()
        self._retired_tag_count=sum(entry.status in {"tombstone","missing_identity"} for entry in plan.blockers)
        count=len(plan.blockers)+len(plan.new_entries)
        if not count:
            self.identity_repair_panel.hide()
            return
        entries=(*plan.blockers,*plan.new_entries)
        preview=", ".join(entry.raw_value for entry in entries[:3])
        if len(preview)>180:
            preview=preview[:177]+"\u2026"
        if count>3:
            preview+=f" and {count-3:,} more"
        self.identity_repair_status.setTextFormat(Qt.PlainText)
        self.identity_repair_status.setText(
            f"{count:,} confirmed SDXL Tag value"
            f"{'s need' if count!=1 else ' needs'} a durable reusable identity "
            "before this image can be saved. Repair creates only unambiguous "
            "genuinely new System Knowledge records; collisions, removed "
            "identities, and ignored values stop for review."
            f"\nNeeds review: {preview}"
        )
        self.identity_repair_status.setToolTip(self._identity_plan_details(plan))
        removed=[entry for entry in plan.blockers if entry.status in {"tombstone","missing_identity"}]
        self.identity_repair_panel.setTitle("Retired Tags on This Image" if removed else "Confirmed Tag Identity Repair")
        self.identity_repair_button.setText("Review Retired Tags…" if removed else "Review Identity Repair…")
        if removed:
            self.identity_repair_status.setText(
                f"{len(removed):,} removed or unavailable {'Tag needs' if len(removed)==1 else 'Tags need'} review in confirmed Tags or incoming evidence. "
                "Review why they were removed and choose replacements that match this image. "
                "Restoring a Tag is a separate global change.\nNeeds review: "+preview
            )
        self.identity_repair_panel.show()

    def _retired_review_plan(self):
        confirmed=self._confirmed_identity_plan()
        pending=self._identity_service.plan(
            (item["value"] for key,item in getattr(self,"_suggested",{}).items()
             if key[0]=="tag"),confirmation=True,
        )
        known={entry.normalized_value for entry in confirmed.entries}
        return type(confirmed)((*confirmed.entries,*(entry for entry in pending.blockers
            if entry.status in {"tombstone","missing_identity"}
            and entry.normalized_value not in known)))

    def _review_retired_entry(self,entry):
        pending=normalize_tag(entry.raw_value) not in self.current_tags.normalized_set()
        dialog=RetiredImageTagDialog(
            self.knowledge,self.categories,entry,
            recovery_record=removed_identity_record(self.knowledge,entry.stable_id),
            pending=pending,parent=self,
        )
        if dialog.exec()!=QDialog.Accepted:return False
        if dialog.action=="restore":
            plan=type(self._confirmed_identity_plan())((entry,))
            reviewed,_restored=self._review_removed_identity_plan(
                plan,source="image_editor_confirmation",
                continuation="Image changes remain pending until Save Changes.",
            )
            if reviewed is None:return False
            return bool(self._stage_add_tags_batch((entry.raw_value,))) if pending else True
        if dialog.action in {"replace","remove"}:
            return self._stage_retired_tag_replacement(
                entry,dialog.replacement_ids if dialog.action=="replace" else (),
                reviewed_manual=dialog.action=="replace",
            )
        return False

    def _remember_identity_resolution(self,resolution):
        self._working_tag_identities.update(
            resolution.identity_by_value
        )

    def _review_removed_identity_plan(self,plan,*,source,continuation):
        outcome=review_removed_identities(
            self,self._identity_service,plan,source=source,
            continuation=continuation,
            stored_ids=self._working_tag_identities,
            confirmation=True,
        )
        if outcome.cancelled or outcome.blocked:
            return None,outcome.restored_ids
        return outcome.plan,outcome.restored_ids

    def _review_confirmed_identity_repair(self):
        plan=self._retired_review_plan()
        for entry in tuple(plan.blockers):
            if entry.status not in {"tombstone","missing_identity"}:continue
            if not any(item.stable_id==entry.stable_id and item.blocked
                       for item in self._retired_review_plan().entries):continue
            if not self._review_retired_entry(entry):return False
        plan=self._confirmed_identity_plan(confirmation=True)
        plan,_restored=self._review_removed_identity_plan(
            plan,source="legacy_confirmed_tag_reconciliation",
            continuation=(
                "The restored identity will be linked to this image only when "
                "you separately save the staged image edits."
            ),
        )
        if plan is None:
            return False
        if plan.blockers:
            QMessageBox.warning(
                self,
                "Confirmed Tag Identity Needs Review",
                "Resolve these remaining System Knowledge or "
                "Automation decisions before saving this image:\n\n"
                +self._identity_plan_details(plan),
            )
            return False
        if not plan.new_entries:
            resolution=self._identity_service.require_existing(
                self._current_confirmed_tags(),
                stored_ids=self._working_tag_identities,
                confirmation=False,
            )
            self._remember_identity_resolution(resolution)
            self._refresh_tag_lists()
            return True
        values=", ".join(entry.raw_value for entry in plan.new_entries[:8])
        remainder=len(plan.new_entries)-8
        if remainder:
            values+=f" and {remainder:,} more"
        answer=QMessageBox.question(
            self,
            "Repair Confirmed Tag Identities?",
            f"Create {len(plan.new_entries):,} uncategorized reusable System "
            f"Knowledge record{'s' if len(plan.new_entries)!=1 else ''}?\n\n"
            f"{values}\n\n"
            "System Knowledge saves immediately with a recovery backup. The "
            "image, TXT, Metadata, raw evidence, and image file remain unchanged "
            "until you separately save image edits.",
            QMessageBox.Yes|QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer!=QMessageBox.Yes:
            return False
        try:
            resolution=self._identity_service.commit(
                plan,source="legacy_confirmed_tag_reconciliation",
            )
        except (OSError,RuntimeError,ValueError) as error:
            QMessageBox.critical(
                self,"Identity Repair Was Not Saved",str(error),
            )
            return False
        self._remember_identity_resolution(resolution)
        self._refresh_tag_lists()
        self.save_failure_details_button.hide()
        self.save_status.setText(
            f"Created {len(resolution.created_ids):,} reusable System Knowledge "
            "record(s) with a recovery backup. No image data changed."
        )
        self.save_status.setObjectName("mutedLabel")
        self.save_status.show()
        return True

    def _stage_retired_tag_replacement(self,entry,replacement_ids,*,reviewed_manual=False):
        if getattr(self,"_ai_busy",False):
            self.save_status.setText("Wait for preparation to finish before replacing retired Tags.")
            self.save_status.show()
            return False
        current_plan=self._retired_review_plan()
        raw=tuple(item.raw_value for item in current_plan.entries
                  if item.stable_id==entry.stable_id and item.blocked)
        if not raw:return False
        if replacement_ids and reviewed_manual is not True:
            QMessageBox.warning(self,"Historical Replacement Needs Review",retired_refinement_hold(self.knowledge,entry.stable_id))
            return False
        replacements=[]
        for tag_id in dict.fromkeys(replacement_ids):
            resolved=self.knowledge.resolve_tag_id(tag_id)
            tag=self.knowledge.tags.get(resolved or "")
            if tag is None or tag.id==entry.stable_id:
                QMessageBox.warning(self,"Replacement Needs Review","A selected replacement is no longer available. Choose its current Tag again.")
                return False
            replacements.append((tag.id,tag.display))
        candidate=self._related_annotation_state()
        try:replace_retired_image_tag(candidate,entry.stable_id,raw,replacements)
        except ValueError as error:
            QMessageBox.warning(self,"Replacement Needs Review",str(error)); return False
        fields=("current_tags","_working_tag_identities","pending_ignored_tags",
                "pending_restored_tags","pending_restored_absent_tags",
                "_working_characters","_working_objects","_working_contexts",
                "_pending_retired_tag_replacements")
        snapshot={name:copy.deepcopy(getattr(self,name)) for name in fields}
        raw_keys={normalize_tag(value) for value in raw}
        values=[display for _id,display in replacements]
        after=[value for value in self.current_tags if normalize_tag(value) not in raw_keys]+values
        self._working_tag_identities={key:value for key,value in self._working_tag_identities.items()
                                      if key not in raw_keys and value!=entry.stable_id}
        self._working_tag_identities.update({normalize_tag(display):tag_id for tag_id,display in replacements})
        if values and not self._prepare_tag_additions(values,prospective_after=after):
            for name,value in snapshot.items():setattr(self,name,value)
            return False
        if values:self._consume_prepared_conflict_replacements()
        for value in raw:
            if value in self.current_tags:
                self.current_tags.remove(value)
            else:
                self.pending_ignored_tags[normalize_tag(value)]=value
            self.pending_restored_tags.discard(normalize_tag(value))
            self.pending_restored_absent_tags.discard(normalize_tag(value))
        for value in values:
            self._restore_ignore_for_add(value); self.current_tags.add(value)
        self._pending_retired_tag_replacements[entry.stable_id]=(raw,tuple(replacements))
        self._working_characters=copy.deepcopy(candidate.characters)
        self._working_objects=copy.deepcopy(candidate.objects)
        self._working_contexts=copy.deepcopy(candidate.contexts)
        self._tag_history.append(("retired_tag",snapshot,None,-1))
        self._refresh_tag_lists(); self._refresh_suggestions()
        self.save_failure_details_button.hide()
        self.save_status.setText("Tag changes are pending until Save Changes. Undo Tag Change restores the previous Tags.")
        self.save_status.setObjectName("mutedLabel"); self.save_status.show()
        return True

    def _apply_retired_tag_replacements(self,annotation):
        for stable_id,(raw,replacements) in self._pending_retired_tag_replacements.items():
            replace_retired_image_tag(annotation,stable_id,raw,replacements)

    def _palette_conflicts(self,ordered):
        details=getattr(self.automation_engine,"introduced_conflicts",None)
        engine=getattr(self.automation_engine,"conflicts",None)
        if not callable(details) or engine is None or self.knowledge is None:
            return {}
        current=TagCollection(ordered).normalized_set()
        candidates={}
        for rule in getattr(engine,"rules",()):
            for raw_value in getattr(rule,"tags",()):
                record=self.knowledge.find(raw_value)
                if record is None or normalize_tag(record.display) in current:
                    continue
                candidates[normalize_tag(record.display)]=record.display
        result={}
        for key,value in candidates.items():
            try:
                matches=details(
                    self.record,ordered,[*ordered,value],
                )
            except (OSError,RuntimeError,ValueError):
                matches=()
            messages=tuple(
                dict.fromkeys(str(match.message) for match in matches)
            )
            if messages:
                result[key]=messages
        return result

    def _stage_ownership_acknowledgement(self):
        selected=self.ownership_ack_choices.currentData()
        engine=getattr(self.automation_engine,"conflicts",None)
        if selected is None or engine is None:return
        if not isinstance(self.record.annotation.source.get("ownership_acknowledgements",[]),list):
            self.ownership_ack_status.setText("Existing ownership decision data has an unsupported shape; it is preserved. A new acknowledgement cannot be applied.")
            return
        match=next((item for item in self._conflicts_for_tags(self._current_confirmed_tags())
                    if item.ambiguous and len(item.found_tags)==2
                    and (item.rule.id,tuple(sorted(normalize_tag(v) for v in item.found_tags)))==selected),None)
        if match is None:
            self.ownership_ack_status.setText("This check changed. Review the current Tag pair again.")
            self._refresh_tag_lists();return
        if QMessageBox.question(self,"Review different subjects",
                "Both apply to different subjects: "+", ".join(match.found_tags)+
                "\n\nThis acknowledges only this ownership check. It does not assign owners, change Tags, or override a same-owner conflict. Staged until Save Changes.",
                QMessageBox.Ok|QMessageBox.Cancel,QMessageBox.Cancel)!=QMessageBox.Ok:return
        try:acknowledgement=engine.ownership_acknowledgement(self._staged_image_record(),match)
        except ValueError as error:
            self.ownership_ack_status.setText(str(error));self.ownership_ack_status.show();return
        self._tag_history.append(("ownership_acknowledgement",copy.deepcopy(self._working_ownership_acknowledgements),None,None))
        self._working_ownership_acknowledgements=[item for item in self._working_ownership_acknowledgements
                if not isinstance(item,dict) or item.get("version")!=1 or (item.get("rule_id"),item.get("pair"))!=(acknowledgement["rule_id"],acknowledgement["pair"])]
        self._working_ownership_acknowledgements.append(acknowledgement)
        self._refresh_tag_lists();self._refresh_suggestions()

    def _conflicts_for_tags(self,values):
        resolver=getattr(self.automation_engine,"conflicts_for_tags",None)
        if not callable(resolver):
            return ()
        try:
            return tuple(resolver(self._staged_image_record(),values))
        except (OSError,RuntimeError,ValueError):
            return ()

    @staticmethod
    def _conflict_index(matches):
        by_tag={}
        for match in matches:
            message=str(getattr(match,"message","") or "").strip()
            for value in getattr(match,"found_tags",()):
                key=normalize_tag(value)
                if key and message:
                    by_tag.setdefault(key,[]).append(message)
        return {
            key:tuple(dict.fromkeys(messages))
            for key,messages in by_tag.items()
        }

    @staticmethod
    def _conflict_review_summary(matches,extra_conflicts=0):
        ownership=sum(bool(getattr(match,"ambiguous",False)) for match in matches)
        definite=len(matches)-ownership+extra_conflicts
        parts=[]
        if definite:parts.append(f"{definite:,} conflict group(s)")
        if ownership:parts.append(f"{ownership:,} ownership check(s)")
        return "; ".join(parts),definite,ownership

    @staticmethod
    def _conflict_state_indicators(matches):
        result={}
        # Definite conflicts take priority when a Tag is in both kinds of group.
        for match in sorted(matches,key=lambda item:not bool(getattr(item,"ambiguous",False))):
            ambiguous=bool(getattr(match,"ambiguous",False))
            indicator=(
                ("Subject ownership unclear",STATUS_COLORS["blue"],"solid","i")
                if ambiguous else
                ("Conflict needing review",STATUS_COLORS["orange"],"dashed")
            )
            for value in match.found_tags:result[normalize_tag(value)]=indicator
        return result

    @staticmethod
    def _set_conflict_filter_labels(control,total,conflict_count):
        if control.property("sharedTagFilter"):
            return
        clear_count=max(0,int(total)-int(conflict_count))
        labels=(
            f"All ({int(total):,})",
            f"No Review Flags ({clear_count:,})",
            f"Conflicts / Ownership ({int(conflict_count):,})",
        )
        for index,label in enumerate(labels):
            control.setItemText(index,label)

    def _refresh_confirmed_tag_surface(self,ordered=None):
        if not hasattr(self,"tag_chips"):
            return
        ordered=list(ordered if ordered is not None else self._current_confirmed_tags())
        selected=self.tag_chips.selected_values()
        scroll=self.tag_chips.scroll.verticalScrollBar().value()
        focus=QApplication.focusWidget()
        focused_value=next((
            chip.value for chip in self.tag_chips._chips
            if focus is chip or (focus is not None and chip.isAncestorOf(focus))
        ),"")
        matches=self._conflicts_for_tags(ordered)
        choices=[item for item in matches if bool(getattr(item,"ambiguous",False)) and len(item.found_tags)==2]
        if hasattr(self,"ownership_ack_choices"):
            previous=self.ownership_ack_choices.currentData()
            self.ownership_ack_choices.clear()
            for item in choices:
                self.ownership_ack_choices.addItem(", ".join(item.found_tags)+" — "+(item.rule.label or item.rule.id),
                    (item.rule.id,tuple(sorted(normalize_tag(v) for v in item.found_tags))))
            index=self.ownership_ack_choices.findData(previous)
            if index>=0:self.ownership_ack_choices.setCurrentIndex(index)
            self.ownership_ack_panel.setVisible(bool(choices))
            self.ownership_ack_choices.setVisible(bool(choices))
            self.ownership_ack_button.setVisible(bool(choices))
            saved=self.record.annotation.source.get("ownership_acknowledgements",[])
            supported=isinstance(saved,list)
            self.ownership_ack_button.setEnabled(supported)
            staged=supported and self._working_ownership_acknowledgements!=saved
            self.ownership_ack_status.setText("Different-subject acknowledgement staged; Save Changes keeps it. Undo restores review." if staged else
                ("Saved different-subject acknowledgements are rechecked against current Tags, rules and owners." if saved else ""))
            if not supported:self.ownership_ack_status.setText("Existing ownership decision data has an unsupported shape; preserved unchanged. New acknowledgement is unavailable, but unrelated image edits may still be saved.")
            self.ownership_ack_status.setVisible(bool(self.ownership_ack_status.text()))
        conflict_index=self._conflict_index(matches)
        match_indicators=self._conflict_state_indicators(matches)
        conflict_keys=set(conflict_index)
        rating_detail=self._rating_detail_for_tags(ordered)
        rating_keys=set()
        if rating_detail is not None:
            rating_message=" ".join(rating_detail.messages)
            for value in (
                *rating_detail.declared_ratings,
                *(display for display,_rating in rating_detail.implied_tags),
            ):
                key=normalize_tag(value)
                rating_keys.add(key)
                conflict_keys.add(key)
                conflict_index.setdefault(key,tuple())
                conflict_index[key]=tuple(dict.fromkeys((
                    *conflict_index[key],rating_message,
                )))
        mode=str(self.confirmed_tag_filter.currentData() or "all")
        shown=[
            value for value in ordered
            if self.tag_palette.matches_view(value,conflict=normalize_tag(value) in conflict_keys) and (
                mode=="all"
                or (mode=="conflicts" and normalize_tag(value) in conflict_keys)
                or (mode=="clear" and normalize_tag(value) not in conflict_keys)
            )
        ]
        query=(
            self.tag_palette.search.text().strip().casefold()
            if hasattr(self,"tag_palette") else ""
        )
        if query:
            filtered=[]
            for value in shown:
                record=self.knowledge.find(value) if self.knowledge is not None else None
                names=self.knowledge.identity_names(record) if record is not None else (value,)
                category=(
                    self.categories.path_label(record.category)
                    if record is not None and self.categories is not None else ""
                )
                if (
                    any(query in str(name).casefold() for name in names)
                    or query in category.casefold()
                ):
                    filtered.append(value)
            shown=filtered
        placements=context_placements(self._staged_context_annotation())
        tooltips={}
        state_indicators={}
        for value in shown:
            key=normalize_tag(value)
            details=list(conflict_index.get(key,()))
            placement=placements.get(key)
            if placement is not None:
                details.append(
                    f"Image context: {placement.label}. Use Set Context to move it."
                )
            tooltips[value]="\n\n".join(details)
            if key in conflict_keys:
                indicator=match_indicators.get(key,(
                    "Conflict needing review",STATUS_COLORS["orange"],"dashed",
                ))
                if key in rating_keys:
                    indicator=("Conflict needing review",STATUS_COLORS["orange"],"dashed")
                label=indicator[0]
                if placement is not None:
                    label+=f" in {placement.label}"
                state_indicators[value]=(label,*indicator[1:])
            elif placement is not None:
                state_indicators[value]=(
                    placement.label,STATUS_COLORS["blue"],"solid","\u21b3",
                )
        self.tag_chips.set_tags(
            shown,self.knowledge,self.categories,self._favorite_tags,
            stable_ids=getattr(self,"_confirmed_display_identities",{}),
            tooltips=tooltips,
            state_indicators=state_indicators,
        )
        self.tag_chips.select_values(selected)
        QTimer.singleShot(
            0,lambda value=scroll:
            self.tag_chips.scroll.verticalScrollBar().setValue(value)
        )
        if focused_value and self.tag_chips.index_of(focused_value)>=0:
            QTimer.singleShot(
                0,lambda value=focused_value:
                self.tag_chips.focus_tag(value)
            )
        self._set_conflict_filter_labels(
            self.confirmed_tag_filter,len(ordered),len(conflict_keys)
        )
        conflict_messages=[str(match.message) for match in matches]
        rating_group_is_new=bool(
            rating_detail is not None
            and not any(
                rating_keys=={
                    normalize_tag(value)
                    for value in getattr(match,"found_tags",())
                    if normalize_tag(value)
                }
                for match in matches
            )
        )
        if rating_detail is not None:
            conflict_messages.extend(rating_detail.messages)
        summary,definite_count,ownership_count=self._conflict_review_summary(
            matches,int(rating_group_is_new),
        )
        if conflict_messages:
            details=" ".join(dict.fromkeys(conflict_messages))
            rating_prefix="Rating conflict; " if rating_detail is not None else ""
            self.confirmed_conflict_status.setText(
                f"{'⚠' if definite_count else 'i'} {rating_prefix}{summary}, "
                f"{len(conflict_keys):,} Tags."
            )
        else:
            details=""
            self.confirmed_conflict_status.setText("No confirmed conflicts.")
        self.confirmed_conflict_status.setToolTip(details)
        self.confirmed_conflict_status.setAccessibleDescription(details)
        self.confirmed_conflict_details.setText(details)
        self.confirmed_conflict_details.setVisible(bool(details) and mode=="conflicts")
        self.confirmed_conflict_status.setAccessibleName(
            self.confirmed_conflict_status.text()
        )
        self._confirmed_conflict_count=definite_count
        self._confirmed_ownership_count=ownership_count
        self.show_confirmed_conflicts_button.setText(
            "Show Review Details" if ownership_count else "Show Conflicts"
        )
        self.show_confirmed_conflicts_button.setVisible(bool(conflict_messages))
        self._refresh_rating_review(rating_detail)
        self._update_tag_manager_tab_status()
        self._update_attention_animation()

    def _refresh_rating_review(self,detail=_RATING_DETAIL_UNSET):
        if not hasattr(self,"rating_review_panel"):
            return
        if detail is _RATING_DETAIL_UNSET:
            detail=self._rating_detail_for_tags(
                self._current_confirmed_tags()
            )
        self._rating_conflict_detail=detail
        self.rating_review_panel.setVisible(detail is not None)
        if detail is None:
            return
        declared=(
            ", ".join(value.title() for value in detail.declared_ratings)
            or "No confirmed rating"
        )
        required=detail.required_rating.title()
        if detail.implied_tags:
            sources=", ".join(
                display
                for display,rating in detail.implied_tags
            )
            plural=len(detail.implied_tags)!=1
            self.rating_review_summary.setText(
                f"{declared} conflicts with "
                f"{'reviewed' if plural else 'the reviewed'} {required} "
                f"rating rule{'s' if plural else ''} for {sources}."
            )
            self.rating_review_summary.setAccessibleDescription(
                f"Use {required} Rating if these Tags are correct, or review "
                f"the implicated Tags: {sources}."
            )
            self.show_rating_tags_button.setText(
                f"Review {len(detail.implied_tags):,} Tag"
                +("s" if plural else "")
            )
            self.show_rating_tags_button.setToolTip(
                "Show and select the confirmed Tags behind this rating: "
                +sources+". Remove a Tag only if it is wrong for this image."
            )
            self.show_rating_tags_button.show()
        else:
            self.rating_review_summary.setText(
                f"{declared} are both confirmed. Keep the strongest applicable "
                f"rating: {required}."
            )
            self.rating_review_summary.setAccessibleDescription(
                "More than one rating Tag is confirmed. Use the strongest "
                "applicable rating or remove an incorrect rating Tag."
            )
            self.show_rating_tags_button.hide()
        self.use_required_rating_button.setText(f"Use {required} Rating")
        self.use_required_rating_button.setAccessibleName(
            f"Use {required} as the confirmed image rating"
        )

    def _rating_detail_for_tags(self,values):
        if self.knowledge is None:
            return None
        annotation=ImageAnnotation(
            str(self.record.image_path),sdxl_tags=list(values),
        )
        return rating_conflict_detail(
            annotation,self.knowledge,self.categories,
        )

    def _show_rating_conflict_tags(self):
        detail=getattr(self,"_rating_conflict_detail",None)
        if detail is None:
            return
        self._reset_shared_tag_filters(conflicts=True)
        values=[display for display,_rating in detail.implied_tags]
        self.tag_chips.select_values(values)
        if values:
            self.tag_chips.focus_tag(values[0])

    def _stage_required_rating(self):
        detail=getattr(self,"_rating_conflict_detail",None)
        required=str(getattr(detail,"required_rating","") or "")
        if required not in RATING_ORDER:
            return
        ordered=self.current_tags.to_list()
        rating_rows=[]
        for index,value in enumerate(ordered):
            key=normalize_tag(value)
            record=self.knowledge.find(value) if self.knowledge is not None else None
            if key not in RATING_ORDER or (
                record is not None and record.category!="rating"
            ):
                continue
            if key==required:
                continue
            rating_rows.append((
                value,index,key in self.pending_restored_tags,
                key in self.pending_restored_absent_tags,
            ))
        prospective=[
            value for value in ordered
            if normalize_tag(value) not in RATING_ORDER
            or normalize_tag(value)==required
        ]
        if required not in {normalize_tag(value) for value in prospective}:
            prospective.append(required)
        prepared=self._prepare_tag_additions(
            (required,),prospective_after=prospective,
        )
        if not prepared:
            return
        replacements=list(self._consume_prepared_conflict_replacements())
        for row in rating_rows:
            if self.current_tags.remove(row[0]):
                self.pending_restored_tags.discard(normalize_tag(row[0]))
                self.pending_restored_absent_tags.discard(normalize_tag(row[0]))
                replacements.append(row)
        value=prepared[0]
        state=self._restore_ignore_for_add(value)
        state["added"]=self.current_tags.add(value)
        self._tag_history.append((
            "replace_conflicts",
            {"additions":((value,state),),"replacements":tuple(replacements)},
            None,-1,
        ))
        self._remember_recent_tag(value)
        self._refresh_tag_lists(refresh_related=False)
        self._refresh_suggestions()
        self.tag_chips.focus_tag(value)

    def _attention_tab_targets(self):
        targets=[]
        pending_tags=int(getattr(self,"_pending_tag_count",0))
        pending_descriptions=int(getattr(self,"_pending_description_count",0))
        tag_attention=(
            pending_tags
            or int(getattr(self,"_confirmed_conflict_count",0))
            or int(getattr(self,"_related_suggestion_count",0))
        )
        if tag_attention:
            index=self.tabs.indexOf(self.image_data_tab)
            if index>=0:targets.append((self.tabs,index))
        if pending_descriptions:
            index=self.tabs.indexOf(self.description_tab)
            if index>=0:targets.append((self.tabs,index))
        if int(getattr(self,"_character_review_count",0)):
            index=self.tabs.indexOf(self.character_tab)
            if index>=0:targets.append((self.tabs,index))
        if (
            pending_tags
            or int(getattr(self,"_related_suggestion_count",0))
        ) and hasattr(self,"pending_tags_group"):
            index=self.tag_activity_tabs.indexOf(self.pending_tags_page)
            if index>=0:targets.append((self.tag_activity_tabs,index))
        return targets

    def _update_attention_animation(self):
        targets=self._attention_tab_targets()
        normal=self.palette().color(QPalette.Text)
        for control in (self.tabs,getattr(self,"tag_activity_tabs",None)):
            if control is None:continue
            for index in range(control.count()):
                control.tabBar().setTabTextColor(index,normal)
        if targets:
            if self._attention_animation_enabled():
                if not self._attention_timer.isActive():
                    self._attention_timer.start()
                self._pulse_review_attention()
            else:
                self._attention_timer.stop()
                accent=self.palette().color(QPalette.Highlight)
                for control,index in targets:
                    control.tabBar().setTabTextColor(index,accent)
                self._update_confirmed_tag_actions()
        else:
            self._attention_timer.stop()

    def _attention_animation_enabled(self):
        return bool(
            self.style().styleHint(QStyle.SH_Widget_Animate,None,self)
        )

    def _pulse_review_attention(self):
        self._attention_phase=not self._attention_phase
        accent=self.palette().color(QPalette.Highlight)
        color=accent.lighter(118) if self._attention_phase else accent.darker(108)
        for control,index in self._attention_tab_targets():
            control.tabBar().setTabTextColor(index,color)
        self._update_confirmed_tag_actions()

    def _show_confirmed_conflicts(self):
        self._reset_shared_tag_filters(conflicts=True)
        index=self.confirmed_tag_filter.findData("conflicts")
        if index>=0:
            self.confirmed_tag_filter.setCurrentIndex(index)
        first=next(
            (
                chip.value for chip in self.tag_chips._chips
                if chip._state_indicator[0]
            ),
            "",
        )
        if first:
            self.tag_chips.focus_tag(first)

    def _update_tag_manager_tab_status(self):
        if not hasattr(self,"tabs") or not hasattr(self,"image_data_tab"):
            return
        index=self.tabs.indexOf(self.image_data_tab)
        if index<0:
            return
        parts=[]
        retired_count=int(getattr(self,"_retired_tag_count",0))
        if retired_count:parts.append(f"{retired_count:,} retired")
        pending_count=int(getattr(self,"_pending_tag_count",0))
        if pending_count:
            parts.append(f"{pending_count:,} to review")
        if self._confirmed_conflict_count:
            parts.append(
                f"{self._confirmed_conflict_count:,} conflict"
                f"{'s' if self._confirmed_conflict_count!=1 else ''}"
            )
        ownership_count=int(getattr(self,"_confirmed_ownership_count",0))
        if ownership_count:
            parts.append(f"{ownership_count:,} ownership check{'s' if ownership_count!=1 else ''}")
        if self._related_suggestion_count:
            parts.append(f"{self._related_suggestion_count:,} related")
        context_count=len(self._working_contexts)
        if context_count:
            parts.append(f"{context_count:,} context{'s' if context_count!=1 else ''}")
        self.tabs.setTabText(
            index,"Image Knowledge"+(f" ({'; '.join(parts)})" if parts else "")
        )
        self.tabs.setTabIcon(
            index,
            self.style().standardIcon(QStyle.SP_MessageBoxWarning)
            if self._confirmed_conflict_count or retired_count else QIcon(),
        )
        tooltip=[]
        if ownership_count:
            tooltip.append(
                f"{ownership_count:,} subject ownership check(s); "
                "these Tags may belong to different subjects and are not proven conflicts"
            )
        if self._confirmed_conflict_count:
            tooltip.append(
                f"{self._confirmed_conflict_count:,} diagnostic conflict "
                "group(s) need review"
            )
        if self._related_suggestion_count:
            tooltip.append(
                f"{self._related_suggestion_count:,} related SDXL Tag "
                "suggestion(s) need review"
            )
        if context_count:
            tooltip.append(
                f"{context_count:,} secondary image context"
                f"{'s' if context_count!=1 else ''}"
            )
        self.tabs.setTabToolTip(
            index,
            "; ".join(
                (
                    *(
                        [
                            f"{pending_count:,} pending SDXL Tag finding(s)"
                        ]
                        if pending_count else []
                    ),
                    *tooltip,
                )
            )
            or "Manage confirmed and pending SDXL Tags on this image",
        )

    def _update_confirmed_tag_actions(self,*_):
        if not hasattr(self,"tag_chips"):
            return
        selected=self.tag_chips.selected_values()
        count=len(selected)
        self.confirmed_tag_selection_status.setText(
            f"{count:,} confirmed SDXL Tag{'s' if count!=1 else ''} selected."
            if count else ""
        )
        self.remove_selected_tags_button.setEnabled(bool(count))
        self.remove_ignore_selected_tags_button.setEnabled(bool(count))
        self.set_context_button.setEnabled(bool(count))
        self.refine_selected_tag_button.setEnabled(count==1)
        self.remove_confirmed_from_training_txt_button.setEnabled(
            self.source_correction_available and count==1
            and bool(self._training_txt_source_value(selected[0]))
        )
        self.remove_confirmed_from_metadata_button.setEnabled(
            self.source_correction_available and count==1
            and bool(self._metadata_source_value(selected[0],"tag"))
        )
        for button in (
            self.remove_confirmed_from_training_txt_button,
            self.remove_confirmed_from_metadata_button,
        ):
            button.setVisible(button.isEnabled())

    def _review_selected_tag_details(self):
        selected=self.tag_chips.selected_values()
        if len(selected)==1:self._review_active_tag_details(self._raw_confirmed_value(selected[0]))

    def _active_refinement_snapshot(self,value):
        if self._edit_snapshot.assess(self.record).stale:
            raise ValueError("The saved image state changed. Reload before reviewing details.")
        plan=self._identity_service.plan((value,),stored_ids=self._working_tag_identities,confirmation=False)
        if not plan.resolved or len(plan.entries)!=1:raise ValueError("Source identity needs review.")
        entry=plan.entries[0]
        service=self._related_tag_service
        if service is None or service.semantic_relationships is None:
            raise ValueError("Reviewed relationship authority is unavailable.")
        # Reuse the native service's stale-file checks, without constructing a store.
        service.semantic_relationships._verify_authority()
        for store in getattr(service.consumer_policies,"stores",()):
            current=store.path.read_bytes() if store.path.exists() else None
            if current!=store._loaded_bytes:raise ValueError("Consumer policy sources changed. Reload before reviewing details.")
        relationships=service.semantic_relationships.effective_relationships
        annotation=self._related_annotation_state()
        annotation.sdxl_tags=self._related_image_wide_tags(annotation)
        occurrences=image_tag_occurrences(annotation,value)
        if not occurrences:raise ValueError("No confirmed structured occurrence is available. Keep the pending evidence for review.")
        # Include full exact image state and graph: a new competing compound or
        # changed owner/negative/source fact invalidates the review too.
        inputs=active_refinement_inputs(self.knowledge,self.categories,relationships,entry.stable_id)
        snapshot=capture_live_refinement(self.knowledge,self.categories,service.semantic_relationships,
            service.consumer_policies,annotation,source_id=entry.stable_id,source_value=value,
            extra_dependencies=dict(image_path=str(self.record.image_path),
                negative=sorted(self._confirmed_absent_tags()),ignored=sorted(self._ignored_tags()),
                rejected={tid:self.automation_engine.is_evidence_rejected(display) for tid,display in inputs['displays'].items()},
                archive=getattr(self.knowledge.removed_archive_for_tag(entry.stable_id),'to_dict',lambda:None)()))
        return entry,snapshot.inputs(),snapshot.occurrences,snapshot

    def _review_active_tag_details(self,value):
        if getattr(self,"_ai_busy",False):return False
        try:
            entry,inputs,occurrences,snapshot=self._active_refinement_snapshot(value)
            # Display union only; the exact chosen scope is re-planned before staging.
            choices={}
            for occurrence in occurrences:
                preview=snapshot.plan(occurrence)
                for choice in preview.choices:
                    if choice.requested_id not in choices or choice.target_id:choices[choice.requested_id]=choice
            dialog=RetiredImageTagDialog(self.knowledge,self.categories,entry,parent=self,
                refinement_choices=tuple(choices.values()),occurrences=occurrences)
            if dialog.exec()!=QDialog.Accepted or dialog.action in {'keep','skip'}:return False
            if dialog.action!='add_specific' or len(dialog.replacement_ids)!=1:return False
            occurrence=dialog.occurrence.currentData()
            if occurrence not in occurrences:return False
            selected=dialog.replacement_ids[0]
            reviewed=snapshot.plan(occurrence,selected_id=selected,reviewed=True)
            if not reviewed.changed and not reviewed.holds:return False
            if reviewed.holds:raise ValueError(', '.join(reviewed.holds))
            choice=next(c for c in reviewed.choices if c.requested_id==selected)
            relationships={r.id:r for r in inputs['relationships']}
            decisions=tuple(self._related_tag_service._policy_decision(
                target_id=choice.target_id,relationship_kind=relationships[rid].kind,
                relationship_id=rid,scope=occurrence.owner_kind,owner_kind=occurrence.owner_kind)
                for rid in choice.relationship_ids)
            if not decisions or not all(d.permits_related_review for d in decisions):
                raise ValueError("Consumer policy does not permit this detail review.")
            def revalidate():
                current_decisions=tuple(self._related_tag_service._policy_decision(
                    target_id=choice.target_id,relationship_kind=relationships[rid].kind,
                    relationship_id=rid,scope=occurrence.owner_kind,owner_kind=occurrence.owner_kind)
                    for rid in choice.relationship_ids)
                if current_decisions!=decisions:raise ValueError("Consumer policy changed. Review this fact again.")
                if self._active_refinement_snapshot(value)[3]!=snapshot:
                    raise ValueError("The image or System Knowledge changed. Review this fact again.")
            revalidate()
            suggestion=RelatedTagSuggestion(choice.target_id,choice.target_display,entry.stable_id,value,
                (entry.stable_id,choice.target_id),(value,choice.target_display),('reviewed_detail',),
                owner_kind=occurrence.owner_kind,owner_id=occurrence.owner_id,owner_group=occurrence.owner_group,
                relationship_ids=choice.relationship_ids,policy_decisions=decisions)
            added=self._stage_add_related_suggestions((suggestion,),revalidate=revalidate)
            if added:self._refresh_tag_lists(refresh_related=False);self._refresh_suggestions()
            return bool(added)
        except (OSError,RuntimeError,ValueError) as error:
            QMessageBox.warning(self,"Tag Details Need Review",str(error));return False

    def _staged_context_annotation(self):
        annotation=self._related_annotation_state()
        annotation.tag_identities=dict(self._working_tag_identities)
        return annotation

    def _set_selected_context(self):
        selected=tuple(
            self._raw_confirmed_value(value)
            for value in self.tag_chips.selected_values()
        )
        if not selected:
            return
        annotation=self._staged_context_annotation()
        placements=context_placements(annotation)
        context_ids={
            placements[normalize_tag(value)].context_id
            for value in selected
            if normalize_tag(value) in placements
        }
        current_context_id=(
            next(iter(context_ids))
            if len(context_ids)==1 and all(
                normalize_tag(value) in placements for value in selected
            ) else ""
        )
        dialog=ImageContextAssignmentDialog(
            annotation,selected,self.knowledge,self.categories,
            current_context_id=current_context_id,parent=self,
        )
        if dialog.exec()!=QDialog.Accepted:
            return
        assignment=dialog.assignment()
        context_id=str(assignment.get("context_id") or "")
        if context_id==ImageContextAssignmentDialog.NEW_CONTEXT:
            context=annotation.add_context(
                kind=str(assignment.get("kind") or CONTEXT_KINDS[0]),
                owner_kind=str(assignment.get("owner_kind") or ""),
                owner_id=str(assignment.get("owner_id") or ""),
                owner_label=str(assignment.get("owner_label") or ""),
            )
            context_id=context.id
        try:
            changed=move_image_facts(
                annotation,selected,context_id=context_id or None,
            )
        except ImageContextError as exc:
            QMessageBox.warning(self,"Image context was not changed",str(exc))
            return
        if not changed:
            return
        self._tag_history.append((
            "set_context",
            tuple(item.to_dict() for item in self._working_contexts),
            None,-1,
        ))
        self._working_contexts=copy.deepcopy(annotation.contexts)
        self._refresh_tag_lists(); self._refresh_suggestions()
        self._update_pending_status()

    def _remove_selected_confirmed_tags(self):
        selected=self.tag_chips.selected_values()
        if not selected:
            return
        removed=[]
        ordered=self.current_tags.to_list()
        for display_value in selected:
            value=self._raw_confirmed_value(display_value)
            index=next(
                (index for index,item in enumerate(ordered)
                 if normalize_tag(item)==normalize_tag(value)),
                len(ordered),
            )
            if self.current_tags.remove(value):
                key=normalize_tag(value)
                restored=key in self.pending_restored_tags
                absent_restored=key in self.pending_restored_absent_tags
                self.pending_restored_tags.discard(key)
                self.pending_restored_absent_tags.discard(key)
                removed.append((value,index,restored,absent_restored))
        if removed:
            self._tag_history.append(("remove_batch",tuple(removed),None,-1))
            self._refresh_tag_lists(); self._refresh_suggestions()

    def _remove_ignore_selected_confirmed_tags(self):
        selected=self.tag_chips.selected_values()
        if not selected:
            return
        removed=[]
        ordered=self.current_tags.to_list()
        for display_value in selected:
            value=self._raw_confirmed_value(display_value)
            index=next(
                (index for index,item in enumerate(ordered)
                 if normalize_tag(item)==normalize_tag(value)),
                len(ordered),
            )
            if not self.current_tags.remove(value):
                continue
            key=normalize_tag(value)
            previous=self.pending_ignored_tags.get(key)
            restored=key in self.pending_restored_tags
            absent_restored=key in self.pending_restored_absent_tags
            self.pending_restored_tags.discard(key)
            self.pending_restored_absent_tags.discard(key)
            self.pending_ignored_tags[key]=value
            removed.append(
                (value,index,previous,restored,absent_restored)
            )
        if removed:
            self._tag_history.append(
                ("remove_ignore_batch",tuple(removed),None,-1)
            )
            self._refresh_tag_lists(); self._refresh_suggestions()

    def _add_dropped_pending_tags(self,values):
        """Stage pending or known-palette tags dropped on the confirmed region."""
        rows=getattr(self,"_related_review_rows",{})
        related=tuple(rows[normalize_tag(value)] for value in values
                      if normalize_tag(value) in rows and rows[normalize_tag(value)].eligible)
        pending=tuple(value for value in values
                      if normalize_tag(value) not in rows
                      and not str(value).startswith("related-review:"))
        self._add_selected_review_tags(pending=pending,related=related)

    def _create_reusable_tag(self):
        initial=self.new_tag.text().strip() if hasattr(self,"new_tag") else ""
        if initial:
            self._request_reusable_tag_creation(initial)
        else:
            self.create_tag_requested.emit("",self)

    def _request_reusable_tag_creation(self,name):
        name=str(name).strip()
        if not name:
            return
        plan=self._identity_service.plan((name,),confirmation=True)
        plan,restored_ids=self._review_removed_identity_plan(
            plan,source="image_editor_confirmation",
            continuation=(
                "The restored tag will then be staged on this image; the image "
                "itself remains unchanged until Save Changes."
            ),
        )
        if plan is None:
            return
        if plan.blockers:
            QMessageBox.warning(
                self,
                "Reusable Tag Needs Review",
                "Nothing was created.\n\n"+self._identity_plan_details(plan),
            )
            return
        entry=plan.entries[0] if plan.entries else None
        existing=(
            self.knowledge.tags.get(entry.stable_id)
            if entry is not None and entry.resolved else None
        )
        if existing is not None:
            if existing.id in restored_ids:
                if self._stage_add_tag(existing.display):
                    self._refresh_tag_lists(); self._refresh_suggestions()
                return
            dialog=QMessageBox(self)
            dialog.setWindowTitle("Use Existing Tag?")
            dialog.setIcon(QMessageBox.Information)
            dialog.setText(
                f'"{name}" already identifies the reusable SDXL Tag '
                f'"{existing.display}".'
            )
            add=dialog.addButton("Add Existing Tag",QMessageBox.AcceptRole)
            dialog.addButton(QMessageBox.Cancel)
            dialog.setDefaultButton(add)
            dialog.exec()
            if dialog.clickedButton() is add and self._stage_add_tag(existing.display):
                self._refresh_tag_lists(); self._refresh_suggestions()
            return
        self.create_tag_requested.emit(name,self)

    def complete_system_tag_creation(self,stable_id):
        """Stage the saved reusable Tag, then refresh the originating editor once."""
        record=(
            self.knowledge.tags.get(str(stable_id))
            if self.knowledge is not None else None
        )
        if record is None:
            return False
        self._refresh_automation_authority()
        if normalize_tag(record.display) in self.current_tags.normalized_set():
            self.refresh_system_knowledge()
            self.tabs.setCurrentWidget(self.image_data_tab)
            return True
        if self._stage_add_tag(record.display):
            self.refresh_system_knowledge()
            self.tabs.setCurrentWidget(self.image_data_tab)
            return True
        self.refresh_system_knowledge()
        return False

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item=layout.takeAt(0)
            widget=item.widget()
            child_layout=item.layout()
            if child_layout is not None:ImageDataEditor._clear_layout(child_layout)
            if widget is not None:widget.deleteLater()

    @staticmethod
    def _related_tag_tooltip(suggestion):
        details=[
            f"Suggested because: {suggestion.path_label} "
            f"({suggestion.relationship_label})",
        ]
        if suggestion.scope!="Image-wide":
            details.append(f"Applies to: {suggestion.scope}")
        if suggestion.blocked_reason:
            details.append(f"Unavailable: {suggestion.blocked_reason}")
        return "\n".join(details)

    @staticmethod
    def _related_tag_technical_details(suggestion):
        return "\n".join((
            f"Related Tag: {suggestion.display}",
            f"Supported by: {suggestion.supporting_display}",
            f"Path: {suggestion.path_label}",
            f"Relationship: {suggestion.relationship_label}",
            f"Support: {suggestion.support_label}",
            f"Scope: {suggestion.scope}",
            f"Source: {suggestion.source}",
            f"Effective use: {suggestion.applicability_label}",
            "Policy path:",
            suggestion.applicability_explanation,
            *(
                (f"Unavailable: {suggestion.blocked_reason}",)
                if suggestion.blocked_reason else ()
            ),
        ))

    def _refresh_related_tags(self):
        if not hasattr(self,"pending_tag_chips"):return

        if self.knowledge is None:
            suggestions=()
        else:
            related_annotation=self._related_annotation_state()
            if self._related_tag_service is None:
                semantic=getattr(self.knowledge,"_app_tag_semantic_service",None)
                if getattr(semantic,"tags",None) is not self.knowledge:
                    semantic=None
                self._related_tag_service=RelatedTagSuggestionService(
                    self.knowledge,self.categories,self.characters,
                    semantic_relationships=semantic,
                )
            ignored_tags=self._ignored_tags()
            ignored_ids={
                tag.id for value in ignored_tags
                if (tag:=self.knowledge.find(value)) is not None
            }
            suggestions=self._related_tag_service.suggest(
                self._related_image_wide_tags(related_annotation),
                confirmed_absent=self.record.annotation.confirmed_absent,
                rejected_tags=ignored_tags,
                annotation=related_annotation,
            )
            suggestions=tuple(
                suggestion for suggestion in suggestions
                if suggestion.tag_id not in ignored_ids
                and normalize_tag(suggestion.display) not in ignored_tags
                and not (
                    suggestion.owner_kind=="character"
                    and self._character_role(
                        self.knowledge.tags.get(suggestion.tag_id)
                    )
                )
            )
        self.related_tag_suggestions=tuple(suggestions)
        self._related_suggestion_count=len(suggestions)
        suggestions=tuple(
            item for item in suggestions
            if self._tag_matches_shared_filters(item.display,item.tag_id)
        )
        self._visible_related_tag_count=len(suggestions)
        self._refresh_pending_tag_surface()
        policy=self._fact_expansion_policy()
        safe=policy.eligible(suggestions)
        self.review_tag_more_button.setVisible(bool(safe))
        self.safe_related_action.setText(
            f"Add {len(safe):,} Safe Related Tag"
            f"{'' if len(safe)==1 else 's'}\u2026"
        )
        self.inspect_related_tag_button.hide()
        self._update_review_workspace_status()
        self._update_suggestion_actions()

    def _selected_related_suggestions(self):
        selected={normalize_tag(value) for value in self.pending_tag_chips.selected_values()}
        return tuple(
            item for key,item in getattr(self,"_related_review_rows",{}).items()
            if key in selected and item.eligible
        )

    def _fact_expansion_policy(self):
        return DeterministicFactExpansionPolicy.from_settings(self.settings)

    def _safe_related_suggestions(self):
        return self._fact_expansion_policy().eligible(
            tuple(item for item in self.related_tag_suggestions
                  if self._tag_matches_shared_filters(item.display,item.tag_id))
        )

    def _update_related_tag_actions(self,*_):
        selected=self._selected_related_suggestions()
        self.inspect_related_tag_button.setVisible(bool(selected))
        self.inspect_related_tag_button.setEnabled(bool(selected))
        self._update_suggestion_actions()

    def _add_selected_related_tags(self):
        selected=self._selected_related_suggestions()
        if not selected:return
        if self._stage_add_related_suggestions(selected):
            self._refresh_tag_lists(); self._refresh_suggestions()

    def _add_selected_review_tags(self,*,pending=None,related=None):
        pending=self._selected_pending_tag_values() if pending is None else pending
        related=self._selected_related_suggestions() if related is None else related
        if not pending and not related:
            return
        history_start=len(self._tag_history)
        changed=False
        retired={entry.normalized_value:entry for entry in self._retired_review_plan().blockers
                 if entry.status in {"tombstone","missing_identity"}}
        ordinary=[]
        for value in pending:
            entry=retired.get(normalize_tag(value))
            if entry is None:
                ordinary.append(value)
            elif self._review_retired_entry(entry):
                changed=True
            else:
                # Earlier reviewed decisions remain staged and independently undoable.
                return
        pending=ordinary
        if pending:
            changed=bool(self._stage_add_tags_batch(pending)) or changed
        if related:
            changed=bool(self._stage_add_related_suggestions(related)) or changed
        if not changed:
            return
        additions=tuple(self._tag_history[history_start:])
        if len(additions)>1:
            del self._tag_history[history_start:]
            self._tag_history.append((
                "add_review_batch",additions,None,-1,
            ))
        self._refresh_tag_lists(); self._refresh_suggestions()

    def _add_safe_related_tags(self):
        policy=self._fact_expansion_policy()
        suggestions=self._safe_related_suggestions()
        if not suggestions:return
        dialog=QDialog(self); dialog.setWindowTitle("Add Safe Related Tags")
        dialog.resize(980,560); layout=QVBoxLayout(dialog)
        intro=QLabel(
            f"{policy.display_name} allows the exact additions below. "
            "Nothing is added automatically: choosing Stage Safe Tags creates one "
            "undoable edit in this window, and Save Changes is still required. "
            "TXT, Metadata, captions, and image files are not changed by this preview."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        tree=QTreeWidget()
        tree.setHeaderLabels(
            ("Related SDXL Tag","Supported By","Relationship Path","Scope")
        )
        tree.setRootIsDecorated(False)
        header=tree.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Fixed); tree.setColumnWidth(0,190)
        header.setSectionResizeMode(1,QHeaderView.Fixed); tree.setColumnWidth(1,250)
        header.setSectionResizeMode(2,QHeaderView.Fixed); tree.setColumnWidth(2,190)
        header.setSectionResizeMode(3,QHeaderView.Stretch)
        for suggestion in suggestions:
            item=QTreeWidgetItem((
                suggestion.display,
                suggestion.supporting_display,
                suggestion.path_label,
                suggestion.scope,
            ))
            tooltip=self._related_tag_tooltip(suggestion)
            for column in range(4):item.setToolTip(column,tooltip)
            tree.addTopLevelItem(item)
        layout.addWidget(tree,1)
        remaining=len(self.related_tag_suggestions)-len(suggestions)
        summary=QLabel(
            f"{counted_noun(len(suggestions),'exact Tag addition')} qualify. "
            f"{counted_noun(remaining,'other suggestion')} remain available for ordinary review."
        )
        summary.setObjectName("mutedLabel"); summary.setWordWrap(True)
        layout.addWidget(summary)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        stage=QPushButton(
            f"Stage {len(suggestions):,} Safe Tag"
            f"{'' if len(suggestions)==1 else 's'}"
        )
        stage.setProperty("primary",True)
        buttons.addButton(stage,QDialogButtonBox.AcceptRole)
        stage.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec()!=QDialog.Accepted:return
        if self._stage_add_related_suggestions(suggestions):
            self._refresh_tag_lists(); self._refresh_suggestions()

    def _review_related_tags(self,suggestions=None):
        suggestions=tuple(suggestions or self.related_tag_suggestions)
        if not suggestions:return
        dialog=QDialog(self); dialog.setWindowTitle("Review Related Tags")
        dialog.resize(920,520); layout=QVBoxLayout(dialog)
        intro=QLabel(
            "These are review suggestions from exact System Knowledge relationships. "
            "The Scope and Path columns explain why each tag applies. Blocked rows "
            "remain visible but cannot be selected."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        tree=QTreeWidget()
        tree.setHeaderLabels(("Add","Related SDXL Tag","Supported By","Path","Scope","Status"))
        tree.setRootIsDecorated(False)
        header=tree.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Fixed); tree.setColumnWidth(0,54)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3,QHeaderView.Stretch)
        header.setSectionResizeMode(4,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5,QHeaderView.ResizeToContents)
        for suggestion in suggestions:
            item=QTreeWidgetItem((
                "",
                suggestion.display,
                suggestion.supporting_display,
                suggestion.path_label,
                suggestion.scope,
                suggestion.blocked_reason or suggestion.applicability_label,
            ))
            item.setData(0,Qt.UserRole,suggestion)
            item.setCheckState(0,Qt.Unchecked)
            if not suggestion.eligible:
                item.setFlags(item.flags()&~Qt.ItemIsUserCheckable)
            tooltip=self._related_tag_tooltip(suggestion)
            for column in range(1,6):item.setToolTip(column,tooltip)
            tree.addTopLevelItem(item)
        layout.addWidget(tree,1)
        technical=QWidget()
        technical_layout=QVBoxLayout(technical)
        technical_layout.setContentsMargins(0,0,0,0)
        technical_text=QPlainTextEdit()
        technical_text.setReadOnly(True)
        technical_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        technical_text.setMaximumHeight(170)
        technical_text.setAccessibleName(
            "Exact relationship and consumer policy details"
        )
        technical_layout.addWidget(technical_text)
        technical_toggle=DisclosureButton(
            "Show Technical Details…","Hide Technical Details",technical,
        )
        layout.addWidget(technical_toggle,0,Qt.AlignLeft)
        layout.addWidget(technical)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        add_button=QPushButton("Add Selected"); add_button.setProperty("primary",True)
        add_button.setEnabled(False); buttons.addButton(add_button,QDialogButtonBox.AcceptRole)
        def update_add_button(*_):
            add_button.setEnabled(any(
                tree.topLevelItem(index).checkState(0)==Qt.Checked
                for index in range(tree.topLevelItemCount())
            ))
        tree.itemChanged.connect(update_add_button)
        edit_support_button=QPushButton("Edit Starting Tag…")
        edit_support_button.setToolTip(
            "Open the System Knowledge record that produced the selected suggestion."
        )
        edit_support_button.setEnabled(False)
        buttons.addButton(edit_support_button,QDialogButtonBox.ActionRole)
        def current_supporting_tag_id():
            item=tree.currentItem()
            suggestion=item.data(0,Qt.UserRole) if item is not None else None
            stable_id=str(getattr(suggestion,"supporting_tag_id","") or "")
            return stable_id if self.knowledge and stable_id in self.knowledge.tags else ""
        def update_current_details(*_):
            edit_support_button.setEnabled(bool(current_supporting_tag_id()))
            item=tree.currentItem()
            suggestion=item.data(0,Qt.UserRole) if item is not None else None
            technical_text.setPlainText(
                self._related_tag_technical_details(suggestion)
                if suggestion is not None else ""
            )
        tree.currentItemChanged.connect(update_current_details)
        def edit_supporting_tag():
            stable_id=current_supporting_tag_id()
            if not stable_id:return
            dialog.reject()
            QTimer.singleShot(
                0,lambda:self.edit_tag_requested.emit(stable_id,self)
            )
        edit_support_button.clicked.connect(edit_supporting_tag)
        if tree.topLevelItemCount():
            tree.setCurrentItem(tree.topLevelItem(0))
        add_button.clicked.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec()!=QDialog.Accepted:return
        selected=[
            tree.topLevelItem(index).data(0,Qt.UserRole)
            for index in range(tree.topLevelItemCount())
            if tree.topLevelItem(index).checkState(0)==Qt.Checked
        ]
        if self._stage_add_related_suggestions(selected):
            self._refresh_tag_lists(); self._refresh_suggestions()

    def _show_new_tag_entry(self):
        self.new_tag_panel.show(); self.new_tag.setFocus(); self.new_tag.selectAll()

    def _add_exact_tag(self):
        values=parse_tag_paste(self.new_tag.text())
        if not values:
            self.new_tag_status.setText("Enter one exact SDXL Tag first."); self.new_tag_status.show()
            return
        added=len(self._stage_add_tags_batch(values)); self.new_tag.clear()
        self.new_tag_status.setText(
            f"Resolved reusable identities and staged {added:,} SDXL Tag(s). "
            "Choose Save Changes to keep the image edits."
            if added else "Already on this image."
        )
        self.new_tag_status.setVisible(not added); self.new_tag_panel.setVisible(not added); self._refresh_tag_lists(); self._refresh_suggestions()

    def _confirm_restore_ignored_tags(self,values):
        values=tuple(values)
        dialog=QMessageBox(self)
        dialog.setWindowTitle("Restore ignored SDXL Tags?")
        dialog.setIcon(QMessageBox.Question)
        dialog.setText(
            f"{len(values):,} selected SDXL Tag(s) are marked as not on this image.\n\n"
            "Restore that image decision and stage the tag addition?"
        )
        dialog.setDetailedText("\n".join(f"• {value}" for value in values))
        restore=dialog.addButton("Restore and Add",QMessageBox.AcceptRole)
        dialog.addButton(QMessageBox.Cancel)
        dialog.setDefaultButton(restore)
        dialog.exec()
        return dialog.clickedButton() is restore

    def _confirm_restore_absent_tags(self,values):
        values=tuple(values)
        dialog=QMessageBox(self)
        dialog.setWindowTitle("Reverse confirmed absence?")
        dialog.setIcon(QMessageBox.Question)
        dialog.setText(
            f"{len(values):,} selected SDXL Tag(s) are explicitly confirmed "
            "absent from this image.\n\n"
            "Reverse that reviewed fact and stage the tag addition?"
        )
        dialog.setInformativeText(
            "Confirmed absence is separate from an ignored suggestion. The "
            "source evidence remains available after this decision is reversed."
        )
        dialog.setDetailedText("\n".join(f"• {value}" for value in values))
        restore=dialog.addButton("Reverse Absence and Add",QMessageBox.AcceptRole)
        dialog.addButton(QMessageBox.Cancel)
        dialog.setDefaultButton(QMessageBox.Cancel)
        dialog.exec()
        return dialog.clickedButton() is restore

    def _confirm_diagnostic_tag_conflicts(self,warnings):
        warnings=tuple(dict.fromkeys(str(value) for value in warnings if str(value).strip()))
        if not warnings:
            return True
        context=dict(self._diagnostic_conflict_context)
        proposed=tuple(context.get("proposed",()))
        replacements=tuple(context.get("replacements",()))
        dialog=QMessageBox(self)
        dialog.setWindowTitle("Review SDXL Tag conflict")
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText(
            "The proposed SDXL Tag selection introduces a diagnostic conflict. "
            "The rule is advisory and will not choose which image fact is correct."
        )
        explanation=[]
        if proposed:
            explanation.append("Proposed: "+", ".join(proposed))
        if replacements:
            explanation.append(
                "Existing involved: "
                +", ".join(str(row[0]) for row in replacements)
            )
        explanation.extend(warnings)
        explanation.append(
            "Choose whether to replace only the disclosed conflicting tags, "
            "keep both sides for later review, or cancel."
        )
        dialog.setInformativeText("\n".join(explanation))
        details=["Conflicts:",*warnings]
        if replacements:
            details.extend((
                "",
                "Replace Conflicting Tags will remove from this image:",
                *(f"• {row[0]}" for row in replacements),
            ))
        else:
            details.extend((
                "",
                "Replacement is unavailable because removing current tags would "
                "not resolve every introduced conflict in this selection.",
            ))
        replace=dialog.addButton(
            "Replace Conflicting Tags",QMessageBox.ActionRole
        )
        replace.setEnabled(bool(replacements))
        if not replacements:
            replace.setToolTip(
                "Review or add the conflicting proposed tags separately."
            )
        dialog.setDetailedText("\n".join(details))
        add=dialog.addButton("Add Both (Keep Conflict)",QMessageBox.AcceptRole)
        dialog.addButton(QMessageBox.Cancel)
        dialog.setDefaultButton(QMessageBox.Cancel)
        dialog.exec()
        if dialog.clickedButton() is replace:
            return "replace"
        if dialog.clickedButton() is add:
            return "add_both"
        return False

    def _prepare_tag_additions(self,values,*,prospective_after=None,scoped=False,revalidate=None):
        self._prepared_conflict_replacements=()
        self._prepared_absent_restores=set()
        self._diagnostic_conflict_context={}
        identity_plan=self._identity_service.plan(
            values,
            stored_ids=self._working_tag_identities,
            confirmation=True,
        )
        identity_plan,_restored=self._review_removed_identity_plan(
            identity_plan,source="image_editor_confirmation",
            continuation=(
                "The restored tag or tags will then be staged in this editor; "
                "the image remains unchanged until Save Changes."
            ),
        )
        if identity_plan is None:
            return ()
        if identity_plan.blockers:
            QMessageBox.information(
                self,
                "SDXL Tag Identity Needs Review",
                "The selection was not added because every confirmed SDXL Tag "
                "must resolve to exactly one reusable System Knowledge identity."
                "\n\n"+self._identity_plan_details(identity_plan),
            )
            return ()
        prepared=[
            entry.canonical_display or entry.raw_value
            for entry in identity_plan.entries
        ]
        if not prepared:return ()
        current=self.current_tags.normalized_set()
        blocked=[
            value for value in prepared
            if (scoped or normalize_tag(value) not in current)
            and self.automation_engine.is_evidence_rejected(value)
        ]
        if blocked:
            QMessageBox.information(
                self,
                "SDXL Tag ignored by Automation",
                "The selection was not added because an active global rule rejects "
                "this SDXL Tag as incoming evidence. Review that rule in Automation "
                "before confirming the tag:\n\n"
                +"\n".join(f"• {value}" for value in blocked),
            )
            return ()
        absent=[
            value for value in prepared
            if normalize_tag(value) in self._confirmed_absent_tags()
        ]
        if absent:
            if not self._confirm_restore_absent_tags(absent):
                return ()
            self._prepared_absent_restores={
                normalize_tag(value) for value in absent
            }
        ignored=[
            value for value in prepared
            if normalize_tag(value) in self._ignored_tags()
        ]
        if ignored and not self._confirm_restore_ignored_tags(ignored):
            return ()
        proposed=[value for value in prepared if normalize_tag(value) not in current]
        after=(
            TagCollection(prospective_after).to_list()
            if prospective_after is not None
            else [*self.current_tags.to_list(),*proposed]
        )
        warnings=self.automation_engine.conflict_warnings(
            self.record.image_path,self.current_tags.to_list(),
            after,
        ) if proposed else []
        matches=()
        replacement_rows=()
        if warnings and not scoped:
            resolver=getattr(
                self.automation_engine,"introduced_conflicts",None
            )
            if callable(resolver):
                try:
                    matches=tuple(resolver(
                        self.record,self.current_tags.to_list(),after,
                    ))
                except (OSError,RuntimeError,ValueError):
                    matches=()
            proposed_keys={
                normalize_tag(value) for value in proposed
            }
            replacement_keys={
                normalize_tag(value)
                for match in matches
                for value in getattr(match,"found_tags",())
                if normalize_tag(value) in current
                and any(
                    normalize_tag(candidate) in proposed_keys
                    for candidate in getattr(match,"found_tags",())
                )
            }
            ordered=self.current_tags.to_list()
            candidate_rows=tuple(
                (
                    (
                        value,index,
                        normalize_tag(value) in self.pending_restored_tags,
                        True,
                    )
                    if normalize_tag(value)
                    in self.pending_restored_absent_tags
                    else (
                        value,index,
                        normalize_tag(value) in self.pending_restored_tags,
                    )
                )
                for index,value in enumerate(ordered)
                if normalize_tag(value) in replacement_keys
            )
            replacement_after=[
                value for value in after
                if normalize_tag(value) not in replacement_keys
            ]
            if candidate_rows:
                try:
                    remaining=tuple(resolver(
                        self.record,self.current_tags.to_list(),
                        replacement_after,
                    ))
                except (OSError,RuntimeError,ValueError):
                    remaining=(object(),)
                if not remaining:
                    replacement_rows=candidate_rows
        proposed_state=frozenset(
            normalize_tag(value)
            for value in after
            if normalize_tag(value)
        )
        decision=True
        if warnings and proposed_state not in self._approved_conflict_states:
            self._diagnostic_conflict_context={
                "proposed":tuple(proposed),
                "matches":matches,
                "replacements":replacement_rows,
            }
            decision=self._confirm_diagnostic_tag_conflicts(warnings)
            self._diagnostic_conflict_context={}
            if not decision:
                return ()
        if decision=="replace" and replacement_rows:
            self._prepared_conflict_replacements=replacement_rows
        elif warnings:
            self._approved_conflict_states.add(proposed_state)
        try:
            if identity_plan.new_entries:
                resolution=self._identity_service.commit(
                    identity_plan,
                    source="image_editor_confirmation",
                )
            else:
                resolution=self._identity_service.require_existing(
                    (entry.raw_value for entry in identity_plan.entries),
                    stored_ids=self._working_tag_identities,
                    confirmation=True,
                )
        except (ConfirmedTagIdentityError,OSError,RuntimeError,ValueError) as error:
            QMessageBox.critical(
                self,
                "Reusable Tag Identity Was Not Saved",
                "The image selection was not staged. "+str(error),
            )
            self._prepared_conflict_replacements=()
            return ()
        if revalidate is not None:revalidate()
        self._remember_identity_resolution(resolution)
        return resolution.values

    def _consume_prepared_conflict_replacements(self):
        rows=tuple(self._prepared_conflict_replacements)
        self._prepared_conflict_replacements=()
        removed=[]
        for row in rows:
            value,index=row[:2]
            if self.current_tags.remove(value):
                if len(row)>2 and row[2]:
                    self.pending_restored_tags.discard(
                        normalize_tag(value)
                    )
                if len(row)>3 and row[3]:
                    self.pending_restored_absent_tags.discard(
                        normalize_tag(value)
                    )
                removed.append(row)
        return tuple(removed)

    def _restore_tag_rows(self,rows):
        values=self.current_tags.to_list()
        for row in sorted(rows,key=lambda item:item[1]):
            value,index=row[:2]
            if normalize_tag(value) in {
                normalize_tag(item) for item in values
            }:
                continue
            values.insert(min(max(0,index),len(values)),value)
            if len(row)>2 and row[2]:
                self.pending_restored_tags.add(normalize_tag(value))
            if len(row)>3 and row[3]:
                self.pending_restored_absent_tags.add(normalize_tag(value))
        self.current_tags=TagCollection(values)

    def _restore_ignore_for_add(self,value):
        key=normalize_tag(value)
        state={
            "pending":self.pending_ignored_tags.pop(key,None),
            "restored":key in self.pending_restored_tags,
            "absent_restored":key in self.pending_restored_absent_tags,
            "added":False,
        }
        if key in self._persisted_ignored_tags():
            self.pending_restored_tags.add(key)
        else:
            self.pending_restored_tags.discard(key)
        if key in self._prepared_absent_restores:
            self.pending_restored_absent_tags.add(key)
            self._prepared_absent_restores.discard(key)
        return state

    def _undo_restored_ignore_for_add(self,value,state):
        key=normalize_tag(value)
        if not isinstance(state,dict):
            if state:self.pending_ignored_tags[key]=state
            return
        pending=state.get("pending")
        if pending:self.pending_ignored_tags[key]=pending
        else:self.pending_ignored_tags.pop(key,None)
        if state.get("restored"):self.pending_restored_tags.add(key)
        else:self.pending_restored_tags.discard(key)
        if state.get("absent_restored"):
            self.pending_restored_absent_tags.add(key)
        else:
            self.pending_restored_absent_tags.discard(key)

    def _stage_add_tag(self,value):
        prepared=self._prepare_tag_additions((value,))
        if not prepared:return False
        replacements=self._consume_prepared_conflict_replacements()
        value=prepared[0]; state=self._restore_ignore_for_add(value)
        state["added"]=self.current_tags.add(value)
        if (
            not state["added"]
            and not state["pending"]
            and normalize_tag(value) not in self.pending_restored_tags
            and normalize_tag(value) not in self.pending_restored_absent_tags
        ):
            self._restore_tag_rows(replacements)
            return False
        if replacements:
            self._tag_history.append((
                "replace_conflicts",
                {
                    "additions":((value,state),),
                    "replacements":replacements,
                },
                None,-1,
            ))
        else:
            self._tag_history.append(("add",value,state,len(self.current_tags)-1))
        self._remember_recent_tag(value)
        return True

    def _stage_add_tags_batch(self,values):
        values=self._prepare_tag_additions(values)
        if not values:return ()
        replacements=self._consume_prepared_conflict_replacements()
        added=[]
        for value in values:
            state=self._restore_ignore_for_add(value)
            state["added"]=self.current_tags.add(value)
            if (
                not state["added"]
                and not state["pending"]
                and normalize_tag(value) not in self.pending_restored_tags
                and normalize_tag(value) not in self.pending_restored_absent_tags
            ):
                continue
            added.append((value,state))
        if not added:
            self._restore_tag_rows(replacements)
            return ()
        if replacements:
            self._tag_history.append((
                "replace_conflicts",
                {
                    "additions":tuple(added),
                    "replacements":replacements,
                },
                None,-1,
            ))
        else:
            self._tag_history.append(("add_batch",tuple(added),None,-1))
        self._remember_recent_tags(value for value,_state in added)
        return tuple(value for value,_state in added)

    def _stage_add_related_suggestions(self,suggestions,*,revalidate=None):
        suggestions=tuple(suggestions)
        values=tuple(
            self.knowledge.tags.get(suggestion.tag_id).display
            if self.knowledge is not None and self.knowledge.tags.get(suggestion.tag_id) is not None
            else str(suggestion.display).strip()
            for suggestion in suggestions
        )
        prepared=(self._prepare_tag_additions(values,scoped=True,revalidate=revalidate)
                  if revalidate is not None else self._prepare_tag_additions(values,scoped=True))
        if not prepared:return ()
        prepared_keys={normalize_tag(value) for value in prepared}
        added=[]
        for suggestion in suggestions:
            known=self.knowledge.tags.get(suggestion.tag_id) if self.knowledge is not None else None
            value=known.display if known is not None else str(suggestion.display).strip()
            key=normalize_tag(value)
            if key not in prepared_keys:
                continue
            pending_key=(
                suggestion.owner_kind,
                suggestion.owner_id,
                suggestion.owner_group or (
                    "other"
                    if suggestion.owner_kind == "character"
                    else "tags"
                    if suggestion.owner_kind == "object"
                    else "sdxl_tags"
                ),
                suggestion.tag_id,
            )
            if pending_key in self._pending_scoped_tag_additions:
                continue
            restore_state=self._restore_ignore_for_add(value)
            added_to_current=self.current_tags.add(value)
            restore_state["added"]=added_to_current
            self._pending_scoped_tag_additions[pending_key]=value
            if suggestion.owner_kind=="object":
                self._scoped_ingress_restore_states[pending_key]=(value,restore_state)
            added.append((
                value,restore_state,pending_key,added_to_current,
            ))
        if not added:return ()
        if any(suggestion.owner_kind=="object" for suggestion in suggestions):
            self._sync_current_tags_from_objects()
        self._tag_history.append(("add_related_batch",tuple(added),None,-1))
        self._remember_recent_tags(value for value,_state,_scope,_current in added)
        return tuple(value for value,_state,_scope,_current in added)

    def _add_palette_tag(self,value):
        if self._stage_add_tag(value):
            self._refresh_tag_lists(refresh_related=False); self._refresh_suggestions(); self.tag_palette.focus_after_added(value)

    def _remove_chip_tag(self,value):
        index=self.tag_chips.index_of(value)
        value=self._raw_confirmed_value(value)
        if self.current_tags.remove(value):
            key=normalize_tag(value)
            previous={
                "restored":key in self.pending_restored_tags,
                "absent_restored":key in self.pending_restored_absent_tags,
            }
            self.pending_restored_tags.discard(key)
            self.pending_restored_absent_tags.discard(key)
            self._tag_history.append(("remove",value,previous,index)); self._refresh_tag_lists(refresh_related=False); self._refresh_suggestions(); self.tag_chips.focus_index(index)

    def _remove_and_ignore_chip_tag(self,value):
        index=self.tag_chips.index_of(value)
        value=self._raw_confirmed_value(value)
        if self.current_tags.remove(value):
            key=normalize_tag(value)
            previous={
                "pending":self.pending_ignored_tags.get(key),
                "restored":key in self.pending_restored_tags,
                "absent_restored":key in self.pending_restored_absent_tags,
            }
            self.pending_restored_tags.discard(key)
            self.pending_restored_absent_tags.discard(key)
            self.pending_ignored_tags[key]=value
            self._tag_history.append(("remove_ignore",value,previous,index)); self._refresh_tag_lists(); self._refresh_suggestions(); self.tag_chips.focus_index(index)

    def _revert_add_history_entry(self,entry):
        action,value,_previous,_index=entry
        if action=="retired_tag":
            for name,state in value.items():setattr(self,name,copy.deepcopy(state))
            return ""
        if action=="replace_conflicts":
            for batch_value,batch_state in reversed(value["additions"]):
                if (
                    not isinstance(batch_state,dict)
                    or batch_state.get("added")
                ):
                    self.current_tags.remove(batch_value)
                self._undo_restored_ignore_for_add(
                    batch_value,batch_state
                )
            self._restore_tag_rows(value["replacements"])
            return (
                value["replacements"][0][0]
                if value["replacements"] else ""
            )
        if action=="add_related_batch":
            for batch_value,batch_state,scope_key,added_to_current in reversed(value):
                if scope_key is not None:
                    self._pending_scoped_tag_additions.pop(scope_key,None)
                    self._scoped_ingress_restore_states.pop(scope_key,None)
                if added_to_current:
                    self.current_tags.remove(batch_value)
                self._undo_restored_ignore_for_add(batch_value,batch_state)
            self._sync_current_tags_from_objects()
            return ""
        if action=="add_batch":
            for batch_value,batch_state in reversed(value):
                if not isinstance(batch_state,dict) or batch_state.get("added"):
                    self.current_tags.remove(batch_value)
                self._undo_restored_ignore_for_add(batch_value,batch_state)
            return ""
        raise ValueError(f"Unsupported combined Tag addition history: {action}")

    def _undo_tag_change(self):
        if not self._tag_history: return
        action,value,previous,index=self._tag_history.pop()
        if action=="ignore_suggestions":
            persisted=self._persisted_ignored_tags()
            for key,pending,restored,was_ignored in value:
                if pending is None:self.pending_ignored_tags.pop(key,None)
                else:self.pending_ignored_tags[key]=pending
                if restored or (not was_ignored and key in persisted):
                    self.pending_restored_tags.add(key)
                else:self.pending_restored_tags.discard(key)
            self._refresh_suggestions(); self._update_pending_status(); return
        if action=="retired_tag":
            for name,state in value.items():setattr(self,name,copy.deepcopy(state))
            self._refresh_tag_lists(); self._refresh_suggestions()
            self.save_status.hide()
            return
        if action=="clear_character_identity":
            # Do not discard text the user typed after the combined correction.
            later_text=self.prose.toPlainText()!=value["cleared_prose"]
            for name,state in value["state"].items():
                setattr(self,name,copy.deepcopy(state))
            if not later_text:
                self._applying_description_autofill=True
                try:self.prose.setPlainText(value["prose"])
                finally:self._applying_description_autofill=False
            elif self._description_autofill_state is not None:
                self._description_autofill_state={**self._description_autofill_state,"enabled":False}
            if self._source_autofill_state is not None:
                blocked=self.automatic_character_sources.blockSignals(True)
                self.automatic_character_sources.setChecked(self._source_autofill_state[0])
                self.automatic_character_sources.blockSignals(blocked)
            self.identity_clear_status.setText("Identity clearing undone. Later description edits were kept." if later_text else "Identity clearing undone.")
            self._refresh_tag_lists(); self._refresh_suggestions(); return
        if action=="ownership_acknowledgement":
            self._working_ownership_acknowledgements=copy.deepcopy(value)
            self._refresh_tag_lists();self._refresh_suggestions();return
        if action=="character_ownership":
            characters,ownership_changes=value
            self._working_characters=[
                CharacterAnnotation.from_dict(item,position)
                for position,item in enumerate(characters,1)
            ]
            self._character_ownership_changes=copy.deepcopy(
                ownership_changes
            )
            self._refresh_tag_lists(); self._refresh_suggestions(); return
        if action=="set_context":
            self._working_contexts=[
                ImageContextAnnotation.from_dict(item)
                for item in value
            ]
            self._refresh_tag_lists(); self._refresh_suggestions(); return
        if action in {"replace_conflicts","add_related_batch","add_batch"}:
            focus=self._revert_add_history_entry((action,value,previous,index))
            self._refresh_tag_lists(); self._refresh_suggestions()
            if focus:self.tag_chips.focus_tag(focus)
            return
        if action=="add_review_batch":
            focus=""
            for entry in reversed(value):
                focus=self._revert_add_history_entry(entry) or focus
            self._refresh_tag_lists(); self._refresh_suggestions()
            if focus:self.tag_chips.focus_tag(focus)
            return
        if action in {"remove_batch","remove_ignore_batch"}:
            rows=sorted(value,key=lambda item:item[1])
            values=self.current_tags.to_list()
            for row in rows:
                batch_value,index=row[:2]
                values.insert(min(max(0,index),len(values)),batch_value)
                restored_index=3 if action=="remove_ignore_batch" else 2
                if len(row)>restored_index and row[restored_index]:
                    self.pending_restored_tags.add(
                        normalize_tag(batch_value)
                    )
                absent_index=4 if action=="remove_ignore_batch" else 3
                if len(row)>absent_index and row[absent_index]:
                    self.pending_restored_absent_tags.add(
                        normalize_tag(batch_value)
                    )
                if action=="remove_ignore_batch":
                    key=normalize_tag(batch_value); previous=row[2]
                    if previous:self.pending_ignored_tags[key]=previous
                    else:self.pending_ignored_tags.pop(key,None)
            self.current_tags=TagCollection(values)
            self._refresh_tag_lists(); self._refresh_suggestions(); return
        key=normalize_tag(value)
        if action=="add":
            if not isinstance(previous,dict) or previous.get("added"):
                self.current_tags.remove(value)
            self._undo_restored_ignore_for_add(value,previous)
        else:
            values=self.current_tags.to_list(); values.insert(min(max(0,index),len(values)),value); self.current_tags=TagCollection(values)
            if action=="remove_ignore":
                if isinstance(previous,dict):
                    pending=previous.get("pending")
                    if pending:self.pending_ignored_tags[key]=pending
                    else:self.pending_ignored_tags.pop(key,None)
                    if previous.get("restored"):
                        self.pending_restored_tags.add(key)
                    if previous.get("absent_restored"):
                        self.pending_restored_absent_tags.add(key)
                elif previous:
                    self.pending_ignored_tags[key]=previous
                else:
                    self.pending_ignored_tags.pop(key,None)
            elif isinstance(previous,dict) and previous.get("restored"):
                self.pending_restored_tags.add(key)
            if isinstance(previous,dict) and previous.get("absent_restored"):
                self.pending_restored_absent_tags.add(key)
        self._refresh_tag_lists(refresh_related=False); self._refresh_suggestions()
        if action!="add":self.tag_chips.focus_tag(value)

    def _remember_recent_tag(self,value):
        self._remember_recent_tags((value,))

    def _remember_recent_tags(self,values):
        for value in values:
            key=normalize_tag(value)
            self._recent_tags=[item for item in self._recent_tags if normalize_tag(item)!=key]
            self._recent_tags.insert(0,value)
        self._recent_tags=self._recent_tags[:30]; self._persist_tag_preferences()

    def _set_favorite_tag(self,value,enabled):
        focus=QApplication.focusWidget(); chip_focus=focus is not None and self.tag_chips.isAncestorOf(focus)
        palette_focus=focus is not None and self.tag_palette.isAncestorOf(focus)
        key=normalize_tag(value); self._favorite_tags=[item for item in self._favorite_tags if normalize_tag(item)!=key]
        if enabled: self._favorite_tags.append(value)
        self._favorite_tags=self._favorite_tags[:100]; self._persist_tag_preferences(); self._refresh_tag_lists()
        if chip_focus:self.tag_chips.focus_tag(value)
        elif palette_focus:self.tag_palette.focus_value(value)

    def _persist_tag_preferences(self):
        if self.settings is None: return
        self.settings.favorite_sdxl_tags=list(self._favorite_tags); self.settings.recent_sdxl_tags=list(self._recent_tags)
        try: self.settings.save()
        except (OSError,AttributeError): pass

    def _current_confirmed_tags(self):
        values=self.current_tags.to_list()
        ordering=TagOrderingService(self.knowledge,self.characters)
        return ordering.order(
            [TagOrderCandidate(value,sequence=index) for index,value in enumerate(values)],
            canonicalize=False,deterministic=True,
        )

    def _apply_character_ownership_placements(self,annotation):
        for tag_id,state in self._character_ownership_changes.items():
            self._character_ownership_service.place_fact(
                annotation,fact_tag_id=tag_id,
                display=str(state.get("display") or ""),
                owned=bool(state.get("owned")),
                role=str(state.get("role") or ""),
            )

    def _related_annotation_state(self):
        annotation=copy.deepcopy(self.record.annotation)
        if isinstance(annotation.source.get("ownership_acknowledgements",[]),list) and (self._working_ownership_acknowledgements or "ownership_acknowledgements" in annotation.source):
            annotation.source["ownership_acknowledgements"]=copy.deepcopy(self._working_ownership_acknowledgements)
        annotation.characters=(
            [] if self._clear_legacy_character_assignments else
            copy.deepcopy(self._working_characters)
        )
        annotation.objects=copy.deepcopy(self._working_objects)
        annotation.tag_identities=dict(self._working_tag_identities)
        self._apply_retired_tag_replacements(annotation)
        self._apply_character_ownership_placements(annotation)
        apply_context_partition(
            annotation,self._working_contexts,
            confirmed_values=self.current_tags,
        )
        desired=self.current_tags.normalized_set()
        image_containers=[annotation.visual_style.style_tags]
        image_containers.extend(annotation.global_tags.values())
        image_containers.extend(annotation.scene.values())
        image_containers.extend(annotation.custom_groups.values())
        for values in image_containers:
            values[:]=[
                value for value in values if normalize_tag(value) in desired
            ]
        for character in annotation.characters:
            for group,values in character.tags.items():
                character.tags[group]=[
                    value for value in values if normalize_tag(value) in desired
                ]
        for item in annotation.objects:
            item.tags=[
                value for value in item.tags if normalize_tag(value) in desired
            ]
            if item.identity_tag and normalize_tag(item.identity_tag) not in desired:
                item.identity_tag_id=None; item.identity_tag=None
        for (
            owner_kind,owner_id,group,_tag_id
        ),value in self._pending_scoped_tag_additions.items():
            if normalize_tag(value) not in desired:
                continue
            add_related_tag_to_destination(
                annotation,
                display=value,
                owner_kind=owner_kind,
                owner_id=owner_id,
                owner_group=group,
            )
        return annotation

    def _staged_image_record(self):
        record=ImageRecord(self.record.image_path,auto_load=False)
        record.annotation=self._related_annotation_state()
        record.loaded=True
        return record

    def _related_image_wide_tags(self,annotation):
        desired=self.current_tags.normalized_set()
        values=TagCollection()
        containers=[annotation.sdxl_tags,annotation.uncategorized]
        for container in containers:
            values.extend(
                value for value in container if normalize_tag(value) in desired
            )
        retained={
            normalize_tag(value)
            for value in self.record.annotation.all_structured_tags()
        }
        scoped_pending={
            normalize_tag(value)
            for (_owner_kind,_owner_id,_group,_tag_id),value
            in self._pending_scoped_tag_additions.items()
        }
        values.extend(
            value for value in self.current_tags
            if normalize_tag(value) not in retained
            and normalize_tag(value) not in scoped_pending
        )
        return values.to_list()

    def _refresh_confirmed_context(self):
        if not hasattr(self,"prose"):
            return
        self._refresh_boundary_review()
        if hasattr(self,"description_edit_actions_button"):
            has_descriptions=bool(self.prose.toPlainText().strip())
            can_undo=self.prose.document().isUndoAvailable()
            self.clear_descriptions_action.setVisible(has_descriptions)
            self.undo_description_action.setVisible(can_undo)
            self.undo_description_action.setEnabled(can_undo)
            self.description_edit_actions_button.setVisible(has_descriptions or can_undo)

    def _refresh_boundary_review(self):
        """Audit the staged record; corrections and Undo never write authority here."""
        if not hasattr(self,"boundary_review_values"):
            return
        # This audit needs only staged confirmed values and their identities.
        # Related/context previews deliberately retain some original containers,
        # so they cannot stand in for the current editor selection here.
        record=ImageRecord(self.record.image_path,auto_load=False)
        record.annotation=ImageAnnotation(
            image=self.record.annotation.image,
            sdxl_tags=self.current_tags.to_list(),
            plain_language=self._descriptions(self.prose.toPlainText()),
            tag_identities=dict(self._working_tag_identities),
        )
        issues=tuple(item for item in audit_typed_boundary(record,self.knowledge)
                     if item.code.startswith("confirmed_"))
        if issues==getattr(self,"_boundary_review_issues",None):
            return
        previous=self.boundary_review_values.currentData()
        self._boundary_review_issues=issues
        self.boundary_review_values.blockSignals(True)
        self.boundary_review_values.clear()
        selected=0
        for index,item in enumerate(issues):
            kind="Tag" if item.kind=="tag" else "Description"
            label=f"{kind}: {item.value}"
            self.boundary_review_values.addItem(label[:100]+("…" if len(label)>100 else ""),item)
            self.boundary_review_values.setItemData(index,label,Qt.ToolTipRole)
            if item==previous:selected=index
        self.boundary_review_values.setCurrentIndex(selected if issues else -1)
        self.boundary_review_values.blockSignals(False)
        self.boundary_review_panel.setVisible(bool(issues))
        self._show_boundary_review_detail()

    def _show_boundary_review_detail(self,*_):
        item=self.boundary_review_values.currentData()
        self.boundary_review_detail.setText(item.message if item is not None else "")
        self.boundary_review_detail.setToolTip(item.value if item is not None else "")
        self.boundary_review_button.setEnabled(item is not None)

    def _review_boundary_value(self):
        item=self.boundary_review_values.currentData()
        if item is None:return
        if item.kind=="tag":
            self.tabs.setCurrentWidget(self.image_data_tab)
            values,_identities=self._canonical_confirmed_state([item.value])
            value=values[0] if values else item.value
            self._reset_shared_tag_filters()
            self.tag_chips.select_values([value])
            self._show_confirmed_tag_from_palette(value)
        else:
            self.tabs.setCurrentWidget(self.description_tab)
            self.prose.moveCursor(QTextCursor.Start)
            self.prose.find(item.value)
            self.prose.setFocus(Qt.OtherFocusReason)
            self.prose.ensureCursorVisible()

    def _update_review_workspace_status(
        self,*,tag_count=None,description_count=None,
    ):
        if tag_count is not None:
            self._pending_tag_count=int(tag_count)
        if description_count is not None:
            self._pending_description_count=int(description_count)
        pending_tags=int(getattr(self,"_pending_tag_count",0))
        related_tags=int(getattr(self,"_related_suggestion_count",0))
        pending_descriptions=int(
            getattr(self,"_pending_description_count",0)
        )
        if hasattr(self,"tag_activity_tabs"):
            pending_index=self.tag_activity_tabs.indexOf(
                self.pending_tags_page
            )
            if pending_index>=0:
                visible=len(self._visible_pending_tag_items())+int(getattr(self,"_visible_related_tag_count",related_tags))
                total=pending_tags+related_tags
                self.tag_activity_tabs.setTabText(
                    pending_index,
                    "Review Pending"
                    +(
                        (f" ({visible:,} of {total:,})" if visible!=total else f" ({total:,})")
                        if pending_tags or related_tags else ""
                    ),
                )
                review_details=[]
                if pending_tags:
                    review_details.append(
                        f"{pending_tags:,} pending Tag finding(s)"
                    )
                if related_tags:
                    review_details.append(
                        f"{related_tags:,} related Tag suggestion(s)"
                    )
                self.tag_activity_tabs.setTabToolTip(
                    pending_index,
                    (
                        "; ".join(review_details)+" need review."
                        if review_details else
                        "No Tag findings currently need review; AI Tag actions remain available."
                    ),
                )
            self._update_tag_workspace_layout()
        if not hasattr(self,"tabs"):
            return
        description_index=self.tabs.indexOf(self.description_tab)
        if description_index>=0:
            self.tabs.setTabText(
                description_index,
                "Descriptions"
                +(f" ({pending_descriptions:,} to review)"
                  if pending_descriptions else ""),
            )
            self.tabs.setTabToolTip(
                description_index,
                (
                    f"{pending_descriptions:,} Plain Language finding(s) "
                    "need review"
                    if pending_descriptions else
                    "Edit confirmed Plain Language Descriptions"
                ),
            )
        self._update_tag_manager_tab_status()

    def _clear_character_identity(self):
        annotation=self._related_annotation_state()
        original_text=self.prose.toPlainText()
        plan=self._character_view_service.identity_clear_plan(
            annotation,self.current_tags,original_text,prior_values=self.original_tags,
        )
        if not (plan.removed_tags or plan.changed_descriptions or plan.association_count):
            self.identity_clear_status.setText("No known character identity or source data to clear on this image.")
            self.identity_clear_status.show(); return
        attributes=(
            "current_tags","_working_characters","_working_objects","_working_contexts",
            "_character_ownership_changes","_pending_scoped_tag_additions",
            "_scoped_ingress_restore_states","_source_autofill_state",
            "_source_autofill_seen_present","_source_autofill_added","_description_autofill_state",
            "pending_ignored_tags","pending_restored_tags","pending_restored_absent_tags",
            "pending_ignored_descriptions","pending_restored_descriptions","pending_accepted_descriptions",
        )
        state={name:copy.deepcopy(getattr(self,name)) for name in attributes}
        removed={normalize_tag(value) for value in plan.removed_tags}
        for value in plan.removed_tags:self.current_tags.remove(value)
        self._working_characters=copy.deepcopy(annotation.characters)
        for character in self._working_characters:
            character.character_id=None
            for group,values in character.tags.items():
                character.tags[group]=[value for value in values if normalize_tag(value) not in removed]
        self._character_ownership_changes={
            key:value for key,value in self._character_ownership_changes.items()
            if normalize_tag(str(value.get("display") or "")) not in removed
        }
        for item in self._working_objects:
            item.tags=[value for value in item.tags if normalize_tag(value) not in removed]
        for context in self._working_contexts:
            context.tags=[value for value in context.tags if normalize_tag(value) not in removed]
            if context.owner_kind=="tag" and normalize_tag(context.owner_label) in removed:
                if normalize_tag(context.owner_label) in normalize_tag(context.label):context.label=""
                context.owner_kind=""; context.owner_id=""; context.owner_label=""
        self._pending_scoped_tag_additions={key:value for key,value in self._pending_scoped_tag_additions.items()
                                            if normalize_tag(value) not in removed}
        self._scoped_ingress_restore_states={key:value for key,value in self._scoped_ingress_restore_states.items()
                                             if normalize_tag(value[0]) not in removed}
        for value in plan.rejected_tags:
            key=normalize_tag(value)
            self.pending_ignored_tags[key]=value
            self.pending_restored_tags.discard(key); self.pending_restored_absent_tags.discard(key)
        for value in plan.changed_descriptions:
            key=normalize_plain_language_exclusion(value)
            self.pending_ignored_descriptions[key]=value
            self.pending_restored_descriptions.discard(key)
            self.pending_accepted_descriptions.discard(value)
        if self._source_autofill_state is not None:
            self._source_autofill_state=(False,self._source_autofill_state[1])
            blocked=self.automatic_character_sources.blockSignals(True)
            self.automatic_character_sources.setChecked(False)
            self.automatic_character_sources.blockSignals(blocked)
        if self._description_autofill_state is not None:
            self._description_autofill_state={**self._description_autofill_state,"enabled":False,
                                              "sentence":"","original_sentence":""}
        self._applying_description_autofill=True
        try:self.prose.setPlainText(plan.description)
        finally:self._applying_description_autofill=False
        self._tag_history.append(("clear_character_identity",{
            "state":state,"prose":original_text,"cleared_prose":plan.description,
        },None,-1))
        self._refresh_tag_lists(); self._refresh_suggestions()
        self.identity_clear_status.setText(
            f"Cleared {len(plan.removed_tags)} identity/source Tag(s), {plan.association_count} named association(s), "
            f"and cleaned {len(plan.changed_descriptions)} description sentence(s). "
            +(f"Removed {len(plan.removed_descriptions)} whole sentence(s) to avoid fragments. " if plan.removed_descriptions else "")
            +"Review the descriptions; Undo restores this change. Save Changes keeps it."
        )
        self.identity_clear_status.show()
        if plan.changed_descriptions:self.tabs.setCurrentWidget(self.description_tab)

    def _clear_confirmed_descriptions(self):
        if not self.prose.toPlainText().strip():
            return
        dialog=QMessageBox(self); dialog.setWindowTitle("Clear Confirmed Descriptions?")
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText("Clear all text in Confirmed Plain Language Descriptions for this image?")
        dialog.setInformativeText(
            "Tags, character and object data, pending findings, and raw evidence stay intact. "
            "Use Undo Description Edit to restore the text. Nothing is written until Save Changes."
        )
        clear_button=dialog.addButton("Clear Descriptions",QMessageBox.DestructiveRole)
        cancel_button=dialog.addButton(QMessageBox.Cancel); dialog.setDefaultButton(cancel_button)
        dialog.exec()
        if dialog.clickedButton() is not clear_button: return
        cursor=self.prose.textCursor()
        cursor.beginEditBlock(); cursor.select(QTextCursor.Document)
        cursor.removeSelectedText(); cursor.endEditBlock()
        self.prose.setTextCursor(cursor); self.prose.setFocus()

    def _stage_clear_confirmed_data(self):
        self.current_tags=TagCollection(); self.prose.clear()
        self._clear_legacy_character_assignments=True
        self._working_characters=[]
        self._character_ownership_changes.clear()
        self._working_objects=[]
        self._working_contexts=[]
        self._pending_scoped_tag_additions={
            key:value
            for key,value in self._pending_scoped_tag_additions.items()
            if key[0] not in {"object","character","context"}
        }
        self._object_owned_keys=set()
        self._refresh_tag_lists(); self._refresh_confirmed_context(); self._update_pending_status()
        self.tabs.setCurrentWidget(self.image_data_tab)

    def _apply_current_tags(self,tags: TagCollection):
        """Apply the edited complete tag set without flattening organized storage."""
        annotation=self.record.annotation; desired=tags.normalized_set()
        containers=[annotation.sdxl_tags,annotation.uncategorized,annotation.visual_style.style_tags]
        containers.extend(annotation.global_tags.values()); containers.extend(annotation.scene.values())
        containers.extend(annotation.custom_groups.values())
        for character in annotation.characters: containers.extend(character.tags.values())
        for item in annotation.objects:containers.append(item.tags)
        for context in annotation.contexts:containers.append(context.tags)
        for values in containers: values[:]=[value for value in values if normalize_tag(value) in desired]
        annotation.objects=[
            item for item in annotation.objects
            if (
                item.identity_tag
                and item.identity_tag_id
                and normalize_tag(item.identity_tag) in desired
            )
        ]
        retained={normalize_tag(value) for value in annotation.all_structured_tags()}
        annotation.sdxl_tags=TagCollection([
            *annotation.sdxl_tags,
            *(value for value in tags if normalize_tag(value) not in retained),
        ]).to_list()

    def _apply_pending_scoped_tag_additions(self):
        desired=self.current_tags.normalized_set()
        for (
            owner_kind,owner_id,group,_tag_id
        ),value in self._pending_scoped_tag_additions.items():
            if normalize_tag(value) not in desired:
                continue
            add_related_tag_to_destination(
                self.record.annotation,
                display=value,
                owner_kind=owner_kind,
                owner_id=owner_id,
                owner_group=group,
            )

    def _image_overview_lines(self):
        path=self.record.image_path; standard=dict(self.record.annotation.standard_metadata)
        if self.report:
            for source in self.report.sources: standard.update(source.standard_metadata)
        try:
            size=path.stat().st_size
            size_text=f"{size / (1024*1024):.2f} MiB" if size>=1024*1024 else f"{size / 1024:.1f} KiB"
        except OSError: size_text="Unavailable"
        width=standard.get("ImageWidth"); height=standard.get("ImageHeight")
        if not width or not height:
            if self.preview_image is not None and not self.preview_image.isNull():
                width=self.preview_image.width(); height=self.preview_image.height()
            else:
                pixmap=QPixmap(str(path)); width=pixmap.width() if not pixmap.isNull() else None; height=pixmap.height() if not pixmap.isNull() else None
        lines=["Image","-----",f"File name: {path.name}",f"Location: {path.parent}",f"File size: {size_text}"]
        if width and height: lines.append(f"Dimensions: {width} × {height}")
        for key,label in (
            ("FileType","Format"),("MIMEType","MIME type"),("ColorType","Color type"),
            ("ColorSpace","Color space"),("BitsPerSample","Bits per sample"),
            ("ProfileDescription","Color profile"),("Orientation","Orientation"),
            ("Software","Generator software"),("CreatorTool","Creator tool"),
        ):
            if key in standard and standard[key] not in (None,""): lines.append(f"{label}: {standard[key]}")
        return lines

    def _populate(self,*,render=True,refresh_pending=True):
        self._ignored_suggestions=0
        self._automation_rejected_suggestions=set()
        self._absent_suggestions=0
        self._stale_source_suggestions=0
        self._suppressed_global_descriptions=set()
        confirmed_values=TagCollection([
            *self.record.annotation.all_structured_tags(),
            *self.current_tags.to_list(),
        ]).to_list()
        confirmed_tags={normalize_tag(value) for value in confirmed_values}
        identity_values=list(confirmed_values)
        if self.report:
            identity_values.extend(
                value
                for source in self.report.sources
                for value in source.tags
            )
        cached_findings=self.record.annotation.source.get(
            "candidate_findings",[]
        )
        if isinstance(cached_findings,list):
            identity_values.extend(
                str(finding.get("value") or "").strip()
                for finding in cached_findings
                if isinstance(finding,dict) and finding.get("kind")=="tag"
            )
        identity_values.extend(
            str(prediction.get("tag") or "").strip()
            for evidence in self.record.annotation.ai_evidence
            for prediction in evidence.typed_tags()
        )
        identity_plan=self._identity_service.plan(
            identity_values,
            stored_ids=self._working_tag_identities,
            confirmation=False,
        )
        self._suggestion_identity_entries={
            entry.normalized_value:entry for entry in identity_plan.entries
        }
        self._confirmed_suggestion_identity_ids={
            entry.stable_id
            for value in confirmed_values
            if (
                entry:=self._suggestion_identity_entries.get(
                    normalize_tag(value)
                )
            ) is not None and entry.stable_id and entry.canonical_display
        }
        self._confirmed_description_values = [
            *self.record.annotation.plain_language,
            *self._descriptions(self.prose.toPlainText()),
        ]
        confirmed_prose = {
            normalize_plain_language_exclusion(value)
            for value in self._confirmed_description_values
            if normalize_plain_language_exclusion(value)
        }
        ignored_prose=self._ignored_descriptions()
        finding_decisions=ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        )
        source_lines = ["Sources","-------"]
        generation_lines=[]
        boundary_issues=audit_typed_boundary(self.record,self.knowledge)
        if boundary_issues:
            source_lines.extend(("","Typed boundary review","---------------------"))
            source_lines.extend(f"{item.message} Value: {item.value}" for item in boundary_issues)
        raw_entries = []
        raw_keys = set()
        generation_count=0
        current_source_keys=current_evidence_keys_by_source(self.report)

        def description_available(
            value,source="Unknown",*,evidence_id="",source_fingerprint="",
        ):
            key=normalize_plain_language_exclusion(value)
            if key in confirmed_prose|ignored_prose:
                return False
            if finding_decisions.source_was_used_as_edited(
                "description",value,source,evidence_id=evidence_id,
                source_fingerprint=source_fingerprint,
            ):
                return False
            if (
                key in self.pending_dismissed_descriptions
                and key not in self.pending_restored_dismissed_descriptions
            ):
                return False
            if self._source_dismissed(
                "description",key,source,evidence_id=evidence_id,
            ):
                return False
            if key and key in self._global_ignored_descriptions():
                self._suppressed_global_descriptions.add(key)
                return False
            return True

        def add_raw(name, field, raw):
            if raw in (None, "", [], {}):
                return
            key = (str(name).casefold(), str(field).casefold(), repr(raw))
            if key in raw_keys:
                return
            raw_keys.add(key)
            label = str(name)
            if field:
                label += f" / {field}"
            raw_entries.append(f"{label}\n{'-' * len(label)}\n{format_raw_generation_payload(raw)}")

        if self.report:
            for source in self.report.sources:
                state = "Available" if source.available else "Not found"
                if source.error:
                    state = f"Could not read: {source.error}"
                source_lines.append(f"{source.name}: {state}")
                if source.details:
                    source_lines.append(f"{source.name}: {len(source.details):,} raw generation payload(s) retained")
                for generation_record in source.generation_records:
                    generation_count+=1
                    generation_lines.extend(("", format_generation_record(generation_record), ""))
                    add_raw(generation_record.source or source.name, generation_record.source_field, generation_record.raw)
                for field,raw in source.raw_generation_fields.items(): add_raw(source.name,field,raw)
                for field,raw in source.tag_fields.items(): add_raw(source.name,f"Tag field: {field}",raw)
                for field,raw in source.description_fields.items(): add_raw(source.name,f"Description field: {field}",raw)
                if source.raw_text:
                    add_raw(source.name,"Exact TXT",source.raw_text)
                if not source.generation_records:
                    for detail in source.details:
                        add_raw(source.name, "", detail)
                for value in source.tags:
                    if normalize_tag(value) not in confirmed_tags:
                        self._suggestion(value, "SDXL Tag", source.name)
                for raw_value in source.plain_language:
                    for value in split_plain_language(raw_value):
                        fingerprint=current_source_fingerprint(self.record,source.name)
                        if description_available(
                            value,source.name,source_fingerprint=fingerprint,
                        ):
                            self._suggestion(
                                value,"Plain Language Description",source.name,
                                source_fingerprint=fingerprint,
                            )
                for value in source.ambiguous_fragments:
                    source_lines.append(f"{source.name} ambiguous (not imported): {value}")
                for message in source.type_issues:
                    source_lines.append(f"{source.name} type warning: {message}")
        saved_evidence = self.record.annotation.source.get("evidence", [])
        if isinstance(saved_evidence, list):
            for source in saved_evidence:
                if not isinstance(source, dict):
                    continue
                name = str(source.get("name") or "Saved Metadata")
                references = source.get("generation_reference", [])
                if isinstance(references, list) and references:
                    source_lines.append(f"{name}: {len(references):,} saved raw generation payload(s)")
                    for value in references:
                        add_raw(name, "Saved Metadata", value)
                if source.get("raw_text"):
                    add_raw(name,"Saved exact TXT",source.get("raw_text"))
                for field,raw in source.get("tag_fields",{}).items() if isinstance(source.get("tag_fields"),dict) else []:
                    add_raw(name,f"Saved tag field: {field}",raw)
                for field,raw in source.get("description_fields",{}).items() if isinstance(source.get("description_fields"),dict) else []:
                    add_raw(name,f"Saved description field: {field}",raw)
        candidate_findings=cached_findings
        if isinstance(candidate_findings,list):
            for finding in candidate_findings:
                if not isinstance(finding,dict): continue
                value=str(finding.get("value") or "").strip()
                source_values=[str(source) for source in finding.get("sources",[]) if str(source).strip()]
                sources=", ".join(source_values) or "Cached source"
                if finding.get("kind")=="tag" and normalize_tag(value) not in confirmed_tags:
                    active_sources=current_candidate_sources(
                        value,"tag",source_values,current_source_keys,
                    )
                    if source_values and not active_sources:
                        self._stale_source_suggestions+=1
                        continue
                    sources=", ".join(active_sources) or "Cached source"
                    self._suggestion(value,"SDXL Tag",sources)
                elif finding.get("kind")=="description":
                    for sentence in split_plain_language(value):
                        active_sources=current_candidate_sources(
                            sentence,"description",source_values,
                            current_source_keys,
                        )
                        stale_count=len(source_values)-len(active_sources)
                        self._stale_source_suggestions+=stale_count
                        for source_name in active_sources or (
                            ["Cached source"] if not source_values else []
                        ):
                            fingerprint=finding_decisions.source_fingerprint(
                                source_name
                            )
                            if description_available(
                                sentence,source_name,
                                source_fingerprint=fingerprint,
                            ):
                                self._suggestion(
                                    sentence,"Plain Language Description",
                                    source_name,source_fingerprint=fingerprint,
                                )
                elif finding.get("kind")=="ambiguous":
                    reason=str(finding.get("reason") or "").strip()
                    source_lines.append(
                        f"Ambiguous cached source value from {sources} (not imported): "
                        f"{value}"+(f". {reason}" if reason else "")
                    )
                elif finding.get("kind") not in {"tag","description"}:
                    source_lines.append(f"Unsupported historical finding kind {finding.get('kind')!r} preserved: {value}")
        if self.record.annotation.generation_records:
            generation_lines.extend(("Structured generation settings",""))
            for generation_record in self.record.annotation.generation_records:
                generation_count+=1
                generation_lines.extend((format_generation_record(generation_record),""))
                add_raw(generation_record.source or "Saved generation reference", generation_record.source_field,
                        generation_record.raw)
        seen_ai_models=set()
        for evidence in self.record.annotation.ai_evidence:
            model_label=evidence.model_id or "Unknown model"
            model_key=model_label.casefold()
            if model_key not in seen_ai_models:
                source_lines.append(f"AI Metadata: {model_label}")
                seen_ai_models.add(model_key)
            for warning in evidence.type_issues():
                source_lines.append(f"AI type warning: {warning}")
            for prediction in evidence.typed_tags():
                value = str(prediction.get("tag") or "")
                key=normalize_tag(value)
                decision=evidence.decisions.get(key,"")
                if (value and decision not in {"accepted","rejected"}
                        and (
                            decision not in ImageFindingDecisionService.DEFERRED_VALUES
                            or key in self.pending_restored_dismissed_tags
                        )
                        and key not in confirmed_tags):
                    self._suggestion(
                        value,"AI Tag",evidence.model_id,
                        evidence_id=evidence.id,
                    )
            typed_prose=evidence.typed_plain_language()
            prose_decision=evidence.decisions.get("plain_language")
            restoring_prose=any(
                normalize_plain_language_exclusion(value)
                in self.pending_restored_dismissed_descriptions
                for value in split_plain_language(typed_prose)
            )
            if (typed_prose and prose_decision not in {"accepted","rejected"}
                    and (
                        prose_decision
                        not in ImageFindingDecisionService.DEFERRED_VALUES
                        or restoring_prose
                    )):
                for value in split_plain_language(typed_prose):
                    if description_available(
                        value,evidence.model_id or "Unknown model",
                        evidence_id=evidence.id,
                    ):
                        self._suggestion(
                            value,"AI Description",evidence.model_id,
                            evidence_id=evidence.id,
                        )
        if not generation_count:
            generation_lines.append(
                "No parsed generator data was found in JSON or image Metadata."
            )
        self.file_overview.setPlainText("\n".join(self._image_overview_lines()))
        self.sources.setPlainText("\n".join(source_lines) or "No source details are available.")
        self.generation_overview.setPlainText(
            "\n".join(generation_lines).strip()
            or "No parsed generator data is available."
        )
        self.raw_sources.setPlainText(
            "\n\n".join(raw_entries) or "No raw generation Metadata is available for this image."
        )
        available=len(self._suggested)
        parts=[]
        if self._absent_suggestions:
            parts.append(
                f"Suppressed {self._absent_suggestions:,} SDXL Tag(s) "
                "explicitly confirmed absent from this image."
            )
        if self._ignored_suggestions:
            parts.append(f"Suppressed {self._ignored_suggestions:,} SDXL Tag(s) ignored for this image.")
        if self._automation_rejected_suggestions:
            parts.append(
                f"Suppressed {len(self._automation_rejected_suggestions):,} SDXL Tag(s) "
                "rejected as incoming evidence by Automation."
            )
        if self._stale_source_suggestions:
            parts.append(f"Suppressed {self._stale_source_suggestions:,} stale cached source finding(s); Import External Evidence on the owning Ingest or Dataset page updates the saved source cache.")
        if self._suppressed_global_descriptions:
            parts.append(f"Suppressed {len(self._suppressed_global_descriptions):,} globally ignored description(s).")
        self._suggestion_status_detail=" ".join(parts)
        if render:
            self._render_suggestion_surfaces(refresh_pending=refresh_pending)
        self._refresh_ignored_items()
        self._update_suggestion_actions()
        self._refresh_character_view()

    def _pending_tag_selection_changed(self,values):
        if not self._restoring_pending_tag_selection:
            visible={
                normalize_tag(chip.value)
                for chip in self.pending_tag_chips._chips
            }
            selected={
                normalize_tag(value) for value in values
                if normalize_tag(value)
            }
            self._pending_tag_selection=(
                self._pending_tag_selection-visible
            )|selected
        self._update_suggestion_actions()
        # Selection refreshes the shared chip's accessible name; replace UI row
        # keys with the readable relationship identity after that refresh.
        for chip in self.pending_tag_chips._chips:
            suggestion=getattr(self,"_related_review_rows",{}).get(normalize_tag(chip.value))
            if suggestion is not None:
                chip.setAccessibleName(
                    f"Related Tag suggestion {suggestion.display}. {suggestion.scope}"
                )

    def _selected_pending_tag_values(self):
        return tuple(
            item["value"]
            for key,item in self._suggested.items()
            if key[0]=="tag" and key[1] in self._pending_tag_selection
        )

    def _pending_drag_values(self,value):
        key=normalize_tag(value)
        if key in self._pending_tag_selection:
            return tuple(self.pending_tag_chips.selected_values())
        return (value,)

    def _update_suggestion_actions(self,*_):
        selected_tags=self._selected_pending_tag_values()
        selected_related=(
            self._selected_related_suggestions()
            if hasattr(self,"related_tag_editors") else ()
        )
        selected_descriptions=self.description_suggestions.selectedItems()
        has_confirmed_descriptions=bool(
            self._descriptions(self.prose.toPlainText())
        )
        self.confirm_description_suggestions.setText(
            "Use Selected…" if has_confirmed_descriptions else "Add Selected"
        )
        self.confirm_description_suggestions.setToolTip(
            "Compare the selected findings with the confirmed descriptions, "
            "then use the new description or keep the existing one."
            if has_confirmed_descriptions else
            "Stage the selected descriptions as confirmed Image Knowledge."
        )
        available=len(self._suggested)
        selected_count=(
            len(selected_tags)+len(selected_related)+len(selected_descriptions)
        )
        status_parts=[]
        review_count=available+getattr(self,"_related_suggestion_count",0)
        if review_count:
            status_parts.append(
                f"{review_count:,} finding(s) to review; "
                f"{selected_count:,} selected."
            )
        if self._suggestion_status_detail:
            status_parts.append(self._suggestion_status_detail)
        self.suggestion_status.setText(" ".join(status_parts))
        self.suggestion_status.setVisible(bool(status_parts))
        tag_count=sum(key[0]=="tag" for key in self._suggested)
        description_count=available-tag_count
        self._update_review_workspace_status(
            tag_count=tag_count,description_count=description_count,
        )
        self.pending_tag_chips.scroll.setAccessibleDescription(
            f"{len(self.pending_tag_chips._chips):,} pending SDXL Tags; {len(selected_tags)+len(selected_related):,} "
            "selected. Use Click, Ctrl+Click, or Shift+Click."
        )
        self.description_suggestion_status.setText(
            f"{description_count:,} pending Plain Language Description"
            f"{'s' if description_count!=1 else ''}; "
            f"{len(selected_descriptions):,} selected."
            if description_count else ""
        )
        self.description_suggestions.setAccessibleDescription(
            f"{description_count:,} pending Plain Language Descriptions; "
            f"{len(selected_descriptions):,} selected. Use Click, Ctrl+Click, "
            "or Shift+Click."
        )
        has_global=bool(self._global_ignored_descriptions())
        self.confirm_tag_suggestions.setEnabled(bool(
            selected_tags or selected_related
        ))
        self.dismiss_tag_suggestions.setEnabled(bool(selected_tags))
        self.ignore_tag_suggestions.setEnabled(bool(self._selected_review_tag_values()))
        tag_count=len(self._visible_pending_tag_items())
        self.dismiss_tag_suggestions.setVisible(bool(tag_count))
        self.ignore_tag_suggestions.setVisible(bool(
            tag_count or int(getattr(self,"_visible_related_tag_count",0))
        ))
        self.pending_tag_footer.setVisible(bool(
            tag_count or int(getattr(self,"_visible_related_tag_count",0))
        ))
        self.remove_pending_from_training_txt_button.setEnabled(
            self.source_correction_available and len(selected_tags)==1
            and bool(self._training_txt_source_value(selected_tags[0]))
        )
        self.remove_pending_from_metadata_button.setEnabled(
            self.source_correction_available and len(selected_tags)==1
            and bool(self._metadata_source_value(selected_tags[0],"tag"))
        )
        source_buttons=(
            self.remove_pending_from_training_txt_button,
            self.remove_pending_from_metadata_button,
        )
        for button in source_buttons:
            button.setVisible(button.isEnabled())
        self.pending_source_actions_panel.setVisible(
            any(button.isEnabled() for button in source_buttons)
        )
        if hasattr(self,"inspect_related_tag_button"):
            self.inspect_related_tag_button.setVisible(bool(selected_related))
            self.inspect_related_tag_button.setEnabled(bool(selected_related))
        for button in (
            self.confirm_description_suggestions,
            self.dismiss_description_suggestions,
            self.ignore_description_suggestions,
        ):
            button.setEnabled(bool(selected_descriptions))
        self.pending_description_actions_panel.setVisible(bool(description_count))
        self.description_suggestion_status.setVisible(bool(description_count))
        self.ignore_everywhere_action.setEnabled(
            bool(selected_descriptions) and self.settings is not None
        )
        self.manage_description_ignores_action.setEnabled(has_global and self.settings is not None)
        self.ignore_button.setEnabled(
            (bool(selected_descriptions) or has_global)
            and self.settings is not None
        )
        self.ignore_button.setVisible(
            bool(selected_descriptions) and self.settings is not None
        )
        selected_description=(
            self._suggestion_value(selected_descriptions[0])
            if len(selected_descriptions)==1 else ""
        )
        self.remove_pending_description_from_training_txt_button.setEnabled(
            self.source_correction_available and bool(selected_description)
            and bool(self._training_txt_description_source_value(
                selected_description
            ))
        )
        self.remove_pending_description_from_metadata_button.setEnabled(
            self.source_correction_available and bool(selected_description)
            and bool(self._metadata_source_value(
                selected_description,"description"
            ))
        )
        for button in (
            self.remove_pending_description_from_training_txt_button,
            self.remove_pending_description_from_metadata_button,
        ):
            button.setVisible(button.isEnabled())
        if hasattr(self,"pending_status"): self._update_pending_status()

    def _training_txt_source_value(self,value: str) -> str:
        """Return the live TXT spelling for one selected normalized Tag."""
        wanted=normalize_tag(value)
        if not wanted or self.report is None:
            return ""
        matches=[]
        for source in getattr(self.report,"sources",()):
            if (
                str(getattr(source,"name","")).casefold()!="txt caption"
                or not getattr(source,"available",False)
                or getattr(source,"error","")
            ):
                continue
            for raw in getattr(source,"tags",()):
                exact=str(raw).lstrip("\ufeff").strip()
                if normalize_tag(exact)==wanted:
                    matches.append(exact)
        return matches[0] if len(matches)==1 else ""

    def _training_txt_description_source_value(self,value: str) -> str:
        """Return one uniquely matching live TXT description spelling."""
        wanted=normalize_plain_language_exclusion(value)
        if not wanted or self.report is None:
            return ""
        matches=[]
        for source in getattr(self.report,"sources",()):
            if (
                str(getattr(source,"name","")).casefold()!="txt caption"
                or not getattr(source,"available",False)
                or getattr(source,"error","")
            ):
                continue
            for raw in getattr(source,"plain_language",()):
                exact=str(raw).lstrip("\ufeff").strip()
                if normalize_plain_language_exclusion(exact)==wanted:
                    matches.append(exact)
        return matches[0] if len(matches)==1 else ""

    def _metadata_source_value(self,value: str,value_kind: str) -> str:
        """Return one exact value from an app-managed XMP source field."""
        if self.report is None:
            return ""
        normalizer=(
            normalize_tag if value_kind=="tag"
            else normalize_plain_language_exclusion
        )
        wanted=normalizer(value)
        if not wanted:
            return ""
        matches=[]
        for source in getattr(self.report,"sources",()):
            if (
                str(getattr(source,"name","")).casefold()!="image metadata"
                or not getattr(source,"available",False)
                or getattr(source,"error","")
            ):
                continue
            if value_kind=="tag":
                raw=getattr(source,"tag_fields",{}).get("Subject")
                values=(raw,) if isinstance(raw,str) else raw
                if not isinstance(values,(list,tuple)):
                    continue
                candidates=[str(item) for item in values]
            else:
                raw=getattr(source,"description_fields",{}).get("Description")
                if not isinstance(raw,str) or not raw:
                    continue
                separator="\r\n\r\n" if "\r\n\r\n" in raw else "\n\n"
                candidates=[
                    part.strip()
                    for part in raw.replace("\r\n","\n").split("\n\n")
                ]
                if any(not part for part in candidates):
                    continue
                if separator.join(candidates)!=raw:
                    continue
            for exact in candidates:
                if normalizer(exact)==wanted:
                    matches.append(exact)
        return matches[0] if len(matches)==1 else ""

    def _request_pending_training_txt_correction(self):
        selected=self._selected_pending_tag_values()
        if len(selected)==1:
            self._request_training_txt_correction(selected[0])

    def _request_confirmed_training_txt_correction(self):
        selected=self.tag_chips.selected_values()
        if len(selected)==1:
            self._request_training_txt_correction(selected[0])

    def _request_pending_metadata_tag_correction(self):
        selected=self._selected_pending_tag_values()
        if len(selected)==1:
            self._request_metadata_correction(selected[0],"tag")

    def _request_confirmed_metadata_tag_correction(self):
        selected=self.tag_chips.selected_values()
        if len(selected)==1:
            self._request_metadata_correction(selected[0],"tag")

    def _selected_pending_description_value(self) -> str:
        selected=self.description_suggestions.selectedItems()
        return self._suggestion_value(selected[0]) if len(selected)==1 else ""

    def _request_pending_training_txt_description_correction(self):
        value=self._selected_pending_description_value()
        exact=self._training_txt_description_source_value(value)
        if exact:
            self._request_source_correction(
                exact,source_kind="training_txt",value_kind="description",
            )

    def _request_pending_metadata_description_correction(self):
        value=self._selected_pending_description_value()
        if value:
            self._request_metadata_correction(value,"description")

    def _request_training_txt_correction(self,value: str):
        exact_value=self._training_txt_source_value(value)
        if not exact_value:
            QMessageBox.information(
                self,"Training TXT source is unavailable",
                "The selected Tag is not present in the current adjacent "
                "Training TXT evidence. Nothing was changed.",
            )
            return
        self._request_source_correction(
            exact_value,source_kind="training_txt",value_kind="tag",
        )

    def _request_metadata_correction(self,value: str,value_kind: str):
        exact_value=self._metadata_source_value(value,value_kind)
        if not exact_value:
            QMessageBox.information(
                self,"Managed Metadata source is unavailable",
                "The selected value is not present in the current app-managed "
                "Metadata field. Nothing was changed.",
            )
            return
        self._request_source_correction(
            exact_value,source_kind="managed_metadata",
            value_kind=value_kind,source_field=(
                "XMP:Subject" if value_kind=="tag" else "XMP:Description"
            ),
        )

    def _request_source_correction(
        self,value: str,*,source_kind: str,value_kind: str,
        source_field: str="",
    ):
        if not self._save_before_source_correction():
            return
        workspace=(
            "descriptions" if value_kind=="description" else "tag_manager"
        )
        self.source_correction_scope={
            "image":str(self.record.image_path),
            "source_kind":source_kind,
            "value_kind":value_kind,
            "value":value,
            "meaning":"source_only",
            "origin":"universal_editor."+workspace,
            "return_context":{
                "image":str(self.record.image_path),
                "workspace":workspace,
                "selected_value":value,
                "queue_index":self.queue_index,
                "queue_count":self.queue_count,
            },
        }
        if source_field:
            self.source_correction_scope["source_field"]=source_field
        self.done(self.REQUEST_SOURCE_CORRECTION)

    def _save_before_source_correction(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        choice=QMessageBox.question(
            self,"Save changes before correcting source evidence?",
            "The source-correction preview must compare current source evidence "
            "with the currently saved Image Knowledge. Save the staged image-data "
            "changes and continue?",
            QMessageBox.Save|QMessageBox.Cancel,QMessageBox.Save,
        )
        return choice==QMessageBox.Save and self._save_record()

    def _suggestion(
        self,value,kind,source,*,evidence_id="",source_fingerprint="",
    ):
        suggestion_kind="tag" if "tag" in kind.casefold() else "description"
        if suggestion_kind=="tag" and has_automation_placeholder(value):
            return
        raw_key=(
            normalize_tag(value) if suggestion_kind=="tag"
            else normalize_plain_language_exclusion(value)
        )
        if self._source_dismissed(
            suggestion_kind,raw_key,source,evidence_id=evidence_id,
        ):
            return
        if suggestion_kind=="tag":
            identity=getattr(self,"_suggestion_identity_entries",{}).get(
                raw_key
            )
            if (
                identity is not None
                and identity.stable_id
                and identity.canonical_display
            ):
                if identity.stable_id in getattr(
                    self,"_confirmed_suggestion_identity_ids",set()
                ):
                    return
                value=identity.canonical_display
            if self.automation_engine.is_evidence_rejected(value):
                self._automation_rejected_suggestions.add(normalize_tag(value))
                return
        key = (
            suggestion_kind,
            normalize_tag(value) if suggestion_kind=="tag"
            else normalize_plain_language_exclusion(value),
        )
        if key[0]=="tag" and key[1] in self._confirmed_absent_tags():
            self._absent_suggestions+=1
            return
        if key[0]=="tag" and key[1] in self._ignored_tags():
            self._ignored_suggestions+=1
            return
        if (
            key[0]=="tag"
            and key[1] in self.pending_dismissed_tags
            and key[1] not in self.pending_restored_dismissed_tags
        ):
            return
        if key[1]!=raw_key and self._source_dismissed(
            key[0],key[1],source,evidence_id=evidence_id,
        ):
            return
        if key in self._suggested:
            item=self._suggested[key]
            item["source"]=self._merge_sources(item["source"],source)
            item["kind"]=self._merge_sources(item["kind"],kind)
            attribution={
                "source":str(source or "Unknown").strip() or "Unknown",
                "evidence_id":str(evidence_id or "").strip(),
                "source_fingerprint":str(source_fingerprint or "").strip(),
            }
            if attribution not in item.get("attributions",[]):
                item.setdefault("attributions",[]).append(attribution)
            if evidence_id:
                item["evidence_ids"]=list(dict.fromkeys([
                    *item.get("evidence_ids",[]),str(evidence_id),
                ]))
            return
        self._suggested[key]={
            "value":str(value).strip(),
            "kind":str(kind),
            "source":self._merge_sources(source),
            "evidence_ids":[str(evidence_id)] if evidence_id else [],
            "attributions":[{
                "source":str(source or "Unknown").strip() or "Unknown",
                "evidence_id":str(evidence_id or "").strip(),
                "source_fingerprint":str(source_fingerprint or "").strip(),
            }],
        }

    def _render_suggestion_surfaces(
        self,selected_tags=(),selected_descriptions=(),
        tag_scroll=0,description_scroll=0,current_description=None,
        *,refresh_pending=True,
    ):
        if refresh_pending:
            self._refresh_pending_tag_surface(
                selected=selected_tags,scroll=tag_scroll,
            )
        self.description_suggestions.clear()
        current_item=None
        selected_description_keys={
            normalize_plain_language_exclusion(value)
            for value in selected_descriptions
        }
        for key,item in self._suggested.items():
            if key[0]!="description":
                continue
            source=item["source"]
            review=description_review_classification(
                item["value"],getattr(
                    self,"_confirmed_description_values",(),
                ),
            )
            row=QTreeWidgetItem((
                item["value"],self._source_label(source),review,
            ))
            row.setData(0,Qt.UserRole,item["value"])
            row.setData(0,Qt.UserRole+1,key)
            row.setData(1,Qt.UserRole,source)
            row.setToolTip(0,item["value"])
            row.setToolTip(1,source)
            row.setToolTip(2,(
                "This finding substantially overlaps confirmed wording. "
                "Compare it deliberately; CollectiGen will not merge or "
                "discard it automatically."
                if review=="Alternative wording" else
                "This finding does not meet the conservative lexical-overlap "
                "threshold for alternative wording."
            ))
            self.description_suggestions.addTopLevelItem(row)
            if key[1] in selected_description_keys:
                row.setSelected(True)
            if key==current_description:
                current_item=row
        if current_item is not None:
            self.description_suggestions.setCurrentItem(current_item)
        QTimer.singleShot(
            0,lambda value=description_scroll:
            self.description_suggestions.verticalScrollBar().setValue(value)
        )
        tag_count=sum(key[0]=="tag" for key in self._suggested)
        description_count=len(self._suggested)-tag_count
        # Retain the review location, but give confirmed prose the unused space.
        self.pending_descriptions_group.setVisible(True)
        self.description_suggestions.setVisible(bool(description_count))
        self.description_suggestion_empty.setVisible(not description_count)
        self.pending_descriptions_group.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Preferred if description_count else QSizePolicy.Maximum,
        )
        self.description_tab.layout().setStretchFactor(
            self.pending_descriptions_group,1 if description_count else 0,
        )

    def _refresh_pending_tag_surface(self,*_,selected=None,scroll=None):
        if not hasattr(self,"pending_tag_chips"):
            return
        if selected is None:
            selected=tuple(self.pending_tag_chips.selected_values())
        if scroll is None:
            scroll=self.pending_tag_chips.scroll.verticalScrollBar().value()
        focus=QApplication.focusWidget()
        focused_value=next((
            chip.value for chip in self.pending_tag_chips._chips
            if focus is chip or (focus is not None and chip.isAncestorOf(focus))
        ),"")
        items={key[1]:item for key,item in self._suggested.items() if key[0]=="tag"}
        # Incoming findings target image-wide facts. Share their review chip
        # with matching relationship support, but never collapse another owner
        # or destination merely because it uses the same displayed Tag.
        incoming_by_id={
            record.id:item["value"]
            for item in items.values()
            if self.knowledge is not None
            and (record:=self.knowledge.find(item["value"])) is not None
        }
        values=[item["value"] for item in items.values()]
        matches=self._conflicts_for_tags([
            *self._current_confirmed_tags(),*values,
        ])
        all_conflicts=self._conflict_index(matches)
        match_indicators=self._conflict_state_indicators(matches)
        pending_keys=set(items)
        pending_conflicts={
            key:messages for key,messages in all_conflicts.items()
            if key in pending_keys
        }
        self._pending_conflicts=pending_conflicts
        items=self._visible_pending_tag_items()
        mode=str(self.pending_tag_filter.currentData() or "all")
        shown=[
            item["value"]
            for key,item in items.items()
            if (
                mode=="all"
                or (mode=="conflicts" and key in pending_conflicts)
                or (mode=="clear" and key not in pending_conflicts)
            )
        ]
        tooltips={}
        for value in shown:
            key=normalize_tag(value); item=items[key]
            details=[
                "Status: Pending review",
                f"Suggested by: {item['source']}",
                f"Evidence: {item['kind']}",
            ]
            if key in pending_conflicts:
                details.extend((match_indicators[key][0]+":",*pending_conflicts[key]))
            tooltips[value]="\n".join(details)
        # Keep owner-specific proposals independently selectable in one flow.
        # Row keys are UI-only; all mutations resolve back to exact evidence.
        related_rows={}
        labels={}
        enabled={}
        stable_ids={
            item["value"]:record.id
            for item in items.values()
            if self.knowledge is not None
            and (record:=self.knowledge.find(item["value"])) is not None
        }
        separate_related_count=0
        for suggestion in getattr(self,"related_tag_suggestions",()):
            incoming=incoming_by_id.get(suggestion.tag_id)
            if (
                incoming is not None
                and suggestion.owner_kind=="image"
                and not suggestion.owner_id
                and suggestion.owner_group in {"","sdxl_tags"}
            ):
                if incoming in tooltips:
                    tooltips[incoming]+="\n\n"+self._related_tag_tooltip(suggestion)
                    tooltips[incoming]+="\nSource: "+suggestion.source
                continue
            separate_related_count+=1
            if not self._tag_matches_shared_filters(suggestion.display,suggestion.tag_id):
                continue
            value="related-review:"+json.dumps((
                suggestion.tag_id,suggestion.owner_kind,suggestion.owner_id,
                suggestion.owner_group,suggestion.path_label,suggestion.source,
            ),ensure_ascii=False,separators=(",",":"))
            key=normalize_tag(value)
            shown.append(value)
            related_rows[key]=suggestion
            labels[key]=suggestion.display
            stable_ids[value]=suggestion.tag_id
            tooltips[value]=self._related_tag_tooltip(suggestion)
            enabled[value]=suggestion.eligible
        self._related_suggestion_count=separate_related_count
        self._visible_related_tag_count=len(related_rows)
        self._related_review_rows=related_rows
        self.related_tag_editors=[self.pending_tag_chips] if related_rows else []
        self._related_editor_suggestions={id(self.pending_tag_chips):tuple(related_rows.values())}
        self._restoring_pending_tag_selection=True
        try:
            self.pending_tag_chips.set_tags(
                shown,self.knowledge,self.categories,
                stable_ids=stable_ids,
                enabled=enabled,
                tooltips=tooltips,
                state_indicators={
                    value:match_indicators[normalize_tag(value)]
                    for value in shown
                    if normalize_tag(value) in pending_conflicts
                },
            )
            requested={
                normalize_tag(value) for value in selected
                if normalize_tag(value)
            }
            self.pending_tag_chips.select_values(requested)
            self._pending_tag_selection={
                normalize_tag(value) for value in self.pending_tag_chips.selected_values()
            }
            for chip in self.pending_tag_chips._chips:
                label=labels.get(normalize_tag(chip.value),chip.value)
                chip.label.setText(label)
                if normalize_tag(chip.value) in related_rows:
                    suggestion=related_rows[normalize_tag(chip.value)]
                    chip.setAccessibleName(f"Related Tag suggestion {label}. {suggestion.scope}")
        finally:
            self._restoring_pending_tag_selection=False
        QTimer.singleShot(
            0,lambda value=scroll:
            self.pending_tag_chips.scroll.verticalScrollBar().setValue(value)
        )
        if focused_value and self.pending_tag_chips.index_of(
            focused_value
        )>=0:
            QTimer.singleShot(
                0,lambda value=focused_value:
                self.pending_tag_chips.focus_tag(value)
            )
        self._set_conflict_filter_labels(
            self.pending_tag_filter,len(values),len(pending_conflicts)
        )
        self._refresh_confirmed_identity_repair()
        conflict_matches=[
            match for match in matches
            if any(normalize_tag(value) in pending_keys
                   for value in getattr(match,"found_tags",()))
        ]
        if conflict_matches:
            details=" ".join(
                dict.fromkeys(str(match.message) for match in conflict_matches)
            )
            summary,definite_count,_ownership_count=self._conflict_review_summary(conflict_matches)
            self.pending_tag_conflict_status.setText(
                f"{'⚠ Attention' if definite_count else 'i'}: {summary}, "
                f"{len(pending_conflicts):,} pending tag(s). {details}"
            )
        else:
            self.pending_tag_conflict_status.setText("No pending conflicts.")
        self.pending_tag_conflict_status.setAccessibleName(
            self.pending_tag_conflict_status.text()
        )
        self.pending_tags_layout.setAlignment(Qt.AlignTop)
        self.pending_tag_filter_panel.setVisible(bool(items))
        self.pending_tag_chips.setVisible(bool(shown))
        self._refresh_pending_tag_empty_state()
        self._update_suggestion_actions()

    @staticmethod
    def _merge_sources(*sources) -> str:
        values=[]
        for source in sources:
            for raw in str(source or "Unknown").split(", "):
                clean=raw.strip()
                if clean and clean not in values: values.append(clean)
        return ", ".join(values or ["Unknown"])

    @staticmethod
    def _source_label(source) -> str:
        aliases={
            "txt caption":"TXT","json sidecar":"JSON","image metadata":"Metadata",
            "cached source":"Cached","automatic rule":"Rule",
        }
        for model_id,(label,_folder,_summary) in PLAIN_LANGUAGE_MODELS.items():
            aliases[model_id.casefold()]=label
        for model_id,(label,_folder) in WD_TAGGER_MODELS.items():
            aliases[model_id.casefold()]=label.replace(" Large v3","").replace(" v3","")
        values=[]
        for raw in ImageDataEditor._merge_sources(source).split(", "):
            clean=raw.strip(); label=aliases.get(clean.casefold())
            if label is None and "/" in clean: label="AI model"
            if label is None: label=clean or "Unknown"
            if label not in values: values.append(label)
        return " + ".join(values)

    @staticmethod
    def _suggestion_value(item) -> str:
        return str(item.data(0,Qt.UserRole) or item.text(0))

    def _persisted_ignored_tags(self):
        service=ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        )
        return {
            normalize_tag(value)
            for value in self.record.annotation.source.get("rejected_tags",[])
            if service._tag_identity(value) in service.ignored_tag_identities()
            and normalize_tag(value)
        }|{
            normalize_tag(tag)
            for evidence in self.record.annotation.ai_evidence
            for tag,decision in evidence.decisions.items()
            if evidence.kind=="sdxl_tags" and decision=="rejected"
            and normalize_tag(tag)
        }

    def _confirmed_absent_tags(self):
        return {
            normalize_tag(value)
            for value in self.record.annotation.confirmed_absent
            if normalize_tag(value)
        }-set(self.pending_restored_absent_tags)

    def _ignored_tags(self):
        return (
            self._persisted_ignored_tags()|set(self.pending_ignored_tags)
        )-set(self.pending_restored_tags)

    def _persisted_ignored_descriptions(self):
        return ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        ).ignored_description_keys()

    def _ignored_descriptions(self):
        return (
            self._persisted_ignored_descriptions()
            |set(self.pending_ignored_descriptions)
        )-set(self.pending_restored_descriptions)

    def _persisted_dismissed_findings(self):
        return ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        ).deferred_findings()

    def _source_dismissed(self,kind,key,source,*,evidence_id=""):
        restored=(
            self.pending_restored_dismissed_tags
            if kind=="tag" else self.pending_restored_dismissed_descriptions
        )
        if key in restored:
            return False
        requested={
            value.strip().casefold()
            for value in str(source or "Unknown").split(", ")
            if value.strip()
        } or {"unknown"}
        service=ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        )
        return all(
            service.source_is_deferred(
                kind,key,requested_source,evidence_id=evidence_id,
            )
            for requested_source in requested
        )

    def _persisted_dismissed_tags(self):
        service=ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        )
        return {
            normalize_tag(item["value"])
            for item in service.deferred_findings()
            if item["kind"]=="tag" and normalize_tag(item["value"])
        }|{
            normalize_tag(tag)
            for evidence in self.record.annotation.ai_evidence
            for tag,decision in evidence.decisions.items()
            if evidence.kind=="sdxl_tags"
            and decision in service.DEFERRED_VALUES and normalize_tag(tag)
        }

    def _dismissed_tags(self):
        return (
            self._persisted_dismissed_tags()|set(self.pending_dismissed_tags)
        )-set(self.pending_restored_dismissed_tags)

    def _persisted_dismissed_descriptions(self):
        return ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        ).deferred_description_keys()

    def _dismissed_descriptions(self):
        return (
            self._persisted_dismissed_descriptions()
            |set(self.pending_dismissed_descriptions)
        )-set(self.pending_restored_dismissed_descriptions)

    def _global_ignored_descriptions(self):
        return {
            normalize_plain_language_exclusion(value)
            for value in getattr(self.settings,"ignored_plain_language_sentences",[])
            if normalize_plain_language_exclusion(value)
        }

    def _image_review_decision_values(self):
        ignored_tags={normalize_tag(value):str(value) for value in self.record.annotation.source.get("rejected_tags",[]) if normalize_tag(value)}
        ignored_tags.update(self.pending_ignored_tags)
        ignored_descriptions={
            normalize_plain_language_exclusion(value):value
            for raw in self.record.annotation.source.get("rejected_descriptions",[])
            for value in split_plain_language(raw)
            if normalize_plain_language_exclusion(value)
        }
        ignored_descriptions.update(self.pending_ignored_descriptions)
        dismissed_tags={
            normalize_tag(item["value"]):str(item["value"])
            for item in self._persisted_dismissed_findings()
            if item["kind"]=="tag" and normalize_tag(item["value"])
        }
        dismissed_tags.update(
            (key,str(item.get("value") or key))
            for key,item in self.pending_dismissed_tags.items()
        )
        dismissed_descriptions={
            normalize_plain_language_exclusion(value):value
            for item in self._persisted_dismissed_findings()
            if item["kind"]=="description"
            for value in split_plain_language(item["value"])
            if normalize_plain_language_exclusion(value)
        }
        dismissed_descriptions.update(
            (key,str(item.get("value") or key))
            for key,item in self.pending_dismissed_descriptions.items()
        )
        for evidence in self.record.annotation.ai_evidence:
            for prediction in evidence.typed_tags():
                value=str(prediction.get("tag") or "").strip(); key=normalize_tag(value)
                decision=evidence.decisions.get(key)
                if key and decision=="rejected": ignored_tags.setdefault(key,value)
                elif (
                    key and decision
                    in ImageFindingDecisionService.DEFERRED_VALUES
                ):
                    dismissed_tags.setdefault(key,value)
            prose_decision=evidence.decisions.get("plain_language")
            if (
                evidence.typed_plain_language()
                and prose_decision in {
                    "rejected",*ImageFindingDecisionService.DEFERRED_VALUES,
                }
            ):
                target=(
                    ignored_descriptions
                    if prose_decision=="rejected" else dismissed_descriptions
                )
                for value in split_plain_language(evidence.typed_plain_language()):
                    key=normalize_plain_language_exclusion(value)
                    if key: target.setdefault(key,value)
        for key in self.pending_restored_tags:
            ignored_tags.pop(key,None)
        for key in self.pending_restored_descriptions:
            ignored_descriptions.pop(key,None)
        for key in self.pending_restored_dismissed_tags:
            dismissed_tags.pop(key,None)
        for key in self.pending_restored_dismissed_descriptions:
            dismissed_descriptions.pop(key,None)
        rows=[]
        for decision,kind,values in (
            ("ignored","SDXL Tag",ignored_tags),
            ("ignored","Plain Language Description",ignored_descriptions),
            ("dismissed","SDXL Tag",dismissed_tags),
            ("dismissed","Plain Language Description",dismissed_descriptions),
        ):
            label=f"{'Ignored' if decision=='ignored' else 'Decide Later'} {kind}"
            for key,value in values.items():
                rows.append(((decision,kind,key),value,label))
        return rows

    def _refresh_ignored_items(self):
        if not hasattr(self,"ignored_items"): return
        selected={
            item.data(0,Qt.UserRole)
            for item in self.ignored_items.selectedItems()
        }
        current=self.ignored_items.currentItem()
        current_key=current.data(0,Qt.UserRole) if current is not None else None
        scroll=self.ignored_items.verticalScrollBar().value()
        rows=self._image_review_decision_values(); self.ignored_items.clear()
        for identity,value,label in sorted(rows,key=lambda item:(item[2].casefold(),item[1].casefold())):
            item=QTreeWidgetItem((value,label))
            item.setData(0,Qt.UserRole,identity)
            item.setToolTip(0,value)
            item.setToolTip(1,label)
            self.ignored_items.addTopLevelItem(item)
            if identity in selected:
                item.setSelected(True)
            if identity==current_key:
                self.ignored_items.setCurrentItem(item)
        count=self.ignored_items.topLevelItemCount()
        self.ignored_items.setVisible(bool(count)); self.ignored_empty.setVisible(not count)
        self.global_ignores_button.setVisible(bool(self._global_ignored_descriptions()))
        QTimer.singleShot(0,lambda value=scroll:self.ignored_items.verticalScrollBar().setValue(value))
        self._update_ignored_actions()

    def _update_ignored_actions(self,*_):
        if not hasattr(self,"ignored_items"): return
        selected=self.ignored_items.selectedItems()
        count=self.ignored_items.topLevelItemCount()
        self.ignored_status.setText(
            f"{count:,} deferred or ignored item(s); {len(selected):,} selected."
            if count else ""
        )
        self.ignored_status.setVisible(bool(count))
        self.ignored_items.setAccessibleDescription(
            f"{count:,} deferred or ignored items available; {len(selected):,} selected. "
            "Select rows with Click, Ctrl+Click, or Shift+Click."
        )
        self.restore_ignored_button.setEnabled(bool(selected))

    def _restore_selected_ignores(self):
        restore_tags=set(); restore_descriptions=set()
        restore_dismissed_tags=set(); restore_dismissed_descriptions=set()
        for item in self.ignored_items.selectedItems():
            decision,kind,key=item.data(0,Qt.UserRole)
            if decision=="ignored":
                target=restore_tags if kind=="SDXL Tag" else restore_descriptions
            else:
                target=restore_dismissed_tags if kind=="SDXL Tag" else restore_dismissed_descriptions
            target.add(str(key))
        if restore_tags or restore_descriptions:
            self._restore_image_ignores(restore_tags,restore_descriptions,refresh=False)
        if restore_dismissed_tags or restore_dismissed_descriptions:
            self._restore_image_dismissals(
                restore_dismissed_tags,restore_dismissed_descriptions,refresh=False,
            )
        if any((restore_tags,restore_descriptions,restore_dismissed_tags,restore_dismissed_descriptions)):
            self._refresh_suggestions()
            self._update_pending_status()

    def _restore_image_ignores(self,restore_tags,restore_descriptions,*,refresh=True):
        restore_tags={normalize_tag(value) for value in restore_tags if normalize_tag(value)}
        restore_descriptions={
            normalize_plain_language_exclusion(value)
            for value in restore_descriptions
            if normalize_plain_language_exclusion(value)
        }
        persisted_tags=self._persisted_ignored_tags()
        persisted_descriptions=self._persisted_ignored_descriptions()
        for key in restore_tags: self.pending_ignored_tags.pop(key,None)
        for key in restore_descriptions: self.pending_ignored_descriptions.pop(key,None)
        self.pending_restored_tags.update(restore_tags & persisted_tags)
        self.pending_restored_descriptions.update(restore_descriptions & persisted_descriptions)
        if refresh:
            self._refresh_suggestions()
            self._update_pending_status()
        return True

    def _restore_image_dismissals(self,restore_tags,restore_descriptions,*,refresh=True):
        restore_tags={normalize_tag(value) for value in restore_tags if normalize_tag(value)}
        restore_descriptions={
            normalize_plain_language_exclusion(value)
            for value in restore_descriptions
            if normalize_plain_language_exclusion(value)
        }
        persisted_tags=self._persisted_dismissed_tags()
        persisted_descriptions=self._persisted_dismissed_descriptions()
        for key in restore_tags:
            self.pending_dismissed_tags.pop(key,None)
        for key in restore_descriptions:
            self.pending_dismissed_descriptions.pop(key,None)
        self.pending_restored_dismissed_tags.update(restore_tags & persisted_tags)
        self.pending_restored_dismissed_descriptions.update(
            restore_descriptions & persisted_descriptions
        )
        if refresh:
            self._refresh_suggestions()
            self._update_pending_status()
        return True

    def _dismiss_selected_suggestions(self):
        changed=self._dismiss_selected_tag_suggestions(refresh=False)
        changed=self._dismiss_selected_description_suggestions(
            refresh=False
        ) or changed
        if changed:
            self._refresh_suggestions()
            self._update_pending_status()

    def _dismiss_selected_tag_suggestions(self,*,refresh=True):
        changed=False
        for value in self._selected_pending_tag_values():
            key=normalize_tag(value)
            item=self._suggested.get(("tag",key),{})
            if key:
                self.pending_restored_dismissed_tags.discard(key)
                self.pending_dismissed_tags[key]={
                    "value":value,
                    "source":str(item.get("source") or "Unknown"),
                    "evidence_ids":list(item.get("evidence_ids",[])),
                }
                changed=True
        if changed and refresh:
            self._refresh_suggestions()
            self._update_pending_status()
        return changed

    def _dismiss_selected_description_suggestions(self,*,refresh=True):
        changed=False
        for item in self.description_suggestions.selectedItems():
            value=self._suggestion_value(item)
            source=str(item.data(1,Qt.UserRole) or item.text(1) or "Unknown")
            suggestion_key=item.data(0,Qt.UserRole+1)
            suggestion=self._suggested.get(suggestion_key,{})
            for sentence in split_plain_language(value):
                key=normalize_plain_language_exclusion(sentence)
                if key:
                    self.pending_restored_dismissed_descriptions.discard(key)
                    self.pending_dismissed_descriptions[key]={
                        "value":sentence,"source":source,
                        "evidence_ids":list(
                            suggestion.get("evidence_ids",[])
                        ),
                    }
                    changed=True
        if changed and refresh:
            self._refresh_suggestions()
            self._update_pending_status()
        return changed

    def _ignore_selected_suggestions(self):
        self._ignore_suggestions(
            self._selected_review_tag_values(),
            [
                self._suggestion_value(item)
                for item in self.description_suggestions.selectedItems()
            ],
        )

    def _ignore_selected_tag_suggestions(self):
        self._ignore_suggestions(
            self._selected_review_tag_values(),(),
        )

    def _pending_tag_actions_menu(self,chip):
        """Mirror review commands for the targeted chip/current selection."""
        menu=QMenu(chip)
        related=getattr(self,"_related_review_rows",{}).get(normalize_tag(chip.value))
        tag=(self.knowledge.tags.get(related.tag_id) if related is not None
             else self.knowledge.find(chip.value))
        if tag is not None:
            details=menu.addAction("Open Tag Details…")
            details.triggered.connect(
                lambda:self.edit_tag_requested.emit(tag.id,self)
            )
            menu.addSeparator()
            review=menu.addAction("Review Details.")
            review.triggered.connect(lambda:self._review_active_tag_details(chip.value))
        for button in (
            self.confirm_tag_suggestions,self.dismiss_tag_suggestions,
            self.ignore_tag_suggestions,self.inspect_related_tag_button,
            self.remove_pending_from_training_txt_button,
            self.remove_pending_from_metadata_button,
        ):
            if button.isHidden():continue
            action=menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.setToolTip(button.toolTip())
            action.triggered.connect(button.click)
        return menu

    def _selected_review_tag_values(self):
        """Resolve review-row keys to Tags for the explicit image-wide ignore."""
        values=list(self._selected_pending_tag_values())
        rows=getattr(self,"_related_review_rows",{})
        for value in self.pending_tag_chips.selected_values():
            suggestion=rows.get(normalize_tag(value))
            if suggestion is not None:values.append(suggestion.display)
        return tuple({normalize_tag(value):value for value in values}.values())

    def _ignore_selected_description_suggestions(self):
        self._ignore_suggestions(
            (),[
                self._suggestion_value(item)
                for item in self.description_suggestions.selectedItems()
            ],
        )

    def _ignore_selected_descriptions_everywhere(self):
        if self.settings is None:
            return
        selected=[]
        for item in self.description_suggestions.selectedItems():
            selected.extend(split_plain_language(self._suggestion_value(item)))
        if not selected:
            return
        try:
            changed=add_description_exclusions(self.settings,selected)
        except Exception as exc:
            QMessageBox.warning(self,"Ignored descriptions were not saved",str(exc)); return
        if changed:
            self._refresh_suggestions()
    def _manage_global_description_ignores(self):
        if self.settings is None:
            return
        dialog=DescriptionExclusionsDialog(self.settings,self)
        dialog.exec()
        if dialog.changed:
            self._refresh_suggestions()
    def _ignore_tag_suggestions(self,values):
        self._ignore_suggestions(values,[])

    def _ignore_suggestions(self,tags,descriptions):
        ignored=[]
        for value in tags:
            display=str(value).strip(); key=normalize_tag(display)
            if key:
                if key not in self.pending_ignored_tags or key in self.pending_restored_tags:
                    ignored.append((
                        key,self.pending_ignored_tags.get(key),
                        key in self.pending_restored_tags,key in self._ignored_tags(),
                    ))
                self.pending_restored_tags.discard(key)
                self.pending_ignored_tags.setdefault(key,display)
        if ignored:
            self._tag_history.append(("ignore_suggestions",tuple(ignored),None,-1))
        for raw in descriptions:
            for value in split_plain_language(raw):
                key=normalize_plain_language_exclusion(value)
                if key:
                    self.pending_restored_descriptions.discard(key)
                    self.pending_ignored_descriptions.setdefault(key,value)
        if not self.pending_ignored_tags and not self.pending_ignored_descriptions:
            return
        self._refresh_suggestions()
        self._update_pending_status()

    def _confirm_selected_suggestions(self):
        active_tab=self.tabs.currentWidget()
        selected_tags=self._selected_pending_tag_values()
        selected_descriptions=[
            self._suggestion_value(item)
            for item in self.description_suggestions.selectedItems()
        ]
        if not selected_tags and not selected_descriptions:
            QMessageBox.information(
                self,"Nothing selected",
                "Select one or more findings first.",
            )
            return
        tag_added=True
        if selected_tags:
            tag_added=bool(self._stage_add_tags_batch(selected_tags))
            if tag_added:self._refresh_tag_lists()
        if selected_descriptions:
            self.pending_accepted_descriptions.update(
                sentence
                for value in selected_descriptions
                for sentence in split_plain_language(value)
            )
            descriptions=self._descriptions(self.prose.toPlainText())
            descriptions=merge_plain_language_in_source_order(
                descriptions,selected_descriptions,self._description_source_sequences(),
            )
            self.prose.setPlainText("\n".join(descriptions))
        self._refresh_suggestions()
        if active_tab is not None:self.tabs.setCurrentWidget(active_tab)

    def _confirm_selected_tag_suggestions(self):
        selected=self._selected_pending_tag_values()
        if not selected:
            return
        selected_indexes=[
            self.pending_tag_chips.index_of(value) for value in selected
        ]
        next_index=min(
            (index for index in selected_indexes if index>=0),default=0,
        )
        if self._stage_add_tags_batch(selected):
            self._refresh_tag_lists(); self._refresh_suggestions()
            if self.pending_tag_chips._chips:
                QTimer.singleShot(
                    0,lambda index=next_index:
                    self.pending_tag_chips.focus_index(index)
                )
            else:
                self.pending_tag_chips.scroll.setFocus(Qt.OtherFocusReason)

    def _selected_description_decision_targets(self):
        targets=[]
        for row in self.description_suggestions.selectedItems():
            key=row.data(0,Qt.UserRole+1)
            suggestion=self._suggested.get(key,{})
            value=self._suggestion_value(row)
            for attribution in suggestion.get("attributions",[]):
                if not isinstance(attribution,dict):
                    continue
                evidence_id=str(attribution.get("evidence_id") or "").strip()
                targets.append(FindingDecisionTarget(
                    kind="description",value=value,
                    sources=(str(
                        attribution.get("source") or "Unknown"
                    ),),
                    evidence_ids=(evidence_id,) if evidence_id else (),
                    source_fingerprint=str(
                        attribution.get("source_fingerprint") or ""
                    ),
                ))
        return targets

    def _confirm_selected_description_suggestions(self):
        selected_targets=self._selected_description_decision_targets()
        selected_items=self.description_suggestions.selectedItems()
        selected=[
            self._suggestion_value(item)
            for item in selected_items
        ]
        if not selected:
            return
        selected_rows=[
            self.description_suggestions.indexOfTopLevelItem(item)
            for item in selected_items
        ]
        next_row=min(
            (index for index in selected_rows if index>=0),
            default=0,
        )
        current=self._descriptions(self.prose.toPlainText())
        replacing=bool(current)
        decided_descriptions=None
        if replacing:
            choice=self._choose_description_use(current,selected)
            if isinstance(choice,tuple):
                decision,decided_descriptions=choice
            else:
                decision=choice
            if decision=="keep":
                self._dismiss_selected_description_suggestions()
                return
            if decision not in {"replace","combined"}:
                return
        descriptions=(
            list(decided_descriptions or [])
            if replacing and decision=="combined"
            else merge_plain_language_in_source_order(
                [] if replacing else current,
                selected,self._description_source_sequences(),
            )
        )
        self.pending_accepted_descriptions.update(
            sentence
            for value in selected
            for sentence in split_plain_language(value)
        )
        if replacing:
            existing={
                (
                    target.value,target.sources,target.evidence_ids,
                    target.source_fingerprint,
                )
                for target in self.pending_used_as_edited_targets
            }
            for target in selected_targets:
                key=(
                    target.value,target.sources,target.evidence_ids,
                    target.source_fingerprint,
                )
                if key not in existing:
                    self.pending_used_as_edited_targets.append(target)
                    existing.add(key)
        self.prose.setPlainText("\n".join(descriptions))
        self._refresh_suggestions()
        if self.description_suggestions.topLevelItemCount():
            next_item=self.description_suggestions.topLevelItem(min(
                next_row,
                self.description_suggestions.topLevelItemCount()-1,
            ))
            self.description_suggestions.setCurrentItem(next_item)
            next_item.setSelected(True)
            self.description_suggestions.setFocus()
        else:
            self.prose.setFocus()

    def _choose_description_use(self,current,selected):
        dialog=QDialog(self)
        dialog.setWindowTitle("Edit confirmed descriptions")
        dialog.setModal(True)
        layout=QVBoxLayout(dialog)
        intro=QLabel(
            "Review the new finding first, then edit the confirmed result below. "
            +(
                "At least one selection is Alternative wording, based only on "
                "strong lexical overlap; CollectiGen will not merge it "
                "automatically. "
                if any(
                    description_review_classification(value,current)
                    =="Alternative wording" for value in selected
                ) else ""
            )+
            "Nothing is written until you choose Save Changes in the image editor."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        ordered_selected=merge_plain_language_in_source_order(
            [],selected,self._description_source_sequences(),
        )
        for title,values,accessible_name,object_name in (
            (
                f"New Selected Descriptions ({len(ordered_selected):,})",
                ordered_selected,
                "New selected Plain Language Descriptions",
                "newSelectedDescriptions",
            ),
            (
                f"Current Confirmed Descriptions ({len(current):,})",
                current,
                "Current confirmed Plain Language Descriptions",
                "currentConfirmedDescriptions",
            ),
        ):
            group=QGroupBox(title)
            group_layout=QVBoxLayout(group)
            text=QTextEdit()
            text.setObjectName(object_name)
            text.setReadOnly(True)
            text.setPlainText("\n".join(values))
            text.setAccessibleName(accessible_name)
            text.setMaximumHeight(120)
            group_layout.addWidget(text)
            layout.addWidget(group)
        result_group=QGroupBox("Confirmed Result (Editable)")
        result_layout=QVBoxLayout(result_group)
        draft=QTextEdit()
        draft.setObjectName("confirmedDescriptionDraft")
        draft.setAccessibleName("Editable confirmed Plain Language Description result")
        draft.setPlainText("\n".join(merge_plain_language_in_source_order(
            current,ordered_selected,self._description_source_sequences(),
        )))
        result_layout.addWidget(draft)
        layout.addWidget(result_group,1)
        note=QLabel(
            "Use Edited Result stages exactly the text above. Use New Only replaces "
            "the confirmed prose with the selected finding. Keep Existing defers "
            "the finding. Saving an accepted result records the exact selected "
            "sources as Used as edited. SDXL Tags and retained source or AI "
            "evidence are unchanged."
        )
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")
        layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        use_edited=buttons.addButton(
            "Use Edited Result",QDialogButtonBox.AcceptRole,
        )
        use_edited.setProperty("primary",True)
        use_edited.setDefault(True)
        use_new=buttons.addButton(
            "Use New Only",QDialogButtonBox.ActionRole,
        )
        keep_existing=buttons.addButton(
            "Keep Existing",QDialogButtonBox.ActionRole,
        )
        decision={"value":""}
        use_edited.clicked.connect(
            lambda:(decision.__setitem__(
                "value",("combined",self._descriptions(draft.toPlainText()))
            ),dialog.accept())
        )
        use_new.clicked.connect(
            lambda:(decision.__setitem__(
                "value",("replace",ordered_selected)
            ),dialog.accept())
        )
        keep_existing.clicked.connect(
            lambda:(decision.__setitem__("value","keep"),dialog.accept())
        )
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(920,680)
        return decision["value"] if dialog.exec()==QDialog.Accepted else ""

    def _refresh_suggestions(self):
        selected_tags=self.pending_tag_chips.selected_values()
        selected_descriptions=[
            self._suggestion_value(item)
            for item in self.description_suggestions.selectedItems()
        ]
        current=self.description_suggestions.currentItem()
        current_key=(
            current.data(0,Qt.UserRole+1)
            if current is not None else None
        )
        tag_scroll=self.pending_tag_chips.scroll.verticalScrollBar().value()
        description_scroll=(
            self.description_suggestions.verticalScrollBar().value()
        )
        self._suggested={}; self._populate(render=False)
        self._render_suggestion_surfaces(
            selected_tags,selected_descriptions,
            tag_scroll,description_scroll,current_key,refresh_pending=False,
        )
        # Related suggestions publish the final pending list with its existing
        # selection and scroll position; avoid rendering an intermediate list.
        self._update_suggestion_actions()
        if hasattr(self,"pending_tag_chips"):
            try:self._refresh_related_tags()
            except Exception:
                # Keep current direct findings, but never republish related
                # results from the previous authority after a failed refresh.
                self.related_tag_suggestions=()
                self._related_suggestion_count=0
                self._visible_related_tag_count=0
                self.review_tag_more_button.hide()
                self.inspect_related_tag_button.hide()
                self.safe_related_action.setText("Add 0 Safe Related Tags\u2026")
                self._refresh_pending_tag_surface()
                self._update_suggestion_actions()
                raise
        self._refresh_confirmed_identity_repair()

    @staticmethod
    def _descriptions(value: str):
        return split_plain_language(value.replace("\r\n", "\n"))

    def _description_source_sequences(self):
        sequences=[]
        if self.report:
            for source in self.report.sources:
                values=[sentence for raw in source.plain_language for sentence in split_plain_language(raw)]
                if values: sequences.append(values)
        saved=self.record.annotation.source.get("evidence",[])
        for source in saved if isinstance(saved,list) else []:
            if not isinstance(source,dict): continue
            values=[sentence for raw in source.get("plain_language",[]) for sentence in split_plain_language(raw)]
            if values: sequences.append(values)
        findings=self.record.annotation.source.get("candidate_findings",[])
        values=[
            sentence for finding in findings if isinstance(finding,dict) and finding.get("kind")=="description"
            for sentence in split_plain_language(finding.get("value"))
        ] if isinstance(findings,list) else []
        if values: sequences.append(values)
        for evidence in self.record.annotation.ai_evidence:
            values=split_plain_language(evidence.typed_plain_language())
            if values: sequences.append(values)
        return sequences

    def _update_pending_status(self,*_):
        changed=self.has_unsaved_changes()
        interrupted=getattr(self,"_history_completion_pending",False)
        if hasattr(self,"pending_status"):
            self.pending_status.setText("Recovery required" if interrupted else "Unsaved changes")
            self.pending_status.setVisible(changed or interrupted)
        if hasattr(self,"save_button"):
            blocked=bool(self._startup_recovery_error)
            self.save_button.setText(
                "Recovery Required" if interrupted else "Save Changes" if changed else "No Changes to Save"
            )
            self.save_button.setEnabled(changed and not blocked)
            primary=bool(changed and self.queue_count==1)
            if bool(self.save_button.property("primary"))!=primary:
                self.save_button.setProperty("primary",primary)
                style=self.save_button.style()
                style.unpolish(self.save_button); style.polish(self.save_button)
            save_description=(
                "Save the staged Image Knowledge changes for this image."
                if changed else "No image data changes are staged."
            )
            self.save_button.setToolTip(save_description)
            self.save_button.setAccessibleDescription(save_description)
        if hasattr(self,"save_next_button") and self.queue_count>1:
            self.save_next_button.setText(
                "Save and Next" if changed else "Next"
            )
            self.save_next_button.setEnabled(
                self.queue_index+1<self.queue_count
                and not bool(self._startup_recovery_error)
            )

    def has_unsaved_changes(self):
        saved_ack=self.record.annotation.source.get("ownership_acknowledgements",[])
        if isinstance(saved_ack,list) and self._working_ownership_acknowledgements!=saved_ack:return True
        if self._pending_retired_tag_replacements:return True
        if self._description_autofill_state!=character_description_autofill_state(self.record.annotation):
            return True
        if self._source_autofill_state!=character_source_autofill_state(self.record.annotation):
            return True
        if (self._pending_scoped_tag_additions
                or self.pending_ignored_tags or self.pending_ignored_descriptions
                or self.pending_used_as_edited_targets
                or self.pending_restored_tags
                or self.pending_restored_absent_tags
                or self.pending_restored_descriptions
                or self.pending_dismissed_tags or self.pending_dismissed_descriptions
                or self.pending_restored_dismissed_tags
                or self.pending_restored_dismissed_descriptions):
            return True
        if self.current_tags.to_list() != self.original_tags:
            return True
        if [item.to_dict() for item in self._working_objects]!=self._original_objects:
            return True
        if [item.to_dict() for item in self._working_characters]!=self._original_characters:
            return True
        if [item.to_dict() for item in self._working_contexts]!=self._original_contexts:
            return True
        if self._descriptions(self.prose.toPlainText()) != self.original_prose:
            return True
        if self._clear_legacy_character_assignments:
            return True
        return False

    def _confirm_discard(self):
        if not self.has_unsaved_changes():
            return True
        return QMessageBox.question(
            self,
            "Discard unsaved changes?",
            "This image has unsaved changes. Continue without saving them?",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        ) == QMessageBox.Discard

    def _navigate(self, result):
        enabled = self.previous_button.isEnabled() if result == self.REQUEST_PREVIOUS else self.skip_button.isEnabled()
        if enabled and self._confirm_discard():
            self.done(result)

    def _request_ai(self, kind: str):
        if kind not in {"tags", "description"} or getattr(self, "_ai_busy", False):
            return
        if not self._save_before_preparation():
            return
        self.set_ai_busy(True, "Generating AI Tags…" if kind == "tags" else "Generating AI Descriptions…")
        self.ai_requested.emit(kind)

    def _save_before_preparation(self) -> bool:
        if self.has_unsaved_changes():
            choice=QMessageBox.question(
                self,
                "Save changes before preparing?",
                "Preparation needs the image decisions currently staged in this "
                "editor, including ignored tags and descriptions. Save those "
                "changes and continue?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if choice!=QMessageBox.Save or not self._save_record():
                return False
        return True

    def _request_preparation(self,options: PreparationOptions):
        if getattr(self,"_ai_busy",False) or not self._save_before_preparation():
            return
        self.save_status.hide()
        self.set_ai_busy(True,"Preparing image…")
        self.preparation_requested.emit(options)

    def set_ai_busy(self, busy: bool, status: str = ""):
        self._ai_busy=bool(busy)
        phase=getattr(self,"preparation_phase",None)
        if phase is not None:
            phase.setText(status or "Running AI…"); phase.setVisible(busy)
        for name in (
            "suggest_ai_tags_button","suggest_ai_descriptions_button",
            "generate_ai_button","identity_repair_button",
        ):
            button=getattr(self,name,None)
            if button is not None:
                button.setEnabled(not busy)
        if busy:
            set_progress_bar(self.ai_progress,detail=status,unknown_text=status or "Running AI…")
            self.ai_progress.show()
        else:
            self.ai_progress.hide()

    def set_preparation_progress(self,index: int,total: int,status: str=""):
        """Keep page-owned preparation progress visible in the modal editor."""
        if not getattr(self,"_ai_busy",False):
            return
        detail=str(status or "Preparing image data…")
        phase=getattr(self,"preparation_phase",None)
        if phase is not None:phase.setText(detail); phase.show()
        set_progress_bar(
            self.ai_progress,index,total,detail=detail,
            unit="work steps",scope="this image",
        )
        self.ai_progress.show()

    @staticmethod
    def _merge_refreshed_sequence(original, current, refreshed, normalizer):
        """Apply an external refresh without discarding staged editor changes."""
        original=list(original); current=list(current); refreshed=list(refreshed)
        if current==original:
            return refreshed
        original_keys={normalizer(value) for value in original if normalizer(value)}
        refreshed_keys={normalizer(value) for value in refreshed if normalizer(value)}
        externally_removed=original_keys-refreshed_keys
        merged=[
            value for value in current
            if normalizer(value) and normalizer(value) not in externally_removed
        ]
        merged_keys={normalizer(value) for value in merged}
        for value in refreshed:
            key=normalizer(value)
            if key and key not in original_keys and key not in merged_keys:
                merged.append(value); merged_keys.add(key)
        return merged

    def _preserve_history_context(self, record: ImageRecord):
        """Keep this editor's admitted save scope when a reader replaces its record."""
        context=getattr(self,"_history_context",None)
        if context is not None:
            if record.image_path.absolute()!=self._history_image:
                raise ValueError("The refreshed record belongs to another image. Reopen its editor separately.")
            record.history_context=context

    def _reconcile_ai_record(self, record: ImageRecord):
        """Merge this editor's completed AI write into its staged working state."""
        self._preserve_history_context(record)
        original_description_state=character_description_autofill_state(self.record.annotation)
        refreshed_description_state=character_description_autofill_state(record.annotation)
        if self._description_autofill_state==original_description_state:
            self._description_autofill_state=refreshed_description_state
        elif all(state is not None for state in (
            original_description_state,refreshed_description_state,self._description_autofill_state,
        )):
            self._description_autofill_state={
                **refreshed_description_state,
                **{key:value for key,value in self._description_autofill_state.items()
                   if original_description_state.get(key)!=value},
            }
        else:
            self._description_autofill_state=refreshed_description_state
        previous_autofill=character_source_autofill_state(self.record.annotation)
        refreshed_autofill=character_source_autofill_state(record.annotation)
        if self._source_autofill_state==previous_autofill:
            self._source_autofill_state=refreshed_autofill
        elif all(state is not None for state in (
            previous_autofill,refreshed_autofill,self._source_autofill_state,
        )):
            enabled,excluded=self._source_autofill_state
            original_enabled,original_excluded=previous_autofill
            self._source_autofill_state=(
                enabled if enabled!=original_enabled else refreshed_autofill[0],
                frozenset((set(refreshed_autofill[1])|(set(excluded)-set(original_excluded)))
                          -(set(original_excluded)-set(excluded))),
            )
        else:
            self._source_autofill_state=refreshed_autofill
        refreshed_tags=TagCollection(record.annotation.all_structured_tags()).to_list()
        current_tags=self.current_tags.to_list()
        merged_tags=self._merge_refreshed_sequence(
            self.original_tags,current_tags,refreshed_tags,normalize_tag,
        )
        refreshed_prose=split_plain_language("\n".join(record.annotation.plain_language))
        current_prose=self._descriptions(self.prose.toPlainText())
        merged_prose=self._merge_refreshed_sequence(
            self.original_prose,current_prose,refreshed_prose,
            normalize_plain_language_exclusion,
        )

        refreshed_characters=[
            item.to_dict() for item in record.annotation.characters
        ]
        if [
            item.to_dict() for item in self._working_characters
        ]==self._original_characters:
            self._working_characters=copy.deepcopy(
                record.annotation.characters
            )
        self._original_characters=refreshed_characters
        self.record=record
        self._working_contexts=copy.deepcopy(record.annotation.contexts)
        self._original_contexts=[
            item.to_dict() for item in record.annotation.contexts
        ]
        self._working_tag_identities=dict(record.annotation.tag_identities)
        self.current_tags=TagCollection(merged_tags)
        blocked=self.automatic_character_sources.blockSignals(True)
        self.automatic_character_sources.setChecked(bool(self._source_autofill_state and self._source_autofill_state[0]))
        self.automatic_character_sources.setEnabled(self._source_autofill_state is not None)
        self.automatic_character_sources.blockSignals(blocked)
        blocked=self.automatic_character_description.blockSignals(True)
        self.automatic_character_description.setChecked(bool(
            self._description_autofill_state and self._description_autofill_state["enabled"]
        ))
        self.automatic_character_description.setEnabled(self._description_autofill_state is not None)
        self.automatic_character_description.blockSignals(blocked)
        self.original_tags=list(refreshed_tags)
        self.original_prose=list(refreshed_prose)
        non_object_annotation=copy.deepcopy(record.annotation)
        non_object_annotation.objects=[]
        self._non_object_original_keys={
            normalize_tag(value)
            for value in non_object_annotation.all_structured_tags()
        }
        self._edit_snapshot=ImageRecordEditSnapshot.capture(record)

        previous=self.prose.blockSignals(True)
        try:
            self.prose.setPlainText("\n".join(merged_prose))
        finally:
            self.prose.blockSignals(previous)

    def refresh_ai_evidence(self, record: ImageRecord | None = None):
        if record is not None:
            self._reconcile_ai_record(record)
        self.set_ai_busy(False)
        self._refresh_tag_lists()
        self._refresh_suggestions()
        self._refresh_confirmed_context()
        self._update_pending_status()
        tag_count=int(getattr(self,"_pending_tag_count",0))
        description_count=int(getattr(self,"_pending_description_count",0))
        if tag_count and not description_count:
            self._show_pending_tag_workspace()
        elif description_count and not tag_count:
            self.tabs.setCurrentWidget(self.description_tab)
        else:
            self._show_pending_tag_workspace()

    def ai_failed(self):
        self.set_ai_busy(False)

    def preparation_unavailable(self,message: str):
        """Explain a page-owned start refusal inside the active modal editor."""
        self.set_ai_busy(False)
        self.save_status.setText(str(message or "Image preparation did not start."))
        self.save_status.show()
        announce_accessibly(self.save_status,self.save_status.text())

    def _adopt_recovered_record(self,record: ImageRecord):
        """Make a verified current record the editor's clean working baseline."""
        self._preserve_history_context(record)
        self.record=record
        self._working_ownership_acknowledgements=copy.deepcopy(record.annotation.source.get("ownership_acknowledgements",[])) if isinstance(record.annotation.source.get("ownership_acknowledgements",[]),list) else []
        self._description_autofill_state=character_description_autofill_state(record.annotation)
        blocked_description=self.automatic_character_description.blockSignals(True)
        self.automatic_character_description.setChecked(bool(
            self._description_autofill_state and self._description_autofill_state["enabled"]
        ))
        self.automatic_character_description.setEnabled(self._description_autofill_state is not None)
        self.automatic_character_description.blockSignals(blocked_description)
        self._working_characters=copy.deepcopy(record.annotation.characters)
        self._source_autofill_state=character_source_autofill_state(record.annotation)
        self._source_autofill_seen_present=set()
        self._source_autofill_added.clear()
        blocked_autofill=self.automatic_character_sources.blockSignals(True)
        self.automatic_character_sources.setChecked(
            bool(self._source_autofill_state and self._source_autofill_state[0])
        )
        self.automatic_character_sources.setEnabled(self._source_autofill_state is not None)
        self.automatic_character_sources.blockSignals(blocked_autofill)
        self._original_characters=[
            item.to_dict() for item in record.annotation.characters
        ]
        self._character_ownership_changes.clear()
        self._working_objects=copy.deepcopy(record.annotation.objects)
        self._original_objects=[item.to_dict() for item in record.annotation.objects]
        self._working_contexts=copy.deepcopy(record.annotation.contexts)
        self._original_contexts=[
            item.to_dict() for item in record.annotation.contexts
        ]
        self._working_tag_identities=dict(record.annotation.tag_identities)
        self.current_tags=TagCollection(record.annotation.all_structured_tags())
        self.original_tags=self.current_tags.to_list()
        self.original_prose=split_plain_language(
            "\n".join(record.annotation.plain_language)
        )
        non_object_annotation=copy.deepcopy(record.annotation)
        non_object_annotation.objects=[]
        self._non_object_original_keys={
            normalize_tag(value)
            for value in non_object_annotation.all_structured_tags()
        }
        self._object_owned_keys={
            normalize_tag(value)
            for item in self._working_objects
            for value in item.all_tags()
        }
        blocked=self.prose.blockSignals(True)
        try:
            self.prose.setPlainText("\n".join(self.original_prose))
        finally:
            self.prose.blockSignals(blocked)
        self._edit_snapshot=ImageRecordEditSnapshot.capture(record)
        self._approved_conflict_states.clear()
        self._mark_saved_state(self.current_tags,self.original_prose)
        autofill_ready=self._source_autofill_ready
        self._source_autofill_ready=False
        try:
            self._refresh_tag_lists()
            self._refresh_suggestions()
            self._refresh_confirmed_context()
            self._refresh_character_view()
        finally:
            self._source_autofill_ready=autofill_ready

    def _recover_stale_record(self) -> bool:
        try:
            plan=self._recovery_service.plan(self._edit_snapshot,self.record)
        except Exception as exc:
            self._show_save_failure(str(exc))
            QMessageBox.warning(self,"Image data was not changed",str(exc))
            return False
        dialog=ImageRecordRecoveryDialog(plan,self)
        outcome=dialog.exec()
        if outcome==ImageRecordRecoveryDialog.DISCARD_LOCAL:
            try:
                current=ImageRecord(plan.image)
            except Exception as exc:
                self._show_save_failure(str(exc))
                QMessageBox.warning(self,"Newest image data was not loaded",str(exc))
                return False
            self._adopt_recovered_record(current)
            self.save_status.setObjectName("mutedLabel")
            self.save_status.setText(
                "Reloaded the newest Image Knowledge and discarded the staged editor changes."
            )
            self.save_status.setToolTip(
                "Training TXT, Metadata, and image pixels were unchanged."
            )
            self.save_status.show()
            return False
        if outcome!=ImageRecordRecoveryDialog.APPLY:
            self.save_status.setObjectName("mutedLabel")
            self.save_status.setText(
                "Nothing was saved. Your staged changes remain available in this editor."
            )
            self.save_status.setToolTip(
                "Choose Save Changes again when you are ready to review the newest Image Knowledge."
            )
            self.save_status.show()
            return False
        try:
            result=self._recovery_service.apply(
                plan,choices=dialog.choices(),knowledge=self.knowledge,
            )
        except Exception as exc:
            interrupted=self._hold_interrupted_history(exc)
            self._show_save_failure(str(exc))
            QMessageBox.warning(self,"Image history needs recovery" if interrupted else "Image data was not changed",str(exc))
            return False
        self._adopt_recovered_record(result.record)
        self._publish_saved_content_safety()
        self.persisted_changes=True
        if result.caption_changed:
            self.save_status.setObjectName("mutedLabel")
            self.save_status.setText(
                "Saved Image Knowledge. The separately changed Training TXT was left unchanged."
            )
            self.save_status.setToolTip(
                "Review or export Training TXT separately if it should match the saved Image Knowledge."
            )
            self.save_status.show()
        else:
            self.save_status.hide()
        return True

    def reject(self):
        if self._confirm_discard():
            super().reject()

    def _save_record(self):
        self.persisted_changes=False
        if self._startup_recovery_error:
            self._show_save_failure(self._startup_recovery_error)
            return False
        identity_plan=self._confirmed_identity_plan(confirmation=False)
        if identity_plan.blockers or identity_plan.new_entries:
            self._refresh_confirmed_identity_repair()
            self.tabs.setCurrentWidget(self.image_data_tab)
            self._show_save_failure(
                f"Choose {self.identity_repair_button.text().rstrip('…')} above to resolve the confirmed "
                "SDXL Tags that need a reusable identity, then choose Save again.\n"
                +self._identity_plan_details(identity_plan)
            )
            self.confirmed_tags_scroll.ensureWidgetVisible(
                self.identity_repair_button
            )
            self.identity_repair_button.setFocus(Qt.OtherFocusReason)
            return False
        identity_resolution=self._identity_service.require_existing(
            self._current_confirmed_tags(),
            stored_ids=self._working_tag_identities,
            confirmation=False,
        )
        self._remember_identity_resolution(identity_resolution)
        original_keys={normalize_tag(value) for value in self.original_tags}
        current_keys=self.current_tags.normalized_set()
        new_values=[
            value for value in self.current_tags
            if normalize_tag(value) not in original_keys
        ]
        unresolved=[
            value for value in new_values
            if normalize_tag(value) in self._ignored_tags()
        ]
        if unresolved:
            self._show_save_failure(
                "The image still marks these newly added SDXL Tags as not present: "
                +", ".join(unresolved)
                +". Restore the image decision before saving."
            )
            return False
        absent_keys={
            normalize_tag(value)
            for value in self.record.annotation.confirmed_absent
            if normalize_tag(value)
        }
        unresolved_absent=[
            value for value in new_values
            if (
                normalize_tag(value) in absent_keys
                and normalize_tag(value)
                not in self.pending_restored_absent_tags
            )
        ]
        if unresolved_absent:
            self._show_save_failure(
                "The image still records these newly added SDXL Tags as "
                "confirmed absent: "+", ".join(unresolved_absent)
                +". Explicitly reverse the confirmed-absence decision before saving."
            )
            return False
        current_engine=active_automation_engine(self.knowledge)
        blocked=[
            value for value in new_values
            if current_engine.is_evidence_rejected(value)
        ]
        if blocked:
            self._show_save_failure(
                "Automation now rejects these newly added SDXL Tags as incoming "
                "evidence: "+", ".join(blocked)
                +". Review the global rule before saving."
            )
            return False
        contexts_changed=(
            [item.to_dict() for item in self._working_contexts]
            != self._original_contexts
        )
        characters_changed=(
            [item.to_dict() for item in self._working_characters]
            != self._original_characters
        )
        structured_scope_changed=contexts_changed or characters_changed
        conflict_state=(
            (
                frozenset(current_keys),
                tuple(repr(item.to_dict()) for item in self._working_contexts),
                tuple(repr(item.to_dict()) for item in self._working_characters),
            )
            if structured_scope_changed else frozenset(current_keys)
        )
        if (new_values or structured_scope_changed) and conflict_state not in self._approved_conflict_states:
            staged_record=self._staged_image_record()
            warnings=(
                current_engine.conflict_warnings(
                    staged_record,self.original_tags,self.current_tags.to_list(),
                )
                if new_values else []
            )
            if structured_scope_changed:
                compare_records=getattr(
                    current_engine,"introduced_record_conflicts",None,
                )
                if callable(compare_records):
                    warnings.extend(
                        item.message for item in compare_records(
                            self.record,staged_record,
                        )
                    )
            warnings=list(dict.fromkeys(warnings))
            if warnings and not self._confirm_diagnostic_tag_conflicts(warnings):
                self._show_save_failure(
                    "Review the diagnostic SDXL Tag conflict before saving."
                )
                return False
            if warnings:self._approved_conflict_states.add(conflict_state)
        tags = self.current_tags.copy()
        saving_record=self.record
        pre_save_annotation=copy.deepcopy(saving_record.annotation)
        pre_save_modified=bool(saving_record.modified)
        pre_save_sidecar_fingerprint=copy.deepcopy(
            getattr(saving_record,"_loaded_sidecar_fingerprint",None)
        )
        apply_character_source_autofill_state(
            self.record.annotation,self._source_autofill_state,
        )
        apply_character_description_autofill_state(
            self.record.annotation,self._description_autofill_state,
        )
        if self._clear_legacy_character_assignments:
            self.record.annotation.characters=[]
        elif characters_changed:
            self.record.annotation.characters=copy.deepcopy(
                self._working_characters
            )
        self.record.annotation.objects=copy.deepcopy(self._working_objects)
        self.record.annotation.tag_identities=dict(
            self._working_tag_identities
        )
        if isinstance(self.record.annotation.source.get("ownership_acknowledgements",[]),list) and (self._working_ownership_acknowledgements or "ownership_acknowledgements" in self.record.annotation.source):
            trial=self._staged_image_record()
            engine=getattr(self.automation_engine,"conflicts",None)
            acknowledgements=(engine.revalidated_ownership_acknowledgements(trial) if engine is not None else
                [{**item,"status":"needs_review"} if isinstance(item,dict) and item.get("version")==1 else item
                 for item in self._working_ownership_acknowledgements])
            self.record.annotation.source["ownership_acknowledgements"]=copy.deepcopy(acknowledgements)
        self._apply_character_ownership_placements(self.record.annotation)
        try:
            self._apply_retired_tag_replacements(self.record.annotation)
            apply_context_partition(
                self.record.annotation,self._working_contexts,
                confirmed_values=tags,
            )
        except (ImageContextError,ValueError) as exc:
            if self.record is saving_record:
                saving_record.annotation=pre_save_annotation
                saving_record.modified=pre_save_modified
            self._show_save_failure(str(exc))
            return False
        if self._clear_legacy_character_assignments:
            self.record.annotation.characters=[]
            self.record.annotation.source.pop("character_type",None)
        prose = self._descriptions(self.prose.toPlainText())
        self._apply_pending_scoped_tag_additions()
        self._apply_current_tags(tags)
        try:
            apply_context_partition(
                self.record.annotation,self.record.annotation.contexts,
                confirmed_values=tags,
            )
        except ImageContextError as exc:
            if self.record is saving_record:
                saving_record.annotation=pre_save_annotation
                saving_record.modified=pre_save_modified
            self._show_save_failure(str(exc))
            return False
        self.record.annotation.tag_identities=dict(
            self._working_tag_identities
        )
        self.record.annotation.plain_language = list(prose)
        if self.pending_restored_absent_tags:
            self.record.annotation.confirmed_absent=[
                value
                for value in self.record.annotation.confirmed_absent
                if normalize_tag(value)
                not in self.pending_restored_absent_tags
            ]
        decisions=ImageFindingDecisionService(
            self.record.annotation,knowledge=self.knowledge,
        )
        decisions.restore_image_ignores(
            tags=self.pending_restored_tags,
            descriptions=self.pending_restored_descriptions,
        )
        decisions.restore_deferred(
            tags=self.pending_restored_dismissed_tags,
            descriptions=self.pending_restored_dismissed_descriptions,
        )
        decisions.accept_confirmed_tags(tags)
        decisions.accept_confirmed_descriptions([
            *prose,*self.pending_accepted_descriptions,
        ])
        decisions.mark_used_as_edited(
            self.pending_used_as_edited_targets
        )
        decisions.ignore_for_image(
            tags=self.pending_ignored_tags.values(),
            descriptions=self.pending_ignored_descriptions.values(),
        )
        deferred_targets=[]
        for kind,pending in (
            ("tag",self.pending_dismissed_tags),
            ("description",self.pending_dismissed_descriptions),
        ):
            for key,item in pending.items():
                deferred_targets.append(FindingDecisionTarget(
                    kind=kind,
                    value=str(item.get("value") or key),
                    sources=tuple(
                        value.strip()
                        for value in str(
                            item.get("source") or "Unknown"
                        ).split(", ")
                        if value.strip()
                    ),
                    evidence_ids=tuple(item.get("evidence_ids",[])),
                ))
        decisions.defer(deferred_targets)
        if self.report:
            for source in self.report.sources:
                for generation_record in source.generation_records:
                    self.record.annotation.add_generation_record(generation_record)
        self.record.modified = True
        assessment=self._edit_snapshot.assess(self.record)
        if not assessment.changed:
            self.record.modified=False
            self._mark_saved_state(tags,prose)
            self.save_status.hide()
            return True
        if assessment.stale:
            recovered=self._recover_stale_record()
            if not recovered and self.record is saving_record:
                saving_record.annotation=pre_save_annotation
                saving_record.modified=pre_save_modified
            return recovered
        metadata = MetadataSyncService(getattr(self.settings, "exiftool_path", "") or None)
        # Reconciliation requires existing JSON authority. The first guarded
        # save creates that authority; it must not write image Metadata first
        # or introduce a cancellable preview after committing the pending edits.
        metadata_available=not self.image_knowledge_only and metadata.is_available()
        metadata_deferred=metadata_available and not self.record.sidecar_path.exists()
        if metadata_available and not metadata_deferred:
            if not reconcile_embedded_with_preview(
                self, self.record, metadata, self.knowledge, self.characters,
                pending_editor_save=True,
            ):
                if self.record is saving_record:
                    saving_record.annotation=pre_save_annotation
                    saving_record.modified=pre_save_modified
                return False
        try:
            self.record.save(
                character_db=self.characters,knowledge=self.knowledge,
                export_txt=not self.image_knowledge_only,force=False,
            )
        except Exception as exc:
            if (
                isinstance(exc,ImageRecordStaleWriteError)
                and self.record is saving_record
                and saving_record._loaded_sidecar_fingerprint==pre_save_sidecar_fingerprint
            ):
                # A writer can arrive after assessment or while a preview is
                # open. Recover the staged delta before restoring the draft;
                # never retry an already-partially-persisted save this way.
                recovered=self._recover_stale_record()
                if not recovered and self.record is saving_record:
                    saving_record.annotation=pre_save_annotation
                    saving_record.modified=pre_save_modified
                return recovered
            if (
                self.record is saving_record
                and getattr(
                    saving_record,"_loaded_sidecar_fingerprint",None
                )==pre_save_sidecar_fingerprint
            ):
                saving_record.annotation=pre_save_annotation
                saving_record.modified=pre_save_modified
            interrupted=self._hold_interrupted_history(exc)
            self._show_save_failure(str(exc))
            QMessageBox.critical(self, "Image history needs recovery" if interrupted else "Image data was not saved", str(exc))
            return False
        self._edit_snapshot=ImageRecordEditSnapshot.capture(self.record)
        self._publish_saved_content_safety()
        self._mark_saved_state(tags,prose)
        self.persisted_changes=True
        self.save_status.hide()
        if metadata_deferred:
            QMessageBox.information(
                self,"Image Knowledge saved; Metadata unchanged",
                "Your Image Knowledge and TXT were saved. Embedded Metadata was "
                "left unchanged because this was the first Image Knowledge save. "
                "You can review a Metadata update after this save.",
            )
        return True

    def _open_generation_plan(self):
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self, record=self.record)

    def _content_safety_result(self):
        resolver=getattr(self.content_safety,"display_rating_for_record",None)
        if not callable(resolver):
            return None
        try:
            return tuple(resolver(self.record))
        except Exception:
            return None

    def _publish_saved_content_safety(self) -> None:
        current=self._content_safety_result()
        previous=self._saved_content_safety_result
        self._saved_content_safety_result=current
        publish=getattr(self.content_safety,"publish_saved_record",None)
        if not callable(publish) or (
            current is not None and current==previous
        ):
            return
        publish(self.record)

    def _mark_saved_state(self,tags: TagCollection,prose: list[str]):
        self._working_ownership_acknowledgements=copy.deepcopy(self.record.annotation.source.get("ownership_acknowledgements",[])) if isinstance(self.record.annotation.source.get("ownership_acknowledgements",[]),list) else []
        self._pending_retired_tag_replacements.clear()
        self._source_autofill_added.clear()
        self.save_failure_details_button.hide()
        self.current_tags=tags.copy()
        self.original_tags=self.current_tags.to_list()
        self._working_characters=copy.deepcopy(
            self.record.annotation.characters
        )
        self._original_characters=[
            item.to_dict() for item in self.record.annotation.characters
        ]
        self._character_ownership_changes.clear()
        self._working_tag_identities=dict(
            self.record.annotation.tag_identities
        )
        self.original_prose=list(prose)
        self._original_objects=[
            item.to_dict() for item in self.record.annotation.objects
        ]
        self._working_contexts=copy.deepcopy(self.record.annotation.contexts)
        self._original_contexts=[
            item.to_dict() for item in self.record.annotation.contexts
        ]
        self._clear_legacy_character_assignments=False
        self.pending_ignored_tags.clear()
        self.pending_ignored_descriptions.clear()
        self.pending_restored_tags.clear()
        self.pending_restored_absent_tags.clear()
        self.pending_restored_descriptions.clear()
        self.pending_dismissed_tags.clear()
        self.pending_dismissed_descriptions.clear()
        self.pending_accepted_descriptions.clear()
        self.pending_used_as_edited_targets.clear()
        self.pending_restored_dismissed_tags.clear()
        self.pending_restored_dismissed_descriptions.clear()
        self._pending_scoped_tag_additions.clear()
        self._scoped_ingress_restore_states.clear()
        self._update_pending_status()
        self._refresh_character_view()

    def _hold_interrupted_history(self, exc):
        from core.image_knowledge_history import HistoryWriteInterrupted
        if not isinstance(exc,HistoryWriteInterrupted):
            return False
        self._history_completion_pending=True
        self._startup_recovery_error=str(exc)
        self._update_pending_status()
        return True

    def _show_save_failure(self,message: str):
        reason=str(message).strip() or "The save could not be completed."
        summary=" ".join(reason.split())
        if len(summary)>320:
            summary=summary[:317]+"…"
        self.save_status.setObjectName("errorLabel")
        prefix=(
            "Data may already be saved. Reopen image for recovery.\n"
            if getattr(self,"_history_completion_pending",False) else
            "Image data was not saved. Your pending edits remain in this editor.\n"
        )
        self.save_status.setText(prefix+summary)
        self.save_status.setToolTip(reason)
        self.save_status.setAccessibleDescription(reason)
        self.save_status.show()
        self.save_failure_details_button.show()
        announce_accessibly(self.save_status,self.save_status.text())

    def _show_save_failure_details(self):
        dialog=QMessageBox(self)
        interrupted=getattr(self,"_history_completion_pending",False)
        dialog.setWindowTitle("Image history needs recovery" if interrupted else "Image data was not saved")
        dialog.setIcon(QMessageBox.Warning)
        dialog.setTextFormat(Qt.PlainText)
        dialog.setText("Image Knowledge may already be saved. Reopen this image to verify history recovery before another save."
                       if interrupted else "Your pending edits remain in this editor.")
        dialog.setInformativeText(self.save_status.text().split("\n",1)[-1])
        dialog.setDetailedText(self.save_status.toolTip())
        dialog.setStandardButtons(QMessageBox.Ok)
        dialog.exec()

    def save(self):
        if self.has_unsaved_changes() and self._save_record():
            self.accept()

    def save_and_next(self):
        if not self.save_next_button.isEnabled():
            return
        if not self.has_unsaved_changes() or self._save_record():
            self.done(self.SAVE_AND_NEXT)
