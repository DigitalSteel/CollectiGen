from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from PySide6.QtCore import QModelIndex,QPersistentModelIndex,QSignalBlocker,QTimer,Qt,Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,QAbstractItemView,QCheckBox,QColorDialog,QComboBox,QDialog,QDialogButtonBox,QFormLayout,QGroupBox,QHBoxLayout,QHeaderView,QLabel,QLineEdit,QMenu,
    QMessageBox,QPushButton,QScrollArea,QSizePolicy,QSplitter,QTabWidget,QTextEdit,QToolButton,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly

from core import (
    CATEGORY_COLOR_POLICY_LABELS,CATEGORY_COLOR_POLICIES,CATEGORY_REFERENCE_POLICIES,
    CATEGORY_SEMANTIC_ROLE_LABELS,
    CONTENT_RATINGS,CategoryWorkspaceSession,ConfirmedTagIdentityService,EvidenceQueryService,
    ImageTagRemovalService,OperationJournal,ORDER_GROUP_LABELS,TagIdentityMergeService,
    affected_order_projection_paths,classify_system_knowledge_change,
    normalize_category_color,
)
from core.paths import USER_DATA_DIR
from core.tag_order_workspace import prepare_category_structure_search_index
from core.category_base_relationships import (
    CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS,
    CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
)
from .components import BoundedSearchableComboBox,DisclosureButton,bind_status_footer,counted_noun
from .gallery import EmbeddedGallery
from .image_tag_removal_dialog import ImageTagRemovalDialog
from .tag_editor import TAG_IDS_MIME,TAG_MIME,TagChipEditor,decode_tag_drag_ids
from .tag_relationship_editor import TagReferenceEditor
from .tag_order_dialog import TagOrderWorkspace
from .workers import AsyncCallTask,CoordinatedCallTask


class CategoryDetailsDialog(QDialog):
    """Keep an autosaving category editor open until its result is known."""

    def __init__(self,can_close,parent=None):
        super().__init__(parent)
        self._can_close=can_close

    def reject(self):
        if self._can_close():super().reject()

    def closeEvent(self,event):
        if self._can_close():
            event.accept()
            self.done(QDialog.Rejected)
        else:event.ignore()


class StructureTree(QTreeWidget):
    semantic_drop=Signal(object,object)
    semantic_tag_drop=Signal(object,object)

    def __init__(self,parent=None):
        super().__init__(parent); self.setHeaderLabels(("Category Structure","Direct / Total SDXL Tags","Source"))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection); self.setDragEnabled(True); self.setAcceptDrops(True)
        self.setDropIndicatorShown(True); self.setDragDropMode(QAbstractItemView.InternalMove); self.setRootIsDecorated(True)
        self.setIndentation(14); header=self.header(); header.setStretchLastSection(False); header.setSectionResizeMode(0,QHeaderView.Stretch); header.setSectionResizeMode(1,QHeaderView.Fixed); header.setSectionResizeMode(2,QHeaderView.ResizeToContents); self.setColumnWidth(1,155)
        self._hover_expand_index=QPersistentModelIndex(); self._hover_expand_timer=QTimer(self); self._hover_expand_timer.setSingleShot(True); self._hover_expand_timer.setInterval(650); self._hover_expand_timer.timeout.connect(self._expand_hovered_category)
        self.setAccessibleName("Semantic category structure")
        self.setAccessibleDescription("Categories are nested by meaning. Drag a category onto another category to stage reparenting, or drag an SDXL Tag onto a category to move and save its membership automatically.")

    @staticmethod
    def _has_tag_payload(event):return event.mimeData().hasFormat(TAG_IDS_MIME) or event.mimeData().hasFormat(TAG_MIME)

    @staticmethod
    def _is_category_item(item):
        key=item.data(0,Qt.UserRole) if item is not None else None
        return bool(key and key[0]=="category")

    def _cancel_hover_expand(self):
        self._hover_expand_timer.stop(); self._hover_expand_index=QPersistentModelIndex()

    def _queue_hover_expand(self,item):
        if item is None or item.childCount()==0 or item.isExpanded():self._cancel_hover_expand(); return
        index=QPersistentModelIndex(self.indexFromItem(item))
        if index==self._hover_expand_index and self._hover_expand_timer.isActive():return
        self._hover_expand_index=index; self._hover_expand_timer.start()

    def _expand_hovered_category(self):
        index=self._hover_expand_index; self._hover_expand_index=QPersistentModelIndex()
        if not index.isValid():return
        item=self.itemFromIndex(QModelIndex(index))
        if item is not None and item.childCount() and not item.isExpanded():item.setExpanded(True); self.scrollToItem(item)

    def dragEnterEvent(self,event):
        if self._has_tag_payload(event):event.setDropAction(Qt.MoveAction); event.accept(); return
        super().dragEnterEvent(event)

    def dragMoveEvent(self,event):
        if self._has_tag_payload(event):
            target=self.itemAt(event.position().toPoint())
            if not self._is_category_item(target):self._cancel_hover_expand(); event.ignore(); return
            self._queue_hover_expand(target)
            event.setDropAction(Qt.MoveAction); event.accept(); return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self,event):
        self._cancel_hover_expand(); super().dragLeaveEvent(event)

    def dropEvent(self,event):
        self._cancel_hover_expand(); target=self.itemAt(event.position().toPoint()); event.ignore()
        if self._is_category_item(target) and self._has_tag_payload(event):
            self.semantic_tag_drop.emit(event.mimeData(),target); event.setDropAction(Qt.MoveAction); event.accept(); return
        source=self.currentItem()
        if self._is_category_item(source) and self._is_category_item(target) and source is not target:self.semantic_drop.emit(source,target)


class TagIdentityMergeDialog(QDialog):
    """Choose one survivor and explicitly resolve incompatible reviewed fields."""

    def __init__(self,records,service,*,use_summary="",parent=None):
        super().__init__(parent); self.records=tuple(records); self.service=service; self._plan=None; self._resolution_boxes={}
        self.setWindowTitle("Merge Duplicate Tags"); self.resize(860,620)
        layout=QVBoxLayout(self); heading=QLabel(f"Merge {len(self.records):,} duplicate SDXL Tags"); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        explanation=QLabel(
            "Choose the one canonical System Knowledge identity to keep. Its displayed name does not change. The other names become searchable aliases, compatible stable-ID relationships move to the survivor, and donor IDs remain as redirecting tombstones so existing Image Knowledge is not orphaned. No image, TXT, Metadata, raw evidence, or Automation rule is rewritten."
        )
        explanation.setWordWrap(True); explanation.setObjectName("mutedLabel"); layout.addWidget(explanation)
        selected=QLabel("Selected identities: "+", ".join(record.display for record in self.records)); selected.setWordWrap(True); selected.setAccessibleName(selected.text()); layout.addWidget(selected)
        if use_summary:
            uses=QLabel(use_summary); uses.setWordWrap(True); uses.setObjectName("mutedLabel"); uses.setAccessibleName(use_summary); layout.addWidget(uses)
        form=QFormLayout(); self.survivor=QComboBox(); self.survivor.setAccessibleName("Canonical survivor")
        self.survivor.addItem("Choose the canonical survivor…","")
        for record in self.records:self.survivor.addItem(record.display,record.id)
        form.addRow("Canonical survivor",self.survivor); layout.addLayout(form)
        self.preview=QLabel("Choose one survivor to calculate the exact merge."); self.preview.setWordWrap(True); self.preview.setObjectName("mutedLabel"); layout.addWidget(self.preview)
        self.scope_details=QTextEdit(); self.scope_details.setReadOnly(True); self.scope_details.setMaximumHeight(150); self.scope_details.setAccessibleName("Exact duplicate tag merge scope"); layout.addWidget(self.scope_details)
        self._update_scope_details()
        self.conflict_title=QLabel("Reviewed conflicts"); self.conflict_title.setObjectName("sectionTitle"); self.conflict_title.hide(); layout.addWidget(self.conflict_title)
        self.conflict_host=QWidget(); self.conflict_form=QFormLayout(self.conflict_host)
        self.conflict_scroll=QScrollArea(); self.conflict_scroll.setWidgetResizable(True); self.conflict_scroll.setWidget(self.conflict_host); self.conflict_scroll.setAccessibleName("Reviewed tag merge conflicts"); self.conflict_scroll.hide()
        layout.addWidget(self.conflict_scroll,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel); self.merge_button=buttons.addButton("Merge Tags",QDialogButtonBox.AcceptRole); self.merge_button.setProperty("primary",True); self.merge_button.setEnabled(False)
        self.merge_button.clicked.connect(self._accept_if_complete); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.survivor.currentIndexChanged.connect(self._refresh_preview)

    def survivor_id(self):return str(self.survivor.currentData() or "")

    def resolutions(self):
        return {key:str(box.currentData() or "") for key,box in self._resolution_boxes.items() if box.currentData()}

    def _update_scope_details(self,plan=None):
        lines=[]; selected_ids={record.id for record in self.records}
        for record in self.records:
            category=self.service.categories.path_label(record.category)
            aliases=", ".join(self.service.tags.effective_aliases(record)) or "none"
            ordering=(
                f"{record.ordering.group} / {record.ordering.priority}"
                +(" (reviewed)" if record.ordering.reviewed else " (inherited)")
            )
            sensitivity=(
                f"{record.content_sensitivity.minimum_rating or 'not classified'} "
                f"({record.content_sensitivity.classification_status})"
            )
            relations=[]
            semantic_parent,semantic_implies=(
                self.service.tags.effective_semantic_ids(record)
            )
            if semantic_parent:relations.append(f"Broader Tag {semantic_parent}")
            if semantic_implies:
                relations.append("Also Means "+", ".join(semantic_implies))
            if record.conflicts:relations.append("conflicts "+", ".join(record.conflicts))
            lines.append(
                f"{record.display} — Current stable ID: {record.id}; Category: {category} ({record.category}); "
                f"aliases: {aliases}; order: {ordering}; sensitivity: {sensitivity}; "
                f"relationships: {'; '.join(relations) or 'none'}; "
                f"provenance records: {len(record.provenance)}."
            )
        if self.service.characters is not None:
            references=[]
            for kind, record_kind, records in (
                ("Character Source","series",self.service.characters.series.values()),
                ("Character Identity","character",self.service.characters.characters.values()),
                ("Character Outfit","outfit",self.service.characters.outfits.values()),
            ):
                for record in records:
                    link=self.service.characters.effective_knowledge_tag(
                        record_kind,record.id,
                    )
                    direct=str(link.get("tag_id") or "")
                    excluded={
                        str(value) for value in link.get("excluded_tag_ids",())
                        if str(value)
                    }
                    if direct in selected_ids or selected_ids.intersection(excluded):
                        reference_type=(
                            "reviewed owner"
                            if direct in selected_ids and link.get("status")=="reviewed"
                            else "direct link"
                            if direct in selected_ids
                            else "reviewed exclusion"
                        )
                        references.append(f"{kind}: {record.name} ({record.id}, {reference_type})")
            lines.append(
                "Character Knowledge references: "
                +(", ".join(references) if references else "none")+"."
            )
        if plan is not None:
            lines.extend((
                "Searchable alternate names added (the survivor name does not change): "+(", ".join(plan.aliases_added) or "none")+".",
                "Dependent tag IDs: "+(", ".join(plan.affected_tag_ids) or "none")+".",
                "Category base-reference IDs: "+(", ".join(plan.category_base_ids) or "none")+".",
                "Canonical relationship IDs: "+(", ".join(plan.canonical_relationship_ids) or "none")+".",
                "Canonical relationships retired because their endpoints collapse or duplicate another record: "+(", ".join(plan.retired_relationship_ids) or "none")+".",
            ))
        self.scope_details.setPlainText("\n".join(lines))

    def _clear_conflicts(self):
        while self.conflict_form.rowCount():self.conflict_form.removeRow(0)
        self._resolution_boxes={}

    def _refresh_preview(self,*_):
        self._clear_conflicts(); self._update_scope_details(); survivor_id=self.survivor_id()
        if not survivor_id:
            self._plan=None; self.preview.setText("Choose one survivor to calculate the exact merge."); self.conflict_title.hide(); self.conflict_scroll.hide(); self.merge_button.setEnabled(False); return
        donors=[record.id for record in self.records if record.id!=survivor_id]
        try:self._plan=self.service.preview(survivor_id,donors)
        except ValueError as error:
            self._plan=None; self.preview.setText(str(error)); self.conflict_title.hide(); self.conflict_scroll.hide(); self.merge_button.setEnabled(False); return
        plan=self._plan
        self._update_scope_details(plan)
        retirement_summary=(
            f"{len(plan.retired_relationship_ids):,} canonical relationship record{'s' if len(plan.retired_relationship_ids)!=1 else ''} will be retired because the merged endpoints would otherwise become self-referential or duplicate another canonical relationship."
            if plan.retired_relationship_ids
            else "No canonical relationship records will be retired."
        )
        self.preview.setText(
            f"{len(plan.donor_ids):,} donor identit{'y' if len(plan.donor_ids)==1 else 'ies'} will redirect to {plan.survivor_name}. "
            f"Scope: {len(plan.aliases_added):,} alternate name{'s' if len(plan.aliases_added)!=1 else ''}, "
            f"{len(plan.affected_tag_ids):,} dependent tag record{'s' if len(plan.affected_tag_ids)!=1 else ''}, "
            f"{len(plan.category_base_ids):,} category base reference{'s' if len(plan.category_base_ids)!=1 else ''}, "
            f"{plan.character_link_count:,} Character Knowledge link{'s' if plan.character_link_count!=1 else ''}, and "
            f"{len(plan.canonical_relationship_ids):,} canonical relationship record{'s' if len(plan.canonical_relationship_ids)!=1 else ''}. "
            f"{retirement_summary}"
        )
        self.conflict_title.setVisible(bool(plan.conflicts))
        self.conflict_scroll.setVisible(bool(plan.conflicts))
        for conflict in plan.conflicts:
            box=QComboBox(); box.setAccessibleName(f"Resolution for {conflict.label}"); box.addItem("Choose…","")
            for choice in conflict.choices:box.addItem(choice.label,choice.key)
            box.currentIndexChanged.connect(self._update_merge_enabled); self.conflict_form.addRow(conflict.label,box); self._resolution_boxes[conflict.key]=box
        self._update_merge_enabled()

    def _update_merge_enabled(self,*_):
        self.merge_button.setEnabled(bool(self._plan and all(box.currentData() for box in self._resolution_boxes.values())))

    def _accept_if_complete(self):
        if self.merge_button.isEnabled():self.accept()


class CategoryBroaderTagResetDialog(QDialog):
    """Preview one explicit, bounded return to dynamic category inheritance."""

    def __init__(self,session,category_id,parent=None):
        super().__init__(parent); self.session=session; self.category_id=str(category_id); self._plan=None
        category=session.get(self.category_id); name=category.name if category is not None else self.category_id
        self.setWindowTitle("Reset Member Broader Tags"); self.resize(820,600)
        layout=QVBoxLayout(self)
        heading=QLabel(f"Reset member Broader Tags in {name}"); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        self.explanation=QLabel(
            "This clears only explicit Broader Tag overrides for members in the selected scope. Each reset Tag will dynamically use its category Base Tag afterward. Category membership, Image Knowledge, TXT captions, Metadata, image files, raw evidence, and Automation rules are not changed."
        )
        explanation=self.explanation
        explanation.setWordWrap(True); explanation.setObjectName("mutedLabel"); layout.addWidget(explanation)
        self.include_descendants=QCheckBox("Include members of descendant categories")
        self.include_descendants.setAccessibleDescription(
            "When selected, the preview and reset include direct members of every child category below the selected category."
        )
        layout.addWidget(self.include_descendants)
        self.summary=QLabel(); self.summary.setWordWrap(True); self.summary.setAccessibleName("Broader Tag reset scope summary"); layout.addWidget(self.summary)
        self.details=QTextEdit(); self.details.setReadOnly(True); self.details.setAccessibleName("Exact member Broader Tag reset preview"); layout.addWidget(self.details,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.reset_button=buttons.addButton("Reset Broader Tags",QDialogButtonBox.AcceptRole); self.reset_button.setProperty("primary",True)
        self.reset_button.clicked.connect(self._accept_current); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.include_descendants.toggled.connect(self._refresh_preview)
        self._refresh_preview()

    def plan(self):return self._plan

    def _refresh_preview(self,*_):
        try:
            self._plan=self.session.preview_member_broader_tag_reset(
                self.category_id,include_descendants=self.include_descendants.isChecked(),
            )
        except (KeyError,ValueError) as error:
            self._plan=None; self.summary.setText(str(error)); self.details.clear(); self.reset_button.setEnabled(False); return
        plan=self._plan; eligible=len(plan.reset_tag_ids); ambiguous=plan.count("ambiguous"); cycles=plan.count("cycle"); unavailable=plan.count("unavailable"); no_ops=plan.count("no_override")
        direct=sum(row.scope=="direct" for row in plan.rows); descendants=len(plan.rows)-direct
        self.summary.setText(
            f"Scope: {direct:,} direct member{'s' if direct!=1 else ''}"
            + (f" and {descendants:,} descendant member{'s' if descendants!=1 else ''}" if plan.include_descendants else "")
            + f". {eligible:,} explicit override{'s' if eligible!=1 else ''} can be reset; "
            f"{ambiguous:,} ambiguous, {cycles:,} cyclic, {unavailable:,} unavailable, and {no_ops:,} already inherit or have no override. Ambiguous and cyclic overrides are cleared together; unavailable rows are held unchanged."
        )
        lines=[]
        for row in plan.rows[:200]:
            record=self.session._state.tags.get(row.tag_id); name=record.display if record is not None else row.tag_id
            current=", ".join(
                self.session._state.tags[target_id].display if target_id in self.session._state.tags else f"missing {target_id}"
                for target_id in row.target_ids
            ) or "no explicit Broader Tag"
            result=(
                "category anchor (no self-link)" if row.result_kind=="anchor" else
                self.session._state.tags[row.result_base_id].display if row.result_base_id in self.session._state.tags else
                "no effective category Base Tag"
            )
            action="RESET" if row.can_reset else "HOLD" if row.status=="unavailable" else "NO CHANGE"
            lines.append(
                f"{action} — {name} ({row.tag_id}) — {row.scope}; {row.status}; "
                f"current: {current}; after reset: {result}; authority: {row.authority}."
            )
        if len(plan.rows)>200:lines.append(f"Showing 200 of {len(plan.rows):,} rows; the counts above cover the exact complete scope.")
        self.details.setPlainText("\n".join(lines) or "No SDXL Tags are members of this scope.")
        self.reset_button.setText(f"Reset {eligible:,} Broader Tag{'s' if eligible!=1 else ''}")
        self.reset_button.setEnabled(bool(eligible))

    def _accept_current(self):
        if self._plan is not None and self._plan.reset_tag_ids:self.accept()


class CategoryCreationDialog(QDialog):
    """Collect one category choice while Category Workspace owns its save."""

    create_requested=Signal(str,object)

    def __init__(
        self,session,*,initial_name="",initial_parent_id=None,parent=None,
    ):
        super().__init__(parent); self.session=session; self._saving=False; self._created=None
        self.setWindowTitle("Create Category"); self.resize(620,260)
        layout=QVBoxLayout(self)
        heading=QLabel("Create a System Knowledge Category")
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        self.explanation=QLabel(
            "The Category saves as its own verified System Knowledge change. "
            "If this was opened while reviewing new Tag names, those Tag decisions "
            "remain separate and are not saved until you choose Save Decisions there."
        )
        explanation=self.explanation
        explanation.setWordWrap(True); explanation.setObjectName("mutedLabel")
        layout.addWidget(explanation)
        form=QFormLayout()
        self.name=QLineEdit(str(initial_name or "").strip())
        self.name.setAccessibleName("New category name")
        self.name.setPlaceholderText("Enter a clear category name")
        self.parent_choice=BoundedSearchableComboBox(
            placeholder="Type any part of a category path…",max_visible=12,
        )
        self.parent_choice.setAccessibleName("Parent category for new category")
        self.parent_choice.setAccessibleDescription(
            "Choose Top level or one existing semantic parent by its complete path."
        )
        self.parent_choice.addItem("Top level","")
        for record in session.all_categories():
            self.parent_choice.addItem(session.path_label(record.id),record.id)
        parent_index=self.parent_choice.findData(str(initial_parent_id or ""))
        self.parent_choice.setCurrentIndex(max(0,parent_index))
        form.addRow("Name",self.name); form.addRow("Parent category",self.parent_choice)
        layout.addLayout(form)
        self.status=QLabel(); self.status.setWordWrap(True); self.status.setObjectName("errorLabel")
        self.status.setAccessibleName("Category creation status"); self.status.hide()
        layout.addWidget(self.status)
        self.buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.create_button=self.buttons.addButton("Create Category",QDialogButtonBox.AcceptRole)
        self.create_button.setProperty("primary",True)
        self.create_button.setAccessibleDescription(
            "Save this Category through the verified Category Workspace transaction."
        )
        self.create_button.clicked.connect(self._request_create)
        self.buttons.rejected.connect(self.reject); layout.addWidget(self.buttons)
        self.name.textChanged.connect(self._refresh_create_enabled)
        self._refresh_create_enabled()

    def created_category(self):return self._created

    def _refresh_create_enabled(self,*_args):
        self.create_button.setEnabled(bool(self.name.text().strip()) and not self._saving)

    def _request_create(self):
        if self._saving:return
        self.status.hide()
        self.create_requested.emit(
            self.name.text().strip(),str(self.parent_choice.currentData() or "") or None,
        )

    def set_saving(self):
        self._saving=True; self.name.setEnabled(False); self.parent_choice.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Cancel).setEnabled(False)
        self.create_button.setEnabled(False); self.create_button.setText("Saving Category…")
        self.status.setObjectName("mutedLabel"); self.status.setText(
            "Saving and verifying the Category Workspace change…"
        ); self.status.show()

    def set_error(self,message):
        self._saving=False; self.name.setEnabled(True); self.parent_choice.setEnabled(True)
        self.buttons.button(QDialogButtonBox.Cancel).setEnabled(True)
        self.create_button.setText("Create Category")
        self.status.setObjectName("errorLabel"); self.status.setText(str(message)); self.status.show()
        self._refresh_create_enabled(); self.name.setFocus(Qt.OtherFocusReason)

    def finish(self,category_id,path_label):
        self._created=(str(category_id),str(path_label)); self.accept()

    def reject(self):
        if not self._saving:super().reject()


