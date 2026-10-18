from __future__ import annotations

from .help_content import workspace_help

import json
import copy
import hashlib
import time
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QItemSelectionModel,QRect,QSignalBlocker,QSize,QThreadPool,QTimer,Qt,Signal
from PySide6.QtWidgets import (
    QAbstractItemView,QApplication,QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFormLayout,QGroupBox,QHBoxLayout,QInputDialog,
    QFrame,QHeaderView,QLabel,QLineEdit,QListWidget,QMenu,QMessageBox,QPushButton,QScrollArea,QSpinBox,QSplitter,QTabBar,QTabWidget,QTextEdit,
    QSizePolicy,QStackedWidget,QToolButton,QTreeView,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly

from core import (
    AutomationRuleRepository,CategoryRegistry,CharacterDatabase,CharacterRecordEditService,CharacterTagBridge,ConfirmedTagIdentityService,DefaultOverrideService,DownstreamChangeClassification,DownstreamUpdateStore,EvidenceQueryService,FoundationKnowledge,
    ImageWorkflowStateService,MAX_TAG_PRIORITY,MIN_TAG_PRIORITY,ORDER_GROUP_DEFAULT_PRIORITY,ORDER_GROUP_LABELS,ORDER_GROUPS,OrderImpact,OrganizationProfile,
    ConflictRuleEngine,PatternRegistry,TagCollection,default_order_group,effective_tag_sensitivity,
    TAG_BROADER_PROPOSAL_TASK_TYPE,TAG_MEANING_TASK_TYPE,
    active_automation_engine,classify_system_knowledge_change,
    exact_category_anchor_ids,reviewed_sensitivity_proposals,sensitivity_health,
)
from core.knowledge_relationships import ENTAILMENT,SCOPED_CONSTRAINT,SPECIFICITY
from core.tag_record_save import (TagRecordSaveRequest,TagDetailsSaveRequest,TagSaveAuthorities,TagSaveImpactRequest,TagSaveImpact,save_tag_details,load_tag_save_authority)
from shiboken6 import isValid
from core.tag_database import TagRecord
from core.automation_rule_conversion import RulePairAdmission,RuleConversionAdmissionError
from core.consumer_policies import (
    COMPATIBILITY_RESOLUTION,CONSUMER_IDS,EXACT_TAG,EXCLUDE_FROM_CONSUMER,
    SEARCH_ORGANIZATION,CANONICALIZE_IDENTITY,ConsumerPolicyResolver,
)
from core.consumer_policy_search import ConsumerPolicySearchService
from core.allowed_dimension_policy import (
    AllowedDimensionPolicyService,AllowedDimensionPolicyUpdate,
    AllowedDimensionValueScope,VALUE_SCOPE_ALL,VALUE_SCOPE_ALL_EXCEPT,
    VALUE_SCOPE_ONLY,VALUE_SCOPE_UNREVIEWED,
)
from core.modifier_composition import ModifierCompositionService
from core.tag_semantic_relationships import (
    TagSemanticChange,TagSemanticRelationshipService,
)
from core.tag_alias_relationships import (
    TagAliasChange,TagAliasProposal,TagAliasRelationshipService,
)
from core.modifier_role_decisions import modifier_role_resolutions
from core.knowledge_editor_context import KnowledgeEditorContext,PreparedCharacterDatabase,adopt_live_tag_authority
from core.paths import (
    APP_ROOT,CATEGORIES_PATH,CONSUMER_POLICIES_PATH,DEFAULTS_DIR,PATTERNS_PATH,
    RELATIONSHIPS_PATH,USER_DATA_DIR,
)
from core.relationship_workspace import (
    RelationshipWorkspaceService,effective_constraint_tag_ids,
)
from core.tag_collection import normalize_tag
from core.tag_order_workspace import (
    CategoryWorkspaceSession,prepare_category_structure_search_index,
)
from .gallery import IMAGE_BADGE_HELP,CompactGalleryStatusController,EmbeddedGallery
from .components import BoundedSearchableComboBox,IncrementalResultModel,IncrementalResultRow,PageHeader,bounded_window_size
from .confidence import STATUS_COLORS
from .character_tag_bridge_dialog import CharacterTagBridgeDialog
from .character_record_editor import CharacterRecordEditorDialog
from .category_workspace import CategoryWorkspace
from .default_overrides_dialog import DefaultOverridesDialog
from .modal_feedback import FeedbackEnabledDialog
from .relationship_workbench import RelationshipWorkspaceDialog
from .system_knowledge_maintenance_widget import SystemKnowledgeMaintenanceWidget
from .tag_appearance_editor import TagAppearanceEditor,TagColorWorkspaceDialog
from .tag_alias_preflight_dialog import TagAliasPreflightDialog


def _value_scope_with_exact(
    scope,value_id,*,allowed,category_path_ids=(),
):
    """Return the smallest scope change that allows or excludes one exact value."""
    values=set(scope.value_ids); exceptions=set(scope.exception_value_ids)
    value_id=str(value_id)
    if allowed:
        if scope.mode in {VALUE_SCOPE_UNREVIEWED,VALUE_SCOPE_ONLY}:
            values.add(value_id); exceptions.discard(value_id); mode=VALUE_SCOPE_ONLY
            return AllowedDimensionValueScope(
                scope.family_id,mode,tuple(sorted(values)),scope.category_ids,
                tuple(sorted(exceptions)),scope.exception_category_ids,
            )
        if scope.mode==VALUE_SCOPE_ALL_EXCEPT:
            values.discard(value_id); candidate=AllowedDimensionValueScope(
                scope.family_id,scope.mode,tuple(sorted(values)),scope.category_ids,
                tuple(sorted(exceptions)),scope.exception_category_ids,
            )
            if candidate.allows(value_id,category_path_ids):
                return candidate
            exceptions.add(value_id)
            return AllowedDimensionValueScope(
                scope.family_id,scope.mode,tuple(sorted(values)),scope.category_ids,
                tuple(sorted(exceptions)),scope.exception_category_ids,
            )
        return scope
    if scope.mode==VALUE_SCOPE_ALL:
        return AllowedDimensionValueScope(scope.family_id,VALUE_SCOPE_ALL_EXCEPT,(value_id,))
    if scope.mode==VALUE_SCOPE_ALL_EXCEPT:
        values.add(value_id); exceptions.discard(value_id)
    else:
        values.discard(value_id); candidate=AllowedDimensionValueScope(
            scope.family_id,scope.mode,tuple(sorted(values)),scope.category_ids,
            tuple(sorted(exceptions)),scope.exception_category_ids,
        )
        if not candidate.allows(value_id,category_path_ids):
            return candidate
        exceptions.add(value_id)
    return AllowedDimensionValueScope(
        scope.family_id,scope.mode,tuple(sorted(values)),scope.category_ids,
        tuple(sorted(exceptions)),scope.exception_category_ids,
    )


class CurrentPageTabWidget(QTabWidget):
    """Size a scroll-hosted tab set from the page the user can actually see."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _index:self.updateGeometry())

    def _current_page_hint(self,minimum=False):
        page=self.currentWidget()
        if page is None:return super().minimumSizeHint() if minimum else super().sizeHint()
        page_hint=page.minimumSizeHint() if minimum else page.sizeHint()
        bar_hint=self.tabBar().minimumSizeHint() if minimum else self.tabBar().sizeHint()
        return QSize(max(page_hint.width()+8,bar_hint.width()+8),page_hint.height()+bar_hint.height()+8)

    def sizeHint(self):return self._current_page_hint(False)

    def minimumSizeHint(self):return self._current_page_hint(True)

    def hasHeightForWidth(self):
        page=self.currentWidget()
        return bool(page is not None and page.hasHeightForWidth())

    def heightForWidth(self,width):
        page=self.currentWidget()
        if page is None:return super().heightForWidth(width)
        page_width=max(0,width-8)
        page_height=(
            page.heightForWidth(page_width)
            if page.hasHeightForWidth() else page.sizeHint().height()
        )
        return page_height+self.tabBar().sizeHint().height()+8


class LayoutSizedWidget(QWidget):
    """Keep a reusable panel's hint synchronized after it moves into a dialog."""

    def sizeHint(self):
        layout=self.layout()
        return layout.sizeHint() if layout is not None else super().sizeHint()

    def minimumSizeHint(self):
        layout=self.layout()
        return layout.minimumSize() if layout is not None else super().minimumSizeHint()
from .tag_editor import refresh_all_tag_appearance_chips
from .tag_identity_review import (
    RemovedVocabularyDialog,removed_identity_record,review_removed_identities,
)
from .tag_relationship_editor import ModifierSlotEditor,TagReferenceEditor,TagReferencePickerDialog,TokenChipEditor
from .workers import AsyncCallTask,CoordinatedCallTask


class FinishedEditingTextEdit(QTextEdit):
    """Report the transaction boundary for a multi-line text edit."""

    editingFinished=Signal()

    def focusOutEvent(self,event):
        super().focusOutEvent(event)
        self.editingFinished.emit()


def _csv(value: str): return [item.strip() for item in value.replace("\n",",").split(",") if item.strip()]
def _join(values): return ", ".join(values or [])
def _percent(value):
    try:return f"{float(value or 0):.0%}"
    except (TypeError,ValueError):return "unknown"


@dataclass(frozen=True)
class KnowledgeViewContext:
    query: str=""
    review_view_id: str="all"
    selected_kind: str=""
    selected_id: str=""
    membership_tag_ids: tuple[str,...]=()
    include_related: bool=False

    @classmethod
    def from_mapping(cls,value):
        if not isinstance(value,dict):return None
        kind=str(value.get("selected_kind") or "")
        if kind not in {"","category","tag"}:kind=""
        selected_id=str(value.get("selected_id") or "") if kind else ""
        members=tuple(dict.fromkeys(str(item) for item in value.get("membership_tag_ids",()) if str(item)))
        return cls(
            query=str(value.get("query") or ""),
            review_view_id=str(value.get("review_view_id") or "all"),
            selected_kind=kind,selected_id=selected_id,membership_tag_ids=members,
            include_related=bool(value.get("include_related",False)),
        )

    def as_mapping(self):
        return {
            "query":self.query,"review_view_id":self.review_view_id,
            "selected_kind":self.selected_kind,"selected_id":self.selected_id,
            "membership_tag_ids":list(self.membership_tag_ids),
            "include_related":self.include_related,
        }


def _bounded_tag_editor_size(available: QRect) -> QSize:
    """Keep the decorated Tag Details window inside the current work area."""
    return bounded_window_size(available,QSize(980,650))


class TagCategoryChangeDialog(FeedbackEnabledDialog):
    """Preview one automatically saved category transaction for stable tags."""

    feedback_workflow = "Change SDXL Tag category"

    def __init__(self,records,categories,parent=None):
        super().__init__(parent); self.records=tuple(records); self.categories=categories
        self.setWindowTitle(f"Change category for {len(self.records):,} SDXL Tags")
        layout=QVBoxLayout(self); heading=QLabel(f"{len(self.records):,} SDXL Tags selected"); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        names=[record.display for record in self.records]
        shown=", ".join(names[:8]); remainder=len(names)-8
        selected_text=shown+(f" and {remainder:,} more" if remainder>0 else "")
        selected=QLabel(f"Selected SDXL Tags: {selected_text}"); selected.setWordWrap(True); selected.setAccessibleName(f"Selected SDXL Tags: {selected_text}"); layout.addWidget(selected)
        distribution={}
        for record in self.records:distribution[record.category]=distribution.get(record.category,0)+1
        current=", ".join(f"{categories.path_label(category_id)} ({count:,})" for category_id,count in sorted(distribution.items(),key=lambda item:categories.path_label(item[0]).casefold()))
        summary=QLabel(f"Current categories: {current or 'None'}"); summary.setWordWrap(True); summary.setAccessibleName(f"Current category distribution: {current or 'None'}"); layout.addWidget(summary)
        form=QFormLayout(); self.category=BoundedSearchableComboBox(placeholder="Type any part of a category path…",max_visible=12); self.category.setAccessibleName("New category for selected SDXL Tags")
        choices=categories.choices() if hasattr(categories,"choices") else [(record.id,categories.path_label(record.id)) for record in sorted(categories.all_categories(),key=lambda item:categories.path_label(item.id).casefold())]
        for category_id,label in choices:self.category.addItem(label,category_id)
        form.addRow("New category",self.category); layout.addLayout(form)
        self.preview=QLabel(); self.preview.setWordWrap(True); self.preview.setObjectName("mutedLabel"); layout.addWidget(self.preview)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel); self.stage_button=buttons.addButton("Apply Change",QDialogButtonBox.AcceptRole); self.stage_button.setProperty("primary",True); self.stage_button.clicked.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.category.currentIndexChanged.connect(self._update_preview); self._update_preview()

    def target_category_id(self):return str(self.category.currentData() or "")

    def _update_preview(self,*_):
        target=self.target_category_id(); changed=sum(record.category!=target for record in self.records); label=self.categories.path_label(target) if target else "an unavailable category"
        if not target:self.preview.setText("Choose an existing category before applying this change.")
        elif not changed:self.preview.setText(f"All selected SDXL Tags already belong to {label}; there is no change to apply.")
        else:self.preview.setText(f"{changed:,} of {len(self.records):,} selected SDXL Tags will move to {label}. Apply Change saves one atomic Category Workspace transaction automatically; Undo saves its inverse.")
        self.stage_button.setEnabled(bool(target and changed))


class AddTagToCategoryDialog(FeedbackEnabledDialog):
    """Find existing vocabulary before offering one explicit new tag."""

    feedback_workflow = "Add SDXL Tags to category"

    CREATE_RESULT=2
    RESTORE_RESULT=3
    RESULT_LIMIT=200

    def __init__(self,records,categories,target_category_id,*,ignored_names=(),removed_tag_ids=(),knowledge=None,identity_service=None,identity_recovery_resolver=None,parent=None):
        super().__init__(parent); self.records=tuple(records); self.categories=categories; self.target_category_id=str(target_category_id); self.ignored_names={normalize_tag(value) for value in ignored_names}; self.removed_tag_ids={str(value) for value in removed_tag_ids}; self.knowledge=knowledge; self.identity_service=identity_service or (ConfirmedTagIdentityService(knowledge) if knowledge is not None else None); self.identity_recovery_resolver=identity_recovery_resolver or (lambda stable_id:removed_identity_record(knowledge,stable_id)); self._tombstone_entry=None; self._tombstone_recovery_record=None; self._restored_tag_id=""
        target_label=categories.path_label(self.target_category_id); self.setWindowTitle(f"Add SDXL Tags to {target_label}"); self.resize(920,620)
        layout=QVBoxLayout(self); heading=QLabel(f"Add SDXL Tags to {target_label}"); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        explanation=QLabel("Search all known SDXL Tags by preferred name or alias. Existing tags move here automatically; creating a new tag is offered only when the name does not already identify known or removed vocabulary.")
        explanation.setWordWrap(True); explanation.setObjectName("mutedLabel"); layout.addWidget(explanation)
        self.search=QLineEdit(); self.search.setClearButtonEnabled(True); self.search.setPlaceholderText("Find a known SDXL Tag or type a new name…"); self.search.setAccessibleName("Find an SDXL Tag to add"); self.search.setAccessibleDescription("Search preferred names and aliases across every System Knowledge category."); layout.addWidget(self.search)
        self.result_summary=QLabel(); self.result_summary.setObjectName("mutedLabel"); self.result_summary.setAccessibleName("Add Tag search results"); layout.addWidget(self.result_summary)
        self.results=QTreeWidget(); self.results.setHeaderLabels(("SDXL Tag","Other names","Current category","Status")); self.results.setRootIsDecorated(False); self.results.setSelectionMode(QAbstractItemView.ExtendedSelection); self.results.setAlternatingRowColors(True); self.results.setAccessibleName("Known SDXL Tag matches")
        self.results.header().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.results.header().setSectionResizeMode(1,QHeaderView.Stretch); self.results.header().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.results.header().setSectionResizeMode(3,QHeaderView.ResizeToContents); layout.addWidget(self.results,1)
        self.create_status=QLabel(); self.create_status.setWordWrap(True); self.create_status.setObjectName("mutedLabel"); layout.addWidget(self.create_status)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel); self.create_button=buttons.addButton("Create New Tag…",QDialogButtonBox.ActionRole); self.move_button=buttons.addButton("Move Selected Here",QDialogButtonBox.AcceptRole); self.move_button.setProperty("primary",True); self.create_button.setProperty("compact",True)
        self.create_button.clicked.connect(self._create_or_restore); self.move_button.clicked.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.search.textChanged.connect(self._refresh_results); self.results.itemSelectionChanged.connect(self._update_actions); self.results.itemDoubleClicked.connect(self._activate_result)
        self._refresh_results(); self.search.setFocus()

    def query(self):return self.search.text().strip()

    def selected_tag_ids(self):
        return tuple(dict.fromkeys(str(item.data(0,Qt.UserRole) or "") for item in self.results.selectedItems() if item.data(0,Qt.UserRole)))

    def restored_tag_id(self):return self._restored_tag_id

    def _removed_identity_id(self,query):
        base="_".join(normalize_tag(query).split()) or "tag"
        return next((value for value in self.removed_tag_ids if value==base or (value.startswith(base+"_") and value[len(base)+1:].isdigit())),None)

    def _aliases(self,record):
        return (
            self.knowledge.effective_aliases(record)
            if self.knowledge is not None else tuple(record.aliases)
        )

    def _match_rank(self,record,query):
        canonical=normalize_tag(record.display)
        aliases=[normalize_tag(value) for value in self._aliases(record)]
        if not query:return 6,canonical,record.id
        if canonical==query:return 0,canonical,record.id
        if query in aliases:return 1,canonical,record.id
        if canonical.startswith(query):return 2,canonical,record.id
        if any(value.startswith(query) for value in aliases):return 3,canonical,record.id
        if query in canonical:return 4,canonical,record.id
        return 5,canonical,record.id

    def _refresh_results(self,*_):
        query=normalize_tag(self.query()); matches=[]; exact=[]
        for record in self.records:
            aliases=self._aliases(record)
            names=[normalize_tag(record.display),*(normalize_tag(value) for value in aliases)]
            if query and not any(query in value for value in names):continue
            matches.append(record)
            if query and query in names:exact.append(record)
        matches.sort(key=lambda record:self._match_rank(record,query)); shown=matches[:self.RESULT_LIMIT]
        self.results.clear()
        for record in shown:
            aliases=self._aliases(record)
            already_here=record.category==self.target_category_id; ignored=bool({normalize_tag(record.display),*(normalize_tag(value) for value in aliases)} & self.ignored_names)
            status="Already here" if already_here else "Available"
            if ignored:status+=(" • " if status else "")+"Always ignored"
            item=QTreeWidgetItem((record.display,", ".join(aliases),self.categories.path_label(record.category),status)); item.setData(0,Qt.UserRole,record.id)
            item.setToolTip(0,f"Preferred name: {record.display}\nCurrent category: {self.categories.path_label(record.category)}"+(f"\nOther names: {', '.join(aliases)}" if aliases else ""))
            if already_here:item.setFlags(item.flags()&~Qt.ItemIsSelectable)
            self.results.addTopLevelItem(item)
        if len(matches)>len(shown):self.result_summary.setText(f"Showing {len(shown):,} of {len(matches):,} matching known SDXL Tags. Refine the search to reach additional results.")
        elif query:self.result_summary.setText(f"{len(matches):,} matching known SDXL Tag{'s' if len(matches)!=1 else ''} across all categories.")
        else:self.result_summary.setText(f"Showing {len(shown):,} of {len(matches):,} known SDXL Tags across all categories.")
        plan=(
            self.identity_service.plan((self.query(),),confirmation=True)
            if query and self.identity_service is not None else None
        )
        self._tombstone_entry=next(
            (
                entry for entry in (plan.entries if plan is not None else ())
                if entry.status=="tombstone"
            ),None,
        )
        tombstone=(
            self._tombstone_entry.stable_id
            if self._tombstone_entry is not None else
            self._removed_identity_id(query) if query else None
        )
        self._tombstone_recovery_record=(
            self.identity_recovery_resolver(tombstone) if tombstone else None
        )
        can_restore=bool(
            self._tombstone_entry is not None
            and self._tombstone_recovery_record is not None
        )
        can_create=bool(query and not exact and not tombstone)
        self.create_button.setText(
            "Review Removed Tag\u2026" if tombstone else "Create New Tag\u2026"
        )
        if exact:
            self.create_status.setText("This name already identifies known vocabulary. Select the intended existing record instead of creating a duplicate.")
        elif tombstone:
            self.create_status.setText(
                "This name matches removed System Knowledge identity "
                f'“{tombstone}”. Review and restore that same identity to continue.'
                if can_restore else
                "This name matches a removed System Knowledge identity, but its "
                "complete reviewed recovery details are unavailable. Restore a "
                "current Knowledge Pack or System Knowledge backup first."
            )
        elif query:
            self.create_status.setText("No exact preferred name or alias exists. Create New Tag opens the established editor with this name and category prefilled.")
        else:self.create_status.setText("Type a complete name to create a new SDXL Tag, or select existing records to move here.")
        self.create_button.setEnabled(can_create or can_restore); self._update_actions()

    def _create_or_restore(self):
        if self._tombstone_entry is None:
            self.done(self.CREATE_RESULT); return
        outcome=review_removed_identities(
            self,self.identity_service,
            self.identity_service.plan((self.query(),),confirmation=True),
            source="category_membership_confirmation",
            continuation=(
                "The restored tag will then continue into the selected category; "
                "no image data will be changed."
            ),
            recovery_resolver=self.identity_recovery_resolver,
        )
        if outcome.restored_ids:
            self._restored_tag_id=outcome.restored_ids[0]
            self.done(self.RESTORE_RESULT)

    def _update_actions(self):self.move_button.setEnabled(bool(self.selected_tag_ids()))

    def _activate_result(self,item,_column):
        if item.flags()&Qt.ItemIsSelectable:self.accept()


class ModifierCompatibilityBatchPreviewDialog(FeedbackEnabledDialog):
    """Bounded exact preview for one reverse-policy System Knowledge batch."""

    feedback_workflow="Review modifier compatibility batch"
    PAGE_SIZE=200

    def __init__(self,rows,*,group_label,modifier_label,parent=None):
        super().__init__(parent); rows=tuple(rows)
        self.setWindowTitle(f"Preview {group_label} compatibility changes")
        self.resize(1080,640)
        layout=QVBoxLayout(self)
        heading=QLabel(f"Exact System Knowledge batch — {modifier_label}"); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        additions=sum(row.columns[3]=="Allow exact value" for row in rows); retained=sum(row.columns[3]=="Confirm exact value" for row in rows); removals=sum(row.columns[3]=="Exclude exact value" for row in rows)
        self.summary=QLabel(
            f"{len(rows):,} affected Base Tag polic{'y' if len(rows)==1 else 'ies'}: "
            f"{additions:,} addition{'s' if additions!=1 else ''}, "
            f"{retained:,} reviewed retention{'s' if retained!=1 else ''}, and "
            f"{removals:,} removal{'s' if removals!=1 else ''}."
        ); self.summary.setWordWrap(True); self.summary.setAccessibleName("Exact modifier compatibility batch summary"); layout.addWidget(self.summary)
        self.authority=QLabel(
            f"Authority: reusable System Knowledge. Applying this preview writes one atomic, recoverable {group_label} relationship batch and its compatibility projection. "
            "It does not change Image Knowledge, TXT, Metadata, or files. You can undo the complete saved batch from Tag Details."
        ); self.authority.setWordWrap(True); self.authority.setObjectName("mutedLabel"); self.authority.setAccessibleName("Modifier compatibility save authority and recovery"); layout.addWidget(self.authority)
        self.model=IncrementalResultModel(("Base Tag","Stable ID","Category","Decision","Current value scope","Result value scope"),page_size=self.PAGE_SIZE,parent=self)
        self.tree=QTreeView(); self.tree.setModel(self.model); self.tree.setRootIsDecorated(False); self.tree.setUniformRowHeights(True); self.tree.setSelectionBehavior(QAbstractItemView.SelectRows); self.tree.setSelectionMode(QAbstractItemView.SingleSelection); self.tree.setAccessibleName("Exact staged modifier compatibility changes"); self.tree.setAccessibleDescription("Every affected stable Base Tag ID, decision, current exact-value scope, and resulting exact-value scope in this one System Knowledge transaction.")
        self.tree.header().setStretchLastSection(False); self.tree.header().setSectionResizeMode(0,QHeaderView.Stretch); self.tree.header().resizeSection(1,210); self.tree.header().resizeSection(2,145); self.tree.header().resizeSection(3,155); self.tree.header().resizeSection(4,190); self.tree.header().resizeSection(5,190); layout.addWidget(self.tree,1)
        count_row=QHBoxLayout(); self.result_count=QLabel(); self.result_count.setObjectName("mutedLabel"); self.load_more=QPushButton("Load More…"); self.load_more.setProperty("compact",True); count_row.addWidget(self.result_count,1); count_row.addWidget(self.load_more); layout.addLayout(count_row)
        self.model.counts_changed.connect(self._counts_changed); self.load_more.clicked.connect(self.model.fetch_more); self.model.set_rows(rows)
        buttons=QDialogButtonBox(); self.back_button=buttons.addButton("Back",QDialogButtonBox.RejectRole); self.apply_button=buttons.addButton("Apply Exact Batch",QDialogButtonBox.AcceptRole); self.apply_button.setProperty("primary",True); self.apply_button.setDefault(True); self.back_button.clicked.connect(self.reject); self.apply_button.clicked.connect(self.accept); layout.addWidget(buttons)

    def _counts_changed(self,shown,total):
        self.result_count.setText(f"Showing {shown:,} of {total:,} exact staged changes.")
        self.load_more.setVisible(shown<total); self.load_more.setText(f"Load {min(self.PAGE_SIZE,total-shown):,} More…" if shown<total else "Load More…")


class ModifierValueScopeDialog(FeedbackEnabledDialog):
    """Broad collection scope with dynamic branches and exact Tag overrides."""

    feedback_workflow="Review reusable value scope"
    PAGE_SIZE=200

    def __init__(
        self,record,policy,definitions,labels,tags=None,categories=None,parent=None,
    ):
        super().__init__(parent); self.record=record; self.policy=policy
        self.tags=tags; self.categories=categories
        self._branch_cache={}
        self.definitions={str(key):tuple(values) for key,values in definitions.items()}
        self.labels={str(key):str(value) for key,value in labels.items()}
        self.scopes={scope.family_id:scope for scope in policy.value_scopes}
        for group in policy.groups:self.scopes.setdefault(group,AllowedDimensionValueScope(group))
        self._allowed_values={}; self._excluded_values={}
        self._allowed_branches={}; self._excluded_branches={}
        for group,scope in self.scopes.items():self._load_semantic_sets(group,scope)
        self.setWindowTitle(f"Review Allowed Values — {record.display}"); self.resize(920,720)
        layout=QVBoxLayout(self)
        self.notice=QLabel(
            "Start broad, then narrow only where needed. Category branch choices stay dynamic as reviewed Tags are added or moved. "
            "Exact Tags override branches; the deepest selected branch wins when nested branches differ."
        ); self.notice.setWordWrap(True); self.notice.setObjectName("mutedLabel"); layout.addWidget(self.notice)
        form=QFormLayout(); self.family=QComboBox(); self.mode=QComboBox()
        for group in policy.groups:self.family.addItem(self.labels.get(group,group),group)
        for label,key in (
            ("Choose scope…",VALUE_SCOPE_UNREVIEWED),
            ("All values",VALUE_SCOPE_ALL),
            ("Only chosen branches and Tags",VALUE_SCOPE_ONLY),
            ("All except chosen branches and Tags",VALUE_SCOPE_ALL_EXCEPT),
        ):self.mode.addItem(label,key)
        form.addRow("Reusable collection",self.family); form.addRow("Starting scope",self.mode); layout.addLayout(form)
        self.tabs=QTabWidget(); self.tabs.setAccessibleName("Allowed value rule level"); self.tabs.setAccessibleDescription("Choose dynamic Category branch rules or exact Tag overrides for the selected reusable collection."); layout.addWidget(self.tabs,1)
        self._build_branch_tab(); self._build_value_tab()
        self.status=QLabel(); self.status.setWordWrap(True); self.status.setObjectName("mutedLabel"); layout.addWidget(self.status)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.save_button=buttons.button(QDialogButtonBox.Save); self.save_button.setText("Save Allowed Values"); self.save_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.family.currentIndexChanged.connect(self._family_changed); self.mode.currentIndexChanged.connect(self._mode_changed)
        self.search.textChanged.connect(self._refresh); self.branch_search.textChanged.connect(self._refresh)
        self.values.itemChanged.connect(self._value_item_changed); self.branches.itemChanged.connect(self._branch_item_changed)
        self.check_matching.clicked.connect(lambda:self._set_value_matching("primary")); self.opposite_matching.clicked.connect(lambda:self._set_value_matching("opposite")); self.clear_matching.clicked.connect(lambda:self._set_value_matching("follow"))
        self.branch_primary.clicked.connect(lambda:self._set_branch_matching("primary")); self.branch_opposite.clicked.connect(lambda:self._set_branch_matching("opposite")); self.branch_follow.clicked.connect(lambda:self._set_branch_matching("follow"))
        self._family_changed()

    def _build_branch_tab(self):
        page=QWidget(); layout=QVBoxLayout(page)
        intro=QLabel("Choose descendant Categories for fast, future-aware rules. Nested choices can narrow a broader branch."); intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        self.branch_search=QLineEdit(); self.branch_search.setClearButtonEnabled(True); self.branch_search.setPlaceholderText("Search descendant Category branches…"); self.branch_search.setAccessibleName("Search descendant Category branches"); layout.addWidget(self.branch_search)
        quick=QHBoxLayout(); self.branch_primary=QPushButton(); self.branch_opposite=QPushButton(); self.branch_follow=QPushButton("Follow Starting Scope")
        for button in (self.branch_primary,self.branch_opposite,self.branch_follow):button.setProperty("compact",True); quick.addWidget(button)
        quick.addStretch(1); layout.addLayout(quick)
        self.branch_count=QLabel(); self.branch_count.setObjectName("mutedLabel"); layout.addWidget(self.branch_count)
        self.branches=QTreeWidget(); self.branches.setHeaderLabels(("Always allow","Always exclude","Category branch","Stable ID")); self.branches.setRootIsDecorated(False); self.branches.setUniformRowHeights(True); self.branches.setAccessibleName("Dynamic descendant Category branch rules"); self.branches.setAccessibleDescription("Each complete row can follow the starting scope, always allow its branch, or always exclude its branch. The deepest matching branch wins."); self.branches.header().setSectionResizeMode(2,QHeaderView.Stretch); self.branches.header().resizeSection(0,105); self.branches.header().resizeSection(1,110); self.branches.header().resizeSection(3,210); layout.addWidget(self.branches,1)
        self.tabs.addTab(page,"Category branches")

    def _build_value_tab(self):
        page=QWidget(); layout=QVBoxLayout(page)
        intro=QLabel("Use exact overrides for individual Tags that should differ from their Category branch."); intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        self.search=QLineEdit(); self.search.setClearButtonEnabled(True); self.search.setPlaceholderText("Search Tags in this reusable collection…"); self.search.setAccessibleName("Search exact Tags in this reusable collection"); layout.addWidget(self.search)
        quick=QHBoxLayout(); self.check_matching=QPushButton(); self.opposite_matching=QPushButton(); self.clear_matching=QPushButton("Follow Category Branch")
        for button in (self.check_matching,self.opposite_matching,self.clear_matching):button.setProperty("compact",True); quick.addWidget(button)
        quick.addStretch(1); layout.addLayout(quick)
        self.count=QLabel(); self.count.setObjectName("mutedLabel"); layout.addWidget(self.count)
        self.values=QTreeWidget(); self.values.setHeaderLabels(("Always allow","Always exclude","Tag","Stable ID")); self.values.setRootIsDecorated(False); self.values.setUniformRowHeights(True); self.values.setAccessibleName("Exact Tag overrides"); self.values.setAccessibleDescription("Each complete row can follow its Category branch, always allow the Tag, or always exclude the Tag. Exact Tag decisions override branch decisions."); self.values.header().setSectionResizeMode(2,QHeaderView.Stretch); self.values.header().resizeSection(0,105); self.values.header().resizeSection(1,110); self.values.header().resizeSection(3,210); layout.addWidget(self.values,1)
        self.tabs.addTab(page,"Exact Tag overrides")

    def _load_semantic_sets(self,group,scope):
        if scope.mode==VALUE_SCOPE_ONLY:
            allowed_values,excluded_values=scope.value_ids,scope.exception_value_ids
            allowed_branches,excluded_branches=scope.category_ids,scope.exception_category_ids
        elif scope.mode==VALUE_SCOPE_ALL_EXCEPT:
            allowed_values,excluded_values=scope.exception_value_ids,scope.value_ids
            allowed_branches,excluded_branches=scope.exception_category_ids,scope.category_ids
        else:allowed_values=excluded_values=allowed_branches=excluded_branches=()
        self._allowed_values[group]=set(allowed_values); self._excluded_values[group]=set(excluded_values)
        self._allowed_branches[group]=set(allowed_branches); self._excluded_branches[group]=set(excluded_branches)

    def _group(self):return str(self.family.currentData() or "")
    def _definitions(self):return self.definitions.get(self._group(),())
    def _branches(self):
        group=self._group()
        if self.categories is None:return ()
        if group not in self._branch_cache:self._branch_cache[group]=tuple(sorted(
            (
                record for record in self.categories.categories.values()
                if record.id!=group
                and group in self.categories.path_ids(record.id)
                and self.categories.modifier_family_source(record.id)==group
            ),
            key=lambda record:(self.categories.path_label(record.id).casefold(),record.id),
        ))
        return self._branch_cache[group]
    def _scope_from_sets(self,group,mode):
        if mode==VALUE_SCOPE_ONLY:
            return AllowedDimensionValueScope(group,mode,tuple(sorted(self._allowed_values.get(group,set()))),tuple(sorted(self._allowed_branches.get(group,set()))),tuple(sorted(self._excluded_values.get(group,set()))),tuple(sorted(self._excluded_branches.get(group,set()))))
        if mode==VALUE_SCOPE_ALL_EXCEPT:
            return AllowedDimensionValueScope(group,mode,tuple(sorted(self._excluded_values.get(group,set()))),tuple(sorted(self._excluded_branches.get(group,set()))),tuple(sorted(self._allowed_values.get(group,set()))),tuple(sorted(self._allowed_branches.get(group,set()))))
        return AllowedDimensionValueScope(group,mode)
    def _store_mode(self):
        group=self._group()
        if group:self.scopes[group]=self._scope_from_sets(group,str(self.mode.currentData() or VALUE_SCOPE_UNREVIEWED))
    def _family_changed(self,*_):
        group=self._group(); scope=self.scopes.get(group,AllowedDimensionValueScope(group)); index=self.mode.findData(scope.mode)
        blocker=QSignalBlocker(self.mode); self.mode.setCurrentIndex(max(0,index)); del blocker; self._refresh()
    def _mode_changed(self,*_):self._store_mode(); self._refresh()
    def _matching(self):
        query=self.search.text().strip().casefold(); result=[]
        for value in self._definitions():
            haystack=" ".join((value.id,value.display,*getattr(value,"aliases",()))).casefold()
            if not query or query in haystack:result.append(value)
        return result
    def _matching_branches(self):
        query=self.branch_search.text().strip().casefold(); result=[]
        for record in self._branches():
            label=self.categories.path_label(record.id)
            if not query or query in f"{record.id} {label}".casefold():result.append((record,label))
        return result
    def _refresh(self,*_):
        group=self._group(); mode=str(self.mode.currentData() or VALUE_SCOPE_UNREVIEWED); restricted=mode in {VALUE_SCOPE_ONLY,VALUE_SCOPE_ALL_EXCEPT}
        allowed=self._allowed_values.setdefault(group,set()); excluded=self._excluded_values.setdefault(group,set()); matches=self._matching()
        self.values.blockSignals(True); self.values.clear()
        for value in matches[:self.PAGE_SIZE]:
            item=QTreeWidgetItem(("","",value.display,value.id)); item.setData(0,Qt.UserRole,value.id); item.setFlags(item.flags()|Qt.ItemIsUserCheckable); item.setCheckState(0,Qt.Checked if value.id in allowed else Qt.Unchecked); item.setCheckState(1,Qt.Checked if value.id in excluded else Qt.Unchecked); self.values.addTopLevelItem(item)
        self.values.blockSignals(False)
        branch_allowed=self._allowed_branches.setdefault(group,set()); branch_excluded=self._excluded_branches.setdefault(group,set()); branch_matches=self._matching_branches()
        has_branches=bool(self._branches()); self.tabs.setTabEnabled(0,has_branches)
        if not has_branches:self.tabs.setCurrentIndex(1)
        self.branches.blockSignals(True); self.branches.clear()
        for record,label in branch_matches[:self.PAGE_SIZE]:
            item=QTreeWidgetItem(("","",label,record.id)); item.setData(0,Qt.UserRole,record.id); item.setFlags(item.flags()|Qt.ItemIsUserCheckable); item.setCheckState(0,Qt.Checked if record.id in branch_allowed else Qt.Unchecked); item.setCheckState(1,Qt.Checked if record.id in branch_excluded else Qt.Unchecked); self.branches.addTopLevelItem(item)
        self.branches.blockSignals(False)
        self.values.setEnabled(restricted); self.branches.setEnabled(restricted)
        primary_label="Allow Matching" if mode==VALUE_SCOPE_ONLY else "Exclude Matching"; opposite_label="Exclude Matching" if mode==VALUE_SCOPE_ONLY else "Allow Matching"
        self.check_matching.setText(primary_label); self.opposite_matching.setText(opposite_label); self.branch_primary.setText(primary_label); self.branch_opposite.setText(opposite_label)
        for button,available in ((self.check_matching,matches),(self.opposite_matching,matches),(self.clear_matching,matches),(self.branch_primary,branch_matches),(self.branch_opposite,branch_matches),(self.branch_follow,branch_matches)):button.setEnabled(restricted and bool(available))
        self.count.setText(f"Showing {min(len(matches),self.PAGE_SIZE):,} of {len(matches):,} matching Tags; {len(allowed):,} always allowed and {len(excluded):,} always excluded.")
        self.branch_count.setText(f"Showing {min(len(branch_matches),self.PAGE_SIZE):,} of {len(branch_matches):,} matching descendant branches; {len(branch_allowed):,} always allowed and {len(branch_excluded):,} always excluded.")
        if mode==VALUE_SCOPE_UNREVIEWED:self.status.setText("Value scope needs review. Existing composed values remain preserved, but new values are unavailable until a scope is chosen.")
        elif mode==VALUE_SCOPE_ALL:self.status.setText("Every current and future reviewed Tag in this reusable collection is allowed.")
        elif mode==VALUE_SCOPE_ONLY:self.status.setText("Only chosen branches and exact Tags are allowed. Chosen branches include future collection members automatically; future collection members are not added by exact Tag choices.")
        else:self.status.setText("Every current and future reviewed Tag is allowed except chosen branches and exact Tags. Always-allow exceptions remain available.")
    @staticmethod
    def _set_state(stable_id,state,allowed,excluded):
        if state=="allow":allowed.add(stable_id); excluded.discard(stable_id)
        elif state=="exclude":excluded.add(stable_id); allowed.discard(stable_id)
        else:allowed.discard(stable_id); excluded.discard(stable_id)
    def _changed_state(self,item,column,allowed,excluded):
        stable_id=str(item.data(0,Qt.UserRole) or "")
        if column==0:
            if item.checkState(0)==Qt.Checked:self._set_state(stable_id,"allow",allowed,excluded)
            else:allowed.discard(stable_id)
        elif item.checkState(1)==Qt.Checked:self._set_state(stable_id,"exclude",allowed,excluded)
        else:excluded.discard(stable_id)
    def _value_item_changed(self,item,column):
        group=self._group(); self._changed_state(item,column,self._allowed_values.setdefault(group,set()),self._excluded_values.setdefault(group,set())); self._store_mode(); self._refresh()
    def _branch_item_changed(self,item,column):
        group=self._group(); self._changed_state(item,column,self._allowed_branches.setdefault(group,set()),self._excluded_branches.setdefault(group,set())); self._store_mode(); self._refresh()
    def _bulk_state(self,kind):
        if kind=="follow":return "follow"
        primary="allow" if str(self.mode.currentData() or "")==VALUE_SCOPE_ONLY else "exclude"
        return primary if kind=="primary" else "exclude" if primary=="allow" else "allow"
    def _set_value_matching(self,kind):
        group=self._group(); allowed=self._allowed_values.setdefault(group,set()); excluded=self._excluded_values.setdefault(group,set()); state=self._bulk_state(kind)
        for value in self._matching():self._set_state(value.id,state,allowed,excluded)
        self._store_mode(); self._refresh()
    def _set_branch_matching(self,kind):
        group=self._group(); allowed=self._allowed_branches.setdefault(group,set()); excluded=self._excluded_branches.setdefault(group,set()); state=self._bulk_state(kind)
        for record,_label in self._matching_branches():self._set_state(record.id,state,allowed,excluded)
        self._store_mode(); self._refresh()
    def policy_update(self):
        self._store_mode()
        return AllowedDimensionPolicyUpdate(tuple(self.policy.groups),tuple(self.scopes[group] for group in self.policy.groups))


class ModifierCompatibilityDialog(FeedbackEnabledDialog):
    """Bounded reverse-policy review with one deliberate batch save."""

    feedback_workflow="Review reusable modifier compatibility"
    PAGE_SIZE=200

    def __init__(self,knowledge,categories,foundation,modifier_record,modifier,*,policy_service=None,pool=None,parent=None):
        super().__init__(parent); self.knowledge=knowledge; self.categories=categories; self.foundation=foundation; self.modifier_record=modifier_record; self.modifier=modifier; self.policy_service=policy_service; self.pool=pool or QThreadPool.globalInstance()
        self._entries=(); self._entry_by_id={}; self._policy_fingerprints={}; self._original_ids=set(); self._added_ids=set(); self._removed_ids=set(); self._reviewed_ids=set(); self._load_task=None; self._loaded=False
        group_label=modifier.group.replace("_"," ").title(); self.setWindowTitle(f"Review {group_label} compatibility — {modifier_record.display}"); self.resize(1120,720)
        layout=QVBoxLayout(self)
        heading=QLabel(f'Base Tags that allow “{modifier_record.display}”'); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        notice=QLabel(
            f'“{modifier_record.display}” is a reviewed {group_label} value. This list shows Base Tags whose value scope allows this exact Tag, not every Base Tag that allows some {group_label} value. '
            "Allow or exclude this exact value in broad or narrow policies, then save all reviewed changes together. No image data changes."
        ); notice.setWordWrap(True); notice.setObjectName("mutedLabel"); layout.addWidget(notice)
        filters=QHBoxLayout(); self.search=QLineEdit(); self.search.setClearButtonEnabled(True); self.search.setPlaceholderText("Search compatible Base Tags, categories, and policy sources…"); self.search.setAccessibleName("Search compatible Base Tags")
        self.source_filter=QComboBox(); self.source_filter.setAccessibleName("Filter compatible Base Tags by policy source")
        for label,key in (("All policy sources","all"),("Needs evidence review","review"),("Canonical relationship policies","canonical"),("Inline compatibility policies","compatibility"),("Foundation defaults","foundation"),("Migrated policies","migration")):self.source_filter.addItem(label,key)
        filters.addWidget(self.search,1); filters.addWidget(self.source_filter); layout.addLayout(filters)
        count_row=QHBoxLayout(); self.result_count=QLabel("No compatible Base Tag results yet."); self.result_count.setObjectName("mutedLabel"); self.load_more=QPushButton("Load More…"); self.load_more.setProperty("compact",True); self.load_more.hide(); count_row.addWidget(self.result_count,1); count_row.addWidget(self.load_more); layout.addLayout(count_row)
        self.load_status=QLabel(); self.load_status.hide()
        from .components import bind_status_footer
        bind_status_footer(self,self.load_status,title="Base Tag policies")
        self.model=IncrementalResultModel(("SDXL Tag","Category","Policy source","Provenance","Evidence"),page_size=self.PAGE_SIZE,parent=self)
        self.results=QTreeView(); self.results.setModel(self.model); self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True); self.results.setSelectionBehavior(QAbstractItemView.SelectRows); self.results.setSelectionMode(QAbstractItemView.ExtendedSelection); self.results.setAccessibleName("Base Tag policies that allow this exact value"); self.results.setAccessibleDescription("Select one or more complete rows. Confirm and Exclude apply to the complete selection; the selected-policy panel explains one row, and Review and Save shows the exact value-scope batch."); self.results.header().setStretchLastSection(False); self.results.header().setSectionResizeMode(0,QHeaderView.Stretch); self.results.header().resizeSection(1,190); self.results.header().resizeSection(2,180); self.results.header().resizeSection(3,165); self.results.header().resizeSection(4,250); layout.addWidget(self.results,1)
        context_group=QGroupBox("Selected item"); context_layout=QVBoxLayout(context_group); self.selection_context=QLabel("Select a row to see its details. Actions on several rows apply to the entire selection."); self.selection_context.setWordWrap(True); self.selection_context.setTextInteractionFlags(self.selection_context.textInteractionFlags()|Qt.TextSelectableByMouse); self.selection_context.setAccessibleName("Selected modifier compatibility policy context"); context_layout.addWidget(self.selection_context); layout.addWidget(context_group)
        actions=QHBoxLayout(); self.add_button=QPushButton("Allow on Base Tag…"); self.retain_button=QPushButton("Confirm Exact Value"); self.remove_button=QPushButton("Exclude Value"); self.remove_matching_button=QPushButton("Exclude All Matching…"); self.undo_button=QPushButton("Undo Staged Changes")
        self.retain_button.setAccessibleName("Confirm the selected exact value as reviewed")
        self.retain_button.setToolTip("Keep this exact value allowed on the selected Base Tags and record reviewed canonical evidence when saved.")
        for button in (self.add_button,self.retain_button,self.remove_button,self.remove_matching_button,self.undo_button):button.setProperty("compact",True); button.setEnabled(False); actions.addWidget(button)
        actions.addStretch(1); layout.addLayout(actions)
        self.staged_status=QLabel("Changes remain staged until you review and save them."); self.staged_status.setWordWrap(True); self.staged_status.setObjectName("mutedLabel"); layout.addWidget(self.staged_status)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel); self.save_button=buttons.addButton("Review and Save…",QDialogButtonBox.AcceptRole); self.save_button.setProperty("primary",True); self.save_button.setEnabled(False); self.save_button.clicked.connect(self._review_staged_changes); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.search.textChanged.connect(self._refresh); self.source_filter.currentIndexChanged.connect(self._refresh); self.load_more.clicked.connect(self.model.fetch_more); self.model.counts_changed.connect(self._counts_changed); self.results.selectionModel().selectionChanged.connect(self._selection_changed)
        self.add_button.clicked.connect(self._choose_base_tag); self.retain_button.clicked.connect(self._retain_selected); self.remove_button.clicked.connect(self._remove_selected); self.remove_matching_button.clicked.connect(self._remove_all_matching); self.undo_button.clicked.connect(self._undo_staged)
        QTimer.singleShot(0,self._start_load)

    def _start_load(self):
        if self._load_task is not None or self._loaded:return
        self._set_loading(True)
        task=AsyncCallTask(
            lambda:(
                self.policy_service.base_entries(
                    group=self.modifier.group,value_id=self.modifier_record.id,
                )
                if self.policy_service is not None else
                self.foundation.modifier_policy_base_entries(
                    self.knowledge,self.categories,group=self.modifier.group,
                )
            )
        ); self._load_task=task
        task.signals.complete.connect(self._loaded_entries); task.signals.failed.connect(self._load_failed); self.pool.start(task)

    def _set_loading(self,loading):
        self.search.setEnabled(not loading); self.source_filter.setEnabled(not loading); self.results.setEnabled(not loading)
        self.load_status.setText("Loading compatible Base Tag policies…" if loading else "")
        self.load_status.setVisible(loading)

    def _loaded_entries(self,entries):
        self._load_task=None; self._entries=tuple(entries); self._entry_by_id={entry.tag_id:entry for entry in self._entries}; self._policy_fingerprints={entry.tag_id:entry.policy_fingerprint for entry in self._entries if getattr(entry,"policy_fingerprint","")}; self._original_ids=set(self._entry_by_id); self._loaded=True; self._set_loading(False); self._refresh()

    def _load_failed(self,message):
        self._load_task=None; self._set_loading(False)
        self.load_status.setText(f"Compatibility could not be loaded: {message}"); self.load_status.show()
        self.load_status.setObjectName("errorLabel"); self.load_status.style().unpolish(self.load_status); self.load_status.style().polish(self.load_status)

    def _entry_source_key(self,entry):
        if entry.is_migration:return "migration"
        if getattr(entry,"authority","")=="canonical_user" or getattr(entry,"authority","")=="canonical_default":return "canonical"
        if getattr(entry,"authority","")=="compatibility":return "compatibility"
        return entry.source_kind

    def _matches_source_filter(self,entry,source,stable_id):
        if source=="all":return True
        if source=="review":return bool(entry.needs_evidence_review and stable_id not in self._reviewed_ids)
        if source=="migration":return bool(entry.is_migration)
        return self._entry_source_key(entry)==source

    def _provenance_label(self,entry):
        if entry.is_migration:return "Migrated — review source"
        if entry.needs_source_review:return "Source not recorded"
        if entry.reviewed_at:return f"Reviewed {entry.reviewed_at}"
        return entry.provenance_source.replace("_"," ").strip().title() or "Shipped with CollectiGen"

    def _evidence(self,entry,stable_id):
        if stable_id in self._added_ids:return "Will allow exact value","The displayed value will be allowed by reviewed canonical authority."
        if stable_id in self._reviewed_ids:return "Will confirm exact value","The displayed value will remain allowed with reviewed canonical authority."
        return entry.evidence_label,entry.evidence_reason

    def _active_ids(self):return (self._original_ids-self._removed_ids)|self._added_ids

    def _matching_rows(self):
        query=self.search.text().strip().casefold(); source=str(self.source_filter.currentData() or "all"); rows=[]
        for stable_id in self._active_ids():
            entry=self._entry_by_id.get(stable_id); record=self.knowledge.tags.get(stable_id)
            if record is None:continue
            category=self.categories.path_label(record.category) if self.categories is not None else record.category
            if entry is None:
                source_label="Staged explicit policy"; provenance="Will be reviewed on save"; evidence,evidence_reason=self._evidence(entry,stable_id)
            else:
                if not self._matches_source_filter(entry,source,stable_id):continue
                source_label=entry.source_label; provenance=self._provenance_label(entry); evidence,evidence_reason=self._evidence(entry,stable_id)
            if entry is None and source!="all":continue
            haystack=" ".join((
                record.id,*self.knowledge.identity_names(record),category,
                source_label,provenance,evidence,evidence_reason,
            )).casefold()
            if query and query not in haystack:continue
            tooltip=f"Stable ID: {record.id}\nCategory: {category}\nPolicy source: {source_label}\nProvenance: {provenance}\nEvidence: {evidence}\nReason: {evidence_reason}"
            rows.append(IncrementalResultRow(record.id,(record.display,category,source_label,provenance,evidence),tooltip,f"{record.display}; {category}; {source_label}; {provenance}; {evidence}; {evidence_reason}"))
        return sorted(rows,key=lambda row:(row.columns[0].casefold(),row.stable_id))

    def _refresh(self,*_):
        if not self._loaded:return
        rows=self._matching_rows(); self.model.set_rows(rows); self._counts_changed(self.model.loaded_count,self.model.total_count); self._update_actions()

    def _counts_changed(self,shown,total):
        if not self._loaded:return
        self.result_count.setText(f"Showing {shown:,} of {total:,} matching compatible Base Tags; {len(self._active_ids()):,} total compatible.")
        self.load_more.setVisible(shown<total); self.load_more.setText(f"Load {min(self.PAGE_SIZE,total-shown):,} More…" if shown<total else "Load More…")

    def _selection_changed(self,*_):self._update_actions()

    def _selected_ids(self):
        selection=self.results.selectionModel(); return tuple(dict.fromkeys(self.model.stable_id(index) for index in selection.selectedRows(0) if self.model.stable_id(index))) if selection is not None else ()

    def _group_id(self):return self.foundation.canonical_modifier_group(self.modifier.group,self.categories)

    def _group_text(self,group):
        stable_id=self.foundation.canonical_modifier_group(str(group),self.categories) or str(group)
        label=self.foundation.modifier_group_label(stable_id,self.categories)
        return f"{label} ({stable_id})"

    def _groups_for_id(self,stable_id):
        entry=self._entry_by_id.get(stable_id)
        if entry is not None:return tuple(entry.groups)
        if self.policy_service is not None:
            resolution=self.policy_service.effective_for_tag(stable_id)
        else:
            resolution=self.foundation.effective_modifier_policy(stable_id,self.knowledge,self.categories)
        return tuple(resolution.groups) if not getattr(resolution,"blocked_reason","") else ()

    def _groups_text(self,groups):
        ordered=self.foundation.ordered_modifier_groups(groups,self.categories)
        return ", ".join(self._group_text(group) for group in ordered) or "No allowed families"

    def _scope_for_id(self,stable_id):
        group_id=self._group_id()
        if self.policy_service is None:return AllowedDimensionValueScope(group_id)
        resolution=self.policy_service.effective_for_tag(stable_id)
        return resolution.scope_for(group_id) if not resolution.blocked_reason else AllowedDimensionValueScope(group_id)

    def _scope_text(self,scope):
        value=self.modifier_record.display; selected=len(scope.value_ids)+len(scope.category_ids)
        opposing=len(scope.exception_value_ids)+len(scope.exception_category_ids)
        path=(
            self.categories.path_ids(self.modifier_record.category)
            if self.categories is not None else ()
        )
        exact=scope.allows(self.modifier_record.id,path)
        if scope.mode==VALUE_SCOPE_ALL:return f"All values; allows {value}"
        override_kind=(
            "always-exclude" if scope.mode==VALUE_SCOPE_ONLY else "always-allow"
        )
        override=f", {opposing:,} {override_kind} override{'s' if opposing!=1 else ''}" if opposing else ""
        if scope.mode==VALUE_SCOPE_ONLY:return f"Only {selected:,} chosen branch/Tag rule{'s' if selected!=1 else ''}{override}; {'allows' if exact else 'excludes'} {value}"
        if scope.mode==VALUE_SCOPE_ALL_EXCEPT:return f"All except {selected:,} chosen branch/Tag rule{'s' if selected!=1 else ''}{override}; {'allows' if exact else 'excludes'} {value}"
        return f"Needs review; excludes new use of {value}"

    def _result_scope_for_id(self,stable_id):
        current=self._scope_for_id(stable_id)
        path=(
            self.categories.path_ids(self.modifier_record.category)
            if self.categories is not None else ()
        )
        if stable_id in self._added_ids or stable_id in self._reviewed_ids:
            return _value_scope_with_exact(
                current,self.modifier_record.id,allowed=True,
                category_path_ids=path,
            )
        if stable_id in self._removed_ids:
            return _value_scope_with_exact(
                current,self.modifier_record.id,allowed=False,
                category_path_ids=path,
            )
        return current

    def _planned_outcome(self,stable_id):
        if stable_id in self._added_ids:return f"Allow exact value {self.modifier_record.display!r} for this Base Tag."
        if stable_id in self._reviewed_ids:return f"Confirm exact value {self.modifier_record.display!r} for this Base Tag."
        if stable_id in self._removed_ids:return f"Exclude exact value {self.modifier_record.display!r} while preserving the family and other values."
        return "No staged change."

    def _update_selection_context(self,selected):
        if not selected:
            self.selection_context.setText("Select a row to see its details. Actions on several rows apply to the entire selection.")
            return
        if len(selected)>1:
            reviewable=sum(bool((entry:=self._entry_by_id.get(stable_id)) is not None and entry.needs_evidence_review and stable_id not in self._reviewed_ids) for stable_id in selected)
            self.selection_context.setText(
                f"Selected: {len(selected):,} complete Base Tag policies. Confirm Exact Value is available only when all {len(selected):,} selected rows need exact-value evidence review; {reviewable:,} currently do. "
                "Exclude Value also applies to every selected row. Review and Save lists every affected stable ID and resulting value scope before anything is written."
            ); return
        stable_id=selected[0]; record=self.knowledge.tags.get(stable_id); entry=self._entry_by_id.get(stable_id)
        if record is None:return
        category=self.categories.path_label(record.category) if self.categories is not None else record.category
        source=entry.source_label if entry is not None else "Staged explicit policy"
        provenance=self._provenance_label(entry) if entry is not None else "Will be reviewed on save"
        evidence_label=entry.evidence_label if entry is not None else "New exact-value authority"
        evidence_reason=entry.evidence_reason if entry is not None else "This Base Tag does not currently allow the displayed family."
        self.selection_context.setText(
            f"Base Tag: {record.display} ({record.id})\nCategory: {category or 'Uncategorized'}\nExact value under review: {self.modifier_record.display} in {self._group_text(self._group_id())}\n"
            f"Current value scope: {self._scope_text(self._scope_for_id(stable_id))}\nCurrent allowed collections: {self._groups_text(self._groups_for_id(stable_id))}\nAuthority: {source}; provenance: {provenance}\n"
            f"Evidence: {evidence_label}. {evidence_reason}\nStaged outcome: {self._planned_outcome(stable_id)}"
        )

    def _update_actions(self):
        changed=bool(self._added_ids or self._removed_ids or self._reviewed_ids); scoped=bool(self.search.text().strip() or str(self.source_filter.currentData() or "all")!="all"); selected=self._selected_ids()
        retainable=bool(selected) and all(stable_id not in self._removed_ids and stable_id not in self._reviewed_ids and (entry:=self._entry_by_id.get(stable_id)) is not None and entry.needs_evidence_review for stable_id in selected)
        self.retain_button.setToolTip("Keep this exact value allowed on the selected Base Tags and record reviewed canonical evidence when saved." if retainable or not selected else "Select only Needs evidence review rows to confirm the complete selection as reviewed.")
        self.add_button.setEnabled(self._loaded); self.retain_button.setEnabled(retainable); self.remove_button.setEnabled(bool(selected)); self.remove_matching_button.setEnabled(bool(self._loaded and scoped and self.model.total_count)); self.undo_button.setEnabled(changed); self.save_button.setEnabled(changed)
        additions=len(self._added_ids); retained=len(self._reviewed_ids); removals=len(self._removed_ids)
        self.staged_status.setText(f"Staged: {additions:,} allowance{'s' if additions!=1 else ''}, {retained:,} reviewed confirmation{'s' if retained!=1 else ''}, {removals:,} exclusion{'s' if removals!=1 else ''}. Review and Save shows the complete change before saving." if changed else "Changes remain staged until you review and save them.")
        self._update_selection_context(selected)

    def stage_added_tag_ids(self,values):
        for stable_id in values:
            stable_id=str(stable_id)
            if stable_id and stable_id!=self.modifier_record.id and stable_id in self.knowledge.tags:
                if self.policy_service is not None and stable_id not in self._policy_fingerprints:
                    resolution=self.policy_service.effective_for_tag(stable_id)
                    if resolution.blocked_reason:
                        continue
                    self._policy_fingerprints[stable_id]=resolution.record_fingerprint
                self._removed_ids.discard(stable_id)
                if stable_id not in self._original_ids:self._added_ids.add(stable_id)
        self._refresh()

    def stage_reviewed_tag_ids(self,values):
        for stable_id in values:
            stable_id=str(stable_id); entry=self._entry_by_id.get(stable_id)
            if stable_id in self._original_ids and entry is not None and entry.needs_evidence_review:
                self._removed_ids.discard(stable_id); self._reviewed_ids.add(stable_id)
        self._refresh()

    def stage_removed_tag_ids(self,values):
        for stable_id in values:
            stable_id=str(stable_id); self._added_ids.discard(stable_id); self._reviewed_ids.discard(stable_id)
            if stable_id in self._original_ids:self._removed_ids.add(stable_id)
        self._refresh()

    def _choose_base_tag(self):
        group_label=self.modifier.group.replace("_"," ").title()
        dialog=TagReferencePickerDialog(
            self.knowledge,self.categories,
            title=f"Add Base Tag that allows {group_label}",
            exclude_ids={self.modifier_record.id,*self._active_ids()},
            allow_identity_creation=True,
            identity_source="modifier_compatibility_policy",
            identity_create_label="Create New Tag…",
            identity_consequence=(
                f"The {group_label} policy remains staged until Review and Save; "
                "no Image Knowledge, TXT, Metadata, or image files change."
            ),
            parent=self,
        )
        if dialog.exec()==QDialog.Accepted:self.stage_added_tag_ids((dialog.selected_tag_id(),))

    def _remove_selected(self):self.stage_removed_tag_ids(self._selected_ids())
    def _retain_selected(self):self.stage_reviewed_tag_ids(self._selected_ids())

    def _remove_all_matching(self):
        ids=tuple(row.stable_id for row in self._matching_rows())
        if not ids:return
        if QMessageBox.question(self,"Stage matching removals",f"Stage removal of {len(ids):,} matching Base Tag policies? Nothing is saved until the exact Review and Save preview is confirmed.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes:self.stage_removed_tag_ids(ids)

    def staged_preview_rows(self):
        rows=[]
        for stable_id in self._added_ids|self._reviewed_ids|self._removed_ids:
            record=self.knowledge.tags.get(stable_id)
            if record is None:continue
            current=self._scope_for_id(stable_id); result=self._result_scope_for_id(stable_id)
            if stable_id in self._added_ids:
                decision="Allow exact value"
            elif stable_id in self._reviewed_ids:
                decision="Confirm exact value"
            else:
                decision="Exclude exact value"
            category=self.categories.path_label(record.category) if self.categories is not None else record.category
            current_text=self._scope_text(current); result_text=self._scope_text(result)
            tooltip=f"Stable ID: {record.id}\nDecision: {decision}\nCurrent value scope: {current_text}\nResult value scope: {result_text}\nAuthority: reusable System Knowledge"
            rows.append(IncrementalResultRow(record.id,(record.display,record.id,category,decision,current_text,result_text),tooltip,f"{record.display}; stable ID {record.id}; {category}; {decision}; current {current_text}; result {result_text}; reusable System Knowledge"))
        order={"Allow exact value":0,"Confirm exact value":1,"Exclude exact value":2}
        return tuple(sorted(rows,key=lambda row:(order.get(row.columns[3],9),row.columns[0].casefold(),row.stable_id)))

    def _review_staged_changes(self):
        expected_ids=self._added_ids|self._reviewed_ids|self._removed_ids
        rows=self.staged_preview_rows()
        if {row.stable_id for row in rows}!=expected_ids:
            QMessageBox.warning(
                self,"Compatibility review changed",
                "The exact staged Base Tag set is no longer available, so no partial preview or save was allowed. Close and reopen this review to use current System Knowledge.",
            ); return
        if not rows:return
        dialog=ModifierCompatibilityBatchPreviewDialog(
            rows,group_label=self.foundation.modifier_group_label(self._group_id(),self.categories),
            modifier_label=self.modifier_record.display,parent=self,
        )
        if dialog.exec()==QDialog.Accepted:self.accept()

    def _undo_staged(self):self._added_ids.clear(); self._removed_ids.clear(); self._reviewed_ids.clear(); self._refresh()
    def original_ids(self):return tuple(sorted(self._original_ids))
    def requested_ids(self):return tuple(sorted(self._active_ids()))
    def reviewed_ids(self):return tuple(sorted(self._reviewed_ids))
    def policy_fingerprints(self):return dict(self._policy_fingerprints)

    def reject(self):
        if self._load_task is not None and hasattr(self._load_task,"cancel"):self._load_task.cancel()
        super().reject()


@dataclass
class KnowledgePagePreparation:
    """Core-only first-open state prepared outside the Qt interface thread."""

    tags: Any
    categories: Any
    characters: Any
    patterns: Any
    foundation: Any
    category_session: Any
    modifier_roles: dict
    modifier_role_error: str
    modifier_policy_service: Any
    modifier_policy_error: str
    modifier_composition_service: Any
    modifier_compositions: dict
    modifier_composition_error: str
    tag_semantic_service: Any
    tag_semantic_error: str
    tag_alias_service: Any
    tag_alias_error: str
    sensitivity_proposals: Any
    identity_group_cache: dict
    review_view_sets: dict
    category_structure_search_index: tuple
    source_fingerprint: str
    relationship_workspace: Any = None
    editor_context: Any = None

    def is_current(self,state: Any) -> bool:
        return (
            self.tags is state.knowledge
            and self.source_fingerprint==_knowledge_preparation_fingerprint(
                state.knowledge, self.editor_context,
            )
        )


def _original_editor_context():
    return KnowledgeEditorContext(DEFAULTS_DIR,USER_DATA_DIR,CATEGORIES_PATH,
                                  PATTERNS_PATH,RELATIONSHIPS_PATH,CONSUMER_POLICIES_PATH,app_root=APP_ROOT)


def _knowledge_preparation_fingerprint(tags, context=None) -> str:
    """Bind a prepared page to exact local System Knowledge authority bytes."""

    context=context or _original_editor_context()
    digest=hashlib.sha256()
    for path in (tags.path,context.categories,context.patterns,context.relationships,
                 context.defaults/"categories.json",context.defaults/"patterns.json",
                 context.defaults/"modifiers.json",context.defaults/"concepts.json",
                 context.defaults/"relationships.json",context.consumer_policies):
        path=Path(path)
        if not path.exists():
            digest.update(b"missing\0")
            continue
        digest.update(b"present\0")
        with path.open("rb") as handle:
            for block in iter(lambda:handle.read(1024*1024),b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _knowledge_review_view_sets(
    session, tags, sensitivity_proposals, *, job=None,
) -> dict[str, set[str] | None]:
    """Build first-open review queues without monopolizing the UI interpreter."""

    records=tuple(session._state.tags.values())
    resolver=session
    derived_anchors=resolver.exact_category_anchor_ids()
    sensitivity_ids=set()
    batch_size=128
    for start in range(0,len(records),batch_size):
        if job is not None and not job.checkpoint():
            raise InterruptedError("Tag Encyclopedia preparation cancelled")
        sensitivity=sensitivity_health(
            records[start:start+batch_size],sensitivity_proposals,
        )
        sensitivity_ids.update(
            issue.tag_id
            for issue in (
                *sensitivity.obvious_unclassified,
                *sensitivity.conflicting,
                *sensitivity.low_confidence,
            )
        )
        time.sleep(0.001)
    needs_organization=set()
    missing_relationships=set()
    for index,record in enumerate(records):
        if index and index%batch_size==0:
            if job is not None and not job.checkpoint():
                raise InterruptedError("Tag Encyclopedia preparation cancelled")
            time.sleep(0.001)
        if record.category=="uncategorized":
            needs_organization.add(record.id)
        category_base=str(resolver.effective_base_tag(
            record.category,derived_anchors=derived_anchors,
        )[0] or "")
        has_base=bool(tags.effective_parent_id(record) or category_base)
        if (
            record.category!="uncategorized"
            and not has_base
            and not record.concept_id
            and not record.properties
        ):
            missing_relationships.add(record.id)
    return {
        "all":None,
        "needs_organization":needs_organization,
        "sensitivity":sensitivity_ids,
        "missing_relationships":missing_relationships,
    }


def _knowledge_relationship_services(tags,categories,foundation,context=None):
    context=context or _original_editor_context()
    workspace=getattr(tags,"_app_relationship_workspace",None)
    try:
        if workspace is None:
            workspace=RelationshipWorkspaceService(
                context.defaults/"relationships.json",context.relationships,
                tags=tags,
            )
        modifier_roles=modifier_role_resolutions(
            workspace.effective,categories=categories,
        )
        modifier_role_error=""
    except (OSError,RuntimeError,ValueError) as error:
        modifier_role_error=str(error)
        inventory=foundation.legacy_modifier_inventory(tags,categories)
        modifier_roles={
            candidate.tag_id:SimpleNamespace(is_ordinary=True,family_id="")
            for candidate in inventory.candidates
        }

    try:
        modifier_policy_service=AllowedDimensionPolicyService(
            tags,categories,foundation,
            default_path=context.defaults/"relationships.json",
            user_path=context.relationships,
            workspace=workspace,
            publish_shared_authority=False,
        )
        modifier_policy_error=""
    except (OSError,RuntimeError,ValueError) as error:
        modifier_policy_service=None; modifier_policy_error=str(error)

    if modifier_role_error:
        modifier_composition_service=None
        modifier_compositions={}
        modifier_composition_error=(
            "Modifier Role authority is unavailable, so modifier composition "
            "cannot be edited safely."
        )
    else:
        try:
            modifier_composition_service=ModifierCompositionService(
                tags,categories,foundation,
                modifier_roles=modifier_roles,
                default_path=context.defaults/"relationships.json",
                user_path=context.relationships,
                workspace=workspace,
                publish_alias_authority=False,
            )
            modifier_compositions=modifier_composition_service.resolutions()
            modifier_composition_error=""
        except (OSError,RuntimeError,ValueError) as error:
            modifier_composition_service=None
            modifier_compositions={}
            modifier_composition_error=str(error)

    try:
        tag_semantic_service=getattr(tags,"_app_tag_semantic_service",None)
        if tag_semantic_service is None:
            tag_semantic_service=TagSemanticRelationshipService(
                tags,
                default_path=context.defaults/"relationships.json",
                user_path=context.relationships,
                workspace=workspace,
            )
        tag_semantic_error=""
    except (OSError,RuntimeError,ValueError) as error:
        tag_semantic_service=None; tag_semantic_error=str(error)

    try:
        tag_alias_service=getattr(tags,"_app_tag_alias_service",None)
        if tag_alias_service is None:
            tag_alias_service=TagAliasRelationshipService(
                tags,
                default_path=context.defaults/"relationships.json",
                user_path=context.relationships,
                workspace=workspace,
            )
        tag_alias_error=""
    except (OSError,RuntimeError,ValueError) as error:
        tag_alias_service=None; tag_alias_error=str(error)

    return {
        "relationship_workspace":workspace,
        "modifier_roles":modifier_roles,
        "modifier_role_error":modifier_role_error,
        "modifier_policy_service":modifier_policy_service,
        "modifier_policy_error":modifier_policy_error,
        "modifier_composition_service":modifier_composition_service,
        "modifier_compositions":modifier_compositions,
        "modifier_composition_error":modifier_composition_error,
        "tag_semantic_service":tag_semantic_service,
        "tag_semantic_error":tag_semantic_error,
        "tag_alias_service":tag_alias_service,
        "tag_alias_error":tag_alias_error,
    }


def prepare_knowledge_page(state: Any,job=None,*,editor_context=None,category_session=None) -> KnowledgePagePreparation:
    """Prepare bounded System Knowledge services without constructing Qt widgets."""

    context=editor_context or _original_editor_context()

    def progress(current,message):
        if job is not None:
            if not job.checkpoint():
                raise InterruptedError("Tag Encyclopedia preparation cancelled")
            job.update(current,6,message)

    progress(0,"Loading System Knowledge indexes")
    tags=state.knowledge
    source_fingerprint=_knowledge_preparation_fingerprint(tags,context)
    categories=(category_session.live_categories if category_session is not None else
                CategoryRegistry(context.defaults/"categories.json",context.categories))
    tags.apply_category_ordering(categories)
    characters=(category_session.characters if category_session is not None else
                PreparedCharacterDatabase(context.user) if context.standalone
                else CharacterDatabase(context.user))
    patterns=(category_session.live_patterns if category_session is not None else
              PatternRegistry(context.defaults/"patterns.json",context.patterns))
    foundation=category_session._modifier_foundation if category_session is not None else FoundationKnowledge(
        context.defaults/"modifiers.json",context.defaults/"concepts.json",
    )
    progress(1,"Resolving reviewed relationship authority")
    services=_knowledge_relationship_services(tags,categories,foundation,context)
    progress(3,"Preparing the editable Category Workspace")
    category_session=category_session or CategoryWorkspaceSession(
        tags,categories,patterns,characters,
        publish_live_authority=False,
    )
    category_structure_search_index=prepare_category_structure_search_index(
        category_session,tags,
    )
    progress(4,"Preparing sensitivity review queues")
    sensitivity_proposals=reviewed_sensitivity_proposals((
        context.defaults/"foundation_tags.json",
        context.defaults/"community_tags.json",
        context.defaults/"promoted_tags.json",
    ))
    identity_group_cache={}
    batch_size=128
    for index,record in enumerate(tags.tags.values()):
        if index and index%batch_size==0:
            if job is not None and not job.checkpoint():
                raise InterruptedError("Tag Encyclopedia preparation cancelled")
            time.sleep(0.001)
        identity_group_cache[record.id]=(
            (record.display,tuple(record.aliases)),
            frozenset(
                normalize_tag(value)
                for value in tags.identity_names(record)
                if normalize_tag(value)
            ),
        )
    review_view_sets=_knowledge_review_view_sets(
        category_session,tags,sensitivity_proposals,job=job,
    )
    if source_fingerprint!=_knowledge_preparation_fingerprint(tags,context):
        raise RuntimeError(
            "System Knowledge changed while Tag Encyclopedia was preparing. Retry to use the current saved state."
        )
    progress(5,"System Knowledge prepared; building the workspace")
    return KnowledgePagePreparation(
        tags=tags,categories=categories,characters=characters,editor_context=context,
        patterns=patterns,foundation=foundation,
        category_session=category_session,
        sensitivity_proposals=sensitivity_proposals,
        identity_group_cache=identity_group_cache,
        review_view_sets=review_view_sets,
        category_structure_search_index=category_structure_search_index,
        source_fingerprint=source_fingerprint,
        **services,
    )


class _TagDetailsDialog(QDialog):
    def __init__(self,parent,page):
        super().__init__(parent)
        self._knowledge_page=weakref.ref(page)

    def reject(self):
        page=self._knowledge_page()
        if page is not None and page._tag_save_task is not None:return
        request=getattr(self,"request_close",None)
        if request is not None and not getattr(self,"_handling_close",False):
            self._handling_close=True
            try:request()
            finally:self._handling_close=False
            return
        super().reject()

    def closeEvent(self,event):
        page=self._knowledge_page()
        if page is not None and page._tag_save_task is not None:
            event.ignore()
            return
        if getattr(self,"request_close",None) is not None:
            event.ignore(); self.reject(); return
        super().closeEvent(event)


class KnowledgePage(QWidget):
    TAG_PAGE_SIZE=200
    TAG_AUTOSAVE_DELAY_MS=500
    data_changed=Signal(); scoped_data_changed=Signal(object); show_dataset_tag=Signal(str)
    edit_image_data=Signal(str); force_ai_requested=Signal(object,str)
    open_automation=Signal(str,object); open_dataset=Signal(str,object)
    content_ratings_requested=Signal(object)
    content_sensitivity_changed=Signal(object)
    downstream_updates_changed=Signal()
    downstream_change_classified=Signal(object)
    knowledge_pack_import_requested=Signal()
    background_work_finished=Signal()
    maintenance_applied=Signal()
    find_new_knowledge_requested=Signal()
    review_meaning_proposals_requested=Signal(str)
    review_broader_proposals_requested=Signal(str)
    suggest_generation_setup_requested=Signal(str,str)

    def __init__(
        self,state: Any,parent=None,*,ui_settings=None,
        preparation: KnowledgePagePreparation | None=None,
        editor_context=None, pool=None, tag_save_runner=None, tag_save_adopted=None,
        external_category_search=True,
        native_operation_runner=None, native_operation_adopted=None,
        character_initialization_review=None,
        character_editor_factory=None,
    ):
        self.editor_context=editor_context or _original_editor_context()
        if self.editor_context.standalone and preparation is None:
            raise ValueError("Standalone Tag Details requires explicitly prepared native authority.")
        if self.editor_context.standalone and (
            preparation.editor_context!=self.editor_context or pool is None
            or tag_save_runner is None or tag_save_adopted is None
            or native_operation_runner is None or native_operation_adopted is None
            or character_initialization_review is None or character_editor_factory is None
        ):
            raise ValueError("Standalone Tag Details requires its exact prepared context, owned pool and selected save/adoption owner.")
        if preparation is not None and preparation.editor_context not in (None,self.editor_context):
            raise ValueError("Prepared editor paths do not match this host.")
        self._tag_save_runner=tag_save_runner or save_tag_details
        self._tag_save_adopted=tag_save_adopted
        self._external_category_search=external_category_search
        self._native_operation_runner=native_operation_runner
        self._native_operation_adopted=native_operation_adopted
        self._character_initialization_review=character_initialization_review
        self._character_editor_factory=character_editor_factory or CharacterRecordEditorDialog
        super().__init__(parent); self.state=state; self.tags=state.knowledge
        self.settings=getattr(state,"settings",None)
        self.downstream_updates=DownstreamUpdateStore(
            self.editor_context.user/"downstream_updates.json"
        )
        self._ui_settings=ui_settings; self._saved_views=self._load_saved_views()
        self._navigation_history=self._load_navigation_history(); self._navigation_index=len(self._navigation_history)-1
        self._restoring_context=True
        self._policy_search_service=None; self._policy_search_error=""
        self._consumer_policy_resolver=None; self._consumer_policy_error=""
        self._modifier_roles={}; self._modifier_role_error=""
        self._modifier_policy_service=None; self._modifier_policy_error=""
        self._loaded_modifier_policy_fingerprint=""
        self._modifier_composition_service=None
        self._modifier_compositions={}; self._modifier_composition_error=""
        self._loaded_modifier_composition_fingerprints={}
        self._tag_semantic_service=None; self._tag_semantic_error=""
        self._tag_alias_service=None; self._tag_alias_error=""
        if preparation is None:
            self.categories=CategoryRegistry(
                self.editor_context.defaults/"categories.json",self.editor_context.categories,
            )
            self.tags.apply_category_ordering(self.categories)
            CharacterTagBridge.recover_interrupted(self.editor_context.user)
            CharacterRecordEditService.recover_interrupted(self.editor_context.user)
            self.characters=CharacterDatabase(self.editor_context.user)
            self.patterns=PatternRegistry(
                self.editor_context.defaults/"patterns.json",self.editor_context.patterns,
            )
            self.foundation=FoundationKnowledge(
                self.editor_context.defaults/"modifiers.json",self.editor_context.defaults/"concepts.json",
            )
            self._reload_modifier_roles()
            self.sensitivity_proposals=reviewed_sensitivity_proposals((
                self.editor_context.defaults/"foundation_tags.json",
                self.editor_context.defaults/"community_tags.json",
                self.editor_context.defaults/"promoted_tags.json",
            ))
            self._prepared_category_session=None
            self._prepared_identity_group_cache=None
            self._prepared_review_view_sets=None
            self._prepared_category_structure_search_index=None
        else:
            if preparation.tags is not self.tags:
                raise ValueError(
                    "Prepared Tag Encyclopedia state no longer matches active System Knowledge."
                )
            self.categories=preparation.categories
            self.characters=preparation.characters
            self.patterns=preparation.patterns
            self.foundation=preparation.foundation
            self._relationship_workspace=preparation.relationship_workspace
            self._modifier_roles=preparation.modifier_roles
            self._modifier_role_error=preparation.modifier_role_error
            self._modifier_policy_service=preparation.modifier_policy_service
            self._modifier_policy_error=preparation.modifier_policy_error
            self._modifier_composition_service=(
                preparation.modifier_composition_service
            )
            self._modifier_compositions=preparation.modifier_compositions
            self._modifier_composition_error=(
                preparation.modifier_composition_error
            )
            self._tag_semantic_service=preparation.tag_semantic_service
            self._tag_semantic_error=preparation.tag_semantic_error
            self._tag_alias_service=preparation.tag_alias_service
            self._tag_alias_error=preparation.tag_alias_error
            self.sensitivity_proposals=preparation.sensitivity_proposals
            self._prepared_identity_group_cache=preparation.identity_group_cache
            self._prepared_category_session=preparation.category_session
            self._prepared_review_view_sets=preparation.review_view_sets
            self._prepared_category_structure_search_index=(
                preparation.category_structure_search_index
            )
        self.current_tag_id=""
        self._tag_editor_record_lock_id=""
        self._loading_tag_editor=False; self._tag_save_pending=False; self._tag_save_task=None; self._tag_close_after_save=None; self._modifier_policy_save_task=None; self._last_modifier_policy_batch=None
        self._pending_tag_conflict_handoff=None
        self._tag_gallery_generation=0; self._tag_gallery_task=None
        self._tag_view_cache=None; self.pool=pool if pool is not None else QThreadPool.globalInstance()
        self._background_jobs=getattr(self.state,"jobs",None)
        self._evidence_queries=getattr(self.state,"evidence_queries",None) or EvidenceQueryService()
        settings=getattr(self.state,"settings",None)
        workflow_states=ImageWorkflowStateService(
            self.tags,self.characters,
            ignored_plain_language=getattr(settings,"ignored_plain_language_sentences",()),categories=self.categories,
        )
        self.gallery_status=CompactGalleryStatusController(workflow_states.assess,pool=self.pool,parent=self)
        self._image_data_event_callback=self._image_data_changed
        self.state.events.subscribe(
            "image_data_changed",self._image_data_event_callback,
        )
        self._tag_search_timer=QTimer(self); self._tag_search_timer.setSingleShot(True); self._tag_search_timer.setInterval(160)
        self._tag_search_timer.timeout.connect(self.refresh_tags)
        self._tag_save_timer=QTimer(self); self._tag_save_timer.setSingleShot(True); self._tag_save_timer.setInterval(self.TAG_AUTOSAVE_DELAY_MS)
        self._tag_save_timer.timeout.connect(self._autosave_tag)
        self._context_timer=QTimer(self); self._context_timer.setSingleShot(True); self._context_timer.setInterval(350)
        self._context_timer.timeout.connect(self._record_navigation_context)
        self._build()
        QTimer.singleShot(0,self._finish_initialization)

    def _build(self):
        root=QVBoxLayout(self); root.setContentsMargins(24,20,24,20); root.setSpacing(10)
        self.header=PageHeader(
            "Tag Encyclopedia","Reusable vocabulary for image review, training captions and generation prompts.",
            workspace_help("knowledge")+IMAGE_BADGE_HELP,
        ); self.header.set_status("Category edits save automatically · Tag Details uses Save Tag"); self.knowledge_staged_status=self.header.status; root.addWidget(self.header)
        self.workspace_stack=QStackedWidget()
        self.workbench=self._build_tag_category_workbench()
        self.workspace_stack.addWidget(self.workbench)
        if self.editor_context.standalone:
            self.workspace_stack.setCurrentWidget(self.workbench)
            self._maintenance_return_context=None
            self._close_after_maintenance=False
            root.addWidget(self.workspace_stack,1)
            return
        self.system_knowledge_maintenance=SystemKnowledgeMaintenanceWidget(
            self.state,self,
            write_block_reason=self._system_knowledge_maintenance_block_reason,
        )
        self.system_knowledge_maintenance.return_requested.connect(
            self._return_from_system_knowledge_maintenance
        )
        self.system_knowledge_maintenance.open_review_view.connect(
            self._open_maintenance_review_view
        )
        self.system_knowledge_maintenance.knowledge_changed.connect(
            self._system_knowledge_maintenance_changed
        )
        self.system_knowledge_maintenance.content_sensitivity_changed.connect(
            self.content_sensitivity_changed.emit
        )
        self.system_knowledge_maintenance.background_work_finished.connect(
            self.background_work_finished.emit
        )
        self.workspace_stack.addWidget(self.system_knowledge_maintenance)
        self.workspace_stack.setCurrentWidget(self.workbench)
        self._maintenance_return_context=None
        self._close_after_maintenance=False
        root.addWidget(self.workspace_stack,1)

    def _build_tag_category_workbench(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
        commands=QHBoxLayout(); self.knowledge_search=QLineEdit(); self.knowledge_search.setPlaceholderText("Search Tag Encyclopedia…"); self.knowledge_search.setAccessibleName("Search Categories and Tags")
        self.knowledge_search.setClearButtonEnabled(True)
        self.knowledge_search.setToolTip("Search names, aliases and categories. Press Enter to open one exact Tag or alias match.")
        self.knowledge_search.setAccessibleDescription(self.knowledge_search.toolTip())
        self.knowledge_search.returnPressed.connect(self._open_exact_search_tag)
        self.knowledge_back_button=QPushButton("Back"); self.knowledge_forward_button=QPushButton("Forward")
        for button in (self.knowledge_back_button,self.knowledge_forward_button):button.setProperty("compact",True); button.hide()
        self.knowledge_back_button.setToolTip("Return to the previous Tag Encyclopedia query, review view, and selection.")
        self.knowledge_forward_button.setToolTip("Move forward through Tag Encyclopedia query, review view, and selection history.")
        self.knowledge_history_button=QToolButton(); self.knowledge_history_button.setText("History"); self.knowledge_history_button.setPopupMode(QToolButton.InstantPopup); self.knowledge_history_button.setProperty("compact",True); self.knowledge_history_button.setAccessibleName("Tag Encyclopedia history")
        self.knowledge_history_menu=QMenu(self.knowledge_history_button); self.knowledge_history_button.setMenu(self.knowledge_history_menu)
        self.knowledge_history_back_action=self.knowledge_history_menu.addAction("Back",self._navigate_back); self.knowledge_history_forward_action=self.knowledge_history_menu.addAction("Forward",self._navigate_forward); self.knowledge_history_menu.addSeparator(); self.knowledge_recent_menu=self.knowledge_history_menu.addMenu("Recent Views"); self.knowledge_history_menu.aboutToShow.connect(self._populate_history_menu)
        self.knowledge_new_button=QToolButton(); self.knowledge_new_button.setText("New…"); self.knowledge_new_button.setPopupMode(QToolButton.InstantPopup); self.knowledge_new_button.setProperty("compact",True); self.knowledge_new_button.setToolTip("Create a new category or SDXL Tag.")
        new_menu=QMenu(self.knowledge_new_button); new_category=new_menu.addAction("New Category…"); new_tag=new_menu.addAction("New SDXL Tag…"); self.knowledge_new_button.setMenu(new_menu)
        self.knowledge_more_button=QToolButton(); self.knowledge_more_button.setText("Knowledge Actions"); self.knowledge_more_button.setPopupMode(QToolButton.InstantPopup); self.knowledge_more_button.setProperty("compact",True); self.knowledge_more_button.setToolTip("Open saved views and supporting Tag Encyclopedia actions.")
        self.knowledge_more_menu=QMenu(self.knowledge_more_button); self.knowledge_more_button.setMenu(self.knowledge_more_menu); self.knowledge_more_menu.aboutToShow.connect(self._populate_knowledge_more_menu)
        self.review_character_links_action=self.knowledge_more_menu.addAction("Review Character Tag Links…",self.show_character_tag_links)
        self.knowledge_review_label=QLabel("Review")
        self.knowledge_review_label.setAccessibleName("Tag Encyclopedia filter")
        self.knowledge_review_view=QComboBox(); self.knowledge_review_view.setAccessibleName("Tag Encyclopedia filter")
        self.knowledge_review_view.setAccessibleDescription(
            "Choose a built-in review queue or a named saved query and review filter."
        )
        self.knowledge_review_view.setToolTip(
            "Choose a built-in review queue or a named saved query and review filter."
        )
        for label,key in (("All tags","all"),("Needs organization","needs_organization"),("Sensitivity review","sensitivity"),("Missing relationships","missing_relationships")):
            self.knowledge_review_view.addItem(label,key)
        self.knowledge_related_search=QCheckBox("Include related concepts")
        self.knowledge_related_search.setAccessibleName("Include policy-approved related concepts in Tag Encyclopedia search")
        self.knowledge_related_search.setToolTip("Deliberately include reviewed aliases, broader or narrower Tags, entailment, composition, and Usually Related paths. This changes discovery only; it never adds Image Knowledge.")
        commands.addWidget(self.knowledge_search,1); commands.addWidget(self.knowledge_related_search); commands.addWidget(self.knowledge_review_label); commands.addWidget(self.knowledge_review_view); commands.addWidget(self.knowledge_history_button); commands.addWidget(self.knowledge_new_button); commands.addWidget(self.knowledge_more_button); layout.addLayout(commands)
        self.knowledge_related_status=QLabel(); self.knowledge_related_status.setObjectName("mutedLabel"); self.knowledge_related_status.setWordWrap(True); self.knowledge_related_status.hide(); layout.addWidget(self.knowledge_related_status)
        self.knowledge_related_results=QTreeWidget(); self.knowledge_related_results.setHeaderLabels(("Related SDXL Tag","Exact relationship path / scope","Effective search use")); self.knowledge_related_results.setRootIsDecorated(False); self.knowledge_related_results.setUniformRowHeights(True); self.knowledge_related_results.setMaximumHeight(190); self.knowledge_related_results.setAccessibleName("Policy-aware related Tag Encyclopedia search results"); self.knowledge_related_results.header().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.knowledge_related_results.header().setSectionResizeMode(1,QHeaderView.Stretch); self.knowledge_related_results.header().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.knowledge_related_results.hide(); layout.addWidget(self.knowledge_related_results)
        self._workbench_host=page; self.tag_scope_page=None
        self.category_scope_page=self._build_categories(); layout.addWidget(self.category_scope_page,1)
        if self._external_category_search:self.category_workspace.use_external_search()
        self.knowledge_search.textChanged.connect(self._workbench_search_changed); self.knowledge_search.textEdited.connect(self._detach_saved_view)
        self.knowledge_related_search.toggled.connect(self._related_search_changed); self.knowledge_related_results.itemDoubleClicked.connect(self._open_policy_search_result)
        self.knowledge_review_view.currentIndexChanged.connect(self._review_view_changed); new_category.triggered.connect(self.category_workspace.new_category); new_tag.triggered.connect(self._new_tag_from_canvas)
        self.knowledge_back_button.clicked.connect(self._navigate_back); self.knowledge_forward_button.clicked.connect(self._navigate_forward)
        self._update_workbench_staged_state(self.category_workspace.has_unsaved_changes())
        self._update_navigation_buttons()
        return page

    def _finish_initialization(self):
        view_sets=self._prepared_review_view_sets
        self._prepared_review_view_sets=None
        self._refresh_review_views(view_sets=view_sets)
        if self._navigation_history:
            self._restore_navigation_context(
                self._navigation_history[self._navigation_index]
            )
        if (self.category_workspace.selected_key is None
                and not self.knowledge_search.text().strip()
                and self.knowledge_review_view.currentData()=="all"):
            self.category_workspace.show_initial_guidance()
        self._restoring_context=False
        self._record_navigation_context()
        self._request_visible_content_ratings()

    def _ensure_tag_editor(self):
        if self.tag_scope_page is not None:return
        self.tag_scope_page=self._build_tags()
        self.tag_scope_page.setParent(self._workbench_host)
        self.tag_scope_page.hide()
        self.refresh_tags()

    def show_character_tag_links(self):
        bridge=CharacterTagBridge(self.tags,self.characters,self.categories)
        dialog=CharacterTagBridgeDialog(bridge,self.tags,self.characters,categories=self.categories,parent=self); dialog.exec()
        if dialog.applied:
            self.characters.load()
            self._reload_modifier_roles()
            if self.current_tag_id:self._refresh_tag_identity(self.current_tag_id)

    def show_removed_vocabulary(self,*_args,initial_ids=()):
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status(
                "Finish or correct the current SDXL Tag edit before reviewing removed vocabulary.",
                error=True,
            )
            return
        if self._category_staging_active():
            QMessageBox.information(
                self,"Wait for System Knowledge",
                "Finish or save the current Category Workspace change before "
                "reviewing removed vocabulary.",
            )
            return
        restored=[]; recovery_requested=[]; dialog=RemovedVocabularyDialog(
            self.tags,self,initial_ids=initial_ids,allow_recovery_handoff=True,
        )
        dialog.restored.connect(lambda values:restored.extend(values))
        dialog.recovery_source_requested.connect(
            lambda:recovery_requested.append(True)
        )
        dialog.exec()
        if recovery_requested:
            self.knowledge_pack_import_requested.emit()
            return
        if restored:
            self.refresh_all(reload=True); self._changed()
            self.header.set_status(
                f"Restored {len(set(restored)):,} removed SDXL Tag"
                f"{'s' if len(set(restored))!=1 else ''} with the original stable identities."
            )

    def show_relationship_workspace(self,*,focus_tag_id="",initial_kind=""):
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status(
                "Finish or correct the current SDXL Tag edit before managing relationships.",
                error=True,
            )
            return
        if getattr(self,"_category_staging_active",lambda:False)():
            QMessageBox.information(
                self,"Wait for System Knowledge",
                "Finish or save the current Category Workspace change before "
                "reviewing relationships.",
            )
            return
        try:dialog=RelationshipWorkspaceDialog(
                self.tags,self.categories,self.characters,parent=self,
                focus_tag_id=focus_tag_id,initial_kind=initial_kind,
            )
        except Exception as exc:
            QMessageBox.warning(self,"Relationships Unavailable",f"The relationship store could not be opened and was left unchanged.\n\n{exc}")
            return
        dialog.exec()
        review_tag_id=getattr(dialog,"review_tag_id","")
        review_character_links=bool(getattr(dialog,"review_character_links",False))
        review_conflict_rule_id=str(
            getattr(dialog,"review_conflict_rule_id","") or ""
        )
        review_character_kind=str(getattr(dialog,"review_character_kind","") or "")
        review_character_id=str(getattr(dialog,"review_character_id","") or "")
        if dialog.changed:
            self._policy_search_service=None; self._policy_search_error=""
            self._consumer_policy_resolver=None; self._consumer_policy_error=""
            if hasattr(self,"_reload_modifier_roles"):
                self._reload_modifier_roles()
            self._changed()
            if self.current_tag_id in self.tags.tags:
                self._load_tag_record_into_editor(
                    self.tags.tags[self.current_tag_id],load_examples=False,
                )
        if review_conflict_rule_id:
            self.open_automation.emit(
                "conflicts.guided_checks",
                {"relationship_id":review_conflict_rule_id},
            )
        elif review_tag_id:
            self._open_tag_details(
                review_tag_id,initial_tab="Relationships",
                initial_relationship_task="Meaning Links",
            )
        elif review_character_kind and review_character_id:
            try:
                record_dialog=self._character_editor_factory(
                    CharacterRecordEditService(
                        self.characters,self.tags,self.categories,
                    ),
                    self.tags,
                    self.categories,
                    kind=review_character_kind,
                    record_id=review_character_id,
                    parent=self,
                )
            except ValueError as error:
                QMessageBox.warning(
                    self,"Character Record Unavailable",str(error),
                )
            else:
                record_dialog.exec()
                if record_dialog.saved:
                    self.characters.load()
                    self._changed()
        elif review_character_links:
            self.show_character_tag_links()

    def _open_tag_conflict_rules(self):
        tag_id=str(self.current_tag_id or "")
        if not tag_id:
            return
        scope={"tag_id":tag_id,"origin":"tag_details"}
        dialog=getattr(self,"_active_tag_editor_dialog",None)
        if dialog is not None and isValid(dialog) and dialog.isVisible():
            self._pending_tag_conflict_handoff=scope
            dialog.reject()
            return
        self.open_automation.emit("conflicts.guided_checks",scope)

    def _effective_conflict_summary(self,tag_id):
        try:
            engine=ConflictRuleEngine.load(
                self.editor_context.defaults/"conflicts.json",tags=self.tags,
            )
        except (OSError,RuntimeError,ValueError) as error:
            return f"Canonical conflict summary unavailable ({error})."
        if engine.relationship_error:
            return (
                "Canonical conflict summary unavailable "
                f"({engine.relationship_error})."
            )
        relationships={
            relationship.id:relationship
            for relationship in (
                engine.relationship_evaluator.registry.relationships
                if engine.relationship_evaluator is not None else ()
            )
            if relationship.kind==SCOPED_CONSTRAINT
        }
        rows=[]
        scope_labels={
            "image":"Image-wide",
            "same_character":"Same subject / character",
            "same_object":"Same owned object / assignment",
        }
        for rule in engine.rules:
            relationship=relationships.get(rule.relationship_id)
            if relationship is None or not any(
                endpoint.kind=="tag" and endpoint.id==str(tag_id)
                for endpoint in relationship.endpoints
            ):
                continue
            rows.append(
                f"{rule.label or relationship.id} · "
                f"{scope_labels.get(rule.scope,rule.scope.replace('_',' ').title())} · "
                f"{rule.allow_when.summary(lambda stable_id:self.tags.tags[stable_id].display if stable_id in self.tags.tags else stable_id)}"
            )
        if not rows:
            return "No active canonical diagnostic conflict rule includes this SDXL Tag."
        shown="\n".join(rows[:3])
        if len(rows)>3:shown+=f"\n+{len(rows)-3:,} more active rule{'s' if len(rows)-3!=1 else ''}"
        return shown

    def _build_tags(self):
        page=QWidget(); layout=QVBoxLayout(page); search_row=QHBoxLayout(); self.tag_search=QLineEdit(); self.tag_search.textChanged.connect(lambda *_:self._tag_search_timer.start())
        self.tag_filter=QComboBox(); self.tag_filter.addItem("All categories","")
        self.tag_order_filter=QComboBox(); self.tag_order_filter.addItem("All ordering groups","")
        for group in ORDER_GROUPS:self.tag_order_filter.addItem(ORDER_GROUP_LABELS[group],group)
        search_row.addStretch(1); search_row.addWidget(self.tag_filter); search_row.addWidget(self.tag_order_filter); layout.addLayout(search_row)
        self.tag_model=IncrementalResultModel(("SDXL Tag","Category","Status"),page_size=self.TAG_PAGE_SIZE,parent=self)
        result_row=QHBoxLayout(); self.tag_results=QLabel(); self.tag_results.setObjectName("mutedLabel"); self.tag_load_more_button=QPushButton("Load More…"); self.tag_load_more_button.setProperty("compact",True); self.tag_load_more_button.clicked.connect(self.tag_model.fetch_more); result_row.addWidget(self.tag_results,1); result_row.addWidget(self.tag_load_more_button); layout.addLayout(result_row)
        selection_row=QHBoxLayout(); self.tag_selection_status=QLabel("No SDXL Tags selected"); self.tag_selection_status.setObjectName("mutedLabel"); self.tag_selection_status.setAccessibleName("SDXL Tag selection status")
        self.tag_change_category_button=QPushButton("Change Category…"); self.tag_change_category_button.setProperty("compact",True); self.tag_change_category_button.setToolTip("Preview and automatically save one category change for every selected SDXL Tag."); self.tag_change_category_button.clicked.connect(self.change_selected_tag_categories)
        self.tag_actions_button=QPushButton("Actions"); self.tag_actions_button.setProperty("compact",True); self.tag_actions_menu=QMenu(self.tag_actions_button); self.tag_actions_button.setMenu(self.tag_actions_menu); self.tag_actions_menu.aboutToShow.connect(self._refresh_tag_actions_menu)
        selection_row.addWidget(self.tag_selection_status,1); selection_row.addWidget(self.tag_change_category_button); selection_row.addWidget(self.tag_actions_button); layout.addLayout(selection_row)
        workspace_splitter=QSplitter(Qt.Vertical); self.tag_workspace_splitter=workspace_splitter; layout.addWidget(workspace_splitter,1)
        splitter=QSplitter(Qt.Horizontal); self.tag_splitter=splitter; workspace_splitter.addWidget(splitter)
        self.tag_list=QTreeView(); self.tag_list.setModel(self.tag_model); self.tag_list.setRootIsDecorated(False); self.tag_list.setUniformRowHeights(True); self.tag_list.setSelectionBehavior(QAbstractItemView.SelectRows); self.tag_list.setSelectionMode(QAbstractItemView.ExtendedSelection); self.tag_list.setAccessibleName("Incremental SDXL Tag results"); self.tag_list.setAccessibleDescription("Use Ctrl or Shift with mouse or keyboard selection to choose several SDXL Tags. Change Category previews and automatically saves one action for the complete selection.")
        self.tag_list.setMinimumWidth(540); self.tag_list.setAlternatingRowColors(False); header=self.tag_list.header(); header.setSectionResizeMode(0,QHeaderView.Stretch); header.setSectionResizeMode(1,QHeaderView.Fixed); header.setSectionResizeMode(2,QHeaderView.Fixed); self.tag_list.setColumnWidth(1,165); self.tag_list.setColumnWidth(2,100); self.tag_list.selectionModel().currentChanged.connect(self.show_tag); self.tag_list.selectionModel().selectionChanged.connect(self._tag_selection_changed)
        self.tag_list.setContextMenuPolicy(Qt.CustomContextMenu); self.tag_list.customContextMenuRequested.connect(self.show_tag_context_menu)
        self.tag_model.counts_changed.connect(self._update_tag_result_counts)
        splitter.addWidget(self.tag_list)
        editor=LayoutSizedWidget(); self.tag_editor_panel=editor; editor_layout=QVBoxLayout(editor)
        self.tag_name=QLineEdit()
        self.tag_category=BoundedSearchableComboBox(
            placeholder="Type any part of a category path…",max_visible=12,
        )
        self.tag_category.setAccessibleName("SDXL Tag category")
        self.tag_sensitivity=QComboBox(); self.tag_sensitivity.addItem("Not classified","")
        for rating in ("general","sensitive","questionable","explicit"): self.tag_sensitivity.addItem(rating.title(),rating)
        self.tag_order_group=QComboBox()
        for group in ORDER_GROUPS:self.tag_order_group.addItem(ORDER_GROUP_LABELS[group],group)
        self.tag_priority=QSpinBox(); self.tag_priority.setRange(MIN_TAG_PRIORITY,MAX_TAG_PRIORITY); self.tag_priority.setToolTip("Higher values appear first within the same ordering group. Equal-priority SDXL Tags are alphabetized automatically.")
        self._tag_ordering_reviewed=False; self._tag_ordering_edited=False
        self.tag_content_flags=QLineEdit(); self.tag_content_flags.setPlaceholderText("Examples: nudity, sexual activity, gore")
        self.tag_aliases=TokenChipEditor(kind="alias",allow_text=True)
        self.tag_aliases.set_remove_guard(self._review_tag_alias_removal)
        self.tag_parent=TagReferenceEditor(self.tags,self.categories,relation_label="Base Tag",single=True)
        self.tag_base_effective=QLabel("No effective Base SDXL Tag"); self.tag_base_effective.setWordWrap(True); self.tag_base_effective.setObjectName("mutedLabel")
        self.tag_make_category_base=QPushButton("Use This Tag as Category Base"); self.tag_make_category_base.setProperty("compact",True); self.tag_make_category_base.clicked.connect(self._make_current_tag_category_base)
        self.tag_broader_review_host=QWidget(); broader_review_layout=QHBoxLayout(self.tag_broader_review_host); broader_review_layout.setContentsMargins(0,0,0,0)
        self.tag_broader_review_status=QLabel("No Broader Tag proposal is waiting"); self.tag_broader_review_status.setObjectName("mutedLabel"); self.tag_broader_review_status.setWordWrap(True)
        self.tag_broader_review_button=QPushButton("Review Broader Tag Proposal…"); self.tag_broader_review_button.setProperty("compact",True)
        self.tag_broader_review_button.setAccessibleDescription("Open the exact reusable-pattern Broader Tag proposal for this SDXL Tag. Pattern confidence does not approve the relationship automatically.")
        self.tag_broader_review_button.clicked.connect(self._review_current_tag_broader)
        broader_review_layout.addWidget(self.tag_broader_review_status,1); broader_review_layout.addWidget(self.tag_broader_review_button)
        self.tag_attributes=ModifierSlotEditor()
        self.tag_composition_preview=QLabel("Choose a Base SDXL Tag to resolve this exact composition.")
        self.tag_composition_preview.setWordWrap(True); self.tag_composition_preview.setObjectName("mutedLabel")
        self.tag_composition_preview.setAccessibleName("Resolved modifier composition preview")
        self.tag_implies=TagReferenceEditor(self.tags,self.categories,relation_label="Also Means")
        self.tag_conflicts=TagReferenceEditor(self.tags,self.categories,relation_label="Contrary Value")
        self.tag_conflicts.set_read_only(
            "Contrary Values use the shared symmetric relationship view. Review their canonical rule in Rules & Automation."
        )
        self.tag_notes=FinishedEditingTextEdit(); self.tag_notes.setMaximumHeight(70); self.tag_notes.setAccessibleName("Meaning and Usage notes"); self._tag_relationship_dirty_fields=set()
        provenance_row=QWidget(); provenance_layout=QHBoxLayout(provenance_row); provenance_layout.setContentsMargins(0,0,0,0)
        self.tag_provenance=QLabel("No recorded provenance"); self.tag_provenance.setObjectName("mutedLabel"); self.tag_provenance.setWordWrap(True)
        provenance_details=QPushButton("View Details…"); provenance_details.clicked.connect(self.view_tag_provenance)
        provenance_layout.addWidget(self.tag_provenance,1); provenance_layout.addWidget(provenance_details)
        self._tag_provenance_details=""
        self.tag_multi_selection_notice=QLabel(); self.tag_multi_selection_notice.setWordWrap(True); self.tag_multi_selection_notice.setObjectName("mutedLabel"); self.tag_multi_selection_notice.setAccessibleName("Multiple SDXL Tag selection notice"); self.tag_multi_selection_notice.hide(); editor_layout.addWidget(self.tag_multi_selection_notice)
        self.tag_editor_tabs=CurrentPageTabWidget(); editor_layout.addWidget(self.tag_editor_tabs,1)
        self.tag_editor_tabs.currentChanged.connect(lambda _index:QTimer.singleShot(0,self._reset_tag_editor_scroll))
        basics=QWidget(); self.tag_basics_tab=basics; basics_layout=QVBoxLayout(basics)
        self.tag_basics_form_host=QWidget(); self.tag_basics_form_host.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        basics_form=QFormLayout(self.tag_basics_form_host); self.tag_basics_form=basics_form
        self.tag_semantic_role=QLabel("Ordinary SDXL Tag"); self.tag_semantic_role.setObjectName("mutedLabel"); self.tag_semantic_role.setWordWrap(True)
        self.tag_consumer_restriction=QLabel(); self.tag_consumer_restriction.setObjectName("mutedLabel"); self.tag_consumer_restriction.setWordWrap(True); self.tag_consumer_restriction.setTextInteractionFlags(self.tag_consumer_restriction.textInteractionFlags()|Qt.TextSelectableByMouse)
        basics_form.addRow("Preferred name",self.tag_name); basics_form.addRow("Category",self.tag_category); basics_form.addRow("Knowledge role",self.tag_semantic_role); basics_form.addRow("Consumer restriction",self.tag_consumer_restriction); self._set_tag_basics_row_visible(self.tag_consumer_restriction,False)
        self.tag_identity_notice=QWidget(); identity_notice_layout=QVBoxLayout(self.tag_identity_notice); identity_notice_layout.setContentsMargins(0,0,0,0)
        self.tag_identity_notice_text=QLabel(); self.tag_identity_notice_text.setWordWrap(True); self.tag_identity_notice_text.setAccessibleName("Character Knowledge record status")
        self.tag_identity_repair_button=QPushButton("Review Character Knowledge…"); self.tag_identity_repair_button.setProperty("compact",True)
        self.tag_identity_repair_button.clicked.connect(self._open_character_knowledge)
        identity_notice_layout.addWidget(self.tag_identity_notice_text); identity_notice_layout.addWidget(self.tag_identity_repair_button,0,Qt.AlignLeft)
        basics_form.addRow("Character Knowledge",self.tag_identity_notice); self._set_tag_basics_row_visible(self.tag_identity_notice,False)
        order_summary=QWidget(); order_summary_layout=QHBoxLayout(order_summary); order_summary_layout.setContentsMargins(0,0,0,0); self.tag_order_summary=QLabel(); self.tag_order_summary.setObjectName("mutedLabel"); change_tag_order=QPushButton("Change Order…"); change_tag_order.setProperty("compact",True); change_tag_order.clicked.connect(self.show_caption_order); order_summary_layout.addWidget(self.tag_order_summary,1); order_summary_layout.addWidget(change_tag_order)
        basics_form.addRow("Caption order",order_summary); self.tag_order_summary_row=order_summary
        basics_layout.addWidget(self.tag_basics_form_host,0,Qt.AlignTop)
        self.tag_meaning_group=QGroupBox("Meaning && Usage"); meaning_layout=QVBoxLayout(self.tag_meaning_group); meaning_layout.addWidget(self.tag_notes)
        meaning_review_row=QHBoxLayout()
        self.tag_meaning_review_status=QLabel("No attributed updates are waiting")
        self.tag_meaning_review_status.setObjectName("mutedLabel"); self.tag_meaning_review_status.setWordWrap(True)
        self.tag_meaning_review_button=QPushButton("Review Proposed Updates…")
        self.tag_meaning_review_button.setProperty("compact",True)
        self.tag_meaning_review_button.setAccessibleDescription(
            "Open source-attributed Meaning and Usage proposals for this exact SDXL Tag. No evidence is accepted automatically."
        )
        self.tag_meaning_review_button.clicked.connect(self._review_current_tag_meanings)
        meaning_review_row.addWidget(self.tag_meaning_review_status,1); meaning_review_row.addWidget(self.tag_meaning_review_button)
        meaning_layout.addLayout(meaning_review_row)
        basics_layout.addWidget(self.tag_meaning_group,0,Qt.AlignTop)
        self.tag_creation_role_help=QLabel(); self.tag_creation_role_help.setObjectName("mutedLabel"); self.tag_creation_role_help.setWordWrap(True); self.tag_creation_role_help.setAccessibleName("Character Knowledge creation guidance"); self.tag_creation_role_help.hide(); basics_layout.addWidget(self.tag_creation_role_help)
        self.tag_character_creation_group=QGroupBox("Character Identity decision")
        character_creation_form=QFormLayout(self.tag_character_creation_group)
        self.tag_character_creation_action=QComboBox()
        self.tag_character_creation_action.setAccessibleName("Character Identity creation outcome")
        self.tag_character_creation_action.addItem("Set up the linked record automatically","automatic")
        self.tag_character_creation_action.addItem("Keep reusable Tag only","tag_only")
        self.tag_character_creation_action.addItem("Link an existing character","link_existing")
        self.tag_character_creation_action.addItem("Create a new character record","create_record")
        self.tag_character_creation_existing=BoundedSearchableComboBox(
            placeholder="Search characters, aliases, or Character Sources…",max_visible=12,
        )
        self.tag_character_creation_existing.setAccessibleName("Existing typed character")
        self.tag_character_creation_alias=QCheckBox()
        self.tag_character_creation_alias.setAccessibleName("Add character and source alias to the new SDXL Tag")
        self.tag_character_creation_status=QLabel()
        self.tag_character_creation_status.setObjectName("mutedLabel")
        self.tag_character_creation_status.setWordWrap(True)
        self.tag_character_creation_status.setAccessibleName("Character Identity decision status")
        character_creation_form.addRow("After Tag creation",self.tag_character_creation_action)
        character_creation_form.addRow("Existing character",self.tag_character_creation_existing)
        character_creation_form.addRow("Suggested alias",self.tag_character_creation_alias)
        character_creation_form.addRow(self.tag_character_creation_status)
        self.tag_character_creation_group.hide(); basics_layout.addWidget(self.tag_character_creation_group)
        self.tag_character_creation_action.currentIndexChanged.connect(self._refresh_character_creation_decision)
        self.tag_character_creation_existing.currentIndexChanged.connect(self._refresh_character_creation_decision)
        self.tag_character_creation_alias.toggled.connect(self._update_tag_create_dialog_action)
        self.tag_name.textChanged.connect(self._refresh_character_creation_alias_only)
        self.tag_editor_tabs.addTab(basics,"Basics")
        relationships=QWidget(); relationships_layout=QVBoxLayout(relationships)
        self.tag_pattern_preview=QLabel("Choose a Base SDXL Tag to see its reusable form."); self.tag_pattern_preview.setWordWrap(True); self.tag_pattern_preview.setObjectName("mutedLabel")
        self.tag_reuse_source=QLabel(); self.tag_reuse_source.setWordWrap(True); self.tag_reuse_source.setObjectName("mutedLabel")
        self.tag_allowed_modifier_editor=TokenChipEditor(kind="modifier family",allow_text=False,placeholder="Choose Color, Material, Pattern, or another family…")
        self._refresh_modifier_family_choices()
        self.tag_allowed_modifier_editor.changed.connect(self._tag_reuse_policy_changed)
        self.tag_reuse_values=QPushButton("Review Value Scopes…"); self.tag_reuse_values.setProperty("compact",True); self.tag_reuse_values.clicked.connect(self._review_tag_value_scopes)
        self.tag_reuse_inherit=QPushButton("Reset Direct Policy…"); self.tag_reuse_inherit.setProperty("compact",True); self.tag_reuse_inherit.setToolTip("Clear this Tag's direct Allowed Details policy and reveal its reviewed default, foundation, inherited, or unset result."); self.tag_reuse_inherit.setAccessibleName("Reset direct Allowed Details policy"); self.tag_reuse_inherit.clicked.connect(self._reset_tag_reuse_policy)
        self.tag_modifier_compatibility_intro=QLabel(); self.tag_modifier_compatibility_intro.setWordWrap(True); self.tag_modifier_compatibility_intro.setObjectName("mutedLabel")
        self.tag_modifier_compatibility_button=QPushButton("Review Compatible Base Tags…"); self.tag_modifier_compatibility_button.setProperty("compact",True); self.tag_modifier_compatibility_button.clicked.connect(self._show_modifier_compatibility_workspace)
        self.tag_modifier_compatibility_undo=QPushButton("Undo Last Saved Batch"); self.tag_modifier_compatibility_undo.setProperty("compact",True); self.tag_modifier_compatibility_undo.clicked.connect(self._undo_last_modifier_compatibility_batch); self.tag_modifier_compatibility_undo.hide()
        names_group=QGroupBox("Names and aliases"); names_form=QFormLayout(names_group)
        names_form.addRow("Aliases (free text)",self.tag_aliases)
        derived_group=QGroupBox("How this tag is derived"); derived_form=QFormLayout(derived_group)
        derived_intro=QLabel("The Base SDXL Tag and modifier values describe how this exact tag specializes a broader tag."); derived_intro.setWordWrap(True); derived_intro.setObjectName("mutedLabel"); derived_form.addRow(derived_intro)
        self.tag_effective_base_card=QFrame(); self.tag_effective_base_card.setFrameShape(QFrame.StyledPanel); self.tag_effective_base_card.setAccessibleName("Effective Base SDXL Tag")
        effective_base_layout=QVBoxLayout(self.tag_effective_base_card); effective_base_layout.setContentsMargins(12,9,12,9); effective_base_layout.setSpacing(6)
        effective_base_title=QLabel("Effective Base SDXL Tag"); effective_base_title.setObjectName("sectionTitle"); effective_base_layout.addWidget(effective_base_title)
        self.tag_effective_base=TagReferenceEditor(self.tags,self.categories,relation_label="Effective Base Tag",single=True)
        self.tag_effective_base.setAcceptDrops(False); self.tag_effective_base.add_button.hide(); self.tag_effective_base.status.hide(); effective_base_layout.addWidget(self.tag_effective_base)
        effective_base_actions=QHBoxLayout(); effective_base_actions.addWidget(self.tag_base_effective,1); effective_base_actions.addWidget(self.tag_make_category_base); effective_base_layout.addLayout(effective_base_actions)
        derived_form.addRow(self.tag_effective_base_card); derived_form.addRow("Base override (optional)",self.tag_parent); derived_form.addRow("Proposed update",self.tag_broader_review_host); derived_form.addRow("Modifier slots",self.tag_attributes); derived_form.addRow("Resolved composition",self.tag_composition_preview)
        self.tag_reuse_group=QGroupBox("Reusable variations"); reuse_layout=QVBoxLayout(self.tag_reuse_group)
        self.tag_reuse_summary=QLabel(); self.tag_reuse_summary.setWordWrap(True); reuse_layout.addWidget(self.tag_reuse_summary)
        self.tag_reuse_policy_host=QWidget(); reuse_policy_layout=QVBoxLayout(self.tag_reuse_policy_host); reuse_policy_layout.setContentsMargins(0,0,0,0)
        reuse_policy_layout.addWidget(self.tag_allowed_modifier_editor)
        reuse_policy_actions=QHBoxLayout(); reuse_policy_actions.addWidget(self.tag_reuse_source,1); reuse_policy_actions.addWidget(self.tag_reuse_values); reuse_policy_actions.addWidget(self.tag_reuse_inherit); reuse_policy_layout.addLayout(reuse_policy_actions); reuse_layout.addWidget(self.tag_reuse_policy_host)
        self.tag_modifier_compatibility_host=QWidget(); compatibility_layout=QVBoxLayout(self.tag_modifier_compatibility_host); compatibility_layout.setContentsMargins(0,0,0,0); compatibility_layout.addWidget(self.tag_modifier_compatibility_intro); compatibility_actions=QHBoxLayout(); compatibility_actions.addWidget(self.tag_modifier_compatibility_button); compatibility_actions.addWidget(self.tag_modifier_compatibility_undo); compatibility_actions.addStretch(1); compatibility_layout.addLayout(compatibility_actions); reuse_layout.addWidget(self.tag_modifier_compatibility_host)
        reuse_layout.addWidget(self.tag_pattern_preview)
        self.tag_reuse_feedback=QLabel(); self.tag_reuse_feedback.setWordWrap(True)
        self.tag_reuse_feedback.setAccessibleName("Allowed Details save status")
        self.tag_reuse_feedback.hide(); reuse_layout.addWidget(self.tag_reuse_feedback)
        links_group=QGroupBox("Semantic links"); links_form=QFormLayout(links_group)
        self.tag_constraint_host=QWidget(); constraint_layout=QVBoxLayout(self.tag_constraint_host); constraint_layout.setContentsMargins(0,0,0,0); self.tag_constraint_summary=QLabel(); self.tag_constraint_summary.setWordWrap(True); self.tag_constraint_summary.setObjectName("mutedLabel"); self.tag_constraint_summary.setAccessibleName("Effective diagnostic conflict rule summary"); constraint_layout.addWidget(self.tag_constraint_summary); constraint_layout.addWidget(self.tag_conflicts)
        self.tag_manage_constraints=QPushButton("Review Conflict Rules…"); self.tag_manage_constraints.setProperty("compact",True); self.tag_manage_constraints.setToolTip("Open this Tag's canonical symmetric conflict rules in Rules & Automation."); self.tag_manage_constraints.clicked.connect(self._open_tag_conflict_rules); constraint_actions=QHBoxLayout(); constraint_actions.addWidget(self.tag_manage_constraints); constraint_actions.addStretch(1); constraint_layout.addLayout(constraint_actions)
        links_form.addRow("Also means",self.tag_implies); links_form.addRow("Contrary values",self.tag_constraint_host)
        manage_relationships=QPushButton("Manage Relationships…"); manage_relationships.setProperty("compact",True); manage_relationships.setToolTip("Open task-focused batch and audit sections for names, meaning, reusable details, compatibility, characters, and migration."); manage_relationships.clicked.connect(lambda:self.show_relationship_workspace())
        relationship_scope=QLabel(
            "Other Names, Base & Composition, and Meaning Links use Save Tag. "
            "Allowed Details saves separately when changed."
        )
        relationship_scope.setWordWrap(True); relationship_scope.setObjectName("mutedLabel")
        relationships_layout.addWidget(relationship_scope)
        relationship_actions=QHBoxLayout(); relationship_actions.addStretch(1)
        relationship_actions.addWidget(manage_relationships)
        relationships_layout.addLayout(relationship_actions)
        self.tag_relationship_tasks=QTabBar()
        self.tag_relationship_tasks.setObjectName("tagRelationshipTasks")
        self.tag_relationship_tasks.setAccessibleName("Relationship tasks")
        self.tag_relationship_tasks.setExpanding(False)
        self._tag_relationship_task_pages=[]
        relationships_layout.addWidget(self.tag_relationship_tasks)
        for title,group in (
            ("Other Names",names_group),
            ("Base && Composition",derived_group),
            ("Allowed Details",self.tag_reuse_group),
            ("Meaning Links",links_group),
        ):
            task=QWidget(); task_layout=QVBoxLayout(task)
            task_layout.setContentsMargins(0,8,0,0)
            task_layout.addWidget(group)
            task.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
            self._tag_relationship_task_pages.append(task)
            relationships_layout.addWidget(task)
            self.tag_relationship_tasks.addTab(title)
        self.tag_relationship_tasks.currentChanged.connect(self._show_tag_relationship_task)
        self._show_tag_relationship_task(self.tag_relationship_tasks.currentIndex())
        relationships_layout.addStretch(1)
        self.tag_editor_tabs.addTab(relationships,"Relationships")
        safety=QWidget(); safety_layout=QVBoxLayout(safety); safety_form=QFormLayout()
        self.tag_sensitivity_effective=QLabel("Not classified"); self.tag_sensitivity_effective.setWordWrap(True); self.tag_sensitivity_effective.setObjectName("mutedLabel")
        safety_form.addRow("Sensitivity override",self.tag_sensitivity); safety_form.addRow("Effective sensitivity",self.tag_sensitivity_effective); safety_form.addRow("Sensitivity reasons",self.tag_content_flags); safety_form.addRow("Knowledge sources",provenance_row)
        safety_layout.addLayout(safety_form); self.tag_stats=QLabel(); self.tag_stats.setObjectName("mutedLabel"); self.tag_stats.setWordWrap(True); safety_layout.addWidget(self.tag_stats); safety_layout.addStretch(1)
        self.tag_editor_tabs.addTab(safety,"Safety && References")
        self.tag_appearance_editor=TagAppearanceEditor(
            self.tags,self.categories,allowed_scopes=("tag",),show_global_tools=False,
        )
        self.tag_appearance_editor.manage_global_requested.connect(
            lambda category_id:self._open_tag_color_workspace(category_id,initial_scope="category")
        )
        self.tag_appearance_tab_index=self.tag_editor_tabs.addTab(self.tag_appearance_editor,"Colors")
        identity=QWidget(); identity_layout=QVBoxLayout(identity)
        self.tag_identity_intro=QLabel("Edit the typed Character Knowledge linked to this SDXL Tag. These fields save automatically and do not change image facts, captions, Metadata, TXT, or image files. Preferred name and aliases remain in Basics and Relationships."); self.tag_identity_intro.setWordWrap(True); self.tag_identity_intro.setObjectName("mutedLabel"); self.tag_identity_intro.setAccessibleName("Character Knowledge data authority"); identity_layout.addWidget(self.tag_identity_intro)
        identity_actions=QVBoxLayout(); self.tag_identity_status=QLabel(); self.tag_identity_status.setObjectName("mutedLabel"); self.tag_identity_status.setWordWrap(True); self.tag_identity_status.setAccessibleName("Character Knowledge relationship status"); identity_actions.addWidget(self.tag_identity_status)
        identity_action_buttons=QHBoxLayout()
        self.tag_identity_create_source=QPushButton("Create Character Source Record…"); self.tag_identity_create_character=QPushButton("Create Character Record…"); self.tag_identity_create_outfit=QPushButton("Create Outfit Record…")
        self.tag_identity_link_existing=QPushButton("Link Existing Record…"); self.tag_identity_link_existing.setProperty("compact",True)
        self.tag_identity_link_existing.clicked.connect(self._link_tag_identity)
        identity_action_buttons.addWidget(self.tag_identity_link_existing)
        for button in (self.tag_identity_create_source,self.tag_identity_create_character,self.tag_identity_create_outfit):button.setProperty("compact",True)
        self.tag_identity_create_source.setToolTip("Create one typed Character Source record linked to this existing Character Source SDXL Tag."); self.tag_identity_create_character.setToolTip("Create one typed character record linked to this existing Character Identity SDXL Tag."); self.tag_identity_create_outfit.setToolTip("Create one typed outfit record linked to this existing Character Outfit SDXL Tag.")
        self.tag_identity_create_source.clicked.connect(lambda:self._create_tag_identity("Series")); self.tag_identity_create_character.clicked.connect(lambda:self._create_tag_identity("Characters")); self.tag_identity_create_outfit.clicked.connect(lambda:self._create_tag_identity("Outfits"))
        identity_action_buttons.addWidget(self.tag_identity_create_source); identity_action_buttons.addWidget(self.tag_identity_create_character); identity_action_buttons.addWidget(self.tag_identity_create_outfit); identity_action_buttons.addStretch(1); identity_actions.addLayout(identity_action_buttons); identity_layout.addLayout(identity_actions)
        self.tag_identity_inline_host=QWidget(); self.tag_identity_inline_host.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum); self.tag_identity_inline_layout=QVBoxLayout(self.tag_identity_inline_host); self.tag_identity_inline_layout.setContentsMargins(0,0,0,0); identity_layout.addWidget(self.tag_identity_inline_host)
        self.tag_identity_inline_editor=None; self._tag_identity_inline_key=None
        self.tag_identity_tree_group=QGroupBox("Linked records"); tree_group_layout=QVBoxLayout(self.tag_identity_tree_group)
        self.tag_identity_tree=QTreeWidget(); self.tag_identity_tree.setHeaderLabels(("Type","Record","Character Source","Relationship details")); self.tag_identity_tree.setRootIsDecorated(True); self.tag_identity_tree.header().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.tag_identity_tree.header().setSectionResizeMode(1,QHeaderView.Stretch); self.tag_identity_tree.header().setSectionResizeMode(2,QHeaderView.Stretch); self.tag_identity_tree.header().setSectionResizeMode(3,QHeaderView.Stretch); self.tag_identity_tree.setAccessibleName("Reviewed Character Source, character, and outfit relationships"); self.tag_identity_tree.setMaximumHeight(190); tree_group_layout.addWidget(self.tag_identity_tree); identity_layout.addWidget(self.tag_identity_tree_group)
        self.tag_identity_suggest_setup=QPushButton("Suggest a Generation Setup…")
        self.tag_identity_suggest_setup.setToolTip("Review recurring details from images of this Character or Outfit. Opening this review saves and generates nothing; it never changes Image Knowledge.")
        self.tag_identity_suggest_setup.hide()
        self.tag_identity_suggest_setup.clicked.connect(self._suggest_tag_identity_setup)
        tree_group_layout.addWidget(self.tag_identity_suggest_setup,0,Qt.AlignLeft)
        self.tag_identity_details=QTextEdit(); self.tag_identity_details.setReadOnly(True); self.tag_identity_details.hide()
        identity_layout.addStretch(1)
        self.tag_identity_tab_index=self.tag_editor_tabs.addTab(identity,"Character Knowledge"); self.tag_editor_tabs.setTabVisible(self.tag_identity_tab_index,False)
        self.tag_identity_tree.currentItemChanged.connect(self._show_tag_identity_details)
        self.tag_editor_tabs.currentChanged.connect(self._character_knowledge_tab_changed)
        actions=QHBoxLayout(); self.tag_new_button=QPushButton("New SDXL Tag"); self.tag_new_button.clicked.connect(self.new_tag); self.tag_new_button.hide()
        self.tag_save_status=QLabel("Changes save automatically."); self.tag_save_status.setObjectName("mutedLabel"); self.tag_save_status.setWordWrap(True)
        self.tag_create_button=QPushButton("Create SDXL Tag"); self.tag_create_button.setProperty("primary",True); self.tag_create_button.clicked.connect(self.save_tag)
        for button in (self.tag_new_button,self.tag_create_button,provenance_details): button.setProperty("compact",True)
        actions.addWidget(self.tag_new_button); actions.addWidget(self.tag_save_status,1); actions.addWidget(self.tag_create_button); editor_layout.addLayout(actions)
        from .components import bind_status_footer
        bind_status_footer(self.tag_editor_panel,self.tag_save_status,title="Tag Details",follow_visibility=True)
        editor.setMinimumWidth(650); splitter.addWidget(editor); splitter.setStretchFactor(0,3); splitter.setStretchFactor(1,5); splitter.setSizes((610,990))
        self.tag_gallery=EmbeddedGallery(page_size=12,card_width=136,compact_empty=True); self.tag_gallery.setMaximumHeight(430); self.tag_gallery.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        self.tag_gallery.visible_paths_changed.connect(self.gallery_status.request)
        self.tag_gallery.visible_paths_changed.connect(self.content_ratings_requested.emit)
        self.gallery_status.updated.connect(self.tag_gallery.refresh_cards)
        self.tag_gallery.edit_requested.connect(self.edit_image_data); self.tag_gallery.force_ai_requested.connect(self.force_ai_requested)
        self.tag_gallery.open_dataset_requested.connect(lambda:self.show_dataset_tag.emit(self.tag_name.text().strip()) if self.tag_name.text().strip() else None)
        basics_layout.addWidget(self.tag_gallery,0,Qt.AlignTop)
        basics_layout.addStretch(1)
        workspace_splitter.setStretchFactor(0,1)
        self.tag_filter.currentIndexChanged.connect(self.refresh_tags); self.tag_order_filter.currentIndexChanged.connect(self.refresh_tags); self.tag_category.currentIndexChanged.connect(self._suggest_order_group); self.tag_category.currentIndexChanged.connect(self._refresh_tag_inheritance); self.tag_category.currentIndexChanged.connect(self._refresh_tag_semantic_role)
        self.tag_name.textEdited.connect(self._queue_tag_name_save)
        self.tag_name.editingFinished.connect(self._flush_tag_save)
        self.tag_content_flags.textEdited.connect(self._queue_tag_save)
        self.tag_content_flags.editingFinished.connect(self._flush_tag_save)
        self.tag_category.lineEdit().textEdited.connect(self._queue_tag_save); self.tag_category.lineEdit().editingFinished.connect(self._flush_tag_save); self.tag_category.activated.connect(self._save_tag_control_change)
        self.tag_aliases.changed.connect(lambda *_:self._tag_relationship_changed("aliases")); self.tag_parent.changed.connect(lambda *_:self._tag_relationship_changed("parent")); self.tag_attributes.changed.connect(lambda *_:self._tag_relationship_changed("attributes")); self.tag_implies.changed.connect(lambda *_:self._tag_relationship_changed("implies"))
        for editor in (self.tag_parent,self.tag_implies,self.tag_conflicts):editor.edit_requested.connect(self._navigate_tag_reference)
        self.tag_sensitivity.activated.connect(self._refresh_tag_inheritance); self.tag_sensitivity.activated.connect(self._save_tag_control_change)
        self.tag_order_group.activated.connect(self._mark_tag_ordering_edited); self.tag_order_group.activated.connect(self._save_tag_control_change)
        self.tag_priority.editingFinished.connect(self._mark_tag_ordering_edited); self.tag_priority.editingFinished.connect(self._save_tag_control_change)
        self.tag_notes.textChanged.connect(self._queue_tag_notes_save)
        self.tag_notes.editingFinished.connect(self._flush_tag_save)
        return page

    def _build_categories(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0)
        workspace=CategoryWorkspace(
            self.tags,self.categories,self.patterns,characters=self.characters,settings=getattr(self.state,"settings",None),
            image_index_provider=lambda:getattr(getattr(self.state,"dataset",None),"index",None),pool=self.pool,
            gallery_status=self.gallery_status,job_manager=self._background_jobs,
            evidence_queries=self._evidence_queries,defer_sequence=True,
            identity_group_cache=self._prepared_identity_group_cache,
            prepared_session=self._prepared_category_session,parent=page,
            defer_initial_details=True,
            prepared_structure_search_index=(
                self._prepared_category_structure_search_index
            ),
            history_operation_factory=self._capture_tag_removal_history,
            history_opener=self._open_tag_removal_history,
            history_recovery_handler=lambda image:self.edit_image_data.emit(str(image)),
        )
        workspace.tag_removal_finished.connect(self._notify_background_work_finished)
        workspace.workspace_saved.connect(self._category_workspace_saved); workspace.routine_change_saved.connect(self._routine_change_saved); workspace.staged_state_changed.connect(self._update_workbench_staged_state); workspace.edit_tag_requested.connect(self._open_tag_details); workspace.edit_category_appearance_requested.connect(self._open_category_appearance); workspace.change_tags_category_requested.connect(self._change_chip_tag_categories); workspace.add_tag_requested.connect(self._add_tags_to_category); workspace.always_ignore_tags_requested.connect(self._always_ignore_tags); workspace.show_all_tags_requested.connect(lambda:self._activate_review_view("all")); workspace.open_automation_requested.connect(lambda scope:self.open_automation.emit("rules",scope)); workspace.update_matching_images_requested.connect(lambda scope:self.open_dataset.emit("dataset.image_facts.update_matching",scope))
        workspace.image_data_changed.connect(
            lambda paths:self.state.events.publish(
                "image_data_changed",images=tuple(str(Path(value)) for value in paths),
            )
        )
        workspace.context_changed.connect(self._record_navigation_context)
        workspace.focus_global_search_requested.connect(
            lambda:QTimer.singleShot(0,self.knowledge_search.setFocus)
        )
        workspace.gallery.visible_paths_changed.connect(self.content_ratings_requested.emit)
        workspace.edit_image_data.connect(self.edit_image_data); workspace.force_ai_requested.connect(self.force_ai_requested); workspace.show_dataset_tag.connect(self.show_dataset_tag)
        layout.addWidget(workspace,1); self.category_workspace=workspace; self.category_views=workspace.views; self.order_workspace=workspace; self._changing_category_view=False
        return page

    def _capture_tag_removal_history(self,images):
        from .history_workflow import capture_operation
        return capture_operation(self,tuple(images),operation_kind="image_tag_removal",
            reason="Remove selected confirmed Tags from affected images")

    def _open_tag_removal_history(self,images,operation_id):
        from .history_workflow import open_history
        dialog=open_history(self,tuple(images),operation_target=operation_id,
            on_complete=lambda paths:self.state.events.publish(
                "image_data_changed",images=tuple(str(image) for image in paths)),
            recovery_handler=lambda image:self.edit_image_data.emit(str(image)))
        panel = dialog.active_panel
        panel.units.setCurrentIndex(panel.units.findData("tags"))
        return dialog

    def _notify_background_work_finished(self):
        if not self.has_active_background_work():
            self.background_work_finished.emit()

    def _workbench_scope(self):
        return "categories"

    def _set_workbench_scope(self,scope):
        # The Tag Encyclopedia now has one canonical category-centered canvas.
        # Keep this compatibility entry point for callers that request it by scope.
        return

    def _workbench_search_changed(self,text):
        self._refresh_policy_search(text)
        if self.category_workspace.search.text()!=text:self.category_workspace.search.setText(text)
        if not self._restoring_context:self._context_timer.start()

    def _open_exact_search_tag(self):
        """Open one deliberate exact match without choosing an ambiguous result."""
        query=self.knowledge_search.text().strip().casefold()
        workspace=self.category_workspace
        if not query or workspace.has_unsaved_changes():return
        review_ids=workspace._review_filter_tag_ids
        matches=[
            record for record in workspace.session.all_tags()
            if (review_ids is None or record.id in review_ids)
            and any(query==str(name).casefold() for name in self.tags.identity_names(record))
        ]
        if len(matches)!=1:
            workspace.structure_tree.setFocus()
            return
        workspace._search_timer.stop()
        workspace.selected_key=("category",matches[0].category)
        with QSignalBlocker(workspace.membership_search):
            workspace.membership_search.clear()
        workspace._apply_search()
        workspace.membership_chips.select_values((matches[0].display,))
        self._context_timer.stop()
        self._record_navigation_context()
        self._open_tag_details(matches[0].id)

    def _related_search_changed(self,*_):
        self._refresh_policy_search(self.knowledge_search.text())
        if not self._restoring_context:self._context_timer.start()

    def _refresh_policy_search(self,text):
        query=str(text or "").strip()
        if not self.knowledge_related_search.isChecked() or not query:
            self.knowledge_related_results.clear(); self.knowledge_related_results.hide(); self.knowledge_related_status.hide()
            if hasattr(self,"category_workspace"):self.category_workspace.set_policy_search_matches()
            return
        if self._policy_search_service is None and not self._policy_search_error:
            try:self._policy_search_service=ConsumerPolicySearchService.from_paths(
                self.tags,
                default_relationships=self.editor_context.defaults/"relationships.json",
                user_relationships=self.editor_context.relationships,
                default_policies=self.editor_context.defaults/"consumer_policies.json",
                user_policies=self.editor_context.consumer_policies,
            )
            except (OSError,RuntimeError,ValueError) as error:self._policy_search_error=str(error)
        self.knowledge_related_results.clear()
        if self._policy_search_service is None:
            summary=f"Policy-aware related search is unavailable and ordinary name/alias search remains active: {self._policy_search_error or 'consumer policy could not be loaded'}."
            self.knowledge_related_status.setText(summary); self.knowledge_related_status.show(); self.knowledge_related_results.hide(); self.category_workspace.set_policy_search_matches(summary=summary); return
        matches=self._policy_search_service.search(query)
        for match in matches:
            path=f"{match.path_label} • {match.relationship_label} • {match.scope_label}"
            item=QTreeWidgetItem((match.display,path,match.effective_use_label)); item.setData(0,Qt.UserRole,match.tag_id)
            for column in range(3):item.setToolTip(column,match.explanation)
            self.knowledge_related_results.addTopLevelItem(item)
        count=len(matches); summary=(f"Policy-aware discovery included {count:,} reviewed related SDXL Tag{'s' if count!=1 else ''}. Each row shows the exact relationship path, scope, and effective Search & Organization decision; discovery never adds Image Knowledge.")
        self.knowledge_related_status.setText(summary); self.knowledge_related_status.show(); self.knowledge_related_results.setVisible(bool(matches)); self.category_workspace.set_policy_search_matches((match.tag_id for match in matches),summary)

    def _open_policy_search_result(self,item,_column=0):
        tag_id=str(item.data(0,Qt.UserRole) or "") if item is not None else ""
        if tag_id:self._open_tag_details(tag_id)

    @staticmethod
    def _review_view_labels():
        return {
            "all":"All tags",
            "needs_organization":"Needs organization",
            "sensitivity":"Sensitivity review",
            "missing_relationships":"Missing relationships",
        }

    def _settings_json(self,key,default):
        if self._ui_settings is None:return default
        try:return json.loads(str(self._ui_settings.value(key,"") or ""))
        except (TypeError,ValueError,json.JSONDecodeError):return default

    def _load_saved_views(self):
        values=self._settings_json("tag_encyclopedia/saved_views",[])
        saved=[]
        for value in values if isinstance(values,list) else ():
            if not isinstance(value,dict):continue
            stable_id=str(value.get("id") or "")
            name=str(value.get("name") or "").strip()
            review_view=str(value.get("review_view") or "all")
            if stable_id and name and review_view in self._review_view_labels():
                saved.append({"id":stable_id,"name":name[:80],"query":str(value.get("query") or "")[:500],"review_view":review_view})
        return saved[:24]

    def _load_navigation_history(self):
        values=self._settings_json("tag_encyclopedia/recent_contexts",[])
        contexts=[]
        for value in values if isinstance(values,list) else ():
            context=KnowledgeViewContext.from_mapping(value)
            if context is None:continue
            if context in contexts:contexts.remove(context)
            contexts.append(context)
        return contexts[-24:]

    def _save_workbench_ui_state(self):
        if self._ui_settings is None:return
        self._ui_settings.setValue("tag_encyclopedia/saved_views",json.dumps(self._saved_views,ensure_ascii=False,separators=(",",":")))
        self._ui_settings.setValue("tag_encyclopedia/recent_contexts",json.dumps([value.as_mapping() for value in self._navigation_history[-24:]],ensure_ascii=False,separators=(",",":")))

    def _saved_view(self,view_id):
        view_id=str(view_id or "")
        return next((value for value in self._saved_views if f"saved:{value['id']}"==view_id),None)

    def _active_review_key(self,view_id=None):
        view_id=str(view_id if view_id is not None else self.knowledge_review_view.currentData() or "all")
        saved=self._saved_view(view_id)
        return saved["review_view"] if saved is not None else view_id if view_id in self._review_view_labels() else "all"

    def _capture_navigation_context(self):
        selected_key,membership_ids=self.category_workspace.navigation_context()
        kind,record_id=(selected_key if selected_key else ("",""))
        return KnowledgeViewContext(
            query=self.knowledge_search.text(),review_view_id=str(self.knowledge_review_view.currentData() or "all"),
            selected_kind=str(kind),selected_id=str(record_id),membership_tag_ids=tuple(membership_ids),
            include_related=self.knowledge_related_search.isChecked(),
        )

    def _record_navigation_context(self):
        if self._restoring_context or not hasattr(self,"category_workspace"):return
        context=self._capture_navigation_context()
        if self._navigation_index>=0 and self._navigation_history[self._navigation_index]==context:
            self._save_workbench_ui_state(); self._update_navigation_buttons(); return
        if self._navigation_index<len(self._navigation_history)-1:
            self._navigation_history=self._navigation_history[:self._navigation_index+1]
        self._navigation_history.append(context); self._navigation_history=self._navigation_history[-24:]
        self._navigation_index=len(self._navigation_history)-1
        self._save_workbench_ui_state(); self._update_navigation_buttons()

    def _restore_navigation_context(self,context):
        if context is None:return
        self._restoring_context=True
        try:
            view_id=context.review_view_id
            if self.knowledge_review_view.findData(view_id)<0:view_id="all"
            blocker=QSignalBlocker(self.knowledge_review_view); self.knowledge_review_view.setCurrentIndex(max(0,self.knowledge_review_view.findData(view_id))); del blocker
            search_blocker=QSignalBlocker(self.knowledge_search); self.knowledge_search.setText(context.query); del search_blocker
            related_blocker=QSignalBlocker(self.knowledge_related_search); self.knowledge_related_search.setChecked(context.include_related); del related_blocker
            self._refresh_policy_search(context.query)
            self.category_workspace.search.setText(context.query)
            self._refresh_review_views()
            key=(context.selected_kind,context.selected_id) if context.selected_kind and context.selected_id else None
            self.category_workspace.restore_navigation_context(key,context.membership_tag_ids)
        finally:self._restoring_context=False
        self._save_workbench_ui_state(); self._update_navigation_buttons()

    def _navigate_back(self):
        if self._navigation_index<=0:return
        self._navigation_index-=1; self._restore_navigation_context(self._navigation_history[self._navigation_index])

    def _navigate_forward(self):
        if self._navigation_index>=len(self._navigation_history)-1:return
        self._navigation_index+=1; self._restore_navigation_context(self._navigation_history[self._navigation_index])

    def _navigate_to_recent(self,index):
        if 0<=index<len(self._navigation_history):
            self._navigation_index=index; self._restore_navigation_context(self._navigation_history[index])

    def _update_navigation_buttons(self):
        if not hasattr(self,"knowledge_back_button"):return
        self.knowledge_back_button.setEnabled(self._navigation_index>0)
        self.knowledge_forward_button.setEnabled(0<=self._navigation_index<len(self._navigation_history)-1)
        if hasattr(self,"knowledge_history_back_action"):
            self.knowledge_history_back_action.setEnabled(self.knowledge_back_button.isEnabled())
            self.knowledge_history_forward_action.setEnabled(self.knowledge_forward_button.isEnabled())

    def _context_label(self,context):
        labels=self._review_view_labels(); saved=self._saved_view(context.review_view_id)
        view_label=saved["name"] if saved is not None else labels.get(context.review_view_id,"All tags")
        query=f'“{context.query}”' if context.query else view_label
        record=""
        if context.selected_kind=="category":
            record=self.category_workspace.session.path_label(context.selected_id)
        elif context.selected_kind=="tag":
            tag=self.category_workspace.session._state.tags.get(context.selected_id); record=tag.display if tag is not None else ""
        return " · ".join(value for value in (query,record) if value)[:120]

    def _populate_knowledge_more_menu(self):
        menu=self.knowledge_more_menu; menu.clear()
        find_new=menu.addAction(
            "Find New Knowledge…",self._request_find_new_knowledge,
        )
        find_new.setToolTip(
            "Open a local report over current vocabulary, meaning questions, "
            "organization, and relationship health. This does not reread TXT "
            "or Metadata or contact an online provider."
        )
        menu.addSeparator()
        menu.addAction("Restore Shipped Values…",self.show_default_overrides)
        if self._knowledge_promotion_dialog_type() is not None:
            menu.addAction(
                "Propose Local Changes as Future Defaults…",
                self.show_knowledge_promotion,
            )
        menu.addAction("Manage Tag Colors…",self._open_tag_color_workspace)
        menu.addAction("Manage Relationships…",lambda:self.show_relationship_workspace())
        menu.addAction("Review Removed Vocabulary…",self.show_removed_vocabulary)
        self.review_character_links_action=menu.addAction("Review Character Tag Links…",self.show_character_tag_links)
        menu.addAction("Review Safe Fixes…",self._open_system_knowledge_maintenance)
        menu.addSeparator(); menu.addAction("Save Current View…",self._save_current_view)
        current=str(self.knowledge_review_view.currentData() or "")
        remove=menu.addAction("Delete Current Saved View…",self._remove_current_saved_view); remove.setEnabled(self._saved_view(current) is not None)

    def _request_find_new_knowledge(self):
        if self.has_active_background_work():
            QMessageBox.information(
                self,"System Knowledge maintenance is running",
                "Wait for the current verified System Knowledge operation to "
                "finish before building a local Knowledge report.",
            )
            return False
        self._flush_tag_save()
        saving=bool(
            self._tag_save_pending
            or self._modifier_policy_save_task is not None
            or self.category_workspace.has_unsaved_changes()
            or self.has_unsaved_order_changes()
        )
        if saving:
            QMessageBox.information(
                self,"Wait for System Knowledge",
                "Wait for the current System Knowledge save to finish before "
                "building a report from saved authority.",
            )
            return False
        self.find_new_knowledge_requested.emit()
        return True

    def local_discovery_review_counts(self):
        """Return bounded saved-authority health counts for the local report."""
        view_sets=self._review_view_sets()
        return {
            "needs_organization":len(view_sets["needs_organization"] or ()),
            "missing_relationships":len(view_sets["missing_relationships"] or ()),
        }
    def show_default_overrides(self):
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status(
                "Finish or correct the current Tag edit before comparing local and shipped values.",
                error=True,
            )
            return
        if self._category_staging_active():
            QMessageBox.information(
                self,"Wait for System Knowledge",
                "Finish or save the current Category Workspace change before "
                "comparing local and shipped values.",
            )
            return
        service=DefaultOverrideService(self.editor_context.app_root)
        try:snapshot=service.scan()
        except Exception as error:
            QMessageBox.critical(
                self,"Could Not Review Defaults",
                f"System Knowledge defaults could not be reconciled safely:\n\n{error}",
            )
            return
        if not snapshot.items:
            QMessageBox.information(
                self,"Local and Shipped Values Match",
                "No current fields differ from shipped System Knowledge. "
                "User-created records remain local and are not shipped defaults.",
            )
            return
        dialog=DefaultOverridesDialog(snapshot,self)
        dialog_result=dialog.exec()
        if dialog_result!=QDialog.Accepted:return
        selected=dialog.selected_keys(); selected_set=set(selected)
        selected_items=[item for item in snapshot.items if item.key in selected_set]
        selected_records={(item.kind,item.stable_id) for item in selected_items}
        record_count=len(selected_records)
        affected_items=(
            [
                item for item in snapshot.items
                if (item.kind,item.stable_id) in selected_records
            ]
            if dialog.restore_complete_records else selected_items
        )
        current_only_count=sum(
            1 for item in affected_items if not item.can_reset_setting
        )
        action=(
            "reset every preserved field on the selected records to its shipped value"
            if dialog.restore_complete_records
            else "reset only the selected settings to their shipped values"
        )
        advanced_effect=(
            f" This includes removing {current_only_count:,} current-only "
            "preserved value(s) that have no shipped counterpart."
            if current_only_count else ""
        )
        if QMessageBox.question(
            self,"Restore Shipped Values",
            f"{action.capitalize()} for {record_count:,} System Knowledge "
            f"record{'s' if record_count!=1 else ''} "
            f"({len(affected_items):,} field{'s' if len(affected_items)!=1 else ''})?"
            f"{advanced_effect}\n\n"
            "The exact working Knowledge files are backed up first. Image Knowledge, images, "
            "TXT, Metadata, and raw evidence are unchanged.",
        )!=QMessageBox.Yes:return
        try:
            result=service.restore(
                selected,
                authority_fingerprint=snapshot.authority_fingerprint,
                complete_records=dialog.restore_complete_records,
            )
        except RuleConversionAdmissionError as error:
            QMessageBox.critical(
                self,"Shipped Values Not Restored",
                "Rule recovery must be resolved before restoring shipped values. "
                f"No restore changes or backups were made.\n\n{error}",
            )
            return
        except Exception as error:
            QMessageBox.critical(
                self,"Shipped Values Not Restored",
                "Restoration was not verified. Resolve the error and reopen this review "
                f"before retrying. Any created recovery backups were kept.\n\n{error}",
            )
            return
        self.tags.load(); self.categories.load(); self.patterns.load(); self.characters.load()
        self.tags.apply_category_ordering(self.categories)
        self._tag_view_cache=None
        self.category_workspace.reload_from_live()
        self.refresh_all(reload=False)
        self.state.events.publish("knowledge_changed")
        QMessageBox.information(
            self,"Shipped Values Restored",
            f"Restored shipped values for {result.restored_fields:,} field(s) across "
            f"{result.restored_records:,} record(s). A verified recovery backup was kept.",
        )

    def _knowledge_promotion_dialog_type(self):
        """Optional original maintainer capability, outside selected editing.

        Public distributions deliberately omit this plugin. Resolve it only in
        the original page; its private dependency graph is not editor runtime.
        An installed plugin's own import errors must remain visible.
        """
        if self.editor_context.standalone:
            return None
        from importlib import import_module
        module_name=__package__+".knowledge_promotion_dialog"
        try:
            module=import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name!=module_name:
                raise
            return None
        return module.KnowledgePromotionDialog

    def show_knowledge_promotion(self):
        """Open the private future-default proposal independently of reset review."""
        dialog_type=self._knowledge_promotion_dialog_type()
        if dialog_type is None:
            return
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status(
                "Finish or correct the current Tag edit before proposing future defaults.",
                error=True,
            )
            return
        if self._category_staging_active():
            QMessageBox.information(
                self,"Wait for System Knowledge",
                "Finish or save the current Category Workspace change before "
                "proposing future defaults.",
            )
            return
        dialog=dialog_type(self.editor_context.app_root,self)
        if not dialog.candidates:
            QMessageBox.information(
                self,"No Future Default Candidates",
                "No eligible local System Knowledge changes currently differ from "
                "the shipped defaults.",
            )
            return
        if dialog.exec()!=QDialog.Accepted:return
        self.tags.load(); self.categories.load(); self.patterns.load(); self.characters.load()
        self.tags.apply_category_ordering(self.categories)
        self._tag_view_cache=None
        self.category_workspace.reload_from_live()
        self.refresh_all(reload=False)
        self.state.events.publish("knowledge_changed")

    def _populate_history_menu(self):
        self._update_navigation_buttons()
        recent=self.knowledge_recent_menu; recent.clear()
        seen=set()
        for index in range(len(self._navigation_history)-1,max(-1,len(self._navigation_history)-9),-1):
            context=self._navigation_history[index]; label=self._context_label(context)
            if not label or label in seen:continue
            seen.add(label); recent.addAction(label,lambda _checked=False,value=index:self._navigate_to_recent(value))
        if not seen:
            empty=recent.addAction("No recent views"); empty.setEnabled(False)

    def _save_current_view(self):
        name,accepted=QInputDialog.getText(self,"Save current view","View name")
        name=str(name).strip()
        if not accepted or not name:return
        current=str(self.knowledge_review_view.currentData() or "all")
        existing=next((value for value in self._saved_views if value["name"].casefold()==name.casefold()),None)
        payload={
            "id":existing["id"] if existing is not None else uuid.uuid4().hex,
            "name":name[:80],"query":self.knowledge_search.text()[:500],"review_view":self._active_review_key(current),
        }
        if existing is not None:self._saved_views[self._saved_views.index(existing)]=payload
        else:self._saved_views.append(payload)
        saved_id=f"saved:{payload['id']}"; self._refresh_review_views()
        blocker=QSignalBlocker(self.knowledge_review_view); self.knowledge_review_view.setCurrentIndex(self.knowledge_review_view.findData(saved_id)); del blocker
        self._save_workbench_ui_state()
        if self._ui_settings is not None:self._ui_settings.sync()
        self._record_navigation_context()

    def _remove_current_saved_view(self):
        current=str(self.knowledge_review_view.currentData() or "")
        saved=self._saved_view(current)
        if saved is None:return
        if QMessageBox.question(self,"Delete saved view?",f"Delete the saved view “{saved['name']}”? System Knowledge will not change.")!=QMessageBox.Yes:return
        self._saved_views.remove(saved)
        blocker=QSignalBlocker(self.knowledge_review_view); self.knowledge_review_view.setCurrentIndex(max(0,self.knowledge_review_view.findData(saved["review_view"]))); del blocker
        self._refresh_review_views(); self._save_workbench_ui_state()
        if self._ui_settings is not None:self._ui_settings.sync()
        self._record_navigation_context()

    def _detach_saved_view(self,*_):
        current=str(self.knowledge_review_view.currentData() or "")
        saved=self._saved_view(current)
        if saved is None:return
        blocker=QSignalBlocker(self.knowledge_review_view); self.knowledge_review_view.setCurrentIndex(max(0,self.knowledge_review_view.findData(saved["review_view"]))); del blocker
        self._refresh_review_views()

    def _review_view_sets(self):
        return _knowledge_review_view_sets(
            self.category_workspace.session,self.tags,self.sensitivity_proposals,
        )

    def _refresh_review_views(self,*,view_sets=None):
        if not hasattr(self,"knowledge_review_view") or not hasattr(self,"category_workspace"):return
        current=str(self.knowledge_review_view.currentData() or "all")
        view_sets=view_sets if view_sets is not None else self._review_view_sets()
        labels=self._review_view_labels()
        total=len(self.category_workspace.session._state.tags)
        blocker=QSignalBlocker(self.knowledge_review_view); self.knowledge_review_view.clear()
        for key in ("all","needs_organization","sensitivity","missing_relationships"):
            count=total if key=="all" else len(view_sets[key] or ())
            self.knowledge_review_view.addItem(f"{labels[key]} ({count:,})",key)
        if self._saved_views:
            self.knowledge_review_view.insertSeparator(self.knowledge_review_view.count())
            for saved in sorted(self._saved_views,key=lambda value:value["name"].casefold()):
                self.knowledge_review_view.addItem(f"Saved: {saved['name']}",f"saved:{saved['id']}")
        self.knowledge_review_view.setCurrentIndex(max(0,self.knowledge_review_view.findData(current))); del blocker
        key=self._active_review_key()
        self.category_workspace.set_review_filter(key,view_sets[key],labels[key])

    def _review_view_changed(self,*_):
        current=str(self.knowledge_review_view.currentData() or "")
        saved=self._saved_view(current)
        if saved is not None and self.knowledge_search.text()!=saved["query"]:
            self._restoring_context=True; self.knowledge_search.setText(saved["query"]); self._restoring_context=False
        self._refresh_review_views()
        self._record_navigation_context()

    def _activate_review_view(self,key):
        index=self.knowledge_review_view.findData(key)
        if index>=0:self.knowledge_review_view.setCurrentIndex(index)

    def open_review_view(self,key,scope=None):
        """Open one stable review destination without depending on tab positions."""
        if (
            hasattr(self,"system_knowledge_maintenance")
            and self.system_knowledge_maintenance.has_active_background_work()
        ):
            QMessageBox.information(
                self,"System Knowledge maintenance is running",
                "Wait for the current System Knowledge scan or verified write "
                "to finish before opening another Knowledge review view.",
            )
            return False
        if hasattr(self,"workspace_stack"):
            self.workspace_stack.setCurrentWidget(self.workbench)
        scope=scope if isinstance(scope,dict) else {}
        if key=="category_base_candidate":
            context=KnowledgeViewContext.from_mapping({
                **scope,"review_view_id":"all","query":"",
            })
            if context is None:return False
            self._restore_navigation_context(context)
            self.category_workspace.set_category_base_review_context(scope)
            QTimer.singleShot(
                0,self.category_workspace._focus_category_details,
            )
            self._record_navigation_context()
            return True
        if (
            scope.get("origin") in {
                "automation","system_knowledge_maintenance",
            }
            and str(scope.get("selected_kind") or "")
            and str(scope.get("selected_id") or "")
        ):
            context=KnowledgeViewContext.from_mapping({
                **scope,
                "review_view_id":str(
                    key or scope.get("review_view_id") or "all"
                ),
            })
            self._restore_navigation_context(context)
            self._record_navigation_context()
            return True
        query=scope.get("query")
        if query is not None:
            self.knowledge_search.setText(str(query))
        self._activate_review_view(str(key or "all"))
        self.category_workspace.refresh_structure()
        self._record_navigation_context()
        return True

    def _maintenance_scope(self):
        context=self._capture_navigation_context()
        return {
            "kind":"system_knowledge",
            "origin":"tag_encyclopedia",
            "query":context.query,
            "review_view_id":context.review_view_id,
            "selected_kind":context.selected_kind,
            "selected_id":context.selected_id,
            "membership_tag_ids":list(context.membership_tag_ids),
            "include_related":context.include_related,
        }

    def _open_system_knowledge_maintenance(self):
        self.open_function(
            "knowledge.system_knowledge.safe_fixes",self._maintenance_scope()
        )

    def _system_knowledge_maintenance_block_reason(self):
        if self._tag_save_pending:
            return (
                "Finish or correct the current SDXL Tag edit before applying "
                "System Knowledge maintenance."
            )
        if self._modifier_policy_save_task is not None:
            return (
                "Wait for the current reusable-policy save to finish before "
                "applying System Knowledge maintenance."
            )
        if self.category_workspace.has_unsaved_changes():
            return (
                "Wait for the current Category Workspace save to finish before "
                "applying System Knowledge maintenance."
            )
        if self.has_unsaved_order_changes():
            return (
                "Wait for the current caption-order save to finish before "
                "applying System Knowledge maintenance."
            )
        return ""

    def open_function(self,function_id,scope=None):
        """Open one stable Tag Encyclopedia destination without starting work."""
        function_id=str(function_id or "")
        if function_id not in {
            "knowledge.system_knowledge.safe_fixes",
        }:
            return False
        if self.has_active_background_work():
            QMessageBox.information(
                self,"System Knowledge maintenance is running",
                "Wait for the current System Knowledge scan or verified write "
                "to finish before opening another maintenance handoff.",
            )
            return False
        self._flush_tag_save()
        blocked=self._system_knowledge_maintenance_block_reason()
        if blocked:
            QMessageBox.information(self,"Wait for System Knowledge",blocked)
            return False
        scope=dict(scope) if isinstance(scope,dict) else {}
        navigation_keys={
            "query","review_view_id","selected_kind","selected_id",
            "membership_tag_ids","include_related",
        }
        if navigation_keys.intersection(scope):
            context=KnowledgeViewContext.from_mapping(scope)
            self._restore_navigation_context(context)
        self._maintenance_return_context=self._capture_navigation_context()
        self.system_knowledge_maintenance.open_scope(function_id,scope)
        self.workspace_stack.setCurrentWidget(self.system_knowledge_maintenance)
        self.header.set_status(
            "Review before apply • Image-facing data unchanged"
        )
        return True

    def _return_from_system_knowledge_maintenance(self):
        self.workspace_stack.setCurrentWidget(self.workbench)
        context=self._maintenance_return_context
        self._maintenance_return_context=None
        if context is not None:
            self._restore_navigation_context(context)
            self._record_navigation_context()
        self.header.set_status("Category edits save automatically · Tag Details uses Save Tag")

    def _open_maintenance_review_view(self,review_view_id,scope):
        if self.open_review_view(review_view_id,scope):
            self._maintenance_return_context=None
            self.header.set_status("Category edits save automatically · Tag Details uses Save Tag")

    def _system_knowledge_maintenance_changed(self):
        if not self.category_workspace.has_unsaved_changes():
            self.category_workspace.reload_from_live()
        self._refresh_review_views()
        if self.tag_scope_page is not None:
            self.refresh_tags()
        self.state.events.publish("knowledge_changed")
        self.maintenance_applied.emit()

    def has_active_background_work(self):
        from .history_workflow import history_dialogs_busy
        workspace=getattr(self,"category_workspace",None)
        return bool(self._tag_save_task is not None or getattr(self,"_tag_identity_setup_task",None) is not None
            or (hasattr(self,"system_knowledge_maintenance") and self.system_knowledge_maintenance.has_active_background_work())
            or (workspace is not None and workspace.has_active_tag_removal()) or history_dialogs_busy(self))

    def request_background_shutdown(self):
        self._category_refresh_shutdown=True
        for refresh in getattr(self,"_identity_refresh_tasks",set()):
            refresh.cancel(); self._release_category_refresh_callbacks(refresh)
        getattr(self,"_identity_refresh_tasks",set()).clear()
        task=getattr(self,"_category_refresh_task",None)
        self._category_refresh_task=None
        if task is not None:
            task.cancel()
            self._release_category_refresh_callbacks(task)
        from .history_workflow import close_history_dialogs
        history_busy=close_history_dialogs(self)
        workspace=getattr(self,"category_workspace",None)
        removal_busy=workspace.request_tag_removal_shutdown() if workspace is not None else False
        maintenance_busy=(self.system_knowledge_maintenance.request_background_shutdown()
                          if hasattr(self,"system_knowledge_maintenance") else False)
        return bool(history_busy or removal_busy or maintenance_busy)

    def allow_external_knowledge_action(self,action):
        if getattr(self,"_category_refresh_pending",False):
            QMessageBox.information(
                self,"Tag workspace is updating",
                "Wait for the saved Tag workspace to finish updating, then "
                f"try {str(action or 'editing System Knowledge')} again.",
            )
            return False
        if not self.has_active_background_work():
            return True
        QMessageBox.information(
            self,"System Knowledge maintenance is running",
            "Wait for the current verified System Knowledge operation to "
            f"finish before {str(action or 'editing System Knowledge')}.",
        )
        return False

    def _new_workbench_record(self):
        self.category_workspace.new_category()

    def _new_tag_from_canvas(self):
        if self._category_staging_active():
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Create the SDXL Tag after it finishes."); return
        self.new_tag(); self._show_tag_editor_dialog("New SDXL Tag")

    def _add_tags_to_category(self,category_id):
        workspace=self.category_workspace; category_id=str(category_id)
        if workspace.has_unsaved_changes():
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Add or create the SDXL Tag after it finishes."); return
        if workspace.session.get(category_id) is None:
            QMessageBox.warning(self,"Category changed","The destination category is no longer available. Select a category and try again."); return
        rules=AutomationRuleRepository(self.editor_context.defaults/"automation_rules.json",self.editor_context.user/"automation_rules.json").combined()
        ignored_names=tuple(str(record.get("tag") or "") for record in rules.get("ignore_rules",()) if str(record.get("tag") or "").strip())
        dialog=AddTagToCategoryDialog(workspace.session.all_tags(),workspace.session,category_id,ignored_names=ignored_names,removed_tag_ids=workspace.session._state.removed_tag_ids,knowledge=self.tags,parent=self)
        outcome=dialog.exec()
        if outcome==QDialog.Accepted:
            selected=dialog.selected_tag_ids()
            if selected:workspace.apply_tag_category_change(selected,category_id,selected_key=("category",category_id))
        elif outcome==dialog.CREATE_RESULT:self._new_tag_in_category(category_id,dialog.query())
        elif outcome==dialog.RESTORE_RESULT:
            stable_id=dialog.restored_tag_id()
            if stable_id and workspace.reload_from_live():
                workspace.apply_tag_category_change(
                    (stable_id,),category_id,selected_key=("category",category_id),
                )

    def _new_tag_in_category(self,category_id,name):
        workspace=self.category_workspace; category=workspace.session.get(category_id); name=str(name).strip()
        if workspace.has_unsaved_changes() or category is None or not name:
            QMessageBox.information(self,"Tag cannot be created","Choose an available category and a non-empty name after the current automatic save finishes."); return
        self.new_tag(); self.tag_name.setText(name); self._fill_category_combo(self.tag_category,category_id); self._suggest_order_group()
        self._set_tag_save_status(f"Ready to create in {workspace.session.path_label(category_id)}. Nothing is saved until you choose Create SDXL Tag.")
        self._show_tag_editor_dialog(f"New SDXL Tag — {name}")

    def create_tag_from_image(self,name,source_editor):
        """Create one reusable tag, then return its stable identity to an image editor."""
        name=str(name).strip()
        self._tag_dialog_parent=source_editor
        self.new_tag()
        self.tag_name.setText(name)
        self._fill_category_combo(self.tag_category,"uncategorized")
        self._suggest_order_group()
        self._set_tag_save_status(
            "Complete the reusable tag details, then choose Create SDXL Tag. "
            "The System Knowledge record saves independently; adding it to the "
            "image remains staged until Save Changes."
        )
        self._show_tag_editor_dialog(f"New SDXL Tag — {name}" if name else "New SDXL Tag")

    def create_tag_for_rule(self,name,rule_editor):
        """Create one canonical Tag while retaining the separate Rule draft."""
        name=str(name).strip()
        if not name or rule_editor is None or not isValid(rule_editor):
            return False
        if (self._tag_save_pending or self._tag_save_task is not None
            or self.category_workspace.has_unsaved_changes()
            or (getattr(self,"_active_tag_editor_dialog",None) is not None
                and isValid(self._active_tag_editor_dialog))):
            rule_editor.rule_tag_creation_cancelled(
                "Finish the current Tag or Category draft first. The rule draft is unchanged."
            )
            return False
        self.tags.load()
        if self.tags.find(name) is not None:
            rule_editor.rule_tag_creation_cancelled(
                "This Tag already exists. Review the unchanged rule draft and save it separately."
            )
            return True
        self._tag_dialog_parent=rule_editor
        self.new_tag()
        if self.current_tag_id:
            self._tag_dialog_parent=None
            rule_editor.rule_tag_creation_cancelled(
                "Finish the current Tag edit before creating another. The rule draft is unchanged."
            )
            return False
        self.tag_name.setText(name)
        self._fill_category_combo(self.tag_category,"uncategorized")
        self._suggest_order_group()
        self._set_tag_save_status(
            "Choose a category, then Create SDXL Tag. This saves System Knowledge "
            "independently; the Rule draft remains unsaved until you return and Save Automatic Rule."
        )
        self._show_tag_editor_dialog(f"New SDXL Tag — {name}",return_to_rule=rule_editor)
        return True

    def _update_workbench_staged_state(self,staged):
        if not hasattr(self,"knowledge_staged_status"):return
        staged=bool(staged); text="Saving System Knowledge…" if staged else "Category edits save automatically · Tag Details uses Save Tag"
        self.header.set_status(text); self.knowledge_staged_status.setAccessibleName(text)
        if hasattr(self,"knowledge_new_button"):self.knowledge_new_button.setEnabled(True)
        self._tag_view_cache=None

    def _open_tag_details(self,stable_id,dialog_parent=None,*,initial_tab="",initial_relationship_task=""):
        self._ensure_tag_editor()
        self._tag_dialog_parent=dialog_parent
        if self._category_staging_active():
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Edit the SDXL Tag after it finishes."); return
        index=self.tag_model.index_for_id(stable_id)
        if not index.isValid():
            record=self._tag_record_map().get(stable_id)
            if record is None:return
            self.tag_search.setText(record.display); self.refresh_tags(); index=self.tag_model.index_for_id(stable_id)
        if not index.isValid():return
        selection=self.tag_list.selectionModel()
        with QSignalBlocker(selection):
            selection.clearSelection(); selection.select(index,QItemSelectionModel.Select|QItemSelectionModel.Rows); selection.setCurrentIndex(index,QItemSelectionModel.NoUpdate)
        self.show_tag(index); self._tag_selection_changed(); self.tag_list.scrollTo(index)
        if initial_tab:
            for tab_index in range(self.tag_editor_tabs.count()):
                if self.tag_editor_tabs.tabText(tab_index)==initial_tab:
                    self.tag_editor_tabs.setCurrentIndex(tab_index)
                    break
        if initial_tab=="Relationships" and initial_relationship_task:
            for task_index in range(self.tag_relationship_tasks.count()):
                if self.tag_relationship_tasks.tabText(task_index)==initial_relationship_task:
                    self.tag_relationship_tasks.setCurrentIndex(task_index)
                    break
        record=self.tags.tags.get(stable_id); self._show_tag_editor_dialog(f"Edit SDXL Tag — {record.display}" if record is not None else "Edit SDXL Tag")

    def _open_category_appearance(self,category_id):
        category=self.categories.get(str(category_id or ""))
        if category is None:return
        self._open_tag_color_workspace(category.id,initial_scope="category")

    def _active_color_category_id(self):
        record=self.tags.tags.get(str(getattr(self,"current_tag_id","") or ""))
        if record is not None:return str(record.category or "")
        workspace=getattr(self,"category_workspace",None)
        selected=getattr(workspace,"selected_key",None)
        if selected and selected[0]=="category":return str(selected[1])
        return ""

    def _open_tag_color_workspace(self,category_id="",*,initial_scope="category"):
        selected=str(category_id or self._active_color_category_id())
        dialog=TagColorWorkspaceDialog(
            self.tags,self.categories,category_id=selected,initial_scope=initial_scope,parent=self,
        )
        screen=dialog.screen() or QApplication.primaryScreen()
        if screen is not None:
            available=screen.availableGeometry(); dialog.resize(
                QSize(min(960,available.width()),min(760,available.height()))
            )
        dialog.exec()

    def _navigate_tag_reference(self,stable_id):
        record=self.tags.tags.get(stable_id)
        if record is None:return
        index=self.tag_model.index_for_id(stable_id)
        if not index.isValid():
            self.tag_search.setText(record.display); self.refresh_tags(); index=self.tag_model.index_for_id(stable_id)
        if not index.isValid():return
        selection=self.tag_list.selectionModel()
        with QSignalBlocker(selection):
            selection.clearSelection(); selection.select(index,QItemSelectionModel.Select|QItemSelectionModel.Rows); selection.setCurrentIndex(index,QItemSelectionModel.NoUpdate)
        self.show_tag(index); self._tag_selection_changed()
        dialog=getattr(self,"_active_tag_editor_dialog",None)
        if dialog is not None:dialog.setWindowTitle(f"Edit SDXL Tag — {record.display}")
        self._set_tag_save_status(f"Opened linked SDXL Tag: {record.display}.")

    def _set_tag_basics_row_visible(self,field,visible):
        field.setVisible(bool(visible))
        label=self.tag_basics_form.labelForField(field)
        if label is not None:label.setVisible(bool(visible))

    def _character_creation_alias_value(self,record):
        source=self.characters.series.get(
            self.characters.character_source_id("character",record.id)
        )
        if source is None:return ""
        value=f"{record.name} from {source.name}"
        return "" if normalize_tag(value)==normalize_tag(self.tag_name.text()) else value

    def _matching_character_creation_records(self):
        key=normalize_tag(self.tag_name.text())
        return tuple(sorted((
            record for record in self.characters.characters.values()
            if key and key in {
                normalize_tag(value)
                for value in (record.name,*record.aliases)
                if normalize_tag(value)
            }
        ),key=lambda record:(record.name.casefold(),record.id)))

    def _refresh_character_creation_alias_only(self,*_):
        if not hasattr(self,"tag_character_creation_alias") or getattr(self,"_refreshing_character_creation",False):
            return
        action=str(self.tag_character_creation_action.currentData() or "tag_only")
        record=self.characters.characters.get(str(self.tag_character_creation_existing.currentData() or ""))
        alias_value=self._character_creation_alias_value(record) if record is not None else ""
        record_id=str(getattr(record,"id","") or "")
        if getattr(self,"_tag_character_alias_record_id","")!=record_id or not alias_value:
            self.tag_character_creation_alias.setChecked(False)
        self._tag_character_alias_record_id=record_id
        self.tag_character_creation_alias.setText(f'Add alias “{alias_value}”' if alias_value else "No additional character/source alias is needed")
        self._set_tag_basics_row_visible(
            self.tag_character_creation_alias,
            bool(action=="link_existing" and record is not None and alias_value),
        )
        if action=="create_record" and not self.tag_character_creation_group.isHidden():
            self.tag_character_creation_status.setText(
                "Existing typed Character Knowledge matches this name. Choose Link an existing character and review its Character Source, or keep the reusable Tag only."
                if self._matching_character_creation_records() else
                "After the reusable Tag is verified, a separate Create Character Record editor will open with Character Source, identity type, aliases, and reusable knowledge. Cancelling that editor keeps the Tag only."
            )
        self._update_tag_create_dialog_action()

    def _refresh_character_creation_decision(self,*_):
        if not hasattr(self,"tag_character_creation_group") or getattr(self,"_refreshing_character_creation",False):
            return
        category_id=self._selected_category_id()
        resolver=self._active_category_resolver()
        role=resolver.effective_semantic_role(category_id) if category_id and hasattr(resolver,"effective_semantic_role") else ""
        creating=not bool(self.current_tag_id)
        visible=bool(creating and role=="character_identity")
        self.tag_character_creation_group.setVisible(visible)
        if not visible:
            self._update_tag_create_dialog_action()
            return
        self._refreshing_character_creation=True
        try:
            selected_id=str(self.tag_character_creation_existing.currentData() or "")
            records=sorted(
                self.characters.characters.values(),
                key=lambda record:(record.name.casefold(),record.id),
            )
            signature=tuple(
                (
                    record.id,record.name,
                    self.characters.character_source_id("character",record.id),
                    tuple(record.aliases),
                    str(self.characters.effective_knowledge_tag("character",record.id).get("status") or ""),
                    self.characters.entity_tag_id("character",record.id),
                    self.characters.series.get(
                        self.characters.character_source_id("character",record.id)
                    ).name
                    if self.characters.series.get(
                        self.characters.character_source_id("character",record.id)
                    ) is not None else "",
                )
                for record in records
            )
            if signature!=getattr(self,"_tag_character_choices_signature",None):
                blocker=QSignalBlocker(self.tag_character_creation_existing)
                self.tag_character_creation_existing.clear()
                self.tag_character_creation_existing.addItem("Choose an existing typed character…","")
                for record in records:
                    source=self.characters.series.get(
                        self.characters.character_source_id("character",record.id)
                    )
                    source_label=source.name if source is not None else "No Character Source"
                    link=self.characters.effective_knowledge_tag("character",record.id)
                    linked_tag=self.tags.tags.get(str(link.get("tag_id") or "")) if link.get("status")=="reviewed" else None
                    state=f"linked to {linked_tag.display}" if linked_tag is not None else "available to link"
                    alias_label=f" ({', '.join(record.aliases)})" if record.aliases else ""
                    label=f"{record.name}{alias_label} — {source_label} — {state}"
                    self.tag_character_creation_existing.addItem(label,record.id)
                    index=self.tag_character_creation_existing.count()-1
                    aliases=_join(record.aliases) or "No aliases"
                    self.tag_character_creation_existing.setItemData(
                        index,
                        f"Character: {record.name}\nCharacter Source: {source_label}\nAliases: {aliases}\nStatus: {state}",
                        Qt.ToolTipRole,
                    )
                index=self.tag_character_creation_existing.findData(selected_id)
                self.tag_character_creation_existing.setCurrentIndex(index if index>=0 else 0)
                self._tag_character_choices_signature=signature
                del blocker
            action=str(self.tag_character_creation_action.currentData() or "tag_only")
            record=self.characters.characters.get(str(self.tag_character_creation_existing.currentData() or ""))
            show_existing=action=="link_existing"
            self._set_tag_basics_row_visible(self.tag_character_creation_existing,show_existing)
            self._refreshing_character_creation=False
            self._refresh_character_creation_alias_only()
            self._refreshing_character_creation=True
            if action=="automatic":
                status="After the Tag is saved, reuse its reviewed link, link one exact matching record, or create a minimal record. Ambiguous matches stay for review; missing facts are not guessed."
            elif action=="tag_only":
                status="Only the reusable Character Identity Tag will be created. No typed character is linked or created."
            elif action=="create_record":
                matches=self._matching_character_creation_records()
                status=(
                    "Existing typed Character Knowledge matches this name. Choose Link an existing character and review its Character Source, or keep the reusable Tag only."
                    if matches else
                    "After the reusable Tag is verified, a separate Create Character Record editor will open with Character Source, identity type, aliases, and reusable knowledge. Cancelling that editor keeps the Tag only."
                )
            elif record is None:
                status="Choose an existing typed character. Search includes preferred names, declared aliases, and Character Source names."
            else:
                source=self.characters.series.get(
                    self.characters.character_source_id("character",record.id)
                )
                link=self.characters.effective_knowledge_tag("character",record.id)
                linked_tag=self.tags.tags.get(str(link.get("tag_id") or "")) if link.get("status")=="reviewed" else None
                if linked_tag is not None:
                    status=f'“{record.name}” is already linked to “{linked_tag.display}”. Choose Use Existing Tag to return it to the image without creating a second reviewed identity link.'
                else:
                    status=f'“{record.name}” ({source.name if source is not None else "No Character Source"}) will receive the new Tag as its reviewed stable identity link.'
            self.tag_character_creation_status.setText(status)
        finally:
            self._refreshing_character_creation=False
        self._update_tag_create_dialog_action()

    def _character_creation_decision(self):
        category_id=self._selected_category_id()
        resolver=self._active_category_resolver()
        role=resolver.effective_semantic_role(category_id) if category_id and hasattr(resolver,"effective_semantic_role") else ""
        if role=="character_source":return {"action":"automatic"},""
        if role!="character_identity":return {"action":"tag_only"},""
        action=str(self.tag_character_creation_action.currentData() or "tag_only")
        if action=="create_record":
            matches=self._matching_character_creation_records()
            if matches:
                names=", ".join(
                    f'{record.name} ({self.characters.series[source_id].name})'
                    if (
                        source_id:=self.characters.character_source_id(
                            "character",record.id
                        )
                    ) in self.characters.series else record.name
                    for record in matches[:3]
                )
                return {},f"Existing typed Character Knowledge matches this name: {names}. Choose Link an existing character, or keep the reusable Tag only."
            return {"action":action},""
        if action!="link_existing":return {"action":action},""
        record_id=str(self.tag_character_creation_existing.currentData() or "")
        record=self.characters.characters.get(record_id)
        if record is None:
            return {},"Choose an existing typed character, or keep the reusable Tag only."
        link=self.characters.effective_knowledge_tag("character",record.id)
        linked_tag=self.tags.tags.get(str(link.get("tag_id") or "")) if link.get("status")=="reviewed" else None
        if linked_tag is not None:
            return {"action":"use_existing","tag_id":linked_tag.id},""
        alias=self._character_creation_alias_value(record) if self.tag_character_creation_alias.isChecked() else ""
        conflict=self.tags.conflicting_name((alias,)) if alias else None
        if conflict is not None:
            return {},f'The suggested alias already identifies “{conflict.display}”. Clear the alias choice or open that existing Tag.'
        return {"action":action,"record_id":record.id,"alias":alias},""

    def _update_tag_create_dialog_action(self,*_):
        button=getattr(self,"_tag_create_dialog_action",None)
        if button is None:return
        action=str(self.tag_character_creation_action.currentData() or "tag_only") if not self.tag_character_creation_group.isHidden() else "tag_only"
        existing=self.tags.find(self.tag_name.text().strip())
        decision,_error=self._character_creation_decision()
        if existing is not None or decision.get("action")=="use_existing":
            target=existing or self.tags.tags.get(decision.get("tag_id"))
            button.setText("Use Existing Tag")
            button.setToolTip(f'Use the exact saved Tag "{target.display}"; no duplicate Tag or character is created.')
            return
        button.setToolTip("")
        button.setText({
            "link_existing":"Create Tag and Link Character",
            "create_record":"Create Tag and Continue",
        }.get(action,"Create SDXL Tag"))

    def _complete_character_creation_decision(self,decision,record,parent):
        action=str(decision.get("action") or "tag_only")
        self._character_creation_link_failed=False
        if action in {"tag_only","use_existing"}:return "Existing reusable Tag selected; no new record was created." if action=="use_existing" else ""
        if action=="link_existing":
            try:
                bridge=CharacterTagBridge(self.tags,self.characters,self.categories)
                bridge.apply_reviewed_link("character",str(decision.get("record_id") or ""),record.id)
            except (OSError,RuntimeError,ValueError) as error:
                self._character_creation_link_failed=True
                QMessageBox.warning(parent,"Character link was not saved",f'The reusable SDXL Tag was created, but the typed character link was left unchanged.\n\n{error}')
                return f"SDXL Tag created; Character link not saved — {error}"
            try:
                self._inline_character_record_saved("Characters",str(decision.get("record_id") or ""))
            except (OSError,RuntimeError,ValueError) as error:
                return f"Tag and reviewed Character link are saved; refreshing their presentation needs attention: {error}. Returning retries the editor refresh without creating another record."
            alias=str(decision.get("alias") or "")
            alias_warning=""
            alias_completion=""
            if alias:
                try:
                    if self._tag_alias_service is None:
                        raise RuntimeError(
                            self._tag_alias_error or "Canonical Other Name authority is unavailable."
                        )
                    aliases=TagCollection([
                        *self._tag_alias_service.effective_for_tag(record.id).aliases,
                        alias,
                    ]).to_list()
                    resolution=self._tag_alias_service.resolve(alias)
                    other_owners=tuple(
                        owner_id for owner_id in resolution.owner_ids
                        if owner_id!=record.id
                    )
                    moves=()
                    if other_owners:
                        movable=bool(
                            len(other_owners)==1
                            and other_owners[0] in self.tags.tags
                            and normalize_tag(alias)!=normalize_tag(
                                self.tags.tags[other_owners[0]].display
                            )
                        )
                        action=self._review_alias_collision(
                            alias,record,other_owners,movable=movable,
                        )
                        if action!="move":
                            alias_warning=(
                                " The Character link was saved, but the optional "
                                "Other Name remains with its current identity."
                            )
                            raise LookupError(alias_warning.strip())
                        moves=(TagAliasMove(alias,other_owners[0]),)
                    if not self._confirm_alias_additions(record,(alias,)):
                        alias_warning=(
                            " The Character link was saved, but the optional "
                            "Other Name was cancelled."
                        )
                        raise LookupError(alias_warning.strip())
                    self._tag_alias_service.apply((TagAliasChange(
                        record.id,tuple(aliases),moves=moves,source="qt_character_link",
                        reason="Optional reviewed Character name",
                    ),))
                    self._reload_modifier_roles()
                except LookupError:
                    pass
                except (OSError,RuntimeError,ValueError) as error:
                    self.tags.load(); self._reload_tag_aliases(); alias_warning=f" The Character link was saved, but the optional alias was not — {error}"
                    QMessageBox.warning(parent,"Character alias was not saved",f'The reviewed Character link was saved, but the optional alias was left unchanged.\n\n{error}')
                else:
                    self._loading_tag_editor=True; self.tag_aliases.set_values(self._tag_aliases(record)); self._loading_tag_editor=False
                    self._queue_identity_dependency_refresh((record.id,*other_owners),(alias,))
                    classification=classify_system_knowledge_change(
                        "tag_aliases",label=(
                            f'Saved optional Character Other Name for "{record.display}"'
                        ),
                        affected_tag_ids=(record.id,),source_names=(alias,),
                        matching_images=True,
                    )
                    KnowledgePage._record_downstream_classification(
                        self,classification,
                    )
                    alias_completion=" "+classification.completion_summary()
                    source_editor=getattr(self,"_active_tag_source_editor",None)
                    if source_editor is not None:source_editor.refresh_system_knowledge()
            linked=self.characters.characters.get(str(decision.get("record_id") or ""))
            result=f'SDXL Tag created and linked to typed character “{linked.name}”.' if linked is not None else "SDXL Tag created and linked to the selected typed character."
            return result+alias_warning+alias_completion
        try:
            character_dialog=self._character_editor_factory(
                CharacterRecordEditService(self.characters,self.tags,self.categories),
                self.tags,self.categories,kind="Characters",tag_id=record.id,
                create=True,parent=parent,
            )
        except (OSError,RuntimeError,ValueError) as error:
            QMessageBox.warning(parent,"Character record cannot be created",f'The reusable SDXL Tag was created, but the Character Record editor could not open.\n\n{error}')
            return f"SDXL Tag created; Character Record not created — {error}"
        character_dialog.exec()
        if not character_dialog.saved:
            return "SDXL Tag created. Character Record creation was cancelled; the Tag remains reusable without a typed character link."
        self._inline_character_record_saved("Characters",character_dialog.record_id)
        linked=self.characters.characters.get(character_dialog.record_id)
        return f'SDXL Tag and typed character “{linked.name}” created.' if linked is not None else "SDXL Tag and typed character created."

    def _set_tag_editor_creation_mode(self,creating,*,dialog_controls=False):
        creating=bool(creating)
        self._set_tag_basics_row_visible(self.tag_semantic_role,not creating)
        self._set_tag_basics_row_visible(self.tag_order_summary_row,not creating)
        self.tag_gallery.setVisible(not creating)
        for index in range(self.tag_editor_tabs.count()):
            if index==0:self.tag_editor_tabs.setTabVisible(index,True)
            elif index!=self.tag_identity_tab_index:self.tag_editor_tabs.setTabVisible(index,not creating)
            elif creating:self.tag_editor_tabs.setTabVisible(index,False)
        self.tag_editor_tabs.tabBar().setVisible(not creating)
        self.tag_create_button.setVisible(creating and not dialog_controls)
        if not creating:self.tag_creation_role_help.hide()
        else:self._refresh_tag_semantic_role()
        self._refresh_character_creation_decision()

    def _show_tag_editor_dialog(self,title,*,return_to_rule=None):
        opening_session=self.category_workspace.session
        opening_tag_selection=tuple(record.id for record in self.category_workspace._membership_records())
        try:opening_snapshot=opening_session.reload_source_snapshot()
        except OSError:opening_snapshot=None
        dialog_parent=getattr(self,"_tag_dialog_parent",None)
        return_editor=dialog_parent if hasattr(dialog_parent,"complete_system_tag_creation") else None
        rule_editor=return_to_rule if return_to_rule is not None and isValid(return_to_rule) else None
        rule_tag_saved=[False]
        source_editor=(
            dialog_parent
            if hasattr(dialog_parent,"refresh_system_knowledge") else None
        )
        self._active_tag_source_editor=source_editor
        self._tag_editor_record_lock_id=""
        dialog=_TagDetailsDialog(dialog_parent or self,self); self._tag_dialog_parent=None; self._active_tag_editor_dialog=dialog; dialog.setWindowTitle(title)
        creating=not bool(self.current_tag_id)
        self._tag_dialog_batch_edit=not creating
        self._tag_save_timer.stop()
        screen=dialog.screen() or QApplication.primaryScreen(); bounded=_bounded_tag_editor_size(screen.availableGeometry()) if screen is not None else QSize(980,650)
        dialog.resize(QSize(min(820,bounded.width()),min(430,bounded.height())) if creating else bounded)
        layout=QVBoxLayout(dialog)
        panel=self.tag_editor_panel
        scroll=QScrollArea(dialog); scroll.setObjectName("tagDetailsScroll"); scroll.setAccessibleName("Scrollable Tag Details")
        scroll.setFrameShape(QFrame.NoFrame); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        layout.addWidget(scroll,1)
        # The persistent Tag panel carries the same save status into this
        # dialog's footer, including failures; do not duplicate the notice.
        self._tag_dialog_status=self.tag_save_status
        panel.show(); self._set_tag_editor_creation_mode(creating,dialog_controls=creating)
        QTimer.singleShot(0,self._reset_tag_editor_scroll)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel if creating else QDialogButtonBox.Close)
        create_action=buttons.addButton("Create SDXL Tag",QDialogButtonBox.AcceptRole) if creating else None
        discard_action=buttons.addButton("Discard Tag Draft",QDialogButtonBox.ResetRole)
        discard_action.setToolTip("Restore this Tag's last loaded saved fields. Character Knowledge and Category Workspace changes save separately.")
        discard_action.setVisible(not creating)
        discard_action.clicked.connect(self._discard_unsaved_tag_changes)
        save_action=buttons.addButton("Save Tag",QDialogButtonBox.ApplyRole)
        save_action.setProperty("primary",True); save_action.setVisible(not creating)
        save_action.clicked.connect(lambda:self._flush_tag_save(force=True))
        if not creating:
            self._set_tag_save_status("Save Tag or Close keeps edits.")
        if create_action is not None:
            create_action.setProperty("primary",True)
            self._tag_create_dialog_action=create_action
            self._update_tag_create_dialog_action()
        close_button=buttons.button(QDialogButtonBox.Cancel if creating else QDialogButtonBox.Close)
        def close_editor():
            if getattr(self,"_tag_identity_setup_task",None) is not None:
                self._set_tag_save_status("Character Knowledge is being set up. Wait for the verified save to finish.")
                return
            if self._modifier_policy_save_task is not None:
                QMessageBox.information(dialog,"Reusable policy is still saving","Wait for the compatible Base Tag policy batch to finish saving before closing Tag Details.")
                return
            if not self.current_tag_id:
                dialog.reject(); return
            if not self._flush_inline_character_editor():
                QMessageBox.warning(dialog,"Changes are not saved","Correct the highlighted Character Knowledge edit before closing.")
                return
            self._flush_tag_save(force=True)
            if self._tag_save_task is not None:
                self._tag_close_after_save=dialog
                return
            if self._tag_save_pending:
                answer=QMessageBox.question(
                    dialog,"Discard unsaved Tag Details?",
                    "These Tag Details could not be saved. Discard the invalid "
                    "fields and close? The last successfully saved System "
                    "Knowledge record will remain unchanged.",
                    QMessageBox.Yes|QMessageBox.No,QMessageBox.No,
                )
                if answer==QMessageBox.Yes:
                    record=self.tags.tags.get(self.current_tag_id)
                    if record is not None:
                        self._load_tag_record_into_editor(
                            record,changing_record=False,load_examples=False,
                        )
                    self._tag_save_pending=False
                    dialog.reject()
                else:
                    self._pending_tag_conflict_handoff=None
                return
            dialog.accept()
        saved_return={"record":None,"retry":None,"busy":False}
        retry_return=buttons.addButton("Retry Return to Image",QDialogButtonBox.ActionRole)
        retry_return.hide()
        retry_return.setProperty("primary",True)
        def retry_saved_return():
            callback=saved_return["retry"]
            if callback is None or saved_return["busy"]:return
            saved_return["busy"]=True; retry_return.setEnabled(False)
            try:
                callback()
            except Exception as error:
                self._set_tag_save_status(
                    "Tag remains saved. Returning to the image needs attention. "
                    "Retry Return to Image retries only the return, without creating another Tag. "
                    f"Details: {error}",error=True,
                )
            finally:
                saved_return["busy"]=False
                if isValid(retry_return):retry_return.setEnabled(True)
        retry_return.clicked.connect(retry_saved_return)
        def create_entry():
            if saved_return["record"] is not None:
                retry_saved_return(); return
            existing=self.tags.find(self.tag_name.text().strip())
            decision,error=({"action":"use_existing","tag_id":existing.id},"") if existing is not None else self._character_creation_decision()
            if error:
                self._set_tag_save_status(f"Not created — {error}",error=True)
                return
            def created(record):
                if not isValid(dialog) or (return_editor is not None and not isValid(return_editor)):return
                # Persistence has completed. Never re-enter the creation writer,
                # even if linking or the originating editor refresh now fails.
                saved_return["record"]=record
                if create_action is not None:create_action.hide(); create_action.setEnabled(False)
                self._tag_create_dialog_action=None
                if close_button is not None:close_button.setText("Close")
                def finish_created(character_status="",*,needs_review=False):
                    if not isValid(dialog) or (return_editor is not None and not isValid(return_editor)):return
                    if rule_editor is not None:
                        rule_tag_saved[0]=True
                        if isValid(rule_editor):rule_editor.rule_tag_created(record)
                        dialog.accept()
                        return
                    saved_return["retry"]=lambda:finish_created(character_status,needs_review=needs_review)
                    if return_editor is not None:retry_return.show()
                    try:
                        staged=bool(return_editor is not None and return_editor.complete_system_tag_creation(record.id))
                    except Exception as error:
                        self._set_tag_save_status(
                            "Tag remains saved. Returning to the image needs attention. "
                            "Choose Retry Return to Image; this does not create another Tag. "
                            f"Details: {error}",error=True,
                        )
                        return
                    if staged:
                        suffix=f" {character_status}" if character_status else ""
                        self._set_tag_save_status("SDXL Tag created and staged on the originating image. Review the unsaved image change there."+suffix)
                        if needs_review:QMessageBox.information(return_editor,"Character Knowledge needs review",character_status+" Open this Tag's Character Knowledge to resolve its record.")
                        dialog.accept()
                        return
                    self._load_tag_record_into_editor(record,changing_record=True,load_examples=False,update_status=False)
                    self._set_tag_editor_creation_mode(False,dialog_controls=True)
                    self._tag_dialog_batch_edit=True; save_action.show(); discard_action.show()
                    dialog.setWindowTitle(f"SDXL Tag — {record.display}")
                    if create_action is not None:create_action.hide()
                    if close_button is not None:close_button.setText("Close")
                    dialog.resize(bounded)
                    self._load_tag_examples(record)
                    if character_status:self._set_tag_save_status(character_status)
                    if decision.get("action")=="automatic":
                        with QSignalBlocker(self.tag_editor_tabs):self.tag_editor_tabs.setCurrentIndex(self.tag_identity_tab_index)
                        self.tag_identity_status.setText(character_status)
                    QTimer.singleShot(0,self._reset_tag_editor_scroll)
                if decision.get("action")=="automatic":
                    self._setup_tag_identity(record.id,on_finished=lambda result:finish_created(
                        result.message if result is not None else "Tag saved; automatic record setup needs attention.",
                        needs_review=result is None or result.status=="review",
                    ))
                else:
                    def finish_character_link():
                        status=self._complete_character_creation_decision(decision,record,dialog)
                        if getattr(self,"_character_creation_link_failed",False):
                            saved_return["retry"]=finish_character_link
                            retry_return.setText("Retry Character Link");retry_return.show()
                            self._set_tag_save_status(status+". Tag remains saved; choose Retry Character Link or Close. No image change has been staged.",error=True)
                            return
                        retry_return.setText("Retry Return to Image")
                        finish_created(status)
                    finish_character_link()
            if decision.get("action")=="use_existing":created(self.tags.tags[decision["tag_id"]])
            else:self.save_tag(on_saved=created)
        if create_action is not None:create_action.clicked.connect(create_entry)
        dialog.request_close=close_editor
        buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        dialog_result=dialog.exec()
        if rule_editor is not None and not rule_tag_saved[0] and isValid(rule_editor):
            rule_editor.rule_tag_creation_cancelled()
        self._tag_dialog_batch_edit=False
        self._tag_editor_record_lock_id=""
        self._active_tag_editor_dialog=None; self._active_tag_source_editor=None
        self._tag_create_dialog_action=None
        self._tag_dialog_status=None
        scroll.takeWidget(); panel.hide(); panel.setParent(self.tag_splitter); self.tag_splitter.addWidget(panel)
        self._set_tag_editor_creation_mode(not bool(self.current_tag_id))
        if getattr(self,"_category_refresh_pending",False):
            self._category_refresh_selection_ids=opening_tag_selection
        if (not getattr(self,"_category_refresh_pending",False)
            and not self.category_workspace.has_unsaved_changes()):
            session=self.category_workspace.session
            saved_session,saved_snapshot=getattr(self,"_category_workspace_saved_snapshot",(None,None))
            unchanged=(saved_snapshot if session is saved_session else
                       opening_snapshot if session is opening_session else None)
            self._queue_category_workspace_refresh(
                unchanged_snapshot=unchanged,selected_tag_ids=opening_tag_selection,
            )
        handoff=self._pending_tag_conflict_handoff
        self._pending_tag_conflict_handoff=None
        if dialog_result==QDialog.Accepted and handoff is not None:
            self.open_automation.emit("conflicts.guided_checks",handoff)

    def _change_chip_tag_categories(self,selected_ids):
        selected_ids=tuple(selected_ids)
        workspace=self.category_workspace; records=[workspace.session._state.tags[stable_id] for stable_id in selected_ids if stable_id in workspace.session._state.tags]
        if not selected_ids or len(records)!=len(selected_ids):
            QMessageBox.warning(self,"Selection changed","One or more selected SDXL Tags are no longer available. Select them again and retry."); return
        dialog=TagCategoryChangeDialog(records,workspace.session,self)
        if dialog.exec()!=QDialog.Accepted:return
        target=dialog.target_category_id()
        if any(stable_id not in workspace.session._state.tags for stable_id in selected_ids):
            QMessageBox.warning(self,"Selection changed","The selected SDXL Tags changed while the preview was open. Nothing was saved."); return
        workspace.apply_tag_category_change(selected_ids,target,selected_key=workspace.selected_key)

    def _always_ignore_tags(self,selected_ids):
        selected_ids=tuple(dict.fromkeys(str(value) for value in selected_ids))
        workspace=self.category_workspace
        if workspace.has_unsaved_changes():
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Open Rules & Automation after it finishes."); return
        records=[self.tags.tags.get(value) for value in selected_ids]
        if not selected_ids or any(record is None for record in records):
            QMessageBox.warning(self,"Selection changed","One or more selected SDXL Tags are no longer available. Select them again and retry."); return
        names=[record.display for record in records]; shown=", ".join(names[:8]); remainder=len(names)-8
        selected_text=shown+(f" and {remainder:,} more" if remainder>0 else "")
        message=(
            f"Always ignore {len(records):,} selected SDXL Tag{'s' if len(records)!=1 else ''}?\n\n"
            f"Selected: {selected_text}.\n\n"
            "This creates one exact global ignore rule per selected tag. Automatic refresh and future managed evidence intake suppress matching TXT, Metadata, or AI findings before review. "
            "Confirmed Image Knowledge and raw evidence are preserved. No image, caption, Metadata, or System Knowledge record changes now, and the rules can be reviewed or removed in Rules & Automation."
        )
        if QMessageBox.question(self,"Create Always-Ignore Rules",message)!=QMessageBox.Yes:return
        repository=AutomationRuleRepository(
            self.editor_context.defaults/"automation_rules.json",self.editor_context.user/"automation_rules.json",
            rule_pair_admission=RulePairAdmission(self.editor_context.user),
        )
        try:added=repository.add_ignore_tags_verified(names)
        except Exception as error:
            workspace.report_always_ignore_result(f"Always-ignore rules were not saved. The prior Rules & Automation file remains active: {error}",error=True); return
        if added:
            workspace.report_always_ignore_result(f"Saved {len(added):,} exact always-ignore rule{'s' if len(added)!=1 else ''}. Matching incoming evidence will be suppressed during automatic refresh and future managed intake; confirmed Image Knowledge did not change.")
        else:
            workspace.report_always_ignore_result(f"All {len(records):,} selected SDXL Tags already have active always-ignore rules. No data changed.")

    def refresh_all(self,*,reload=True):
        if reload:
            self.tags.load(); self.categories.load(); self.characters.load(); self.gallery_status.invalidate()
            self._reload_modifier_roles()
        self._tag_view_cache=None
        if self.tag_scope_page is not None:self.refresh_tags()
        if hasattr(self,"category_workspace") and not self.category_workspace.has_unsaved_changes():self.category_workspace.reload_from_live()
        self._refresh_review_views()

    def _category_staging_active(self):
        return bool(hasattr(self,"category_workspace") and self.category_workspace.has_unsaved_changes())

    def _tag_record_map(self):
        if self._category_staging_active():return self.category_workspace.session._state.tags
        return self.tags.tags

    def _tag_aliases(self,record):
        service=getattr(self,"_tag_alias_service",None)
        if service is not None:return service.effective_for_tag(record.id).aliases
        return self.tags.effective_aliases(record)

    def _tag_names(self,record):
        return self.tags.identity_names(record)

    def _category_path_label(self,category_id):
        if self._category_staging_active():return self.category_workspace.session.path_label(category_id)
        return self.categories.path_label(category_id)

    def _category_exists(self,category_id):
        if self._category_staging_active():return self.category_workspace.session.get(category_id)
        return self.categories.get(category_id)

    def _tag_search_text(self,record):
        provenance=[str(item.get("source") or "") for item in [*record.provenance,*record.ordering.provenance]]
        semantic_parent,semantic_implies=self.tags.effective_semantic_ids(record)
        return " ".join([
            *self._tag_names(record),record.category,record.concept_id or "",
            record.ordering.group,ORDER_GROUP_LABELS[record.ordering.group],
            str(record.ordering.priority),*record.attributes,semantic_parent,
            *semantic_implies,*record.conflicts,*provenance,
        ]).casefold()

    @staticmethod
    def _tag_matches_filters(record,haystack,query,category,order_group):
        return ((not query or query in haystack)
                and (not category or record.category==category)
                and (not order_group or record.ordering.group==order_group))

    def _tag_rows(self):
        records=self._tag_record_map()
        if not self._category_staging_active() and self._tag_view_cache is not None and len(self._tag_view_cache)==len(records):
            return self._tag_view_cache
        derived_anchors=exact_category_anchor_ids(
            self.categories.categories.values(),records.values(),
        )
        self._tag_derived_anchors=derived_anchors
        rows=[]
        for record in sorted(
            records.values(),
            key=lambda item: (
                not bool(item.display[:1].isalpha()),
                item.display.casefold(),
            ),
        ):
            haystack=self._tag_search_text(record)
            if record.category and not self._category_exists(record.category): status,label="red","Invalid category"
            elif record.category=="uncategorized": status,label="orange","Needs category"
            elif not self.tags.effective_base_tag(
                record,self.categories,derived_anchors=derived_anchors,
            )[0] and not record.concept_id and not record.properties: status,label="yellow","Category assigned"
            else: status,label="green","Structured"
            rows.append((record,haystack,self._category_path_label(record.category),status,label))
        if not self._category_staging_active():self._tag_view_cache=rows
        return rows

    def _tag_result_row(self,record,category_label,status,status_label):
        reviewed="reviewed" if record.ordering.reviewed else "category default"
        anchors=getattr(self,"_tag_derived_anchors",None)
        base_id,base_source,base_kind=self.tags.effective_base_tag(
            record,self.categories,derived_anchors=anchors,
        ); base=self.tags.tags.get(base_id)
        rating,rating_source,rating_kind=effective_tag_sensitivity(record,self.categories)
        base_text=(f"{base.display} ({'tag override' if base_kind=='explicit' else 'inherited from '+self.categories.path_label(base_source)})" if base else "not set")
        rating_text=(f"{rating} ({'tag override' if rating_kind=='explicit' else 'inherited from '+self.categories.path_label(rating_source)})" if rating else "intentionally unclassified" if rating_kind=="explicit_none" else "not classified")
        tooltip=f"Output: {ORDER_GROUP_LABELS[record.ordering.group]}, priority {record.ordering.priority} ({reviewed})\nSensitivity: {rating_text}\nObserved in {record.seen_count:,} image(s)\nBase SDXL Tag: {base_text}"
        accessible=f"SDXL Tag {record.display}; category {category_label}; status {status_label}; output {ORDER_GROUP_LABELS[record.ordering.group]}; {reviewed}"
        return IncrementalResultRow(record.id,(record.display,category_label,status_label),tooltip,accessible,STATUS_COLORS.get(status,""))

    def refresh_tags(self,*_):
        if self.tag_scope_page is None:return
        selected_ids=self.selected_tag_ids() or ([self.current_tag_id] if self.current_tag_id else []); preserve_count=self.tag_model.loaded_count
        self.tag_filter.blockSignals(True); current=self.tag_filter.currentData(); self.tag_filter.clear(); self.tag_filter.addItem("All categories","")
        rows=self._tag_rows()
        used=sorted({record.category for record,_,_,_,_ in rows},key=lambda value:self._category_path_label(value).casefold())
        for category in used: self.tag_filter.addItem(self._category_path_label(category),category)
        self.tag_filter.setCurrentIndex(max(0,self.tag_filter.findData(current))); self.tag_filter.blockSignals(False)
        query=self.tag_search.text().strip().casefold(); category=str(self.tag_filter.currentData() or ""); order_group=str(self.tag_order_filter.currentData() or ""); items=[]
        for record,haystack,category_label,status,status_label in rows:
            if not self._tag_matches_filters(record,haystack,query,category,order_group):continue
            items.append(self._tag_result_row(record,category_label,status,status_label))
        self._restoring_tag_selection=True; self.tag_model.set_rows(items,preserve_count=preserve_count,ensure_ids=selected_ids); self._restore_tag_selection(selected_ids); self._restoring_tag_selection=False; self.show_tag()

    def _update_tag_result_counts(self,shown,total):
        suffix=" Load more to continue." if shown<total else ""
        self.tag_results.setText(f"Showing {shown:,} of {total:,} matching SDXL Tags.{suffix}")
        remaining=max(0,total-shown); self.tag_load_more_button.setVisible(bool(remaining)); self.tag_load_more_button.setText(f"Load {min(self.TAG_PAGE_SIZE,remaining):,} More…" if remaining else "Load More…"); self.tag_load_more_button.setAccessibleName(f"Load {min(self.TAG_PAGE_SIZE,remaining):,} more SDXL Tags; {shown:,} of {total:,} shown" if remaining else "All matching SDXL Tags are shown")

    def selected_tag_ids(self):
        if not hasattr(self,"tag_list") or self.tag_list.selectionModel() is None:return []
        return [self.tag_model.stable_id(index) for index in sorted(self.tag_list.selectionModel().selectedRows(0),key=lambda value:value.row()) if self.tag_model.stable_id(index)]

    def _restore_tag_selection(self,stable_ids):
        selection=self.tag_list.selectionModel(); blocker=QSignalBlocker(selection); selection.clearSelection(); current=None
        for stable_id in stable_ids:
            index=self.tag_model.index_for_id(stable_id)
            if not index.isValid():continue
            selection.select(index,QItemSelectionModel.Select|QItemSelectionModel.Rows)
            if stable_id==self.current_tag_id or current is None:current=index
        if current is None and self.tag_model.rowCount():current=self.tag_model.index(0,0); selection.select(current,QItemSelectionModel.Select|QItemSelectionModel.Rows)
        if current is not None:selection.setCurrentIndex(current,QItemSelectionModel.NoUpdate)
        del blocker
        self._tag_selection_changed()

    def show_tag(self,current=None,*_):
        if getattr(self,"_restoring_tag_selection",False):return
        index=current if current is not None and current.isValid() else self.tag_list.currentIndex()
        selection=self.tag_list.selectionModel(); row_index=index.sibling(index.row(),0) if index.isValid() else index
        if selection is not None and selection.selectedRows(0) and not selection.isSelected(row_index):return
        selected_id=self.tag_model.stable_id(index)
        if not selected_id:return
        locked_id=getattr(self,"_tag_editor_record_lock_id","")
        if locked_id and selected_id!=locked_id:return
        changing_record=bool(self.current_tag_id and selected_id!=self.current_tag_id)
        if self.current_tag_id and selected_id!=self.current_tag_id and not self._flush_inline_character_editor():
            self._set_tag_save_status("Finish or correct the Character Knowledge edit before selecting another SDXL Tag.",error=True)
            self._restoring_tag_selection=True
            try:self._restore_tag_selection((self.current_tag_id,))
            finally:self._restoring_tag_selection=False
            return
        if self._tag_save_pending and self.current_tag_id and selected_id!=self.current_tag_id:
            self._flush_tag_save()
            if self._tag_save_pending or self._tag_save_task is not None:
                self._restore_tag_selection((self.current_tag_id,))
                return
        record=self._tag_record_map().get(selected_id)
        if not record: return
        self._load_tag_record_into_editor(record,changing_record=changing_record)

    def _load_tag_record_into_editor(self,record,*,changing_record=False,load_examples=True,update_status=True):
        """Bind every Tag Details tab to one already-resolved stable record."""
        self._tag_save_timer.stop(); self._tag_save_pending=False; self._loading_tag_editor=True
        self._set_tag_reuse_feedback("")
        self.current_tag_id=record.id; self.tag_name.setText(record.display); self._fill_category_combo(self.tag_category,record.category)
        self.tag_order_group.setCurrentIndex(max(0,self.tag_order_group.findData(record.ordering.group))); self.tag_priority.setValue(record.ordering.priority); self._tag_ordering_reviewed=record.ordering.reviewed; self._tag_ordering_edited=False; self.tag_sensitivity.setCurrentIndex(max(0,self.tag_sensitivity.findData(record.content_sensitivity.minimum_rating))); self.tag_content_flags.setText(_join(record.content_sensitivity.content_flags))
        try:
            effective_conflicts=effective_constraint_tag_ids(
                self.tags,record.id,
                default_path=self.editor_context.defaults/"relationships.json",
                user_path=self.editor_context.relationships,
            )
            conflict_status="Contrary Values use the shared symmetric relationship view. Review their scope and exceptions in Rules & Automation."
        except (OSError,RuntimeError,ValueError) as error:
            effective_conflicts=self.tags.legacy_conflict_ids(record.id)
            conflict_status=f"Canonical constraints are unavailable; showing the read-only compatibility view ({error})."
        alias_values=tuple(record.aliases); alias_status=""
        if self._tag_alias_service is not None:
            try:alias_values=self._tag_alias_service.effective_for_tag(record.id).aliases
            except (OSError,RuntimeError,ValueError) as error:
                self._tag_alias_error=str(error); self._tag_alias_service=None
        if self._tag_alias_service is None:
            alias_status=(
                "Canonical Other Name authority is unavailable; showing the "
                "read-only compatibility projection"
                +(f" ({self._tag_alias_error})." if self._tag_alias_error else ".")
            )
        semantic_parent=str(record.parent_id or "")
        semantic_implies=tuple(record.implies)
        semantic_status=""
        if self._tag_semantic_service is not None:
            try:
                semantic=self._tag_semantic_service.effective_for_tag(record.id)
                semantic_parent=semantic.parent_id
                semantic_implies=semantic.implies
            except (OSError,RuntimeError,ValueError) as error:
                self._tag_semantic_error=str(error)
                self._tag_semantic_service=None
        if self._tag_semantic_service is None:
            semantic_status=(
                "Canonical Broader Tag and Also Means authority is unavailable; "
                "showing the read-only compatibility projection"
                + (f" ({self._tag_semantic_error})." if self._tag_semantic_error else ".")
            )
        composition_values=tuple(record.attributes)
        self._loaded_modifier_composition_fingerprints={}
        if self._modifier_composition_service is not None:
            try:
                composition=self._modifier_composition_service.effective_for_tag(record.id)
                composition_values=composition.values
                self._loaded_modifier_composition_fingerprints=composition.fingerprints()
            except (OSError,RuntimeError,ValueError) as error:
                self._modifier_composition_error=str(error)
                self._modifier_composition_service=None
        self.tag_aliases.set_read_only(alias_status); self.tag_aliases.set_values(alias_values); self.tag_parent.set_read_only(semantic_status); self.tag_parent.set_values((semantic_parent,) if semantic_parent else ()); self.tag_attributes.set_values(composition_values); self.tag_implies.set_read_only(semantic_status); self.tag_implies.set_values(semantic_implies); self.tag_conflicts.setEnabled(True); self.tag_conflicts.set_read_only(conflict_status); self.tag_conflicts.set_values(effective_conflicts); self.tag_constraint_summary.setText(self._effective_conflict_summary(record.id)); self.tag_notes.setPlainText(record.notes)
        self._update_tag_meaning_review_path(record.id)
        self._update_tag_broader_review_path(record.id)
        for editor in (self.tag_parent,self.tag_attributes,self.tag_implies):editor.setEnabled(True)
        for field,editor in (("parent",self.tag_parent),("implies",self.tag_implies)):
            editor.set_context(record.id,lambda values,relation=field:self._tag_relation_candidate_error(relation,values))
        self._tag_relationship_dirty_fields.clear(); self._refresh_tag_composition()
        self._loading_tag_editor=False; self._set_tag_editor_creation_mode(False)
        if update_status:
            message=(
                "A Category Workspace change is saving; single-record editing is temporarily paused."
                if self._category_staging_active() else
                "Save Tag or Close keeps edits."
                if getattr(self,"_active_tag_editor_dialog",None) is not None else
                "Changes save automatically."
            )
            self._set_tag_save_status(message)
        self.tag_appearance_editor.refresh_context(tag_id=record.id,category_id=record.category)
        self.tag_order_summary.setText(f"{ORDER_GROUP_LABELS[record.ordering.group]} • {'Tag override' if record.ordering.reviewed else 'Category order'}")
        sources=[*record.provenance,*record.ordering.provenance,*record.content_sensitivity.provenance]
        source_names=sorted({str(item.get("source") or "Recorded source").replace("_"," ").title() for item in sources})
        self.tag_provenance.setText((f"{len(sources):,} record(s) • "+", ".join(source_names[:3])) if sources else "No recorded provenance")
        self._tag_provenance_details="\n".join(json.dumps(item,ensure_ascii=False,sort_keys=True) for item in sources)
        self.tag_stats.setText(f"Observed in {record.seen_count:,} image(s) • {len(sources):,} recorded knowledge source(s) • safety and semantic ordering are independent")
        self._refresh_tag_consumer_restriction(record)
        self._refresh_tag_identity(record.id)
        if load_examples:self._load_tag_examples(record)
        if changing_record:
            QTimer.singleShot(0,self._reset_tag_editor_scroll)

    def _reset_tag_editor_scroll(self):
        if not isValid(self.tag_editor_panel):return
        parent=self.tag_editor_panel.parentWidget()
        while parent is not None:
            if isinstance(parent,QScrollArea):
                layout=self.tag_editor_panel.layout()
                if layout is not None:
                    layout.invalidate(); layout.activate(); hint=layout.sizeHint(); viewport=parent.viewport().size()
                    self.tag_editor_panel.resize(
                        max(viewport.width(),hint.width()),
                        max(viewport.height(),hint.height()),
                    )
                parent.verticalScrollBar().setValue(0)
                parent.horizontalScrollBar().setValue(0)
                return
            parent=parent.parentWidget()

    def _show_tag_relationship_task(self,index):
        for task_index,task in enumerate(self._tag_relationship_task_pages):
            task.setVisible(task_index==index)
        self._reset_tag_editor_scroll()

    def _refresh_tag_identity(self,tag_id,*,preserve_inline=False):
        self._ensure_tag_editor()
        self.tag_identity_tree.clear(); self.tag_identity_details.clear()
        linked_tag=self.tags.tags.get(tag_id); linked_tag_label=linked_tag.display if linked_tag is not None else tag_id
        linked_sources=[record for record in self.characters.series.values() if self.characters.entity_tag_id("series",record.id)==tag_id]
        linked_characters=[record for record in self.characters.characters.values() if self.characters.entity_tag_id("character",record.id)==tag_id]
        linked_outfits=[record for record in self.characters.outfits.values() if self.characters.entity_tag_id("outfit",record.id)==tag_id]
        for record in sorted(linked_sources,key=lambda item:item.name.casefold()):
            characters=sorted((
                self.characters.characters[item_id]
                for item_id in self.characters.character_ids_for_source(record.id)
                if item_id in self.characters.characters
            ),key=lambda item:item.name.casefold())
            outfits=sorted((
                item for item in self.characters.outfits.values()
                if self.characters.character_source_id("outfit",item.id)==record.id
            ),key=lambda item:item.name.casefold())
            details=f"{len(record.aliases):,} aliases • {len(record.original_style_tags):,} original/style tags • {len(characters):,} characters • {len(outfits):,} outfits"
            item=QTreeWidgetItem(("Character Source",linked_tag_label,linked_tag_label,details)); item.setData(0,Qt.UserRole,("Series",record.id)); self.tag_identity_tree.addTopLevelItem(item)
            for character in characters:
                child=QTreeWidgetItem(("Character",self._character_identity_tag_label(character),linked_tag_label,character.identity_type.replace("_"," ").title())); child.setData(0,Qt.UserRole,("Characters",character.id)); item.addChild(child)
            for outfit in outfits:
                child=QTreeWidgetItem(("Character outfit",self._character_outfit_tag_label(outfit),linked_tag_label,f"Associated characters: {len(self.characters.associated_character_ids(outfit.id)):,}")); child.setData(0,Qt.UserRole,("Outfits",outfit.id)); item.addChild(child)
            item.setExpanded(True)
        for record in sorted(linked_characters,key=lambda item:item.name.casefold()):
            source=self.characters.series.get(
                self.characters.character_source_id("character",record.id)
            ); outfits=sorted((
                self.characters.outfits[item_id]
                for item_id in self.characters.outfit_ids_for_character(record.id)
                if item_id in self.characters.outfits
            ),key=lambda item:item.name.casefold())
            details=f"{record.identity_type.replace('_',' ').title()} • {len(record.aliases):,} aliases • {len(record.plain_language):,} descriptions • {len(record.tag_associations):,} evidence summaries"
            item=QTreeWidgetItem(("Character",linked_tag_label,self._character_source_tag_label(source),details)); item.setData(0,Qt.UserRole,("Characters",record.id)); self.tag_identity_tree.addTopLevelItem(item)
            for outfit in outfits:
                outfit_label=self._character_outfit_tag_label(outfit)
                child=QTreeWidgetItem(("Potential outfit",outfit_label,self._character_source_tag_label(source),f"{len(outfit.aliases):,} aliases • {len(outfit.sdxl_tags):,} collection SDXL Tags")); child.setData(0,Qt.UserRole,("Outfits",outfit.id)); item.addChild(child)
            item.setExpanded(True)
        for record in sorted(linked_outfits,key=lambda item:item.name.casefold()):
            source=self.characters.series.get(
                self.characters.character_source_id("outfit",record.id)
            ); associated_ids=self.characters.associated_character_ids(record.id); names=[self._character_identity_tag_label(self.characters.characters[value]) for value in associated_ids if value in self.characters.characters]
            details=f"Associated characters: {', '.join(names) or 'none recorded'} • {len(record.aliases):,} aliases"
            item=QTreeWidgetItem(("Outfit",linked_tag_label,self._character_source_tag_label(source),details)); item.setData(0,Qt.UserRole,("Outfits",record.id)); self.tag_identity_tree.addTopLevelItem(item)
            for character_id in associated_ids:
                character=self.characters.characters.get(character_id)
                if character is None:continue
                character_tag=self.tags.tags.get(self.characters.entity_tag_id("character",character.id)); character_label=self._character_identity_tag_label(character)
                label="Reviewed Character Identity SDXL Tag" if character_tag is not None else "No reviewed Character Identity SDXL Tag"
                child=QTreeWidgetItem(("Associated character",character_label,self._character_source_tag_label(source),label)); child.setData(0,Qt.UserRole,("Characters",character.id)); item.addChild(child)
            item.setExpanded(True)
        count=len(linked_sources)+len(linked_characters)+len(linked_outfits); tag=self.tags.tags.get(tag_id)
        reviewed_count=sum(
            self.characters.reviewed_knowledge_tag_id(kind,record.id)==tag_id
            for kind,records in (("series",linked_sources),("character",linked_characters),("outfit",linked_outfits))
            for record in records
        )
        role=self.categories.effective_semantic_role(tag.category) if tag is not None else ""
        eligible=role in {"character_source","character_identity","character_outfit"}
        missing=bool(eligible and not count)
        record_label={"character_source":"Character Source","character_identity":"Character","character_outfit":"Outfit"}.get(role,"Character Knowledge")
        automatic=role in {"character_identity","character_source"}
        missing_text=(f"No {record_label} record is linked yet. Open Character Knowledge to link an exact match or create an editable record automatically. Uncertain matches require review."
            if automatic else "No reviewed Outfit record is linked. Review existing matches or create a record.")
        self._set_tag_basics_row_visible(self.tag_identity_notice,bool(eligible or count))
        self.tag_identity_notice_text.setText(missing_text if missing else f"Linked {record_label} record ready to edit.")
        self.tag_identity_repair_button.setText(f"{'Set Up' if missing and automatic else 'Edit'} {record_label} Record…")
        self.tag_identity_link_existing.setText(f"Link Existing {record_label}…")
        self.tag_identity_link_existing.setVisible(missing)
        self.tag_identity_link_existing.setEnabled(missing and not self._category_staging_active())
        self.tag_editor_tabs.setTabVisible(self.tag_identity_tab_index,bool(count or eligible))
        creation_buttons=((self.tag_identity_create_source,"character_source"),(self.tag_identity_create_character,"character_identity"),(self.tag_identity_create_outfit,"character_outfit"))
        for button,semantic_role in creation_buttons:button.setVisible(bool(role==semantic_role and not count)); button.setEnabled(bool(role==semantic_role and not count and not self._category_staging_active()))
        self.tag_identity_tree_group.setVisible(bool(count))
        self.tag_identity_status.setText(
            f"{reviewed_count:,} reviewed reusable link{'s' if reviewed_count!=1 else ''}; "
            f"{count-reviewed_count:,} linked record{'s' if count-reviewed_count!=1 else ''} still need link review. "
            "Automatic source/description policies also require unambiguous links and confirmed image evidence. "
            "These relationships do not assign facts to an image."
            if count else missing_text
        )
        if count:self.tag_identity_tree.setCurrentItem(self.tag_identity_tree.topLevelItem(0))
        inline_target=None
        if count==1:
            if linked_sources:inline_target=("Series",linked_sources[0].id)
            elif linked_characters:inline_target=("Characters",linked_characters[0].id)
            elif linked_outfits:inline_target=("Outfits",linked_outfits[0].id)
        self._set_tag_identity_inline_editor(inline_target,force=not preserve_inline)

    def _set_tag_identity_inline_editor(self,target,*,force=False):
        normalized=tuple(target) if isinstance(target,(tuple,list)) and len(target)==2 else None
        if normalized==self._tag_identity_inline_key and self.tag_identity_inline_editor is not None and not force:
            return
        current=self.tag_identity_inline_editor
        if current is not None:
            if not current.flush_pending():
                return False
            current.dispose()
            self.tag_identity_inline_layout.removeWidget(current)
            current.hide(); current.setEnabled(False); current.deleteLater()
        self.tag_identity_inline_editor=None; self._tag_identity_inline_key=None
        if normalized is None:
            return True
        kind,record_id=str(normalized[0]),str(normalized[1])
        try:
            editor=self._character_editor_factory(
                CharacterRecordEditService(self.characters,self.tags,self.categories),
                self.tags,self.categories,kind=kind,record_id=record_id,
                embedded=True,parent=self.tag_identity_inline_host,
            )
        except ValueError as error:
            self.tag_identity_status.setText(f"Character Knowledge cannot be edited here: {error}")
            return False
        editor.record_saved.connect(self._inline_character_record_saved)
        self.tag_identity_inline_editor=editor; self._tag_identity_inline_key=(kind,record_id)
        self.tag_identity_inline_layout.addWidget(editor); editor.show()
        return True

    def _queue_identity_dependency_refresh(self,tag_ids,source_names,*,workspace_current=False):
        """Resolve newly attached names off-thread before publishing exact scope."""
        from core.tag_record_save import prepare_identity_save_impact
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        request=TagSaveImpactRequest(index,tuple(source_names))
        task=AsyncCallTask(lambda:prepare_identity_save_impact(request))
        pending=getattr(self,"_identity_refresh_tasks",None)
        if pending is None:self._identity_refresh_tasks=pending=set()
        pending.add(task)
        def complete(impact):
            pending.discard(task)
            self._release_category_refresh_callbacks(task)
            if getattr(self,"_category_refresh_shutdown",False):return
            try:current=index.evidence_snapshot_token()==impact.snapshot_token
            except Exception:current=False
            exact=current and impact.affected_paths is not None
            self._changed(defer_workspace=True,dependency_kind="tag_identity",
                          complete=exact,tag_ids=tuple(tag_ids),
                          images=impact.affected_paths if exact else (),
                          **({"workspace_current":True} if workspace_current else {}))
        task.signals.complete.connect(complete)
        task.signals.failed.connect(lambda _message:complete(TagSaveImpact()))
        QThreadPool.globalInstance().start(task)

    def _inline_character_record_saved(self,_kind,_record_id):
        self.characters.load()
        self._reload_modifier_roles()
        if self.current_tag_id:
            self._refresh_tag_identity(self.current_tag_id,preserve_inline=True)
            self._changed(defer_workspace=True,dependency_kind="relationship",
                          complete=True,tag_ids=(self.current_tag_id,))

    def _flush_inline_character_editor(self):
        editor=self.tag_identity_inline_editor
        return True if editor is None else editor.flush_pending()

    def _character_source_tag_label(self,source):
        if source is None:return "—"
        link=self.characters.effective_knowledge_tag("series",source.id)
        source_tag=self.tags.tags.get(str(link.get("tag_id") or ""))
        if link.get("status")=="reviewed" and source_tag is not None and self.categories.effective_semantic_role(source_tag.category)=="character_source":return source_tag.display
        return f"{source.name} (legacy typed source)"

    def _character_identity_tag_label(self,character):
        link=self.characters.effective_knowledge_tag("character",character.id)
        tag=self.tags.tags.get(str(link.get("tag_id") or ""))
        if link.get("status")=="reviewed" and tag is not None and self.categories.effective_semantic_role(tag.category)=="character_identity":return tag.display
        return f"{character.name} (legacy typed character)"

    def _character_outfit_tag_label(self,outfit):
        link=self.characters.effective_knowledge_tag("outfit",outfit.id)
        tag=self.tags.tags.get(str(link.get("tag_id") or ""))
        if link.get("status")=="reviewed" and tag is not None and self.categories.effective_semantic_role(tag.category)=="character_outfit":return tag.display
        return f"{outfit.name} (legacy typed outfit)"

    def _show_tag_identity_details(self,item,_previous=None):
        target=item.data(0,Qt.UserRole) if item is not None else None
        selected_kind=str(target[0]) if isinstance(target,(tuple,list)) and len(target)==2 else ""
        selected_id=str(target[1]) if isinstance(target,(tuple,list)) and len(target)==2 else ""
        self.tag_identity_suggest_setup.setVisible(
            (selected_kind=="Characters" and selected_id in self.characters.characters) or
            (selected_kind=="Outfits" and selected_id in self.characters.outfits)
        )
        if not isinstance(target,(tuple,list)) or len(target)!=2:self.tag_identity_details.clear(); return
        kind,record_id=str(target[0]),str(target[1])
        if kind=="Series":
            record=self.characters.series.get(record_id)
            if record is None:self.tag_identity_details.clear(); return
            characters=[
                self._character_identity_tag_label(self.characters.characters[value])
                for value in self.characters.character_ids_for_source(record.id)
                if value in self.characters.characters
            ]
            outfits=[
                self._character_outfit_tag_label(value)
                for value in self.characters.outfits.values()
                if self.characters.character_source_id("outfit",value.id)==record.id
            ]
            lines=[
                f"Aliases: {_join(record.aliases) or 'None recorded'}",
                f"Original/style tags: {_join(record.original_style_tags) or 'None recorded'}",
                f"Characters from this source: {_join(characters) or 'None recorded'}",
                f"Named outfits from this source: {_join(outfits) or 'None recorded'}",
            ]
        elif kind=="Characters":
            record=self.characters.characters.get(record_id)
            if record is None:self.tag_identity_details.clear(); return
            source=self.characters.series.get(
                self.characters.character_source_id("character",record.id)
            ); tendencies=[]; provenance=[]
            for relation in record.tag_associations[:12]:
                label=str(relation.get("tag") or "Unnamed tendency"); qualifiers=[]
                confidence=relation.get("confidence")
                if confidence not in (None,""):qualifiers.append(_percent(confidence))
                observations=relation.get("observations")
                if observations not in (None,""):qualifiers.append(f"{int(observations):,} observations")
                tendencies.append(label+(f" ({', '.join(qualifiers)})" if qualifiers else ""))
                if relation.get("source"):provenance.append(str(relation["source"]))
            lines=[
                f"Identity type: {record.identity_type.replace('_',' ').title()}", f"Character Source SDXL Tag: {self._character_source_tag_label(source) if source else 'None recorded'}",
                f"Aliases: {_join(record.aliases) or 'None recorded'}", f"Descriptions: {'; '.join(record.plain_language) or 'None recorded'}",
                f"Character collection SDXL Tags: {_join(record.sdxl_tags) or 'None recorded'}", f"Original/style tags: {_join(record.original_style_tags) or 'None recorded'}",
                f"Origin / creator: {' / '.join(value for value in (record.origin_label,record.creator) if value) or 'None recorded'}",
                f"Observed tendencies (not per-image facts): {'; '.join(tendencies) or 'None recorded'}",
                f"Tendency provenance: {_join(tuple(dict.fromkeys(provenance))) or 'None recorded'}",
            ]
        else:
            record=self.characters.outfits.get(record_id)
            if record is None:self.tag_identity_details.clear(); return
            source=self.characters.series.get(
                self.characters.character_source_id("outfit",record.id)
            ); characters=[
                self._character_identity_tag_label(self.characters.characters[value])
                for value in self.characters.associated_character_ids(record.id)
                if value in self.characters.characters
            ]
            lines=[
                f"Character Source SDXL Tag: {self._character_source_tag_label(source) if source else 'None recorded'}", f"Associated characters: {_join(characters) or 'None recorded'}",
                f"Aliases: {_join(record.aliases) or 'None recorded'}", f"Outfit collection SDXL Tags: {_join(record.sdxl_tags) or 'None recorded'}",
                f"Descriptions: {'; '.join(record.plain_language) or 'None recorded'}", f"Style tags: {_join(record.style_tags) or 'None recorded'}",
                f"Notes: {record.notes or 'None recorded'}",
            ]
        self.tag_identity_details.setPlainText("\n".join(lines))

    def _suggest_tag_identity_setup(self):
        item=self.tag_identity_tree.currentItem()
        target=item.data(0,Qt.UserRole) if item is not None else None
        if not isinstance(target,(tuple,list)) or len(target)!=2:
            return
        kind,record_id=str(target[0]),str(target[1])
        owner_kind="character" if kind=="Characters" else "outfit" if kind=="Outfits" else ""
        records=self.characters.characters if owner_kind=="character" else self.characters.outfits
        if not owner_kind or record_id not in records:
            return
        if self._tag_save_pending or self._tag_save_task is not None or self._category_staging_active():
            self.tag_identity_status.setText("Finish saving the current Knowledge edit before suggesting a Generation Setup.")
            return
        self.suggest_generation_setup_requested.emit(owner_kind,record_id)

    def _open_character_knowledge(self):
        if self.tag_editor_tabs.currentIndex()==self.tag_identity_tab_index:self._setup_tag_identity(self.current_tag_id)
        else:self.tag_editor_tabs.setCurrentIndex(self.tag_identity_tab_index)

    def _character_knowledge_tab_changed(self,index):
        if index==self.tag_identity_tab_index and not self._loading_tag_editor:
            tag_id=self.current_tag_id
            QTimer.singleShot(0,lambda:self._setup_tag_identity(tag_id) if (
                isValid(self) and self.current_tag_id==tag_id
                and self.tag_editor_tabs.currentIndex()==self.tag_identity_tab_index
            ) else None)

    def _setup_tag_identity(self,tag_id,*,on_finished=None):
        from core.character_tag_bridge import CharacterTagSetupRequest,CharacterTagSetupResult,setup_character_tag_record
        if not tag_id or tag_id!=self.current_tag_id or getattr(self,"_tag_identity_setup_task",None) is not None:return
        tag=self.tags.tags.get(tag_id)
        if tag is None or self.categories.effective_semantic_role(tag.category) not in {"character_identity","character_source"}:return
        expected_kind={"character_identity":"character","character_source":"series"}[self.categories.effective_semantic_role(tag.category)]
        if self.tag_identity_inline_editor is not None and self.tag_identity_inline_editor.kind==expected_kind and on_finished is None:return
        if self._tag_save_pending or self._tag_save_task is not None or self._category_staging_active():
            message="Save the pending Tag edit before setting up its linked record."
            self.tag_identity_status.setText(message)
            if on_finished is not None:on_finished(CharacterTagSetupResult("review",message=message))
            return
        if getattr(self,"_category_refresh_shutdown",False):return
        initialization_approved=(self._character_initialization_review()
            if self._character_initialization_review is not None else True)
        if not initialization_approved:
            self._set_tag_save_status("Character Knowledge setup was cancelled. The Tag remains saved.")
            if on_finished is not None:on_finished(CharacterTagSetupResult("review",message="Character initialization was not approved."))
            return
        request=CharacterTagSetupRequest(tag_id,self.tags.path,self.tags._loaded_bytes,self.characters.data_dir,
            self.categories.default_path,self.categories.user_path,
            tuple((path,self.categories._loaded_source_fingerprints[path]) for path in (self.categories.default_path,self.categories.user_path)),
            rule_pair_admission=getattr(self.tags,"rule_pair_admission",None))
        task=AsyncCallTask(lambda:self._run_native_operation("character_setup",
            lambda:setup_character_tag_record(request), initialization_approved=initialization_approved))
        self._tag_identity_setup_task=task
        self.tag_editor_panel.setEnabled(False)
        self.tag_identity_status.setText("Setting up the linked record…")
        self._set_tag_save_status("Setting up Character Knowledge…")
        def finished(result=None,error=""):
            if not isValid(self) or getattr(self,"_tag_identity_setup_task",None) is not task:return
            self._tag_identity_setup_task=None
            self.tag_editor_panel.setEnabled(True)
            completion_delivered=False
            try:
                if not getattr(self,"_category_refresh_shutdown",False):
                    if result is not None:
                        self._adopt_native_operation(result)
                        self.characters.load(); self._reload_modifier_roles()
                        if self.current_tag_id==tag_id:
                            self._refresh_tag_identity(tag_id)
                            self.tag_identity_status.setText(result.message)
                            if result.status=="review":
                                self._set_tag_identity_inline_editor(None,force=True)
                                self.tag_identity_notice_text.setText(result.message)
                                self.tag_identity_repair_button.setText("Review Character Knowledge…")
                                self.tag_identity_link_existing.setVisible(True)
                                self.tag_identity_link_existing.setEnabled(True)
                        if result.changed:
                            self._changed(defer_workspace=True,dependency_kind="relationship",complete=True,tag_ids=(tag_id,))
                            source=getattr(self,"_active_tag_source_editor",None)
                            if source is not None and isValid(source):source.refresh_system_knowledge()
                        self._set_tag_save_status(result.message,error=result.status=="review")
                    else:
                        self.tag_identity_status.setText(f"Record setup needs attention: {error}. Reopen Character Knowledge to retry.")
                        self._set_tag_save_status(f"The Tag remains saved. Record setup needs attention: {error}",error=True)
                    if on_finished is not None:
                        completion_delivered=True
                        on_finished(result)
            except (OSError,RuntimeError,ValueError) as refresh_error:
                self._set_tag_save_status(f"The Tag remains saved. Refreshing Character Knowledge needs attention: {refresh_error}",error=True)
                if on_finished is not None and not completion_delivered:on_finished(result)
            finally:
                self.background_work_finished.emit()
        task.signals.complete.connect(lambda result:finished(result))
        task.signals.failed.connect(lambda error:finished(error=error))
        try:self.pool.start(task)
        except Exception as error:finished(error=str(error))

    def _link_tag_identity(self):
        self._flush_tag_save()
        if self._tag_save_pending or self._category_staging_active():
            self._set_tag_save_status("Finish saving the Tag before linking Character Knowledge.",error=True)
            return
        tag=self.tags.tags.get(self.current_tag_id)
        if tag is None:return
        parent=getattr(self,"_active_tag_editor_dialog",None) or self
        try:
            dialog=CharacterTagBridgeDialog(
                CharacterTagBridge(self.tags,self.characters,self.categories),self.tags,self.characters,
                categories=self.categories,parent=parent,tag_id=tag.id,
            )
        except ValueError as error:
            QMessageBox.warning(parent,"Character Knowledge cannot be linked",str(error)); return
        try:
            dialog.exec()
            if dialog.applied:self._inline_character_record_saved("","")
        finally:dialog.deleteLater()

    def _create_tag_identity(self,kind):
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status("Finish saving the Tag before creating Character Knowledge.",error=True)
            return
        tag=self.tags.tags.get(self.current_tag_id)
        expected={"Series":"character_source","Characters":"character_identity","Outfits":"character_outfit"}.get(str(kind))
        if tag is None or self.categories.effective_semantic_role(tag.category)!=expected:return
        if self._category_staging_active():
            QMessageBox.information(self,"Wait for System Knowledge","Create the linked typed record after the current automatic System Knowledge save finishes."); return
        parent=getattr(self,"_active_tag_editor_dialog",None) or self
        try:
            dialog=self._character_editor_factory(
                CharacterRecordEditService(self.characters,self.tags,self.categories),self.tags,self.categories,
                kind=kind,tag_id=tag.id,create=True,parent=parent,
            )
        except ValueError as error:
            QMessageBox.warning(self,"Typed record cannot be created",str(error)); return
        dialog.exec()
        if not dialog.saved:return
        self._inline_character_record_saved(kind,dialog.record_id)
        wanted=(kind,dialog.record_id)
        for index in range(self.tag_identity_tree.topLevelItemCount()):
            item=self.tag_identity_tree.topLevelItem(index)
            if item.data(0,Qt.UserRole)==wanted:self.tag_identity_tree.setCurrentItem(item); break

    def view_tag_provenance(self):
        parent=getattr(self,"_active_tag_editor_dialog",None) or self
        dialog=QDialog(parent); dialog.setWindowTitle("Knowledge provenance")
        layout=QVBoxLayout(dialog)
        summary=QLabel(self.tag_provenance.text()); summary.setTextFormat(Qt.PlainText); summary.setWordWrap(True)
        layout.addWidget(summary)
        explanation=QLabel("Provenance records explain where reusable knowledge came from; they do not confirm facts about any particular image.")
        explanation.setWordWrap(True); layout.addWidget(explanation)
        records=QTextEdit(); records.setReadOnly(True)
        records.setAccessibleName("Knowledge provenance records")
        records.setPlainText(self._tag_provenance_details or "No recorded provenance.")
        layout.addWidget(records,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        dialog.resize(bounded_window_size(parent.screen().availableGeometry(),QSize(760,520)))
        try:dialog.exec()
        finally:dialog.deleteLater()

    def _fill_category_combo(self,combo,current=""):
        combo.clear()
        choices=self.categories.choices()
        if self._category_staging_active():choices=[(record.id,self.category_workspace.session.path_label(record.id)) for record in sorted(self.category_workspace.session.all_categories(),key=lambda item:self.category_workspace.session.path_label(item.id).casefold())]
        for category_id,label in choices: combo.addItem(label,category_id)
        combo.setCurrentIndex(max(0,combo.findData(current)))

    def _suggest_order_group(self,*_):
        if self._tag_ordering_reviewed or self._tag_ordering_edited:return
        category=str(self.tag_category.currentData() or self.categories.find_id_by_label(self.tag_category.currentText()) or "uncategorized")
        group,priority,_,_=self.categories.effective_ordering(category); self.tag_order_group.setCurrentIndex(max(0,self.tag_order_group.findData(group))); self.tag_priority.setValue(priority)

    def _active_category_resolver(self):
        return self.category_workspace.session if self._category_staging_active() else self.categories

    def _refresh_tag_semantic_role(self,*_):
        if not hasattr(self,"tag_semantic_role"):
            return
        category_id=self._selected_category_id()
        resolver=self._active_category_resolver()
        role=resolver.effective_semantic_role(category_id) if category_id and hasattr(resolver,"effective_semantic_role") else ""
        label={
            "character_source":"Character Source SDXL Tag",
            "character_identity":"Character name SDXL Tag",
            "character_outfit":"Outfit name SDXL Tag",
            "reusable_modifier":"Reusable modifier SDXL Tag",
            "modifier_color":"Color modifier SDXL Tag",
            "modifier_material":"Material modifier SDXL Tag",
            "modifier_pattern":"Pattern modifier SDXL Tag",
        }.get(role,"Ordinary SDXL Tag")
        source=resolver.semantic_role_source(category_id) if role and hasattr(resolver,"semantic_role_source") else ""
        if source and source!=category_id:
            label+=f" • inherited from {resolver.path_label(source)}"
        elif role:
            label+=" • set by this category"
        self.tag_semantic_role.setText(label)
        if hasattr(self,"tag_creation_role_help"):
            guidance={
                "character_source":"Saving this Character Source Tag also sets up its editable record automatically. Exact matches are linked; missing records are created; uncertain matches stay for review. No image fact is added here.",
                "character_identity":"Automatic setup links an exact existing record or creates an editable character record after saving the Tag. Missing facts stay unset. You can choose a different outcome below; no image fact is assigned here.",
                "character_outfit":"This creates the reusable Character Outfit Tag only. After creation, Character Knowledge lets you connect the reviewed character and Character Source; the category alone never assigns the outfit to an image.",
            }.get(role,"") if not self.current_tag_id else ""
            self.tag_creation_role_help.setText(guidance)
            self.tag_creation_role_help.setVisible(bool(guidance))
        self._refresh_character_creation_decision()

    def _refresh_tag_consumer_restriction(self,record=None):
        if not hasattr(self,"tag_consumer_restriction"):return
        record=record or self.tags.tags.get(self.current_tag_id)
        if record is None:
            self.tag_consumer_restriction.clear(); self.tag_consumer_restriction.setToolTip(""); self._set_tag_basics_row_visible(self.tag_consumer_restriction,False); return
        if self._consumer_policy_resolver is None and not self._consumer_policy_error:
            try:self._consumer_policy_resolver=ConsumerPolicyResolver.from_paths(self.editor_context.defaults/"consumer_policies.json",self.editor_context.consumer_policies)
            except (OSError,RuntimeError,ValueError) as error:self._consumer_policy_error=str(error)
        resolver=self._consumer_policy_resolver
        if resolver is None:
            self.tag_consumer_restriction.clear(); self.tag_consumer_restriction.setToolTip(""); self._set_tag_basics_row_visible(self.tag_consumer_restriction,False); return
        decisions=tuple(resolver.resolve(consumer,tag_id=record.id) for consumer in CONSUMER_IDS)
        exact=tuple(decision for decision in decisions if decision.selector_type==EXACT_TAG)
        if not exact:
            self.tag_consumer_restriction.clear(); self.tag_consumer_restriction.setToolTip(""); self._set_tag_basics_row_visible(self.tag_consumer_restriction,False); return
        modes={decision.consumer:decision.mode for decision in exact}
        if (
            modes.get(SEARCH_ORGANIZATION)==CANONICALIZE_IDENTITY
            and modes.get("import_compatibility")==COMPATIBILITY_RESOLUTION
            and all(modes.get(consumer)==EXCLUDE_FROM_CONSUMER for consumer in ("image_identification","maximal_image_knowledge","training_projection","generation_prompt_planning"))
        ):
            summary="Compatibility-only identity: exact search and historical import remain available; image facts, maximal knowledge, training, and generation prompting exclude it."
        else:
            exceptions=", ".join(f"{decision.consumer.replace('_',' ')}: {decision.mode.replace('_',' ')}" for decision in exact)
            summary=f"Exact reviewed consumer exception: {exceptions}."
        details=[]
        for decision in exact:
            details.append(f"{decision.consumer}: {decision.mode}")
            details.extend(decision.explanation_path)
            details.append(decision.explanation)
        self.tag_consumer_restriction.setText(summary); self.tag_consumer_restriction.setToolTip("\n".join(details)); self._set_tag_basics_row_visible(self.tag_consumer_restriction,True)

    def _editor_effective_base(self):
        explicit=self.tag_parent.values()[0] if self.tag_parent.values() else ""
        category_id=self._selected_category_id(); resolver=self._active_category_resolver()
        current=self.tags.tags.get(self.current_tag_id)
        if (
            current is not None and current.category==category_id
            and (explicit or "")==self.tags.effective_parent_id(current)
        ):
            return self.tags.effective_base_tag(current,resolver)
        if explicit:return explicit,None,"explicit"
        if not category_id or not hasattr(resolver,"effective_base_tag"):return "",None,"none"
        base_id,source,_=resolver.effective_base_tag(category_id); base_id=str(base_id or "")
        if base_id and base_id==self.current_tag_id:return "",source,"anchor"
        return base_id,source,"inherited" if base_id else "none"

    def _refresh_tag_inheritance(self,*_):
        if not hasattr(self,"tag_base_effective"):return
        self._refresh_tag_semantic_role(); self._refresh_tag_consumer_restriction(None)
        base_id,source,kind=self._editor_effective_base(); base=self.tags.tags.get(base_id)
        resolver=self._active_category_resolver()
        current=self.tags.tags.get(self.current_tag_id)
        if base is not None:
            suffix="Explicit override" if kind=="explicit" else f"Inherited from {resolver.path_label(source)}"
            self.tag_base_effective.setText(suffix)
            self.tag_effective_base.set_values((base.id,))
        elif kind=="anchor":
            self.tag_base_effective.setText("This is the Base Tag for its category")
            self.tag_effective_base.set_values((current.id,) if current is not None else ())
        elif kind=="cycle":
            self.tag_base_effective.setText("No effective base • inherited relationship cycle needs review")
            self.tag_effective_base.set_values(())
        else:
            self.tag_base_effective.setText("No effective Base SDXL Tag")
            self.tag_effective_base.set_values(())
        self.tag_effective_base_card.setProperty("baseKind",kind)
        self.tag_effective_base_card.setAccessibleDescription(self.tag_base_effective.text())
        category_id=self._selected_category_id()
        is_anchor=bool(current and category_id and resolver.effective_base_tag(category_id)[0]==current.id)
        self.tag_make_category_base.setEnabled(bool(current and category_id and current.category==category_id and not is_anchor and not self._category_staging_active()))
        self.tag_make_category_base.setText("Category Base Tag" if is_anchor else "Use This Tag as Category Base")
        selected_rating=str(self.tag_sensitivity.currentData() or "")
        if selected_rating:self.tag_sensitivity_effective.setText(f"{selected_rating.title()} • Tag override")
        elif current is not None and current.content_sensitivity.classification_status=="intentionally_unclassified":self.tag_sensitivity_effective.setText("Intentionally unclassified • Tag override")
        elif category_id and hasattr(resolver,"effective_sensitivity"):
            rating,rating_source,_=resolver.effective_sensitivity(category_id)
            self.tag_sensitivity_effective.setText(f"{rating.title()} • Inherited from {resolver.path_label(rating_source)}" if rating and rating_source else "Not classified")
        else:self.tag_sensitivity_effective.setText("Not classified")

    def _make_current_tag_category_base(self):
        self._flush_tag_save(); record=self.tags.tags.get(self.current_tag_id); category_id=self._selected_category_id()
        if record is None or not category_id or record.category!=category_id:return
        self.tag_make_category_base.setEnabled(False); self._set_tag_save_status("Saving category Base SDXL Tag...")
        if not self.category_workspace.set_category_base_tag(category_id,record.id):
            self._set_tag_save_status("Wait for the current System Knowledge save to finish before setting the category Base SDXL Tag.",error=True)

    def _mark_tag_ordering_edited(self,*_):
        if self._loading_tag_editor:return
        record=self.tags.tags.get(self.current_tag_id)
        group=str(self.tag_order_group.currentData() or "other")
        priority=self.tag_priority.value()
        if record is None or (group,priority)!=(record.ordering.group,record.ordering.priority):self._tag_ordering_edited=True

    def _tag_relation_candidate_error(self,field,values):
        parent=(values[0] if values else None) if field=="parent" else (self.tag_parent.values()[0] if self.tag_parent.values() else None)
        implies=values if field=="implies" else self.tag_implies.values(); conflicts=values if field=="conflicts" else self.tag_conflicts.values()
        try:self.tags.validate_tag_relationships(self.current_tag_id,parent_id=parent,implies=implies,conflicts=conflicts)
        except ValueError as error:return str(error)
        return ""

    def _tag_relationship_changed(self,field):
        if self._loading_tag_editor:return
        self._tag_relationship_dirty_fields.add(field)
        if field in {"parent","attributes"}:self._refresh_tag_composition()
        self._save_tag_control_change()

    def _modifier_definitions(self):
        return self.foundation.modifier_group_definitions(
            self.tags,self.categories,self._modifier_roles,
        )

    def _reload_modifier_roles(self):
        workspace=None
        try:
            workspace=RelationshipWorkspaceService(
                self.editor_context.defaults/"relationships.json",self.editor_context.relationships,
                tags=self.tags,
            )
            self._modifier_roles=modifier_role_resolutions(
                workspace.effective,categories=self.categories,
            )
            self._modifier_role_error=""
        except (OSError,RuntimeError,ValueError) as error:
            self._modifier_role_error=str(error)
            inventory=self.foundation.legacy_modifier_inventory(
                self.tags,self.categories,
            )
            self._modifier_roles={
                candidate.tag_id:SimpleNamespace(is_ordinary=True,family_id="")
                for candidate in inventory.candidates
            }
        self._relationship_workspace=workspace
        self._reload_modifier_policies(workspace=workspace)
        self._reload_modifier_compositions(workspace=workspace)
        self._reload_tag_semantic_relationships(workspace=workspace)
        self._reload_tag_aliases(workspace=workspace)

    def _reload_modifier_policies(self,*,workspace=None):
        try:
            self._modifier_policy_service=AllowedDimensionPolicyService(
                self.tags,self.categories,self.foundation,
                default_path=self.editor_context.defaults/"relationships.json",
                user_path=self.editor_context.relationships,
                workspace=workspace,
            )
            self._modifier_policy_error=""
        except (OSError,RuntimeError,ValueError) as error:
            self._modifier_policy_service=None
            self._modifier_policy_error=str(error)

    def _reload_modifier_compositions(self,*,workspace=None):
        if self._modifier_role_error:
            self._modifier_composition_service=None
            self._modifier_compositions={}
            self._modifier_composition_error=(
                "Modifier Role authority is unavailable, so modifier composition "
                "cannot be edited safely."
            )
            return
        try:
            service=ModifierCompositionService(
                self.tags,self.categories,self.foundation,
                modifier_roles=self._modifier_roles,
                default_path=self.editor_context.defaults/"relationships.json",
                user_path=self.editor_context.relationships,
                workspace=workspace,
            )
            self._modifier_composition_service=service
            self._modifier_compositions=service.resolutions()
            self._modifier_composition_error=""
        except (OSError,RuntimeError,ValueError) as error:
            self._modifier_composition_service=None
            self._modifier_compositions={}
            self._modifier_composition_error=str(error)

    def _reload_tag_semantic_relationships(self,*,workspace=None):
        try:
            self._tag_semantic_service=TagSemanticRelationshipService(
                self.tags,
                default_path=self.editor_context.defaults/"relationships.json",
                user_path=self.editor_context.relationships,
                workspace=workspace,
            )
            self._tag_semantic_error=""
        except (OSError,RuntimeError,ValueError) as error:
            self._tag_semantic_service=None
            self._tag_semantic_error=str(error)

    def _reload_tag_aliases(self,*,workspace=None):
        try:
            self._tag_alias_service=TagAliasRelationshipService(
                self.tags,
                default_path=self.editor_context.defaults/"relationships.json",
                user_path=self.editor_context.relationships,
                workspace=workspace,
            )
            self._tag_alias_error=""
        except (OSError,RuntimeError,ValueError) as error:
            self._tag_alias_service=None
            self._tag_alias_error=str(error)

    def _modifier_group_label(self,group):
        return self.foundation.modifier_group_label(group,self.categories)

    def _refresh_modifier_family_choices(self):
        definitions=self._modifier_definitions()
        choices=[]
        for group,values in definitions.items():
            source=self.categories.categories.get(group)
            source_path=self.categories.path_label(group) if source is not None else "Compatibility family"
            choices.append((
                group,self._modifier_group_label(group),
                f"{len(values):,} reviewed value{'s' if len(values)!=1 else ''} • {source_path}",
            ))
        self.tag_allowed_modifier_editor.set_choices(choices)

    def _suggested_base_from_tag_name(self,record):
        if record is None:return None
        words=normalize_tag(record.display).split()
        for start in range(1,len(words)):
            candidate=self.tags.find(" ".join(words[start:]))
            if candidate is not None and candidate.id!=record.id:return candidate
        return None

    def _refresh_tag_composition(self,*_):
        if self.tag_scope_page is None:return
        base_id,_,_=self._editor_effective_base(); base=self.tags.tags.get(base_id)
        current=self.tags.tags.get(self.current_tag_id)
        definitions=self._modifier_definitions()
        labels={group:self._modifier_group_label(group) for group in definitions}
        self._refresh_tag_inheritance()
        self.tag_implies.set_excluded_ids((base_id,) if base_id else ())
        if base is None:
            suggested=self._suggested_base_from_tag_name(current)
            reason="Modifier values describe how this exact tag specializes a Base SDXL Tag. Choose a Base Tag above before adding a reviewed value."
            if suggested is not None:reason+=f" Possible base from this tag’s exact name: {suggested.display}. Review it with Choose Base Tag; it will not be selected automatically."
            self.tag_attributes.configure(
                (),definitions,labels=labels,available=False,reason=reason,
            )
        else:
            policy=(
                self._modifier_policy_service.effective_for_tag(base.id)
                if self._modifier_policy_service is not None else
                self.foundation.effective_modifier_policy(
                    base,self.tags,self.categories,
                )
            )
            groups=list(policy.groups)
            definitions={
                group:tuple(
                    value for value in definitions.get(group,())
                    if (
                        self._modifier_policy_service.allows_value(
                            policy,group,value.id,
                        )
                        if self._modifier_policy_service is not None else
                        policy.allows_value(group,value.id)
                    )
                )
                for group in groups
            }
            suggestions=self.foundation.suggest_modifier_slots(
                current,base,groups,definitions,self.tag_attributes.values(),
            )
            suggestions=tuple(
                suggestion for suggestion in suggestions
                if not self._modifier_suggestion_was_rejected(
                    current,base,suggestion,
                )
            )
            if self._modifier_composition_error:
                reason=(
                    "Canonical modifier relationships are unavailable. Existing "
                    "values remain visible, but changes are blocked so the inline "
                    "compatibility field cannot become authority again ("+
                    self._modifier_composition_error+")."
                )
                self.tag_attributes.configure(
                    (),definitions,labels=labels,available=False,reason=reason,
                )
            elif not groups:
                reason=f'{base.display} has no reviewed modifier policy. Existing modifier values are preserved, but none can be added or removed here.'
                self.tag_attributes.configure(
                    (),definitions,labels=labels,available=False,reason=reason,
                )
            else:
                self.tag_attributes.configure(
                    groups,definitions,suggestions,labels=labels,
                )
        self._refresh_tag_composition_preview(current,base)
        self._refresh_tag_reuse_policy(current)

    def _modifier_suggestion_was_rejected(self,record,base,suggestion):
        resolution=self._modifier_compositions.get(
            getattr(record,"id",""),{},
        ).get(str(suggestion.group))
        if resolution is not None:
            return bool(
                resolution.operation=="reject_exact_name_suggestion"
                and resolution.suggested_value_id==suggestion.suggested_value_id
                and resolution.base_tag_id==getattr(base,"id","")
                and resolution.tag_name==normalize_tag(getattr(record,"display",""))
            )
        provenance=getattr(record,"relation_extras",{}).get(
            "modifier_value_provenance",{},
        )
        decision=(
            provenance.get(suggestion.group,{})
            if isinstance(provenance,dict) else {}
        )
        return bool(
            isinstance(decision,dict)
            and decision.get("operation")=="reject_exact_name_suggestion"
            and decision.get("suggested_value_id")==suggestion.suggested_value_id
            and decision.get("base_tag_id")==getattr(base,"id","")
            and decision.get("tag_name")==normalize_tag(getattr(record,"display",""))
        )

    def _refresh_tag_composition_preview(self,record,base):
        if base is None:
            self.tag_composition_preview.setText(
                "No composition is resolved until a Base SDXL Tag is reviewed."
            )
            return
        definitions=self._modifier_definitions(); by_id={
            str(value.id):(group,str(value.display))
            for group,values in definitions.items() for value in values
        }
        resolved=self.tag_attributes.resolved_values(); parts=[]
        for group in self.foundation.ordered_modifier_groups(resolved,self.categories):
            value_id=resolved.get(group,""); display=by_id.get(value_id,(group,value_id))[1]
            parts.append(f"{self._modifier_group_label(group)}: {display}")
        parts.append(f"Base: {base.display}")
        target=getattr(record,"display","") or "exact tag"
        preview=" + ".join(parts)+f" → {target}"
        if self.tag_attributes.conflict_groups():
            preview="Review required — "+preview
        if self.tag_attributes.preserved_values():
            preview+=". Preserved values outside these named slots remain unresolved."
        self.tag_composition_preview.setText(preview)

    def _refresh_tag_reuse_policy(self,record):
        self.tag_reuse_group.setVisible(record is not None)
        if record is None:return
        self._loaded_modifier_policy_fingerprint=""
        self._refresh_modifier_family_choices()
        modifier=self.foundation.modifier_for_tag(
            record,self.categories,self._modifier_roles,
        )
        if modifier is not None:
            group=modifier.group; group_label=self._modifier_group_label(group)
            undo_batch=self._last_modifier_policy_batch
            can_undo=bool(
                undo_batch
                and undo_batch.get("modifier_tag_id")==record.id
                and undo_batch.get("group")==group
            )
            self.tag_modifier_compatibility_undo.setVisible(can_undo)
            self.tag_modifier_compatibility_undo.setEnabled(
                can_undo and self._modifier_policy_save_task is None
            )
            self.tag_reuse_summary.setText(
                f'“{record.display}” is a {group_label} modifier value. Compatible Base Tags are loaded only when you open the review workspace.'
            )
            self.tag_modifier_compatibility_intro.setText(
                f"Review which canonical Base Tag policies allow the {group_label} family. Results load in the background, remain bounded and searchable, and save only as one deliberate batch."
            )
            self.tag_reuse_policy_host.hide(); self.tag_modifier_compatibility_host.show()
            self.tag_modifier_compatibility_button.setEnabled(
                self._modifier_policy_service is not None
            )
            if self._modifier_policy_service is None:
                self.tag_modifier_compatibility_intro.setText(
                    "Allowed Details relationships are unavailable. Compatibility "
                    "remains visible only through current saved projections; reload "
                    "after resolving the relationship store problem ("+
                    (self._modifier_policy_error or "unknown relationship error")+
                    ")."
                )
            self.tag_pattern_preview.hide()
            self.tag_reuse_source.setText("Derived from the reusable policy on each compatible base tag.")
            self.tag_reuse_inherit.hide()
            return
        self.tag_modifier_compatibility_undo.hide()
        if self._modifier_policy_service is not None:
            policy=self._modifier_policy_service.effective_for_tag(record.id)
            self._loaded_modifier_policy_fingerprint=policy.record_fingerprint
        else:
            policy=self.foundation.effective_modifier_policy(
                record,self.tags,self.categories,
            )
        groups=list(policy.groups); labels=[self._modifier_group_label(group) for group in groups]
        self.tag_allowed_modifier_editor.set_values(groups)
        policy_blocked=bool(
            self._modifier_policy_error or getattr(policy,"blocked_reason","")
        )
        self.tag_allowed_modifier_editor.setEnabled(not policy_blocked)
        self.tag_reuse_policy_host.show(); self.tag_modifier_compatibility_host.hide()
        if labels:
            self.tag_reuse_summary.setText(
                "This tag can be specialized by "+", ".join(labels)+"."
            )
        elif policy.is_resolved:
            self.tag_reuse_summary.setText(
                "This tag allows no reusable modifier families under its "
                f"{policy.source_kind} policy."
            )
        else:
            self.tag_reuse_summary.setText(
                "No reusable modifier policy is set. Choose the families that can form variations of this tag."
            )
        self.tag_reuse_source.setText(policy.source_label or "No inherited or explicit policy")
        if policy_blocked:
            self.tag_reuse_source.setText(
                "Allowed Details cannot be edited safely until relationship "
                "authority is available ("+
                (getattr(policy,"blocked_reason","") or self._modifier_policy_error)+
                ")."
            )
        if self._modifier_role_error and any(
            self.foundation.modifiers_for(value)
            for value in (record.display,*record.aliases)
        ):
            self.tag_reuse_source.setText(
                "Modifier Role decisions are unavailable; this legacy-name "
                "candidate is failing closed as ordinary until recovery."
            )
        if self._modifier_policy_service is not None:
            has_direct_user_policy=(
                getattr(policy,"authority","") in {"canonical_user","compatibility"}
                and getattr(policy,"source_tag_id","")==record.id
            )
        else:
            has_direct_user_policy=record.allowed_modifier_groups is not None
        self.tag_reuse_inherit.setVisible(has_direct_user_policy)
        self.tag_reuse_inherit.setEnabled(not policy_blocked)
        self.tag_reuse_values.setVisible(bool(groups))
        self.tag_reuse_values.setEnabled(bool(groups) and not policy_blocked)
        if groups:
            self.tag_pattern_preview.setText(
                "Value scopes — "+"; ".join(
                    f"{self._modifier_group_label(group)}: "+(
                        "all values" if policy.scope_for(group).mode==VALUE_SCOPE_ALL else
                        f"only {len(policy.scope_for(group).value_ids)+len(policy.scope_for(group).category_ids):,} chosen branch/Tag rules" if policy.scope_for(group).mode==VALUE_SCOPE_ONLY else
                        f"all except {len(policy.scope_for(group).value_ids)+len(policy.scope_for(group).category_ids):,} chosen branch/Tag rules" if policy.scope_for(group).mode==VALUE_SCOPE_ALL_EXCEPT else
                        "review needed"
                    ) for group in groups
                )+"."
            )
            self.tag_pattern_preview.show()
        else:
            self.tag_pattern_preview.setText(record.display)
            self.tag_pattern_preview.hide()

    def _set_tag_reuse_feedback(self,text,*,error=False):
        label=getattr(self,"tag_reuse_feedback",None)
        if label is None:return
        label.setText(text); label.setVisible(bool(text))
        label.setObjectName("errorLabel" if error else "mutedLabel")
        label.style().unpolish(label); label.style().polish(label)

    def _save_reuse_policy(self,record,groups,message,*,operation):
        service=self._modifier_policy_service
        if service is None:
            self._set_tag_reuse_feedback(
                "Allowed Details not saved. Relationship authority is unavailable; "
                "the Tag draft remains unchanged.",error=True,
            )
            self._set_tag_save_status(
                "Not saved — Allowed Details relationship authority is unavailable ("+
                (self._modifier_policy_error or "unknown relationship error")+
                ").",error=True,
            )
            self._refresh_tag_composition(); return False
        update=groups
        if groups is not None and not isinstance(groups,AllowedDimensionPolicyUpdate):
            current=service.effective_for_tag(record.id)
            desired=tuple(self.foundation.ordered_modifier_groups(groups,self.categories))
            update=AllowedDimensionPolicyUpdate(
                desired,tuple(
                    current.scope_for(group)
                    if group in current.groups else AllowedDimensionValueScope(group)
                    for group in desired
                ),
            )
        try:
            result=self._run_native_operation("modifier_policy",lambda:service.apply(
                {record.id:update},
                expected_fingerprints={
                    record.id:self._loaded_modifier_policy_fingerprint,
                },
                operation=operation,
                review_source="qt_knowledge_editor",
            ))
            self._adopt_native_operation(result)
        except (OSError,RuntimeError,ValueError) as error:
            if self.editor_context.standalone:
                self._modifier_compatibility_save_failed(str(error))
                current=None
            else:
                self.tags.load(); self._reload_modifier_roles()
                current=self.tags.tags.get(self.current_tag_id)
            if current is not None:
                # This policy saves independently of the Tag Details draft.
                # Rebind only its authority after failure: loading the whole
                # Tag would replace pending notes/name/relationships and clear
                # _tag_save_pending without the user's Save or Discard choice.
                was_loading=self._loading_tag_editor
                self._loading_tag_editor=True
                try:self._refresh_tag_reuse_policy(current)
                finally:self._loading_tag_editor=was_loading
            draft_note=(
                " The unsaved Tag draft remains available."
                if self._tag_save_pending else ""
            )
            self._set_tag_save_status(
                f"Allowed Details save not confirmed.{draft_note} Review its current policy before retrying.",
                error=True,
            )
            self._set_tag_reuse_feedback(
                f"Allowed Details save not confirmed — {error}. Keep any recovery records and backups; review the current policy before retrying."
                +(" The separate Tag draft is still unsaved." if self._tag_save_pending else ""),
                error=True,
            )
            return False
        self._reload_modifier_roles()
        self._routine_change_saved({
            "kind":"modifier_policy","tag_ids":(record.id,),
        })
        if self._tag_save_pending:
            message+=" Other Tag Details are still unsaved; choose Save Tag or Discard Tag Draft."
        self._set_tag_save_status(message); self._refresh_tag_composition()
        self._set_tag_reuse_feedback(message)
        return True

    def _review_tag_value_scopes(self):
        if self._loading_tag_editor:return
        record=self.tags.tags.get(self.current_tag_id); service=self._modifier_policy_service
        if record is None or service is None:return
        policy=service.effective_for_tag(record.id)
        if policy.blocked_reason or not policy.groups:return
        definitions=self._modifier_definitions()
        labels={group:self._modifier_group_label(group) for group in policy.groups}
        dialog=ModifierValueScopeDialog(
            record,policy,definitions,labels,
            tags=self.tags,categories=self.categories,
            parent=getattr(self,"_active_tag_editor_dialog",None) or self,
        )
        if dialog.exec()!=QDialog.Accepted:return
        self._save_reuse_policy(
            record,dialog.policy_update(),"Saved reusable value scopes.",
            operation="review_value_scopes",
        )

    def _tag_reuse_policy_changed(self,values):
        if self._loading_tag_editor:return
        record=self.tags.tags.get(self.current_tag_id)
        if record is None or self.foundation.modifier_for_tag(
            record,self.categories,self._modifier_roles,
        ) is not None:return
        groups=tuple(self.foundation.ordered_modifier_groups(values,self.categories))
        self._save_reuse_policy(
            record,groups,"Saved reusable modifier policy automatically.",
            operation="manual_policy_edit",
        )

    def _reset_tag_reuse_policy(self):
        if self._loading_tag_editor:return
        record=self.tags.tags.get(self.current_tag_id); service=self._modifier_policy_service
        if record is None or service is None:return
        current=service.effective_for_tag(record.id)
        if (
            current.authority not in {"canonical_user","compatibility"}
            or current.source_tag_id!=record.id
        ):return
        fallback=service.effective_after_reset(record.id)
        def describe(policy):
            if policy.blocked_reason:
                return "Unavailable — "+policy.blocked_reason
            labels=[self._modifier_group_label(group) for group in policy.groups]
            if labels:return ", ".join(labels)
            if policy.is_resolved:return "No allowed details"
            return "No direct or inherited policy"
        current_authority=(
            "Personal relationship policy"
            if current.authority=="canonical_user" else
            "Compatibility policy awaiting review"
        )
        fallback_authority=fallback.source_label or "No fallback authority"
        parent=getattr(self,"_active_tag_editor_dialog",None) or self
        if QMessageBox.question(
            parent,"Reset Direct Allowed Details Policy?",
            f'Reset the direct Allowed Details policy for “{record.display}”?\n\n'
            f"Current — {current_authority}: {describe(current)}.\n"
            f"After reset — {fallback_authority}: {describe(fallback)}.\n\n"
            "This clears the direct relationship policy and its old-reader "
            "compatibility projection with one recovery copy. Image Knowledge, "
            "captions, TXT, Metadata, raw evidence, and images stay unchanged.",
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No,
        )!=QMessageBox.Yes:return
        self._save_reuse_policy(
            record,None,"Reset the direct Allowed Details policy and restored its reviewed fallback.",
            operation="restore_inherited_policy",
        )

    def _show_modifier_compatibility_workspace(self):
        if self._loading_tag_editor:return
        if self._modifier_policy_save_task is not None:
            QMessageBox.information(self,"Reusable policy is still saving","Wait for the current compatible Base Tag policy batch to finish saving."); return
        self._flush_tag_save()
        if self._tag_save_pending:
            QMessageBox.information(self,"Finish Tag Details first","Correct or save the pending Tag Details before reviewing compatible Base Tags."); return
        record=self.tags.tags.get(self.current_tag_id); modifier=self.foundation.modifier_for_tag(record,self.categories,self._modifier_roles) if record is not None else None
        if record is None or modifier is None:return
        parent=getattr(self,"_active_tag_editor_dialog",None) or self
        dialog=ModifierCompatibilityDialog(
            self.tags,self.categories,self.foundation,record,modifier,
            policy_service=self._modifier_policy_service,
            pool=self.pool,parent=parent,
        )
        if dialog.exec()!=QDialog.Accepted:return
        self._apply_modifier_compatibility_request(
            record.id,modifier.group,dialog.original_ids(),dialog.requested_ids(),
            reviewed_ids=dialog.reviewed_ids(),
            expected_fingerprints=dialog.policy_fingerprints(),
        )

    def _apply_modifier_compatibility_request(self,modifier_tag_id,group,original_ids,requested_ids,*,reviewed_ids=(),expected_fingerprints=None):
        if self._modifier_policy_save_task is not None:return False
        service=self._modifier_policy_service
        if service is None:
            self._set_tag_save_status(
                "Reusable policy batch was not saved — Allowed Details "
                "relationship authority is unavailable.",error=True,
            ); return False
        modifier_record=self.tags.tags.get(str(modifier_tag_id)); group=self.foundation.canonical_modifier_group(str(group or ""),self.categories)
        previous=set(str(value) for value in original_ids); requested=set(str(value) for value in requested_ids); retained={str(value) for value in reviewed_ids}&previous&requested; changed=previous.symmetric_difference(requested)|retained
        if modifier_record is None or not group or not changed:return False
        unavailable={stable_id for stable_id in changed if stable_id not in self.tags.tags or stable_id==modifier_record.id}
        if unavailable:
            self._set_tag_save_status(
                f"Compatibility was not saved because {len(unavailable):,} staged Base Tag record{' is' if len(unavailable)==1 else 's are'} no longer available. Reopen the review to use current System Knowledge.",error=True,
            ); return False
        updates={}; fingerprints={}; operations={}
        modifier_category_path=self.categories.path_ids(modifier_record.category)
        for stable_id in changed:
            policy=service.effective_for_tag(stable_id)
            if policy.blocked_reason:
                self._set_tag_save_status(
                    f"Reusable policy batch was not saved — {policy.blocked_reason}",
                    error=True,
                ); return False
            expected=(expected_fingerprints or {}).get(stable_id)
            fingerprints[stable_id]=str(expected or policy.record_fingerprint)
            groups=list(policy.groups)
            operation="retain" if stable_id in retained else "add" if stable_id in requested else "remove"
            scopes={scope.family_id:scope for scope in policy.value_scopes}
            current=scopes.get(group,AllowedDimensionValueScope(group))
            if operation in {"add","retain"}:
                if group not in groups:groups.append(group)
                current=_value_scope_with_exact(
                    current,modifier_record.id,allowed=True,
                    category_path_ids=modifier_category_path,
                )
            else:
                current=_value_scope_with_exact(
                    current,modifier_record.id,allowed=False,
                    category_path_ids=modifier_category_path,
                )
            scopes[group]=current
            ordered=tuple(self.foundation.ordered_modifier_groups(groups,self.categories))
            updates[stable_id]=AllowedDimensionPolicyUpdate(
                ordered,tuple(scopes.get(family,AllowedDimensionValueScope(family)) for family in ordered),
            )
            operations[stable_id]=operation
        self.tag_editor_panel.setEnabled(False); group_label=self._modifier_group_label(group); self._set_tag_save_status(f"Saving one {group_label} compatibility batch for {len(changed):,} Base Tag{'s' if len(changed)!=1 else ''}…")
        task=AsyncCallTask(lambda:self._run_native_operation("modifier_policy",lambda:service.apply(
            updates,expected_fingerprints=fingerprints,
            operation="reverse_compatibility_batch",
            tag_operations=operations,
            review_source="qt_modifier_compatibility_workspace",
            modifier_tag_id=modifier_record.id,
            reviewed_family_id=group,
        ))); self._modifier_policy_save_task=task
        task.signals.complete.connect(
            lambda result,ids=tuple(sorted(changed)),label=group_label,
            family=group,modifier_id=modifier_record.id:
            self._modifier_compatibility_saved(
                ids,label,family,modifier_id,result,
            )
        )
        task.signals.failed.connect(self._modifier_compatibility_save_failed)
        self.pool.start(task); return True

    def _modifier_compatibility_saved(self,changed_ids,group_label,group,modifier_tag_id,result):
        try:self._adopt_native_operation(result)
        except Exception as error:
            self._modifier_compatibility_save_failed(str(error)); return
        self._modifier_policy_save_task=None; self._last_modifier_policy_batch={
            "modifier_tag_id":modifier_tag_id,"group":group,"result":result,
        }; self.tag_editor_panel.setEnabled(True); self._reload_modifier_roles()
        self._routine_change_saved({"kind":"modifier_policy","tag_ids":tuple(changed_ids)})
        self._refresh_tag_composition(); self._set_tag_save_status(f"Saved {group_label} compatibility on {len(changed_ids):,} Base Tag{'s' if len(changed_ids)!=1 else ''} as one reviewed batch.")

    def _modifier_compatibility_save_failed(self,message):
        self._modifier_policy_save_task=None
        self.tag_editor_panel.setEnabled(True)
        self._reload_modifier_roles(); self._refresh_tag_composition()
        self._set_tag_save_status(
            f"Reusable policy batch completion not confirmed — {message}. "
            "Keep any recovery records and backups; review the current policy before retrying.",
            error=True,
        )

    def _undo_last_modifier_compatibility_batch(self):
        if self._modifier_policy_save_task is not None:return False
        batch=self._last_modifier_policy_batch
        if not batch:return False
        result=batch.get("result"); service=self._modifier_policy_service
        if result is None or service is None:return False
        changed_ids=tuple(result.tag_ids); group_label=self._modifier_group_label(batch.get("group") or "modifier")
        self.tag_editor_panel.setEnabled(False); self._set_tag_save_status(f"Undoing the last {group_label} compatibility batch…")
        task=AsyncCallTask(lambda:self._run_native_operation("modifier_policy",lambda:service.restore(result))); self._modifier_policy_save_task=task
        def adopted_undo(result):
            try:self._adopt_native_operation(result)
            except Exception as error:
                self._modifier_compatibility_save_failed(str(error)); return
            self._modifier_compatibility_undo_saved(changed_ids,group_label)
        task.signals.complete.connect(adopted_undo)
        task.signals.failed.connect(self._modifier_compatibility_save_failed)
        self.pool.start(task); return True

    def _modifier_compatibility_undo_saved(self,changed_ids,group_label):
        self._modifier_policy_save_task=None; self._last_modifier_policy_batch=None; self.tag_editor_panel.setEnabled(True); self._reload_modifier_roles()
        self._routine_change_saved({"kind":"modifier_policy","tag_ids":tuple(changed_ids)})
        self._refresh_tag_composition(); self._set_tag_save_status(f"Undid the last {group_label} compatibility batch for {len(changed_ids):,} Base Tag{'s' if len(changed_ids)!=1 else ''}.")

    def _run_native_operation(self, kind, operation, **kwargs):
        if self._native_operation_runner is None:return operation()
        return self._native_operation_runner(operation,kind=kind,**kwargs)

    def _adopt_native_operation(self, result):
        if self._native_operation_adopted is not None:
            self._native_operation_adopted(self,result)

    def _load_tag_examples(self,record):
        if self._tag_gallery_task is not None and hasattr(self._tag_gallery_task,"cancel"):
            self._tag_gallery_task.cancel()
        self._tag_gallery_generation+=1; generation=self._tag_gallery_generation
        context=f'Examples using “{record.display}”'; index=getattr(getattr(self.state,"dataset",None),"index",None)
        if index is None:
            self.tag_gallery.set_unavailable(context=context); return
        keys={normalize_tag(value) for value in self._tag_names(record) if normalize_tag(value)}
        self.tag_gallery.set_loading(context)
        groups=(frozenset(keys),)
        def work(job=None):
            progress=(
                (lambda current,total,message:self._background_jobs.update(
                    job.id,current,total,message,
                )) if job is not None else None
            )
            return self._evidence_queries.query(
                index,groups,True,
                should_continue=job.checkpoint if job is not None else None,
                progress=progress,
            )
        if self._background_jobs is not None:
            task=CoordinatedCallTask(
                self._background_jobs,"Search Tag Encyclopedia examples",work,
                resource="cpu",
                key=f"evidence:{self._evidence_queries.request_key(index,groups,True)}",
            )
            self._tag_gallery_task=task
            task.signals.complete.connect(
                lambda paths,g=generation,r=record.id:
                self._tag_examples_loaded((g,r,paths))
            )
            task.signals.failed.connect(
                lambda message,g=generation,c=context:
                self._tag_examples_failed(g,c,message)
            )
            task.start()
        else:
            task=AsyncCallTask(work); self._tag_gallery_task=task
            task.signals.complete.connect(
                lambda paths,g=generation,r=record.id:
                self._tag_examples_loaded((g,r,paths))
            )
            task.signals.failed.connect(
                lambda message,g=generation,c=context:
                self._tag_examples_failed(g,c,message)
            )
            self.pool.start(task)

    def _tag_examples_loaded(self,result):
        generation,record_id,paths=result
        if generation!=self._tag_gallery_generation or record_id!=self.current_tag_id:return
        record=self.tags.tags.get(record_id)
        if record is None:return
        self.tag_gallery.set_paths(
            paths,total_count=len(paths),context=f'Examples using “{record.display}”',
            configure=self.gallery_status.configure,
        )
        self._tag_gallery_task=None

    def _tag_examples_failed(self,generation,context,message):
        if generation!=self._tag_gallery_generation:return
        self.tag_gallery.set_error(message,context); self._tag_gallery_task=None

    def refresh_visible_gallery_statuses(self):
        paths=list(self.tag_gallery.visible_paths()) if self.tag_scope_page is not None else []
        if hasattr(self,"category_workspace"):paths.extend(self.category_workspace.gallery.visible_paths())
        self.gallery_status.request(paths,force=True)
        self.content_ratings_requested.emit(tuple(dict.fromkeys(paths)))

    def _image_data_changed(
        self,image=None,images=(),*,evidence_changed=False,**_payload,
    ):
        image_values=(
            (images,)
            if isinstance(images,(str,Path))
            else tuple(images or ())
        )
        affected=tuple(dict.fromkeys(
            str(Path(value))
            for value in ([image] if image else [])+list(image_values)
            if value
        ))
        if not affected:
            return
        self.gallery_status.invalidate(affected)
        visible=[
            *(self.tag_gallery.visible_paths() if self.tag_scope_page is not None else ()),
            *(
                self.category_workspace.gallery.visible_paths()
                if hasattr(self,"category_workspace") else ()
            ),
        ]
        affected_keys={value.casefold() for value in affected}
        refresh=tuple(
            value for value in dict.fromkeys(visible)
            if str(Path(value)).casefold() in affected_keys
        )
        if refresh:
            self.gallery_status.request(refresh)
        if not evidence_changed:
            return
        record=self.tags.tags.get(self.current_tag_id)
        if record is not None and self.tag_scope_page is not None:
            self._load_tag_examples(record)
        if hasattr(self,"category_workspace"):
            self.category_workspace.refresh_examples()

    def _request_visible_content_ratings(self):
        paths=list(self.tag_gallery.visible_paths()) if self.tag_scope_page is not None else []
        if hasattr(self,"category_workspace"):paths.extend(self.category_workspace.gallery.visible_paths())
        if paths:self.content_ratings_requested.emit(tuple(dict.fromkeys(paths)))

    def closeEvent(self,event):
        if self._tag_save_task is not None:
            self._set_tag_save_status("Wait for the verified Tag save to finish before closing.")
            event.ignore()
            return
        if self.has_active_background_work():
            self.request_background_shutdown()
            if not self._close_after_maintenance:
                self._close_after_maintenance=True
                self.background_work_finished.connect(self.close)
            event.ignore()
            return
        self.request_background_shutdown()
        self.state.events.unsubscribe(
            "image_data_changed",self._image_data_event_callback,
        )
        super().closeEvent(event)

    def new_tag(self):
        self._ensure_tag_editor()
        self._set_tag_basics_row_visible(self.tag_identity_notice,False)
        if not self._set_tag_identity_inline_editor(None,force=True):
            self._set_tag_save_status("Finish or correct the Character Knowledge edit before creating another SDXL Tag.",error=True)
            return
        self._tag_save_timer.stop(); self._tag_save_pending=False; self._loading_tag_editor=True; self.current_tag_id=""; self.tag_editor_tabs.setCurrentIndex(0); self.tag_name.clear(); self._fill_category_combo(self.tag_category,"uncategorized"); self.tag_parent.set_values(()); self.tag_aliases.set_values(()); self.tag_attributes.set_values(()); self.tag_implies.set_values(()); self.tag_conflicts.set_values(()); self.tag_constraint_summary.setText("Create the SDXL Tag before reviewing canonical conflict rules."); self._tag_relationship_dirty_fields.clear(); self._refresh_tag_composition(); self.tag_order_group.setCurrentIndex(max(0,self.tag_order_group.findData("other"))); self.tag_priority.setValue(ORDER_GROUP_DEFAULT_PRIORITY["other"]); self._tag_ordering_reviewed=False; self._tag_ordering_edited=False; self.tag_sensitivity.setCurrentIndex(0); self.tag_content_flags.clear(); self.tag_notes.clear(); self.tag_provenance.setText("No recorded provenance"); self._tag_provenance_details=""; self.tag_stats.setText("New knowledge entry"); self.tag_identity_tree.clear(); self.tag_identity_details.clear(); self.tag_editor_tabs.setTabVisible(self.tag_identity_tab_index,False); self.tag_character_creation_action.setCurrentIndex(0); self.tag_character_creation_existing.setCurrentIndex(0); self.tag_character_creation_alias.setChecked(False); self._tag_character_alias_record_id=""; self.tag_appearance_editor.refresh_context(category_id="uncategorized"); self._tag_gallery_generation+=1; self.tag_gallery.set_unavailable("Create the SDXL Tag before looking for matching images.","Example images"); self._loading_tag_editor=False
        self._refresh_tag_semantic_role()
        self._refresh_tag_consumer_restriction(None)
        self.tag_aliases.set_read_only(
            "Canonical Other Name authority is unavailable."
            if self._tag_alias_service is None else ""
        )
        for editor in (self.tag_parent,self.tag_attributes,self.tag_implies,self.tag_conflicts):editor.setEnabled(False)
        self._update_tag_meaning_review_path("")
        self._update_tag_broader_review_path("")
        self._set_tag_editor_creation_mode(True); self._set_tag_save_status("New entries are created only when you choose Create SDXL Tag.")

    def _set_tag_save_status(self,text: str,*,error=False):
        if self.tag_scope_page is None:return
        summary=""
        if not error and text.startswith("Tag saved. "):
            summary="Tag saved. Full status in Details."
        elif not error and text.startswith("SDXL Tag created. "):
            summary="SDXL Tag created. Full status in Details."
        self.tag_save_status.setProperty("footerSummary",summary)
        self.tag_save_status.setText(text); self.tag_save_status.setObjectName("errorLabel" if error else "mutedLabel")
        self.tag_save_status.style().unpolish(self.tag_save_status); self.tag_save_status.style().polish(self.tag_save_status)
        dialog_status=getattr(self,"_tag_dialog_status",None)
        if dialog_status is not None and dialog_status is not self.tag_save_status:
            dialog_status.setText(text if error else "")
            dialog_status.setVisible(bool(error))

    def _discard_unsaved_tag_changes(self):
        if (self._tag_save_task is not None or getattr(self,"_tag_identity_setup_task",None) is not None
            or self._modifier_policy_save_task is not None or self._category_staging_active()):
            self._set_tag_save_status("Wait for the current save or setup to finish before discarding Tag changes.",error=True)
            return
        if not self._tag_save_pending:
            self._set_tag_save_status("No unsaved Tag changes to discard.")
            return
        record=self.tags.tags.get(self.current_tag_id)
        if record is None:
            self._set_tag_save_status("The saved Tag is unavailable. Keep this draft and reload System Knowledge before discarding.",error=True)
            return
        inline=getattr(self,"tag_identity_inline_editor",None)
        if inline is not None and (inline._dirty or inline._save_task is not None):
            self._set_tag_save_status("Finish or correct the separate Character Knowledge edit before discarding Tag changes.",error=True)
            return
        getattr(self,"_preapproved_alias_removals",set()).clear()
        self._pending_tag_conflict_handoff=None
        self._load_tag_record_into_editor(record,changing_record=False,load_examples=False,update_status=False)
        self._set_tag_save_status("Draft discarded.")

    def _queue_tag_save(self,*_):
        if self._loading_tag_editor or not self.current_tag_id or self._category_staging_active():return
        if getattr(self,"_tag_dialog_batch_edit",False):
            self._tag_save_pending=True; self._set_tag_save_status("Unsaved. Save Tag or Close."); return
        self._tag_save_pending=True; self._set_tag_save_status("Saving changes..."); self._tag_save_timer.start()

    def _queue_tag_notes_save(self,*_):
        if self._loading_tag_editor or not self.current_tag_id or self._category_staging_active():return
        self._tag_save_timer.stop()
        self._tag_save_pending=True
        self._set_tag_save_status("Unsaved notes. Save Tag or Close." if getattr(self,"_tag_dialog_batch_edit",False) else "Notes changed. Saving when editing finishes.")

    def _pending_meaning_proposals(self,tag_id):
        reviews=getattr(self.state,"reviews",None)
        if reviews is None:return []
        return sorted((
            task for task in reviews.tasks.values()
            if task.status=="pending"
            and task.task_type==TAG_MEANING_TASK_TYPE
            and str(task.details.get("tag_id") or "")==str(tag_id or "")
        ),key=lambda task:(task.created_at,task.id))

    def _update_tag_meaning_review_path(self,tag_id):
        proposals=self._pending_meaning_proposals(tag_id) if tag_id else []
        count=len(proposals)
        self.tag_meaning_review_button.setVisible(bool(count))
        self.tag_meaning_review_button.setEnabled(bool(count))
        self.tag_meaning_review_status.setText(
            f"{count:,} attributed update{'s' if count!=1 else ''} waiting in Review Center"
            if count else "No attributed updates are waiting"
        )

    def _review_current_tag_meanings(self):
        proposals=self._pending_meaning_proposals(self.current_tag_id)
        if not proposals:
            QMessageBox.information(
                self,"No attributed updates",
                "No source-attributed Meaning & Usage proposal is currently waiting for this exact SDXL Tag. Run Find New Knowledge to check the local evidence inbox.",
            )
            self._update_tag_meaning_review_path(self.current_tag_id)
            return
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status(
                "Wait for the current Meaning & Usage edit to finish saving before reviewing attributed evidence.",error=True,
            )
            return
        self.review_meaning_proposals_requested.emit(proposals[0].id)

    def _pending_broader_proposals(self,tag_id):
        reviews=getattr(self.state,"reviews",None)
        if reviews is None:return []
        return sorted((
            task for task in reviews.tasks.values()
            if task.status=="pending"
            and task.task_type==TAG_BROADER_PROPOSAL_TASK_TYPE
            and str(task.details.get("tag_id") or "")==str(tag_id or "")
        ),key=lambda task:(task.created_at,task.id))

    def _update_tag_broader_review_path(self,tag_id):
        proposals=self._pending_broader_proposals(tag_id) if tag_id else []
        count=len(proposals)
        self.tag_broader_review_host.setVisible(bool(count))
        self.tag_broader_review_button.setEnabled(bool(count))
        self.tag_broader_review_status.setText(
            f"{count:,} exact Broader Tag proposal{'s' if count!=1 else ''} waiting in Review Center"
            if count else "No Broader Tag proposal is waiting"
        )

    def _review_current_tag_broader(self):
        proposals=self._pending_broader_proposals(self.current_tag_id)
        if not proposals:
            QMessageBox.information(
                self,"No Broader Tag proposal",
                "No exact reusable-pattern Broader Tag proposal is currently waiting for this SDXL Tag. Run Find New Knowledge to build a current local report.",
            )
            self._update_tag_broader_review_path(self.current_tag_id)
            return
        self._flush_tag_save()
        if self._tag_save_pending:
            self._set_tag_save_status(
                "Wait for the current Tag edit to finish saving before reviewing the Broader Tag proposal.",error=True,
            )
            return
        self.review_broader_proposals_requested.emit(proposals[0].id)

    def _queue_tag_name_save(self,*_):
        if self._loading_tag_editor or not self.current_tag_id or self._category_staging_active():return
        self._tag_save_timer.stop()
        self._tag_save_pending=True
        self._set_tag_save_status("Unsaved name. Save Tag or Close." if getattr(self,"_tag_dialog_batch_edit",False) else "Name change pending; finish editing to save.")

    def _save_tag_control_change(self,*_):
        if self._loading_tag_editor or not self.current_tag_id or self._category_staging_active():return
        if getattr(self,"_tag_dialog_batch_edit",False):
            self._queue_tag_save(); return
        self._tag_save_pending=True; self._set_tag_save_status("Saving changes...")
        try:self._flush_tag_save()
        finally:getattr(self,"_preapproved_alias_removals",set()).clear()

    def _flush_tag_save(self,*_,force=False):
        if getattr(self,"_tag_dialog_batch_edit",False) and not force:return
        if self._loading_tag_editor or not self.current_tag_id or not self._tag_save_pending:return
        self._tag_save_timer.stop(); self._autosave_tag()

    def _autosave_tag(self):
        if self.current_tag_id:
            accepted=self._save_tag(auto=True)
            self._tag_save_pending=self._tag_save_task is not None or not accepted

    def _selected_category_id(self) -> str:
        text=self.tag_category.currentText().strip()
        index=self.tag_category.currentIndex()
        if index>=0 and self.tag_category.itemText(index).strip().casefold()==text.casefold():
            category=str(self.tag_category.itemData(index) or "")
            resolver=self._active_category_resolver()
            if (category and resolver.path_ids(category)
                and resolver.path_label(category).strip().casefold()==text.casefold()):
                return category
        return (self.categories.find_id_by_label(text) or "") if text else ""

    def _tag_save_problem(self,title: str,message: str,*,auto: bool) -> bool:
        self._set_tag_save_status(f"Not saved — {message}",error=True)
        if not auto:QMessageBox.warning(self,title,message)
        return False

    def _review_tag_alias_removal(self,alias: str) -> bool:
        """Review one chip removal before changing its visible saved state."""
        if self._category_staging_active():
            QMessageBox.information(
                getattr(self,"_active_tag_editor_dialog",None) or self,
                "Wait for System Knowledge",
                "The current Category Workspace change is still saving. "
                "Remove the Other Name after it finishes.",
            )
            return False
        record=self.tags.tags.get(self.current_tag_id)
        if record is None or self._tag_alias_service is None:return False
        self._alias_preflight_choice="cancel"
        if not self._confirm_alias_additions(record,(alias,),action="remove"):
            return False
        approved=getattr(self,"_preapproved_alias_removals",set())
        approved.add(normalize_tag(alias))
        self._preapproved_alias_removals=approved
        return True

    def _confirm_alias_additions(self,record,aliases,*,action="attach") -> bool:
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        try:records=index.all_records() if index is not None else None
        except Exception:records=None
        target=(record.display if record is not None else self.tag_name.text().strip())
        target_id=(
            record.id if record is not None
            else f"proposed:{normalize_tag(target)}"
        )
        plan=self._tag_alias_service.preflight(
            tuple(TagAliasProposal(alias,target_id,target) for alias in aliases),
            records=records,action=action,
        )
        dialog_parent=getattr(self,"_active_tag_editor_dialog",None) or self
        dialog=TagAliasPreflightDialog(plan,self.tags,parent=dialog_parent)
        accepted=dialog.exec()==QDialog.Accepted
        self._alias_preflight_choice=dialog.choice
        if accepted:return True
        if dialog.choice=="inspect":
            owner_ids=dialog.current_owner_ids()
            if owner_ids:
                QTimer.singleShot(
                    0,lambda:self._navigate_tag_reference(owner_ids[0]),
                )
                self._set_tag_save_status(
                    "Other Name change paused while its current owner is inspected."
                )
        elif dialog.choice=="merge":
            tag_ids=dialog.merge_tag_ids()
            if tag_ids:
                QTimer.singleShot(
                    0,lambda:self.category_workspace.merge_tag_ids(tag_ids),
                )
                self._set_tag_save_status(
                    "Other Name change paused for duplicate-Tag review."
                )
        elif dialog.choice=="restore":
            removed_ids=dialog.removed_tag_ids()
            QTimer.singleShot(
                0,lambda:self.show_removed_vocabulary(initial_ids=removed_ids),
            )
            self._set_tag_save_status(
                "Other Name change paused for removed-vocabulary review."
            )
        elif dialog.choice=="matching":
            scope=dialog.matching_scope()
            confirmed=(
                plan.entries[0].impact.confirmed_images
                if len(plan.entries)==1 and plan.entries[0].impact.available
                else None
            )
            try:
                self.downstream_updates.record_identity_change(
                    stable_ids=scope["stable_ids"],
                    source_names=scope["source_names"],
                    label=scope["label"],
                    cached_possible_count=confirmed,
                )
            except OSError:
                pass
            else:
                self.downstream_updates_changed.emit()
            if getattr(self,"_active_tag_editor_dialog",None) is not None:
                QTimer.singleShot(
                    0,lambda scope=dict(scope):self._complete_alias_matching_handoff(scope),
                )
            else:
                self.open_dataset.emit(
                    "dataset.image_facts.update_matching",scope,
                )
            self._set_tag_save_status(
                "Other Name removal paused until matching image Tags are reviewed."
            )
        return False

    def _complete_alias_matching_handoff(self,scope) -> None:
        """Leave modal Tag Details before opening the matching-image review."""
        record=self.tags.tags.get(self.current_tag_id)
        if record is not None:
            aliases=self._tag_aliases(record)
            if self._tag_alias_service is not None:
                try:aliases=self._tag_alias_service.effective_for_tag(record.id).aliases
                except (OSError,RuntimeError,ValueError):pass
            self._loading_tag_editor=True
            try:self.tag_aliases.set_values(aliases)
            finally:self._loading_tag_editor=False
        self._tag_relationship_dirty_fields.discard("aliases")
        self._tag_save_timer.stop(); self._tag_save_pending=False
        dialog=getattr(self,"_active_tag_editor_dialog",None)
        handoff=lambda:self.open_dataset.emit(
            "dataset.image_facts.update_matching",dict(scope),
        )
        if dialog is None:
            handoff(); return
        dialog.accept()
        QTimer.singleShot(0,handoff)

    def _review_alias_collision(self,alias,record,owner_ids):
        owners=[self.tags.tags[value] for value in owner_ids if value in self.tags.tags]
        if not owners:return "cancel"
        box=QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Other Name already owned")
        box.setText(f'“{alias}” already identifies another live SDXL Tag.')
        target=(
            f'Proposed owner: “{record.display}”\nStable ID: {record.id}\n'
            f'Category: {self.categories.path_label(record.category)}\n\n'
            if record is not None else
            f'Proposed new Tag: “{self.tag_name.text().strip()}”\n\n'
        )
        owner_text="\n\n".join(
            f'Current owner: “{owner.display}”\nStable ID: {owner.id}\n'
            f'Category: {self.categories.path_label(owner.category)}\n'
            f'Other Names: {", ".join(self._tag_aliases(owner)) or "none"}'
            for owner in owners
        )
        box.setInformativeText(
            target+owner_text+"\n\nChoose only after deciding whether these are the "
            "same identity or two distinct Tags."
        )
        inspect=box.addButton("Inspect Current Owner",QMessageBox.ActionRole)
        merge=(
            box.addButton("Merge Tags…",QMessageBox.ActionRole)
            if record is not None else None
        )
        cancel=box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel); box.exec(); clicked=box.clickedButton()
        if clicked is inspect:
            QTimer.singleShot(0,lambda:self._navigate_tag_reference(owners[0].id))
            return "inspect"
        if merge is not None and clicked is merge:
            tag_ids=(record.id,*(owner.id for owner in owners))
            QTimer.singleShot(0,lambda:self.category_workspace.merge_tag_ids(tag_ids))
            return "merge"
        return "cancel"

    def save_tag(self,*_,on_saved=None):
        return self._save_tag(auto=False,on_saved=on_saved)

    def _save_tag(self,*,auto: bool,on_saved=None) -> bool:
        # Modal alias/identity reviews run a nested event loop. Focus changes
        # must not enter a second save before the first has submitted its work.
        if getattr(self,"_tag_save_preparing",False):return False
        self._tag_save_preparing=True
        try:return self._prepare_tag_save(auto=auto,on_saved=on_saved)
        finally:self._tag_save_preparing=False

    def _prepare_tag_save(self,*,auto: bool,on_saved=None) -> bool:
        if self._tag_save_task is not None:return False
        if self._category_staging_active():return self._tag_save_problem("Wait for System Knowledge","The current Category Workspace change is still saving. Edit the SDXL Tag after it finishes.",auto=auto)
        if not self._flush_inline_character_editor():return self._tag_save_problem("Character Knowledge not saved","Correct the Character Knowledge edit before changing shared SDXL Tag fields.",auto=auto)
        name=self.tag_name.text().strip()
        if not name:return self._tag_save_problem("Name required","Enter the preferred SDXL Tag name.",auto=auto)
        category=self._selected_category_id()
        if not category:return self._tag_save_problem("Category required","Choose an existing category before this change can save.",auto=auto)
        record=copy.deepcopy(self.tags.tags.get(self.current_tag_id)) if self.current_tag_id else None
        if auto and record is None:return self._tag_save_problem("Tag unavailable","This SDXL Tag is no longer available. Select it again before editing.",auto=True)
        created=record is None
        returning_to_image=created and getattr(self,"_active_tag_source_editor",None) is not None
        effective_aliases=self._tag_aliases(record) if record is not None else ()
        if record is not None and self._tag_alias_service is not None:
            try:effective_aliases=self._tag_alias_service.effective_for_tag(record.id).aliases
            except (OSError,RuntimeError,ValueError) as error:
                self._tag_alias_service=None; self._tag_alias_error=str(error)
        previous_identity_group=(record.display,*effective_aliases) if record is not None else ()
        previous_category=record.category if record is not None else ""
        previous_sensitivity=record.content_sensitivity.to_dict() if record is not None else {}
        previous_details=copy.deepcopy(record.to_dict()) if record is not None else {}
        previous_details.pop("content_sensitivity",None)
        previous_effective_rating=(
            effective_tag_sensitivity(record,self.categories)[0]
            if record is not None else ""
        )
        aliases_edited="aliases" in self._tag_relationship_dirty_fields
        requested_aliases=TagCollection(self.tag_aliases.values() if created or "aliases" in self._tag_relationship_dirty_fields else effective_aliases).to_list()
        lifecycle_aliases=([] if aliases_edited else [
            str(item.get("previous_display") or "").strip()
            for item in (record.provenance if record is not None else ())
            if isinstance(item,dict)
            and item.get("action")=="canonical_rename"
            and str(item.get("previous_display") or "").strip()
        ])
        aliases=TagCollection([*requested_aliases,*lifecycle_aliases]).to_list()
        previous_display=record.display if record is not None else ""
        renamed=bool(
            record is not None
            and normalize_tag(previous_display)!=normalize_tag(name)
        )
        if renamed:
            aliases=TagCollection([*aliases,previous_display]).to_list()
        aliases=[
            value for value in aliases
            if normalize_tag(value)!=normalize_tag(name)
        ]
        conflict=self.tags.conflicting_name([name],exclude_id=self.current_tag_id)
        if conflict:
            if auto:
                return self._tag_save_problem(
                    "Name already in use",
                    f'"{name}" already identifies another stable SDXL Tag. '
                    "Discard the invalid name or choose a distinct identity.",
                    auto=True,
                )
            action=self._review_alias_collision(
                name,record,self.tags.identity_owner_ids(name),movable=False,
            )
            if action in {"inspect","merge"}:
                self._set_tag_save_status(
                    "Name change paused while the existing stable identity is reviewed."
                )
                return False
            return self._tag_save_problem("Name already in use",f'"{name}" already identifies "{conflict.display}". Review those stable identities instead of creating an ambiguous duplicate.',auto=auto)
        removed_id=self.tags.removed_identity_id(name) if created else None
        if removed_id:
            identity_service=ConfirmedTagIdentityService(
                self.tags,evidence_rejected=active_automation_engine(
                    self.tags,
                ).is_evidence_rejected,
            )
            plan=identity_service.plan((name,),confirmation=True)
            outcome=review_removed_identities(
                self,identity_service,plan,
                source="qt_knowledge_editor_restoration",
                continuation=(
                    "The restored record will open with its original reviewed "
                    "details. No image data will be changed."
                ),
                confirmation=True,
            )
            if outcome.restored_ids:
                restored_id=outcome.restored_ids[0]
                self._navigate_tag_reference(restored_id)
                restored=self.tags.tags[restored_id]
                self._set_tag_save_status(
                    f"Restored {restored.display} with stable identity "
                    f"{restored.id}. Image data was not changed."
                )
                return True
            if outcome.cancelled or outcome.blocked:
                return False
            return self._tag_save_problem(
                "Removed tag identity",
                "\n".join(
                    entry.message or f"{entry.raw_value}: {entry.status}"
                    for entry in outcome.plan.blockers
                ) or (
                    f'"{name}" matches removed System Knowledge identity '
                    f'"{removed_id}" and was not restored.'
                ),
                auto=auto,
            )
        if created and active_automation_engine(
            self.tags
        ).is_evidence_rejected(name):
            return self._tag_save_problem(
                "Tag ignored by Automation",
                f'"{name}" is covered by an active incoming-evidence '
                "rejection rule. Review that rule in Automation before "
                "creating the reusable tag.",
                auto=auto,
            )
        semantic_dirty={"parent","implies"}.intersection(self._tag_relationship_dirty_fields)
        alias_dirty=bool(aliases_edited or renamed or (created and aliases))
        additions=[]; removals=[]
        if alias_dirty and self._tag_alias_service is None:
            return self._tag_save_problem(
                "Other Names not saved",
                "Canonical Other Name authority is unavailable. Reload System "
                "Knowledge after resolving the relationship-store problem"
                +(f" ({self._tag_alias_error})." if self._tag_alias_error else "."),
                auto=auto,
            )
        if alias_dirty:
            previous_by_key={normalize_tag(value):value for value in effective_aliases}
            desired_by_key={normalize_tag(value):value for value in aliases}
            additions=[desired_by_key[key] for key in desired_by_key if key not in previous_by_key]
            removals=[previous_by_key[key] for key in previous_by_key if key not in desired_by_key]
            self._alias_preflight_choice="cancel"
            # Retaining this identity's own former preferred name introduces no
            # new ownership decision. Explicit new Other Names still get review.
            review_additions=[value for value in additions if not (
                renamed and normalize_tag(value)==normalize_tag(previous_display)
                and self._tag_alias_service.resolve(value).owner_ids==(record.id,)
            )]
            if review_additions and not self._confirm_alias_additions(record,review_additions):
                if self._alias_preflight_choice in {"inspect","merge","restore","matching"}:
                    return False
                return self._tag_save_problem(
                    "Other Names not saved","The Other Name impact review was cancelled.",auto=auto,
                )
            approved_removals=getattr(self,"_preapproved_alias_removals",set())
            unreviewed_removals=[
                value for value in removals
                if normalize_tag(value) not in approved_removals
            ]
            for value in removals:
                approved_removals.discard(normalize_tag(value))
            if unreviewed_removals and not self._confirm_alias_additions(record,unreviewed_removals,action="remove"):
                if self._alias_preflight_choice in {"inspect","merge","restore","matching"}:
                    return False
                return self._tag_save_problem(
                    "Other Names not saved","The Other Name removal review was cancelled.",auto=auto,
                )
        semantic_parent=str(record.parent_id or "") if record is not None else ""
        semantic_implies=tuple(record.implies) if record is not None else ()
        if record is not None and self._tag_semantic_service is not None:
            try:
                semantic=self._tag_semantic_service.effective_for_tag(record.id)
                semantic_parent=semantic.parent_id
                semantic_implies=semantic.implies
            except (OSError,RuntimeError,ValueError) as error:
                self._tag_semantic_service=None; self._tag_semantic_error=str(error)
        if semantic_dirty and self._tag_semantic_service is None:
            return self._tag_save_problem(
                "Relationship not saved",
                "Canonical Broader Tag and Also Means authority is unavailable. "
                "Reload System Knowledge after resolving the relationship-store problem"
                + (f" ({self._tag_semantic_error})." if self._tag_semantic_error else "."),
                auto=auto,
            )
        parent_id=(self.tag_parent.values()[0] if self.tag_parent.values() else None) if created or "parent" in self._tag_relationship_dirty_fields else (semantic_parent or None)
        implies=list(self.tag_implies.values()) if created or "implies" in self._tag_relationship_dirty_fields else list(semantic_implies)
        conflicts=list(record.conflicts) if record is not None else []
        attributes=list(self.tag_attributes.values()) if created or "attributes" in self._tag_relationship_dirty_fields else list(record.attributes)
        if record is not None and {"parent","implies"}.intersection(self._tag_relationship_dirty_fields):
            try:parent_id,implies,_=self.tags.validate_tag_relationships(record.id,parent_id=parent_id,implies=implies,conflicts=self.tag_conflicts.values())
            except ValueError as error:return self._tag_save_problem("Relationship not saved",str(error),auto=auto)
        if "attributes" in self._tag_relationship_dirty_fields:
            if self.tag_attributes.conflict_groups():return self._tag_save_problem("Modifier value not saved","Resolve every single-value modifier slot marked Review required before saving.",auto=auto)
        if record is None:
            draft_database=copy.copy(self.tags)
            draft_database.tags=dict(self.tags.tags)
            draft_database.removed_tag_ids=set(self.tags.removed_tag_ids)
            record=draft_database.add_known_tag(name,category=category,provenance={"source":"qt_knowledge_editor"},rebuild_lookup=False)
        modifier_changes=(
            self.tag_attributes.pending_changes()
            if "attributes" in self._tag_relationship_dirty_fields else ()
        )
        previous_attributes=tuple(record.attributes)
        previous_ordering=(record.ordering.group,record.ordering.priority,record.ordering.reviewed); record.display=name; record.category=category; record.aliases=list(effective_aliases) if alias_dirty else aliases; record.attributes=attributes; record.conflicts=conflicts; record.notes=self.tag_notes.toPlainText().strip(); record.ordering.reviewed=self._tag_ordering_reviewed or self._tag_ordering_edited
        if renamed:
            record.provenance.append({
                "source":"qt_knowledge_editor",
                "action":"canonical_rename",
                "previous_display":previous_display,
                "reviewed_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        if record.ordering.reviewed: record.ordering.group=str(self.tag_order_group.currentData() or default_order_group(record.category)); record.ordering.priority=self.tag_priority.value()
        else: record.ordering.group,record.ordering.priority,_,_=self.categories.effective_ordering(record.category)
        if record.ordering.reviewed and previous_ordering!=(record.ordering.group,record.ordering.priority,record.ordering.reviewed): record.ordering.provenance.append({"source":"qt_knowledge_editor","reviewed_at":datetime.now(timezone.utc).isoformat(timespec="seconds")})
        previous_status=record.content_sensitivity.classification_status; record.content_sensitivity.minimum_rating=str(self.tag_sensitivity.currentData() or ""); record.content_sensitivity.content_flags=_csv(self.tag_content_flags.text())
        if record.content_sensitivity.minimum_rating: record.content_sensitivity.classification_status="reviewed"; record.content_sensitivity.classification_confidence=1.0
        elif previous_status=="intentionally_unclassified": record.content_sensitivity.classification_status=previous_status; record.content_sensitivity.classification_confidence=1.0
        else: record.content_sensitivity.classification_status="unclassified"; record.content_sensitivity.classification_confidence=0.0
        unchanged_details=record.to_dict()
        unchanged_details.pop("content_sensitivity",None)
        rating_fields_only=bool(
            not created and not alias_dirty and not semantic_dirty
            and not modifier_changes and previous_details==unchanged_details
        )
        if rating_fields_only and previous_sensitivity==record.content_sensitivity.to_dict():
            self._set_tag_save_status("Saved automatically.")
            return True
        semantic_result=None; modifier_result=None; modifier_base_id=""; save_impact=TagSaveImpact()
        def complete_save():
            self.current_tag_id=record.id; self._tag_relationship_dirty_fields.clear(); self.tag_attributes.clear_pending_changes()
            self.tag_appearance_editor.refresh_context(tag_id=record.id,category_id=record.category)
            saved_aliases=self._tag_aliases(record)
            if tuple(self.tag_aliases.values())!=saved_aliases:
                self._loading_tag_editor=True
                self.tag_aliases.set_values(saved_aliases)
                self._loading_tag_editor=False
            identity_changed=bool(
                aliases_edited
                and previous_identity_group!=(record.display,*saved_aliases)
            )
            sensitivity_changed=previous_sensitivity!=record.content_sensitivity.to_dict()
            current_details=record.to_dict()
            current_details.pop("content_sensitivity",None)
            sensitivity_only=bool(
                not created and sensitivity_changed and not alias_dirty
                and not semantic_dirty and not modifier_changes
                and previous_details==current_details
            )
            inherited_rating_changed=bool(
                previous_category!=record.category
                and previous_effective_rating!=effective_tag_sensitivity(record,self.categories)[0]
            )
            sensitivity_dependency_changed=bool(
                (
                    created
                    and bool(effective_tag_sensitivity(record,self.categories)[0])
                )
                or identity_changed or sensitivity_changed
                or inherited_rating_changed
            )
            if sensitivity_only:
                # Ratings have their own indexed dependency refresh below. Do not
                # also rebuild rule results and every gallery. Refresh the detached
                # category snapshot off-thread so its next save is not stale.
                self._tag_view_cache=None
                if self.order_workspace is not None and not self.order_workspace.has_unsaved_changes():
                    self._queue_category_workspace_refresh()
            elif alias_dirty or created:
                # Include indexed old/new spellings below before publishing exact
                # identity scope; stable-ID dependencies alone miss legacy names.
                pass
            elif semantic_dirty and semantic_result is not None and not created:
                dependency_tag_ids=tuple(dict.fromkeys(
                    str(value) for value in (
                        record.id,semantic_parent,*semantic_implies,
                        str(parent_id or ""),*implies,
                    ) if str(value)
                ))
                self._changed(
                    defer_workspace=True,
                    dependency_kind="relationship",complete=True,
                    tag_ids=dependency_tag_ids,
                    relationship_ids=tuple(semantic_result.relationship_ids),
                )
            elif modifier_changes and modifier_result is not None and not created:
                dependency_tag_ids=tuple(dict.fromkeys(
                    str(value) for value in (
                        record.id,modifier_base_id,*previous_attributes,*attributes,
                    ) if str(value)
                ))
                self._changed(
                    defer_workspace=True,
                    dependency_kind="relationship",complete=True,
                    tag_ids=dependency_tag_ids,
                    relationship_ids=tuple(modifier_result.relationship_ids),
                )
            elif not created:
                self._changed(
                    defer_workspace=True,dependency_kind="tag_policy",
                    complete=True,tag_ids=(record.id,),
                )
            else:
                self._changed(defer_workspace=True)
            source_editor=getattr(self,"_active_tag_source_editor",None)
            if source_editor is not None and not returning_to_image and not sensitivity_only:
                source_editor.refresh_system_knowledge()
            if sensitivity_dependency_changed:
                self.content_sensitivity_changed.emit({
                    "tag_ids":(record.id,),
                    "identity_groups":(
                        (previous_identity_group,)
                        if identity_changed and previous_identity_group else ()
                    ),
                })
            if not created and not returning_to_image and not sensitivity_only:
                self._refresh_tag_composition(); self._refresh_tag_identity(record.id)
                if previous_category!=record.category and self.categories.effective_semantic_role(record.category) in {"character_identity","character_source","character_outfit"}:
                    self.tag_editor_tabs.setCurrentIndex(self.tag_identity_tab_index)
            if created and not returning_to_image:
                if getattr(self,"_active_tag_editor_dialog",None) is not None:
                    self._tag_editor_record_lock_id=record.id
                self._update_selected_tag_row(record,ensure_visible=True)
                index=self.tag_model.index_for_id(record.id)
                if index.isValid():
                    selection=self.tag_list.selectionModel()
                    with QSignalBlocker(selection):
                        selection.clearSelection()
                        selection.select(index,QItemSelectionModel.Select|QItemSelectionModel.Rows)
                        selection.setCurrentIndex(index,QItemSelectionModel.NoUpdate)
                    self._tag_selection_changed()
                if on_saved is None:
                    self._load_tag_record_into_editor(
                        record,changing_record=True,
                        load_examples=getattr(self,"_active_tag_editor_dialog",None) is None,
                    )
            elif not created:self._update_selected_tag_row(record)
            if created and not returning_to_image:
                for field,editor in (("parent",self.tag_parent),("implies",self.tag_implies)):
                    editor.setEnabled(True); editor.set_context(record.id,lambda values,relation=field:self._tag_relation_candidate_error(relation,values))
                self.tag_conflicts.setEnabled(True)
                self.tag_attributes.setEnabled(True)
                self._set_tag_editor_creation_mode(False)
            elif not created:self.tag_create_button.hide()
            current_ordering=(
                record.ordering.group,record.ordering.priority,
                record.ordering.reviewed,
            )
            category_changed=bool(not created and previous_category!=record.category)
            ordering_changed=bool(not created and previous_ordering!=current_ordering)
            source_names=tuple(dict.fromkeys(
                value for value in (
                    previous_display if renamed else "",*additions,*removals,
                ) if str(value).strip()
            ))
            affected_paths=save_impact.affected_paths
            audit=save_impact.audit
            identity_refresh_paths=save_impact.identity_refresh_paths
            projection_changed=bool(category_changed or ordering_changed)
            if alias_dirty or created:
                self._changed(
                    defer_workspace=True,
                    dependency_kind="tag_identity",complete=affected_paths is not None,
                    tag_ids=(record.id,),images=tuple(dict.fromkeys((
                        *(affected_paths or ()),*identity_refresh_paths,
                    ))),
                )
            settings=getattr(self.state,"settings",None)
            organization_affected=bool(
                projection_changed and OrganizationProfile.from_dict(
                    getattr(settings,"organization_profile",{})
                ).depends_on_tag_order
            )
            impact=OrderImpact(
                changed_categories=(record.category,) if category_changed else (),
                changed_tags=(record.id,) if projection_changed else (),
                affected_tag_ids=(record.id,),affected_tags=1,
                affected_image_paths=affected_paths,
                captions_may_be_stale=projection_changed,
                metadata_may_be_stale=projection_changed,
                organization_affected=organization_affected,
            )
            if created:change_kind="tag_created"
            elif renamed:change_kind="tag_renamed"
            elif alias_dirty:change_kind="tag_aliases"
            elif category_changed:change_kind="tag_membership"
            elif ordering_changed:change_kind="caption_order"
            elif semantic_dirty:change_kind="tag_relationships"
            elif modifier_changes:change_kind="modifier_policy"
            elif sensitivity_changed:change_kind="content_sensitivity_only"
            else:change_kind="tag_details"
            classification=classify_system_knowledge_change(
                change_kind,label=(
                    f'Update matching images after changes to "{record.display}"'
                    if alias_dirty else
                    f'Saved Tag Details for "{record.display}"'
                ),
                affected_tag_ids=(record.id,),source_names=source_names,
                changed_category_ids=(record.category,) if category_changed else (),
                matching_images=alias_dirty,
                cached_matching_image_count=(
                    None if affected_paths is None else len(affected_paths)
                ),impact=impact,
            )
            KnowledgePage._record_downstream_classification(self,classification)
            saved_status=(
                "SDXL Tag created. Future changes save automatically."
                if created else "Tag saved." if getattr(self,"_active_tag_editor_dialog",None) is not None else "Saved automatically."
            )
            self._set_tag_save_status(
                saved_status+" "+classification.completion_summary()
            )
            if renamed:self._offer_rename_image_update(record,previous_display,audit=audit,audit_prepared=True)
            if on_saved is not None:on_saved(record)
            if not created and previous_category!=record.category:
                self._setup_tag_identity(record.id)
            return True

        if sum(bool(value) for value in (modifier_changes,semantic_dirty,alias_dirty))>1:
            return self._tag_save_problem("Could not save SDXL Tag","Finish the current Other Name, semantic relationship, or modifier save before changing another relationship group.",auto=auto)
        modifier_base_id=self._editor_effective_base()[0] if modifier_changes else ""
        mode="aliases" if alias_dirty else "semantics" if semantic_dirty else "modifiers" if modifier_changes else "record"
        changes={
            "aliases":aliases,"parent_id":str(parent_id or ""),"implies":implies,
            "kinds":[kind for field,kind in (("parent",SPECIFICITY),("implies",ENTAILMENT)) if field in semantic_dirty],
            "attributes":attributes,"changes":modifier_changes,"base_tag_id":modifier_base_id,
            "fingerprints":self._loaded_modifier_composition_fingerprints,
        }
        service={"aliases":self._tag_alias_service,"semantics":self._tag_semantic_service,
                 "modifiers":self._modifier_composition_service}.get(mode)
        if mode=="record":service=self._tag_alias_service or self._tag_semantic_service or self._modifier_composition_service
        workspace=(service.workspace if service is not None else getattr(self,"_relationship_workspace",None) or getattr(self.tags,"_app_relationship_workspace",None))
        if workspace is None or (mode!="record" and service is None):
            return self._tag_save_problem("Could not save SDXL Tag","Reload System Knowledge after resolving its relationship-store problem before saving.",auto=auto)
        source_paths=(self.categories.default_path,self.categories.user_path,self.foundation.modifiers_path,self.foundation.concepts_path)
        authorities=TagSaveAuthorities(
            *source_paths,workspace.default_store.path,workspace.user_store.path,
            workspace.default_store._loaded_bytes,workspace.user_store._loaded_bytes,
            tuple((path,owner._loaded_source_fingerprints[path]) for owner in (self.categories,self.foundation) for path in
                  ((owner.default_path,owner.user_path) if owner is self.categories else (owner.modifiers_path,owner.concepts_path))),
        )
        source=getattr(self,"_active_tag_source_editor",None)
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        projection_changed=not created and (
            previous_category!=record.category or previous_ordering!=(
                record.ordering.group,record.ordering.priority,record.ordering.reviewed))
        impact_request=(TagSaveImpactRequest(index,tuple(dict.fromkeys(
            value for value in (name if created else "",previous_display if renamed else "",*aliases,*additions,*removals)
            if str(value).strip())),bool(alias_dirty or created))
            if index is not None and (created or alias_dirty or projection_changed) else None)
        request=TagDetailsSaveRequest(TagRecordSaveRequest(
            self.tags.path,self.tags._loaded_bytes,
            json.dumps(self.tags._record_payload(record) if mode=="record" else record.to_dict(),ensure_ascii=False),created,
            rule_pair_admission=self.tags.rule_pair_admission,
        ),authorities,mode,json.dumps(changes,ensure_ascii=False),source is not None,impact_request,
            prepare_live_delta=created and mode=="record")
        def verified(result):
            nonlocal record,semantic_result,modifier_result,save_impact
            save_impact=result.impact
            if save_impact.snapshot_token is not None:
                try:
                    if (getattr(getattr(self.state,"dataset",None),"index",None) is not index
                        or index.evidence_snapshot_token()!=save_impact.snapshot_token):
                        save_impact=TagSaveImpact()
                except Exception:save_impact=TagSaveImpact()
            record=self.tags.tags[result.record_id]
            source=getattr(self,"_active_tag_source_editor",None)
            prepare=getattr(source,"prepare_system_knowledge_refresh",None)
            if callable(prepare) and isValid(source):prepare(result.palette_index,result.database._loaded_bytes)
            if semantic_dirty:semantic_result=result.branch_result
            if modifier_changes:
                modifier_result=result.branch_result
                self._loaded_modifier_composition_fingerprints=self._modifier_composition_service.effective_for_tag(record.id).fingerprints()
            change=getattr(result,"record_change",None)
            if change is not None:
                self._pending_tag_workspace_rebase=(
                    self.category_workspace.session,change,result.database._loaded_bytes,
                )
            return complete_save()
        return self._start_tag_record_save(request,verified,auto=auto)

    def _adopt_tag_save_authority(self,database,services):
        adopt_live_tag_authority(self.tags,self.categories,self.foundation,database,services)
        for name,value in services.items():setattr(self,"_"+name,value)
        self._tag_view_cache=None

    def _start_tag_record_save(self,request,complete,*,auto):
        """Submit immutable inputs; hold editing until verified GUI adoption."""
        from core.tag_record_save import TagCreatedSaveDelta,prepare_created_tag_adoption_steps
        expected_records=self.tags.tags
        expected_revision=getattr(self.tags,"_relationship_signature_revision",0)
        operation=f"tag-save:{id(self)}"
        begin=getattr(self.state,"begin_long_operation",None)
        if begin is not None:
            acquired,blocking=begin(operation,"Saving SDXL Tag")
            if not acquired:
                return self._tag_save_problem("Tag save is waiting",f"Wait for {blocking or 'the current operation'} to finish, then retry this edit.",auto=auto)
        window=None; was_enabled=True
        try:
            task=AsyncCallTask(lambda:self._tag_save_runner(request))
            self._tag_save_task=task
            self._tag_save_outcome=None
            self._tag_save_pending=True
            self._tag_save_timer.stop()
            window=getattr(self,"_active_tag_editor_dialog",None) or self.window()
            was_enabled=window.isEnabled()
            window.setEnabled(False)
            self._set_tag_save_status("Saving SDXL Tag… Verifying the saved change.")
        except Exception as error:
            self._tag_save_task=None
            self._tag_save_outcome=False
            self._tag_save_pending=True
            if window is not None and isValid(window):window.setEnabled(was_enabled)
            finish=getattr(self.state,"finish_long_operation",None)
            if finish is not None:finish(operation)
            return self._tag_save_problem("Could not save SDXL Tag",f"The save could not start; your draft remains available ({error}).",auto=auto)
        released=False
        def disconnect_finished_callbacks(finished_task):
            if finished_task is None or getattr(finished_task,"_tag_callbacks_disconnected",False):return
            for signal in (finished_task.signals.complete,finished_task.signals.failed):
                try:signal.disconnect()
                except (RuntimeError,TypeError):pass
            finished_task._tag_callbacks_disconnected=True
        def release(success):
            nonlocal released
            if released:return
            released=True
            cleanup=getattr(task,"adoption_cleanup",None)
            if cleanup is not None:cleanup()
            self._tag_save_task=None
            self._tag_save_outcome=success
            self._tag_save_pending=not success
            # Plain closures capture the page and task. Disconnect after the
            # terminal result so hidden Qt trees do not survive as GC cycles.
            for finished_task in (task,getattr(task,"recovery",None)):
                disconnect_finished_callbacks(finished_task)
            if isValid(window):window.setEnabled(was_enabled)
            finish=getattr(self.state,"finish_long_operation",None)
            if finish is not None:finish(operation)
        def report_failure(message):
            release(False)
            if self.editor_context.standalone:self.category_workspace.setEnabled(False)
            self._tag_close_after_save=None
            self._pending_tag_conflict_handoff=None
            if isValid(self):
                self._tag_save_problem("Could not save SDXL Tag",f"The change remains in the editor. Review it and retry after resolving the storage problem ({message}).",auto=auto)
                self.background_work_finished.emit()
        def failed(message):
            if task is not self._tag_save_task:return
            if self.editor_context.standalone:
                report_failure(message)
                return
            # Rebase authority off-thread so a stale rejection has a usable
            # retry. The user's widget draft is deliberately left untouched.
            try:
                recovery=AsyncCallTask(lambda:load_tag_save_authority(
                    request.record.path,request.authorities,
                    rule_pair_admission=request.record.rule_pair_admission))
                def reloaded(result):
                    if task is not self._tag_save_task:return
                    try:
                        from core.tag_record_save import tag_save_authority_is_current
                        if not tag_save_authority_is_current(*result):
                            report_failure(f"{message}; sources changed during recovery. The draft remains available; reload current System Knowledge before retrying.")
                            return
                        if isValid(self):self._adopt_tag_save_authority(*result)
                    except Exception as error:
                        report_failure(f"{message}; authority reload could not be displayed: {error}")
                        return
                    report_failure(message)
                recovery.signals.complete.connect(reloaded)
                def reload_failed(error):
                    if task is self._tag_save_task:report_failure(f"{message}; authority reload also failed: {error}")
                recovery.signals.failed.connect(reload_failed)
                task.recovery=recovery
                self.pool.start(recovery)
            except Exception as error:report_failure(f"{message}; authority reload could not start: {error}")
        def saved(result):
            if task is not self._tag_save_task:return
            if isinstance(result,TagCreatedSaveDelta):
                steps=prepare_created_tag_adoption_steps(self.tags,request,result,
                    expected_records=expected_records,expected_revision=expected_revision)
                timer=QTimer(self)
                timer.setSingleShot(True)
                def cleanup():
                    timer.stop()
                    try:timer.timeout.disconnect()
                    except (RuntimeError,TypeError):pass
                    steps.close()
                    timer.deleteLater()
                    disconnect_finished_callbacks(task)
                    task.adoption_cleanup=None
                task.adoption_cleanup=cleanup
                def advance():
                    if task is not self._tag_save_task:
                        cleanup()
                        return
                    if (not isValid(self) or not isValid(window)
                        or getattr(self,"_category_refresh_shutdown",False)):
                        release(False)
                        return
                    try:
                        deadline=time.perf_counter()+0.008
                        while True:
                            next(steps)
                            if time.perf_counter()>=deadline:break
                    except StopIteration as prepared:
                        cleanup()
                        saved(prepared.value)
                        return
                    except Exception as error:
                        cleanup()
                        failed(f"The saved Tag could not be adopted: {error}")
                        return
                    timer.start(0)
                timer.timeout.connect(advance)
                advance()
                return
            try:
                current=request.record.path.read_bytes()
                relationship_path=request.authorities.relationship_user
                relationship_bytes=relationship_path.read_bytes() if relationship_path.exists() else None
                if current!=result.database._loaded_bytes or relationship_bytes!=result.relationship_bytes:
                    failed("The Tag was saved, then System Knowledge changed again before the editor could refresh. Review the latest record before continuing.")
                    return
                self._adopt_tag_save_authority(result.database,result.services)
                if self._tag_save_adopted is not None:self._tag_save_adopted(self,result)
                release(True)
                if not isValid(self) or not isValid(window):return
                complete(result)
                close_after=self._tag_close_after_save
                self._tag_close_after_save=None
                if close_after is not None and isValid(close_after):close_after.accept()
            except Exception as error:
                if released:
                    if isValid(self):self._set_tag_save_status(f"The Tag was saved. Refreshing the editor needs attention: {error}",error=True)
                else:report_failure(f"The save completed but refreshing the editor failed: {error}")
            finally:
                if isValid(self) and self._tag_save_task is None:self.background_work_finished.emit()
        try:
            task.signals.complete.connect(saved)
            task.signals.failed.connect(failed)
            self.pool.start(task)
        except Exception as error:report_failure(f"The save could not start: {error}")
        return self._tag_save_outcome is not False

    def _offer_rename_image_update(self,record,previous_display,*,audit=None,audit_prepared=False):
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        if audit is None and index is not None and not audit_prepared:
            try:
                audit=ConfirmedTagIdentityService.audit_cached_uses(
                    (record,),index.all_records(),names_for=self.tags.identity_names,
                )
            except Exception:
                audit=None
        prompt=QMessageBox(
            getattr(self,"_active_tag_editor_dialog",None) or self
        )
        prompt.setIcon(QMessageBox.Question)
        prompt.setWindowTitle("Update Tag name on images?")
        if audit is not None and (audit.used_paths or audit.uncertain_paths):
            impact=(
                f"{len(audit.used_paths):,} indexed image "
                f"{'uses' if len(audit.used_paths)==1 else 'use'} this identity; "
                f"{len(audit.uncertain_paths):,} older indexed record"
                f"{' also needs' if len(audit.uncertain_paths)==1 else 's also need'} checking."
            )
        elif audit is not None:
            impact=(
                "The saved index does not currently identify a matching image. "
                "The reviewed Automation check can still inspect the current library "
                "for older spellings or identity links."
            )
        else:
            impact=(
                "The saved library index is not ready, so the affected-image count is "
                "not available. The reviewed Dataset check can inspect the current "
                "library when it is available."
            )
        prompt.setText(
            f'The reusable SDXL Tag was renamed from "{previous_display}" to '
            f'"{record.display}". {impact}'
        )
        prompt.setInformativeText(
            "Update All Images opens the reviewed matching-image correction for "
            "this exact stable identity. It can replace the stored display snapshot "
            "without changing scope, order, decisions, provenance, raw evidence, TXT, "
            "Metadata, source images, or file locations. No scan or write starts in "
            "the background. Not Now keeps the existing image spellings."
        )
        update=prompt.addButton("Update All Images…",QMessageBox.AcceptRole)
        later=prompt.addButton("Not Now",QMessageBox.RejectRole)
        prompt.setDefaultButton(later)
        prompt.exec()
        if prompt.clickedButton() is not update:return
        self.open_dataset.emit("dataset.image_facts.update_matching",{
            "kind":"canonical_rename",
            "origin":"tag_encyclopedia",
            "stable_ids":(record.id,),
            "source_names":(previous_display,),
            "label":f'Update "{previous_display}" to "{record.display}" on all matching images',
            "selected_kind":"tag",
            "selected_id":record.id,
        })

    def _update_selected_tag_row(self,record,*,ensure_visible=False):
        self._tag_view_cache=None
        if ensure_visible:
            # Extend only the category choices and matching result; retain the
            # current query, loaded page and selection without a model reset.
            if self.tag_filter.findData(record.category)<0:
                label=self._category_path_label(record.category); position=1
                while position<self.tag_filter.count() and self.tag_filter.itemText(position).casefold()<label.casefold():
                    position+=1
                with QSignalBlocker(self.tag_filter):self.tag_filter.insertItem(position,label,record.category)
            if not self._tag_matches_filters(
                record,self._tag_search_text(record),self.tag_search.text().strip().casefold(),
                str(self.tag_filter.currentData() or ""),str(self.tag_order_filter.currentData() or ""),
            ):return
        self._tag_derived_anchors=exact_category_anchor_ids(
            self.categories.categories.values(),self._tag_record_map().values(),
        )
        if record.category and not self._category_exists(record.category):status,label="red","Invalid category"
        elif record.category=="uncategorized":status,label="orange","Needs category"
        elif not self.tags.effective_base_tag(
            record,self.categories,derived_anchors=self._tag_derived_anchors,
        )[0] and not record.concept_id and not record.properties:status,label="yellow","Category assigned"
        else:status,label="green","Structured"
        row=self._tag_result_row(record,self._category_path_label(record.category),status,label)
        if ensure_visible:self.tag_model.ensure_visible_row(row)
        else:self.tag_model.update_row(row)
        self._tag_view_cache=None

    def hideEvent(self,event):
        self._flush_tag_save(); super().hideEvent(event)

    def _tag_selection_changed(self,*_):
        selected_ids=self.selected_tag_ids(); count=len(selected_ids); text=f"{count:,} SDXL Tag{'s' if count!=1 else ''} selected" if count else "No SDXL Tags selected"; previous=getattr(self,"_tag_selection_count",None); self._tag_selection_count=count
        staged=self._category_staging_active(); self.tag_selection_status.setText(text); self.tag_selection_status.setAccessibleName(text); self.tag_change_category_button.setEnabled(bool(count and not staged)); self.tag_actions_button.setEnabled(bool(count))
        selection=self.tag_list.selectionModel(); current=self.tag_list.currentIndex(); current_id=self.tag_model.stable_id(current)
        if selected_ids and current_id not in selected_ids:
            first=self.tag_model.index_for_id(selected_ids[0]); selection.setCurrentIndex(first,QItemSelectionModel.NoUpdate)
        multiple=count>1; locked=multiple or staged; self.tag_editor_tabs.setEnabled(not locked); self.tag_multi_selection_notice.setVisible(locked)
        if staged:
            self.tag_multi_selection_notice.setText("A Category Workspace change is saving. Tag results show the pending category state, and single-record editing resumes automatically when the save finishes.")
            self._set_tag_save_status("A Category Workspace change is saving; single-record editing is temporarily paused.")
        elif multiple:
            selected_text=", ".join(self.tags.tags[stable_id].display for stable_id in selected_ids[:3] if stable_id in self.tags.tags)
            remainder=count-3
            if remainder>0:selected_text+=f" and {remainder:,} more"
            self.tag_multi_selection_notice.setText(f"{count:,} SDXL Tags selected: {selected_text}. Single-record fields are unavailable for a multiple selection. Use Change Category to preview one change for exactly these highlighted rows.")
            self._set_tag_save_status("Multiple selection active; no single SDXL Tag will be changed.")
        elif self.current_tag_id and not self._tag_save_pending and not self._tag_editor_record_lock_id:
            self._set_tag_save_status("Changes save automatically.")
        if previous is not None and previous!=count and self.isVisible() and not getattr(self,"_restoring_tag_selection",False):announce_accessibly(self.tag_selection_status,text)

    def _populate_tag_actions(self,menu):
        menu.clear(); selected=self.selected_tag_ids(); count=len(selected); staged=self._category_staging_active()
        change=menu.addAction(f"Change Category for {count:,} SDXL Tag{'s' if count!=1 else ''}…"); change.setEnabled(bool(count and not staged)); change.setToolTip("Wait for the current automatic Category Workspace save." if staged else "Preview one category change for the complete selection."); change.triggered.connect(self.change_selected_tag_categories)
        menu.addSeparator(); find_images=menu.addAction("Open Matching Images in Dataset…"); find_images.setEnabled(count==1); find_images.triggered.connect(self._open_selected_tag_in_dataset)
        copy_tags=menu.addAction("Copy Selected SDXL Tag Name" if count==1 else "Copy Selected SDXL Tag Names"); copy_tags.setEnabled(bool(count)); copy_tags.triggered.connect(self._copy_selected_tag_names)

    def _refresh_tag_actions_menu(self):self._populate_tag_actions(self.tag_actions_menu)

    def _open_selected_tag_in_dataset(self):
        selected=self.selected_tag_ids()
        if len(selected)!=1:return
        record=self._tag_record_map().get(selected[0])
        if record is not None:self.show_dataset_tag.emit(record.display)

    def _copy_selected_tag_names(self):
        records=self._tag_record_map(); values=[records[stable_id].display for stable_id in self.selected_tag_ids() if stable_id in records]
        if values:QApplication.clipboard().setText("\n".join(values))

    def show_tag_context_menu(self,position):
        index=self.tag_list.indexAt(position)
        if not index.isValid():return
        index=index.sibling(index.row(),0); selection=self.tag_list.selectionModel()
        if not selection.isSelected(index):selection.clearSelection(); selection.select(index,QItemSelectionModel.Select|QItemSelectionModel.Rows)
        selection.setCurrentIndex(index,QItemSelectionModel.NoUpdate); self._tag_selection_changed()
        menu=QMenu(self); self._populate_tag_actions(menu); menu.exec(self.tag_list.viewport().mapToGlobal(position))

    def _choose_tag_category_change(self,records):
        dialog=TagCategoryChangeDialog(records,self.categories,self)
        return dialog.target_category_id() if dialog.exec()==QDialog.Accepted else ""

    def change_selected_tag_categories(self):
        selected_ids=tuple(self.selected_tag_ids())
        if not selected_ids:return
        if self._tag_save_pending:
            QMessageBox.information(self,"Current SDXL Tag is still saving","Wait for the current single-record change to finish saving, then choose Change Category again. The batch action did not stage or save anything."); return
        workspace=self.category_workspace
        if workspace.has_unsaved_changes():
            QMessageBox.information(self,"Wait for System Knowledge","The current Category Workspace change is still saving. Start the next category change after it finishes."); self._set_workbench_scope("categories"); return
        records=[workspace.session._state.tags[stable_id] for stable_id in selected_ids if stable_id in workspace.session._state.tags]
        if len(records)!=len(selected_ids):
            QMessageBox.warning(self,"Selection changed","One or more selected SDXL Tags are no longer available. Refresh the results and select them again."); self.refresh_tags(); return
        target=self._choose_tag_category_change(records)
        if not target:return
        if tuple(self.selected_tag_ids())!=selected_ids:
            QMessageBox.warning(self,"Selection changed","The highlighted SDXL Tags changed while the preview was open. Nothing was saved; review the selection and try again."); return
        first_id=selected_ids[0]; workspace.search.clear()
        if not workspace.apply_tag_category_change(selected_ids,target,selected_key=("tag",first_id)):return
        self.knowledge_search.clear(); self._set_workbench_scope("categories"); self.category_views.setCurrentIndex(0)

    def _ensure_order_workspace(self):
        return self.order_workspace is not None

    def show_caption_order(self):
        self._flush_tag_save(); self._set_workbench_scope("categories")
        if self._ensure_order_workspace():self.category_workspace.open_caption_arrangement(("tag",self.current_tag_id) if self.current_tag_id else self.category_workspace.selected_key)

    def _category_view_changed(self,index):
        return

    def has_unsaved_order_changes(self):
        return bool(self.order_workspace is not None and self.order_workspace.has_unsaved_changes())

    def confirm_discard_order_changes(self):
        if self._tag_save_task is not None:
            self._set_tag_save_status("Wait for the verified Tag save to finish before closing.")
            return False
        if self._modifier_policy_save_task is not None:
            QMessageBox.information(
                self,
                "Reusable policy is still saving",
                "Wait for the compatible Base Tag policy batch to finish "
                "saving before closing CollectiGen. The save will not be "
                "cancelled.",
            )
            return False
        if not self.has_unsaved_order_changes():return True
        if self.order_workspace.save_in_progress:
            QMessageBox.information(self,"System Knowledge is still saving","Wait for the current automatic save to finish before closing CollectiGen. The save will not be cancelled."); return False
        if QMessageBox.question(self,"Cancel pending category change?","Close CollectiGen and cancel the pending in-memory Category Workspace change?")!=QMessageBox.Yes:return False
        self.order_workspace.discard_staged_changes(); return True

    def _changed(self,*,defer_workspace=False,workspace_current=False,**dependency):
        self._tag_view_cache=None
        self.state.events.publish("knowledge_changed",**dependency)
        if dependency.get("complete") is True:
            self.scoped_data_changed.emit(dependency)
        else:self.data_changed.emit()
        if workspace_current:return
        if defer_workspace:
            self._queue_category_workspace_refresh()
            return
        if self.order_workspace is not None and not self.order_workspace.has_unsaved_changes():self.order_workspace.reload_from_live()
        self._refresh_review_views()

    def _queue_category_workspace_refresh(self,*,unchanged_snapshot=None,selected_tag_ids=None):
        """Prepare a fresh derived workspace after a nested verified Tag save."""
        if getattr(self,"_category_refresh_shutdown",False):return
        if selected_tag_ids is not None:
            self._category_refresh_selection_ids=tuple(selected_tag_ids)
        elif not getattr(self,"_category_refresh_pending",False):
            self._category_refresh_selection_ids=tuple(
                record.id for record in self.category_workspace._membership_records()
            )
        self._category_refresh_pending=True
        self.workbench.setEnabled(False)
        self.category_workspace.setEnabled(False)
        retry=getattr(self,"_category_refresh_retry",None)
        if retry is not None:retry.hide()
        error_label=getattr(self,"_category_refresh_error",None)
        if error_label is not None:error_label.hide()
        self.header.set_status("Updating the Tag workspace in the background…")
        if getattr(self,"_category_refresh_task",None) is not None:
            self._category_refresh_again=True
            return
        self._category_refresh_again=False
        source=self.category_workspace.session
        rebase=getattr(self,"_pending_tag_workspace_rebase",None)
        self._pending_tag_workspace_rebase=None
        live=(self.tags,self.categories,self.patterns,self.characters)
        proposals=dict(self.sensitivity_proposals)
        def prepare(job=None):
            if job is not None and not job.checkpoint():raise InterruptedError()
            if unchanged_snapshot is not None and source.reload_source_snapshot()==unchanged_snapshot:
                return source,live,(unchanged_snapshot,None,None,None)
            prepared=(source.prepare_verified_tag_rebase(*rebase[1:])
                      if rebase is not None and rebase[0] is source else None)
            if prepared is None:prepared=source.prepare_detached_reload()
            if prepared is None:return source,live,None
            before,session=prepared
            if job is not None and not job.checkpoint():raise InterruptedError()
            structure=prepare_category_structure_search_index(session,session.live_tags)
            views=_knowledge_review_view_sets(session,session.live_tags,proposals,job=job)
            return source,live,(before,session,structure,views)
        task=(CoordinatedCallTask(
            self._background_jobs,"Updating the Tag workspace",prepare,resource="cpu",
        ) if self._background_jobs is not None else AsyncCallTask(prepare))
        self._category_refresh_task=task
        task.signals.complete.connect(lambda result,owner=task:self._category_workspace_refreshed(owner,result))
        task.signals.failed.connect(lambda message,owner=task:self._category_workspace_refresh_failed(owner,message))
        if hasattr(task.signals,"cancelled"):
            task.signals.cancelled.connect(lambda owner=task:self._category_workspace_refresh_failed(owner,"The workspace update was cancelled. Retry to finish loading it."))
        def start():
            if task is not getattr(self,"_category_refresh_task",None):return
            if self._background_jobs is not None:task.start()
            else:QThreadPool.globalInstance().start(task)
        QTimer.singleShot(0,start)

    @staticmethod
    def _release_category_refresh_callbacks(task):
        if task is None or getattr(task,"_category_callbacks_released",False):return
        task._category_callbacks_released=True
        # Terminal callbacks capture their task/page; release them promptly so
        # old complete workspace graphs do not wait for cyclic collection.
        for name in ("complete","failed","cancelled"):
            signal=getattr(task.signals,name,None)
            if signal is not None:
                try:signal.disconnect()
                except (RuntimeError,TypeError):pass

    def _category_workspace_refreshed(self,task,result):
        self._release_category_refresh_callbacks(task)
        if task is not getattr(self,"_category_refresh_task",None):return
        self._category_refresh_task=None
        source,live,prepared=result
        if (getattr(self,"_category_refresh_again",False)
            or any(current is not previous for current,previous in zip(
                (self.tags,self.categories,self.patterns,self.characters),live))
            or self.category_workspace.session is not source
            or prepared is None):
            self._queue_category_workspace_refresh(); return
        fingerprint,session,structure,views=prepared
        try:current=source.reload_source_snapshot()
        except OSError as error:
            self._category_workspace_refresh_failed(None,str(error)); return
        if fingerprint!=current:
            self._queue_category_workspace_refresh(); return
        workspace=self.category_workspace
        if workspace.has_unsaved_changes():
            self._category_workspace_refresh_failed(None,"The workspace changed while its update was loading.")
            return
        selected_tag_ids=getattr(self,"_category_refresh_selection_ids",())
        if session is not None:
            if session.ownership_state=="prepared":
                try:session.activate_verified_tag_rebase(source,current)
                except (OSError,RuntimeError):
                    self._queue_category_workspace_refresh(); return
            session.live_tags,session.live_categories,session.live_patterns,session.characters=live
            workspace.session=session; workspace.sequence.session=session
            workspace._structure_search_cache=(session._state,None,frozenset(),*structure)
            workspace._identity_group_cache={}
            workspace.refresh_all()
            self._refresh_review_views(view_sets=views)
            if workspace.selected_key:
                workspace._show_details(workspace.selected_key,refresh_examples=False)
        self._category_workspace_saved_snapshot=(workspace.session,fingerprint)
        self._category_refresh_pending=False
        workspace.setEnabled(True)
        self.workbench.setEnabled(True)
        if workspace.selected_key and selected_tag_ids:
            selected_names=tuple(
                record.display for tag_id in selected_tag_ids
                if (record:=workspace.session._state.tags.get(tag_id)) is not None
            )
            if selected_names and workspace.membership_chips.selected_values()!=selected_names:
                workspace.membership_chips.select_values(selected_names)
        self._category_refresh_selection_ids=()
        workspace.setToolTip("")
        self.header.set_status("Category edits save automatically · Tag Details uses Save Tag")

    def _category_workspace_refresh_failed(self,task,message):
        self._release_category_refresh_callbacks(task)
        if task is not getattr(self,"_category_refresh_task",None):return
        self._category_refresh_task=None
        self.header.set_status("The Tag was saved, but the workspace update needs a retry.")
        self.category_workspace.setToolTip(str(message))
        error_label=getattr(self,"_category_refresh_error",None)
        if error_label is None:
            error_label=QLabel()
            error_label.setWordWrap(True)
            error_label.setTextFormat(Qt.PlainText)
            error_label.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
            error_label.setAccessibleName("Tag workspace update error")
            self.layout().insertWidget(1,error_label)
            self._category_refresh_error=error_label
        error_label.setText(str(message))
        error_label.show()
        retry=getattr(self,"_category_refresh_retry",None)
        if retry is None:
            retry=QPushButton("Retry Workspace Update")
            retry.clicked.connect(self._queue_category_workspace_refresh)
            self.layout().insertWidget(1,retry)
            self._category_refresh_retry=retry
        retry.show()

    def _record_downstream_classification(self,classification):
        """Publish one typed consequence and persist only its optional jobs."""

        if not isinstance(classification,DownstreamChangeClassification):
            return classification
        self._last_downstream_classification=classification
        signal=getattr(self,"downstream_change_classified",None)
        if signal is not None and hasattr(signal,"emit"):
            signal.emit(classification)
        if not classification.pending_targets:
            return classification
        store=getattr(self,"downstream_updates",None)
        if store is None:
            return classification
        try:store.record_classification(classification)
        except OSError:return classification
        changed=getattr(self,"downstream_updates_changed",None)
        if changed is not None and hasattr(changed,"emit"):changed.emit()
        return classification

    def _category_workspace_saved(self):
        """Publish a workspace save without replacing its persisted undo history."""
        self._reload_modifier_roles()
        self._tag_view_cache=None; self.state.events.publish("knowledge_changed"); self.data_changed.emit(); self.refresh_tags(); self._refresh_review_views()

    def _routine_change_saved(self,change):
        """Keep routine System Knowledge saves local to the consumers they affect."""
        self._tag_view_cache=None
        change=change if isinstance(change,dict) else {}
        kind=str(change.get("kind") or "routine_change")
        impact=change.get("impact")
        matching_scope=dict(change.get("matching_scope") or {})
        classification=change.get("classification")
        if not isinstance(classification,DownstreamChangeClassification):
            labels={
                "caption_order":"Saved caption-order change",
                "tag_membership":"Saved SDXL Tag category change",
                "category_structure":"Saved Category structure change",
                "tag_removal":"Saved SDXL Tag removal",
                "tag_identity_merge":"Saved duplicate SDXL Tag merge",
                "tag_relationships":"Saved SDXL Tag relationship change",
                "category_details":"Saved Category details change",
                "category_sensitivity":"Saved Category sensitivity change",
                "modifier_policy":"Saved reusable modifier policy",
            }
            classification=classify_system_knowledge_change(
                kind,
                label=str(matching_scope.get("label") or labels.get(
                    kind,"Saved System Knowledge change",
                )),
                affected_tag_ids=tuple(
                    matching_scope.get("stable_ids")
                    or change.get("tag_ids",()) or ()
                ),
                source_names=tuple(matching_scope.get("source_names",()) or ()),
                changed_category_ids=tuple(change.get("category_ids",()) or ()),
                matching_images=bool(matching_scope),impact=impact,
            )
        if change.get("kind") in {
            "tag_removal","tag_identity_merge","category_details",
            "category_structure","caption_order",
        }:
            self._reload_modifier_roles()
        if change.get("kind")=="tag_membership":
            tag_ids=tuple(change.get("tag_ids",()))
            self.state.events.publish(
                "tag_membership_changed",tag_ids=tag_ids,complete=True,
            )
            self.content_sensitivity_changed.emit({
                "tag_ids":tuple(change.get("sensitivity_tag_ids",()))
            })
        elif change.get("kind")=="tag_removal":
            tag_ids=tuple(change.get("tag_ids",()))
            self.state.events.publish("knowledge_changed")
            self.data_changed.emit(); self.refresh_tags()
            self.content_sensitivity_changed.emit({
                "tag_ids":tuple(change.get("sensitivity_tag_ids",tag_ids)),
                "identity_groups":tuple(change.get("identity_groups",())),
            })
        elif change.get("kind")=="tag_identity_merge":
            # The merge has already adopted its verified workspace. Preserve
            # donor dependencies and cached name evidence instead of sending
            # a generic notification that discards every image's status.
            tag_ids=tuple(matching_scope.get("stable_ids") or change.get("tag_ids",()))
            self._queue_identity_dependency_refresh(
                tag_ids,tuple(matching_scope.get("source_names",())),
                workspace_current=True,
            )
            if self.tag_scope_page is not None:self.refresh_tags()
            self.content_sensitivity_changed.emit({
                "tag_ids":tag_ids,
                "identity_groups":tuple(change.get("identity_groups",())),
            })
        elif change.get("kind")=="tag_relationships":
            tag_ids=tuple(change.get("tag_ids",()))
            self.state.events.publish(
                "knowledge_changed",dependency_kind="relationship",
                complete=bool(tag_ids),tag_ids=tag_ids,
                relationship_ids=tuple(change.get("relationship_ids",())),
            )
            self._refresh_tag_composition()
        elif change.get("kind")=="content_sensitivity_only":
            self.content_sensitivity_changed.emit({
                "tag_ids":tuple(change.get("tag_ids",())),
                "identity_groups":tuple(change.get("identity_groups",())),
            })
        elif change.get("kind")=="category_details":
            self.state.events.publish("category_details_changed",category_ids=tuple(change.get("category_ids",())))
            dependency_tag_ids=tuple(change.get("tag_ids",()))
            relationship_ids=tuple(change.get("relationship_ids",()))
            if dependency_tag_ids:
                self.state.events.publish(
                    "knowledge_changed",
                    dependency_kind=(
                        "relationship" if relationship_ids else "tag_policy"
                    ),
                    complete=True,tag_ids=dependency_tag_ids,
                    relationship_ids=relationship_ids,
                )
            self._refresh_tag_composition(); self._set_tag_save_status("Saved automatically.")
        elif change.get("kind")=="category_sensitivity":
            tag_ids=tuple(change.get("sensitivity_tag_ids",()))
            category_ids=tuple(change.get("category_ids",()))
            self.state.events.publish("category_sensitivity_changed",category_ids=category_ids)
            self.content_sensitivity_changed.emit({
                "tag_ids":tag_ids,"category_ids":category_ids,
                "identity_groups":tuple(change.get("identity_groups",())),
            })
        elif change.get("kind")=="category_structure":
            tag_ids=tuple(change.get("sensitivity_tag_ids",()))
            category_ids=tuple(change.get("category_ids",()))
            self.state.events.publish("knowledge_changed")
            self.data_changed.emit(); self.refresh_tags()
            self.content_sensitivity_changed.emit({"tag_ids":tag_ids})
        elif change.get("kind")=="modifier_policy":
            tag_ids=tuple(change.get("tag_ids",()))
            self.state.events.publish(
                "modifier_policy_changed",tag_ids=tag_ids,
                complete=bool(tag_ids),
            )
            self.data_changed.emit()
        elif change.get("kind")=="caption_order":
            self.state.events.publish("knowledge_changed")
            self.data_changed.emit(); self.refresh_tags()
        KnowledgePage._record_downstream_classification(self,classification)
        self._refresh_review_views()