class CategoryWorkspace(QWidget):
    """One category canvas whose confirmed System Knowledge changes autosave."""

    # Keep type-ahead from materializing the complete category tree for every
    # character. Empty queries remain immediate, while non-empty input is
    # coalesced before rebuilding the tree.
    SEARCH_DEBOUNCE_MS=180

    open_automation_requested=Signal(object)
    update_matching_images_requested=Signal(object)
    image_data_changed=Signal(object)
    tag_removal_finished=Signal()
    workspace_saved=Signal(); staged_state_changed=Signal(bool); edit_tag_requested=Signal(str); edit_category_appearance_requested=Signal(str); change_tags_category_requested=Signal(object); add_tag_requested=Signal(str)
    routine_change_saved=Signal(object); always_ignore_tags_requested=Signal(object)
    edit_image_data=Signal(str); force_ai_requested=Signal(object,str); show_dataset_tag=Signal(str)
    context_changed=Signal(); show_all_tags_requested=Signal()
    focus_global_search_requested=Signal()
    def __init__(
        self,tags,categories,patterns,*,characters=None,settings=None,image_index_provider=None,
        pool=None,gallery_status=None,job_manager=None,evidence_queries=None,
        defer_sequence=False,prepared_session=None,identity_group_cache=None,
        defer_initial_details=False,prepared_structure_search_index=None,
        history_operation_factory=None,history_opener=None,history_recovery_handler=None,
        session_factory=None,merge_service_factory=None,merge_saved_adopter=None,
        parent=None,
    ):
        super().__init__(parent); self.tags=tags; self.categories=categories; self.patterns=patterns; self.characters=characters; self.settings=settings
        self.image_index_provider=image_index_provider; self.pool=pool; self.gallery_status=gallery_status
        self.job_manager=job_manager
        self._removal_history_factory=history_operation_factory
        self._removal_history_opener=history_opener
        self._removal_recovery_handler=history_recovery_handler
        self._session_factory=session_factory or (lambda *args:CategoryWorkspaceSession(*args))
        self._merge_service_factory=merge_service_factory or (lambda *args:TagIdentityMergeService(*args))
        self._merge_saved_adopter=merge_saved_adopter
        self._image_tag_removal_dialogs=[]
        self.evidence_queries=evidence_queries or EvidenceQueryService()
        self._identity_group_cache=dict(identity_group_cache or {})
        if prepared_session is None:
            admission=getattr(tags,"rule_pair_admission",None)
            with (admission.admit() if admission is not None else nullcontext()):
                TagIdentityMergeService.recover_interrupted(tags.path.parent)
                CategoryWorkspaceSession.recover_interrupted(tags.path.parent)
                categories.load(); tags.load(); patterns.load(); tags.apply_category_ordering(categories)
            prepared_session=self._session_factory(
                tags,categories,patterns,characters,
            )
        elif (
            prepared_session.live_tags is not tags
            or prepared_session.live_categories is not categories
            or prepared_session.live_patterns is not patterns
        ):
            raise ValueError(
                "Prepared Category Workspace state no longer matches the active System Knowledge."
            )
        self.session=prepared_session; self.selected_key=None; self._loading_details=False; self._refreshing_views=False; self._detail_category_id=""; self._detail_counts=(0,0); self._last_staged_state=None; self._last_health_issues=None
        self._structure_result_summary=""
        self._category_base_review_context={}
        self._policy_search_tag_ids=frozenset(); self._policy_search_summary=""
        self._review_filter_id="all"; self._review_filter_tag_ids=None; self._review_filter_label="All System Knowledge"
        self._defer_sequence=bool(defer_sequence)
        self._defer_initial_details=bool(defer_initial_details)
        self._sequence_ready=not bool(defer_sequence)
        self._gallery_generation=0; self._gallery_task=None; self._gallery_context_key=None
        self._membership_filter_cache=None
        self._structure_search_cache=(
            (
                self.session._state,None,frozenset(),
                *prepared_structure_search_index,
            )
            if prepared_structure_search_index is not None else None
        )
        self._search_timer=QTimer(self); self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._apply_search)
        self._automatic_save_task=None; self._automatic_save_outcome=None; self._automatic_save_window=None; self._automatic_save_focus=None
        self._category_edit_dialog=None; self._category_edit_status=None; self._category_edit_buttons=None
        self._category_edit_validation_pending=False
        self._details_timer=QTimer(self); self._details_timer.setSingleShot(True); self._details_timer.setInterval(450); self._details_timer.timeout.connect(self._stage_details)
        self._category_name_dirty=False
        self._build(); self.refresh_all()

    def _build(self):
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(8)
        intro=QLabel(
            "Search or browse a category, then double-click a Tag to open its details. "
            "Select several Tags for shared actions or to copy their names."
        )
        intro.setObjectName("mutedLabel"); intro.setWordWrap(True); root.addWidget(intro)
        top=QHBoxLayout(); self.top_actions_layout=top; self.search=QLineEdit(); self.search.setPlaceholderText("Find a category, path, or SDXL Tag…"); self.search.setAccessibleName("Search Category Workspace")
        self.search.setAccessibleDescription("Filter semantic categories, complete category paths, aliases, and SDXL Tags without changing saved knowledge.")
        self.move_button=QPushButton("Move"); self.move_button.setProperty("compact",True); self.move_menu=QMenu(self.move_button); self.move_button.setMenu(self.move_menu)
        self.actions_button=QPushButton("Actions"); self.actions_button.setProperty("compact",True); self.actions_menu=QMenu(self.actions_button); self.actions_button.setMenu(self.actions_menu)
        top.addWidget(self.search,1); top.addWidget(self.move_button); top.addWidget(self.actions_button); root.addLayout(top)
        self.review_filter_group=QGroupBox("Filtered review")
        review_filter_layout=QHBoxLayout(self.review_filter_group)
        self.review_filter_summary=QLabel(); self.review_filter_summary.setWordWrap(True)
        self.review_filter_summary.setAccessibleName("Active Tag Encyclopedia review filter")
        self.review_category_base_button=QPushButton("Review Category Base…"); self.review_category_base_button.setProperty("compact",True)
        self.review_category_base_button.setToolTip("Open the selected category's existing base-tag and inheritance controls.")
        self.review_show_all_button=QPushButton("Show All Tags",self); self.review_show_all_button.hide()
        review_filter_layout.addWidget(self.review_filter_summary,1); review_filter_layout.addWidget(self.review_category_base_button)
        self.review_filter_group.hide(); root.addWidget(self.review_filter_group)
        self.views=QTabWidget(); self.views.setAccessibleName("Category Structure"); self.views.setAccessibleDescription("Browse semantic categories and manage their direct SDXL Tags from one canvas."); self.views.addTab(self._build_structure(),"Category Structure"); self.views.tabBar().hide()
        self.sequence=TagOrderWorkspace(self.tags,self.categories,settings=self.settings,image_index_provider=self.image_index_provider,session=self.session,embedded=True,defer_refresh=self._defer_sequence)
        self.sequence_holder=QWidget(self); self.sequence.setParent(self.sequence_holder); self.sequence.hide(); root.addWidget(self.views,1)
        self.views.setProperty("learningTopicId","system-knowledge.category-workspace")
        self.context_status=QLabel(); self.context_status.setObjectName("mutedLabel"); self.context_status.setWordWrap(True); self.context_status.setAccessibleName("Category Workspace selection and validation"); root.addWidget(self.context_status)
        self.status=QLabel(self); self.status.setObjectName("mutedLabel"); self.status.setWordWrap(True); self.status.setAccessibleName("Category Workspace status")
        bind_status_footer(self,self.status,title="Category Workspace",follow_visibility=True)
        self.matching_images_button=QPushButton("Review Matching Images…")
        self.matching_images_button.setProperty("compact",True)
        self.matching_images_button.setAccessibleDescription(
            "Open Dataset's shared Correct image facts preview for the identities changed by the completed duplicate-tag merge."
        )
        self.matching_images_button.clicked.connect(
            self._open_matching_images_handoff
        )
        self.matching_images_button.hide()
        root.addWidget(self.matching_images_button,0,Qt.AlignLeft)
        self._matching_images_scope={}
        actions=QHBoxLayout(); self.undo_button=QPushButton("Undo"); self.redo_button=QPushButton("Redo")
        self.undo_button.setProperty("compact",True); self.redo_button.setProperty("compact",True)
        self.undo_button.setAccessibleDescription("Undo the last Category Workspace change; already-saved routine moves save their inverse automatically.")
        self.redo_button.setAccessibleDescription("Redo the last Category Workspace change; routine moves save again automatically.")
        actions.addWidget(self.undo_button); actions.addWidget(self.redo_button); actions.addStretch(1); root.addLayout(actions)
        self.search.textChanged.connect(self._search_text_changed)
        self.search.textEdited.connect(self._search_text_edited)
        self.views.currentChanged.connect(self._view_changed)
        self.structure_tree.currentItemChanged.connect(self._structure_selected); self.structure_tree.itemSelectionChanged.connect(self._menus_changed)
        self.structure_tree.itemDoubleClicked.connect(self._activate_structure_item)
        self.structure_tree.semantic_drop.connect(self._semantic_drop); self.structure_tree.setContextMenuPolicy(Qt.CustomContextMenu); self.structure_tree.customContextMenuRequested.connect(self._context_menu)
        self.structure_tree.semantic_tag_drop.connect(self._semantic_tag_drop)
        self.sequence.tree.currentItemChanged.connect(self._sequence_selected)
        self.undo_button.clicked.connect(self._undo); self.redo_button.clicked.connect(self._redo)
        self.move_menu.aboutToShow.connect(self._populate_move_menu); self.actions_menu.aboutToShow.connect(self._populate_actions_menu)
        self.review_category_base_button.clicked.connect(
            lambda:self._focus_category_details(show_policies=True)
        )
        self.review_show_all_button.clicked.connect(self.show_all_tags_requested.emit)

    def use_external_search(self):
        if getattr(self,"_using_external_search",False):return
        self._using_external_search=True; self.search.hide(); self.move_button.hide(); self.actions_button.hide()
        self.move_button.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed); self.actions_button.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed)

    def set_policy_search_matches(self,tag_ids=(),summary=""):
        values=frozenset(str(value) for value in tag_ids if str(value))
        summary=str(summary or "")
        if values==self._policy_search_tag_ids and summary==self._policy_search_summary:return
        self._policy_search_tag_ids=values; self._policy_search_summary=summary
        if self.search.text().strip():self.refresh_structure()

    def _build_structure(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0)
        splitter=QSplitter(Qt.Horizontal); layout.addWidget(splitter,1)
        self.structure_tree=StructureTree(); splitter.addWidget(self.structure_tree)
        inspector=QWidget(); self.inspector=inspector; inspector_layout=QVBoxLayout(inspector); inspector_layout.setContentsMargins(8,0,0,0)
        self.detail_title=QLabel("Choose a category"); self.detail_title.setObjectName("sectionTitle"); self.detail_title.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum); inspector_layout.addWidget(self.detail_title)
        self.category_fields=QWidget(); self.category_fields.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
        category_layout=QVBoxLayout(self.category_fields); category_layout.setContentsMargins(0,0,0,0); category_layout.setSpacing(10)

        def category_section(title,description):
            group=QGroupBox(title); section_layout=QVBoxLayout(group)
            summary=QLabel(description); summary.setObjectName("mutedLabel"); summary.setWordWrap(True); section_layout.addWidget(summary)
            fields=QFormLayout(); fields.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); section_layout.addLayout(fields)
            category_layout.addWidget(group)
            return group,fields

        self.category_name=QLineEdit(); self.category_parent=BoundedSearchableComboBox(placeholder="Type to find a parent category…",max_visible=12)
        self.category_parent.setAccessibleName("Parent category")
        self.category_parent.setAccessibleDescription("Choose where this category belongs in the meaning hierarchy. This is separate from caption order.")
        self.category_parent_value=QLabel(); self.category_parent_value.setObjectName("mutedLabel"); self.category_parent_value.setWordWrap(True); self.category_parent_value.hide()
        self.category_parent_host=QWidget(); parent_layout=QVBoxLayout(self.category_parent_host); parent_layout.setContentsMargins(0,0,0,0); parent_layout.addWidget(self.category_parent); parent_layout.addWidget(self.category_parent_value)
        self.category_description=QTextEdit(); self.category_description.setMaximumHeight(82)

        self.basics_group,basics_form=category_section(
            "Basics",
            "Name the category, choose where it belongs in the category tree, and describe what belongs here.",
        )
        basics_form.addRow("Name",self.category_name)
        self.category_parent_label=QLabel("Parent category"); basics_form.addRow(self.category_parent_label,self.category_parent_host)
        basics_form.addRow("Description",self.category_description)

        self.reusable_group=QGroupBox("Reusable values"); reusable_layout=QVBoxLayout(self.reusable_group)
        reusable_intro=QLabel("Make the reviewed SDXL Tags in this category and its child categories available as one reusable collection. Base Tags still decide whether all, selected, or all-except values are compatible.")
        reusable_intro.setObjectName("mutedLabel"); reusable_intro.setWordWrap(True); reusable_layout.addWidget(reusable_intro)
        self.reusable_modifier_family=QCheckBox("Use this category as a reusable value collection")
        self.reusable_modifier_locked=QLabel("✓ Built-in reusable value collection — always enabled"); self.reusable_modifier_locked.setWordWrap(True); self.reusable_modifier_locked.hide()
        self.reusable_value_summary=QLabel(); self.reusable_value_summary.setObjectName("mutedLabel"); self.reusable_value_summary.setWordWrap(True)
        reusable_layout.addWidget(self.reusable_modifier_family); reusable_layout.addWidget(self.reusable_modifier_locked); reusable_layout.addWidget(self.reusable_value_summary)
        category_layout.addWidget(self.reusable_group)

        self.color_policy=QComboBox()
        for policy in CATEGORY_COLOR_POLICIES:self.color_policy.addItem(CATEGORY_COLOR_POLICY_LABELS[policy],policy)
        self.base_tag_policy=QComboBox()
        base_policy_labels={"inherit":"Inherit from parent category","own":"Use a specific Base SDXL Tag","none":"Do not supply a Base SDXL Tag"}
        for policy in CATEGORY_REFERENCE_POLICIES:self.base_tag_policy.addItem(base_policy_labels[policy],policy)
        self.base_tag_policy.setAccessibleName("Category Base meaning setting")
        self.category_base_tag=TagReferenceEditor(self.tags,self.categories,relation_label="Category Base Tag",single=True)
        self.category_base_scope=QComboBox()
        self.category_base_scope.addItem(
            "Direct members only",CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
        )
        self.category_base_scope.addItem(
            "Direct and descendant members",CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS,
        )
        self.category_base_scope.setAccessibleName("Category Base member scope")
        self.sensitivity_policy=QComboBox()
        sensitivity_policy_labels={"inherit":"Inherit from parent category","own":"Use a specific rating","none":"Leave members unclassified"}
        for policy in CATEGORY_REFERENCE_POLICIES:self.sensitivity_policy.addItem(sensitivity_policy_labels[policy],policy)
        self.sensitivity_policy.setAccessibleName("Category content-sensitivity setting")
        self.category_sensitivity=QComboBox()
        for rating in CONTENT_RATINGS:self.category_sensitivity.addItem(rating.title(),rating)
        self.category_policy_status=QLabel(self.category_fields); self.category_policy_status.hide()
        self.base_policy_status=QLabel(); self.base_policy_status.setObjectName("mutedLabel"); self.base_policy_status.setWordWrap(True); self.base_policy_status.setAccessibleName("Effective category Base meaning")
        self.sensitivity_policy_status=QLabel(); self.sensitivity_policy_status.setObjectName("mutedLabel"); self.sensitivity_policy_status.setWordWrap(True); self.sensitivity_policy_status.setAccessibleName("Effective category content sensitivity")

        self.base_group,base_form=category_section(
            "Base meaning",
            "Set the broader SDXL Tag that members inherit when they do not define their own Base Tag.",
        )
        base_form.addRow("Setting",self.base_tag_policy)
        self.category_base_tag_label=QLabel("Base SDXL Tag"); base_form.addRow(self.category_base_tag_label,self.category_base_tag)
        self.category_base_scope_label=QLabel("Applies to"); base_form.addRow(self.category_base_scope_label,self.category_base_scope)
        base_form.addRow("Effective result",self.base_policy_status)

        self.sensitivity_group,sensitivity_form=category_section(
            "Content sensitivity",
            "Classify members here, inherit a reviewed parent rating, or leave this category unclassified.",
        )
        sensitivity_form.addRow("Setting",self.sensitivity_policy)
        self.category_sensitivity_label=QLabel("Rating"); sensitivity_form.addRow(self.category_sensitivity_label,self.category_sensitivity)
        sensitivity_form.addRow("Effective result",self.sensitivity_policy_status)

        self.reset_member_broader_tags=QPushButton("Review Member Broader Tag Overrides…"); self.reset_member_broader_tags.setProperty("compact",True)
        self.reset_member_broader_tags.setAccessibleName("Review member Broader Tag overrides")
        self.reset_member_broader_tags.setAccessibleDescription("Preview direct or descendant member Tags whose explicit Broader Tag relationships can be cleared so category Base Tag inheritance applies dynamically.")
        color_row=QWidget(); color_layout=QHBoxLayout(color_row); color_layout.setContentsMargins(0,0,0,0)
        self.category_color_row=color_row
        self.category_color_swatch=QLabel(); self.category_color_swatch.setFixedSize(26,26); self.category_color_swatch.setAccessibleName("No category own color selected")
        self.category_color=QLineEdit(); self.category_color.setPlaceholderText("#RRGGBB"); self.category_color.setMaxLength(7); self.category_color.setAccessibleName("Category own color code")
        self.choose_color=QPushButton("Choose…"); self.choose_color.setProperty("compact",True); color_layout.addWidget(self.category_color_swatch); color_layout.addWidget(self.category_color,1); color_layout.addWidget(self.choose_color)
        self.edit_chip_colors=QPushButton("Customize Tag Chip Colors…"); self.edit_chip_colors.setProperty("compact",True); self.edit_chip_colors.setAccessibleName("Edit this category's presentation-only SDXL Tag chip colors")
        self.reusable_modifier_family.setAccessibleDescription(
            "Treat reviewed SDXL Tags in this category and its child categories as one reusable value collection. This supplies available values but does not make the collection compatible with any Base Tag."
        )
        self.reusable_modifier_family.setToolTip(
            "Examples: designate Animals, Colors, Patterns, or Materials here. Base Tags choose the collection separately and can allow all, only selected, or all except selected values."
        )

        self.appearance_policy_status=QLabel(); self.appearance_policy_status.setObjectName("mutedLabel"); self.appearance_policy_status.setWordWrap(True); self.appearance_policy_status.setAccessibleName("Effective category appearance")
        self.appearance_group,appearance_form=category_section(
            "Appearance",
            "Choose the category color policy and optionally customize individual Tag chips. Appearance never changes tag meaning.",
        )
        appearance_form.addRow("Category color",self.color_policy)
        self.category_color_label=QLabel("Own color"); appearance_form.addRow(self.category_color_label,color_row)
        appearance_form.addRow("Effective result",self.appearance_policy_status)
        appearance_form.addRow("Individual Tag chips",self.edit_chip_colors)

        self.category_effective_summary=QLabel()
        self.category_effective_summary.setObjectName("mutedLabel")
        self.category_effective_summary.setWordWrap(True)
        self.category_effective_summary.setAccessibleName("Current category meaning and classification")
        category_layout.addWidget(self.category_effective_summary)
        self.category_advanced_group=QGroupBox("Category meaning and classification options")
        advanced_layout=QVBoxLayout(self.category_advanced_group)
        for group in (self.reusable_group,self.base_group,self.sensitivity_group):
            category_layout.removeWidget(group)
            advanced_layout.addWidget(group)
        maintenance=QGroupBox("Advanced maintenance")
        maintenance_layout=QVBoxLayout(maintenance)
        advanced_summary=QLabel("Review explicit member Broader Tag links before clearing them so those Tags dynamically inherit this category's Base meaning. The preview changes no Image Knowledge, captions, Metadata, or image files.")
        advanced_summary.setObjectName("mutedLabel"); advanced_summary.setWordWrap(True); maintenance_layout.addWidget(advanced_summary); maintenance_layout.addWidget(self.reset_member_broader_tags,0,Qt.AlignLeft)
        advanced_layout.addWidget(maintenance)
        self.category_advanced_toggle=DisclosureButton("Show meaning, reusable values and sensitivity…","Hide category options",self.category_advanced_group)
        self.category_advanced_toggle.setAccessibleDescription("Show exact Base meaning, reusable collection, sensitivity and previewed relationship repair settings.")
        category_layout.addWidget(self.category_advanced_toggle,0,Qt.AlignLeft); category_layout.addWidget(self.category_advanced_group)
        category_layout.addStretch(1)
        inspector_layout.addWidget(self.category_fields); self.category_fields.hide()
        self.detail_summary=QLabel(); self.detail_summary.setObjectName("mutedLabel"); self.detail_summary.setWordWrap(True); self.detail_summary.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum); inspector_layout.addWidget(self.detail_summary)
        self.initial_actions=QWidget(); self.initial_actions.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum); initial_actions_layout=QHBoxLayout(self.initial_actions); initial_actions_layout.setContentsMargins(0,0,0,0)
        self.find_tag_button=QPushButton("Find a Tag"); self.find_tag_button.setProperty("primary",True)
        self.find_tag_button.setAccessibleDescription("Focus the Tag Encyclopedia search without changing any saved Tags or categories.")
        self.browse_categories_button=QPushButton("Browse Categories")
        self.browse_categories_button.setAccessibleDescription("Focus the existing category tree without selecting or changing a category.")
        initial_actions_layout.addWidget(self.find_tag_button); initial_actions_layout.addWidget(self.browse_categories_button); initial_actions_layout.addStretch(1)
        inspector_layout.addWidget(self.initial_actions,0,Qt.AlignTop); self.initial_actions.hide()
        self.find_tag_button.clicked.connect(self.focus_global_search_requested.emit)
        self.browse_categories_button.clicked.connect(
            lambda:QTimer.singleShot(0,self.structure_tree.setFocus)
        )
        membership_header=QHBoxLayout(); self.membership_label=QLabel("Direct SDXL Tags"); self.membership_label.setObjectName("sectionTitle")
        self.add_tag_button=QPushButton("Add Tag…"); self.add_tag_button.setProperty("primary",True)
        self.context_actions_button=QToolButton(); self.context_actions_button.setText("Actions"); self.context_actions_button.setPopupMode(QToolButton.InstantPopup); self.context_actions_button.setProperty("compact",True); self.context_actions_button.setAccessibleName("Actions for the selected category or SDXL Tags"); self.context_actions_button.setMenu(self.actions_menu)
        self.edit_tag_button=QPushButton("Edit Tag…",self); self.change_tag_category_button=QPushButton("Change Category…",self); self.arrange_caption_button=QPushButton("Arrange Caption Order…",self)
        for button in (self.add_tag_button,self.edit_tag_button,self.change_tag_category_button,self.arrange_caption_button):button.setProperty("compact",True)
        for button in (self.edit_tag_button,self.change_tag_category_button,self.arrange_caption_button):button.hide()
        membership_header.addWidget(self.membership_label,1); membership_header.addWidget(self.add_tag_button); membership_header.addWidget(self.context_actions_button); inspector_layout.addLayout(membership_header)
        self.membership_search=QLineEdit(); self.membership_search.setClearButtonEnabled(True); self.membership_search.setPlaceholderText("Filter direct SDXL Tags in this category…"); self.membership_search.setAccessibleName("Filter direct SDXL Tags in selected category"); self.membership_search.setAccessibleDescription("Filter direct members by preferred name or alias without changing the selected category or its example images."); inspector_layout.addWidget(self.membership_search)
        self.membership_chips=TagChipEditor(editable=False,show_status=True,draggable=True,manage=True,automation_actions_provider=lambda:not self.session.has_changes,context_menu_provider=self._membership_context_menu); self.membership_chips.setMinimumHeight(125); inspector_layout.addWidget(self.membership_chips,1)
        self.gallery=EmbeddedGallery(page_size=12,card_width=136); self.gallery.setMinimumHeight(230); inspector_layout.addWidget(self.gallery,2)
        if self.gallery_status is not None:
            self.gallery.visible_paths_changed.connect(self.gallery_status.request)
            self.gallery_status.updated.connect(self.gallery.refresh_cards)
        splitter.addWidget(inspector); splitter.setStretchFactor(0,3); splitter.setStretchFactor(1,4); splitter.setSizes((650,850))
        self.category_name.textEdited.connect(self._category_name_edited); self.category_name.editingFinished.connect(self._flush_details)
        self.category_description.textChanged.connect(self._queue_details); self.category_color.textChanged.connect(self._update_category_color_preview); self.category_color.textEdited.connect(lambda *_:self._queue_details()); self.category_color.editingFinished.connect(self._flush_details)
        self.category_parent.activated.connect(lambda *_:self._stage_details()); self.color_policy.activated.connect(self._color_policy_changed)
        self.base_tag_policy.activated.connect(self._category_policy_changed); self.sensitivity_policy.activated.connect(self._category_policy_changed); self.category_sensitivity.activated.connect(lambda *_:self._stage_details())
        self.category_base_scope.activated.connect(lambda *_:self._stage_details())
        self.category_base_tag.changed.connect(self._category_base_tag_changed)
        self.reusable_modifier_family.toggled.connect(self._reusable_value_toggled)
        self.category_base_tag.edit_requested.connect(self.edit_tag_requested)
        self.reset_member_broader_tags.clicked.connect(self._reset_member_broader_tag_overrides)
        self.choose_color.clicked.connect(self._choose_color); self.edit_chip_colors.clicked.connect(lambda:self.edit_category_appearance_requested.emit(self._detail_category_id) if self._detail_category_id else None); self.gallery.edit_requested.connect(self.edit_image_data); self.gallery.force_ai_requested.connect(self.force_ai_requested); self.gallery.open_dataset_requested.connect(self._open_gallery_in_dataset)
        self.membership_search.textChanged.connect(self._membership_filter_changed)
        self.membership_chips.selection_changed.connect(self._membership_selection_changed); self.membership_chips.edit_requested.connect(self._edit_membership_tag); self.membership_chips.change_category_requested.connect(self._change_membership_categories); self.membership_chips.arrange_requested.connect(self._arrange_membership_tag)
        self.membership_chips.remove_knowledge_requested.connect(self._remove_membership_tags); self.membership_chips.always_ignore_requested.connect(self._always_ignore_membership_tags)
        self.membership_chips.merge_knowledge_requested.connect(self._merge_membership_tags)
        self.add_tag_button.clicked.connect(self._request_add_tag); self.edit_tag_button.clicked.connect(self._edit_selected_membership_tag); self.change_tag_category_button.clicked.connect(self._change_selected_membership_categories); self.arrange_caption_button.clicked.connect(self._arrange_selected_caption_order)
        self._membership_selection_changed(())
        return page

    def _search_text_changed(self,text):
        """Coalesce every non-empty query, including pasted text."""
        if not str(text).strip():
            self._search_timer.stop()
            self._apply_search()
            return
        self._search_timer.setInterval(self.SEARCH_DEBOUNCE_MS)
        self._search_timer.start()

    def _search_text_edited(self,text):
        """Coalesce user type-ahead; clearing remains responsive."""
        if not str(text).strip():
            return
        self._search_timer.setInterval(self.SEARCH_DEBOUNCE_MS)
        self._search_timer.start()

    def _apply_search(self):
        if self.save_in_progress:return
        self.refresh_structure()
        if not self.structure_tree.topLevelItemCount():self._clear_search_selection()
        elif self.selected_key:self._show_details(self.selected_key,refresh_examples=False)
        if self._sequence_ready:self.sequence.search.setText(self.search.text())
        self._refresh_actions()

    def _clear_search_selection(self):
        """An empty result cannot retain an actionable hidden record or gallery."""
        self._details_timer.stop()
        self.selected_key=None
        self._detail_category_id=""
        self._membership_category_id=""
        self.category_fields.hide()
        self.initial_actions.hide()
        self.detail_title.setText("No matching categories or Tags")
        self.detail_summary.setText("Clear the search or choose All tags in Review to broaden the results.")
        self.membership_label.setText("Direct SDXL Tags (0)")
        self.membership_chips.set_tags((),None,None)
        self.membership_chips.hide()
        self.membership_label.hide()
        self.membership_search.hide()
        self.add_tag_button.hide()
        self.context_actions_button.hide()
        self.inspector.layout().setAlignment(Qt.AlignTop)
        if self._gallery_task is not None and hasattr(self._gallery_task,"cancel"):
            self._gallery_task.cancel()
        self._gallery_generation+=1
        self._gallery_task=None
        self._gallery_context_key=None
        self.gallery.set_paths((),total_count=0,context="No matching Tags")
        self.gallery.open_dataset.hide()
        self.gallery.hide()
        self._menus_changed()
        self._update_review_filter_summary()
        self.context_changed.emit()

    def show_initial_guidance(self):
        """Show the existing search and category routes before any selection."""
        if self.selected_key or self.search.text().strip():return
        self._clear_search_selection()
        self.detail_title.setText("Find a Tag or Browse Categories")
        self.detail_summary.setText(
            "Search for one known Tag above to review its meaning, or choose a "
            "category from the tree to browse its Tags. Nothing changes until "
            "you edit or save a selected item."
        )
        self.initial_actions.setVisible(bool(self.structure_tree.topLevelItemCount()))

    # Compatibility for direct callers while search input takes the debounced
    # route above.
    def _search_changed(self,text):
        self._apply_search()

    def _view_changed(self,index):
        if self.save_in_progress:return
        self._flush_details(); selected=self.selected_key; self._menus_changed()
        if index==0:self.refresh_structure()
        else:
            self._sequence_ready=True
            self.sequence.tree.blockSignals(True)
            self.sequence.refresh(); self.sequence.select_key(selected)
            self.sequence.tree.blockSignals(False); self.selected_key=selected
            if selected:self._show_details(selected)
            self.sequence._selection_changed(); self._menus_changed()

    def _set_status(self,text,*,announce=False,local_text=None):
        self.matching_images_button.hide()
        self.context_status.hide(); self.status.setText(text); self.status.setAccessibleName(text); self.status.show()
        if self._category_edit_status is not None:
            summary=local_text or text
            self._category_edit_status.setText(summary)
            self._category_edit_status.setAccessibleName(summary)
            self._category_edit_status.setToolTip(text if summary!=text else "")
            if announce:announce_accessibly(self._category_edit_status,summary)
        if announce:announce_accessibly(self.status,text)

    def _set_context_status(self,text,*,announce=False):
        self.matching_images_button.hide()
        self.status.setText(text); self.status.setAccessibleName(text); self.status.hide()
        self.context_status.setText(text); self.context_status.setAccessibleName(text); self.context_status.show()
        if self._category_edit_status is not None:
            self._category_edit_status.setText(text)
            self._category_edit_status.setAccessibleName(text)
            if announce:announce_accessibly(self._category_edit_status,text)
        if announce:announce_accessibly(self.context_status,text)

    def _open_matching_images_handoff(self):
        if self._matching_images_scope:
            self.update_matching_images_requested.emit(
                dict(self._matching_images_scope)
            )

    def refresh_all(self):
        self.refresh_structure()
        if self._sequence_ready:self.sequence.refresh()
        self._refresh_actions()

    def set_review_filter(self,filter_id,tag_ids=None,label=""):
        """Show one built-in review queue without creating another authority."""
        filter_id=str(filter_id or "all")
        normalized=None if filter_id=="all" else frozenset(str(value) for value in (tag_ids or ()))
        label=str(label or "All System Knowledge")
        changed=(filter_id!=self._review_filter_id or normalized!=self._review_filter_tag_ids or label!=self._review_filter_label)
        self._review_filter_id=filter_id; self._review_filter_tag_ids=normalized; self._review_filter_label=label
        self._update_review_filter_summary()
        if not changed:return
        self.refresh_structure()
        current=self.structure_tree.currentItem(); key=self._key(current)
        if key is not None:
            self.selected_key=key; self._show_details(key,refresh_examples=False)
        else:
            self.selected_key=None
            has_categories=bool(self.structure_tree.topLevelItemCount())
            if not has_categories:
                self._clear_search_selection()
                self._refresh_actions()
                return
            self.detail_title.setText(
                "Choose a category" if has_categories else "No matching categories"
            )
            self.detail_summary.setText(
                "Select a category to load its details."
                if has_categories else f"{label} has no matching SDXL Tags."
            )
            self.category_fields.hide(); self.membership_label.setText("Direct SDXL Tags (0)"); self.membership_chips.set_tags((),None,None)
        self._refresh_actions()

    def set_category_base_review_context(self,scope):
        """Retain one non-writing discovery proposal for the next exact editor."""
        self._category_base_review_context=(
            dict(scope) if isinstance(scope,dict) else {}
        )

    def _review_filter_explanation(self):
        return {
            "needs_organization":"Shows reusable Tags that do not yet have a reviewed category.",
            "sensitivity":"Shows Tags whose content-sensitivity classification still needs review.",
            "missing_relationships":(
                "Shows categorized Tags with no Tag-specific base, inherited Category Base Tag, concept, or modifier/property relationship. "
                "That absence is a review prompt, not proof that every Tag needs another relationship."
            ),
        }.get(self._review_filter_id,"")

    def _update_review_filter_summary(self):
        if not hasattr(self,"review_filter_group"):return
        filtered=self._review_filter_tag_ids is not None
        self.review_filter_group.setVisible(filtered)
        if not filtered:return
        self.review_filter_group.setTitle(f"Filtered review: {self._review_filter_label}")
        selected=self.selected_key if self.selected_key and self.selected_key[0]=="category" else None
        record=self.session.get(selected[1]) if selected else None
        remaining=None
        if record is not None:
            category_ids={record.id,*self.session.descendant_ids(record.id)}
            remaining=sum(
                tag.id in self._review_filter_tag_ids and tag.category in category_ids
                for tag in self.session.all_tags()
            )
        completion=(
            f" {self.session.path_label(record.id)} has 0 remaining matches and stays visible so completing the review cannot look like category deletion."
            if record is not None and remaining==0 else ""
        )
        hidden=" Categories with no matches are otherwise hidden. Choose All tags in Review to leave this filter."
        text=f"{self._review_filter_explanation()}{completion}{hidden}"
        self.review_filter_summary.setText(text); self.review_filter_summary.setAccessibleName(text)
        self.review_category_base_button.setVisible(self._review_filter_id=="missing_relationships" and record is not None)
        self.review_category_base_button.setEnabled(record is not None and not self.has_unsaved_changes())

    def _structure_search_index(self):
        """Return immutable-per-staged-state search inputs for the tree view."""
        state=self.session._state
        cached=self._structure_search_cache
        if (
            cached is not None and cached[0] is state
            and cached[1]==self._review_filter_tag_ids
            and cached[2]==self._policy_search_tag_ids
        ):
            return cached[3:]
        (
            records,children,tags_by_category,paths,ancestors,
            category_haystacks,tag_haystacks,
        )=prepare_category_structure_search_index(
            self.session,self.tags,
            review_filter_tag_ids=self._review_filter_tag_ids,
        )
        self._structure_search_cache=(
            state,self._review_filter_tag_ids,self._policy_search_tag_ids,
            records,children,tags_by_category,paths,ancestors,
            category_haystacks,tag_haystacks,
        )
        return self._structure_search_cache[3:]

    def refresh_structure(self):
        query=self.search.text().strip().casefold(); selected=self.selected_key
        expanded=set(); pending=[self.structure_tree.topLevelItem(index) for index in range(self.structure_tree.topLevelItemCount())]
        while pending:
            item=pending.pop(); key=self._key(item)
            if key and key[0]=="category" and item.isExpanded():expanded.add(key[1])
            pending.extend(item.child(index) for index in range(item.childCount()))
        had_structure=bool(self.structure_tree.topLevelItemCount()); scroll_value=self.structure_tree.verticalScrollBar().value()
        signal_blocker=QSignalBlocker(self.structure_tree) if had_structure else None
        self.structure_tree.clear()
        (
            records,children,tags_by_category,paths,ancestors,
            category_haystacks,tag_haystacks,
        )=self._structure_search_index()
        selected_tag_id=selected[1] if selected and selected[0]=="tag" else ""
        visible=set(records) if self._review_filter_tag_ids is None else set()
        if self._review_filter_tag_ids is not None:
            for category_id in tags_by_category:visible.update(ancestors.get(category_id,()))
        category_matches=set(); matching_tag_ids=set()
        if query:
            query_visible=set()
            for category_id,record in records.items():
                category_match=query in category_haystacks[category_id]
                tag_matches=[
                    tag for tag in tags_by_category.get(category_id,())
                    if query in tag_haystacks[tag.id]
                ]
                if category_match and (self._review_filter_tag_ids is None or tag_matches):category_matches.add(category_id)
                matching_tag_ids.update(tag.id for tag in tag_matches)
                if (category_match and self._review_filter_tag_ids is None) or tag_matches:query_visible.update(ancestors.get(category_id,()))
            policy_matches={
                tag_id for tag_id in self._policy_search_tag_ids
                if tag_id in self.session._state.tags
                and (
                    self._review_filter_tag_ids is None
                    or tag_id in self._review_filter_tag_ids
                )
            }
            matching_tag_ids.update(policy_matches)
            for tag_id in policy_matches:
                query_visible.update(
                    ancestors.get(self.session._state.tags[tag_id].category,())
                )
            visible=query_visible
            if self._review_filter_tag_ids is None and selected and selected[0]=="category" and selected[1] in records:
                visible.update(ancestors[selected[1]])
        elif self._review_filter_tag_ids is not None and selected and selected[0]=="category" and selected[1] in records:
            visible.update(ancestors[selected[1]])
        selected_item=None; added=set()
        selected_tag_category=self.session._state.tags[selected_tag_id].category if selected_tag_id in self.session._state.tags else ""
        protected_ids={
            record.id for record in records.values()
            if self.session.is_protected_category(record.id)
        }
        protected_branch_ids=set(protected_ids); pending_protected=list(protected_ids)
        while pending_protected:
            parent_id=pending_protected.pop()
            for child in children.get(parent_id,()):
                if child.id in protected_branch_ids:continue
                protected_branch_ids.add(child.id); pending_protected.append(child.id)
        def add_branch(record,parent,section_ids,path=()):
            nonlocal selected_item
            if record.id in path or record.id not in visible or record.id not in section_ids:return
            direct=len(tags_by_category.get(record.id,())); descendants=self.session.descendant_ids(record.id); total=direct+sum(len(tags_by_category.get(value,())) for value in descendants); source="Protected" if record.id in protected_ids else "Override" if record.source=="user" else "Default"
            item=QTreeWidgetItem((record.name,f"{direct:,} / {total:,}",source)); item.setData(0,Qt.UserRole,("category",record.id)); item.setToolTip(0,paths[record.id]); item.setData(0,Qt.AccessibleTextRole,f"Category {record.name}; {direct} direct and {total} total SDXL Tags; {source}")
            parent.addChild(item)
            added.add(record.id)
            for child in children.get(record.id,()):add_branch(child,item,section_ids,(*path,record.id))
            if selected==("category",record.id) or selected_tag_category==record.id:selected_item=item
        sections=(
            ("Editable categories",set(records)-protected_branch_ids,
             "Category branches whose reviewed settings and semantic placement can be changed."),
            ("Protected system categories",protected_branch_ids,
             "Branches anchored by built-in semantic-role categories. Protected rows are labelled in Source; editable descendants remain beneath their real parent category."),
        )
        for label,section_ids,tooltip in sections:
            section_ids={category_id for category_id in section_ids if category_id in visible}
            if not section_ids:continue
            section=QTreeWidgetItem((label,f"{len(section_ids):,} categories",""))
            section.setFlags(Qt.ItemIsEnabled)
            section.setToolTip(0,tooltip)
            section.setData(0,Qt.AccessibleTextRole,f"{label}; {len(section_ids):,} categories. {tooltip}")
            self.structure_tree.addTopLevelItem(section)
            roots=sorted(
                (
                    records[category_id] for category_id in section_ids
                    if records[category_id].parent_id not in section_ids
                ),
                key=lambda item:(paths[item.id].casefold(),item.id),
            )
            for record in roots:add_branch(record,section,section_ids)
            for record in sorted(
                (records[category_id] for category_id in section_ids),
                key=lambda item:(paths[item.id].casefold(),item.id),
            ):
                if record.id not in added:add_branch(record,section,section_ids)
            section.setExpanded(True)
        if query:self.structure_tree.expandAll()
        elif had_structure:
            pending=[self.structure_tree.topLevelItem(index) for index in range(self.structure_tree.topLevelItemCount())]
            while pending:
                item=pending.pop(); key=self._key(item)
                if key and key[0]=="category":item.setExpanded(key[1] in expanded)
                pending.extend(item.child(index) for index in range(item.childCount()))
        else:
            for index in range(self.structure_tree.topLevelItemCount()):self.structure_tree.topLevelItem(index).setExpanded(True)
        if selected_item is not None:
            self.structure_tree.blockSignals(True); self.structure_tree.setCurrentItem(selected_item); self.structure_tree.blockSignals(False)
        elif (
            self.structure_tree.topLevelItemCount()
            and not self._defer_initial_details
        ):
            first=self.structure_tree.topLevelItem(0)
            while first is not None and self._key(first) is None and first.childCount():first=first.child(0)
            if first is not None:self.structure_tree.setCurrentItem(first)
        self.structure_tree.verticalScrollBar().setValue(scroll_value)
        view_count=sum(len(values) for values in tags_by_category.values())
        view_categories=len(tags_by_category)
        view_summary="" if self._review_filter_tag_ids is None else f"Review view: {self._review_filter_label} contains {view_count:,} SDXL Tag{'s' if view_count!=1 else ''} across {view_categories:,} categor{'y' if view_categories==1 else 'ies'}."
        if query:
            category_text=f"{len(category_matches):,} matching categor{'y' if len(category_matches)==1 else 'ies'}"
            category_ids={self.session._state.tags[value].category for value in matching_tag_ids}
            tag_text=f"{len(matching_tag_ids):,} matching SDXL Tag(s) across {len(category_ids):,} categor{'y' if len(category_ids)==1 else 'ies'}"
            next_step=("Choose a category to review its Tags." if category_matches or matching_tag_ids else "Clear the search or change the Review filter to broaden the results.")
            self._structure_result_summary=" ".join(value for value in (view_summary,f"Search: {category_text}; {tag_text}. {next_step}",self._policy_search_summary) if value)
        else:self._structure_result_summary=view_summary
        if signal_blocker is not None:del signal_blocker
        self._update_review_filter_summary()

    def _key(self,item):
        return item.data(0,Qt.UserRole) if item is not None else None

    def _structure_selected(self,current,_previous=None):
        if self._refreshing_views or self.save_in_progress:return
        key=self._key(current)
        if not key:return
        self._defer_initial_details=False
        self.selected_key=key; self._show_details(key); self._menus_changed(); self._update_review_filter_summary(); self.context_changed.emit()

    def _sequence_selected(self,current,_previous=None):
        if self._refreshing_views or self.save_in_progress:return
        key=self._key(current)
        if not key or key[0] not in {"category","tag"}:
            self._menus_changed(); return
        self.selected_key=key; self._show_details(key); self._menus_changed(); self.context_changed.emit()

    def _show_details(self,key,*,refresh_examples=True):
        self._details_timer.stop(); self._loading_details=True
        self.inspector.layout().setAlignment(Qt.AlignmentFlag(0))
        self.initial_actions.hide()
        self.context_actions_button.show()
        self.gallery.show()
        self._category_name_dirty=False
        kind,record_id=key
        if kind=="category":
            record=self.session.get(record_id)
            if record is None:self._loading_details=False; return
            category_changed=getattr(self,"_membership_category_id","")!=record.id; self._membership_category_id=record.id
            if category_changed:
                self.membership_search.blockSignals(True); self.membership_search.clear(); self.membership_search.blockSignals(False)
            self.membership_search.setPlaceholderText(f"Filter direct SDXL Tags in {record.name}…"); self.membership_search.show(); self.membership_label.show(); self.membership_chips.show(); self.add_tag_button.show(); self.detail_title.setText(record.name)
            self.category_name.setText(record.name); self.category_description.setPlainText(record.description); self.category_color.setText(record.color)
            self._configure_reusable_value_control(record)
            self.category_parent.clear(); self.category_parent.addItem("Top level","")
            for candidate in sorted(self.session.all_categories(),key=lambda item:self.session.path_label(item.id).casefold()):
                if candidate.id!=record.id and candidate.id not in self.session.descendant_ids(record.id):self.category_parent.addItem(self.session.path_label(candidate.id),candidate.id)
            self.category_parent.setCurrentIndex(max(0,self.category_parent.findData(record.parent_id or ""))); self.category_parent.setToolTip(self.session.path_label(record.parent_id) if record.parent_id else "Top level"); self.color_policy.setCurrentIndex(max(0,self.color_policy.findData(record.color_policy)))
            direct_base_id,direct_base_policy=self._direct_category_base_values(record.id)
            self.base_tag_policy.setCurrentIndex(max(0,self.base_tag_policy.findData(direct_base_policy))); self.category_base_tag.set_values((direct_base_id,) if direct_base_id else ()); self.category_base_tag.set_context("")
            self._set_category_base_scope(record.id)
            self.sensitivity_policy.setCurrentIndex(max(0,self.sensitivity_policy.findData(record.sensitivity_policy))); self.category_sensitivity.setCurrentIndex(max(0,self.category_sensitivity.findData(record.sensitivity_rating or "general")))
            self._update_color_controls(); self._update_category_policy_controls()
            group,_,source=self.session._category_effective(record.id); effective_color,color_source,depth,policy=self.session.effective_color(record.id); direct=self.session.direct_tag_count(record.id); total=self.session.total_tag_count(record.id)
            self._detail_category_id=record.id; self._detail_counts=(direct,total)
            color_text=(f"Effective color {effective_color} from {self.session.path_label(color_source)}" if effective_color and color_source else "No effective category color")
            order_text=(f"{ORDER_GROUP_LABELS[group]} caption group set here" if source==record.id else f"{ORDER_GROUP_LABELS[group]} caption group inherited from {self.session.path_label(source)}")
            self.detail_summary.setText(
                f"{counted_noun(direct,'direct Tag')} • "
                f"{counted_noun(total,'Tag')} including subcategories • "
                f"{record.source.title()}"
            )
            self.detail_summary.setToolTip(
                f"{order_text} • {color_text} • {self._category_semantic_summary(record.id)}"
            )
            self._membership_filter_cache=None
            self._refresh_membership_tags(record,effective_color)
            if refresh_examples:self._load_examples(key)
        else:
            record=self.session._state.tags.get(record_id)
            if record is None:self._loading_details=False; return
            self._membership_category_id=""; self.membership_search.hide(); self.category_fields.hide(); self.membership_label.show(); self.membership_label.setText("Selected SDXL Tag"); self.membership_chips.show(); self.add_tag_button.hide(); self.detail_title.setText(record.display)
            self.detail_summary.setText(f"Category: {self.session.path_label(record.category)} • {'Tag order override' if record.ordering.reviewed else 'Uses category order'} • {len(record.provenance):,} provenance record(s)")
            self.membership_chips.set_tags((record.display,),self._staged_tags(),self.session)
            if refresh_examples:self._load_examples(key)
        self._loading_details=False

    def _membership_filter_changed(self,*_):
        if self._loading_details or not self.selected_key or self.selected_key[0]!="category":return
        record=self.session.get(self.selected_key[1])
        if record is not None:self._refresh_membership_tags(record,self.session.effective_color(record.id)[0])

    def _refresh_membership_tags(self,record,effective_color):
        cache_key=(id(self.session),record.id,self._review_filter_tag_ids)
        cache=self._membership_filter_cache
        if cache is None or cache[0]!=cache_key:
            all_direct_tags=self.session.tag_order(record.id)
            direct_tags=tuple(
                all_direct_tags if self._review_filter_tag_ids is None else
                (
                    tag for tag in all_direct_tags
                    if tag.id in self._review_filter_tag_ids
                )
            )
            search_text=tuple(
                " ".join(self.tags.identity_names(tag)).casefold()
                for tag in direct_tags
            )
            cache=(cache_key,direct_tags,search_text)
            self._membership_filter_cache=cache
        else:
            direct_tags=cache[1]
            search_text=cache[2]
        global_query=self.search.text().strip().casefold(); local_query=self.membership_search.text().strip().casefold(); queries=tuple(value for value in (global_query,local_query) if value)
        available_count=len(direct_tags)
        if queries:
            direct_tags=[
                tag for tag,haystack in zip(direct_tags,search_text)
                if all(query in haystack for query in queries)
            ]
        shown=direct_tags[:200]
        if queries:
            source="current searches" if len(queries)>1 else "category filter" if local_query else "current search"
            text=f"Direct SDXL Tags — {len(direct_tags):,} of {available_count:,} match the {source}"
            if len(shown)<len(direct_tags):text=f"Direct SDXL Tags — showing {len(shown):,} of {len(direct_tags):,} matches ({available_count:,} available)"
            self.membership_label.setText(text)
        elif self._review_filter_tag_ids is not None:
            self.membership_label.setText(f"{self._review_filter_label} — showing {len(shown):,} of {len(direct_tags):,}" if len(direct_tags)>len(shown) else f"{self._review_filter_label} ({len(direct_tags):,})")
        else:self.membership_label.setText(f"Direct SDXL Tags — showing {len(shown):,} of {len(direct_tags):,}" if len(direct_tags)>len(shown) else f"Direct SDXL Tags ({len(direct_tags):,})")
        self.membership_chips.set_tags([tag.display for tag in shown],self._staged_tags(shown),self.session,shared_category=(self.session.path_label(record.id),effective_color,record.description,record.id),stable_ids={tag.display:tag.id for tag in shown})

    def _staged_tags(self,records=None):
        class View:
            def __init__(self,records,knowledge):
                from core import normalize_tag
                self.tags=records; self.lookup={}
                for record in records.values():
                    for value in knowledge.identity_names(record):
                        self.lookup.setdefault(normalize_tag(value),record)
            def find(self,value):
                from core import normalize_tag
                return self.lookup.get(normalize_tag(value))
        scoped_records=(
            self.session._state.tags
            if records is None else {record.id:record for record in records}
        )
        return View(scoped_records,self.tags)

    def _membership_records(self,values=None):
        values=tuple(values if values is not None else self.membership_chips.selected_values())
        records=[]
        for value in values:
            record=self._staged_tags().find(value)
            if record is not None and record.id not in {item.id for item in records}:records.append(record)
        return records

    def _membership_selection_changed(self,values):
        records=self._membership_records(values); count=len(records); has_context=bool(self.selected_key and self.selected_key[0] in {"category","tag"})
        self.edit_tag_button.setEnabled(count==1); self.change_tag_category_button.setEnabled(bool(count)); self.arrange_caption_button.setEnabled(bool(count or has_context))
        self.edit_tag_button.setToolTip("Edit the selected SDXL Tag in a focused window." if count==1 else "Select one SDXL Tag to edit.")
        self.change_tag_category_button.setToolTip(f"Preview and automatically save one category change for all {count:,} selected SDXL Tags." if count else "Select one or more SDXL Tags.")
        self.arrange_caption_button.setToolTip("Open the temporary caption arrangement tool focused on the selected SDXL Tag." if count==1 else "Open the temporary caption arrangement tool for this category." if has_context else "Choose a category first.")
        if not self._loading_details and self.selected_key and self.selected_key[0]=="category":
            example_key=("membership",tuple(record.id for record in records)) if records else self.selected_key
            if example_key!=self._gallery_context_key:self._load_examples(example_key)
        if hasattr(self,"status"):self._refresh_actions()
        if not self._loading_details:self.context_changed.emit()

    def navigation_context(self):
        """Return stable record and direct-tag selections for the shared workbench."""
        return self.selected_key,tuple(record.id for record in self._membership_records())

    def restore_navigation_context(self,selected_key,membership_tag_ids=()):
        """Restore a validated stable-ID context after search/filter navigation."""
        key=tuple(selected_key) if selected_key else None
        if key and len(key)==2:
            kind,record_id=str(key[0]),str(key[1])
            if kind=="category" and self.session.get(record_id) is not None:key=(kind,record_id)
            elif kind=="tag" and record_id in self.session._state.tags:key=(kind,record_id)
            else:key=None
        else:key=None
        if key is None:
            current=self._key(self.structure_tree.currentItem())
            key=current if current is not None else self.selected_key
        if key is not None:
            self._defer_initial_details=False
            self.selected_key=key; self.refresh_structure()
            if not self.structure_tree.topLevelItemCount():
                self._clear_search_selection()
                return None
            self._show_details(key,refresh_examples=False)
        available={
            record.id:record.display for record in self._membership_records(
                [self.session._state.tags[tag_id].display for tag_id in membership_tag_ids if tag_id in self.session._state.tags]
            )
        }
        self.membership_chips.select_values(available.values())
        self._menus_changed()
        return self.selected_key

    def _edit_membership_tag(self,value):
        record=self._staged_tags().find(value)
        if record is not None:self.edit_tag_requested.emit(record.id)

    def _edit_selected_membership_tag(self):
        records=self._membership_records()
        if len(records)==1:self.edit_tag_requested.emit(records[0].id)

    def _change_membership_categories(self,values):
        records=self._membership_records(values)
        if records:self.change_tags_category_requested.emit(tuple(record.id for record in records))

    def _change_selected_membership_categories(self):
        self._change_membership_categories(self.membership_chips.selected_values())

    def _add_copy_names_action(self,menu):
        records=self._membership_records()
        action=menu.addAction("Copy Selected Tag Name" if len(records)==1 else "Copy Selected Tag Names")
        action.setEnabled(bool(records))
        action.setToolTip(
            "Copy selected preferred names, one per line, for deliberate reuse in a prompt or elsewhere."
            if records else "Select one or more Tags to copy their names."
        )
        action.triggered.connect(self._copy_selected_membership_names)

    def _copy_selected_membership_names(self):
        records=self._membership_records()
        if not records:return
        QApplication.clipboard().setText("\n".join(record.display for record in records))
        announce_accessibly(self.membership_chips,f"Copied {len(records):,} Tag name{'s' if len(records)!=1 else ''}.")

    def _membership_context_menu(self,chip):
        menu=chip.manage_actions_menu()
        menu.addSeparator()
        self._add_copy_names_action(menu)
        return menu

    def _request_add_tag(self):
        if not self.selected_key or self.selected_key[0]!="category":return
        if self.session.has_changes:
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Add or create the SDXL Tag after it finishes."); return
        if self._automatic_save_task is not None:
            QMessageBox.information(self,"Wait for the current save","The current System Knowledge change is still saving. Add the SDXL Tag after it finishes."); return
        if self.session.get(self.selected_key[1]) is not None:self.add_tag_requested.emit(self.selected_key[1])

    def _tag_merge_use_summary(self,records):
        try:
            index=self.image_index_provider() if callable(self.image_index_provider) else self.image_index_provider
            if index is None:return "The included-library index is unavailable, so matching-image counts are not shown. Donor stable IDs still remain as redirecting tombstones."
            audit=ConfirmedTagIdentityService.audit_cached_uses(
                records,index.all_records(),names_for=self.tags.identity_names,
            )
            return (
                f"Current cached library impact: {len(audit.used_paths):,} confirmed image "
                f"{'use' if len(audit.used_paths)==1 else 'uses'} and "
                f"{len(audit.uncertain_paths):,} older or uncertain "
                f"{'record' if len(audit.uncertain_paths)==1 else 'records'}. "
                "This merge changes no image data; later optional normalization remains a separate reviewed Automation job."
            )
        except Exception as error:
            return f"Matching-image counts are unavailable ({error}). Donor stable IDs still remain as redirecting tombstones."

    def _merge_membership_tags(self,values,*,require_selection=True):
        records=self._membership_records(values)
        if len(records)<2:
            QMessageBox.information(self,"Merge not available","Select two or more SDXL Tags that you have determined are the same identity."); return
        if self.has_unsaved_changes():
            QMessageBox.information(self,"Wait for System Knowledge","The current System Knowledge change is still saving. Start the duplicate merge after it finishes."); return
        service=self._merge_service_factory(self.tags,self.categories,self.characters)
        dialog=TagIdentityMergeDialog(records,service,use_summary=self._tag_merge_use_summary(records),parent=self)
        if dialog.exec()!=QDialog.Accepted:return
        survivor_id=dialog.survivor_id(); donor_ids=tuple(record.id for record in records if record.id!=survivor_id)
        if require_selection and set(record.id for record in self._membership_records())!=set(record.id for record in records):
            QMessageBox.warning(self,"Selection changed","The selected SDXL Tags changed while the merge preview was open. Nothing was saved; review the selection and try again."); return
        self._set_status("Saving duplicate SDXL Tag merge…")
        task=AsyncCallTask(lambda:service.apply(survivor_id,donor_ids,resolutions=dialog.resolutions()))
        task.survivor_id=survivor_id; task.donor_ids=donor_ids
        task.return_key=self.selected_key
        task.matching_scope={
            "kind":"system_knowledge_identity",
            "origin":"tag_encyclopedia",
            "stable_ids":(survivor_id,*donor_ids),
            "source_names":tuple(dict.fromkeys(
                value
                for record in records
                for value in self.tags.identity_names(record)
                if str(value).strip()
            )),
            "label":(
                f"duplicate-tag merge into "
                f"{next(record.display for record in records if record.id==survivor_id)}"
            ),
        }
        task.identity_groups=(tuple(task.matching_scope["source_names"]),)
        self._automatic_save_task=task
        task.signals.complete.connect(self._tag_merge_complete)
        task.signals.failed.connect(self._tag_merge_failed)
        self._set_automatic_save_busy(True); self._refresh_actions()
        from PySide6.QtCore import QThreadPool
        (self.pool or QThreadPool.globalInstance()).start(task)

    def merge_tag_ids(self,tag_ids):
        """Open the existing recoverable merge for an explicit stable-ID set."""
        requested=tuple(dict.fromkeys(str(value) for value in tag_ids if str(value)))
        if len(requested)<2 or any(
            tag_id not in self.session._state.tags for tag_id in requested
        ):
            QMessageBox.information(
                self,"Merge not available",
                "One or more SDXL Tags changed before the duplicate review opened. "
                "Reload System Knowledge and compare the current identities again.",
            )
            return False
        values=[
            self.session._state.tags[tag_id].display
            for tag_id in requested
        ]
        return self._merge_membership_tags(values,require_selection=False)

    def _merge_selected_membership_tags(self):
        self._merge_membership_tags(self.membership_chips.selected_values())

    def _tag_merge_complete(self,result):
        task=self._automatic_save_task
        if task is None:return
        survivor_id=str(getattr(task,"survivor_id","")); donor_count=len(tuple(getattr(task,"donor_ids",())))
        matching_scope=dict(getattr(task,"matching_scope",{}) or {})
        classification=classify_system_knowledge_change(
            "tag_identity_merge",label=str(matching_scope.get("label") or ""),
            affected_tag_ids=tuple(matching_scope.get("stable_ids",()) or ()),
            source_names=tuple(matching_scope.get("source_names",()) or ()),
            matching_images=bool(matching_scope),
        )
        self._automatic_save_task=None; self._automatic_save_outcome=True; self._set_automatic_save_busy(False)
        donor_ids=tuple(getattr(task,"donor_ids",()))
        def refresh_failed():
            self._matching_images_scope=matching_scope
            self.workspace_saved.emit()
            self.routine_change_saved.emit({
                "kind":"tag_identity_merge","tag_ids":(survivor_id,),
                "identity_groups":tuple(getattr(task,"identity_groups",())),
                "matching_scope":matching_scope,"classification":classification,
            })
            self._set_status(
                "The duplicate merge was saved and verified, but the Tag Encyclopedia could not refresh to show it. Restart CollectiGen before making another System Knowledge change.",
                announce=True,
            )
            self.matching_images_button.setVisible(bool(matching_scope))
        try:
            if self._merge_saved_adopter is not None:self._merge_saved_adopter(result)
            if self.characters is not None:self.characters.load()
            self.categories.load(); self.tags.load(); self.patterns.load(); self.tags.apply_category_ordering(self.categories)
            self.session=self._session_factory(self.tags,self.categories,self.patterns,self.characters); self.sequence.session=self.session
        except Exception:
            refresh_failed()
            return
        if (
            survivor_id not in self.session._state.tags
            or any(donor_id in self.session._state.tags for donor_id in donor_ids)
            or any(self.tags.resolve_tag_id(donor_id)!=survivor_id for donor_id in donor_ids)
        ):
            self.selected_key=None
            self.refresh_all()
            refresh_failed()
            return
        return_key=getattr(task,"return_key",None)
        if (
            return_key
            and return_key[0]=="category"
            and self.session.get(return_key[1]) is not None
        ):
            self.selected_key=return_key
        else:
            self.selected_key=("tag",survivor_id)
        self.refresh_all()
        self._show_details(self.selected_key,refresh_examples=False)
        if self.selected_key[0]=="category":
            survivor=self.session._state.tags[survivor_id]
            if survivor.category==self.selected_key[1]:
                self.membership_chips.select_values((survivor.display,))
        self.routine_change_saved.emit({
            "kind":"tag_identity_merge","tag_ids":(survivor_id,),
            "identity_groups":tuple(getattr(task,"identity_groups",())),
            "matching_scope":matching_scope,"classification":classification,
        })
        self._set_status(
            f"Saved duplicate merge into {self.tags.tags[survivor_id].display}; "
            f"{donor_count:,} donor identit{'y' if donor_count==1 else 'ies'} now redirect to the survivor. "
            "Image data was not rewritten. "+classification.completion_summary(),
            announce=True,
        )
        self._matching_images_scope=matching_scope
        self.matching_images_button.setVisible(bool(matching_scope))

    def _tag_merge_failed(self,message):
        if self.characters is not None:
            try:self.characters.load()
            except Exception:pass
        self._automatic_save_failed(message,operation="Duplicate merge")

    def _remove_membership_tags(self,values):
        records=self._membership_records(values)
        if not records:return
        tag_ids=tuple(record.id for record in records); names=[record.display for record in records]
        try:
            index=self.image_index_provider() if callable(self.image_index_provider) else self.image_index_provider
            if index is None:
                raise RuntimeError(
                    "The working-library index is unavailable. Refresh the "
                    "library before removing a reusable SDXL Tag."
                )
            use_audit=ConfirmedTagIdentityService.audit_cached_uses(
                records,index.all_records(),names_for=self.tags.identity_names,
            )
        except Exception as error:
            QMessageBox.information(
                self,
                "Removal needs a current library check",
                f"{error}\n\nNo System Knowledge or image data was changed.",
            )
            return
        try:impact=self.session.preview_tag_removal(tag_ids)
        except ValueError as error:QMessageBox.information(self,"Remove not available",str(error)); return
        shown=", ".join(names[:8]); remainder=len(names)-8
        selected_text=shown+(f" and {remainder:,} more" if remainder>0 else "")
        links=impact.parent_links+impact.implication_links+impact.conflict_links
        link_text=(
            f" {links:,} incoming tag relationship{'s' if links!=1 else ''} on "
            f"{len(impact.relationship_record_ids):,} remaining tag{'s' if len(impact.relationship_record_ids)!=1 else ''} will also be detached."
            if links else " No other tag relationships point to this selection."
        )
        if impact.category_base_refs:
            link_text+=f" {len(impact.category_base_refs):,} category base-tag reference{'s' if len(impact.category_base_refs)!=1 else ''} will return to inherited or unset behavior."
        relationship_details=""
        if impact.canonical_relationship_ids:
            visible_ids=impact.canonical_relationship_ids[:8]
            canonical_ids=", ".join(visible_ids)
            hidden_ids=len(impact.canonical_relationship_ids)-len(visible_ids)
            if hidden_ids:
                canonical_ids+=f" and {hidden_ids:,} more (open Details for every ID)"
            link_text+=(
                f" {len(impact.canonical_relationship_ids):,} canonical relationship "
                f"record{'s' if len(impact.canonical_relationship_ids)!=1 else ''} will be updated: {canonical_ids}."
            )
            retired=set(impact.retired_relationship_ids)
            relationship_details="Affected canonical relationship IDs:\n"+"\n".join(
                f"{relationship_id}{' (will become disabled history)' if relationship_id in retired else ''}"
                for relationship_id in impact.canonical_relationship_ids
            )
        else:
            link_text+=" No canonical relationship records use this selection."
        if impact.retired_relationship_ids:
            visible_retired_ids=impact.retired_relationship_ids[:8]
            retired_ids=", ".join(visible_retired_ids)
            hidden_retired_ids=len(impact.retired_relationship_ids)-len(visible_retired_ids)
            if hidden_retired_ids:
                retired_ids+=f" and {hidden_retired_ids:,} more (open Details for every ID)"
            link_text+=(
                f" {len(impact.retired_relationship_ids):,} will be retained as disabled historical "
                f"record{'s' if len(impact.retired_relationship_ids)!=1 else ''}: {retired_ids}."
            )
        affected_paths=tuple(dict.fromkeys((*use_audit.used_paths,*use_audit.uncertain_paths)))
        remove_everywhere=False
        if affected_paths:
            prompt=QMessageBox(self)
            prompt.setIcon(QMessageBox.Warning)
            prompt.setTextFormat(Qt.PlainText)
            prompt.setWindowTitle("Confirmed image facts still use this Tag")
            prompt.setText(
                f"Review {len(affected_paths):,} affected images before removing "
                f"{len(records):,} reusable Tag{'s' if len(records)!=1 else ''}."
            )
            prompt.setInformativeText(
                f"Confirmed uses: {len(use_audit.used_paths):,}\n"
                f"Older image records to check: {len(use_audit.uncertain_paths):,}\n\n"
                "Review the images first. Confirming removal there changes their "
                "selected confirmed Tag facts before removing the reusable Tag.\n\n"
                "Captions, Metadata, original images and raw evidence stay unchanged. "
                "Cancel changes nothing."
            )
            if len(impact.canonical_relationship_ids)>8:
                prompt.setDetailedText(relationship_details)
            remove_all=prompt.addButton("Review Affected Images…",QMessageBox.AcceptRole)
            cancel=prompt.addButton("Cancel",QMessageBox.RejectRole)
            prompt.setDefaultButton(cancel)
            prompt.exec()
            if prompt.clickedButton() is not remove_all:return
            service=ImageTagRemovalService(
                self.tags,
                OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
                index=index,
            )
            relationship_review=[]
            if links:
                relationship_review.append(f"{links:,} incoming Tag relationships will be detached.")
            if impact.category_base_refs:
                relationship_review.append(f"{len(impact.category_base_refs):,} category base references will return to inherited or unset behavior.")
            if impact.canonical_relationship_ids:
                relationship_review.append(f"{len(impact.canonical_relationship_ids):,} canonical relationship records will be updated.")
            if impact.retired_relationship_ids:
                relationship_review.append(f"{len(impact.retired_relationship_ids):,} relationship records will remain as disabled history.")
            if relationship_details:
                relationship_review.append(relationship_details)
            dialog=ImageTagRemovalDialog(
                service,affected_paths,tag_ids,pool=self.pool,parent=self,
                relationship_summary="\n".join(relationship_review),
                history_operation_factory=self._removal_history_factory,
                history_opener=self._removal_history_opener,
                recovery_handler=self._removal_recovery_handler,
            )
            self._image_tag_removal_dialogs.append(dialog)
            try:
                accepted=dialog.exec()==QDialog.Accepted and dialog.completed
                changed=dialog.changed_paths()
                if changed:self.image_data_changed.emit(tuple(changed))
            finally:
                self._image_tag_removal_dialogs.remove(dialog)
                self.tag_removal_finished.emit()
            if not accepted:return
            remove_everywhere=True
        message=(
            f"Remove {len(records):,} selected SDXL Tag{'s' if len(records)!=1 else ''} from System Knowledge?\n\n"
            f"Selected: {selected_text}.\n\n{link_text}\n\n"
            "This removes reusable tag records only. Image Knowledge, captions, Metadata, raw evidence, image files, and Rules & Automation remain unchanged. "
            "Shipped tags stay removed through a local tombstone. Confirming this preview saves the complete removal automatically as one recoverable transaction."
        )
        if not remove_everywhere:
            if len(impact.canonical_relationship_ids)>8:
                confirmation=QMessageBox(QMessageBox.Warning,"Remove from System Knowledge",message,QMessageBox.Yes|QMessageBox.No,self)
                confirmation.setDefaultButton(QMessageBox.No)
                confirmation.setDetailedText(relationship_details)
                if confirmation.exec()!=QMessageBox.Yes:return
            elif QMessageBox.question(self,"Remove from System Knowledge",message)!=QMessageBox.Yes:return
        retained=self.selected_key if self.selected_key and self.selected_key[0]=="category" else None
        sensitivity_before=self._sensitivity_snapshot(tag_ids)
        try:self.session.remove_tags(tag_ids)
        except ValueError as error:QMessageBox.information(self,"Tags not removed",str(error)); return
        self._record_sensitivity_impact(sensitivity_before)
        self.selected_key=retained; self.refresh_all()
        if retained:self._show_details(retained,refresh_examples=False)
        self._persist_automatic_change(
            f"Saved removal of {len(records):,} SDXL Tag{'s' if len(records)!=1 else ''} from System Knowledge.",
            tag_ids=tag_ids,kind="tag_removal",
            completed_image_changes=len(changed) if remove_everywhere else None,
        )

    def has_active_tag_removal(self):
        return bool(self._image_tag_removal_dialogs)

    def request_tag_removal_shutdown(self):
        for dialog in tuple(self._image_tag_removal_dialogs):
            dialog.reject()
        return self.has_active_tag_removal()

    def _remove_selected_membership_tags(self):
        self._remove_membership_tags(self.membership_chips.selected_values())

    def _always_ignore_membership_tags(self,values):
        if self.session.has_changes:
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Open Rules & Automation after it finishes."); return
        records=self._membership_records(values)
        if records:self.always_ignore_tags_requested.emit(tuple(record.id for record in records))

    def _always_ignore_selected_membership_tags(self):
        self._always_ignore_membership_tags(self.membership_chips.selected_values())

    def report_always_ignore_result(self,text,*,error=False):
        self._set_status(text,announce=True)

    def _arrange_membership_tag(self,value):
        record=self._staged_tags().find(value)
        self.open_caption_arrangement(("tag",record.id) if record is not None else None)

    def _arrange_selected_caption_order(self):
        records=self._membership_records(); key=("tag",records[0].id) if len(records)==1 else self.selected_key
        self.open_caption_arrangement(key)

    def open_caption_arrangement(self,key=None):
        self._flush_details(); dialog=QDialog(self); dialog.setWindowTitle("Arrange Caption Order"); dialog.resize(1120,720)
        layout=QVBoxLayout(dialog); explanation=QLabel("Temporarily arrange categories or SDXL Tags within their output group. Closing this window saves completed ordering changes automatically as one recoverable System Knowledge transaction.")
        explanation.setWordWrap(True); explanation.setObjectName("mutedLabel"); layout.addWidget(explanation)
        self._sequence_ready=True; self.sequence.setParent(dialog); layout.addWidget(self.sequence,1); self.sequence.show(); self.sequence.intro.hide(); self.sequence.search.show()
        for button in (*self.sequence.move_buttons.values(),self.sequence.inherit):button.show()
        self.sequence.refresh(); self.sequence.select_key(key or self.selected_key)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        dialog.exec()
        self.sequence.hide(); self.sequence.setParent(self.sequence_holder); self.sequence.search.hide()
        for button in (*self.sequence.move_buttons.values(),self.sequence.inherit):button.hide()
        self.refresh_structure()
        if self.selected_key:self._show_details(self.selected_key,refresh_examples=False)
        self._refresh_actions()
        if self.session.has_changes:
            self._persist_automatic_change("Saved caption-order changes.",kind="caption_order")

    def _configure_reusable_value_control(self,record):
        explicit_family=record.extras.get("reusable_modifier_family") is True
        built_in_family=self.session.is_reusable_modifier_family(record.id) and not explicit_family
        blocker=QSignalBlocker(self.reusable_modifier_family)
        self.reusable_modifier_family.setChecked(explicit_family or built_in_family)
        self.reusable_modifier_family.setEnabled(not built_in_family)
        if built_in_family:
            self.reusable_modifier_family.setText(
                "Built-in reusable value collection — always enabled"
            )
            explanation=(
                "This protected system category already supplies a reusable value collection. "
                "Its role is always enabled so saved compatibility policies remain deterministic."
            )
        else:
            self.reusable_modifier_family.setText(
                "Use this category as a reusable value collection"
            )
            explanation=(
                "Treat reviewed SDXL Tags in this category and its child categories as one reusable value collection. "
                "This supplies available values but does not make the collection compatible with any Base Tag."
            )
        self.reusable_modifier_family.setAccessibleDescription(explanation)
        self.reusable_modifier_family.setToolTip(explanation)
        self.reusable_modifier_family.setVisible(not built_in_family)
        self.reusable_modifier_locked.setVisible(built_in_family)
        self.reusable_modifier_locked.setText("✓ Built-in reusable value collection — always enabled")
        self.reusable_modifier_locked.setAccessibleName("Built-in reusable value collection, always enabled")
        self.reusable_modifier_locked.setToolTip(explanation)
        self._update_reusable_value_summary(record)
        del blocker

    def _update_reusable_value_summary(self,record=None):
        if record is None and self.selected_key and self.selected_key[0]=="category":record=self.session.get(self.selected_key[1])
        if record is None:
            self.reusable_value_summary.setText("No category selected."); return
        total=self.session.total_tag_count(record.id); descendants=len(self.session.descendant_ids(record.id))
        scope=(
            f"{total:,} reviewed SDXL Tag{'s' if total!=1 else ''} from this category"
            + (f" and {descendants:,} child categor{'y' if descendants==1 else 'ies'}" if descendants else "")
        )
        enabled=self.reusable_modifier_family.isChecked()
        self.reusable_value_summary.setText(
            ("Collection available: " if enabled else "If enabled, the collection will include: ")
            +scope+". Compatibility is reviewed separately on Base Tags."
        )

    def _reusable_value_toggled(self,*_):
        if self._loading_details:return
        self._update_reusable_value_summary(); self._update_category_policy_controls()
        self._set_context_status("Reusable collection choice pending. Saving automatically after this edit.")
        self._queue_details()

    def _queue_details(self):
        if not self._loading_details and self.selected_key and self.selected_key[0]=="category":self._details_timer.start()

    def _category_name_edited(self,*_):
        if self._loading_details:return
        self._category_name_dirty=True
        # A category name is a complete value, not a live-search field. Do not
        # let the shared details debounce save a partially typed name.
        self._details_timer.stop()

    def _flush_details(self):
        pending=self._details_timer.isActive() or self._category_name_dirty
        if self._details_timer.isActive():self._details_timer.stop()
        if pending:self._stage_details()

    def _stage_details(self):
        if self._loading_details or not self.selected_key or self.selected_key[0]!="category":return
        category_id=self.selected_key[1]; color_text=self.category_color.text().strip(); color=normalize_category_color(color_text)
        if color_text and not color:
            self._category_edit_validation_pending=True
            self._set_context_status("Enter a six-digit color such as #3A78B8, or clear Own color."); return
        parent_id=str(self.category_parent.currentData() or "") or None; record=self.session.get(category_id)
        if self.base_tag_policy.currentData()=="own" and not self.category_base_tag.values():
            self._category_edit_validation_pending=True
            if self._category_edit_dialog is not None:self.category_advanced_toggle.setChecked(True)
            self._set_context_status("Choose a Base SDXL Tag, or use inherited/no base-tag behavior."); return
        selected_sensitivity=(str(self.category_sensitivity.currentData() or "") if self.sensitivity_policy.currentData()=="own" else record.sensitivity_rating if record is not None else "")
        parent_changed=bool(record is not None and record.parent_id!=parent_id)
        if parent_changed and not self._confirm_reparent(record,parent_id):
            self._loading_details=True; self.category_parent.setCurrentIndex(max(0,self.category_parent.findData(record.parent_id or ""))); self._loading_details=False; return
        had_staged_changes=self.session.has_changes
        direct_base_id,direct_base_policy=self._direct_category_base_values(category_id)
        direct_base_scope=self._direct_category_base_scope(category_id)
        direct_base_scope_reviewed=self._direct_category_base_scope_reviewed(category_id)
        before=(record.name,record.parent_id,record.description,record.color,record.color_policy,direct_base_id,direct_base_policy,direct_base_scope,direct_base_scope_reviewed,record.sensitivity_rating,record.sensitivity_policy,record.extras.get("reusable_modifier_family") is True) if record is not None else None
        dependency_may_change=bool(
            before is not None and (
                parent_changed
                or before[9]!=selected_sensitivity
                or before[10]!=str(self.sensitivity_policy.currentData() or "none")
            )
        )
        sensitivity_before=(
            self._sensitivity_snapshot(self._category_sensitivity_tag_ids(category_id))
            if dependency_may_change else {}
        )
        try:
            updated=self.session.update_category(category_id,name=self.category_name.text(),parent_id=parent_id,description=self.category_description.toPlainText(),color=color,color_policy=str(self.color_policy.currentData() or "none"),base_tag_id=next(iter(self.category_base_tag.values()),""),base_tag_policy=str(self.base_tag_policy.currentData() or "none"),base_member_scope=str(self.category_base_scope.currentData() or CATEGORY_BASE_MEMBER_SCOPE_DIRECT),sensitivity_rating=selected_sensitivity,sensitivity_policy=str(self.sensitivity_policy.currentData() or "none"),reusable_modifier_family=(self.reusable_modifier_family.isChecked() if self.reusable_modifier_family.isEnabled() else None))
        except (KeyError,ValueError) as error:
            self._category_edit_validation_pending=True
            self._set_context_status(str(error)); return
        was_invalid=self._category_edit_validation_pending
        self._category_edit_validation_pending=False
        updated_base_id,updated_base_policy=self._direct_category_base_values(updated.id)
        updated_base_scope=self._direct_category_base_scope(updated.id)
        updated_base_scope_reviewed=self._direct_category_base_scope_reviewed(updated.id)
        after=(updated.name,updated.parent_id,updated.description,updated.color,updated.color_policy,updated_base_id,updated_base_policy,updated_base_scope,updated_base_scope_reviewed,updated.sensitivity_rating,updated.sensitivity_policy,updated.extras.get("reusable_modifier_family") is True)
        if before==after:
            self._category_name_dirty=False
            if was_invalid and self._category_edit_dialog is not None:
                self._set_context_status("Correction is valid. No Category change to save.")
            return
        if before is not None and before[:2]!=after[:2]:
            self._refreshing_views=True
            try:
                self.refresh_structure()
                if self._sequence_ready:self.sequence.refresh()
            finally:self._refreshing_views=False
        else:self._refresh_structure_category_item(category_id)
        self._refresh_staged_category_inspector(updated)
        self._category_name_dirty=False
        sensitivity_changed=bool(before is not None and before[9:11]!=after[9:11])
        if dependency_may_change:self._record_sensitivity_impact(sensitivity_before)
        self._persist_automatic_change(
            f"Saved {'Parent category and category details' if parent_changed else 'category details'} for {updated.name}.",
            category_ids=(updated.id,),
            kind="category_structure" if parent_changed or had_staged_changes else "category_sensitivity" if sensitivity_changed else "category_details",
        )

    def _refresh_staged_category_inspector(self,record):
        self._loading_details=True
        try:
            self.detail_title.setText(record.name); self.category_name.setText(record.name); self.category_description.setPlainText(record.description); self.category_color.setText(record.color)
            self._configure_reusable_value_control(record)
            self.category_parent.setCurrentIndex(max(0,self.category_parent.findData(record.parent_id or ""))); self.category_parent.setToolTip(self.session.path_label(record.parent_id) if record.parent_id else "Top level")
            self.color_policy.setCurrentIndex(max(0,self.color_policy.findData(record.color_policy))); self._update_color_controls()
            direct_base_id,direct_base_policy=self._direct_category_base_values(record.id)
            self.base_tag_policy.setCurrentIndex(max(0,self.base_tag_policy.findData(direct_base_policy))); self.category_base_tag.set_values((direct_base_id,) if direct_base_id else ())
            self._set_category_base_scope(record.id)
            self.sensitivity_policy.setCurrentIndex(max(0,self.sensitivity_policy.findData(record.sensitivity_policy))); self.category_sensitivity.setCurrentIndex(max(0,self.category_sensitivity.findData(record.sensitivity_rating or "general"))); self._update_category_policy_controls()
            group,_,source=self.session._category_effective(record.id); effective_color,color_source,_,_=self.session.effective_color(record.id)
            direct,total=self._detail_counts if self._detail_category_id==record.id else (self.session.direct_tag_count(record.id),self.session.total_tag_count(record.id))
            self._detail_category_id=record.id; self._detail_counts=(direct,total)
            color_text=(f"Effective color {effective_color} from {self.session.path_label(color_source)}" if effective_color and color_source else "No effective category color")
            order_text=(f"{ORDER_GROUP_LABELS[group]} caption group set here" if source==record.id else f"{ORDER_GROUP_LABELS[group]} caption group inherited from {self.session.path_label(source)}")
            self.detail_summary.setText(
                f"{counted_noun(direct,'direct Tag')} • "
                f"{counted_noun(total,'Tag')} including subcategories • "
                f"{record.source.title()}"
            )
            self.detail_summary.setToolTip(
                f"{order_text} • {color_text} • {self._category_semantic_summary(record.id)}"
            )
            self.membership_chips.refresh_category_details(self.session.path_label(record.id),effective_color,record.description,record.id)
        finally:self._loading_details=False

    def _refresh_structure_category_item(self,category_id):
        pending=[self.structure_tree.topLevelItem(index) for index in range(self.structure_tree.topLevelItemCount())]
        while pending:
            item=pending.pop(); key=self._key(item)
            if key==("category",category_id):
                record=self.session.get(category_id)
                if record is None:return
                direct=self.session.direct_tag_count(category_id); total=self.session.total_tag_count(category_id); source="Override" if record.source=="user" else "Default"
                item.setText(0,record.name); item.setText(1,f"{direct:,} / {total:,}"); item.setText(2,source)
                item.setToolTip(0,self.session.path_label(category_id)); item.setData(0,Qt.AccessibleTextRole,f"Category {record.name}; {direct} direct and {total} total SDXL Tags; {source}")
                return
            pending.extend(item.child(index) for index in range(item.childCount()))

    def _choose_color(self):
        selected=QColorDialog.getColor(QColor(normalize_category_color(self.category_color.text()) or "#3A78B8"),self,"Choose Category Color")
        if selected.isValid():
            self.category_color.setText(selected.name().upper()); self.color_policy.setCurrentIndex(self.color_policy.findData("own")); self._update_color_controls(); self._stage_details()

    def _color_policy_changed(self,*_):
        self._update_color_controls(); self._stage_details()

    def _category_base_tag_changed(self,*_):
        if self._loading_details:return
        category_id=self.selected_key[1] if self.selected_key and self.selected_key[0]=="category" else ""
        direct=self.session.direct_category_base(category_id)
        if self.category_base_tag.values() and (direct is None or direct.policy!="own"):
            self.category_base_scope.setCurrentIndex(
                self.category_base_scope.findData(CATEGORY_BASE_MEMBER_SCOPE_DIRECT)
            )
        policy="own" if self.category_base_tag.values() else "inherit" if self.category_parent.currentData() else "none"
        self.base_tag_policy.setCurrentIndex(self.base_tag_policy.findData(policy))
        self._update_category_policy_controls(); self._stage_details()

    def _category_policy_changed(self,*_):
        if self.sender() is self.base_tag_policy and self.base_tag_policy.currentData()=="own":
            category_id=self.selected_key[1] if self.selected_key and self.selected_key[0]=="category" else ""
            direct=self.session.direct_category_base(category_id)
            if direct is None or direct.policy!="own":
                self.category_base_scope.setCurrentIndex(
                    self.category_base_scope.findData(CATEGORY_BASE_MEMBER_SCOPE_DIRECT)
                )
        self._update_category_policy_controls()
        if self.sender() is self.base_tag_policy and self.base_tag_policy.currentData()=="own" and not self.category_base_tag.values():
            self._set_context_status("Choose one Base SDXL Tag to finish this setting; nothing has changed yet.")
            QTimer.singleShot(0,self._choose_category_base_for_policy)
            return
        self._stage_details()

    def _choose_category_base_for_policy(self):
        self.category_base_tag._choose()
        if self.category_base_tag.values() or self.base_tag_policy.currentData()!="own":return
        record=self.session.get(self.selected_key[1]) if self.selected_key and self.selected_key[0]=="category" else None
        if record is None:return
        self._loading_details=True
        _,direct_base_policy=self._direct_category_base_values(record.id)
        try:self.base_tag_policy.setCurrentIndex(max(0,self.base_tag_policy.findData(direct_base_policy)))
        finally:self._loading_details=False
        self._update_category_policy_controls()
        self._set_context_status("Base meaning unchanged; no Base SDXL Tag was selected.")

    def _update_category_policy_controls(self):
        own_base=self.base_tag_policy.currentData()=="own"; own_sensitivity=self.sensitivity_policy.currentData()=="own"
        self.category_base_tag.setEnabled(True); self.category_sensitivity.setEnabled(own_sensitivity)
        self.category_base_tag.setVisible(own_base); self.category_base_tag_label.setVisible(own_base)
        self.category_base_scope.setVisible(own_base); self.category_base_scope_label.setVisible(own_base)
        self.category_sensitivity.setVisible(own_sensitivity); self.category_sensitivity_label.setVisible(own_sensitivity)
        self.category_base_tag.setToolTip("Choose or drop one existing SDXL Tag. Selecting it completes this category's explicit Base meaning.")
        self.category_base_scope.setToolTip(
            "Direct members is the safe default. Descendant coverage must be an explicit reviewed choice."
        )
        self.category_sensitivity.setToolTip("This reviewed category rating applies to members that do not have a tag-specific rating." if own_sensitivity else "Select Use selected sensitivity to set a category-specific rating.")
        category_id=self.selected_key[1] if self.selected_key and self.selected_key[0]=="category" else ""
        base_text=self._category_base_summary(category_id) if category_id else "No category selected"
        sensitivity_text=self._category_sensitivity_summary(category_id) if category_id else "No category selected"
        if own_base and not self.category_base_tag.values():base_text="Choose one Base SDXL Tag to finish this setting. Nothing has changed yet."
        self.base_policy_status.setText(base_text); self.sensitivity_policy_status.setText(sensitivity_text)
        self.category_policy_status.setText(f"{base_text} • {sensitivity_text}")
        if category_id:
            reusable_enabled=self.reusable_modifier_family.isChecked()
            reusable_text=(
                "Built-in reusable collection: On; Base Tag compatibility is chosen separately."
                if reusable_enabled and not self.reusable_modifier_family.isEnabled() else
                "Reusable collection: On; Base Tag compatibility is chosen separately."
                if reusable_enabled else "Reusable collection: Off."
            )
            summary=f"{base_text}\n{sensitivity_text}\n{reusable_text}"
        else:summary="No category selected."
        self.category_effective_summary.setText(summary)
        self.category_effective_summary.setAccessibleDescription(summary)

    def _category_base_summary(self,category_id):
        base_id,base_source,_=self.session.effective_base_tag(category_id); base=self.session._state.tags.get(base_id)
        if not base or not base_source:return "No effective Base SDXL Tag"
        direct=self.session.direct_category_base(base_source)
        scope_text=(
            "direct and descendant members"
            if direct is not None and direct.member_scope==CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS
            else "direct members only"
        )
        review_text=(
            " (legacy scope preserved; choose a scope to review it)"
            if direct is not None and not direct.scope_reviewed
            else ""
        )
        return f"Base SDXL Tag: {base.display} — from {self.session.path_label(base_source)}; {scope_text}{review_text}"

    def _direct_category_base_values(self,category_id):
        direct=self.session.direct_category_base(category_id)
        if direct is None or direct.ambiguous:return "","none"
        return (
            direct.base_tag_id if direct.policy=="own" else "",
            direct.policy,
        )

    def _direct_category_base_scope(self,category_id):
        direct=self.session.direct_category_base(category_id)
        return (
            direct.member_scope
            if direct is not None and not direct.ambiguous
            else CATEGORY_BASE_MEMBER_SCOPE_DIRECT
        )

    def _direct_category_base_scope_reviewed(self,category_id):
        direct=self.session.direct_category_base(category_id)
        return bool(
            direct is None or direct.ambiguous or direct.scope_reviewed
        )

    def _set_category_base_scope(self,category_id):
        scope=self._direct_category_base_scope(category_id)
        self.category_base_scope.setCurrentIndex(
            max(0,self.category_base_scope.findData(scope))
        )

    def _category_sensitivity_summary(self,category_id):
        rating,rating_source,_=self.session.effective_sensitivity(category_id)
        return f"Rating: {rating.title()} — from {self.session.path_label(rating_source)}" if rating and rating_source else "No effective content-sensitivity classification"

    def _category_semantic_summary(self,category_id):
        return f"{self._category_base_summary(category_id)} • {self._category_sensitivity_summary(category_id)}"

    def _update_color_controls(self):
        own=self.color_policy.currentData()=="own"
        self.category_color.setEnabled(own); self.choose_color.setEnabled(True)
        self.category_color_row.setVisible(own); self.category_color_label.setVisible(own)
        self.category_color.setToolTip("Enter this category's own color." if own else "Choose a color to switch automatically to Own color; inherited policies resolve dynamically until then.")
        self.choose_color.setToolTip("Choose a color. A successful choice automatically switches the policy to Own color.")
        category_id=self.selected_key[1] if self.selected_key and self.selected_key[0]=="category" else ""
        if category_id:
            effective_color,color_source,_,_=self.session.effective_color(category_id)
            self.appearance_policy_status.setText(
                f"Color: {effective_color} — from {self.session.path_label(color_source)}"
                if effective_color and color_source else "No effective category color"
            )
        else:self.appearance_policy_status.setText("No category selected")
        self._update_category_color_preview()

    def _update_category_color_preview(self,*_):
        value=self.category_color.text().strip(); color=normalize_category_color(value); own=self.color_policy.currentData()=="own"
        if color:
            self.category_color_swatch.setStyleSheet(f"background:{color}; border:1px solid palette(mid); border-radius:4px;")
            state="selected" if own else "stored but inactive"
            text=f"Category own color {color}, {state}"
        else:
            self.category_color_swatch.setStyleSheet("background:transparent; border:1px dashed palette(mid); border-radius:4px;")
            text="Invalid category own color code" if value else "No category own color selected"
        self.category_color_swatch.setAccessibleName(text); self.category_color_swatch.setToolTip(text)

    def _confirm_reparent(self,record,parent_id):
        old=self.session.path_label(record.parent_id) if record.parent_id else "Top level"
        new=self.session.path_label(parent_id) if parent_id else "Top level"
        descendants=len(self.session.descendant_ids(record.id)); tags=self.session.total_tag_count(record.id)
        message=f"Change the Parent category for {record.name} from {old} to {new}?\n\n{descendants:,} descendant categor{'y' if descendants==1 else 'ies'} and {tags:,} SDXL Tag(s) remain attached. Inherited Base meaning, sensitivity, caption order, and color will be recalculated. Confirming saves this System Knowledge change automatically."
        return QMessageBox.question(self,"Preview Parent Category Change",message)==QMessageBox.Yes

    def _semantic_drop(self,source,target):
        source_key=self._key(source); target_key=self._key(target)
        if not source_key or not target_key:return
        if target_key[0]=="tag":
            target_record=self.session._state.tags.get(target_key[1]); target_key=("category",target_record.category) if target_record else None
        if not target_key or target_key[0]!="category":return
        target_id=target_key[1]
        try:
            if source_key[0]=="tag":
                self.apply_tag_category_change((source_key[1],),target_id,selected_key=source_key); return
            elif source_key[0]=="category":
                record=self.session.get(source_key[1])
                if record is None or record.id==target_id:return
                if not self._confirm_reparent(record,target_id):return
                sensitivity_before=self._sensitivity_snapshot(
                    self._category_sensitivity_tag_ids(record.id)
                )
                self.session.update_category(record.id,name=record.name,parent_id=target_id,description=record.description,color=record.color,color_policy=record.color_policy); text=f"Saved Parent category change for {record.name}."
                self._record_sensitivity_impact(sensitivity_before)
            else:return
        except ValueError as error:QMessageBox.information(self,"Move not available",str(error)); return
        self.selected_key=source_key; self.refresh_all(); self._show_details(source_key)
        self._persist_automatic_change(text,category_ids=(source_key[1],),kind="category_structure")

    def _semantic_tag_drop(self,payload,target):
        view=self._staged_tags()
        if hasattr(payload,"hasFormat"):tag_ids=decode_tag_drag_ids(payload,view)
        else:
            tag_ids=[]
            for value in ((payload,) if isinstance(payload,str) else payload or ()):
                record=view.tags.get(str(value)) or view.find(value)
                if record is not None and record.id not in tag_ids:tag_ids.append(record.id)
            tag_ids=tuple(tag_ids)
        target_key=self._key(target)
        if target_key and target_key[0]=="tag":
            target_record=self.session._state.tags.get(target_key[1]); target_key=("category",target_record.category) if target_record else None
        if not tag_ids or not target_key or target_key[0]!="category":return
        self.apply_tag_category_change(tag_ids,target_key[1])

    def _routine_action_tag_ids(self):
        action=self.session._state.actions[-1] if self.session._state.actions else {}
        if action.get("kind")!="tag" or action.get("action")!="change_category":return ()
        values=action.get("ids") or ((action.get("id"),) if action.get("id") else ())
        return tuple(dict.fromkeys(str(value) for value in values if str(value)))

    def _routine_action_sensitivity_tag_ids(self):
        action=self.session._state.actions[-1] if self.session._state.actions else {}
        return tuple(dict.fromkeys(
            str(value) for value in action.get("sensitivity_tag_ids",())
            if str(value)
        ))

    def _routine_action_relationship_ids(self):
        action=self.session._state.actions[-1] if self.session._state.actions else {}
        return tuple(dict.fromkeys(
            str(value) for value in action.get("relationship_ids",())
            if str(value)
        ))

    def _sensitivity_snapshot(self,tag_ids):
        result={}
        for stable_id in dict.fromkeys(str(value) for value in tag_ids if str(value)):
            record=self.session._state.tags.get(stable_id)
            if record is None:
                result[stable_id]=None; continue
            sensitivity=record.content_sensitivity
            if (
                sensitivity.classification_status=="reviewed"
                and sensitivity.minimum_rating in CONTENT_RATINGS
            ):
                result[stable_id]=sensitivity.minimum_rating
            elif sensitivity.classification_status=="intentionally_unclassified":
                result[stable_id]=""
            else:
                result[stable_id]=self.session.effective_sensitivity(record.category)[0]
        return result

    def _category_sensitivity_tag_ids(self,*category_ids):
        selected=set()
        for category_id in category_ids:
            value=str(category_id or "")
            if not value:continue
            selected.add(value); selected.update(self.session.descendant_ids(value))
        return tuple(
            record.id for record in self.session.all_tags()
            if record.category in selected
        )

    def _record_sensitivity_impact(self,before):
        after=self._sensitivity_snapshot(before)
        affected=tuple(
            stable_id for stable_id,value in before.items()
            if after.get(stable_id)!=value
        )
        if self.session._state.actions:
            self.session._state.actions[-1]["sensitivity_tag_ids"]=affected
        return affected

    def _routine_action_change(self):
        action=self.session._state.actions[-1] if self.session._state.actions else {}
        if action.get("kind")=="tag" and action.get("action")=="reset_broader_tag_overrides":
            return (
                "tag_relationships",
                tuple(str(value) for value in action.get("ids",()) if str(value)),
                tuple(str(value) for value in action.get("category_ids",()) if str(value)),
            )
        if action.get("kind")=="tag" and action.get("action")=="change_category":
            return "tag_membership",self._routine_action_tag_ids(),()
        if action.get("kind")=="tag" and action.get("action")=="remove_from_system_knowledge":
            return "tag_removal",tuple(str(value) for value in action.get("ids",()) if str(value)),()
        if action.get("kind")=="category" and action.get("action")=="update" and action.get("id"):
            kind=(
                "category_structure"
                if action.get("old_parent_id")!=action.get("parent_id") else
                "category_sensitivity"
                if action.get("sensitivity_changed") else
                "category_details"
            )
            tag_ids=tuple(
                str(value) for value in action.get("dependency_tag_ids",())
                if str(value)
            )
            return kind,tag_ids,(str(action["id"]),)
        if action.get("kind")=="category" and action.get("action") in {"create","merge","delete","restore_default"}:
            values=(action.get("id"),action.get("keep_id"),*action.get("remove_ids",()),*action.get("child_categories",()))
            tag_ids=tuple(str(value) for value in action.get("tag_ids",()) if str(value))
            category_ids=tuple(dict.fromkeys(str(value) for value in values if value))
            return "category_structure",tag_ids,category_ids
        if action.get("kind") in {"category","tag"}:
            record_id=action.get("id")
            return "caption_order",((str(record_id),) if action.get("kind")=="tag" and record_id else ()),((str(record_id),) if action.get("kind")=="category" and record_id else ())
        return "routine_change",(),()

    def _set_automatic_save_busy(self,busy):
        if self._category_edit_buttons is not None:self._category_edit_buttons.setEnabled(not busy)
        if self._category_edit_dialog is not None:self.category_fields.setEnabled(not busy)
        if busy:
            window=self.window(); self._automatic_save_window=window; self._automatic_save_focus=window.focusWidget() if window is not None else None
            if window is not None:window.setEnabled(False)
            return
        window=self._automatic_save_window; focus=self._automatic_save_focus; self._automatic_save_window=None; self._automatic_save_focus=None
        if window is not None:
            try:window.setEnabled(True)
            except RuntimeError:pass
        if focus is not None:
            try:focus.setFocus(Qt.OtherFocusReason)
            except RuntimeError:pass

    def _refresh_after_routine_save(self,kind="routine_change",category_ids=()):
        if kind=="category_details":
            for category_id in category_ids:self._refresh_structure_category_item(category_id)
            if self.selected_key and self.selected_key[0]=="category":
                record=self.session.get(self.selected_key[1])
                if record is not None:self._refresh_staged_category_inspector(record)
        else:
            self.refresh_structure()
            if self.selected_key:self._show_details(self.selected_key)
        self._refresh_actions(); self._menus_changed()

    def _automatic_save_complete(self,result):
        task=self._automatic_save_task
        if task is None:return
        completion_callback=getattr(task,"completion_callback",None)
        tag_ids=tuple(getattr(task,"tag_ids",())); category_ids=tuple(getattr(task,"category_ids",())); kind=str(getattr(task,"change_kind","routine_change")); success_text=str(getattr(task,"success_text","Saved Category Workspace change."))
        labels={
            "caption_order":"Saved caption-order change",
            "tag_membership":"Saved SDXL Tag category change",
            "category_structure":"Saved Category structure change",
            "tag_removal":"Saved SDXL Tag removal",
            "tag_relationships":"Saved SDXL Tag relationship change",
            "category_details":"Saved Category details change",
            "category_sensitivity":"Saved Category sensitivity change",
        }
        classification=classify_system_knowledge_change(
            kind,label=labels.get(kind,"Saved System Knowledge change"),
            affected_tag_ids=tag_ids,changed_category_ids=category_ids,
            impact=result.impact,
        )
        self._automatic_save_task=None; self._automatic_save_outcome=True; self._set_automatic_save_busy(False)
        if kind in {"tag_membership","tag_removal","tag_relationships","category_details","category_sensitivity","category_structure","caption_order"}:
            self.routine_change_saved.emit({
                "kind":kind,"tag_ids":tag_ids,"category_ids":category_ids,
                "sensitivity_tag_ids":tuple(getattr(task,"sensitivity_tag_ids",())),
                "identity_groups":tuple(getattr(task,"identity_groups",())),
                "relationship_ids":tuple(getattr(task,"relationship_ids",())),
                "impact":result.impact,"classification":classification,
            })
        else:
            self.workspace_saved.emit()
        self._refresh_after_routine_save(kind,category_ids)
        image_changes=getattr(task,"completed_image_changes",None)
        if image_changes is not None:
            local_text=(
                success_text+f" Saved image corrections: {image_changes:,}. "
                "Undo and Redo affect reusable Tags only, not image facts."
            )
            self._set_status(
                local_text+" Captions, Metadata, original images and raw evidence stay unchanged.",
                announce=True,local_text=local_text,
            )
        else:
            self._set_status(
                success_text+" Undo and Redo also save automatically. Image data was "
                "not rewritten. "+classification.completion_summary(),announce=True,
                local_text=success_text+" Undo and Redo are available; image data was not rewritten.",
            )
        if completion_callback is not None:completion_callback(result)

    def _automatic_save_failed(self,message,*,operation="Change"):
        task=self._automatic_save_task
        if task is None:return
        image_changes=getattr(task,"completed_image_changes",None)
        saved_image_notice=(
            f"Image corrections to {image_changes:,} images remain saved. "
            if image_changes is not None else ""
        )
        affected_tag_ids=tuple(getattr(task,"tag_ids",()))
        failure_callback=getattr(task,"failure_callback",None)
        operation=str(getattr(task,"failure_operation",operation) or operation)
        self._automatic_save_task=None; self._automatic_save_outcome=False; self._set_automatic_save_busy(False)
        try:
            self.categories.load(); self.tags.load(); self.patterns.load(); self.tags.apply_category_ordering(self.categories)
            self.session=self._session_factory(self.tags,self.categories,self.patterns,self.characters); self.sequence.session=self.session
        except Exception as reload_error:
            self.session.reset(); self.refresh_all()
            failure_message=(
                saved_image_notice+"The reusable Tag save reported an error and its outcome is unverified. "
                f"Resolve the storage problem and restart CollectiGen before editing Knowledge again: {reload_error}"
                if image_changes is not None else
                f"{operation} was not saved, and the live System Knowledge could not be reloaded. The pending change was cancelled. Resolve the storage problem and restart CollectiGen before editing Knowledge again: {reload_error}"
            )
            self._set_status(failure_message,announce=True)
            if failure_callback is not None:failure_callback(failure_message)
            return
        if self.selected_key:
            kind,record_id=self.selected_key
            if kind=="category" and self.session.get(record_id) is None:self.selected_key=None
            elif kind=="tag" and record_id not in self.session._state.tags:self.selected_key=None
        self.refresh_all()
        if self.selected_key:self._show_details(self.selected_key)
        if operation=="Duplicate merge":
            failure_message=(
                "Duplicate merge was not saved. The restored live System Knowledge "
                "was reloaded, so both selected identities correctly remain unchanged. "
                f"Review the reported System Knowledge issue before trying again: {message}"
            )
        else:
            failure_message=f"{operation} was not saved. The restored live System Knowledge was reloaded, so there is no stale change left to retry. Repeat the action after resolving the storage problem: {message}"
        if image_changes is not None:
            remaining=sum(stable_id in self.tags.tags for stable_id in affected_tag_ids)
            tag_outcome=(
                "Reusable Tags remain available in reloaded Knowledge. "
                if remaining==len(affected_tag_ids) else
                "Reusable Tags are absent from reloaded Knowledge. Review the saved result before repeating removal. "
                if remaining==0 else
                "The reusable Tag selection has mixed saved outcomes. Review Knowledge before repeating removal. "
            )
            failure_message=(saved_image_notice+tag_outcome+
                f"The reusable Tag save reported an error. Resolve the storage problem before making more changes: {message}")
        self._set_status(failure_message,announce=True)
        if failure_callback is not None:failure_callback(failure_message)

    def _persist_automatic_change(
        self,success_text,tag_ids=(),category_ids=(),kind="",*,
        sensitivity_tag_ids=None,relationship_ids=None,
        completion_callback=None,failure_callback=None,
        failure_operation="Change",
        completed_image_changes=None,
    ):
        if self._automatic_save_task is not None:
            self._set_status("A Category Workspace change is already saving. Wait for it to finish before making another change.",announce=True); return False
        inferred_kind,inferred_tags,inferred_categories=self._routine_action_change()
        tag_ids=tuple(dict.fromkeys(str(value) for value in (tag_ids or inferred_tags) if str(value)))
        category_ids=tuple(dict.fromkeys(str(value) for value in (category_ids or inferred_categories) if str(value)))
        kind=kind or inferred_kind
        sensitivity_tag_ids=tuple(dict.fromkeys(
            str(value) for value in (
                self._routine_action_sensitivity_tag_ids()
                if sensitivity_tag_ids is None else sensitivity_tag_ids
            ) if str(value)
        ))
        identity_groups=()
        relationship_ids=tuple(dict.fromkeys(
            str(value) for value in (
                self._routine_action_relationship_ids()
                if relationship_ids is None else relationship_ids
            ) if str(value)
        ))
        if kind=="tag_removal":
            records=(
                self.session._state.tags.get(stable_id)
                or self.session._initial.tags.get(stable_id)
                for stable_id in tag_ids
            )
            identity_groups=tuple(
                self.tags.identity_names(record) for record in records
                if record is not None
            )
        organization_depends=self._organization_depends()
        self._automatic_save_outcome=None; self._set_status("Saving changes…")
        save_session=self.session
        def save_change():
            affected_paths=None
            if kind in {"caption_order","tag_membership","category_structure"}:
                impact=save_session.impact(
                    organization_depends_on_order=organization_depends,
                )
            else:
                impact=None
            if impact is not None and (
                impact.captions_may_be_stale and impact.affected_tag_ids
            ):
                try:
                    index=(
                        self.image_index_provider()
                        if callable(self.image_index_provider) else None
                    )
                    if index is not None:
                        affected_paths=affected_order_projection_paths(
                            impact,save_session._state.tags,index.all_records(),
                            aliases_for=self.tags.effective_aliases,
                        )
                except Exception:
                    affected_paths=None
            return save_session.save(
                organization_depends_on_order=organization_depends,
                affected_image_paths=affected_paths,preserve_history=True,
                category_details_only=kind=="category_details",
            )
        task=AsyncCallTask(save_change); task.save_session=save_session; task.tag_ids=tag_ids; task.category_ids=category_ids; task.sensitivity_tag_ids=sensitivity_tag_ids; task.identity_groups=identity_groups; task.relationship_ids=relationship_ids; task.change_kind=kind; task.success_text=success_text; task.completion_callback=completion_callback; task.failure_callback=failure_callback; task.failure_operation=failure_operation
        task.completed_image_changes=completed_image_changes
        self._automatic_save_task=task; task.signals.complete.connect(self._automatic_save_complete); task.signals.failed.connect(self._automatic_save_failed)
        self._set_automatic_save_busy(True); self._refresh_actions(reuse_health=kind=="category_details")
        from PySide6.QtCore import QThreadPool
        (self.pool or QThreadPool.globalInstance()).start(task)
        return self._automatic_save_outcome is not False

    def apply_tag_category_change(self,tag_ids,target_id,*,selected_key=None):
        tag_ids=tuple(dict.fromkeys(str(value) for value in tag_ids))
        if self.has_unsaved_changes():
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Move the SDXL Tags after it finishes."); return False
        records=[self.session._state.tags.get(value) for value in tag_ids]
        if not tag_ids or any(record is None for record in records):
            QMessageBox.warning(self,"Selection changed","One or more selected SDXL Tags are no longer available. Select them again and retry."); return False
        changed=[record for record in records if record.category!=target_id]
        if not changed:
            self._set_status(f"All {len(records):,} selected SDXL Tags already belong to {self.session.path_label(target_id)}.",announce=True); return True
        retained_context=selected_key or self.selected_key
        if retained_context and retained_context[0]=="tag":
            selected_record=self.session._state.tags.get(retained_context[1]); retained_context=("category",selected_record.category) if selected_record is not None else None
        if not retained_context and changed:retained_context=("category",changed[0].category)
        sensitivity_before=self._sensitivity_snapshot(record.id for record in changed)
        try:self.session.change_tag_categories(tag_ids,target_id)
        except (KeyError,ValueError) as error:QMessageBox.information(self,"Move not available",str(error)); return False
        self._record_sensitivity_impact(sensitivity_before)
        self.selected_key=retained_context or ("category",changed[0].category)
        return self._persist_automatic_change(f"Saved category move for {len(changed):,} SDXL Tag{'s' if len(changed)!=1 else ''} to {self.session.path_label(target_id)}.",tuple(record.id for record in changed),kind="tag_membership")

    def set_category_base_tag(self,category_id,tag_id):
        """Use an existing member tag as the category anchor and autosave it."""
        if self.session.has_changes or self._automatic_save_task is not None:return False
        record=self.session.get(str(category_id)); tag=self.session._state.tags.get(str(tag_id))
        if record is None or tag is None or tag.category!=record.id:return False
        direct_base_id,direct_base_policy=self._direct_category_base_values(record.id)
        if direct_base_policy=="own" and direct_base_id==tag.id:return True
        self.session.update_category(
            record.id,name=record.name,parent_id=record.parent_id,description=record.description,
            color=record.color,color_policy=record.color_policy,base_tag_id=tag.id,base_tag_policy="own",
            base_member_scope=CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
            sensitivity_rating=record.sensitivity_rating,sensitivity_policy=record.sensitivity_policy,
        )
        return self._persist_automatic_change(
            f"Saved {tag.display} as the Base SDXL Tag for {record.name}.",
            category_ids=(record.id,),kind="category_details",
        )

    def _apply_category_base_candidate(
        self,candidate,proposal_text=None,apply_button=None,*,
        member_scope=CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
        allow_broader_review=False,
    ):
        """Confirm and save one still-current exact Category Base candidate."""
        if self._automatic_save_task is not None or self.session.has_changes:
            QMessageBox.information(
                self,"Wait for System Knowledge",
                "Wait for the current Category Workspace change to finish before applying this candidate.",
            )
            return False
        scope=dict(candidate) if isinstance(candidate,dict) else {}
        try:
            plan=self.session.preview_category_base_application(
                str(scope.get("category_id") or ""),
                str(scope.get("target_id") or ""),
                expected_match_basis=str(scope.get("match_basis") or ""),
                member_scope=member_scope,
                allow_broader_review=allow_broader_review,
            )
        except (KeyError,RuntimeError,TypeError,ValueError) as error:
            QMessageBox.information(
                self,"Category Base candidate changed",
                f"{error}\n\nNothing was applied. Run Find New Knowledge again for a current preview.",
            )
            if apply_button is not None:apply_button.setEnabled(False)
            return False
        target_effect=(
            "The current effective Base target is unchanged; this replaces a derived fallback with portable canonical authority."
            if plan.effective_target_unchanged else
            "This establishes a new effective Base target for the selected scope."
        )
        if plan.member_scope==CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS:
            scope_effect=(
                f"Scope: {plan.scope_label}; {plan.affected_direct_member_count:,} of "
                f"{plan.direct_member_count:,} direct members and "
                f"{plan.affected_descendant_member_count:,} descendant members across "
                f"{plan.descendant_category_count:,} descendant "
                f"Categor{'y' if plan.descendant_category_count==1 else 'ies'} are covered."
            )
        else:
            scope_effect=(
                f"Scope: {plan.scope_label}; {plan.affected_direct_member_count:,} of "
                f"{plan.direct_member_count:,} direct members are covered. "
                f"The {plan.descendant_member_count:,} members across "
                f"{plan.descendant_category_count:,} descendant "
                f"Categor{'y is' if plan.descendant_category_count==1 else 'ies are'} not granted descendant scope."
            )
        message=(
            f"Apply {plan.target} as the reviewed Base SDXL Tag for {plan.category}?\n\n"
            f"Exact evidence: {plan.evidence_label}.\n"
            "Before: derived exact-identity fallback with no canonical Category Base relationship.\n"
            f"After: one portable, layered canonical Category Base relationship. {target_effect}\n\n"
            f"{scope_effect}\n"
            f"Exact current comparison: {plan.changed_effective_base_count:,} branch SDXL "
            f"Tag{' changes' if plan.changed_effective_base_count==1 else 's change'} effective Base target. "
            f"{plan.tag_specific_base_override_count:,} existing Tag-specific Broader Tag{'s' if plan.tag_specific_base_override_count!=1 else ''} remain authoritative.\n\n"
            "Confirming saves through the existing recovery transaction and can be undone. Image Knowledge, captions, TXT, Metadata, pixels, and Automation rules are not rewritten."
        )
        if QMessageBox.question(
            self,"Apply Reviewed Category Base Candidate",message,
        )!=QMessageBox.Yes:
            return False
        try:self.session.apply_category_base_application(plan)
        except (KeyError,RuntimeError,TypeError,ValueError) as error:
            QMessageBox.information(
                self,"Category Base candidate changed",
                f"{error}\n\nNothing was applied. Review a fresh preview.",
            )
            if apply_button is not None:apply_button.setEnabled(False)
            return False
        if apply_button is not None:
            apply_button.setEnabled(False); apply_button.setText("Applying Proposed Base Tag…")
        def completed(_result):
            self._category_base_review_context={}
            if proposal_text is not None:
                proposal_text.setText(
                    f"Applied {plan.target} as the reviewed Base SDXL Tag for {plan.category}. "
                    +(
                        "The effective target did not change. "
                        if plan.effective_target_unchanged else
                        f"The new target changed {plan.changed_effective_base_count:,} branch SDXL Tag result{'s' if plan.changed_effective_base_count!=1 else ''}. "
                    )
                    +f"Scope: {plan.scope_label}. Undo and Redo save automatically."
                )
            if apply_button is not None:apply_button.setText("Applied")
        def failed(_message):
            if proposal_text is not None:
                proposal_text.setText(
                    "The candidate was not saved. Live System Knowledge was restored; run Find New Knowledge again before retrying."
                )
            if apply_button is not None:
                apply_button.setText("Apply Proposed Base Tag…"); apply_button.setEnabled(False)
        return self._persist_automatic_change(
            f"Saved {plan.target} as the reviewed Base SDXL Tag for {plan.category}.",
            category_ids=(plan.category_id,),kind="category_details",
            completion_callback=completed,failure_callback=failed,
            failure_operation="Category Base candidate",
        )

    def _reset_member_broader_tag_overrides(self):
        if self._automatic_save_task is not None:
            self._set_status("Wait for the current Category Workspace save to finish before resetting Broader Tags.",announce=True); return False
        if self.session.has_changes:
            self._set_status("Save or Undo the current staged Category Workspace change before previewing member Broader Tags.",announce=True); return False
        if not self.selected_key or self.selected_key[0]!="category":return False
        category_id=self.selected_key[1]
        dialog=CategoryBroaderTagResetDialog(self.session,category_id,self)
        if dialog.exec()!=QDialog.Accepted:return False
        plan=dialog.plan()
        if plan is None:return False
        try:applied=self.session.reset_member_broader_tag_overrides(plan)
        except (KeyError,RuntimeError,ValueError) as error:
            self._set_status(str(error),announce=True); return False
        count=len(applied.reset_tag_ids)
        category=self.session.get(category_id); name=category.name if category is not None else category_id
        return self._persist_automatic_change(
            f"Saved category-inheritance reset for {count:,} member Broader Tag{'s' if count!=1 else ''} in {name}.",
            tag_ids=applied.reset_tag_ids,category_ids=applied.category_ids,kind="tag_relationships",
        )

    def _populate_move_menu(self):
        self.move_menu.clear()
        move=self.move_menu.addAction("To Category…"); move.setEnabled(bool(self.selected_key)); move.triggered.connect(self._move_to_category)

    def _populate_actions_menu(self):
        self._fill_actions_menu(self.actions_menu)

    def _fill_actions_menu(self,menu):
        menu.clear(); keys=self._structure_selected_keys(); single_key=keys[0] if len(keys)==1 else None
        category_selected=bool(single_key and single_key[0]=="category"); tag_selected=bool(single_key and single_key[0]=="tag"); membership=self._membership_records(); mergeable=len(keys)>1 and all(key[0]=="category" for key in keys)
        protected=bool(category_selected and self.session.is_protected_category(single_key[1]))
        role=self.session.effective_semantic_role(single_key[1]) if category_selected else ""
        protected_reason=(
            f"This category defines the {CATEGORY_SEMANTIC_ROLE_LABELS.get(role,role.replace('_',' ').title())} system role. "
            "Its semantic placement is protected, while its name, base tag, sensitivity, color, and description remain customizable."
            if protected else ""
        )
        new=menu.addAction("New Child Category…" if category_selected else "New Category…"); new.triggered.connect(self._new_category)
        edit=menu.addAction("Edit Category Details…"); edit.setEnabled(category_selected); edit.setToolTip("Edit the selected category's safe presentation and inheritance details."); edit.triggered.connect(self._focus_category_details)
        add_tag=menu.addAction("Add Tag…"); add_available=bool(category_selected and not self.session.has_changes and self._automatic_save_task is None); add_tag.setEnabled(add_available)
        add_tag.setToolTip("Find an existing known SDXL Tag or create a new one in this category." if add_available else "Select one category and wait for the current automatic save before adding a tag."); add_tag.triggered.connect(self._request_add_tag)
        edit_tag=menu.addAction("Edit Selected Tag…"); edit_tag.setEnabled(len(membership)==1 or tag_selected); edit_tag.triggered.connect(lambda:self.edit_tag_requested.emit(single_key[1]) if tag_selected else self._edit_selected_membership_tag())
        change_tags=menu.addAction(f"Change Category for {len(membership):,} Selected Tag{'s' if len(membership)!=1 else ''}…"); change_tags.setEnabled(bool(membership)); change_tags.triggered.connect(self._change_selected_membership_categories)
        self._add_copy_names_action(menu)
        arrange=menu.addAction("Arrange Caption Order…"); arrange.triggered.connect(self._arrange_selected_caption_order)
        menu.addSeparator()
        merge_tags=menu.addAction("Merge Duplicate Tags…"); merge_tags.setEnabled(len(membership)>=2 and not self.has_unsaved_changes())
        merge_tags.setToolTip("Choose one canonical survivor for the complete selection." if len(membership)>=2 else "Select two or more SDXL Tags that represent the same identity.")
        merge_tags.triggered.connect(self._merge_selected_membership_tags)
        ignore=menu.addAction(f"Always Ignore {len(membership):,} Selected Tag{'s' if len(membership)!=1 else ''}…"); ignore.setEnabled(bool(membership) and not self.session.has_changes); ignore.setToolTip("Create exact global ignore rules for the complete tag selection." if not self.session.has_changes else "Wait for the current automatic System Knowledge save before writing Rules & Automation."); ignore.triggered.connect(self._always_ignore_selected_membership_tags)
        remove_tags=menu.addAction(f"Remove {len(membership):,} Selected Tag{'s' if len(membership)!=1 else ''} from System Knowledge…"); remove_tags.setEnabled(bool(membership)); remove_tags.setToolTip("Review removal of the selected reusable Tags. If images use them, review removal of those confirmed image facts first. Ignore rules stay unchanged."); remove_tags.triggered.connect(self._remove_selected_membership_tags)
        menu.addSeparator()
        move=menu.addAction("Move Category…"); move.setEnabled(category_selected and not protected); move.setToolTip(protected_reason or "Move the selected category under another Parent category."); move.triggered.connect(self._move_to_category)
        selected_categories=[key[1] for key in keys if key[0]=="category"]
        keep_id=(
            self.selected_key[1]
            if self.selected_key and self.selected_key[0]=="category" and self.selected_key[1] in selected_categories
            else selected_categories[0] if selected_categories else ""
        )
        removable=[value for value in selected_categories if value!=keep_id]
        merge_allowed=mergeable and not any(self.session.is_protected_category(value) for value in removable)
        merge=menu.addAction("Preview Merge…"); merge.setEnabled(merge_allowed)
        merge.setToolTip(
            "Merge the other selected categories into the current category."
            if merge_allowed else
            "A protected system-role category cannot be merged away. Make the protected category the current retained category, or change the selection."
            if mergeable else "Select two or more categories to preview a merge."
        )
        merge.triggered.connect(self._merge_selected)
        delete=menu.addAction("Delete Category…"); delete.setEnabled(bool(category_selected and single_key[1]!="uncategorized" and not protected))
        delete.setToolTip(protected_reason or ("Preview removal of this category record and promotion of its direct members." if delete.isEnabled() else "Select one category. Uncategorized cannot be deleted."))
        delete.triggered.connect(self._delete_selected_category)
        restore=menu.addAction("Restore Default"); restore.setEnabled(bool(category_selected and single_key[1] in self.session._default_categories)); restore.triggered.connect(self._restore_default)
        issues=self.session.structural_issues(); health=menu.addAction("Review Health Issues…"); health.setEnabled(bool(issues)); health.setToolTip("Review duplicate names, cycles, missing parents, or dangling references." if issues else "No Category Workspace health issues are present."); health.triggered.connect(self._show_health_issues)
        if tag_selected:
            reveal=menu.addAction("Open Matching Images in Dataset…"); reveal.triggered.connect(self._open_gallery_in_dataset)
        menu.addSeparator(); expand=menu.addAction("Expand All"); expand.triggered.connect(self.structure_tree.expandAll); collapse=menu.addAction("Collapse All"); collapse.triggered.connect(self.structure_tree.collapseAll)
        reset=menu.addAction("Cancel Pending Change"); reset.setEnabled(self.session.has_changes and self._automatic_save_task is None); reset.setToolTip("Cancel a pending in-memory change before automatic saving starts."); reset.triggered.connect(self._reset)

    def _menus_changed(self,*_):
        keys=self._structure_selected_keys()
        movable=bool(
            len(keys)==1 and keys[0][0]=="category"
            and not self.session.is_protected_category(keys[0][1])
        )
        self.move_button.setEnabled(movable)
        if len(keys)==1 and keys[0][0]=="category" and self.session.is_protected_category(keys[0][1]):
            role=self.session.effective_semantic_role(keys[0][1])
            label=CATEGORY_SEMANTIC_ROLE_LABELS.get(role,role.replace("_"," ").title())
            move_tip=f"This category defines the {label} system role, so its semantic placement is protected."
        else:
            move_tip="Move the selected category under another Parent category." if movable else "Select one category to move it."
        self.move_button.setToolTip(move_tip)
        self.move_button.setAccessibleDescription(self.move_button.toolTip())
        self.actions_button.setAccessibleDescription("Open the commands available for the complete current selection, including disabled-command explanations.")
        self._refresh_actions()

    def _refresh_actions(self,*,reuse_health=False):
        busy=self._automatic_save_task is not None
        self.undo_button.setEnabled(self.session.can_undo and not busy); self.redo_button.setEnabled(self.session.can_redo and not busy)
        add_available=bool(self.selected_key and self.selected_key[0]=="category" and not self.session.has_changes and self._automatic_save_task is None)
        self.add_tag_button.setEnabled(add_available)
        self.add_tag_button.setToolTip("Find existing known SDXL Tags or create a new one in this category." if add_available else "Choose one category and wait for the current automatic save before adding a tag.")
        self.add_tag_button.setAccessibleDescription(self.add_tag_button.toolTip())
        staged=bool(self.session.has_changes)
        if staged!=self._last_staged_state:self._last_staged_state=staged; self.staged_state_changed.emit(staged)
        if reuse_health and self._last_health_issues is not None:
            issues=self._last_health_issues
        else:
            issues=self.session.structural_issues(); self._last_health_issues=issues
        parts=["Saving changes…" if busy else "All category changes are saved."]
        keys=self._structure_selected_keys()
        if len(keys)>1:
            categories=sum(key[0]=="category" for key in keys); tags=sum(key[0]=="tag" for key in keys)
            parts.append(
                f"{counted_noun(len(keys),'item')} selected "
                f"({counted_noun(categories,'category','categories')}, "
                f"{counted_noun(tags,'Tag')}). "
                "Available actions apply to the full selection."
            )
        membership_count=len(self.membership_chips.selected_values())
        if membership_count:parts.append(f"{membership_count:,} direct SDXL Tag{'s' if membership_count!=1 else ''} selected in the inspector.")
        if self._structure_result_summary:parts.append(self._structure_result_summary)
        if issues:parts.append(f"{len(issues):,} health issue(s) need attention.")
        self._set_context_status(" ".join(parts))

    def _context_menu(self,position):
        item=self.structure_tree.itemAt(position)
        if item is not None:
            if not item.isSelected():self.structure_tree.clearSelection(); item.setSelected(True)
            self.structure_tree.setCurrentItem(item)
        menu=QMenu(self); self._fill_actions_menu(menu); menu.exec(self.structure_tree.viewport().mapToGlobal(position))

    def _activate_structure_item(self,item,_column=0):
        """Dispatch a tree double-click through the existing edit commands."""
        key=self._key(item)
        if key is None or len(self._structure_selected_keys())!=1:
            return
        if key[0]=="category":
            self._focus_category_details()
        elif key[0]=="tag":
            self.edit_tag_requested.emit(key[1])

    def _can_close_category_details(self):
        self._flush_details()
        if self._automatic_save_task is not None:
            self._set_status("Saving Category details. Wait for the saved or recovery result before closing.",announce=True)
            return False
        if self._category_edit_validation_pending or self._category_name_dirty:
            if QMessageBox.question(
                self._category_edit_dialog,"Discard unsaved Category correction?",
                "The latest Category correction was not saved because a value is invalid. "
                "Discard that correction and close? Choose No to keep editing.",
            )!=QMessageBox.Yes:return False
            record=self.session.get(self.selected_key[1]) if self.selected_key else None
            if record is not None:self._refresh_staged_category_inspector(record)
            self._category_name_dirty=False; self._category_edit_validation_pending=False
            self._set_status("Unsaved Category correction discarded. The saved Category is unchanged.",announce=True)
        return True

    def _focus_category_details(self,*_,show_policies=False):
        if not self.selected_key or self.selected_key[0]!="category":return
        self._flush_details(); record=self.session.get(self.selected_key[1])
        if record is None:return
        dialog=CategoryDetailsDialog(self._can_close_category_details,self); dialog.setWindowTitle(f"Edit Category — {record.name}"); dialog.resize(940,760)
        self._category_edit_dialog=dialog; self._category_edit_validation_pending=False
        layout=QVBoxLayout(dialog)
        protected=self.session.is_protected_category(record.id)
        role=self.session.effective_semantic_role(record.id)
        role_label=CATEGORY_SEMANTIC_ROLE_LABELS.get(role,role.replace("_"," ").title())
        context=QLabel(f"Protected system category • {role_label}" if protected else "Editable category")
        context.setObjectName("sectionTitle"); layout.addWidget(context)
        explanation=QLabel(
            f"This category defines the {role_label} system role. Its system location is fixed so character and reusable-value behavior remains deterministic. "
            "Its name, Base meaning, sensitivity, appearance, and description still save automatically."
            if protected else
            "Routine changes save automatically. A Parent category change asks for confirmation because it can change inherited Base meaning, sensitivity, color, and caption order."
        )
        explanation.setObjectName("mutedLabel"); explanation.setWordWrap(True); layout.addWidget(explanation)
        candidate=dict(self._category_base_review_context or {})
        if str(candidate.get("category_id") or "")==record.id:
            proposal=QGroupBox("Find New Knowledge candidate")
            proposal_layout=QVBoxLayout(proposal)
            if "independently_safe" in candidate:
                broader_review=not bool(candidate.get("independently_safe"))
            else:
                broader_review=not (
                    bool(candidate.get("effective_target_unchanged",True))
                    and int(candidate.get("descendant_category_count") or 0)==0
                )
            try:
                application_plan=self.session.preview_category_base_application(
                    record.id,str(candidate.get("target_id") or ""),
                    expected_match_basis=str(candidate.get("match_basis") or ""),
                    member_scope=CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
                    allow_broader_review=broader_review,
                )
                candidate_error=""
            except (KeyError,RuntimeError,TypeError,ValueError) as error:
                application_plan=None; candidate_error=str(error)
            scope_picker=QComboBox()
            scope_picker.setAccessibleName("Category Base candidate member scope")
            scope_picker.addItem(
                "Direct members only",CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
            )
            descendant_count=int(
                application_plan.descendant_category_count
                if application_plan is not None else
                candidate.get("descendant_category_count") or 0
            )
            if descendant_count:
                scope_picker.addItem(
                    "Direct and descendant members",
                    CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS,
                )
            scope_picker.setAccessibleDescription(
                "Choose whether this reviewed Category Base authority applies only to direct members or also to members of descendant Categories."
            )
            scope_form=QFormLayout()
            scope_form.addRow("Member scope",scope_picker)
            proposal_layout.addLayout(scope_form)
            proposal_text=QLabel()
            proposal_text.setWordWrap(True)
            proposal_text.setAccessibleName("Category Base discovery candidate")
            proposal_layout.addWidget(proposal_text)
            apply_candidate=QPushButton("Apply Proposed Base Tag…")
            apply_candidate.setProperty("primary",True)
            apply_candidate.setEnabled(application_plan is not None)
            apply_candidate.setAccessibleName("Apply reviewed Category Base candidate")
            apply_candidate.setAccessibleDescription(
                "Revalidate the exact target and selected member scope, show the current branch impact for confirmation, then save through Category Workspace recovery and Undo."
            )
            def show_candidate_preview(plan,error=""):
                if plan is None:
                    proposal_text.setText(
                        f"Proposed Base SDXL Tag: {candidate.get('target') or candidate.get('target_id') or 'Unknown'}\n"
                        f"Exact evidence: {candidate.get('evidence_label') or 'Exact stable identity'}\n\n"
                        f"Candidate no longer current: {error}"
                    )
                    apply_candidate.setEnabled(False)
                    return
                target_effect=(
                    "The current effective target is unchanged."
                    if plan.effective_target_unchanged else
                    "This would establish a new effective target."
                )
                descendant_effect=(
                    f"{plan.affected_descendant_member_count:,} of {plan.descendant_member_count:,} descendant members across "
                    f"{plan.descendant_category_count:,} descendant Categor{'y' if plan.descendant_category_count==1 else 'ies'} are covered."
                    if plan.member_scope==CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS else
                    f"{plan.descendant_member_count:,} descendant members across {plan.descendant_category_count:,} descendant "
                    f"Categor{'y is' if plan.descendant_category_count==1 else 'ies are'} excluded from the authority scope."
                )
                proposal_text.setText(
                    f"Proposed Base SDXL Tag: {plan.target}\n"
                    f"Exact evidence: {plan.evidence_label}\n"
                    f"Before: {candidate.get('before') or 'No canonical Category Base authority.'}\n"
                    f"After: {candidate.get('after') or 'One reviewed canonical Category Base relationship.'}\n"
                    f"Selected scope: {plan.scope_label}. {target_effect}\n"
                    f"Impact: {plan.affected_direct_member_count:,} of {plan.direct_member_count:,} direct members; "
                    f"{descendant_effect} Exact comparison shows {plan.changed_effective_base_count:,} branch SDXL "
                    f"Tag{' changes' if plan.changed_effective_base_count==1 else 's change'} effective Base target. "
                    f"{plan.tag_specific_base_override_count:,} existing Tag-specific Broader Tags remain authoritative."
                )
                apply_candidate.setEnabled(True)
            def refresh_candidate_preview(*_args):
                selected_scope=str(
                    scope_picker.currentData() or CATEGORY_BASE_MEMBER_SCOPE_DIRECT
                )
                try:
                    plan=self.session.preview_category_base_application(
                        record.id,str(candidate.get("target_id") or ""),
                        expected_match_basis=str(candidate.get("match_basis") or ""),
                        member_scope=selected_scope,
                        allow_broader_review=broader_review,
                    )
                    error=""
                except (KeyError,RuntimeError,TypeError,ValueError) as exc:
                    plan=None; error=str(exc)
                show_candidate_preview(plan,error)
            apply_candidate.clicked.connect(lambda:self._apply_category_base_candidate(
                candidate,proposal_text,apply_candidate,
                member_scope=str(
                    scope_picker.currentData() or CATEGORY_BASE_MEMBER_SCOPE_DIRECT
                ),
                allow_broader_review=broader_review,
            ))
            scope_picker.currentIndexChanged.connect(refresh_candidate_preview)
            show_candidate_preview(application_plan,candidate_error)
            proposal_layout.addWidget(apply_candidate); layout.addWidget(proposal)
        parent_path=self.session.path_label(record.parent_id) if record.parent_id else "Top level"
        self.category_parent_label.setText("System location" if protected else "Parent category")
        self.category_parent.setVisible(not protected); self.category_parent_value.setVisible(protected)
        self.category_parent_value.setText(f"{parent_path} — fixed by the {role_label} system role" if protected else "")
        self.category_advanced_toggle.setChecked(
            bool(show_policies or str(candidate.get("category_id") or "")==record.id)
        )
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setAccessibleName("Edit Category sections"); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.category_fields); layout.addWidget(scroll,1); self.category_fields.show()
        self.category_parent.setEnabled(True)
        self.category_parent.setToolTip(
            f"This system location is fixed because the category defines the {role_label} role."
            if protected else
            parent_path
        )
        feedback=QLabel("Changes save automatically. Wait for Saved before closing.")
        feedback.setObjectName("categoryEditStatus"); feedback.setAccessibleName(feedback.text())
        feedback.setWordWrap(True); layout.addWidget(feedback)
        self._category_edit_status=feedback
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        self._category_edit_buttons=buttons
        self.category_name.setFocus(); self.category_name.selectAll()
        try:dialog.exec()
        finally:
            self._category_base_review_context={}
            self._flush_details(); self.category_parent.setEnabled(True)
            self._category_edit_dialog=None; self._category_edit_status=None; self._category_edit_buttons=None
            scroll.takeWidget(); self.category_fields.setParent(self.inspector); self.category_fields.hide(); dialog.deleteLater()

    def _show_health_issues(self):
        issues=self.session.structural_issue_records()
        if not issues:
            QMessageBox.information(self,"Category Health","No Category Workspace health issues are present."); return
        dialog=QDialog(self); dialog.setWindowTitle("Category Health"); dialog.resize(920,460)
        layout=QVBoxLayout(dialog); explanation=QLabel("Select an issue to reveal its first affected category or SDXL Tag. Nothing changes from this review.")
        explanation.setWordWrap(True); layout.addWidget(explanation)
        tree=QTreeWidget(); tree.setHeaderLabels(("Area","Issue","Affected records")); tree.setRootIsDecorated(False); tree.setAccessibleName("Category Workspace health issues")
        for issue in issues:
            labels=[]
            for kind,record_id in issue.record_keys:
                if kind=="category":record=self.session.get(record_id); labels.append(record.name if record else record_id)
                elif kind=="tag":record=self.session._state.tags.get(record_id); labels.append(record.display if record else record_id)
                elif kind=="pattern":record=self.session._state.patterns.get(record_id); labels.append(record.name if record else record_id)
            item=QTreeWidgetItem((issue.area,issue.summary,", ".join(labels) or "Not available")); item.setData(0,Qt.UserRole,issue); tree.addTopLevelItem(item)
        for column in range(tree.columnCount()):tree.resizeColumnToContents(column)
        tree.header().setStretchLastSection(True); layout.addWidget(tree,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); reveal=buttons.addButton("Reveal Selected",QDialogButtonBox.ActionRole); reveal.setEnabled(False)
        tree.currentItemChanged.connect(lambda current,_previous:reveal.setEnabled(bool(current and any(key[0] in {"category","tag"} for key in current.data(0,Qt.UserRole).record_keys))))
        reveal.clicked.connect(lambda:self._reveal_health_issue(tree.currentItem().data(0,Qt.UserRole) if tree.currentItem() else None,dialog))
        buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if tree.topLevelItemCount():tree.setCurrentItem(tree.topLevelItem(0))
        dialog.exec()

    def _reveal_health_issue(self,issue,dialog=None):
        key=next((key for key in getattr(issue,"record_keys",()) if key[0] in {"category","tag"}),None)
        if key is None:return
        if dialog is not None:dialog.accept()
        self.views.setCurrentIndex(0); self.search.clear(); self.selected_key=key; self.refresh_structure(); self._show_details(key); self._refresh_actions()
        if self.structure_tree.currentItem() is None or self._key(self.structure_tree.currentItem())!=key:
            self._set_context_status(f"{issue.summary} The affected {key[0]} is open in the inspector but cannot appear in the tree until its missing relationship is repaired.",announce=True)

    def _structure_selected_keys(self):
        keys=[key for key in (self._key(item) for item in self.structure_tree.selectedItems()) if key and key[0] in {"category","tag"}]
        if self.selected_key and self.selected_key[0]=="tag" and len(keys)==1:
            tag=self.session._state.tags.get(self.selected_key[1])
            if tag is not None and keys[0]==("category",tag.category):return [self.selected_key]
        return keys

    def _selected_category_ids(self):
        return [key[1] for key in self._structure_selected_keys() if key[0]=="category"]

    def create_category_dialog(
        self,*,parent=None,initial_name="",initial_parent_id=None,
    ):
        if self.has_unsaved_changes():
            QMessageBox.information(
                parent or self,"Wait for System Knowledge",
                "A Category Workspace change is still saving. Create the Category after it finishes.",
            )
            return None
        dialog=CategoryCreationDialog(
            self.session,initial_name=initial_name,
            initial_parent_id=initial_parent_id,parent=parent or self,
        )

        def create(name,parent_id):
            if self.has_unsaved_changes():
                dialog.set_error(
                    "A Category Workspace change started while this form was open. "
                    "Wait for it to finish, then try again."
                ); return
            try:
                record=self.session.create_category(
                    name,parent_id=parent_id,
                    color_policy="parent_lighter" if parent_id else "none",
                )
            except ValueError as error:
                dialog.set_error(str(error)); return
            self.selected_key=("category",record.id); dialog.set_saving()

            def complete(_result):
                dialog.finish(record.id,self.session.path_label(record.id))

            self._persist_automatic_change(
                f"Saved new category {record.name}.",category_ids=(record.id,),
                kind="category_structure",completion_callback=complete,
                failure_callback=dialog.set_error,failure_operation="Category creation",
            )

        dialog.create_requested.connect(create)
        return dialog.created_category() if dialog.exec()==QDialog.Accepted else None

    def _new_category(self):
        keys=self._structure_selected_keys(); parent=keys[0][1] if len(keys)==1 and keys[0][0]=="category" else None
        self.create_category_dialog(parent=self,initial_parent_id=parent)

    def new_category(self):
        self._new_category()

    def _move_to_category(self):
        if not self.selected_key or len(self._structure_selected_keys())!=1:return
        if self.selected_key[0]=="category" and self.session.is_protected_category(self.selected_key[1]):
            role=self.session.effective_semantic_role(self.selected_key[1])
            label=CATEGORY_SEMANTIC_ROLE_LABELS.get(role,role.replace("_"," ").title())
            QMessageBox.information(
                self,"Move not available",
                f"This category defines the {label} system role, so its semantic placement is protected.",
            )
            return
        choices=[(self.session.path_label(record.id),record.id) for record in self.session.all_categories() if not (self.selected_key[0]=="category" and (record.id==self.selected_key[1] or record.id in self.session.descendant_ids(self.selected_key[1])))]
        if self.selected_key[0]=="category" and self.session.get(self.selected_key[1]).parent_id is not None:choices.insert(0,("Top level",None))
        if not choices:
            QMessageBox.information(self,"Move not available","No compatible destination category is available."); return
        target,ok=self._choose_category("Move to Category","Destination category",choices)
        if not ok:return
        if self.selected_key[0]=="tag":
            self.apply_tag_category_change((self.selected_key[1],),target,selected_key=self.selected_key); return
        else:
            record=self.session.get(self.selected_key[1])
            if not self._confirm_reparent(record,target):return
            sensitivity_before=self._sensitivity_snapshot(
                self._category_sensitivity_tag_ids(record.id)
            )
            self.session.update_category(record.id,name=record.name,parent_id=target,description=record.description,color=record.color,color_policy=record.color_policy)
            self._record_sensitivity_impact(sensitivity_before)
            outcome=f"Saved Parent category change for {record.name}."
        self.refresh_all(); self._show_details(self.selected_key)
        self._persist_automatic_change(outcome,category_ids=(record.id,),kind="category_structure")

    def _choose_category(self,title,label,choices):
        dialog=QDialog(self); dialog.setWindowTitle(title); layout=QVBoxLayout(dialog); layout.addWidget(QLabel(label))
        combo=BoundedSearchableComboBox(placeholder="Type any part of a category path…",max_visible=12); combo.setAccessibleName(label)
        for choice_label,category_id in choices:combo.addItem(choice_label,category_id)
        layout.addWidget(combo); buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        accepted=dialog.exec()==QDialog.Accepted
        return (combo.currentData() if accepted and combo.count() else None),accepted

    def _merge_selected(self):
        selected=self._selected_category_ids()
        if len(selected)<2:return
        keep=self.selected_key[1] if self.selected_key and self.selected_key[0]=="category" and self.selected_key[1] in selected else selected[0]
        remove=[value for value in selected if value!=keep]
        try:impact=self.session.preview_category_merge(keep,remove)
        except ValueError as error:QMessageBox.information(self,"Merge not available",str(error)); return
        message=f"Keep {self.session.path_label(keep)} and merge {len(remove):,} selected category(s) into it?\n\nReassign {len(impact.tag_ids):,} SDXL Tag(s), {len(impact.pattern_ids):,} reusable pattern(s), and {len(impact.child_categories):,} child category reference(s). Stable tag and category IDs outside the removed records remain unchanged."
        if QMessageBox.question(self,"Preview Category Merge",message)!=QMessageBox.Yes:return
        sensitivity_before=self._sensitivity_snapshot(
            self._category_sensitivity_tag_ids(*remove)
        )
        try:self.session.merge_categories(keep,remove)
        except ValueError as error:QMessageBox.information(self,"Categories not merged",str(error)); return
        self._record_sensitivity_impact(sensitivity_before)
        self.selected_key=("category",keep); self.refresh_all(); self._show_details(self.selected_key)
        self._persist_automatic_change(f"Saved category merge into {self.session.path_label(keep)}.",category_ids=(keep,*remove),kind="category_structure")

    def _delete_selected_category(self):
        keys=self._structure_selected_keys()
        if len(keys)!=1 or keys[0][0]!="category":return
        category_id=keys[0][1]; record=self.session.get(category_id)
        if record is None:
            QMessageBox.information(self,"Delete not available","The selected category is no longer available."); return
        replacement_id=None
        if record.parent_id is None:
            descendants=self.session.descendant_ids(category_id)
            choices=[
                (self.session.path_label(candidate.id),candidate.id) for candidate in self.session.all_categories()
                if candidate.id!=category_id and candidate.id not in descendants
            ]
            if not choices:
                QMessageBox.information(self,"Delete not available","No valid replacement category is available for this top-level category."); return
            replacement_id,accepted=self._choose_category("Delete Top-Level Category","Move its direct members to",choices)
            if not accepted:return
        try:impact=self.session.preview_category_deletion(category_id,replacement_id=replacement_id)
        except ValueError as error:QMessageBox.information(self,"Delete not available",str(error)); return
        destination=self.session.path_label(impact.destination_id)
        shipped=" This shipped category will be disabled so an update does not recreate it." if impact.disables_default else ""
        message=(
            f"Delete {self.session.path_label(category_id)}?\n\n"
            f"Move {len(impact.child_categories):,} direct child categor{'y' if len(impact.child_categories)==1 else 'ies'}, "
            f"{len(impact.tag_ids):,} direct SDXL Tag{'s' if len(impact.tag_ids)!=1 else ''}, and "
            f"{len(impact.pattern_ids):,} reusable pattern{'s' if len(impact.pattern_ids)!=1 else ''} to {destination}. "
            "Deeper descendants remain attached to their promoted direct children."
            f"{shipped}\n\nOnly the category record is removed. Stable surviving IDs, Image Knowledge, captions, Metadata, raw evidence, and image files remain unchanged. Confirming saves the System Knowledge transaction automatically."
        )
        if QMessageBox.question(self,"Preview Category Deletion",message)!=QMessageBox.Yes:return
        sensitivity_before=self._sensitivity_snapshot(
            self._category_sensitivity_tag_ids(category_id)
        )
        try:self.session.delete_category(category_id,replacement_id=replacement_id)
        except ValueError as error:QMessageBox.information(self,"Category not deleted",str(error)); return
        self._record_sensitivity_impact(sensitivity_before)
        self.selected_key=("category",impact.destination_id); self.refresh_all(); self._show_details(self.selected_key)
        self._persist_automatic_change(
            f"Saved category deletion; direct members moved to {destination}.",
            tag_ids=impact.tag_ids,category_ids=(category_id,impact.destination_id,*impact.child_categories),kind="category_structure",
        )

    def _restore_default(self):
        if not self.selected_key or self.selected_key[0]!="category":return
        sensitivity_before=self._sensitivity_snapshot(
            self._category_sensitivity_tag_ids(self.selected_key[1])
        )
        try:self.session.restore_category_default(self.selected_key[1])
        except ValueError as error:QMessageBox.information(self,"Default not available",str(error)); return
        self._record_sensitivity_impact(sensitivity_before)
        category_id=self.selected_key[1]; label=self.session.path_label(category_id); self.refresh_all(); self._show_details(self.selected_key)
        self._persist_automatic_change(f"Saved restored shipped category default for {label}.",category_ids=(category_id,),kind="category_structure")

    def _undo(self):
        if self.save_in_progress:return
        self._details_timer.stop()
        was_staged=self.session.has_changes; routine_kind,routine_tag_ids,routine_category_ids=self._routine_action_change(); routine_sensitivity_tag_ids=self._routine_action_sensitivity_tag_ids(); routine_relationship_ids=self._routine_action_relationship_ids()
        if not self.session.undo():return
        if not was_staged and self.session.last_history_target_was_saved:
            self._persist_automatic_change("Saved Undo of the last routine Category Workspace change.",routine_tag_ids,routine_category_ids,routine_kind,sensitivity_tag_ids=routine_sensitivity_tag_ids,relationship_ids=routine_relationship_ids); return
        self.refresh_all(); self._show_details(self.selected_key) if self.selected_key else None; self._set_status("Undid the last staged Category Workspace change.",announce=True)

    def _redo(self):
        if self.save_in_progress:return
        self._details_timer.stop()
        was_staged=self.session.has_changes
        if not self.session.redo():return
        if not was_staged and self.session.last_history_target_was_saved:
            routine_kind,routine_tag_ids,routine_category_ids=self._routine_action_change(); routine_sensitivity_tag_ids=self._routine_action_sensitivity_tag_ids()
            self._persist_automatic_change("Saved Redo of the last routine Category Workspace change.",routine_tag_ids,routine_category_ids,routine_kind,sensitivity_tag_ids=routine_sensitivity_tag_ids); return
        self.refresh_all(); self._show_details(self.selected_key) if self.selected_key else None; self._set_status("Redid the last staged Category Workspace change.",announce=True)

    def _reset(self):
        if self.save_in_progress:return
        self._details_timer.stop(); self.session.reset(); self.refresh_all(); self._show_details(self.selected_key) if self.selected_key else None; self._set_status("Discarded staged changes and restored saved System Knowledge.",announce=True)

    def _organization_depends(self):
        return self.sequence._organization_depends()

    def _impact(self):
        impact=self.session.impact(organization_depends_on_order=self._organization_depends())
        try:
            index=self.image_index_provider() if callable(self.image_index_provider) else None
            tag_records={**self.session._initial.tags,**self.session._state.tags}
            paths=(
                None if index is None else affected_order_projection_paths(
                    impact,tag_records,index.all_records(),
                    aliases_for=self.tags.effective_aliases,
                )
            )
        except Exception:paths=None
        return self.session.impact(organization_depends_on_order=self._organization_depends(),affected_image_paths=paths)

    def _load_examples(self,key):
        if self._gallery_task is not None and hasattr(self._gallery_task,"cancel"):
            self._gallery_task.cancel()
        self._gallery_generation+=1; generation=self._gallery_generation
        kind,record_id=key
        if kind=="tag":
            record=self.session._state.tags[record_id]; records=(record,); context=f'Examples using “{record.display}”'; match_all=True; context_key=key; self.gallery.open_dataset.show()
        elif kind=="membership":
            records=tuple(self.session._state.tags[value] for value in record_id if value in self.session._state.tags)
            if not records:
                if self.selected_key and self.selected_key[0]=="category":self._load_examples(self.selected_key)
                return
            context=(f'Examples using “{records[0].display}”' if len(records)==1 else f"Examples using all {len(records):,} selected SDXL Tags")
            match_all=True; context_key=("membership",tuple(record.id for record in records)); self.gallery.open_dataset.setVisible(len(records)==1)
        else:
            category=self.session.get(record_id); category_ids={record_id,*self.session.descendant_ids(record_id)}; records=tuple(record for record in self.session.all_tags() if record.category in category_ids); context=f'Examples in “{category.name}”'; match_all=False; context_key=key; self.gallery.open_dataset.hide()
        self._gallery_context_key=context_key
        try:index=self.image_index_provider() if callable(self.image_index_provider) else None
        except Exception as error:self.gallery.set_error(str(error),context); return
        if index is None:self.gallery.set_unavailable(context=context); return
        from core import normalize_tag
        self.gallery.set_loading(context)
        groups=[]
        for record in records:
            fingerprint=(record.display,tuple(record.aliases))
            cached=self._identity_group_cache.get(record.id)
            if cached is None or cached[0]!=fingerprint:
                cached=(
                    fingerprint,
                    frozenset(
                        normalize_tag(value)
                        for value in self.tags.identity_names(record)
                        if normalize_tag(value)
                    ),
                )
                self._identity_group_cache[record.id]=cached
            groups.append(cached[1])
        groups=tuple(groups)
        def work(job=None):
            # A broad category can contain thousands of Tags. Cached identity
            # normalization above keeps setup bounded; the catalog evidence
            # scan itself remains cancellable coordinated worker work.
            progress=(
                (lambda current,total,message:self.job_manager.update(
                    job.id,current,total,message,
                )) if job is not None else None
            )
            return self.evidence_queries.query(
                index,groups,match_all,
                should_continue=job.checkpoint if job is not None else None,
                progress=progress,
            )
        if self.job_manager is not None:
            task=CoordinatedCallTask(
                self.job_manager,"Search Tag Encyclopedia examples",work,
                resource="cpu",
                key=(
                    f"evidence:tag-encyclopedia:{kind}:{record_id}:"
                    f"{len(records)}:{id(self.tags)}"
                ),
            )
            self._gallery_task=task
            task.signals.complete.connect(
                lambda paths,g=generation,k=context_key,c=context:
                self._examples_loaded((g,k,paths,c))
            )
            task.signals.failed.connect(
                lambda message,g=generation,c=context:
                self._examples_failed(g,c,message)
            )
            task.start()
        else:
            task=AsyncCallTask(work); self._gallery_task=task
            task.signals.complete.connect(
                lambda paths,g=generation,k=context_key,c=context:
                self._examples_loaded((g,k,paths,c))
            )
            task.signals.failed.connect(
                lambda message,g=generation,c=context:
                self._examples_failed(g,c,message)
            )
            from PySide6.QtCore import QThreadPool
            (self.pool or QThreadPool.globalInstance()).start(task)

    def refresh_examples(self):
        """Rerun only the currently visible contextual evidence query."""
        if self._gallery_context_key is not None:
            self._load_examples(self._gallery_context_key)

    def _examples_loaded(self,result):
        generation,key,paths,context=result
        if generation!=self._gallery_generation or key!=self._gallery_context_key:return
        configure=self.gallery_status.configure if self.gallery_status is not None else None
        self.gallery.set_paths(paths,total_count=len(paths),context=context,configure=configure); self._gallery_task=None

    def _examples_failed(self,generation,context,message):
        if generation!=self._gallery_generation:return
        self.gallery.set_error(message,context); self._gallery_task=None

    def _open_gallery_in_dataset(self):
        records=self._membership_records()
        if self.selected_key and self.selected_key[0]=="category" and len(records)==1:
            self.show_dataset_tag.emit(records[0].display); return
        if self.selected_key and self.selected_key[0]=="tag":
            record=self.session._state.tags.get(self.selected_key[1])
            if record is not None:self.show_dataset_tag.emit(record.display)

    def has_unsaved_changes(self):
        return self.session.has_changes or self.save_in_progress

    @property
    def save_in_progress(self):
        return self._automatic_save_task is not None

    def discard_staged_changes(self):
        if self.save_in_progress:return False
        self._details_timer.stop(); self.session.reset(); self.refresh_all()
        return True

    def reload_from_live(self):
        if self.has_unsaved_changes():return False
        self.categories.load(); self.tags.load(); self.patterns.load(); self.tags.apply_category_ordering(self.categories)
        self.session=self._session_factory(self.tags,self.categories,self.patterns,self.characters); self.sequence.session=self.session; self.refresh_all()
        if self.selected_key:self._show_details(self.selected_key,refresh_examples=False)
        return True
